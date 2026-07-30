from __future__ import annotations

from dataclasses import asdict, replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch

import cv2  # type: ignore
import numpy as np  # type: ignore

from v3se_printer.calibration import CalibrationError, ExposureReading, FocusMesh, FocusSample, NormalizedROI
from v3se_printer.editor import EditRecipe
from v3se_printer.nikon import CapturePair, RemoteCapturePair, RemoteFile
from v3se_printer.printer import MotionBounds, Position, PrinterStatus
from v3se_printer.raw import (
    LIBRAW_VERSION,
    RAWPY_VERSION,
    DevelopedRaw,
    RawDevelopmentRecipe,
    RawHeadroomReading,
    WhiteBalance,
)
from v3se_printer.service import (
    CalibrationPlan,
    CalibrationResult,
    ExposureCalibration,
    FocusGridPlan,
    FocusObservation,
    FocusSurfaceCalibration,
    PreviewCaptureError,
    ScanPlan,
    ScannerService,
    ServiceStateError,
    WhiteBalanceCalibration,
    scan_axis,
    scan_centers,
)


class FakePrinter:
    def __init__(self) -> None:
        self.bounds = MotionBounds()
        self.connected = True
        self.initialized = True
        self.faulted = False
        self.position = Position(0.0, 0.0, 10.0)
        self.remembered_position = Position(12.0, 34.0, 56.0)
        self.moves: list[dict[str, float]] = []
        self.stop_count = 0
        self.emergency_count = 0
        self.move_started = threading.Event()
        self.move_release = threading.Event()
        self.block_moves = False

    def status(self) -> PrinterStatus:
        return PrinterStatus(
            self.connected,
            self.initialized,
            self.faulted,
            "/dev/fake",
            115200,
            self.position,
            "Marlin",
            "Fake",
            None,
            self.remembered_position,
        )

    def connect(self, port: str, *, baud: int, eol: str) -> PrinterStatus:
        self.connected = True
        self.initialized = False
        self.faulted = False
        return self.status()

    def disconnect(self) -> PrinterStatus:
        self.connected = False
        self.initialized = False
        self.position = None
        return self.status()

    def home(self) -> Position:
        self.initialized = True
        self.position = Position(0.0, 0.0, 0.0)
        return self.position

    def set_origin(self) -> Position:
        return self.home()

    def restore_remembered_position(self) -> Position:
        if self.remembered_position is None:
            raise RuntimeError("No remembered position")
        self.initialized = True
        self.position = self.remembered_position
        return self.position

    def move_absolute(self, **values: float) -> Position:
        self.moves.append(dict(values))
        self.move_started.set()
        if self.block_moves:
            self.move_release.wait(2.0)
        assert self.position is not None
        self.position = Position(
            values.get("x", self.position.x),
            values.get("y", self.position.y),
            values.get("z", self.position.z),
        )
        return self.position

    def move_relative(self, *, dx: float, dy: float, dz: float, speed_mm_s: float) -> Position:
        assert self.position is not None
        return self.move_absolute(
            x=self.position.x + dx,
            y=self.position.y + dy,
            z=self.position.z + dz,
            speed_mm_s=speed_mm_s,
        )

    def stop(self) -> PrinterStatus:
        self.stop_count += 1
        self.initialized = False
        self.position = None
        self.move_release.set()
        return self.status()

    def emergency_stop(self) -> PrinterStatus:
        self.emergency_count += 1
        self.faulted = True
        self.initialized = False
        self.position = None
        self.move_release.set()
        return self.status()


class FakeCamera:
    def __init__(self) -> None:
        self.control_taken = True
        self.connected = True
        self.latest_jpeg_path: Path | None = None
        self.configurations: list[tuple[str, str]] = []
        self.scan_targets: list[CapturePair] = []
        self.iso = "160"
        self.shutter: str | None = None
        self.configured_profile: str | None = None
        self.latest_capture_profile: str | None = None
        self.calibration_targets: list[Path] = []
        self.preview_targets: list[Path] = []
        self.preview_error: Exception | None = None
        self.capture_storage = "Internal RAM"
        self.remote_scan_targets: list[RemoteCapturePair] = []
        self.deleted_remote_scan_targets: list[RemoteCapturePair] = []
        self.restored_capture_targets: list[str] = []
        self.scan_operations: list[str] = []

    def status(self) -> dict[str, object]:
        return {
            "control_taken": self.control_taken,
            "connected": self.connected,
            "model": "Nikon J4" if self.connected else None,
            "image_quality": None,
            "configured_profile": self.configured_profile,
            "latest_capture_profile": self.latest_capture_profile,
            "iso": self.iso,
            "iso_choices": ("160", "200", "400"),
            "shutter": self.shutter,
            "shutter_choices": self.shutter_choices(),
            "latest_jpeg_path": None if self.latest_jpeg_path is None else str(self.latest_jpeg_path),
        }

    def take_control(self) -> dict[str, object]:
        self.control_taken = True
        return self.status()

    def connect(self) -> dict[str, object]:
        self.connected = True
        return self.status()

    def disconnect(self) -> None:
        self.connected = False
        self.control_taken = False

    def configure(self, shutter: str, *, profile: str) -> dict[str, str]:
        self.configurations.append((shutter, profile))
        self.shutter = shutter
        self.configured_profile = profile
        return {"shutterspeed2": shutter}

    def set_iso(self, iso: str) -> dict[str, object]:
        if iso not in {"160", "200", "400"}:
            raise ValueError("ISO is not available")
        self.iso = iso
        return self.status()

    def shutter_choices(self) -> tuple[str, ...]:
        return ("1/100", "1/50", "1/25", "1")

    def capture_calibration(self, path: str | Path) -> Path:
        target = Path(path)
        target.write_bytes(b"jpeg")
        self.calibration_targets.append(target)
        self.latest_jpeg_path = target
        self.latest_capture_profile = "analysis"
        return target

    def capture_preview(self, path: str | Path) -> Path:
        if self.preview_error is not None:
            raise self.preview_error
        target = Path(path)
        target.write_bytes(b"preview")
        self.preview_targets.append(target)
        self.latest_jpeg_path = target
        self.latest_capture_profile = "preview"
        return target

    def capture_scan(self, jpeg_path: str | Path, nef_path: str | Path) -> CapturePair:
        self.scan_operations.append("local")
        jpeg = Path(jpeg_path)
        nef = Path(nef_path)
        if not cv2.imwrite(str(jpeg), np.zeros((4, 6, 3), dtype=np.uint8)):
            raise RuntimeError("Failed to write fake JPEG")
        nef.write_bytes(b"nef")
        pair = CapturePair(jpeg, nef)
        self.scan_targets.append(pair)
        self.latest_jpeg_path = jpeg
        self.latest_capture_profile = "raw"
        return pair

    def use_camera_storage(self) -> str:
        original = self.capture_storage
        self.capture_storage = "Memory card"
        return original

    def restore_capture_storage(self, target: str) -> None:
        self.scan_operations.append("restore")
        self.restored_capture_targets.append(target)
        self.capture_storage = target

    def capture_scan_to_camera(self) -> RemoteCapturePair:
        self.scan_operations.append("camera")
        index = len(self.remote_scan_targets)
        pair = RemoteCapturePair(
            RemoteFile("/card", f"DSC_{index:04d}.JPG"),
            RemoteFile("/card", f"DSC_{index:04d}.NEF"),
        )
        self.remote_scan_targets.append(pair)
        return pair

    def download_scan(
        self,
        _sources: RemoteCapturePair,
        jpeg_path: str | Path,
        nef_path: str | Path,
    ) -> CapturePair:
        self.scan_operations.append("import")
        jpeg = Path(jpeg_path)
        nef = Path(nef_path)
        if not cv2.imwrite(str(jpeg), np.zeros((4, 6, 3), dtype=np.uint8)):
            raise RuntimeError("Failed to write fake JPEG")
        nef.write_bytes(b"nef")
        pair = CapturePair(jpeg, nef)
        self.scan_targets.append(pair)
        self.latest_jpeg_path = jpeg
        self.latest_capture_profile = "raw"
        return pair

    def delete_scan(self, sources: RemoteCapturePair) -> None:
        self.scan_operations.append("delete")
        self.deleted_remote_scan_targets.append(sources)


def calibration_plan(output_dir: str) -> CalibrationPlan:
    return CalibrationPlan(
        x_min=0.0,
        x_max=10.0,
        y_min=0.0,
        y_max=10.0,
        exposure_roi=NormalizedROI(),
        focus_roi=NormalizedROI(),
        gray_roi=NormalizedROI(),
        output_dir=output_dir,
    )


def focus_grid_plan(output_dir: str) -> FocusGridPlan:
    return FocusGridPlan(
        x_min=0.0,
        x_max=50.0,
        y_min=0.0,
        y_max=34.0,
        focus_roi=NormalizedROI(),
        output_dir=output_dir,
    )


def raw_recipe(balance: WhiteBalance) -> RawDevelopmentRecipe:
    return RawDevelopmentRecipe(
        2,
        balance,
        1.5,
        0.3,
        0.2,
        NormalizedROI(),
        "/tmp/calibration/white_balance.jpg",
        "/tmp/calibration/white_balance.nef",
        ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
    )


def focus_surface(
    mesh: FocusMesh,
    directory: str = "/tmp/calibration",
    *,
    method: str = "grid",
) -> FocusSurfaceCalibration:
    if method == "flat":
        measurement = FocusObservation(
            "single",
            (mesh.x_min + mesh.x_max) / 2.0,
            (mesh.y_min + mesh.y_max) / 2.0,
            mesh.z00,
        )
        return FocusSurfaceCalibration("flat", mesh, (measurement,), directory)
    x0, x1 = mesh.node_xs
    y0, y1 = mesh.node_ys
    measurements = (
        FocusObservation("lower_left", x0, y0, mesh.z_at(x0, y0)),
        FocusObservation("upper_left", x0, y1, mesh.z_at(x0, y1)),
        FocusObservation(
            "center",
            (mesh.x_min + mesh.x_max) / 2.0,
            (mesh.y_min + mesh.y_max) / 2.0,
            mesh.z_at(
                (mesh.x_min + mesh.x_max) / 2.0,
                (mesh.y_min + mesh.y_max) / 2.0,
            ),
        ),
        FocusObservation("lower_right", x1, y0, mesh.z_at(x1, y0)),
        FocusObservation("upper_right", x1, y1, mesh.z_at(x1, y1)),
    )
    return FocusSurfaceCalibration("grid", mesh, measurements, directory)


def calibration_result(
    mesh: FocusMesh,
    balance: WhiteBalance,
    directory: str,
    *,
    shutter: str = "1/100",
    iso: str = "160",
    method: str = "grid",
) -> CalibrationResult:
    return CalibrationResult(
        focus_surface(mesh, directory, method=method),
        shutter,
        ExposureReading(128.0, 240.0, 0.0),
        balance,
        directory,
        iso,
        raw_recipe(balance),
    )


def flat_focus_surface(
    z: float,
    directory: str = "/tmp/calibration",
    *,
    x_min: float = 0.0,
    x_max: float = 50.0,
    y_min: float = 0.0,
    y_max: float = 34.0,
) -> FocusSurfaceCalibration:
    mesh = FocusMesh(x_min, x_max, y_min, y_max, z, z, z, z)
    return focus_surface(mesh, directory, method="flat")


def fake_openexr_helper(*, source_path: Path, output_path: Path) -> Path:
    output_path.write_bytes(b"helper")
    output_path.chmod(0o755)
    return output_path


class ScannerServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.printer = FakePrinter()
        self.camera = FakeCamera()
        self.sleeps: list[float] = []
        self.preview_directory = TemporaryDirectory()
        self.addCleanup(self.preview_directory.cleanup)
        self.openexr_patcher = patch(
            "v3se_printer.service.build_openexr_helper",
            side_effect=fake_openexr_helper,
        )
        self.openexr_build = self.openexr_patcher.start()
        self.addCleanup(self.openexr_patcher.stop)
        self.service = ScannerService(
            self.printer,
            self.camera,
            sleeper=self.sleeps.append,
            preview_dir=self.preview_directory.name,
            scan_root=Path(self.preview_directory.name) / "scans",
        )

    def test_scan_axis_includes_exact_end_without_duplicate(self) -> None:
        self.assertEqual(scan_axis(0.0, 10.0, 5.0), [0.0, 5.0, 10.0])
        self.assertEqual(scan_axis(0.0, 10.0, 6.0), [0.0, 5.0, 10.0])

    def test_editor_apply_runs_without_connected_hardware_and_publishes_revision(self) -> None:
        self.printer.connected = False
        self.printer.initialized = False
        self.camera.connected = False
        project = object()
        with TemporaryDirectory() as directory:
            revision = Path(directory) / "revision-001"
            revision.mkdir()

            def apply(_project: object, _recipe: EditRecipe, **kwargs: object) -> Path:
                kwargs["progress_cb"]("editor-tiles", "Developing", 0, 1, "NEFs")
                kwargs["progress_cb"]("editor-tiles", "Developing", 1, 1, "NEFs")
                return revision

            with patch.object(self.service, "_load_editor_project", return_value=project), patch(
                "v3se_printer.service.apply_editor_revision",
                side_effect=apply,
            ):
                self.service.start_editor_apply("/scan", EditRecipe())
                assert self.service._operation_thread is not None
                self.service._operation_thread.join(1.0)

            status = self.service.status()
            self.assertEqual(status["state"], "idle")
            self.assertEqual(status["editor_result"], {"revision": "revision-001", "directory": str(revision)})
            self.assertEqual(status["step_progress"]["phase"], "editor-tiles")

    def test_editor_apply_can_be_cooperatively_cancelled(self) -> None:
        self.printer.connected = False
        self.camera.connected = False
        started = threading.Event()

        def apply(_project: object, _recipe: EditRecipe, **kwargs: object) -> Path:
            started.set()
            self.service._cancel.wait(1.0)
            kwargs["cancel_cb"]()
            raise AssertionError("Cancellation callback must raise")

        with patch.object(self.service, "_load_editor_project", return_value=object()), patch(
            "v3se_printer.service.apply_editor_revision",
            side_effect=apply,
        ):
            self.service.start_editor_apply("/scan", EditRecipe())
            self.assertTrue(started.wait(1.0))
            self.service.stop()
            assert self.service._operation_thread is not None
            self.service._operation_thread.join(1.0)

        status = self.service.status()
        self.assertEqual(status["state"], "idle")
        self.assertEqual(status["message"], "Stopped")
        self.assertIsNone(status["editor_result"])

    def test_editor_preview_reserves_the_shared_operation_lock(self) -> None:
        project = object()
        started = threading.Event()
        release = threading.Event()
        result: list[bytes] = []

        def render(*_args: object) -> bytes:
            started.set()
            release.wait(1.0)
            return b"preview"

        with patch.object(self.service, "_load_editor_project", return_value=project), patch(
            "v3se_printer.service.render_editor_preview",
            side_effect=render,
        ):
            thread = threading.Thread(
                target=lambda: result.append(self.service.editor_preview("/scan", EditRecipe(), "mosaic", None))
            )
            thread.start()
            self.assertTrue(started.wait(1.0))
            with self.assertRaisesRegex(ServiceStateError, "already running"):
                self.service.start_editor_apply("/scan", EditRecipe())
            release.set()
            thread.join(1.0)

        self.assertEqual(result, [b"preview"])

    def test_scan_axes_cover_bounds_with_measured_footprint_and_overlap(self) -> None:
        footprint_width = 25.0
        footprint_height = 17.0
        overlap = 0.25

        xs = scan_centers(
            0.0,
            100.0,
            footprint_width,
            footprint_width * (1.0 - overlap),
        )
        ys = scan_centers(
            0.0,
            100.0,
            footprint_height,
            footprint_height * (1.0 - overlap),
        )

        self.assertEqual(xs, [12.5, 31.25, 50.0, 68.75, 87.5])
        self.assertEqual(len(ys), 8)
        self.assertEqual((ys[0], ys[-1]), (8.5, 91.5))
        self.assertTrue(all(right - left <= 12.75 for left, right in zip(ys, ys[1:])))
        self.assertEqual(scan_centers(0.0, footprint_width, footprint_width, 18.75), [12.5])
        with self.assertRaisesRegex(ValueError, "at least one camera frame"):
            scan_centers(0.0, 24.9, footprint_width, 18.75)

    def test_calibration_requires_current_position_inside_focus_mesh(self) -> None:
        with TemporaryDirectory() as directory:
            plan = calibration_plan(directory)
            invalid = replace(plan, x_min=1.0)
            with self.assertRaisesRegex(ValueError, "Current exposure position"):
                self.service.start_calibration(invalid)
        self.assertEqual(self.service.status()["state"], "idle")

    def test_normal_stop_finishes_as_stopped_without_stopping_printer(self) -> None:
        with TemporaryDirectory() as directory:
            started = threading.Event()

            def operation(_plan: CalibrationPlan) -> None:
                started.set()
                self.service._cancel.wait(2.0)
                self.service._require_not_cancelled()

            self.service._run_calibration = operation
            self.service.start_calibration(calibration_plan(directory))
            self.assertTrue(started.wait(1.0))
            self.service.stop()
            assert self.service._operation_thread is not None
            self.service._operation_thread.join(1.0)

        status = self.service.status()
        self.assertEqual(status["state"], "idle")
        self.assertEqual(status["message"], "Stopped")
        self.assertIsNone(status["error"])
        self.assertTrue(status["printer"]["connected"])
        self.assertTrue(status["printer"]["initialized"])
        self.assertEqual(self.printer.stop_count, 0)
        with self.assertRaises(ServiceStateError):
            self.service.stop()

    def test_cleanup_error_after_cancel_is_not_reported_as_stopped(self) -> None:
        started = threading.Event()

        def operation(_plan: CalibrationPlan) -> None:
            started.set()
            self.service._cancel.wait(2.0)
            raise RuntimeError("cleanup failed")

        with TemporaryDirectory() as directory:
            self.service._run_calibration = operation
            self.service.start_calibration(calibration_plan(directory))
            self.assertTrue(started.wait(1.0))
            self.service.stop()
            assert self.service._operation_thread is not None
            self.service._operation_thread.join(1.0)

        status = self.service.status()
        self.assertEqual(status["state"], "idle")
        self.assertEqual(status["message"], "Calibration failed")
        self.assertEqual(status["error"], "cleanup failed")
        self.assertEqual(self.printer.stop_count, 0)

    def test_emergency_stop_is_not_overwritten_by_finishing_move(self) -> None:
        self.printer.block_moves = True
        outcome: list[BaseException] = []

        def move() -> None:
            try:
                self.service.move_printer(x=1.0)
            except BaseException as exc:
                outcome.append(exc)

        thread = threading.Thread(target=move)
        thread.start()
        self.assertTrue(self.printer.move_started.wait(1.0))
        self.service.emergency_stop()
        thread.join(1.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcome), 1)
        self.assertEqual(self.service.status()["state"], "faulted")
        self.assertEqual(self.service.status()["message"], "Emergency stop sent")
        self.assertEqual(
            self.service.status()["error"],
            "Reconnect and initialize the printer before further motion",
        )
        self.assertEqual(self.printer.emergency_count, 1)

    def test_failed_emergency_stop_delivery_keeps_service_faulted(self) -> None:
        with patch.object(
            self.printer,
            "emergency_stop",
            side_effect=RuntimeError("M112 write failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "M112 write failed"):
                self.service.emergency_stop()

        status = self.service.status()
        self.assertEqual(status["state"], "faulted")
        self.assertEqual(status["message"], "Emergency stop failed")
        self.assertEqual(status["error"], "M112 write failed")

    def test_normal_stop_interrupts_manual_move(self) -> None:
        self.printer.block_moves = True
        outcome: list[dict[str, object]] = []

        def move() -> None:
            outcome.append(self.service.move_printer(x=1.0))

        thread = threading.Thread(target=move)
        thread.start()
        self.assertTrue(self.printer.move_started.wait(1.0))
        self.service.stop()
        self.assertTrue(thread.is_alive())
        self.printer.move_release.set()
        thread.join(1.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcome), 1)
        self.assertEqual(outcome[0]["state"], "idle")
        self.assertEqual(outcome[0]["message"], "Stopped")
        self.assertIsNone(outcome[0]["error"])
        self.assertEqual(self.printer.stop_count, 0)
        self.assertTrue(self.printer.connected)
        self.assertTrue(self.printer.initialized)
        self.assertEqual(self.camera.preview_targets, [])
        self.assertEqual(self.service.status()["state"], "idle")
        self.assertEqual(self.service.status()["message"], "Stopped")

    def test_manual_move_uses_separate_z_then_xy_speeds(self) -> None:
        self.service.move_printer(x=20.0, y=30.0, z=4.0, speed_xy_mm_s=40.0, speed_z_mm_s=3.0)
        self.assertEqual(
            self.printer.moves,
            [
                {"z": 4.0, "speed_mm_s": 3.0},
                {"x": 20.0, "y": 30.0, "speed_mm_s": 40.0},
            ],
        )
        self.assertEqual(len(self.camera.preview_targets), 1)

    def test_calibrated_xy_move_derives_z_from_focus_mesh(self) -> None:
        mesh = FocusMesh(0.0, 10.0, 0.0, 10.0, 10.0, 11.0, 12.0, 13.0)
        balance = WhiteBalance(1.0, 1.0, 1.0, 1.0)
        self.service._calibration = calibration_result(mesh, balance, "/tmp/calibration")
        self.service._focus_surface = self.service._calibration.focus_surface

        self.service.move_printer(x=10.0, y=10.0, z=99.0)

        expected_z = mesh.z_at(10.0, 10.0)
        self.assertEqual(self.printer.position, Position(10.0, 10.0, expected_z))
        self.assertEqual(self.printer.moves[0], {"z": expected_z, "speed_mm_s": 10.0})

    def test_home_moves_to_capture_position_and_previews(self) -> None:
        self.service.home_printer()

        self.assertEqual(self.printer.position, Position(110.0, 110.0, 203.0))
        self.assertEqual(
            self.printer.moves,
            [
                {"z": 203.0, "speed_mm_s": 10.0},
                {"x": 110.0, "y": 110.0, "speed_mm_s": 200.0},
            ],
        )
        self.assertEqual(len(self.camera.preview_targets), 1)

    def test_motion_succeeds_without_preview_when_camera_is_disconnected(self) -> None:
        self.camera.connected = False

        self.service.move_printer(z=11.0)

        self.assertEqual(self.printer.position, Position(0.0, 0.0, 11.0))
        self.assertEqual(self.camera.preview_targets, [])

    def test_preview_failure_reports_that_motion_already_completed(self) -> None:
        self.camera.preview_error = RuntimeError("camera unavailable")

        with self.assertRaisesRegex(PreviewCaptureError, "Stage command completed"):
            self.service.move_printer(z=11.0)

        status = self.service.status()
        self.assertEqual(self.printer.position, Position(0.0, 0.0, 11.0))
        self.assertEqual(status["state"], "idle")
        self.assertEqual(status["message"], "Stage command completed; preview failed")
        self.assertIn("camera unavailable", status["error"])

    def test_manual_moves_replace_the_previous_lightweight_preview(self) -> None:
        self.service.move_printer(z=11.0)
        first = self.camera.preview_targets[-1]
        self.assertTrue(first.is_file())

        self.service.move_printer(z=12.0)

        self.assertFalse(first.exists())
        self.assertTrue(self.camera.preview_targets[-1].is_file())
        self.assertEqual(self.service.status()["latest_jpeg_path"], str(self.camera.preview_targets[-1]))

    def test_restore_remembered_position_initializes_and_previews(self) -> None:
        self.printer.initialized = False
        self.printer.position = None

        status = self.service.restore_printer_position()

        self.assertTrue(status["printer"]["initialized"])
        self.assertEqual(status["printer"]["position"]["z"], 56.0)
        self.assertEqual(len(self.camera.preview_targets), 1)

    def test_z_jog_uses_z_speed_and_rejects_mixed_axes(self) -> None:
        with self.assertRaisesRegex(ValueError, "separately"):
            self.service.set_jog(1.0, 0.0, 1.0, 30.0, 2.0)

        self.printer.block_moves = True
        self.service.set_jog(0.0, 0.0, 1.0, 30.0, 2.0)
        self.assertTrue(self.printer.move_started.wait(1.0))
        self.service.set_jog(0.0, 0.0, 0.0, 30.0, 2.0)
        self.printer.move_release.set()
        assert self.service._jog_thread is not None
        self.service._jog_thread.join(1.0)

        self.assertFalse(self.service._jog_thread.is_alive())
        self.assertEqual(self.printer.moves[0]["speed_mm_s"], 2.0)
        self.assertAlmostEqual(self.printer.moves[0]["z"], 10.3)
        self.assertEqual(len(self.camera.preview_targets), 1)

    def test_calibrated_xy_jog_derives_z_from_focus_mesh(self) -> None:
        mesh = FocusMesh(0.0, 10.0, 0.0, 10.0, 10.0, 11.0, 12.0, 13.0)
        balance = WhiteBalance(1.0, 1.0, 1.0, 1.0)
        self.service._calibration = calibration_result(mesh, balance, "/tmp/calibration")
        self.service._focus_surface = self.service._calibration.focus_surface
        self.printer.block_moves = True

        self.service.set_jog(1.0, 0.0, 0.0, 10.0, 2.0)
        self.assertTrue(self.printer.move_started.wait(1.0))
        self.service.set_jog(0.0, 0.0, 0.0, 10.0, 2.0)
        self.printer.move_release.set()
        assert self.service._jog_thread is not None
        self.service._jog_thread.join(1.0)

        self.assertFalse(self.service._jog_thread.is_alive())
        self.assertAlmostEqual(self.printer.moves[0]["x"], 1.5)
        self.assertAlmostEqual(self.printer.moves[0]["y"], 0.0)
        self.assertAlmostEqual(self.printer.moves[0]["z"], mesh.z_at(1.5, 0.0))

    def test_stop_during_jog_preview_finishes_as_stopped(self) -> None:
        preview_started = threading.Event()
        release_preview = threading.Event()
        capture_preview = self.camera.capture_preview
        self.printer.block_moves = True

        def block_preview(path: str | Path) -> Path:
            preview_started.set()
            release_preview.wait(1.0)
            return capture_preview(path)

        with patch.object(self.camera, "capture_preview", side_effect=block_preview):
            self.service.set_jog(1.0, 0.0, 0.0, 10.0, 2.0)
            self.assertTrue(self.printer.move_started.wait(1.0))
            self.service.set_jog(0.0, 0.0, 0.0, 10.0, 2.0)
            self.printer.move_release.set()
            self.assertTrue(preview_started.wait(1.0))
            self.service.stop()
            release_preview.set()
            assert self.service._jog_thread is not None
            self.service._jog_thread.join(1.0)

        status = self.service.status()
        self.assertEqual(status["state"], "idle")
        self.assertEqual(status["message"], "Stopped")
        self.assertIsNone(status["error"])
        self.assertEqual(self.printer.stop_count, 0)

    def test_jog_can_restart_after_cooperative_stop(self) -> None:
        self.printer.block_moves = True
        self.printer.move_release.clear()
        self.service.set_jog(1.0, 0.0, 0.0, 1.0, 1.0)
        self.assertTrue(self.printer.move_started.wait(1.0))
        first_thread = self.service._jog_thread
        assert first_thread is not None
        self.service.stop()
        self.printer.move_release.set()
        first_thread.join(1.0)
        self.assertFalse(first_thread.is_alive())
        self.assertEqual(self.service.status()["message"], "Stopped")

        self.printer.move_started.clear()
        self.printer.move_release.clear()
        self.service.set_jog(1.0, 0.0, 0.0, 1.0, 1.0)
        self.assertTrue(self.printer.move_started.wait(1.0))
        second_thread = self.service._jog_thread
        assert second_thread is not None
        self.assertIsNot(second_thread, first_thread)
        self.service.set_jog(0.0, 0.0, 0.0, 1.0, 1.0)
        self.printer.move_release.set()
        second_thread.join(1.0)

        self.assertFalse(second_thread.is_alive())
        self.assertEqual(self.service.status()["state"], "idle")
        self.assertIsNone(self.service.status()["error"])

    def test_stitch_cancel_callback_honors_operation_cancellation(self) -> None:
        cancel_cb = self.service._require_not_cancelled
        cancel_cb()
        self.service._cancel.set()
        with self.assertRaisesRegex(InterruptedError, "cancelled"):
            cancel_cb()

    def test_camera_connect_sets_default_global_shutter_and_analysis_profile(self) -> None:
        self.camera.connected = False

        status = self.service.connect_camera()

        self.assertEqual(self.camera.configurations, [("1/6", "analysis")])
        self.assertEqual(status["camera"]["shutter"], "1/6")
        self.assertEqual(status["camera"]["configured_profile"], "analysis")

    def test_set_shutter_and_test_capture_use_global_analysis_settings(self) -> None:
        with TemporaryDirectory() as directory:
            self.service.set_camera_shutter("1/25")
            self.service.test_camera(directory)

        self.assertEqual(self.camera.configurations, [("1/25", "analysis"), ("1/25", "analysis")])
        self.assertEqual(len(self.camera.calibration_targets), 1)
        self.assertEqual(self.camera.latest_capture_profile, "analysis")

    def test_auto_exposure_reaches_fast_shutters_beyond_eight_captures(self) -> None:
        choices = tuple(f"1/{value}" for value in (16000, 8000, 4000, 2000, 1000, 500, 250, 125, 60, 30))
        self.camera.shutter_choices = lambda: choices
        self.camera.shutter = "1/30"
        readings = [ExposureReading(145.0, 255.0, 0.01)] * 8 + [ExposureReading(128.0, 240.0, 0.0)] * 2
        with TemporaryDirectory() as directory, patch(
            "v3se_printer.service.read_jpeg", return_value=np.zeros((1, 1), dtype=np.uint8)
        ), patch("v3se_printer.service.analyze_exposure", side_effect=readings):
            shutter, reading, pair = self.service._run_auto_exposure(Path(directory), "exposure", NormalizedROI())

        self.assertEqual(shutter, "1/8000")
        self.assertTrue(reading.accepted)
        self.assertIsNone(pair)
        self.assertEqual(len(self.camera.configurations), 10)
        self.assertEqual(len(self.camera.calibration_targets), 10)
        self.assertTrue(self.camera.calibration_targets[-1].name.endswith("_verify.jpg"))

    def test_auto_exposure_measures_both_adjacent_sides_and_selects_closest_ev(self) -> None:
        self.camera.shutter = "1/25"
        readings = [
            ExposureReading(145.0, 246.0, 0.0),
            ExposureReading(112.0, 233.0, 0.0),
            ExposureReading(130.0, 244.0, 0.0),
        ]
        with TemporaryDirectory() as directory, patch(
            "v3se_printer.service.read_jpeg", return_value=np.zeros((1, 1), dtype=np.uint8)
        ), patch("v3se_printer.service.analyze_exposure", side_effect=readings):
            shutter, reading, _pair = self.service._run_auto_exposure(Path(directory), "exposure", NormalizedROI())

        self.assertEqual(shutter, "1/25")
        self.assertEqual(reading, ExposureReading(130.0, 244.0, 0.0))
        self.assertEqual([configuration[0] for configuration in self.camera.configurations], ["1/25", "1/50", "1/25"])
        self.assertEqual(len(self.camera.calibration_targets), 3)
        self.assertEqual(self.service.status()["measurements"][-1]["phase"], "selected")
        self.assertTrue(self.service.status()["measurements"][-1]["accepted"])

    def test_auto_exposure_fills_a_skipped_shutter_before_selecting_bracket(self) -> None:
        self.camera.shutter = "1/100"
        readings = [
            ExposureReading(60.0, 100.0, 0.0),
            ExposureReading(150.0, 246.0, 0.0),
            ExposureReading(125.0, 235.0, 0.0),
            ExposureReading(125.0, 235.0, 0.0),
        ]
        with TemporaryDirectory() as directory, patch(
            "v3se_printer.service.read_jpeg", return_value=np.zeros((1, 1), dtype=np.uint8)
        ), patch("v3se_printer.service.analyze_exposure", side_effect=readings):
            shutter, reading, _pair = self.service._run_auto_exposure(Path(directory), "exposure", NormalizedROI())

        self.assertEqual(shutter, "1/50")
        self.assertEqual(reading, ExposureReading(125.0, 235.0, 0.0))
        self.assertEqual(
            [path.name.split("_search_")[1] for path in self.camera.calibration_targets[:-1]],
            ["00.jpg", "01.jpg", "02.jpg"],
        )
        self.assertEqual([configuration[0] for configuration in self.camera.configurations], ["1/100", "1/25", "1/50", "1/50"])

    def test_auto_exposure_can_select_clipped_jpeg_side_of_bracket(self) -> None:
        self.camera.shutter = "1/25"
        readings = [
            ExposureReading(145.0, 255.0, 0.2),
            ExposureReading(112.0, 233.0, 0.0),
            ExposureReading(130.0, 255.0, 0.2),
        ]
        with TemporaryDirectory() as directory, patch(
            "v3se_printer.service.read_jpeg", return_value=np.zeros((1, 1), dtype=np.uint8)
        ), patch("v3se_printer.service.analyze_exposure", side_effect=readings):
            shutter, reading, _pair = self.service._run_auto_exposure(Path(directory), "exposure", NormalizedROI())

        self.assertEqual(shutter, "1/25")
        self.assertEqual(reading, ExposureReading(130.0, 255.0, 0.2))

    def test_auto_exposure_can_select_nikon_opaque_shutter_step(self) -> None:
        self.camera.shutter_choices = lambda: ("1/2", "Unknown value 0009", "1/4")
        self.camera.shutter = "1/2"
        readings = [
            ExposureReading(150.0, 247.0, 0.0),
            ExposureReading(128.0, 237.0, 0.0000038),
            ExposureReading(128.0, 237.0, 0.0000038),
        ]
        with TemporaryDirectory() as directory, patch(
            "v3se_printer.service.read_jpeg", return_value=np.zeros((1, 1), dtype=np.uint8)
        ), patch("v3se_printer.service.analyze_exposure", side_effect=readings):
            shutter, reading, _pair = self.service._run_auto_exposure(Path(directory), "exposure", NormalizedROI())

        self.assertEqual(shutter, "Unknown value 0009")
        self.assertEqual(reading, ExposureReading(128.0, 237.0, 0.0000038))
        self.assertEqual(
            [configuration[0] for configuration in self.camera.configurations],
            ["1/2", "Unknown value 0009", "Unknown value 0009"],
        )

    def test_auto_exposure_accepts_off_target_verification_with_warning(self) -> None:
        self.camera.shutter = "1/25"
        readings = [ExposureReading(128.0, 240.0, 0.0), ExposureReading(90.0, 220.0, 0.0)]
        with TemporaryDirectory() as directory, patch(
            "v3se_printer.service.read_jpeg", return_value=np.zeros((1, 1), dtype=np.uint8)
        ), patch("v3se_printer.service.analyze_exposure", side_effect=readings):
            shutter, reading, _pair = self.service._run_auto_exposure(Path(directory), "exposure", NormalizedROI())

        self.assertEqual(shutter, "1/25")
        self.assertIn("best available exposure", reading.warning or "")
        measurements = self.service.status()["measurements"]
        self.assertEqual([measurement["phase"] for measurement in measurements], ["search", "verify", "selected"])
        self.assertIn("EV -", measurements[-1]["result"])
        self.assertTrue(measurements[-1]["accepted"])

    def test_raw_verification_increases_underfilled_exposure_to_sensor_target(self) -> None:
        self.camera.shutter_choices = lambda: ("1/13", "10/25", "1/2", "1")
        self.camera.shutter = "1/13"
        readings = [
            ExposureReading(128.0, 147.0, 0.0),
            ExposureReading(139.0, 149.0, 0.0),
            ExposureReading(230.0, 255.0, 0.1),
        ]
        headroom = [
            RawHeadroomReading((0.06, 0.13675214, 0.07, 0.13675214), (0.0, 0.0, 0.0, 0.0)),
            RawHeadroomReading((0.39, 0.89, 0.45, 0.89), (0.0, 0.0, 0.0, 0.0)),
        ]
        with TemporaryDirectory() as directory, patch(
            "v3se_printer.service.read_jpeg", return_value=np.zeros((4, 6, 3), dtype=np.uint8)
        ), patch(
            "v3se_printer.service.analyze_exposure", side_effect=readings
        ), patch(
            "v3se_printer.service.analyze_raw_headroom", side_effect=headroom
        ):
            shutter, reading, pair = self.service._run_auto_exposure(
                Path(directory),
                "exposure",
                NormalizedROI(),
                verify_raw=True,
            )

        self.assertEqual(shutter, "1/2")
        self.assertEqual(reading.raw_highlight_level, 0.89)
        self.assertIsNone(reading.warning)
        assert pair is not None
        self.assertEqual(pair.nef.name, "exposure_optimized.nef")
        self.assertEqual(
            [configuration[0] for configuration in self.camera.configurations],
            ["1/13", "1/13", "1/2"],
        )
        self.assertEqual(
            [measurement["phase"] for measurement in self.service.status()["measurements"]],
            ["search", "verify", "verify RAW", "optimized", "optimized RAW", "selected"],
        )

    def test_raw_optimization_saturation_backs_off_one_discrete_step(self) -> None:
        self.camera.shutter_choices = lambda: ("1/13", "10/25", "1/2", "1")
        self.camera.shutter = "1/13"
        readings = [ExposureReading(128.0, 147.0, 0.0)] * 4
        headroom = [
            RawHeadroomReading((0.06, 0.13675214, 0.07, 0.13675214), (0.0, 0.0, 0.0, 0.0)),
            RawHeadroomReading((0.45, 1.0, 0.5, 1.0), (0.0, 0.02, 0.0, 0.02)),
            RawHeadroomReading((0.36, 0.72, 0.4, 0.72), (0.0, 0.0, 0.0, 0.0)),
        ]
        with TemporaryDirectory() as directory, patch(
            "v3se_printer.service.read_jpeg", return_value=np.zeros((4, 6, 3), dtype=np.uint8)
        ), patch(
            "v3se_printer.service.analyze_exposure", side_effect=readings
        ), patch(
            "v3se_printer.service.analyze_raw_headroom", side_effect=headroom
        ):
            shutter, reading, pair = self.service._run_auto_exposure(
                Path(directory),
                "exposure",
                NormalizedROI(),
                verify_raw=True,
            )

        self.assertEqual(shutter, "10/25")
        self.assertEqual(reading.raw_highlight_level, 0.72)
        self.assertIn("shortened the shutter by one step", reading.warning or "")
        assert pair is not None
        self.assertEqual(pair.nef.name, "exposure_protected.nef")

    def test_raw_target_at_maximum_shutter_is_accepted_with_warning(self) -> None:
        self.camera.shutter = "1"
        readings = [ExposureReading(128.0, 147.0, 0.0)] * 2
        with TemporaryDirectory() as directory, patch(
            "v3se_printer.service.read_jpeg", return_value=np.zeros((4, 6, 3), dtype=np.uint8)
        ), patch(
            "v3se_printer.service.analyze_exposure", side_effect=readings
        ), patch(
            "v3se_printer.service.analyze_raw_headroom",
            return_value=RawHeadroomReading((0.1, 0.2, 0.1, 0.2), (0.0, 0.0, 0.0, 0.0)),
        ):
            shutter, reading, _pair = self.service._run_auto_exposure(
                Path(directory),
                "exposure",
                NormalizedROI(),
                verify_raw=True,
            )

        self.assertEqual(shutter, "1")
        self.assertTrue(reading.accepted)
        self.assertIn("maximum shutter", reading.warning or "")

    def test_raw_saturation_backs_off_once_then_accepts_remaining_high_contrast(self) -> None:
        self.camera.shutter = "1/25"
        readings = [
            ExposureReading(128.0, 255.0, 0.2),
            ExposureReading(128.0, 255.0, 0.2),
            ExposureReading(112.0, 245.0, 0.01),
        ]
        headroom = [
            RawHeadroomReading((1.0, 0.7, 0.6, 0.7), (0.02, 0.0, 0.0, 0.0)),
            RawHeadroomReading((1.0, 0.7, 0.6, 0.7), (0.015, 0.0, 0.0, 0.0)),
        ]
        with TemporaryDirectory() as directory, patch(
            "v3se_printer.service.read_jpeg", return_value=np.zeros((4, 6, 3), dtype=np.uint8)
        ), patch(
            "v3se_printer.service.analyze_exposure", side_effect=readings
        ), patch(
            "v3se_printer.service.analyze_raw_headroom", side_effect=headroom
        ):
            shutter, reading, pair = self.service._run_auto_exposure(
                Path(directory),
                "exposure",
                NormalizedROI(),
                verify_raw=True,
            )

        self.assertEqual(shutter, "1/50")
        self.assertIsNotNone(pair)
        assert pair is not None
        self.assertEqual(pair.nef.name, "exposure_protected.nef")
        self.assertEqual(reading.raw_saturated_fraction, 0.015)
        self.assertIn("shortened the shutter by one step", reading.warning or "")
        self.assertIn("high scene contrast", reading.warning or "")
        self.assertEqual(
            [measurement["phase"] for measurement in self.service.status()["measurements"]],
            ["search", "verify", "verify RAW", "protected", "protected RAW", "selected"],
        )

    def test_raw_saturation_at_fastest_shutter_is_accepted_with_warning(self) -> None:
        self.camera.shutter = "1/100"
        readings = [
            ExposureReading(128.0, 255.0, 0.5),
            ExposureReading(128.0, 255.0, 0.5),
        ]
        with TemporaryDirectory() as directory, patch(
            "v3se_printer.service.read_jpeg", return_value=np.zeros((4, 6, 3), dtype=np.uint8)
        ), patch(
            "v3se_printer.service.analyze_exposure", side_effect=readings
        ), patch(
            "v3se_printer.service.analyze_raw_headroom",
            return_value=RawHeadroomReading((1.0, 0.7, 0.6, 0.7), (0.25, 0.0, 0.0, 0.0)),
        ):
            shutter, reading, _pair = self.service._run_auto_exposure(
                Path(directory),
                "exposure",
                NormalizedROI(),
                verify_raw=True,
            )

        self.assertEqual(shutter, "1/100")
        self.assertEqual(reading.raw_saturated_fraction, 0.25)
        self.assertIn("high scene contrast", reading.warning or "")

    def test_cancellation_during_raw_verification_prevents_protection_capture(self) -> None:
        self.camera.shutter = "1/25"

        def cancel_during_headroom(*_args: object, **_kwargs: object) -> RawHeadroomReading:
            self.service._cancel.set()
            return RawHeadroomReading((1.0, 0.7, 0.6, 0.7), (0.02, 0.0, 0.0, 0.0))

        with TemporaryDirectory() as directory, patch(
            "v3se_printer.service.read_jpeg", return_value=np.zeros((4, 6, 3), dtype=np.uint8)
        ), patch(
            "v3se_printer.service.analyze_exposure",
            return_value=ExposureReading(128.0, 255.0, 0.2),
        ), patch(
            "v3se_printer.service.analyze_raw_headroom",
            side_effect=cancel_during_headroom,
        ), self.assertRaises(InterruptedError):
            self.service._run_auto_exposure(
                Path(directory),
                "exposure",
                NormalizedROI(),
                verify_raw=True,
            )

        self.assertEqual(len(self.camera.scan_targets), 1)
        self.assertEqual(self.camera.scan_targets[0].nef.name, "exposure_verify.nef")

    def test_quick_auto_exposure_starts_from_global_shutter_and_preserves_focus(self) -> None:
        self.camera.shutter = "1/100"
        self.service._focus_surface = flat_focus_surface(9.5)
        with TemporaryDirectory() as directory, patch(
            "v3se_printer.service.read_jpeg", return_value=np.zeros((1, 1), dtype=np.uint8)
        ), patch(
            "v3se_printer.service.analyze_exposure",
            side_effect=[
                ExposureReading(70.0, 120.0, 0.0),
                ExposureReading(128.0, 240.0, 0.0),
                ExposureReading(128.0, 240.0, 0.0),
                ExposureReading(128.0, 240.0, 0.0),
                ExposureReading(128.0, 240.0, 0.0),
            ],
        ), patch(
            "v3se_printer.service.analyze_raw_headroom",
            return_value=RawHeadroomReading((0.9, 0.7, 0.6, 0.7), (0.0, 0.0, 0.0, 0.0)),
        ):
            self.service.start_auto_exposure(NormalizedROI(), directory)
            assert self.service._operation_thread is not None
            self.service._operation_thread.join(1.0)
            exposure_root = sorted(Path(directory).glob("exposure_*"))[-1]
            exposure_payload = json.loads((exposure_root / "exposure.json").read_text(encoding="utf-8"))
            self.service.start_auto_exposure(NormalizedROI(), directory)
            assert self.service._operation_thread is not None
            self.service._operation_thread.join(1.0)

        status = self.service.status()
        quick = status["quick_calibration"]
        self.assertEqual(status["state"], "idle")
        self.assertEqual(status["camera"]["shutter"], "1/25")
        self.assertEqual(quick["exposure"]["shutter"], "1/25")
        self.assertEqual(exposure_payload["capture_profile"], "raw")
        self.assertEqual(quick["focus_z"], 9.5)
        self.assertIsNone(quick["white_balance"])
        self.assertEqual(
            [(measurement["phase"], measurement["profile"], measurement["accepted"]) for measurement in status["measurements"]],
            [
                ("search", "analysis", False),
                ("search", "analysis", True),
                ("verify", "raw", True),
                ("verify RAW", "raw", True),
                ("selected", "raw", True),
                ("search", "analysis", True),
                ("verify", "raw", True),
                ("verify RAW", "raw", True),
                ("selected", "raw", True),
            ],
        )
        self.assertEqual([measurement["sequence"] for measurement in status["measurements"]], list(range(1, 10)))

    def test_cancelled_auto_exposure_clears_only_exposure_and_does_not_stop_printer(self) -> None:
        started = threading.Event()
        self.service._focus_surface = flat_focus_surface(9.5)

        def operation(_roi: NormalizedROI, _output: str) -> None:
            started.set()
            self.service._cancel.wait(1.0)
            self.service._require_not_cancelled()

        self.service._run_quick_auto_exposure = operation
        with TemporaryDirectory() as directory:
            self.service.start_auto_exposure(NormalizedROI(), directory)
            self.assertTrue(started.wait(1.0))
            self.service.stop()
            assert self.service._operation_thread is not None
            self.service._operation_thread.join(1.0)

        self.assertEqual(self.printer.stop_count, 0)
        quick = self.service.status()["quick_calibration"]
        self.assertIsNone(quick["exposure"])
        self.assertEqual(quick["focus_z"], 9.5)
        self.assertEqual(self.service.status()["state"], "idle")

    def test_auto_exposure_finishing_after_stop_cannot_publish_result(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def exposure(*_args: object, **_kwargs: object) -> tuple[str, ExposureReading, None]:
            started.set()
            release.wait(1.0)
            return "1/50", ExposureReading(128.0, 240.0, 0.0), None

        self.service._run_auto_exposure = exposure
        with TemporaryDirectory() as directory:
            self.service.start_auto_exposure(NormalizedROI(), directory)
            self.assertTrue(started.wait(1.0))
            self.service.stop()
            release.set()
            assert self.service._operation_thread is not None
            self.service._operation_thread.join(1.0)

        self.assertIsNone(self.service.status()["quick_calibration"])
        self.assertEqual(self.service.status()["state"], "idle")

    def test_non_printer_calibration_error_returns_to_idle_with_cause(self) -> None:
        self.camera.shutter = "1/50"

        def operation(_roi: NormalizedROI, _output: str) -> None:
            raise RuntimeError("focus peak missing")

        self.service._run_quick_auto_exposure = operation
        with TemporaryDirectory() as directory:
            self.service.start_auto_exposure(NormalizedROI(), directory)
            assert self.service._operation_thread is not None
            self.service._operation_thread.join(1.0)

        status = self.service.status()
        self.assertEqual(status["state"], "idle")
        self.assertEqual(status["message"], "Calibration failed")
        self.assertEqual(status["error"], "focus peak missing")
        self.assertFalse(status["printer"]["faulted"])
        self.service.set_camera_shutter("1/25")

    def test_shutdown_does_not_stop_printer_during_camera_only_calibration(self) -> None:
        started = threading.Event()

        def operation(_roi: NormalizedROI, _output: str) -> None:
            started.set()
            self.service._cancel.wait(1.0)
            self.service._require_not_cancelled()

        self.service._run_quick_auto_exposure = operation
        with TemporaryDirectory() as directory:
            self.service.start_auto_exposure(NormalizedROI(), directory)
            self.assertTrue(started.wait(1.0))
            self.service.shutdown()

        self.assertEqual(self.printer.stop_count, 0)
        self.assertFalse(self.printer.connected)
        self.assertFalse(self.camera.connected)

    def test_shutdown_does_not_stop_printer_during_scan_postprocessing(self) -> None:
        started = threading.Event()

        def operation(_plan: ScanPlan) -> None:
            started.set()
            self.service._cancel.wait(1.0)
            self.service._require_not_cancelled()

        with TemporaryDirectory() as directory, patch.object(
            self.service,
            "_validate_scan_plan",
        ), patch.object(self.service, "_run_validated_scan", side_effect=operation):
            plan = ScanPlan(0.0, 25.0, 0.0, 17.0, 25.0, 17.0, 25.0, directory)
            self.service.start_scan(plan)
            self.assertTrue(started.wait(1.0))
            self.service.shutdown()

        self.assertEqual(self.printer.stop_count, 0)
        self.assertFalse(self.printer.connected)
        self.assertFalse(self.camera.connected)

    def test_quick_auto_focus_defaults_to_current_z_and_updates_status(self) -> None:
        self.camera.shutter = "1/50"
        samples = [FocusSample(float(index), float(index + 1)) for index in range(8)]
        with TemporaryDirectory() as directory, patch(
            "v3se_printer.service.run_focus_sweep", return_value=(10.25, samples)
        ) as sweep:
            self.service.start_auto_focus(
                NormalizedROI(),
                directory,
                x_min=0.0,
                x_max=50.0,
                y_min=0.0,
                y_max=34.0,
                speed_z_mm_s=10.0,
            )
            assert self.service._operation_thread is not None
            self.service._operation_thread.join(1.0)

        self.assertEqual(sweep.call_args.kwargs["start_z"], 10.0)
        self.assertEqual(sweep.call_args.kwargs["z_min"], 0.0)
        self.assertEqual(sweep.call_args.kwargs["z_max"], 250.0)
        self.assertEqual(self.camera.configurations[-1], ("1/50", "analysis"))
        self.assertEqual(self.service.status()["quick_calibration"]["focus_z"], 10.25)
        self.assertTrue(self.camera.calibration_targets[-1].name.startswith("focus_selected_z10.2500"))

    def test_single_auto_focus_creates_flat_surface_and_authorizes_scan(self) -> None:
        self.camera.shutter = "1/50"
        self.printer.position = Position(20.0, 12.0, 10.0)
        reading = ExposureReading(128.0, 240.0, 0.0)
        balance = WhiteBalance(1.0, 1.0, 1.0, 1.0)
        recipe = raw_recipe(balance)
        self.service._exposure_calibration = ExposureCalibration(
            "1/50",
            reading,
            "160",
            "/tmp/exposure",
        )
        self.service._white_balance_calibration = WhiteBalanceCalibration(
            balance,
            "1/50",
            "160",
            "/tmp/white-balance",
            recipe,
        )

        with TemporaryDirectory() as directory, patch(
            "v3se_printer.service.run_focus_sweep",
            return_value=(10.25, [FocusSample(10.25, 100.0)]),
        ):
            self.service.start_auto_focus(
                NormalizedROI(),
                directory,
                x_min=0.0,
                x_max=50.0,
                y_min=0.0,
                y_max=34.0,
            )
            assert self.service._operation_thread is not None
            self.service._operation_thread.join(1.0)

            status = self.service.status()
            surface = status["focus_grid"]
            self.assertEqual(surface["method"], "flat")
            self.assertEqual(
                surface["measurements"],
                [{"name": "single", "x": 20.0, "y": 12.0, "z": 10.25}],
            )
            self.assertEqual(
                {surface["mesh"][name] for name in ("z00", "z10", "z01", "z11")},
                {10.25},
            )
            self.assertEqual(status["calibration"]["focus_method"], "flat")
            saved = json.loads(
                (Path(surface["directory"]) / "focus.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                saved["coverage"],
                {"x_min": 0.0, "x_max": 50.0, "y_min": 0.0, "y_max": 34.0},
            )

            plan = ScanPlan(0.0, 50.0, 0.0, 34.0, 25.0, 17.0, 25.0, directory)
            with patch.object(self.service, "_run_scan") as run_scan:
                self.service.start_scan(plan)
                assert self.service._operation_thread is not None
                self.service._operation_thread.join(1.0)
            run_scan.assert_called_once_with(plan)

    def test_single_auto_focus_rejects_current_position_outside_scan_bounds(self) -> None:
        self.camera.shutter = "1/50"
        with TemporaryDirectory() as directory, self.assertRaisesRegex(
            ValueError,
            "inside the scan bounds",
        ):
            self.service.start_auto_focus(
                NormalizedROI(),
                directory,
                x_min=1.0,
                x_max=50.0,
                y_min=1.0,
                y_max=34.0,
            )

        self.assertIsNone(self.service._operation_thread)
        self.assertEqual(self.camera.calibration_targets, [])

    def test_focus_grid_fits_all_five_observations_and_reports_center(self) -> None:
        self.camera.shutter = "1/50"
        grid_peaks = [10.0, 14.0, 15.0, 12.0, 16.0]
        grid_results = [
            (peak, [FocusSample(peak, 100.0)])
            for peak in grid_peaks
        ]

        with TemporaryDirectory() as directory, patch(
            "v3se_printer.service.run_focus_sweep",
            side_effect=grid_results,
        ) as grid_sweep:
            self.service.start_focus_grid(focus_grid_plan(directory))
            assert self.service._operation_thread is not None
            self.service._operation_thread.join(2.0)

            grid = self.service.status()["focus_grid"]
            self.assertIsNotNone(grid)
            self.assertEqual(grid["method"], "grid")
            self.assertAlmostEqual(grid["mesh"]["z00"], 10.4)
            self.assertAlmostEqual(grid["mesh"]["z10"], 12.4)
            self.assertAlmostEqual(grid["mesh"]["z01"], 14.4)
            self.assertAlmostEqual(grid["mesh"]["z11"], 16.4)
            self.assertNotEqual(grid["mesh"]["z00"], grid_peaks[0])
            self.assertEqual(
                [measurement["name"] for measurement in grid["measurements"]],
                ["lower_left", "upper_left", "center", "lower_right", "upper_right"],
            )
            self.assertIsNone(self.service.status()["calibration"])
            self.assertEqual(
                [
                    (move["x"], move["y"])
                    for move in self.printer.moves
                    if "x" in move and "y" in move
                ],
                [(12.5, 8.5), (12.5, 25.5), (25.0, 17.0), (37.5, 8.5), (37.5, 25.5)],
            )
            self.assertEqual(len(grid_sweep.call_args_list), 5)
            saved = json.loads((Path(grid["directory"]) / "focus_grid.json").read_text(encoding="utf-8"))
            self.assertEqual(
                [(item["name"], item["x"], item["y"], item["z"]) for item in saved["measurements"]],
                [
                    ("lower_left", 12.5, 8.5, 10.0),
                    ("upper_left", 12.5, 25.5, 14.0),
                    ("center", 25.0, 17.0, 15.0),
                    ("lower_right", 37.5, 8.5, 12.0),
                    ("upper_right", 37.5, 25.5, 16.0),
                ],
            )

    def test_focus_grid_uses_horizontal_route_for_tall_coverage(self) -> None:
        self.camera.shutter = "1/50"
        plan = FocusGridPlan(
            0.0,
            34.0,
            0.0,
            50.0,
            NormalizedROI(),
            "/tmp/focus-grid",
        )

        with TemporaryDirectory() as directory, patch(
            "v3se_printer.service.run_focus_sweep",
            return_value=(10.0, [FocusSample(10.0, 100.0)]),
        ):
            self.service.start_focus_grid(replace(plan, output_dir=directory))
            assert self.service._operation_thread is not None
            self.service._operation_thread.join(2.0)

        self.assertEqual(
            [
                (move["x"], move["y"])
                for move in self.printer.moves
                if "x" in move and "y" in move
            ],
            [(8.5, 12.5), (25.5, 12.5), (17.0, 25.0), (8.5, 37.5), (25.5, 37.5)],
        )

    def test_auto_focus_search_decisions_are_logged_with_samples(self) -> None:
        self.camera.shutter = "1/50"

        def sweep(**kwargs: object) -> tuple[float, list[FocusSample]]:
            sample = FocusSample(10.0, 200.0)
            kwargs["on_event"]("coarse", "Peak not bracketed; expanding both directions", None)
            kwargs["on_sample"]("coarse", 0, sample)
            kwargs["on_event"]("fine", "Resolved peak Z 10.1250 mm with 40.0% endpoint prominence", None)
            return 10.125, [sample]

        with TemporaryDirectory() as directory, patch(
            "v3se_printer.service.run_focus_sweep", side_effect=sweep
        ):
            self.service.start_auto_focus(
                NormalizedROI(),
                directory,
                x_min=0.0,
                x_max=50.0,
                y_min=0.0,
                y_max=34.0,
            )
            assert self.service._operation_thread is not None
            self.service._operation_thread.join(1.0)

        measurements = self.service.status()["measurements"]
        self.assertEqual(
            [measurement["phase"] for measurement in measurements],
            ["coarse search", "coarse", "fine search", "selected"],
        )
        self.assertIn("expanding both directions", measurements[0]["result"])
        self.assertIsNone(measurements[0]["accepted"])
        self.assertIn("endpoint prominence", measurements[2]["result"])

    def test_quick_auto_focus_requires_global_shutter(self) -> None:
        with TemporaryDirectory() as directory, self.assertRaisesRegex(ServiceStateError, "global shutter"):
            self.service.start_auto_focus(
                NormalizedROI(),
                directory,
                x_min=0.0,
                x_max=50.0,
                y_min=0.0,
                y_max=34.0,
            )

    def test_full_calibration_fits_quadrants_and_center_with_monotonic_progress(self) -> None:
        self.camera.shutter = "1/50"
        peaks = [10.0, 14.0, 15.0, 12.0, 16.0]
        sweep_results = [
            (peak, [FocusSample(peak, 100.0)])
            for peak in peaks
        ]
        balance = WhiteBalance(1.2, 1.0, 1.4, 1.0)
        with TemporaryDirectory() as directory, patch(
            "v3se_printer.service.run_focus_sweep", side_effect=sweep_results
        ) as sweep, patch(
            "v3se_printer.service.read_jpeg",
            return_value=np.zeros((4, 6, 3), dtype=np.uint8),
        ), patch(
            "v3se_printer.service.analyze_exposure",
            return_value=ExposureReading(128.0, 240.0, 0.0),
        ), patch(
            "v3se_printer.service.analyze_raw_headroom",
            return_value=RawHeadroomReading((0.9, 0.7, 0.6, 0.7), (0.0, 0.0, 0.0, 0.0)),
        ), patch(
            "v3se_printer.service.calibrate_white_balance", return_value=balance
        ), patch(
            "v3se_printer.service.calibrate_development_recipe", return_value=raw_recipe(balance)
        ), patch.object(
            self.service,
            "_set_step_progress",
            wraps=self.service._set_step_progress,
        ) as progress:
            self.service.start_calibration(calibration_plan(directory))
            assert self.service._operation_thread is not None
            self.service._operation_thread.join(2.0)
            calibration_root = Path(self.service.status()["calibration"]["directory"])
            calibration_payload = json.loads((calibration_root / "calibration.json").read_text(encoding="utf-8"))

        status = self.service.status()
        self.assertEqual(status["state"], "idle")
        self.assertEqual(status["message"], "Completed")
        self.assertAlmostEqual(status["calibration"]["mesh"]["z00"], 10.4)
        self.assertAlmostEqual(status["calibration"]["mesh"]["z10"], 12.4)
        self.assertAlmostEqual(status["calibration"]["mesh"]["z01"], 14.4)
        self.assertAlmostEqual(status["calibration"]["mesh"]["z11"], 16.4)
        self.assertEqual(len(self.camera.scan_targets), 1)
        self.assertEqual(self.camera.scan_targets[0].jpeg.name, "final_exposure_verify.jpg")
        self.assertEqual(
            calibration_payload["result"]["capture_profiles"],
            {
                "preliminary_exposure": "analysis",
                "final_exposure": "raw",
                "focus": "analysis",
                "white_balance": "raw",
            },
        )
        self.assertEqual([call.kwargs["start_z"] for call in sweep.call_args_list], [10.0] * 5)
        self.assertTrue(all(call.kwargs["z_min"] == 0.0 for call in sweep.call_args_list))
        self.assertTrue(all(call.kwargs["z_max"] == 250.0 for call in sweep.call_args_list))
        self.assertEqual(
            [
                (move["x"], move["y"])
                for move in self.printer.moves
                if "x" in move and "y" in move
            ][:5],
            [(2.5, 2.5), (2.5, 7.5), (5.0, 5.0), (7.5, 2.5), (7.5, 7.5)],
        )
        updates = [call.args for call in progress.call_args_list]
        starts = [
            update
            for index, update in enumerate(updates)
            if index == 0 or update[0] != updates[index - 1][0]
        ]
        self.assertEqual(
            [update[0] for update in starts],
            [
                "pre_exposure-exposure-search",
                "pre_exposure-exposure-verify",
                "focus-grid-1",
                "focus-grid-2",
                "focus-grid-3",
                "focus-grid-4",
                "focus-grid-5",
                "focus-fit",
                "final_exposure-exposure-search",
                "final_exposure-exposure-verify",
                "white-balance-capture",
                "white-balance-gains",
                "raw-recipe",
            ],
        )
        self.assertTrue(all(update[2] == 0 for update in starts))
        self.assertTrue(
            all(
                update[3] is None
                for update in starts
                if update[0].startswith("focus-grid-")
            )
        )

    def test_quick_white_balance_uses_global_shutter_without_exposure_gate(self) -> None:
        balance = WhiteBalance(1.2, 1.0, 1.4, 1.0)
        self.camera.shutter = "1/50"
        with TemporaryDirectory() as directory, patch(
            "v3se_printer.service.calibrate_white_balance", return_value=balance
        ), patch(
            "v3se_printer.service.calibrate_development_recipe", return_value=raw_recipe(balance)
        ):
            self.service.start_white_balance(NormalizedROI(), directory)
            assert self.service._operation_thread is not None
            self.service._operation_thread.join(1.0)

        self.assertEqual(self.camera.configurations[-1], ("1/50", "raw"))
        self.assertEqual(self.service.status()["quick_calibration"]["white_balance"]["red"], 1.2)

    def test_reused_exposure_pair_honors_cancellation_before_white_balance(self) -> None:
        self.service._cancel.set()
        pair = CapturePair(Path("verification.jpg"), Path("verification.nef"))
        with TemporaryDirectory() as directory, patch(
            "v3se_printer.service.read_jpeg"
        ) as read, self.assertRaises(InterruptedError):
            self.service._calibrate_white_balance(
                Path(directory),
                NormalizedROI(),
                "1/50",
                pair=pair,
            )

        read.assert_not_called()

    def test_white_balance_cancellation_skips_raw_recipe(self) -> None:
        balance = WhiteBalance(1.2, 1.0, 1.4, 1.0)
        pair = CapturePair(Path("verification.jpg"), Path("verification.nef"))

        def cancel_after_balance(*_args: object, **_kwargs: object) -> WhiteBalance:
            self.service._cancel.set()
            return balance

        with TemporaryDirectory() as directory, patch(
            "v3se_printer.service.read_jpeg",
            return_value=np.zeros((4, 6, 3), dtype=np.uint8),
        ), patch(
            "v3se_printer.service.calibrate_white_balance",
            side_effect=cancel_after_balance,
        ), patch(
            "v3se_printer.service.calibrate_development_recipe",
        ) as recipe, self.assertRaises(InterruptedError):
            self.service._calibrate_white_balance(
                Path(directory),
                NormalizedROI(),
                "1/50",
                pair=pair,
            )

        recipe.assert_not_called()

    def test_global_iso_change_clears_exposure_without_discarding_focus(self) -> None:
        self.service._exposure_calibration = ExposureCalibration(
            "1/50",
            ExposureReading(128.0, 240.0, 0.0),
            "160",
            "/tmp/exposure",
        )
        self.service._focus_surface = flat_focus_surface(10.0)
        self.service.set_camera_iso("400")

        self.assertEqual(self.camera.iso, "400")
        quick = self.service.status()["quick_calibration"]
        self.assertIsNone(quick["exposure"])
        self.assertEqual(quick["focus_z"], 10.0)

    def test_faulted_printer_can_disconnect_then_reconnect_uninitialized(self) -> None:
        self.service.emergency_stop()
        disconnected = self.service.disconnect_printer()
        self.assertEqual(disconnected["state"], "idle")
        self.assertFalse(disconnected["printer"]["connected"])

        connected = self.service.connect_printer("/dev/fake")
        self.assertTrue(connected["printer"]["connected"])
        self.assertFalse(connected["printer"]["faulted"])
        self.assertFalse(connected["printer"]["initialized"])

    def test_coordinate_and_camera_session_changes_invalidate_calibration(self) -> None:
        mesh = FocusMesh(0.0, 10.0, 0.0, 10.0, 10.0, 10.0, 10.0, 10.0)
        balance = WhiteBalance(1.0, 1.0, 1.0, 1.0)
        result = calibration_result(
            mesh,
            balance,
            "/tmp/calibration",
            shutter="1/50",
        )
        actions = (self.service.home_printer, self.service.set_printer_origin, self.service.disconnect_camera)
        for action in actions:
            with self.subTest(action=action.__name__):
                self.service._calibration = result
                self.service._focus_surface = result.focus_surface
                if action == self.service.disconnect_camera:
                    self.camera.connected = True
                action()
                self.assertIsNone(self.service.status()["calibration"])
                self.assertIsNone(self.service.status()["focus_grid"])

    def test_scan_requires_exposure_focus_grid_white_balance_and_exact_coverage(self) -> None:
        self.camera.shutter = "1/50"
        plan = ScanPlan(0.0, 50.0, 0.0, 34.0, 25.0, 17.0, 25.0, "/tmp/scans")
        reading = ExposureReading(128.0, 240.0, 0.0)
        balance = WhiteBalance(1.2, 1.0, 1.4, 1.0)
        grid_results = [
            (peak, [FocusSample(peak, 100.0)])
            for peak in (10.0, 14.0, 13.0, 12.0, 16.0)
        ]

        with self.assertRaisesRegex(ServiceStateError, "calibration"):
            self.service.start_scan(plan)

        with TemporaryDirectory() as directory, patch.object(
            self.service,
            "_run_auto_exposure",
            return_value=("1/50", reading, None),
        ), patch(
            "v3se_printer.service.run_focus_sweep",
            side_effect=grid_results,
        ), patch(
            "v3se_printer.service.calibrate_white_balance",
            return_value=balance,
        ), patch(
            "v3se_printer.service.calibrate_development_recipe",
            return_value=raw_recipe(balance),
        ):
            self.service.start_auto_exposure(NormalizedROI(), directory)
            assert self.service._operation_thread is not None
            self.service._operation_thread.join(1.0)
            with self.assertRaisesRegex(ServiceStateError, "calibration"):
                self.service.start_scan(plan)

            self.service.start_focus_grid(focus_grid_plan(directory))
            assert self.service._operation_thread is not None
            self.service._operation_thread.join(2.0)
            with self.assertRaisesRegex(ServiceStateError, "calibration"):
                self.service.start_scan(plan)

            self.service.start_white_balance(NormalizedROI(), directory)
            assert self.service._operation_thread is not None
            self.service._operation_thread.join(1.0)

        self.assertIsNotNone(self.service.status()["calibration"])
        with self.assertRaisesRegex(ValueError, "bounds|coverage"):
            self.service.start_scan(replace(plan, x_max=51.0))

        with patch.object(self.service, "_run_scan") as run_scan:
            self.service.start_scan(plan)
            assert self.service._operation_thread is not None
            self.service._operation_thread.join(1.0)
        run_scan.assert_called_once_with(plan)

    def test_flat_focus_surface_rebinds_constant_z_to_scan_bounds(self) -> None:
        balance = WhiteBalance(1.0, 1.0, 1.0, 1.0)
        mesh = FocusMesh(0.0, 10.0, 0.0, 10.0, 10.25, 10.25, 10.25, 10.25)
        self.service._calibration = calibration_result(
            mesh,
            balance,
            "/tmp/calibration",
            method="flat",
        )
        self.service._focus_surface = self.service._calibration.focus_surface
        plan = ScanPlan(0.0, 50.0, 0.0, 34.0, 25.0, 17.0, 25.0, "/tmp/scans")

        with patch.object(self.service, "_run_scan") as run_scan:
            self.service.start_scan(plan)
            assert self.service._operation_thread is not None
            self.service._operation_thread.join(1.0)

        surface = self.service._calibration.focus_surface
        self.assertEqual(surface.method, "flat")
        self.assertEqual(
            (surface.mesh.x_min, surface.mesh.x_max, surface.mesh.y_min, surface.mesh.y_max),
            (0.0, 50.0, 0.0, 34.0),
        )
        self.assertEqual(
            {surface.mesh.z00, surface.mesh.z10, surface.mesh.z01, surface.mesh.z11},
            {10.25},
        )
        run_scan.assert_called_once_with(plan)

    def test_rejected_concurrent_scan_does_not_rebind_flat_focus_bounds(self) -> None:
        balance = WhiteBalance(1.0, 1.0, 1.0, 1.0)
        mesh = FocusMesh(0.0, 50.0, 0.0, 17.0, 10.25, 10.25, 10.25, 10.25)
        self.service._calibration = calibration_result(
            mesh,
            balance,
            "/tmp/calibration",
            method="flat",
        )
        self.service._focus_surface = self.service._calibration.focus_surface
        original_surface = self.service._focus_surface
        plan = ScanPlan(0.0, 75.0, 0.0, 17.0, 25.0, 17.0, 25.0, "/tmp/scans")
        self.service._state = "scanning"
        self.service._operation_lock.acquire()
        try:
            with self.assertRaisesRegex(ServiceStateError, "already running"):
                self.service.start_scan(plan)
        finally:
            self.service._operation_lock.release()
            self.service._state = "idle"

        self.assertIs(self.service._focus_surface, original_surface)
        self.assertIs(self.service._calibration.focus_surface, original_surface)

    def test_grid_focus_surface_rejects_changed_scan_bounds(self) -> None:
        balance = WhiteBalance(1.0, 1.0, 1.0, 1.0)
        mesh = FocusMesh(0.0, 50.0, 0.0, 34.0, 10.0, 11.0, 12.0, 13.0)
        self.service._calibration = calibration_result(mesh, balance, "/tmp/calibration")

        with self.assertRaisesRegex(ValueError, "match the calibrated grid"):
            self.service.start_scan(
                ScanPlan(0.0, 51.0, 0.0, 34.0, 25.0, 17.0, 25.0, "/tmp/scans")
            )

        self.assertIsNone(self.service._operation_thread)

    def test_scan_writes_all_image_roles_and_uses_separate_z_speed(self) -> None:
        mesh = FocusMesh(0.0, 50.0, 0.0, 34.0, 10.0, 11.0, 12.0, 13.0)
        self.camera.iso = "400"
        self.camera.shutter = "1/25"
        with TemporaryDirectory() as directory:
            balance = WhiteBalance(1.2, 1.0, 1.4, 1.0)
            self.service._calibration = calibration_result(
                mesh,
                balance,
                directory,
                shutter="1/100",
                iso="200",
            )
            plan = ScanPlan(0.0, 50.0, 0.0, 34.0, 25.0, 17.0, 25.0, directory)
            stitched: list[dict[str, object]] = []

            def develop(
                _nef: Path,
                display: Path,
                scene: Path,
                _recipe: RawDevelopmentRecipe,
                *,
                output_size: tuple[int, int],
            ) -> DevelopedRaw:
                self.assertEqual(output_size, (6, 4))
                display.write_bytes(b"tiff16")
                scene.write_bytes(b"float32")
                return DevelopedRaw(display, scene)

            def stitch(**values: object) -> None:
                stitched.append(values)

            with patch("v3se_printer.service.develop_nef", side_effect=develop), patch(
                "v3se_printer.service.stitch_scan_outputs", side_effect=stitch
            ):
                self.service.start_scan(plan)
                assert self.service._operation_thread is not None
                self.service._operation_thread.join(2.0)

            status = self.service.status()
            self.assertEqual(status["state"], "idle")
            self.assertEqual(status["message"], "Completed")
            root = Path(status["last_scan_dir"])
            captures = json.loads((root / "captures.json").read_text(encoding="utf-8"))
            tiles = json.loads((root / "tiles.json").read_text(encoding="utf-8"))
            params = json.loads((root / "scan_params.json").read_text(encoding="utf-8"))
            recipe = json.loads((root / "raw_development.json").read_text(encoding="utf-8"))

        self.assertEqual(len(captures), 9)
        self.assertEqual(len(tiles), 9)
        self.assertEqual(len(stitched[0]["tiles"]), 9)
        self.assertEqual(stitched[0]["image_roles"], "raw")
        self.assertEqual(params["iso"], "400")
        self.assertEqual(params["shutter"], "1/25")
        self.assertEqual(params["exposure"]["metered_luminance"], 128.0)
        self.assertEqual(params["capture_profile"], "raw")
        self.assertEqual(params["requested_step_x_mm"], 18.75)
        self.assertEqual(params["requested_step_y_mm"], 12.75)
        self.assertEqual(params["step_x_mm"], 12.5)
        self.assertEqual(params["step_y_mm"], 8.5)
        self.assertEqual(params["raw_development_recipe"], "raw_development.json")
        self.assertEqual(recipe["display_linear_gain"], 1.5)
        self.assertEqual(recipe["working_space"], "linear-rec2020-d65")
        self.assertEqual(recipe["highlight_mode"], "Ignore")
        self.assertEqual(recipe["rawpy_version"], RAWPY_VERSION)
        self.assertEqual(recipe["libraw_version"], LIBRAW_VERSION)
        self.assertEqual(
            recipe["camera_to_working_matrix"],
            [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
        )
        self.assertIn(("1/25", "raw"), self.camera.configurations)
        self.assertTrue(callable(stitched[0]["progress_cb"]))
        self.assertEqual([Path(pair.jpeg).stem.split("_c")[1][:3] for pair in self.camera.scan_targets[3:6]], ["002", "001", "000"])
        self.assertTrue(all({"file", "raw_file", "display_file", "scene_linear_file"} <= tile.keys() for tile in tiles))
        self.assertEqual(
            status["scan_progress"],
            {
                "points": [
                    {"x": tile["x_mm"], "y": tile["y_mm"], "row": tile["row"], "col": tile["col"]}
                    for tile in tiles
                ],
                "completed": 9,
                "current_index": None,
            },
        )
        self.assertEqual(
            status["step_progress"],
            {
                "phase": "scan-acquire-develop",
                "label": "Capturing and developing",
                "completed": 9,
                "total": 9,
                "unit": "tiles",
                "eta_seconds": 0.0,
            },
        )
        self.assertEqual(
            {(tile["x_mm"], tile["y_mm"]) for tile in tiles},
            {
                (12.5, 8.5),
                (25.0, 8.5),
                (37.5, 8.5),
                (12.5, 17.0),
                (25.0, 17.0),
                (37.5, 17.0),
                (12.5, 25.5),
                (25.0, 25.5),
                (37.5, 25.5),
            },
        )
        for tile in tiles:
            self.assertAlmostEqual(tile["z_mm"], mesh.z_at(tile["x_mm"], tile["y_mm"]))
        self.assertEqual(len(self.printer.moves), 18)
        for tile, z_move, xy_move in zip(tiles, self.printer.moves[::2], self.printer.moves[1::2]):
            self.assertEqual(xy_move["speed_mm_s"], 200.0)
            self.assertEqual((xy_move["x"], xy_move["y"]), (tile["x_mm"], tile["y_mm"]))
            self.assertEqual(z_move["speed_mm_s"], 10.0)
            self.assertEqual(set(z_move), {"z", "speed_mm_s"})
            self.assertAlmostEqual(z_move["z"], tile["z_mm"])
        self.assertEqual(self.sleeps, [0.25] * 9)

    def test_scan_preflights_openexr_before_camera_or_motion(self) -> None:
        mesh = FocusMesh(0.0, 50.0, 0.0, 34.0, 10.0, 10.0, 10.0, 10.0)
        balance = WhiteBalance(1.0, 1.0, 1.0, 1.0)
        self.camera.shutter = "1/25"
        with TemporaryDirectory() as directory:
            self.service._calibration = calibration_result(
                mesh,
                balance,
                directory,
                shutter="1/25",
            )
            self.openexr_build.side_effect = RuntimeError("OpenEXR unavailable")
            self.service.start_scan(ScanPlan(0.0, 50.0, 0.0, 34.0, 25.0, 17.0, 25.0, directory))
            assert self.service._operation_thread is not None
            self.service._operation_thread.join(1.0)

        status = self.service.status()
        self.assertEqual(status["state"], "idle")
        self.assertEqual(status["message"], "Scan failed")
        self.assertEqual(status["error"], "OpenEXR unavailable")
        self.assertEqual(self.camera.configurations, [])
        self.assertEqual(self.camera.scan_targets, [])
        self.assertEqual(self.printer.moves, [])

    def test_openexr_helper_is_cached_outside_scan_outputs(self) -> None:
        first = self.service._prepare_openexr_helper()
        second = self.service._prepare_openexr_helper()

        self.assertEqual(first, second)
        self.assertEqual(first.parent, Path(self.preview_directory.name).resolve() / ".runtime")
        self.assertEqual(self.openexr_build.call_count, 1)

    def test_custom_scan_roots_persist_across_service_restart(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            scan_root = root / "default-scans"
            custom_root = root / "custom-scans"
            first = ScannerService(
                FakePrinter(),
                FakeCamera(),
                preview_dir=root / "preview",
                scan_root=scan_root,
            )
            first._register_editor_scan_root(custom_root)
            restarted = ScannerService(
                FakePrinter(),
                FakeCamera(),
                preview_dir=root / "preview",
                scan_root=scan_root,
            )

            with patch("v3se_printer.service.discover_editor_projects", return_value=[]) as discover:
                self.assertEqual(restarted.editor_projects(), [])

            discover.assert_called_once_with((scan_root.resolve(), custom_root.resolve()))

    def test_scan_cancelled_during_helper_build_never_configures_camera(self) -> None:
        started = threading.Event()
        release = threading.Event()
        mesh = FocusMesh(0.0, 25.0, 0.0, 17.0, 10.0, 10.0, 10.0, 10.0)

        def prepare() -> Path:
            started.set()
            release.wait(1.0)
            return Path(self.preview_directory.name) / "unused-helper"

        with TemporaryDirectory() as directory:
            self.service._calibration = calibration_result(
                mesh,
                WhiteBalance(1.0, 1.0, 1.0, 1.0),
                directory,
                shutter="1/25",
            )
            self.camera.shutter = "1/25"
            plan = ScanPlan(0.0, 25.0, 0.0, 17.0, 25.0, 17.0, 25.0, directory)
            with patch.object(self.service, "_prepare_openexr_helper", side_effect=prepare):
                self.service.start_scan(plan)
                self.assertTrue(started.wait(1.0))
                self.service.stop()
                release.set()
                assert self.service._operation_thread is not None
                self.service._operation_thread.join(1.0)

        self.assertEqual(self.service.status()["message"], "Stopped")
        self.assertEqual(self.camera.configurations, [])
        self.assertEqual(self.printer.moves, [])

    def test_capture_failure_exposes_the_recoverable_scan_directory(self) -> None:
        mesh = FocusMesh(0.0, 25.0, 0.0, 17.0, 10.0, 10.0, 10.0, 10.0)
        self.camera.shutter = "1/25"
        with TemporaryDirectory() as directory:
            self.service._calibration = calibration_result(
                mesh,
                WhiteBalance(1.0, 1.0, 1.0, 1.0),
                directory,
                shutter="1/25",
            )
            with patch.object(self.camera, "capture_scan", side_effect=RuntimeError("capture failed")):
                self.service.start_scan(
                    ScanPlan(0.0, 25.0, 0.0, 17.0, 25.0, 17.0, 25.0, directory)
                )
                assert self.service._operation_thread is not None
                self.service._operation_thread.join(1.0)

            status = self.service.status()
            scan_dir = Path(status["last_scan_dir"])
            self.assertEqual(status["error"], "capture failed")
            self.assertTrue((scan_dir / "scan_params.json").is_file())
            self.assertTrue((scan_dir / "raw_development.json").is_file())
            self.assertFalse((scan_dir / "_write_openexr").exists())

    def test_quick_acquisition_captures_every_card_pair_before_importing(self) -> None:
        mesh = FocusMesh(0.0, 50.0, 0.0, 17.0, 10.0, 10.0, 10.0, 10.0)
        self.camera.shutter = "1/25"
        with TemporaryDirectory() as directory:
            balance = WhiteBalance(1.0, 1.0, 1.0, 1.0)
            self.service._calibration = calibration_result(
                mesh,
                balance,
                directory,
                shutter="1/25",
            )
            plan = ScanPlan(
                0.0,
                50.0,
                0.0,
                17.0,
                25.0,
                17.0,
                25.0,
                directory,
                quick_acquisition=True,
            )

            def develop(
                _nef: Path,
                display: Path,
                scene: Path,
                _recipe: RawDevelopmentRecipe,
                *,
                output_size: tuple[int, int],
            ) -> DevelopedRaw:
                self.assertEqual(output_size, (6, 4))
                display.write_bytes(b"tiff16")
                scene.write_bytes(b"float32")
                return DevelopedRaw(display, scene)

            with patch.object(
                self.service,
                "_set_step_progress",
                wraps=self.service._set_step_progress,
            ) as progress, patch("v3se_printer.service.develop_nef", side_effect=develop), patch(
                "v3se_printer.service.stitch_scan_outputs",
            ):
                self.service.start_scan(plan)
                assert self.service._operation_thread is not None
                self.service._operation_thread.join(2.0)

            status = self.service.status()
            root = Path(status["last_scan_dir"])
            remote = json.loads((root / "camera_captures.json").read_text(encoding="utf-8"))
            captures = json.loads((root / "captures.json").read_text(encoding="utf-8"))
            params = json.loads((root / "scan_params.json").read_text(encoding="utf-8"))

        self.assertEqual(status["state"], "idle")
        self.assertEqual(len(remote), 3)
        self.assertEqual(len(captures), 3)
        self.assertTrue(params["quick_acquisition"])
        self.assertEqual(self.camera.scan_operations[:3], ["camera"] * 3)
        self.assertEqual(
            self.camera.deleted_remote_scan_targets,
            list(reversed(self.camera.remote_scan_targets)),
        )
        self.assertEqual(
            self.camera.scan_operations,
            ["camera"] * 3 + ["import"] * 3 + ["delete"] * 3 + ["restore"],
        )
        self.assertEqual(self.camera.restored_capture_targets, ["Internal RAM"])
        self.assertEqual(self.camera.capture_storage, "Internal RAM")
        self.assertEqual(
            [
                call.args
                for call in progress.call_args_list
                if call.args[0] in {"camera-cleanup", "camera-storage-restore"}
            ],
            [
                ("camera-cleanup", "Cleaning camera files", 0, 3, "pairs"),
                ("camera-cleanup", "Cleaning camera files", 1, 3, "pairs"),
                ("camera-cleanup", "Cleaning camera files", 2, 3, "pairs"),
                ("camera-cleanup", "Cleaning camera files", 3, 3, "pairs"),
                ("camera-storage-restore", "Restoring camera storage", 0, 1, "targets"),
                ("camera-storage-restore", "Restoring camera storage", 1, 1, "targets"),
            ],
        )
        self.assertEqual(
            status["step_progress"],
            {
                "phase": "camera-storage-restore",
                "label": "Restoring camera storage",
                "completed": 1,
                "total": 1,
                "unit": "targets",
                "eta_seconds": 0.0,
            },
        )

    def test_quick_acquisition_cancel_deletes_only_returned_pairs_and_restores_target(self) -> None:
        mesh = FocusMesh(0.0, 50.0, 0.0, 17.0, 10.0, 10.0, 10.0, 10.0)
        self.camera.shutter = "1/25"
        captured = threading.Event()
        release = threading.Event()
        capture_scan_to_camera = self.camera.capture_scan_to_camera
        unrelated = RemoteCapturePair(
            RemoteFile("/card", "UNRELATED.JPG"),
            RemoteFile("/card", "UNRELATED.NEF"),
        )

        def capture() -> RemoteCapturePair:
            pair = capture_scan_to_camera()
            captured.set()
            release.wait(1.0)
            return pair

        with TemporaryDirectory() as directory:
            balance = WhiteBalance(1.0, 1.0, 1.0, 1.0)
            self.service._calibration = calibration_result(
                mesh,
                balance,
                directory,
                shutter="1/25",
            )
            plan = ScanPlan(
                0.0,
                50.0,
                0.0,
                17.0,
                25.0,
                17.0,
                25.0,
                directory,
                quick_acquisition=True,
            )
            with patch.object(
                self.service,
                "_set_step_progress",
                wraps=self.service._set_step_progress,
            ) as progress, patch.object(self.camera, "capture_scan_to_camera", side_effect=capture), patch(
                "v3se_printer.service.stitch_scan_outputs",
            ):
                self.service.start_scan(plan)
                self.assertTrue(captured.wait(1.0))
                self.service.stop()
                release.set()
                assert self.service._operation_thread is not None
                self.service._operation_thread.join(2.0)

        status = self.service.status()
        self.assertEqual(status["state"], "idle")
        self.assertEqual(status["message"], "Stopped")
        self.assertIsNone(status["error"])
        self.assertEqual(self.printer.stop_count, 0)
        self.assertTrue(self.printer.connected)
        self.assertTrue(self.printer.initialized)
        self.assertEqual(
            self.camera.deleted_remote_scan_targets,
            self.camera.remote_scan_targets,
        )
        self.assertNotIn(unrelated, self.camera.deleted_remote_scan_targets)
        self.assertEqual(self.camera.capture_storage, "Internal RAM")
        self.assertEqual(
            [
                call.args
                for call in progress.call_args_list
                if call.args[0] in {"camera-cleanup", "camera-storage-restore"}
            ],
            [
                ("camera-cleanup", "Cleaning camera files", 0, 1, "pairs"),
                ("camera-cleanup", "Cleaning camera files", 1, 1, "pairs"),
                ("camera-storage-restore", "Restoring camera storage", 0, 1, "targets"),
                ("camera-storage-restore", "Restoring camera storage", 1, 1, "targets"),
            ],
        )
        self.assertEqual(status["step_progress"]["phase"], "camera-storage-restore")

    def test_quick_acquisition_failure_deletes_returned_pairs_and_restores_target(self) -> None:
        mesh = FocusMesh(0.0, 50.0, 0.0, 17.0, 10.0, 10.0, 10.0, 10.0)
        self.camera.shutter = "1/25"
        capture_scan_to_camera = self.camera.capture_scan_to_camera

        def capture() -> RemoteCapturePair:
            if self.camera.remote_scan_targets:
                raise RuntimeError("camera capture failed")
            return capture_scan_to_camera()

        with TemporaryDirectory() as directory:
            balance = WhiteBalance(1.0, 1.0, 1.0, 1.0)
            self.service._calibration = calibration_result(
                mesh,
                balance,
                directory,
                shutter="1/25",
            )
            plan = ScanPlan(
                0.0,
                50.0,
                0.0,
                17.0,
                25.0,
                17.0,
                25.0,
                directory,
                quick_acquisition=True,
            )
            with patch.object(
                self.service,
                "_set_step_progress",
                wraps=self.service._set_step_progress,
            ) as progress, patch.object(
                self.camera,
                "capture_scan_to_camera",
                side_effect=capture,
            ):
                self.service.start_scan(plan)
                assert self.service._operation_thread is not None
                self.service._operation_thread.join(2.0)

        status = self.service.status()
        self.assertEqual(status["state"], "idle")
        self.assertEqual(status["message"], "Scan failed")
        self.assertEqual(status["error"], "camera capture failed")
        self.assertEqual(
            self.camera.deleted_remote_scan_targets,
            self.camera.remote_scan_targets,
        )
        self.assertEqual(self.camera.capture_storage, "Internal RAM")
        self.assertEqual(
            [
                call.args
                for call in progress.call_args_list
                if call.args[0] in {"camera-cleanup", "camera-storage-restore"}
            ],
            [
                ("camera-cleanup", "Cleaning camera files", 0, 1, "pairs"),
                ("camera-cleanup", "Cleaning camera files", 1, 1, "pairs"),
                ("camera-storage-restore", "Restoring camera storage", 0, 1, "targets"),
                ("camera-storage-restore", "Restoring camera storage", 1, 1, "targets"),
            ],
        )
        self.assertEqual(status["step_progress"]["phase"], "camera-storage-restore")

    def test_quick_cleanup_error_surfaces_and_does_not_skip_target_restore(self) -> None:
        mesh = FocusMesh(0.0, 50.0, 0.0, 17.0, 10.0, 10.0, 10.0, 10.0)
        self.camera.shutter = "1/25"
        captured = threading.Event()
        release = threading.Event()
        delete_attempts: list[RemoteCapturePair] = []
        capture_scan_to_camera = self.camera.capture_scan_to_camera

        def capture() -> RemoteCapturePair:
            pair = capture_scan_to_camera()
            if len(self.camera.remote_scan_targets) == 2:
                captured.set()
                release.wait(1.0)
            return pair

        def delete(sources: RemoteCapturePair) -> None:
            delete_attempts.append(sources)
            raise RuntimeError("camera delete failed")

        with TemporaryDirectory() as directory:
            balance = WhiteBalance(1.0, 1.0, 1.0, 1.0)
            self.service._calibration = calibration_result(
                mesh,
                balance,
                directory,
                shutter="1/25",
            )
            plan = ScanPlan(
                0.0,
                50.0,
                0.0,
                17.0,
                25.0,
                17.0,
                25.0,
                directory,
                quick_acquisition=True,
            )
            with patch.object(self.camera, "capture_scan_to_camera", side_effect=capture), patch.object(
                self.camera,
                "delete_scan",
                side_effect=delete,
            ):
                self.service.start_scan(plan)
                self.assertTrue(captured.wait(1.0))
                self.service.stop()
                release.set()
                assert self.service._operation_thread is not None
                self.service._operation_thread.join(2.0)

        status = self.service.status()
        self.assertEqual(status["state"], "idle")
        self.assertEqual(status["message"], "Scan failed")
        self.assertEqual(status["error"], "camera delete failed")
        self.assertEqual(delete_attempts, list(reversed(self.camera.remote_scan_targets)))
        self.assertEqual(self.camera.restored_capture_targets, ["Internal RAM"])
        self.assertEqual(self.camera.capture_storage, "Internal RAM")

    def test_emergency_stop_preserves_quick_cleanup_failure(self) -> None:
        mesh = FocusMesh(0.0, 50.0, 0.0, 17.0, 10.0, 10.0, 10.0, 10.0)
        self.camera.shutter = "1/25"
        captured = threading.Event()
        release = threading.Event()
        delete_attempts: list[RemoteCapturePair] = []
        capture_scan_to_camera = self.camera.capture_scan_to_camera

        def capture() -> RemoteCapturePair:
            pair = capture_scan_to_camera()
            captured.set()
            release.wait(1.0)
            return pair

        def delete(sources: RemoteCapturePair) -> None:
            delete_attempts.append(sources)
            raise RuntimeError("camera delete failed")

        with TemporaryDirectory() as directory:
            self.service._calibration = calibration_result(
                mesh,
                WhiteBalance(1.0, 1.0, 1.0, 1.0),
                directory,
                shutter="1/25",
            )
            plan = ScanPlan(
                0.0,
                50.0,
                0.0,
                17.0,
                25.0,
                17.0,
                25.0,
                directory,
                quick_acquisition=True,
            )
            with patch.object(self.camera, "capture_scan_to_camera", side_effect=capture), patch.object(
                self.camera,
                "delete_scan",
                side_effect=delete,
            ):
                self.service.start_scan(plan)
                self.assertTrue(captured.wait(1.0))
                self.service.emergency_stop()
                release.set()
                assert self.service._operation_thread is not None
                self.service._operation_thread.join(2.0)

        status = self.service.status()
        self.assertEqual(status["state"], "faulted")
        self.assertEqual(status["message"], "Emergency stop sent")
        self.assertEqual(
            status["error"],
            "Reconnect and initialize the printer before further motion; "
            "active operation failed: camera delete failed",
        )
        self.assertEqual(delete_attempts, self.camera.remote_scan_targets)
        self.assertEqual(self.camera.restored_capture_targets, ["Internal RAM"])
        self.assertEqual(self.camera.capture_storage, "Internal RAM")

    def test_scan_reports_live_path_and_overlaps_raw_development_with_next_capture(self) -> None:
        mesh = FocusMesh(0.0, 50.0, 0.0, 17.0, 10.0, 10.0, 10.0, 10.0)
        self.camera.shutter = "1/25"
        first_development = threading.Event()
        release_development = threading.Event()
        second_capture = threading.Event()
        capture_scan = self.camera.capture_scan

        def capture(jpeg_path: str | Path, nef_path: str | Path) -> CapturePair:
            pair = capture_scan(jpeg_path, nef_path)
            if len(self.camera.scan_targets) == 2:
                second_capture.set()
            return pair

        def develop(
            _nef: Path,
            display: Path,
            scene: Path,
            _recipe: RawDevelopmentRecipe,
            *,
            output_size: tuple[int, int],
        ) -> DevelopedRaw:
            self.assertEqual(output_size, (6, 4))
            if not first_development.is_set():
                first_development.set()
                release_development.wait(1.0)
            display.write_bytes(b"tiff16")
            scene.write_bytes(b"float32")
            return DevelopedRaw(display, scene)

        with TemporaryDirectory() as directory:
            balance = WhiteBalance(1.0, 1.0, 1.0, 1.0)
            self.service._calibration = calibration_result(
                mesh,
                balance,
                directory,
                shutter="1/25",
            )
            plan = ScanPlan(0.0, 50.0, 0.0, 17.0, 25.0, 17.0, 25.0, directory)
            with patch.object(self.camera, "capture_scan", side_effect=capture), patch(
                "v3se_printer.service.develop_nef",
                side_effect=develop,
            ), patch("v3se_printer.service.stitch_scan_outputs"):
                self.service.start_scan(plan)
                self.assertTrue(first_development.wait(1.0))
                self.assertTrue(second_capture.wait(1.0))
                deadline = time.monotonic() + 1.0
                live = self.service.status()
                while live["scan_progress"]["completed"] < 2 and time.monotonic() < deadline:
                    time.sleep(0.005)
                    live = self.service.status()
                self.assertEqual(live["state"], "scanning")
                self.assertEqual(live["scan_progress"]["completed"], 2)
                self.assertEqual(live["scan_progress"]["current_index"], 1)
                self.assertEqual(len(live["scan_progress"]["points"]), 3)
                self.assertEqual(
                    live["step_progress"],
                    {
                        "phase": "scan-acquire-develop",
                        "label": "Capturing and developing",
                        "completed": 0,
                        "total": 3,
                        "unit": "tiles",
                        "eta_seconds": None,
                    },
                )
                release_development.set()
                assert self.service._operation_thread is not None
                self.service._operation_thread.join(2.0)

        self.assertFalse(self.service._operation_thread.is_alive())
        status = self.service.status()
        self.assertEqual(status["scan_progress"]["completed"], 3)
        self.assertEqual(status["scan_progress"]["current_index"], None)
        self.assertEqual(status["step_progress"]["completed"], 3)
        self.assertEqual(status["step_progress"]["eta_seconds"], 0.0)


if __name__ == "__main__":
    unittest.main()
