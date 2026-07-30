from __future__ import annotations

from dataclasses import replace
import math
from pathlib import Path
from queue import Queue
from tempfile import TemporaryDirectory
from threading import Condition, Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from v3se_printer.scan.params import ScanParams
from v3se_printer.scan import worker


class Value:
    def __init__(self, value: object) -> None:
        self.value = value

    def get(self) -> object:
        return self.value


class FakeGui:
    def __init__(self) -> None:
        self._events: Queue[tuple[str, object]] = Queue()
        self._worker = object()
        self._cam_connected = True
        self._coord_mode_var = Value("absolute")
        self._speed_xy_var = Value(150.0)
        self._speed_z_var = Value(10.0)
        self._bed_z_min_var = Value(0.0)
        self._bed_z_max_var = Value(250.0)
        self._cam_frame_cond = Condition()
        self._cam_frame_seq = 0
        self._cam_config = SimpleNamespace(
            fps=30.0,
            af_settle_ms=0,
            af_slow_step_mm=0.1,
            rotation_deg=0,
            crop_left_pct=0.0,
            crop_top_pct=0.0,
            crop_right_pct=0.0,
            crop_bottom_pct=0.0,
        )
        self._scan_stop = Event()
        self._cam_af_stop = Event()
        self.commands: list[str] = []
        self.fail_command: str | None = None

    def _send_and_wait(self, command: str, **_kwargs: object) -> tuple[bool, list[str]]:
        self.commands.append(command)
        return command != self.fail_command, []

    def _camera_wait_for_next_frame(self, sequence: int, **_kwargs: object) -> tuple[int, np.ndarray]:
        return sequence + 1, np.full((8, 8, 3), 64, dtype=np.uint8)

    def _camera_autofocus_thread(self, *_args: object, **_kwargs: object) -> tuple[bool, float, float, str]:
        return True, 203.0, 1.0, "ok"

    def finished(self) -> list[tuple[bool, str]]:
        results: list[tuple[bool, str]] = []
        for kind, payload in list(self._events.queue):
            if kind == "scan-finished" and isinstance(payload, tuple) and len(payload) == 2:
                results.append((bool(payload[0]), str(payload[1])))
        return results


def scan_params(output_dir: str) -> ScanParams:
    return ScanParams(
        x_min=0.0,
        x_max=0.0,
        y_min=0.0,
        y_max=0.0,
        step_x_mm=1.0,
        step_y_mm=1.0,
        serpentine=True,
        focus_plane=False,
        mesh_nx=2,
        mesh_ny=2,
        autofocus_each_tile=False,
        shots_per_tile=1,
        stack_mode="none",
        capture_settle_ms=0,
        downsample=1,
        build_pyramidal_tiff=False,
        tiff_compression="deflate",
        out_base_dir=output_dir,
    )


