from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import asdict, dataclass, replace
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Callable

from .calibration import (
    CalibrationError,
    ExposureReading,
    FocusMesh,
    FocusSample,
    NormalizedROI,
    RAW_HIGHLIGHT_TARGET,
    analyze_exposure,
    exposure_error_ev,
    fit_focus_mesh,
    next_raw_shutter,
    next_shutter,
    read_jpeg,
    run_focus_sweep,
    shutter_choices,
)
from .editor import (
    EditRecipe,
    apply_editor_revision,
    discover_editor_projects,
    editor_project_details,
    editor_results,
    load_editor_project,
    render_editor_preview,
)
from .nikon import CapturePair, NikonJ4Camera, PROFILE_ANALYSIS, PROFILE_RAW, RemoteCapturePair
from .printer import PrinterController
from .progress import StepProgressTracker
from .raw import (
    RawDevelopmentRecipe,
    RawHeadroomReading,
    WhiteBalance,
    analyze_raw_headroom,
    calibrate_development_recipe,
    calibrate_white_balance,
    develop_nef,
)
from .scan.stitch_outputs import stitch_scan_outputs
from .scan.stitching.openexr import build_openexr_helper


DEFAULT_XY_SPEED_MM_S = 200.0
DEFAULT_Z_SPEED_MM_S = 10.0
DEFAULT_JOG_XY_SPEED_MM_S = 100.0
HOME_X_MM = 110.0
HOME_Y_MM = 110.0
HOME_Z_MM = 203.0
DEFAULT_SHUTTER = "1/6"
SCAN_RESULT_FILES = {
    "full_tiff": "mosaic_full.tif",
    "pyramidal_tiff": "mosaic_pyramidal.ome.tif",
    "scene_linear_exr": "mosaic_scene_linear.exr",
    "preview_jpeg": "mosaic_thumb_2000.jpg",
    "project_metadata": "scan_params.json",
    "recipe_metadata": "raw_development.json",
    "stitch_metadata": "stitch_meta.json",
}


class ServiceStateError(RuntimeError):
    pass


class PreviewCaptureError(RuntimeError):
    pass