class ScanWorkerTests(unittest.TestCase):
    def run_worker(self, gui: FakeGui, params: ScanParams) -> None:
        with (
            patch.object(worker.time, "sleep"),
            patch.object(worker, "transform_frame", side_effect=lambda frame, **_kwargs: frame),
            patch.object(worker, "imwrite"),
        ):
            worker.run_scan_worker(gui, params)

    def test_output_directories_do_not_collide_within_the_same_second(self) -> None:
        with TemporaryDirectory() as directory, patch.object(worker.time, "strftime", return_value="20260730_120000"):
            first = FakeGui()
            second = FakeGui()
            self.run_worker(first, scan_params(directory))
            self.run_worker(second, scan_params(directory))

            scans = sorted(path for path in Path(directory).iterdir() if path.is_dir())
            self.assertEqual(len(scans), 2)
            self.assertNotEqual(scans[0], scans[1])
            self.assertTrue(all((path / "scan_params.json").is_file() for path in scans))
            self.assertEqual(len(first.finished()), 1)
            self.assertEqual(len(second.finished()), 1)
            self.assertTrue(first.finished()[0][0] and second.finished()[0][0])

    def test_invalid_geometry_fails_before_output_or_motion(self) -> None:
        with TemporaryDirectory() as directory:
            cases = (
                replace(scan_params(directory), step_x_mm=0.0),
                replace(scan_params(directory), step_y_mm=math.inf),
                replace(scan_params(directory), x_min=math.nan),
                replace(scan_params(directory), x_min=2.0, x_max=1.0),
                replace(scan_params(directory), x_min="bad"),
            )
            for params in cases:
                with self.subTest(params=params):
                    gui = FakeGui()
                    worker.run_scan_worker(gui, params)
                    self.assertEqual(len(gui.finished()), 1)
                    self.assertFalse(gui.finished()[0][0])
                    self.assertIn("Invalid scan parameters", gui.finished()[0][1])
                    self.assertEqual(gui.commands, [])
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_g90_failure_aborts_before_scan_motion(self) -> None:
        with TemporaryDirectory() as directory:
            gui = FakeGui()
            gui.fail_command = "G90"
            self.run_worker(gui, scan_params(directory))

            self.assertEqual(gui.commands, ["G90"])
            self.assertEqual(len(gui.finished()), 1)
            self.assertFalse(gui.finished()[0][0])
            self.assertIn("G-code failed: G90", gui.finished()[0][1])

    def test_focus_motion_failure_reports_failure_and_restores_once(self) -> None:
        with TemporaryDirectory() as directory:
            gui = FakeGui()
            gui.fail_command = "G0 X0 Y0 F9000"
            self.run_worker(gui, replace(scan_params(directory), focus_plane=True))

            self.assertEqual(gui.finished(), [(False, "Scan failed: G-code failed: G0 X0 Y0 F9000")])
            self.assertEqual(gui.commands, ["G90", "G0 X0 Y0 F9000", "G90"])

    def test_critical_manifest_write_failures_fail_the_scan(self) -> None:
        original = worker._write_json
        cases = (
            ("scan_params.json", scan_params),
            ("focus_mesh.json", lambda directory: replace(scan_params(directory), x_max=1.0, y_max=1.0, focus_plane=True)),
            ("tiles.json", scan_params),
        )
        for filename, make_params in cases:
            with self.subTest(filename=filename), TemporaryDirectory() as directory:
                gui = FakeGui()

                def write(path: str, payload: object) -> None:
                    if Path(path).name == filename:
                        raise OSError("write failed")
                    original(path, payload)

                with patch.object(worker, "_write_json", side_effect=write):
                    self.run_worker(gui, make_params(directory))

                self.assertEqual(len(gui.finished()), 1)
                self.assertFalse(gui.finished()[0][0])
                self.assertIn(filename, gui.finished()[0][1])

    def test_focus_and_stitch_cancellation_are_reported_as_stopped(self) -> None:
        with TemporaryDirectory() as directory:
            focus_gui = FakeGui()
            focus_gui._scan_stop.set()
            self.run_worker(focus_gui, replace(scan_params(directory), focus_plane=True))
            self.assertEqual(focus_gui.finished(), [(False, "Scan stopped.")])
            self.assertEqual(focus_gui.commands, ["G90", "G90"])

        with TemporaryDirectory() as directory:
            stitch_gui = FakeGui()
            params = replace(scan_params(directory), build_pyramidal_tiff=True)

            def stitch(**kwargs: object) -> None:
                stitch_gui._scan_stop.set()
                cancel = kwargs["cancel_cb"]
                if not callable(cancel):
                    raise AssertionError("stitch cancel callback is missing")
                cancel()

            with patch.object(worker, "stitch_scan_outputs", side_effect=stitch):
                self.run_worker(stitch_gui, params)

            self.assertEqual(stitch_gui.finished(), [(False, "Scan stopped.")])
            self.assertFalse(any(Path(directory).glob("scan_*/stitch_error.txt")))


if __name__ == "__main__":
    unittest.main()