@dataclass(frozen=True)
class CalibrationPlan:
    x_min: float
    x_max: float
    y_min: float
    y_max: float
    exposure_roi: NormalizedROI
    focus_roi: NormalizedROI
    gray_roi: NormalizedROI
    output_dir: str
    speed_xy_mm_s: float = DEFAULT_XY_SPEED_MM_S
    speed_z_mm_s: float = DEFAULT_Z_SPEED_MM_S

    def __post_init__(self) -> None:
        values = (
            self.x_min,
            self.x_max,
            self.y_min,
            self.y_max,
            self.speed_xy_mm_s,
            self.speed_z_mm_s,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Calibration values must be finite")
        if self.x_min >= self.x_max or self.y_min >= self.y_max:
            raise ValueError("Calibration area must have positive dimensions")
        if self.speed_xy_mm_s <= 0 or self.speed_z_mm_s <= 0:
            raise ValueError("Motion speeds must be positive")
        if self.speed_xy_mm_s > 300 or self.speed_z_mm_s > 50:
            raise ValueError("Motion speeds exceed the supported limits")


@dataclass(frozen=True)
class FocusGridPlan:
    x_min: float
    x_max: float
    y_min: float
    y_max: float
    focus_roi: NormalizedROI
    output_dir: str
    speed_xy_mm_s: float = DEFAULT_XY_SPEED_MM_S
    speed_z_mm_s: float = DEFAULT_Z_SPEED_MM_S

    def __post_init__(self) -> None:
        values = (
            self.x_min,
            self.x_max,
            self.y_min,
            self.y_max,
            self.speed_xy_mm_s,
            self.speed_z_mm_s,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Focus-grid values must be finite")
        if self.x_min >= self.x_max or self.y_min >= self.y_max:
            raise ValueError("Focus-grid coverage must have positive dimensions")
        if self.speed_xy_mm_s <= 0 or self.speed_z_mm_s <= 0:
            raise ValueError("Motion speeds must be positive")
        if self.speed_xy_mm_s > 300 or self.speed_z_mm_s > 50:
            raise ValueError("Motion speeds exceed the supported limits")


@dataclass(frozen=True)
class CalibrationResult:
    focus_surface: "FocusSurfaceCalibration"
    shutter: str
    exposure: ExposureReading
    white_balance: WhiteBalance
    directory: str
    iso: str
    raw_recipe: RawDevelopmentRecipe

    def __post_init__(self) -> None:
        if self.raw_recipe.white_balance != self.white_balance:
            raise ValueError("RAW recipe white balance must match calibration")


@dataclass(frozen=True)
class ExposureCalibration:
    shutter: str
    reading: ExposureReading
    iso: str
    directory: str


@dataclass(frozen=True)
class FocusObservation:
    name: str
    x: float
    y: float
    z: float

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("Focus observation name must be non-empty")
        if not all(math.isfinite(value) for value in (self.x, self.y, self.z)):
            raise ValueError("Focus observation coordinates must be finite")


@dataclass(frozen=True)
class FocusSurfaceCalibration:
    method: str
    mesh: FocusMesh
    measurements: tuple[FocusObservation, ...]
    directory: str

    def __post_init__(self) -> None:
        if self.method not in {"flat", "grid"}:
            raise ValueError("Focus surface method must be flat or grid")
        expected_count = 1 if self.method == "flat" else 5
        if len(self.measurements) != expected_count:
            raise ValueError(f"{self.method.title()} focus requires {expected_count} measurement(s)")
        if self.method == "flat":
            z = self.measurements[0].z
            if not all(
                math.isclose(value, z, abs_tol=1e-9)
                for value in (self.mesh.z00, self.mesh.z10, self.mesh.z01, self.mesh.z11)
            ):
                raise ValueError("Flat focus mesh must use its measured Z everywhere")
        elif {measurement.name for measurement in self.measurements} != {
            "lower_left",
            "lower_right",
            "upper_left",
            "upper_right",
            "center",
        }:
            raise ValueError("Grid focus requires four quarter-area observations plus center")
        if not self.directory:
            raise ValueError("Focus surface directory must be non-empty")


@dataclass(frozen=True)
class WhiteBalanceCalibration:
    balance: WhiteBalance
    shutter: str
    iso: str
    directory: str
    raw_recipe: RawDevelopmentRecipe

    def __post_init__(self) -> None:
        if self.raw_recipe.white_balance != self.balance:
            raise ValueError("RAW recipe white balance must match calibration")


@dataclass(frozen=True)
class MeasurementRecord:
    sequence: int
    timestamp: str
    operation: str
    phase: str
    profile: str
    parameter: str
    result: str
    accepted: bool | None


@dataclass(frozen=True)
class ScanPoint:
    x: float
    y: float
    row: int
    col: int


@dataclass(frozen=True)
class ScanProgress:
    points: tuple[ScanPoint, ...]
    completed: int
    current_index: int | None


@dataclass(frozen=True)
class ScanCapture:
    row: int
    col: int
    x_mm: float
    y_mm: float
    z_mm: float
    file: str
    raw_file: str
    capture_profile: str


@dataclass(frozen=True)
class ScanPlan:
    x_min: float
    x_max: float
    y_min: float
    y_max: float
    frame_width_mm: float
    frame_height_mm: float
    overlap_percent: float
    output_dir: str
    speed_xy_mm_s: float = DEFAULT_XY_SPEED_MM_S
    speed_z_mm_s: float = DEFAULT_Z_SPEED_MM_S
    settle_ms: int = 1000
    quick_acquisition: bool = False

    def __post_init__(self) -> None:
        values = (
            self.x_min,
            self.x_max,
            self.y_min,
            self.y_max,
            self.frame_width_mm,
            self.frame_height_mm,
            self.overlap_percent,
            self.speed_xy_mm_s,
            self.speed_z_mm_s,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Scan values must be finite")
        if self.x_min >= self.x_max or self.y_min >= self.y_max:
            raise ValueError("Scan area must have positive dimensions")
        if self.frame_width_mm <= 0 or self.frame_height_mm <= 0:
            raise ValueError("Camera footprint must be positive")
        if self.x_max - self.x_min < self.frame_width_mm:
            raise ValueError("Scan width must be at least one frame wide")
        if self.y_max - self.y_min < self.frame_height_mm:
            raise ValueError("Scan height must be at least one frame high")
        if not 0 < self.overlap_percent < 90:
            raise ValueError("Overlap must be between 0 and 90 percent")
        if self.speed_xy_mm_s <= 0 or self.speed_z_mm_s <= 0:
            raise ValueError("Motion speeds must be positive")
        if self.speed_xy_mm_s > 300 or self.speed_z_mm_s > 50:
            raise ValueError("Motion speeds exceed the supported limits")
        if isinstance(self.settle_ms, bool) or not 0 <= self.settle_ms <= 5000:
            raise ValueError("Settle time must be between 0 and 5000 ms")
        if not isinstance(self.quick_acquisition, bool):
            raise ValueError("Quick acquisition must be true or false")


def scan_axis(start: float, stop: float, step: float) -> list[float]:
    if step <= 0 or stop < start:
        raise ValueError("Invalid scan axis")
    span = stop - start
    if span <= 1e-9:
        return [start]
    intervals = max(1, math.ceil(span / step))
    actual_step = span / intervals
    return [stop if index == intervals else start + index * actual_step for index in range(intervals + 1)]


def scan_centers(start: float, stop: float, footprint: float, step: float) -> list[float]:
    if footprint <= 0 or stop - start < footprint:
        raise ValueError("Scan coverage must be at least one camera frame")
    return scan_axis(start + footprint / 2.0, stop - footprint / 2.0, step)


def write_json(path: Path, payload: object) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def scan_results(scan_dir: str | None) -> dict[str, dict[str, str] | None]:
    root = None if scan_dir is None else Path(scan_dir)
    return {
        artifact: (
            {"name": filename, "download_url": f"/api/scan/results/{artifact}"}
            if root is not None and (root / filename).is_file()
            else None
        )
        for artifact, filename in SCAN_RESULT_FILES.items()
    }


class ScannerService:
    def __init__(
        self,
        printer: PrinterController | None = None,
        camera: NikonJ4Camera | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        preview_dir: str | Path | None = None,
        scan_root: str | Path | None = None,
    ) -> None:
        self.printer = printer or PrinterController()
        self.camera = camera or NikonJ4Camera()
        self._clock = clock
        self._sleep = sleeper
        self._preview_dir = Path(tempfile.gettempdir(), "MarlinScan", "previews") if preview_dir is None else Path(preview_dir).expanduser().resolve()
        self._scan_root = Path.cwd() / "output" / "scans" if scan_root is None else Path(scan_root).expanduser().resolve()
        self._scan_roots_path = self._scan_root.parent / "scan_roots.json"
        self._editor_scan_roots = self._load_editor_scan_roots()
        self._auto_preview_path: Path | None = None
        self._lock = threading.RLock()
        self._operation_lock = threading.Lock()
        self._cancel = threading.Event()
        self._state = "idle"
        self._message = "Ready"
        self._progress_tracker = StepProgressTracker(clock)
        self._error: str | None = None
        self._operation_thread: threading.Thread | None = None
        self._active_motion = False
        self._calibration: CalibrationResult | None = None
        self._exposure_calibration: ExposureCalibration | None = None
        self._focus_surface: FocusSurfaceCalibration | None = None
        self._white_balance_calibration: WhiteBalanceCalibration | None = None
        self._last_scan_dir: str | None = None
        self._last_editor_revision_dir: str | None = None
        self._scan_progress: ScanProgress | None = None
        self._measurements: list[MeasurementRecord] = []
        self._measurement_sequence = 0
        self._jog_vector = (0.0, 0.0, 0.0)
        self._jog_speed_xy = DEFAULT_JOG_XY_SPEED_MM_S
        self._jog_speed_z = DEFAULT_Z_SPEED_MM_S
        self._jog_deadline = 0.0
        self._jog_thread: threading.Thread | None = None

    def status(self) -> dict[str, object]:
        with self._lock:
            calibration = None
            if self._calibration is not None:
                calibration = {
                    "mesh": asdict(self._calibration.focus_surface.mesh),
                    "focus_method": self._calibration.focus_surface.method,
                    "focus_measurements": [
                        asdict(measurement)
                        for measurement in self._calibration.focus_surface.measurements
                    ],
                    "shutter": self._calibration.shutter,
                    "exposure": asdict(self._calibration.exposure),
                    "white_balance": asdict(self._calibration.white_balance),
                    "display_linear_gain": self._calibration.raw_recipe.display_linear_gain,
                    "directory": self._calibration.directory,
                    "iso": self._calibration.iso,
                }
            focus_grid = None
            if self._focus_surface is not None:
                focus_grid = {
                    "method": self._focus_surface.method,
                    "mesh": asdict(self._focus_surface.mesh),
                    "measurements": [
                        asdict(measurement)
                        for measurement in self._focus_surface.measurements
                    ],
                    "directory": self._focus_surface.directory,
                }
            quick_focus_z = (
                self._focus_surface.measurements[0].z
                if self._focus_surface is not None and self._focus_surface.method == "flat"
                else None
            )
            quick_calibration = None
            if any(
                value is not None
                for value in (
                    self._exposure_calibration,
                    quick_focus_z,
                    self._white_balance_calibration,
                )
            ):
                quick_calibration = {
                    "exposure": None
                    if self._exposure_calibration is None
                    else {
                        "shutter": self._exposure_calibration.shutter,
                        "reading": asdict(self._exposure_calibration.reading),
                    },
                    "focus_z": quick_focus_z,
                    "white_balance": None
                    if self._white_balance_calibration is None
                    else asdict(self._white_balance_calibration.balance),
                }
            scan_progress = None
            if self._scan_progress is not None:
                scan_progress = {
                    "points": [asdict(point) for point in self._scan_progress.points],
                    "completed": self._scan_progress.completed,
                    "current_index": self._scan_progress.current_index,
                }
            return {
                "state": self._state,
                "message": self._message,
                "step_progress": (
                    None
                    if self._progress_tracker.current is None
                    else asdict(self._progress_tracker.current)
                ),
                "error": self._error,
                "printer": asdict(self.printer.status()),
                "camera": self.camera.status(),
                "calibration": calibration,
                "focus_grid": focus_grid,
                "quick_calibration": quick_calibration,
                "measurements": [asdict(measurement) for measurement in self._measurements],
                "last_scan_dir": self._last_scan_dir,
                "scan_results": scan_results(self._last_scan_dir),
                "last_editor_revision_dir": self._last_editor_revision_dir,
                "editor_result": (
                    None
                    if self._last_editor_revision_dir is None
                    else {
                        "revision": Path(self._last_editor_revision_dir).name,
                        "directory": self._last_editor_revision_dir,
                    }
                ),
                "editor_results": editor_results(self._last_editor_revision_dir),
                "scan_progress": scan_progress,
                "latest_jpeg_path": None if self.camera.latest_jpeg_path is None else str(self.camera.latest_jpeg_path),
            }

    def start_calibration(self, plan: CalibrationPlan) -> dict[str, object]:
        self._validate_calibration_plan(plan)
        self._start_operation(
            "calibrating",
            lambda: self._run_calibration(plan),
            clear_components=frozenset({"exposure", "focus", "white_balance"}),
        )
        return self.status()

    def start_scan(self, plan: ScanPlan) -> dict[str, object]:
        self._validate_scan_plan(plan)
        self._start_operation("scanning", lambda: self._run_validated_scan(plan))
        return self.status()

    def editor_projects(self) -> list[dict[str, object]]:
        with self._lock:
            roots = self._editor_scan_roots
        return discover_editor_projects(roots)

    def editor_project(self, project_dir: str) -> dict[str, object]:
        return editor_project_details(self._load_editor_project(project_dir))

    def editor_original_preview(self, project_dir: str) -> Path:
        project = self._load_editor_project(project_dir)
        outputs = project.stitch_metadata.get("outputs")
        if not isinstance(outputs, dict) or not isinstance(outputs.get("preview_jpeg"), str):
            raise ValueError("Editor project does not declare a JPEG preview")
        preview = (project.root / outputs["preview_jpeg"]).resolve()
        if project.root not in preview.parents:
            raise ValueError("Editor project preview must stay inside the scan project")
        if not preview.is_file():
            raise FileNotFoundError(preview)
        return preview

    def editor_tile_preview(self, project_dir: str, tile_index: int) -> bytes:
        if not self._operation_lock.acquire(blocking=False):
            raise ServiceStateError("Another scanner operation is already running")
        try:
            with self._lock:
                if self._state != "idle":
                    raise ServiceStateError(f"Cannot preview while {self._state}")
            project = self._load_editor_project(project_dir)
            return render_editor_preview(project, EditRecipe(), "tile", tile_index)
        finally:
            self._operation_lock.release()

    def start_editor_apply(self, project_dir: str, recipe: EditRecipe) -> dict[str, object]:
        project = self._load_editor_project(project_dir)

        def apply() -> None:
            revision = apply_editor_revision(
                project,
                recipe,
                openexr_source=Path(__file__).resolve().parents[1] / "tools" / "write_openexr.cpp",
                progress_cb=self._set_step_progress,
                cancel_cb=self._require_not_cancelled,
            )
            with self._lock:
                self._require_not_cancelled()
                self._last_editor_revision_dir = str(revision)

        self._start_operation(
            "editing",
            apply,
            require_printer=False,
            require_camera=False,
            mark_completed=True,
        )
        return self.status()

    def start_auto_exposure(
        self,
        roi: NormalizedROI,
        output_dir: str,
    ) -> dict[str, object]:
        self._start_operation(
            "calibrating",
            lambda: self._run_quick_auto_exposure(roi, output_dir),
            require_printer=False,
            clear_components=frozenset({"exposure", "white_balance"}),
            mark_completed=False,
        )
        return self.status()

    def start_auto_focus(
        self,
        roi: NormalizedROI,
        output_dir: str,
        *,
        x_min: float,
        x_max: float,
        y_min: float,
        y_max: float,
        speed_z_mm_s: float = DEFAULT_Z_SPEED_MM_S,
    ) -> dict[str, object]:
        printer = self.printer.status()
        if not printer.initialized or printer.position is None:
            raise ServiceStateError("Initialize printer coordinates before auto focus")
        self._validate_flat_focus_bounds(
            x_min,
            x_max,
            y_min,
            y_max,
            printer.position.x,
            printer.position.y,
            printer.position.z,
        )
        speed = self._bounded_speed(speed_z_mm_s, "Z focus speed", 50.0)
        shutter = self._current_shutter()
        self._start_operation(
            "calibrating",
            lambda: self._run_quick_auto_focus(
                roi,
                output_dir,
                printer.position.x,
                printer.position.y,
                printer.position.z,
                x_min,
                x_max,
                y_min,
                y_max,
                speed,
                shutter,
            ),
            clear_components=frozenset({"focus"}),
            mark_completed=False,
        )
        return self.status()

    def start_focus_grid(self, plan: FocusGridPlan) -> dict[str, object]:
        self._validate_focus_grid_plan(plan)
        shutter = self._current_shutter()
        self._start_operation(
            "calibrating",
            lambda: self._run_focus_grid_calibration(plan, shutter),
            clear_components=frozenset({"focus"}),
            mark_completed=False,
        )
        return self.status()

    def start_white_balance(self, roi: NormalizedROI, output_dir: str) -> dict[str, object]:
        shutter = self._current_shutter()
        self._start_operation(
            "calibrating",
            lambda: self._run_quick_white_balance(roi, output_dir, shutter),
            require_printer=False,
            clear_components=frozenset({"white_balance"}),
            mark_completed=False,
        )
        return self.status()

    def connect_printer(self, port: str, *, baud: int = 115200, eol: str = "crlf") -> dict[str, object]:
        def connect() -> None:
            self.printer.connect(port, baud=baud, eol=eol)
            self._invalidate_calibration()

        return self._run_sync(
            "connecting",
            "Connecting printer",
            connect,
            allowed_states=frozenset({"idle", "faulted"}),
        )

    def disconnect_printer(self) -> dict[str, object]:
        def disconnect() -> None:
            self.printer.disconnect()
            self._invalidate_calibration()

        return self._run_sync(
            "disconnecting",
            "Disconnecting printer",
            disconnect,
            allowed_states=frozenset({"idle", "faulted"}),
        )

    def home_printer(self) -> dict[str, object]:
        def home() -> None:
            self._require_not_cancelled()
            self._invalidate_calibration()
            self._run_motion_command(self.printer.home)
            self._require_not_cancelled()
            self._move_to(HOME_X_MM, HOME_Y_MM, HOME_Z_MM, DEFAULT_XY_SPEED_MM_S, DEFAULT_Z_SPEED_MM_S)
            self._capture_motion_preview()

        return self._run_sync("moving", "Homing printer", home)

    def set_printer_origin(self) -> dict[str, object]:
        def set_origin() -> None:
            self._require_not_cancelled()
            self._invalidate_calibration()
            self.printer.set_origin()
            self._require_not_cancelled()

        return self._run_sync("moving", "Setting origin", set_origin)

    def restore_printer_position(self) -> dict[str, object]:
        def restore() -> None:
            self._require_not_cancelled()
            self._invalidate_calibration()
            self.printer.restore_remembered_position()
            self._require_not_cancelled()
            self._capture_motion_preview()

        return self._run_sync("moving", "Restoring remembered position", restore)

    def move_printer(
        self,
        *,
        x: float | None = None,
        y: float | None = None,
        z: float | None = None,
        speed_xy_mm_s: float = DEFAULT_XY_SPEED_MM_S,
        speed_z_mm_s: float = DEFAULT_Z_SPEED_MM_S,
    ) -> dict[str, object]:
        if x is None and y is None and z is None:
            raise ValueError("At least one target axis is required")
        if not math.isfinite(speed_xy_mm_s) or not 0 < speed_xy_mm_s <= 300:
            raise ValueError("XY move speed must be between 0 and 300 mm/s")
        if not math.isfinite(speed_z_mm_s) or not 0 < speed_z_mm_s <= 50:
            raise ValueError("Z move speed must be between 0 and 50 mm/s")

        position = self.printer.status().position
        with self._lock:
            focus_surface = self._focus_surface
        if focus_surface is not None and (x is not None or y is not None):
            if position is None:
                raise ServiceStateError("Printer position is unavailable")
            target_x = position.x if x is None else float(x)
            target_y = position.y if y is None else float(y)
            z = focus_surface.mesh.z_at(target_x, target_y)

        def move() -> None:
            self._require_not_cancelled()
            if z is not None:
                self._run_motion_command(
                    lambda: self.printer.move_absolute(z=z, speed_mm_s=speed_z_mm_s)
                )
                self._require_not_cancelled()
            if x is not None or y is not None:
                self._run_motion_command(
                    lambda: self.printer.move_absolute(x=x, y=y, speed_mm_s=speed_xy_mm_s)
                )
                self._require_not_cancelled()
            self._capture_motion_preview()

        return self._run_sync(
            "moving",
            "Moving printer",
            move,
        )

    def take_camera_control(self) -> dict[str, object]:
        return self._run_sync("connecting", "Taking control of Nikon", self.camera.take_control)

    def connect_camera(self) -> dict[str, object]:
        def connect() -> None:
            self.camera.connect()
            self.camera.configure(DEFAULT_SHUTTER, profile=PROFILE_ANALYSIS)
            self._invalidate_calibration()

        return self._run_sync("connecting", "Connecting Nikon", connect)

    def set_camera_iso(self, iso: str) -> dict[str, object]:
        def configure() -> None:
            self.camera.set_iso(iso)
            with self._lock:
                self._calibration = None
                self._exposure_calibration = None
                self._white_balance_calibration = None

        return self._run_sync("configuring", "Setting Nikon ISO", configure)

    def set_camera_shutter(self, shutter: str) -> dict[str, object]:
        def configure() -> None:
            self.camera.configure(shutter, profile=PROFILE_ANALYSIS)
            with self._lock:
                self._calibration = None
                self._exposure_calibration = None
                self._white_balance_calibration = None

        return self._run_sync("configuring", "Setting Nikon shutter", configure)

    def disconnect_camera(self) -> dict[str, object]:
        def disconnect() -> None:
            self.camera.disconnect()
            self._invalidate_calibration()

        return self._run_sync("disconnecting", "Disconnecting Nikon", disconnect)

    def test_camera(self, output_dir: str) -> dict[str, object]:
        base = Path(output_dir).expanduser().resolve()
        target = base / f"camera_test_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.jpg"

        def capture() -> None:
            base.mkdir(parents=True, exist_ok=True)
            shutter = self._current_shutter()
            self.camera.configure(shutter, profile=PROFILE_ANALYSIS)
            self.camera.capture_calibration(target)

        return self._run_sync("capturing", "Capturing test image", capture)

    def stop(self) -> dict[str, object]:
        with self._lock:
            if self._state not in {"calibrating", "scanning", "editing", "moving", "jogging"}:
                raise ServiceStateError(f"Cannot stop while {self._state}")
            self._cancel.set()
            self._state = "stopping"
            self._message = "Stopping"
            self._jog_vector = (0.0, 0.0, 0.0)
            self._jog_deadline = 0.0
        return self.status()

    def emergency_stop(self) -> dict[str, object]:
        self._cancel.set()
        with self._lock:
            self._state = "faulted"
            self._message = "Emergency stop requested"
            self._error = "Emergency stop delivery is not yet confirmed"
            self._calibration = None
            self._exposure_calibration = None
            self._focus_surface = None
            self._white_balance_calibration = None
        try:
            self.printer.emergency_stop()
        except Exception as exc:
            with self._lock:
                self._message = "Emergency stop failed"
                self._error = str(exc)
            raise
        with self._lock:
            self._message = "Emergency stop sent"
            self._error = "Reconnect and initialize the printer before further motion"
        return self.status()

    def set_jog(
        self,
        dx: float,
        dy: float,
        dz: float,
        speed_xy_mm_s: float,
        speed_z_mm_s: float,
    ) -> dict[str, object]:
        values = (float(dx), float(dy), float(dz))
        if not all(math.isfinite(value) and -1.0 <= value <= 1.0 for value in values):
            raise ValueError("Jog vector components must be between -1 and 1")
        if abs(values[2]) > 1e-9 and (abs(values[0]) > 1e-9 or abs(values[1]) > 1e-9):
            raise ValueError("Jog Z separately from XY")
        if not math.isfinite(speed_xy_mm_s) or not 0 < speed_xy_mm_s <= 300:
            raise ValueError("XY jog speed must be between 0 and 300 mm/s")
        if not math.isfinite(speed_z_mm_s) or not 0 < speed_z_mm_s <= 50:
            raise ValueError("Z jog speed must be between 0 and 50 mm/s")
        with self._lock:
            if self._state not in {"idle", "jogging"}:
                raise ServiceStateError(f"Cannot jog while {self._state}")
            new_session = self._state == "idle"
            self._jog_vector = values
            self._jog_speed_xy = speed_xy_mm_s
            self._jog_speed_z = speed_z_mm_s
            self._jog_deadline = self._clock() + 0.6
            if any(abs(value) > 1e-9 for value in values):
                if new_session:
                    self._cancel.clear()
                    self._progress_tracker.reset()
                self._state = "jogging"
                self._message = "Jogging"
                if new_session or self._jog_thread is None or not self._jog_thread.is_alive():
                    self._jog_thread = threading.Thread(target=self._jog_loop, name="scanner-jog", daemon=True)
                    self._jog_thread.start()
        return self.status()

    def shutdown(self) -> None:
        self._cancel.set()
        threads = [thread for thread in (self._operation_thread, self._jog_thread) if thread is not None and thread.is_alive()]
        with self._lock:
            active_motion = self._active_motion
        printer_status = self.printer.status()
        if active_motion and printer_status.connected and not printer_status.faulted:
            self.printer.stop()
        for thread in threads:
            thread.join(timeout=15.0)
            if thread.is_alive():
                raise RuntimeError("Scanner operation did not stop")
        self.camera.disconnect()
        if self.printer.status().connected:
            self.printer.disconnect()

    def _start_operation(
        self,
        state: str,
        target: Callable[[], None],
        *,
        require_printer: bool = True,
        require_camera: bool = True,
        clear_components: frozenset[str] = frozenset(),
        mark_completed: bool = True,
    ) -> None:
        if not self._operation_lock.acquire(blocking=False):
            raise ServiceStateError("Another scanner operation is already running")
        with self._lock:
            if self._state != "idle":
                self._operation_lock.release()
                raise ServiceStateError(f"Cannot start while {self._state}")
            printer = self.printer.status()
            camera = self.camera.status()
            if require_printer and (not printer.connected or not printer.initialized or printer.faulted):
                self._operation_lock.release()
                raise ServiceStateError("Printer must be connected and initialized")
            if require_camera and not camera["connected"]:
                self._operation_lock.release()
                raise ServiceStateError("Nikon J4 must be connected")
            self._cancel.clear()
            self._state = state
            self._message = state.capitalize()
            self._progress_tracker.reset()
            self._error = None
            if state == "scanning":
                self._scan_progress = None
            if clear_components & {"exposure", "focus", "white_balance"}:
                self._calibration = None
            if "exposure" in clear_components:
                self._exposure_calibration = None
            if "focus" in clear_components:
                self._focus_surface = None
            if "white_balance" in clear_components:
                self._white_balance_calibration = None

        def run() -> None:
            try:
                target()
                with self._lock:
                    self._require_not_cancelled()
                    if self._state == state:
                        self._state = "idle"
                        self._message = "Completed" if mark_completed else "Ready"
            except Exception as exc:
                with self._lock:
                    if isinstance(exc, InterruptedError) and self._state == "stopping":
                        self._state = "idle"
                        self._message = "Stopped"
                        self._error = None
                    elif self._state == "faulted":
                        progress = self._progress_tracker.current
                        if (
                            not isinstance(exc, InterruptedError)
                            and progress is not None
                            and progress.phase in {"camera-cleanup", "camera-storage-restore"}
                        ):
                            detail = str(exc) or type(exc).__name__
                            if self._error is None:
                                self._error = detail
                            elif detail not in self._error:
                                self._error = f"{self._error}; active operation failed: {detail}"
                    else:
                        printer_faulted = self.printer.status().faulted
                        self._state = "faulted" if printer_faulted else "idle"
                        self._message = "Printer fault" if printer_faulted else {
                            "calibrating": "Calibration failed",
                            "scanning": "Scan failed",
                            "editing": "Editor apply failed",
                        }.get(state, "Operation failed")
                        self._error = str(exc)
            finally:
                with self._lock:
                    self._active_motion = False
                self._operation_lock.release()

        self._operation_thread = threading.Thread(target=run, name=f"scanner-{state}", daemon=True)
        self._operation_thread.start()

    def _run_sync(
        self,
        state: str,
        message: str,
        target: Callable[[], object],
        *,
        allowed_states: frozenset[str] = frozenset({"idle"}),
    ) -> dict[str, object]:
        if not self._operation_lock.acquire(blocking=False):
            raise ServiceStateError("Another scanner operation is already running")
        with self._lock:
            if self._state not in allowed_states:
                self._operation_lock.release()
                raise ServiceStateError(f"Cannot start while {self._state}")
            self._cancel.clear()
            self._state = state
            self._message = message
            self._progress_tracker.reset()
            self._error = None
        try:
            target()
            with self._lock:
                self._require_not_cancelled()
        except Exception as exc:
            with self._lock:
                stopped = isinstance(exc, InterruptedError) and self._state == "stopping"
                if self._state == "faulted":
                    pass
                elif stopped:
                    self._state = "idle"
                    self._message = "Stopped"
                    self._error = None
                else:
                    self._state = "idle"
                    self._message = (
                        "Stage command completed; preview failed"
                        if isinstance(exc, PreviewCaptureError)
                        else f"{message} failed"
                    )
                    self._error = str(exc)
            if not stopped:
                raise
        finally:
            with self._lock:
                self._active_motion = False
            self._operation_lock.release()
        with self._lock:
            if self._state == state:
                self._state = "idle"
                self._message = "Ready"
                self._error = None
        return self.status()

    def _capture_raw_exposure(
        self,
        directory: Path,
        prefix: str,
        suffix: str,
        phase: str,
        shutter: str,
        roi: NormalizedROI,
    ) -> tuple[CapturePair, ExposureReading, RawHeadroomReading]:
        self._require_not_cancelled()
        self.camera.configure(shutter, profile=PROFILE_RAW)
        self._require_not_cancelled()
        pair = self.camera.capture_scan(
            directory / f"{prefix}_{suffix}.jpg",
            directory / f"{prefix}_{suffix}.nef",
        )
        self._require_not_cancelled()
        image = read_jpeg(pair.jpeg)
        reading = analyze_exposure(image, roi)
        self._require_not_cancelled()
        headroom = analyze_raw_headroom(
            pair.nef,
            roi,
            reference_size=(image.shape[1], image.shape[0]),
        )
        self._require_not_cancelled()
        self._record_exposure(prefix, phase, shutter, reading, profile=PROFILE_RAW, accepted=True)
        self._record_raw_headroom(prefix, phase, shutter, headroom)
        return pair, reading, headroom

    def _run_auto_exposure(
        self,
        directory: Path,
        prefix: str,
        roi: NormalizedROI,
        *,
        verify_raw: bool = False,
    ) -> tuple[str, ExposureReading, CapturePair | None]:
        choices = shutter_choices(self.camera.shutter_choices(), max_seconds=1.0)
        current_shutter = self.camera.status()["shutter"]
        current_index = next((index for index, choice in enumerate(choices) if choice[0] == current_shutter), None)
        if current_index is None:
            raise CalibrationError(f"Current shutter is not usable for auto exposure: {current_shutter!r}")
        tested: dict[int, ExposureReading] = {}
        selected_index: int | None = None
        search_phase = f"{prefix}-exposure-search"
        search_label = prefix.replace("_", " ").title()
        self._set_step_progress(search_phase, search_label, 0, None, "samples")
        for attempt in range(len(choices)):
            self._require_not_cancelled()
            current = choices[current_index]
            self.camera.configure(current[0], profile=PROFILE_ANALYSIS)
            self._require_not_cancelled()
            path = directory / f"{prefix}_search_{attempt:02d}.jpg"
            self.camera.capture_calibration(path)
            self._require_not_cancelled()
            reading = analyze_exposure(read_jpeg(path), roi)
            self._record_exposure(prefix, "search", current[0], reading)
            self._require_not_cancelled()
            tested[current_index] = reading
            self._set_step_progress(
                search_phase,
                search_label,
                attempt + 1,
                None,
                "samples",
            )
            if reading.accepted:
                selected_index = current_index
                break

            under = [
                index
                for index, sample in tested.items()
                if exposure_error_ev(sample) < 0.0
            ]
            over = [
                index
                for index, sample in tested.items()
                if exposure_error_ev(sample) > 0.0
            ]
            brackets = [(lower, upper) for lower in under for upper in over if lower < upper]
            if brackets:
                lower, upper = min(brackets, key=lambda pair: pair[1] - pair[0])
                if upper - lower > 1:
                    current_index = (lower + upper) // 2
                    continue
                selected_index = min((lower, upper), key=lambda index: abs(exposure_error_ev(tested[index])))
                break

            selected = next_shutter(reading, current[1], choices)
            if selected is None:
                selected_index = current_index
                break
            current_index = next(index for index, choice in enumerate(choices) if choice[0] == selected[0])
            if current_index in tested:
                raise RuntimeError("Auto exposure oscillated between shutter speeds")
        if selected_index is None:
            raise RuntimeError("Auto exposure did not converge after testing every shutter speed")

        selected = choices[selected_index]
        search_reading = tested[selected_index]
        verify_phase = f"{prefix}-exposure-verify"
        verify_label = f"{search_label} verification"
        self._set_step_progress(verify_phase, verify_label, 0, 1, "samples")
        self._require_not_cancelled()
        pair: CapturePair | None = None
        raw_headroom: RawHeadroomReading | None = None
        if verify_raw:
            pair, verified, raw_headroom = self._capture_raw_exposure(
                directory,
                prefix,
                "verify",
                "verify",
                selected[0],
                roi,
            )
            profile = PROFILE_RAW
        else:
            self.camera.configure(selected[0], profile=PROFILE_ANALYSIS)
            self._require_not_cancelled()
            verify_path = directory / f"{prefix}_verify.jpg"
            self.camera.capture_calibration(verify_path)
            self._require_not_cancelled()
            verified = analyze_exposure(read_jpeg(verify_path), roi)
            self._require_not_cancelled()
            profile = PROFILE_ANALYSIS
            self._record_exposure(prefix, "verify", selected[0], verified, profile=profile)
        seed_verified = verified
        self._set_step_progress(verify_phase, verify_label, 1, 1, "samples")
        warning: list[str] = []
        if raw_headroom is not None and not raw_headroom.meaningful_saturation:
            raw_selected = next_raw_shutter(raw_headroom.highlight_level, selected[1], choices)
            if raw_selected is not None:
                selected_index = next(
                    index for index, choice in enumerate(choices) if choice[0] == raw_selected[0]
                )
                selected = choices[selected_index]
                optimize_phase = f"{prefix}-raw-exposure-optimization"
                optimize_label = f"{search_label} RAW exposure optimization"
                self._set_step_progress(optimize_phase, optimize_label, 0, 1, "samples")
                pair, verified, raw_headroom = self._capture_raw_exposure(
                    directory,
                    prefix,
                    "optimized",
                    "optimized",
                    selected[0],
                    roi,
                )
                self._set_step_progress(optimize_phase, optimize_label, 1, 1, "samples")
        if raw_headroom is not None and raw_headroom.meaningful_saturation:
            if selected_index > 0:
                selected_index -= 1
                selected = choices[selected_index]
                protect_phase = f"{prefix}-raw-highlight-protection"
                protect_label = f"{search_label} RAW highlight protection"
                self._set_step_progress(protect_phase, protect_label, 0, 1, "samples")
                pair, verified, raw_headroom = self._capture_raw_exposure(
                    directory,
                    prefix,
                    "protected",
                    "protected",
                    selected[0],
                    roi,
                )
                self._set_step_progress(protect_phase, protect_label, 1, 1, "samples")
                warning.append("RAW highlight protection shortened the shutter by one step")
            if raw_headroom.meaningful_saturation:
                warning.append("high scene contrast still saturates part of a RAW channel; using the best available exposure")
        if (
            raw_headroom is not None
            and selected_index == len(choices) - 1
            and raw_headroom.highlight_level < RAW_HIGHLIGHT_TARGET
        ):
            warning.append("RAW highlight target is unavailable at the maximum shutter; using the brightest available exposure")
        if raw_headroom is None and not verified.accepted:
            warning.append("metered brightness is outside the nominal target; using the best available exposure")
        if abs(exposure_error_ev(search_reading) - exposure_error_ev(seed_verified)) > 0.5:
            warning.append("verification brightness differed from the search capture")
        final = replace(
            verified,
            raw_saturated_fraction=None
            if raw_headroom is None
            else max(raw_headroom.saturated_fractions),
            raw_highlight_level=None if raw_headroom is None else raw_headroom.highlight_level,
            warning="; ".join(warning) or None,
        )
        self._record_exposure(prefix, "selected", selected[0], final, profile=profile, accepted=True)
        return selected[0], final, pair

    def _run_quick_auto_exposure(
        self,
        roi: NormalizedROI,
        output_dir: str,
    ) -> None:
        root = self._new_output_directory(output_dir, "exposure")
        shutter, reading, _pair = self._run_auto_exposure(
            root,
            "exposure",
            roi,
            verify_raw=True,
        )
        iso = str(self.camera.status()["iso"])
        result = {
            "iso": iso,
            "shutter": shutter,
            "reading": asdict(reading),
            "roi": asdict(roi),
            "capture_profile": PROFILE_RAW,
        }
        write_json(root / "exposure.json", result)
        with self._lock:
            self._require_not_cancelled()
            self._exposure_calibration = ExposureCalibration(shutter, reading, iso, str(root))
            self._refresh_calibration()

    def _run_quick_auto_focus(
        self,
        roi: NormalizedROI,
        output_dir: str,
        x: float,
        y: float,
        start_z: float,
        x_min: float,
        x_max: float,
        y_min: float,
        y_max: float,
        speed_z_mm_s: float,
        shutter: str,
    ) -> None:
        root = self._new_output_directory(output_dir, "focus")
        self._set_step_progress("autofocus", "Autofocus", 0, None, "samples")
        self._require_not_cancelled()
        self.camera.configure(shutter, profile=PROFILE_ANALYSIS)

        def capture(index: int, z: float) -> Path:
            self._require_not_cancelled()
            return self.camera.capture_calibration(root / f"focus_{index:02d}_z{z:.4f}.jpg")

        def move_focus(z: float) -> None:
            self._require_not_cancelled()
            self._run_motion_command(
                lambda: self.printer.move_absolute(z=z, speed_mm_s=speed_z_mm_s)
            )
            self._sleep(0.25)

        def record_sample(phase: str, index: int, sample: FocusSample) -> None:
            self._set_step_progress("autofocus", "Autofocus", index + 1, None, "samples")
            self._set_message(f"{phase.title()} focus: Z {sample.z:.4f} mm, sharpness {sample.score:.4f}")
            self._record_measurement(
                "Auto focus",
                phase,
                PROFILE_ANALYSIS,
                f"Z {sample.z:.4f} mm",
                f"sharpness {sample.score:.4f}",
                True,
            )

        def record_event(phase: str, message: str, accepted: bool | None) -> None:
            self._set_message(message)
            self._record_measurement(
                "Auto focus",
                f"{phase} search",
                PROFILE_ANALYSIS,
                "decision",
                message,
                accepted,
            )

        peak, samples = run_focus_sweep(
            start_z=start_z,
            z_min=self.printer.bounds.z_min,
            z_max=self.printer.bounds.z_max,
            roi=roi,
            move_z=move_focus,
            capture=capture,
            on_sample=record_sample,
            on_event=record_event,
        )
        self._require_not_cancelled()
        self._set_step_progress("focus-selected", "Saving selected focus", 0, 1, "captures")
        selected_image = self.camera.capture_calibration(root / f"focus_selected_z{peak:.4f}.jpg")
        self._require_not_cancelled()
        self._set_step_progress("focus-selected", "Saving selected focus", 1, 1, "captures")
        self._record_measurement(
            "Auto focus",
            "selected",
            PROFILE_ANALYSIS,
            f"start Z {start_z:.4f} mm",
            f"peak Z {peak:.4f} mm",
            True,
        )
        observation = FocusObservation("single", x, y, peak)
        mesh = FocusMesh(x_min, x_max, y_min, y_max, peak, peak, peak, peak)
        surface = FocusSurfaceCalibration("flat", mesh, (observation,), str(root))
        write_json(
            root / "focus.json",
            {
                "start_z": start_z,
                "peak_z": peak,
                "coverage": {
                    "x_min": x_min,
                    "x_max": x_max,
                    "y_min": y_min,
                    "y_max": y_max,
                },
                "roi": asdict(roi),
                "samples": [asdict(sample) for sample in samples],
                "selected_image": selected_image.name,
                "capture_profile": PROFILE_ANALYSIS,
                "focus_surface": asdict(surface),
            },
        )
        with self._lock:
            self._require_not_cancelled()
            self._focus_surface = surface
            self._refresh_calibration()

    def _run_quick_white_balance(self, roi: NormalizedROI, output_dir: str, shutter: str) -> None:
        root = self._new_output_directory(output_dir, "white_balance")
        balance, recipe = self._calibrate_white_balance(root, roi, shutter)
        self._record_white_balance("Gray white balance", shutter, balance)
        iso = str(self.camera.status()["iso"])
        write_json(
            root / "white_balance.json",
            {
                "iso": iso,
                "shutter": shutter,
                "roi": asdict(roi),
                "white_balance": asdict(balance),
                "raw_development": asdict(recipe),
                "capture_profile": PROFILE_RAW,
            },
        )
        with self._lock:
            self._require_not_cancelled()
            self._white_balance_calibration = WhiteBalanceCalibration(
                balance,
                shutter,
                iso,
                str(root),
                recipe,
            )
            self._refresh_calibration()

    def _calibrate_white_balance(
        self,
        root: Path,
        roi: NormalizedROI,
        shutter: str,
        *,
        pair: CapturePair | None = None,
    ) -> tuple[WhiteBalance, RawDevelopmentRecipe]:
        self._set_step_progress(
            "white-balance-capture",
            "Capturing white balance",
            0,
            1,
            "pairs",
        )
        if pair is None:
            self._require_not_cancelled()
            self.camera.configure(shutter, profile=PROFILE_RAW)
            self._require_not_cancelled()
            pair = self.camera.capture_scan(root / "white_balance.jpg", root / "white_balance.nef")
        self._require_not_cancelled()
        self._set_step_progress(
            "white-balance-capture",
            "Capturing white balance",
            1,
            1,
            "pairs",
        )
        image = read_jpeg(pair.jpeg)
        self._require_not_cancelled()
        reference_size = (image.shape[1], image.shape[0])
        self._set_step_progress(
            "white-balance-gains",
            "Calculating white balance",
            0,
            1,
            "NEFs",
        )
        self._require_not_cancelled()
        balance = calibrate_white_balance(pair.nef, roi, reference_size=reference_size)
        self._require_not_cancelled()
        self._set_step_progress(
            "white-balance-gains",
            "Calculating white balance",
            1,
            1,
            "NEFs",
        )
        self._set_step_progress("raw-recipe", "Calibrating RAW recipe", 0, 1, "recipes")
        self._require_not_cancelled()
        recipe = calibrate_development_recipe(
            pair.nef,
            pair.jpeg,
            balance,
            roi,
            reference_size=reference_size,
        )
        self._set_step_progress("raw-recipe", "Calibrating RAW recipe", 1, 1, "recipes")
        self._require_not_cancelled()
        return balance, recipe

    def _run_focus_grid_calibration(self, plan: FocusGridPlan, shutter: str) -> None:
        root = self._new_output_directory(plan.output_dir, "focus_grid")
        self._require_not_cancelled()
        self.camera.configure(shutter, profile=PROFILE_ANALYSIS)
        surface = self._measure_focus_grid(plan, root)
        with self._lock:
            self._require_not_cancelled()
            self._focus_surface = surface
            self._refresh_calibration()

    def _measure_focus_grid(
        self,
        plan: FocusGridPlan,
        root: Path,
    ) -> FocusSurfaceCalibration:
        x_span = plan.x_max - plan.x_min
        y_span = plan.y_max - plan.y_min
        x0, x1 = plan.x_min + x_span * 0.25, plan.x_min + x_span * 0.75
        y0, y1 = plan.y_min + y_span * 0.25, plan.y_min + y_span * 0.75
        lower_left = ("lower_left", "focus_0_0", x0, y0)
        lower_right = ("lower_right", "focus_0_1", x1, y0)
        upper_left = ("upper_left", "focus_1_0", x0, y1)
        upper_right = ("upper_right", "focus_1_1", x1, y1)
        center = (
            "center",
            "focus_center",
            (plan.x_min + plan.x_max) / 2.0,
            (plan.y_min + plan.y_max) / 2.0,
        )
        points = (
            (lower_left, upper_left, center, lower_right, upper_right)
            if x_span >= y_span
            else (lower_left, lower_right, center, upper_left, upper_right)
        )
        measurements: list[FocusObservation] = []
        for point_index, (name, directory_name, x, y) in enumerate(points):
            self._require_not_cancelled()
            progress_phase = f"focus-grid-{point_index + 1}"
            progress_label = f"Grid autofocus point {point_index + 1} of 5"
            self._set_step_progress(
                progress_phase,
                progress_label,
                0,
                None,
                "samples",
            )
            self._set_message(
                f"{progress_label} ({name.replace('_', ' ')}) at X {x:.2f}, Y {y:.2f}"
            )
            current = self.printer.status().position
            if current is None:
                raise ServiceStateError("Printer position is unavailable")
            self._move_to(x, y, current.z, plan.speed_xy_mm_s, plan.speed_z_mm_s)
            point_dir = root / directory_name
            point_dir.mkdir()

            def capture(index: int, z: float, *, directory: Path = point_dir) -> Path:
                self._require_not_cancelled()
                return self.camera.capture_calibration(directory / f"focus_{index:02d}_z{z:.4f}.jpg")

            def move_focus(z: float) -> None:
                self._require_not_cancelled()
                self._run_motion_command(
                    lambda: self.printer.move_absolute(z=z, speed_mm_s=plan.speed_z_mm_s)
                )
                self._sleep(0.25)

            def record_sample(
                phase: str,
                index: int,
                sample: FocusSample,
                *,
                point: int = point_index + 1,
            ) -> None:
                self._set_step_progress(
                    progress_phase,
                    progress_label,
                    index + 1,
                    None,
                    "samples",
                )
                self._set_message(
                    f"Focus grid point {point}: {phase} Z {sample.z:.4f} mm, sharpness {sample.score:.4f}"
                )
                self._record_measurement(
                    f"Focus grid point {point}",
                    phase,
                    PROFILE_ANALYSIS,
                    f"Z {sample.z:.4f} mm",
                    f"sharpness {sample.score:.4f}",
                    True,
                )

            def record_event(
                phase: str,
                message: str,
                accepted: bool | None,
                *,
                point: int = point_index + 1,
            ) -> None:
                self._set_message(f"Focus grid point {point}: {message}")
                self._record_measurement(
                    f"Focus grid point {point}",
                    f"{phase} search",
                    PROFILE_ANALYSIS,
                    "decision",
                    message,
                    accepted,
                )

            peak, samples = run_focus_sweep(
                start_z=current.z,
                z_min=self.printer.bounds.z_min,
                z_max=self.printer.bounds.z_max,
                roi=plan.focus_roi,
                move_z=move_focus,
                capture=capture,
                on_sample=record_sample,
                on_event=record_event,
            )
            measurements.append(FocusObservation(name, x, y, peak))
            self._record_measurement(
                f"Focus grid point {point_index + 1}",
                "selected",
                PROFILE_ANALYSIS,
                f"X {x:.2f}, Y {y:.2f}, start Z {current.z:.4f} mm",
                f"peak Z {peak:.4f} mm",
                True,
            )
            write_json(point_dir / "samples.json", [asdict(sample) for sample in samples])

        self._set_step_progress("focus-fit", "Fitting focus surface", 0, 1, "surfaces")
        mesh = fit_focus_mesh(
            plan.x_min,
            plan.x_max,
            plan.y_min,
            plan.y_max,
            [
                (measurement.x, measurement.y, measurement.z)
                for measurement in measurements
            ],
        )
        for x, y in (
            (plan.x_min, plan.y_min),
            (plan.x_max, plan.y_min),
            (plan.x_min, plan.y_max),
            (plan.x_max, plan.y_max),
        ):
            self.printer.bounds.require(x, y, mesh.z_at(x, y))
        write_json(
            root / "focus_grid.json",
            {
                "coverage": {
                    "x_min": plan.x_min,
                    "x_max": plan.x_max,
                    "y_min": plan.y_min,
                    "y_max": plan.y_max,
                },
                "node_xs": list(mesh.node_xs),
                "node_ys": list(mesh.node_ys),
                "method": "grid",
                "measurements": [asdict(measurement) for measurement in measurements],
                "mesh": asdict(mesh),
                "capture_profile": PROFILE_ANALYSIS,
            },
        )
        self._set_step_progress("focus-fit", "Fitting focus surface", 1, 1, "surfaces")
        return FocusSurfaceCalibration("grid", mesh, tuple(measurements), str(root))

    def _run_calibration(self, plan: CalibrationPlan) -> None:
        root = self._new_output_directory(plan.output_dir, "calibration")
        initial = self.printer.status().position
        if initial is None:
            raise ServiceStateError("Printer position is unavailable")
        self._run_auto_exposure(
            root,
            "pre_exposure",
            plan.exposure_roi,
        )

        grid_plan = FocusGridPlan(
            plan.x_min,
            plan.x_max,
            plan.y_min,
            plan.y_max,
            plan.focus_roi,
            plan.output_dir,
            plan.speed_xy_mm_s,
            plan.speed_z_mm_s,
        )
        surface = self._measure_focus_grid(grid_plan, root)
        mesh = surface.mesh
        exposure_z = mesh.z_at(initial.x, initial.y)
        self._move_to(initial.x, initial.y, exposure_z, plan.speed_xy_mm_s, plan.speed_z_mm_s)
        shutter, reading, exposure_pair = self._run_auto_exposure(
            root,
            "final_exposure",
            plan.exposure_roi,
            verify_raw=True,
        )
        self._require_not_cancelled()
        balance, recipe = self._calibrate_white_balance(
            root,
            plan.gray_roi,
            shutter,
            pair=exposure_pair,
        )
        self._record_white_balance("Calibration", shutter, balance)
        iso = str(self.camera.status()["iso"])
        surface = replace(surface, directory=str(root))
        result = CalibrationResult(surface, shutter, reading, balance, str(root), iso, recipe)
        write_json(
            root / "calibration.json",
            {
                "plan": asdict(plan),
                "result": {
                    "mesh": asdict(mesh),
                    "focus_method": surface.method,
                    "focus_measurements": [
                        asdict(measurement) for measurement in surface.measurements
                    ],
                    "iso": iso,
                    "shutter": shutter,
                    "exposure": asdict(reading),
                    "white_balance": asdict(balance),
                    "raw_development": asdict(recipe),
                    "capture_profiles": {
                        "preliminary_exposure": PROFILE_ANALYSIS,
                        "final_exposure": PROFILE_RAW,
                        "focus": PROFILE_ANALYSIS,
                        "white_balance": PROFILE_RAW,
                    },
                },
            },
        )
        with self._lock:
            self._require_not_cancelled()
            self._exposure_calibration = ExposureCalibration(shutter, reading, iso, str(root))
            self._focus_surface = surface
            self._white_balance_calibration = WhiteBalanceCalibration(
                balance,
                shutter,
                iso,
                str(root),
                recipe,
            )
            self._calibration = result

    def _run_validated_scan(self, plan: ScanPlan) -> None:
        with self._lock:
            self._require_not_cancelled()
            calibration = self._calibration
            if calibration is None:
                raise ServiceStateError("Complete calibration before scanning")
            surface = self._focus_surface_for_scan(plan, calibration)
            if surface is not calibration.focus_surface:
                self._focus_surface = surface
                self._calibration = replace(calibration, focus_surface=surface)
        self._run_scan(plan)

    def _run_scan(self, plan: ScanPlan) -> None:
        calibration = self._calibration
        if calibration is None:
            raise ServiceStateError("Complete calibration before scanning")
        surface = calibration.focus_surface
        mesh = surface.mesh
        camera = self.camera.status()
        shutter = self._current_shutter()
        iso = str(camera["iso"])
        requested_step_x = plan.frame_width_mm * (1.0 - plan.overlap_percent / 100.0)
        requested_step_y = plan.frame_height_mm * (1.0 - plan.overlap_percent / 100.0)
        xs = scan_centers(plan.x_min, plan.x_max, plan.frame_width_mm, requested_step_x)
        ys = scan_centers(plan.y_min, plan.y_max, plan.frame_height_mm, requested_step_y)
        points = tuple(
            ScanPoint(x, y, row, column)
            for row, y in enumerate(ys)
            for column, x in (
                reversed(tuple(enumerate(xs)))
                if row % 2
                else enumerate(xs)
            )
        )
        step_x = 0.0 if len(xs) == 1 else xs[1] - xs[0]
        step_y = 0.0 if len(ys) == 1 else ys[1] - ys[0]
        root = self._new_output_directory(plan.output_dir, "scan")
        self._register_editor_scan_root(root.parent)
        self._set_step_progress(
            "prepare-openexr",
            "Preparing OpenEXR writer",
            0,
            1,
            "writers",
        )
        openexr_helper = self._prepare_openexr_helper()
        self._require_not_cancelled()
        self._set_step_progress(
            "prepare-openexr",
            "Preparing OpenEXR writer",
            1,
            1,
            "writers",
        )
        params = {
            **asdict(plan),
            "step_x_mm": step_x,
            "step_y_mm": step_y,
            "requested_step_x_mm": requested_step_x,
            "requested_step_y_mm": requested_step_y,
            "image_roles": "raw",
            "capture_profile": PROFILE_RAW,
            "serpentine": True,
            "shutter": shutter,
            "iso": iso,
            "exposure": asdict(calibration.exposure),
            "focus_mesh": asdict(mesh),
            "focus_method": surface.method,
            "focus_measurements": [
                asdict(measurement) for measurement in surface.measurements
            ],
            "white_balance": asdict(calibration.white_balance),
            "raw_development_recipe": "raw_development.json",
        }
        write_json(root / "scan_params.json", params)
        write_json(root / "raw_development.json", asdict(calibration.raw_recipe))
        with self._lock:
            self._last_scan_dir = str(root)
        self._require_not_cancelled()
        self.camera.configure(shutter, profile=PROFILE_RAW)
        self._require_not_cancelled()
        with self._lock:
            self._scan_progress = ScanProgress(points, 0, 0)
        captures: list[dict[str, object]] = []
        deferred: list[tuple[ScanCapture, RemoteCapturePair]] = []
        owned_remote_pairs: list[RemoteCapturePair] = []
        remote_captures: list[dict[str, object]] = []
        tiles: list[dict[str, object]] = []
        total = len(points)
        captured = 0
        developed = 0
        output_size: tuple[int, int] | None = None
        pending: Future[dict[str, object]] | None = None
        with (
            ExitStack() as camera_cleanup,
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="scan-raw") as executor,
        ):
            if plan.quick_acquisition:
                self._require_not_cancelled()
                self._set_step_progress(
                    "camera-storage-select",
                    "Selecting camera storage",
                    0,
                    1,
                    "targets",
                )
                self._require_not_cancelled()
                previous_capture_storage = self.camera.use_camera_storage()
                camera_cleanup.callback(
                    self._cleanup_quick_camera,
                    owned_remote_pairs,
                    previous_capture_storage,
                )
                self._set_step_progress(
                    "camera-storage-select",
                    "Selecting camera storage",
                    1,
                    1,
                    "targets",
                )
                self._set_step_progress(
                    "quick-capture",
                    "Quick capture",
                    0,
                    total,
                    "tiles",
                )
            else:
                self._set_step_progress(
                    "scan-acquire-develop",
                    "Capturing and developing",
                    0,
                    total,
                    "tiles",
                )
            for index, point in enumerate(points):
                self._require_not_cancelled()
                z = mesh.z_at(point.x, point.y)
                self._set_message(
                    f"Quick capture to camera {captured + 1}/{total}"
                    if plan.quick_acquisition
                    else f"Capturing tile {captured + 1}/{total}"
                )
                self._update_scan_progress(captured, index)
                self._move_to(point.x, point.y, z, plan.speed_xy_mm_s, plan.speed_z_mm_s)
                if plan.settle_ms:
                    self._sleep(plan.settle_ms / 1000.0)
                self._require_not_cancelled()
                stem = (
                    f"tile_r{point.row:03d}_c{point.col:03d}"
                    f"_x{point.x:.2f}_y{point.y:.2f}"
                )
                capture = ScanCapture(
                    point.row,
                    point.col,
                    point.x,
                    point.y,
                    z,
                    f"{stem}.jpg",
                    f"{stem}.nef",
                    PROFILE_RAW,
                )
                if plan.quick_acquisition:
                    sources = self.camera.capture_scan_to_camera()
                    owned_remote_pairs.append(sources)
                    deferred.append((capture, sources))
                    remote_captures.append(
                        {"capture": asdict(capture), "remote": asdict(sources)}
                    )
                    write_json(root / "camera_captures.json", remote_captures)
                    self._require_not_cancelled()
                    captured += 1
                    self._update_scan_progress(captured, index)
                    self._set_step_progress(
                        "quick-capture",
                        "Quick capture",
                        captured,
                        total,
                        "tiles",
                    )
                    continue

                pair = self.camera.capture_scan(
                    root / capture.file,
                    root / capture.raw_file,
                )
                if output_size is None:
                    alignment_image = read_jpeg(pair.jpeg)
                    output_size = (alignment_image.shape[1], alignment_image.shape[0])
                captures.append(asdict(capture))
                write_json(root / "captures.json", captures)
                self._require_not_cancelled()
                captured += 1
                self._update_scan_progress(captured, index)
                if pending is not None:
                    tile = pending.result()
                    self._require_not_cancelled()
                    developed += 1
                    tiles.append(tile)
                    write_json(root / "tiles.json", tiles)
                    self._set_step_progress(
                        "scan-acquire-develop",
                        "Capturing and developing",
                        developed,
                        total,
                        "tiles",
                    )
                pending = executor.submit(
                    self._develop_scan_capture,
                    capture,
                    root,
                    calibration.raw_recipe,
                    output_size,
                )

            if plan.quick_acquisition:
                self._set_step_progress(
                    "quick-import-develop",
                    "Importing and developing",
                    0,
                    total,
                    "tiles",
                )
                for import_index, (capture, sources) in enumerate(deferred):
                    self._require_not_cancelled()
                    self._set_message(f"Importing camera tile {import_index + 1}/{total}")
                    self._update_scan_progress(captured, None)
                    pair = self.camera.download_scan(
                        sources,
                        root / capture.file,
                        root / capture.raw_file,
                    )
                    if output_size is None:
                        alignment_image = read_jpeg(pair.jpeg)
                        output_size = (alignment_image.shape[1], alignment_image.shape[0])
                    captures.append(asdict(capture))
                    write_json(root / "captures.json", captures)
                    self._require_not_cancelled()
                    if pending is not None:
                        tile = pending.result()
                        self._require_not_cancelled()
                        developed += 1
                        tiles.append(tile)
                        write_json(root / "tiles.json", tiles)
                        self._set_step_progress(
                            "quick-import-develop",
                            "Importing and developing",
                            developed,
                            total,
                            "tiles",
                        )
                    pending = executor.submit(
                        self._develop_scan_capture,
                        capture,
                        root,
                        calibration.raw_recipe,
                        output_size,
                    )

            self._set_message(f"Developing tile {developed + 1}/{total}")
            self._update_scan_progress(captured, None)
            if pending is None:
                raise RuntimeError("Scan produced no captures")
            tile = pending.result()
            self._require_not_cancelled()
            developed += 1
            tiles.append(tile)
            write_json(root / "tiles.json", tiles)
            if plan.quick_acquisition:
                self._set_step_progress(
                    "quick-import-develop",
                    "Importing and developing",
                    developed,
                    total,
                    "tiles",
                )
            else:
                self._set_step_progress(
                    "scan-acquire-develop",
                    "Capturing and developing",
                    developed,
                    total,
                    "tiles",
                )

        self._update_scan_progress(captured, None)
        stitch_scan_outputs(
            tiles=tiles,
            out_dir=str(root),
            build_pyramidal_tiff=True,
            tiff_compression="deflate",
            image_roles="raw",
            openexr_helper=openexr_helper,
            progress_cb=self._set_step_progress,
            cancel_cb=self._require_not_cancelled,
            stitch_settings={
                "final_megapix": -1,
                "layout_blend": "feather",
                "layout_refine_positions": True,
                "layout_refine_exposure": False,
                "use_memmap": True,
                "preview_max_dim": 2000,
                "preview_quality": 88,
            },
        )
        with self._lock:
            self._require_not_cancelled()
            self._scan_progress = ScanProgress(points, total, None)

    def _develop_scan_capture(
        self,
        capture: ScanCapture,
        root: Path,
        recipe: RawDevelopmentRecipe,
        output_size: tuple[int, int],
    ) -> dict[str, object]:
        stem = Path(capture.raw_file).stem
        developed = develop_nef(
            root / capture.raw_file,
            root / f"{stem}.tif",
            root / f"{stem}_scene_linear.tif",
            recipe,
            output_size=output_size,
        )
        return {
            **asdict(capture),
            "display_file": developed.display_path.name,
            "scene_linear_file": developed.scene_linear_path.name,
        }

    def _cleanup_quick_camera(
        self,
        pairs: list[RemoteCapturePair],
        capture_target: str,
    ) -> None:
        completed = [0]
        total = len(pairs)
        self._set_step_progress(
            "camera-cleanup",
            "Cleaning camera files",
            0,
            total,
            "pairs",
        )
        with ExitStack() as cleanup:
            cleanup.callback(
                self._restore_camera_storage_progress,
                capture_target,
            )
            for pair in pairs:
                cleanup.callback(
                    self._delete_camera_pair_progress,
                    pair,
                    completed,
                    total,
                )

    def _delete_camera_pair_progress(
        self,
        pair: RemoteCapturePair,
        completed: list[int],
        total: int,
    ) -> None:
        self.camera.delete_scan(pair)
        completed[0] += 1
        self._set_step_progress(
            "camera-cleanup",
            "Cleaning camera files",
            completed[0],
            total,
            "pairs",
        )

    def _restore_camera_storage_progress(self, capture_target: str) -> None:
        self._set_step_progress(
            "camera-storage-restore",
            "Restoring camera storage",
            0,
            1,
            "targets",
        )
        self.camera.restore_capture_storage(capture_target)
        self._set_step_progress(
            "camera-storage-restore",
            "Restoring camera storage",
            1,
            1,
            "targets",
        )

    def _update_scan_progress(
        self,
        completed: int,
        current_index: int | None,
    ) -> None:
        with self._lock:
            if self._scan_progress is None:
                raise RuntimeError("Scan progress is unavailable")
            self._scan_progress = ScanProgress(
                self._scan_progress.points,
                completed,
                current_index,
            )

    def _jog_loop(self) -> None:
        try:
            preview = False
            while True:
                self._require_not_cancelled()
                with self._lock:
                    now = self._clock()
                    vector = self._jog_vector if now <= self._jog_deadline else (0.0, 0.0, 0.0)
                    speed = self._jog_speed_z if abs(vector[2]) > 1e-9 else self._jog_speed_xy
                    if not any(abs(value) > 1e-9 for value in vector):
                        self._jog_vector = (0.0, 0.0, 0.0)
                        preview = self._state == "jogging"
                        break
                duration = 0.15
                dx = vector[0] * speed * duration
                dy = vector[1] * speed * duration
                dz = vector[2] * speed * duration
                with self._lock:
                    focus_surface = self._focus_surface
                if focus_surface is not None and abs(vector[2]) <= 1e-9:
                    position = self.printer.status().position
                    if position is None:
                        raise ServiceStateError("Printer position is unavailable")
                    x = position.x + dx
                    y = position.y + dy
                    self._run_motion_command(
                        lambda: self.printer.move_absolute(
                            x=x,
                            y=y,
                            z=focus_surface.mesh.z_at(x, y),
                            speed_mm_s=speed,
                        )
                    )
                else:
                    self._run_motion_command(
                        lambda: self.printer.move_relative(
                            dx=dx,
                            dy=dy,
                            dz=dz,
                            speed_mm_s=speed,
                        )
                    )
                self._require_not_cancelled()
            self._require_not_cancelled()
            if preview:
                self._capture_motion_preview()
            with self._lock:
                self._require_not_cancelled()
                if self._state == "jogging":
                    self._state = "idle"
                    self._message = "Ready"
        except PreviewCaptureError as exc:
            with self._lock:
                if self._state != "faulted":
                    self._state = "idle"
                    self._message = "Jog completed; preview failed"
                    self._error = str(exc)
        except Exception as exc:
            with self._lock:
                if isinstance(exc, InterruptedError) and self._state == "stopping":
                    self._state = "idle"
                    self._message = "Stopped"
                    self._error = None
                elif self._state == "faulted":
                    pass
                else:
                    self._state = "faulted" if self.printer.status().faulted else "idle"
                    self._message = "Jog failed"
                    self._error = str(exc)

    def _run_motion_command(self, command: Callable[[], object]) -> None:
        with self._lock:
            self._require_not_cancelled()
            if self._active_motion:
                raise ServiceStateError("Another printer motion command is already running")
            self._active_motion = True
        try:
            command()
        finally:
            with self._lock:
                self._active_motion = False

    def _move_to(self, x: float, y: float, z: float, speed_xy_mm_s: float, speed_z_mm_s: float) -> None:
        self._require_not_cancelled()
        self._run_motion_command(
            lambda: self.printer.move_absolute(z=z, speed_mm_s=speed_z_mm_s)
        )
        self._require_not_cancelled()
        self._run_motion_command(
            lambda: self.printer.move_absolute(x=x, y=y, speed_mm_s=speed_xy_mm_s)
        )
        self._require_not_cancelled()

    def _capture_motion_preview(self) -> None:
        self._require_not_cancelled()
        if not self.camera.status()["connected"]:
            return
        try:
            self._preview_dir.mkdir(parents=True, exist_ok=True)
            target = self._preview_dir / f"latest_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.jpg"
            previous = self._auto_preview_path
            self.camera.capture_preview(target)
            self._auto_preview_path = target
            if previous is not None and previous.is_file():
                previous.unlink()
        except Exception as exc:
            raise PreviewCaptureError(
                f"Stage command completed, but automatic preview capture failed: {exc}"
            ) from exc

    def _invalidate_calibration(self) -> None:
        with self._lock:
            self._calibration = None
            self._exposure_calibration = None
            self._focus_surface = None
            self._white_balance_calibration = None

    def _refresh_calibration(self) -> None:
        exposure = self._exposure_calibration
        focus_surface = self._focus_surface
        white_balance = self._white_balance_calibration
        if exposure is None or focus_surface is None or white_balance is None:
            self._calibration = None
            return
        if exposure.iso != white_balance.iso or exposure.shutter != white_balance.shutter:
            raise RuntimeError("Exposure and white balance do not match the global camera settings")
        camera = self.camera.status()
        if str(camera["iso"]) != exposure.iso or camera["shutter"] != exposure.shutter:
            raise RuntimeError("Calibration does not match the current global camera settings")
        self._calibration = CalibrationResult(
            focus_surface,
            exposure.shutter,
            exposure.reading,
            white_balance.balance,
            focus_surface.directory,
            exposure.iso,
            white_balance.raw_recipe,
        )

    def _validate_calibration_plan(self, plan: CalibrationPlan) -> None:
        printer = self.printer.status()
        if not printer.initialized or printer.position is None:
            raise ServiceStateError("Initialize printer coordinates before calibration")
        position = printer.position
        if not plan.x_min <= position.x <= plan.x_max or not plan.y_min <= position.y <= plan.y_max:
            raise ValueError("Current exposure position must be inside the calibration area")
        self.printer.bounds.require(plan.x_min, plan.y_min, position.z)
        self.printer.bounds.require(plan.x_max, plan.y_max, position.z)

    def _load_editor_project(self, project_dir: str):
        with self._lock:
            roots = self._editor_scan_roots
        return load_editor_project(project_dir, roots)

    def _validate_focus_grid_plan(self, plan: FocusGridPlan) -> None:
        printer = self.printer.status()
        if not printer.initialized or printer.position is None:
            raise ServiceStateError("Initialize printer coordinates before grid autofocus")
        self.printer.bounds.require(plan.x_min, plan.y_min, printer.position.z)
        self.printer.bounds.require(plan.x_max, plan.y_max, printer.position.z)

    def _validate_flat_focus_bounds(
        self,
        x_min: float,
        x_max: float,
        y_min: float,
        y_max: float,
        x: float,
        y: float,
        z: float,
    ) -> None:
        if not all(math.isfinite(value) for value in (x_min, x_max, y_min, y_max)):
            raise ValueError("Focus coverage must be finite")
        if x_min >= x_max or y_min >= y_max:
            raise ValueError("Focus coverage must have positive dimensions")
        if not x_min <= x <= x_max or not y_min <= y <= y_max:
            raise ValueError("Current focus position must be inside the scan bounds")
        self.printer.bounds.require(x_min, y_min, z)
        self.printer.bounds.require(x_max, y_max, z)

    def _validate_scan_plan(self, plan: ScanPlan) -> None:
        with self._lock:
            calibration = self._calibration
        if calibration is None:
            raise ServiceStateError("Complete calibration before scanning")
        self._focus_surface_for_scan(plan, calibration)

    def _focus_surface_for_scan(
        self,
        plan: ScanPlan,
        calibration: CalibrationResult,
    ) -> FocusSurfaceCalibration:
        surface = calibration.focus_surface
        mesh = surface.mesh
        expected = (mesh.x_min, mesh.x_max, mesh.y_min, mesh.y_max)
        actual = (plan.x_min, plan.x_max, plan.y_min, plan.y_max)
        if any(not math.isclose(left, right, abs_tol=1e-6) for left, right in zip(expected, actual)):
            if surface.method == "grid":
                raise ValueError("Scan bounds must match the calibrated grid focus surface")
            z = surface.measurements[0].z
            mesh = FocusMesh(plan.x_min, plan.x_max, plan.y_min, plan.y_max, z, z, z, z)
            surface = replace(surface, mesh=mesh)
        for x, y in (
            (plan.x_min, plan.y_min),
            (plan.x_max, plan.y_min),
            (plan.x_min, plan.y_max),
            (plan.x_max, plan.y_max),
        ):
            self.printer.bounds.require(x, y, mesh.z_at(x, y))
        return surface

    @staticmethod
    def _new_output_directory(base_dir: str, prefix: str) -> Path:
        base = Path(base_dir).expanduser().resolve()
        base.mkdir(parents=True, exist_ok=True)
        root = base / f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
        root.mkdir(exist_ok=False)
        return root

    def _load_editor_scan_roots(self) -> tuple[Path, ...]:
        default = self._scan_root.resolve()
        if not self._scan_roots_path.exists():
            return (default,)
        with self._scan_roots_path.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
        if not isinstance(payload, dict) or set(payload) != {"version", "scan_roots"}:
            raise ValueError("Malformed editor scan-root index")
        if type(payload["version"]) is not int or payload["version"] != 1:
            raise ValueError("Unsupported editor scan-root index version")
        values = payload["scan_roots"]
        if not isinstance(values, list):
            raise ValueError("Malformed editor scan-root records")
        roots = [default]
        for value in values:
            if not isinstance(value, str) or not value or value != value.strip() or not Path(value).is_absolute():
                raise ValueError("Editor scan roots must be absolute paths")
            root = Path(value).resolve()
            if root not in roots:
                roots.append(root)
        return tuple(roots)

    def _register_editor_scan_root(self, root: str | Path) -> None:
        resolved = Path(root).expanduser().resolve()
        with self._lock:
            if resolved in self._editor_scan_roots:
                return
            roots = (*self._editor_scan_roots, resolved)
            self._scan_roots_path.parent.mkdir(parents=True, exist_ok=True)
            write_json(
                self._scan_roots_path,
                {"version": 1, "scan_roots": [str(path) for path in roots]},
            )
            self._editor_scan_roots = roots

    def _prepare_openexr_helper(self) -> Path:
        source = Path(__file__).resolve().parents[1] / "tools" / "write_openexr.cpp"
        digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
        runtime_dir = self._preview_dir / ".runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        helper = runtime_dir / f"write_openexr-{digest}"
        if helper.exists():
            if not helper.is_file() or not os.access(helper, os.X_OK):
                raise RuntimeError(f"Cached OpenEXR helper is not executable: {helper}")
            return helper
        return build_openexr_helper(source_path=source, output_path=helper)

    def _require_not_cancelled(self) -> None:
        if self._cancel.is_set():
            raise InterruptedError("Operation cancelled")

    def _set_step_progress(
        self,
        phase: str,
        label: str,
        completed: int,
        total: int | None,
        unit: str,
    ) -> None:
        with self._lock:
            self._progress_tracker.update(phase, label, completed, total, unit)

    def _set_message(self, message: str) -> None:
        with self._lock:
            self._message = message

    def _record_exposure(
        self,
        operation: str,
        phase: str,
        shutter: str,
        reading: ExposureReading,
        *,
        profile: str = PROFILE_ANALYSIS,
        accepted: bool | None = None,
    ) -> None:
        self._record_measurement(
            operation.replace("_", " ").title(),
            phase,
            profile,
            f"ISO {self.camera.status()['iso']} / {shutter}",
            f"EV {exposure_error_ev(reading):+.3f}, meter {reading.metered_luminance:.1f}, "
            f"P99 {reading.percentile_99:.1f}, JPEG clipped {reading.clipped_fraction * 100:.3f}%",
            reading.accepted if accepted is None else accepted,
        )

    def _record_raw_headroom(
        self,
        operation: str,
        phase: str,
        shutter: str,
        reading: RawHeadroomReading,
    ) -> None:
        values = ", ".join(
            f"CFA{index} P99 {level * 100:.1f}%, saturated {fraction * 100:.3f}%"
            for index, (level, fraction) in enumerate(
                zip(reading.percentile_99_levels, reading.saturated_fractions, strict=True)
            )
        )
        self._record_measurement(
            operation.replace("_", " ").title(),
            f"{phase} RAW",
            PROFILE_RAW,
            f"ISO {self.camera.status()['iso']} / {shutter}",
            f"Sensor levels: {values}",
            True,
        )

    def _record_white_balance(self, operation: str, shutter: str, balance: WhiteBalance) -> None:
        self._record_measurement(
            operation,
            "selected",
            PROFILE_RAW,
            f"ISO {self.camera.status()['iso']} / {shutter}",
            f"R {balance.red:.3f}, G1 {balance.green:.3f}, B {balance.blue:.3f}, G2 {balance.green_2:.3f}",
            True,
        )

    def _record_measurement(
        self,
        operation: str,
        phase: str,
        profile: str,
        parameter: str,
        result: str,
        accepted: bool | None,
    ) -> None:
        with self._lock:
            self._measurement_sequence += 1
            self._measurements.append(
                MeasurementRecord(
                    self._measurement_sequence,
                    datetime.now().astimezone().isoformat(timespec="seconds"),
                    operation,
                    phase,
                    profile,
                    parameter,
                    result,
                    accepted,
                )
            )

    def _current_shutter(self) -> str:
        shutter = self.camera.status()["shutter"]
        if not isinstance(shutter, str) or not shutter:
            raise ServiceStateError("Select a global shutter speed before capture")
        return shutter

    @staticmethod
    def _finite_number(value: float, name: str) -> float:
        if isinstance(value, bool):
            raise ValueError(f"{name} must be finite")
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError(f"{name} must be finite")
        return parsed

    @classmethod
    def _positive_number(cls, value: float, name: str) -> float:
        parsed = cls._finite_number(value, name)
        if parsed <= 0:
            raise ValueError(f"{name} must be positive")
        return parsed

    @classmethod
    def _bounded_speed(cls, value: float, name: str, maximum: float) -> float:
        parsed = cls._positive_number(value, name)
        if parsed > maximum:
            raise ValueError(f"{name} must not exceed {maximum:g} mm/s")
        return parsed
