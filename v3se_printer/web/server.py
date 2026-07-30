from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from gphoto2 import GPhoto2Error
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    StrictBool,
    StrictInt,
    StringConstraints,
    ValidationError,
    model_validator,
)

from ..calibration import CalibrationError, NormalizedROI
from ..editor import EDITOR_RESULT_FILES, EditRecipe
from ..printer import PrinterError, PrinterStateError
from ..service import (
    SCAN_RESULT_FILES,
    CalibrationPlan,
    FocusGridPlan,
    ScannerService,
    ScanPlan,
    ServiceStateError,
)


DEFAULT_STATIC_DIR = Path(__file__).with_name("static")
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
PositiveFloat = Annotated[FiniteFloat, Field(gt=0)]
XYSpeed = Annotated[FiniteFloat, Field(gt=0, le=300)]
ZSpeed = Annotated[FiniteFloat, Field(gt=0, le=50)]
NonNegativeFloat = Annotated[FiniteFloat, Field(ge=0)]
UnitVector = Annotated[FiniteFloat, Field(ge=-1, le=1)]


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PrinterConnectRequest(RequestModel):
    port: Text
    baud: StrictInt = Field(default=115200, gt=0)
    eol: Literal["crlf", "lf"] = "crlf"


class MoveRequest(RequestModel):
    x: FiniteFloat | None = None
    y: FiniteFloat | None = None
    z: FiniteFloat | None = None
    speed_xy_mm_s: XYSpeed = 200.0
    speed_z_mm_s: ZSpeed = 10.0

    @model_validator(mode="after")
    def require_axis(self) -> "MoveRequest":
        if self.x is None and self.y is None and self.z is None:
            raise ValueError("At least one target axis is required")
        return self


class CameraTestRequest(RequestModel):
    output_dir: Text


class ROIRequest(RequestModel):
    x: NonNegativeFloat = 0.2
    y: NonNegativeFloat = 0.2
    width: PositiveFloat = 0.6
    height: PositiveFloat = 0.6

    @model_validator(mode="after")
    def require_containment(self) -> "ROIRequest":
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError("ROI must fit inside the image")
        return self

    def normalized(self) -> NormalizedROI:
        return NormalizedROI(**self.model_dump())


class CalibrationStartRequest(RequestModel):
    x_min: FiniteFloat
    x_max: FiniteFloat
    y_min: FiniteFloat
    y_max: FiniteFloat
    exposure_roi: ROIRequest
    focus_roi: ROIRequest
    gray_roi: ROIRequest
    output_dir: Text
    speed_xy_mm_s: XYSpeed = 200.0
    speed_z_mm_s: ZSpeed = 10.0

    def plan(self) -> CalibrationPlan:
        return CalibrationPlan(
            x_min=self.x_min,
            x_max=self.x_max,
            y_min=self.y_min,
            y_max=self.y_max,
            exposure_roi=self.exposure_roi.normalized(),
            focus_roi=self.focus_roi.normalized(),
            gray_roi=self.gray_roi.normalized(),
            output_dir=self.output_dir,
            speed_xy_mm_s=self.speed_xy_mm_s,
            speed_z_mm_s=self.speed_z_mm_s,
        )


class FocusGridStartRequest(RequestModel):
    x_min: FiniteFloat
    x_max: FiniteFloat
    y_min: FiniteFloat
    y_max: FiniteFloat
    focus_roi: ROIRequest
    output_dir: Text
    speed_xy_mm_s: XYSpeed = 200.0
    speed_z_mm_s: ZSpeed = 10.0

    def plan(self) -> FocusGridPlan:
        return FocusGridPlan(
            x_min=self.x_min,
            x_max=self.x_max,
            y_min=self.y_min,
            y_max=self.y_max,
            focus_roi=self.focus_roi.normalized(),
            output_dir=self.output_dir,
            speed_xy_mm_s=self.speed_xy_mm_s,
            speed_z_mm_s=self.speed_z_mm_s,
        )


class ScanStartRequest(RequestModel):
    x_min: FiniteFloat
    x_max: FiniteFloat
    y_min: FiniteFloat
    y_max: FiniteFloat
    frame_width_mm: PositiveFloat
    frame_height_mm: PositiveFloat
    overlap_percent: FiniteFloat
    output_dir: Text
    speed_xy_mm_s: XYSpeed = 200.0
    speed_z_mm_s: ZSpeed = 10.0
    settle_ms: StrictInt = Field(default=250, ge=0, le=5000)
    quick_acquisition: StrictBool = False

    def plan(self) -> ScanPlan:
        return ScanPlan(**self.model_dump())


class JogRequest(RequestModel):
    dx: UnitVector
    dy: UnitVector
    dz: UnitVector
    speed_xy_mm_s: XYSpeed = 100.0
    speed_z_mm_s: ZSpeed = 10.0

    @model_validator(mode="after")
    def separate_z(self) -> "JogRequest":
        if self.dz != 0 and (self.dx != 0 or self.dy != 0):
            raise ValueError("Jog Z separately from XY")
        return self


class CameraSettingsRequest(RequestModel):
    iso: Text


class CameraShutterRequest(RequestModel):
    shutter: Text


class AutoExposureRequest(RequestModel):
    exposure_roi: ROIRequest
    output_dir: Text


class AutoFocusRequest(RequestModel):
    x_min: FiniteFloat
    x_max: FiniteFloat
    y_min: FiniteFloat
    y_max: FiniteFloat
    focus_roi: ROIRequest
    output_dir: Text
    speed_z_mm_s: ZSpeed = 10.0


class WhiteBalanceRequest(RequestModel):
    gray_roi: ROIRequest
    output_dir: Text


class EditRecipeRequest(RequestModel):
    version: StrictInt = 1
    material: Literal["positive", "negative"] = "positive"
    exposure_ev: FiniteFloat = 0.0
    temperature: FiniteFloat = 0.0
    tint: FiniteFloat = 0.0
    contrast: FiniteFloat = 0.0
    highlights: FiniteFloat = 0.0
    shadows: FiniteFloat = 0.0
    black_point: FiniteFloat = 0.0
    white_point: FiniteFloat = 1.0
    saturation: FiniteFloat = 1.0
    red_balance: FiniteFloat = 1.0
    green_balance: FiniteFloat = 1.0
    blue_balance: FiniteFloat = 1.0
    film_base_red: FiniteFloat = 1.0
    film_base_green: FiniteFloat = 1.0
    film_base_blue: FiniteFloat = 1.0
    film_density: FiniteFloat = 1.0

    def value(self) -> EditRecipe:
        return EditRecipe(**self.model_dump())


class EditorProjectRequest(RequestModel):
    project_dir: Text


class EditorPreviewRequest(EditorProjectRequest):
    source: Literal["mosaic", "tile"]
    tile_index: StrictInt | None = None
    recipe: EditRecipeRequest

    @model_validator(mode="after")
    def validate_source(self) -> "EditorPreviewRequest":
        if self.source == "mosaic" and self.tile_index is not None:
            raise ValueError("Mosaic preview does not accept a tile index")
        if self.source == "tile" and self.tile_index is None:
            raise ValueError("Tile preview requires a tile index")
        return self


class EditorApplyRequest(EditorProjectRequest):
    recipe: EditRecipeRequest


def _validation_detail(error: RequestValidationError | ValidationError) -> str:
    item = error.errors()[0]
    location = ".".join(str(part) for part in item["loc"] if part != "body")
    return f"{location}: {item['msg']}" if location else str(item["msg"])


async def _validation_error(_request: Request, error: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": _validation_detail(error)})


async def _value_error(_request: Request, error: Exception) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": str(error)})


async def _not_found_error(_request: Request, error: Exception) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(error)})


async def _conflict_error(_request: Request, error: Exception) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(error)})


async def _hardware_error(_request: Request, error: Exception) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(error)})


def create_app(
    service: ScannerService | None = None,
    *,
    static_dir: str | Path | None = DEFAULT_STATIC_DIR,
) -> FastAPI:
    scanner = ScannerService() if service is None else service

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            scanner.shutdown()

    app = FastAPI(title="MarlinScan", lifespan=lifespan)
    app.state.scanner_service = scanner
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(ValueError, _value_error)
    app.add_exception_handler(FileNotFoundError, _not_found_error)
    for error_type in (ServiceStateError, PrinterStateError, FileExistsError):
        app.add_exception_handler(error_type, _conflict_error)
    for error_type in (PrinterError, CalibrationError, GPhoto2Error, OSError, RuntimeError):
        app.add_exception_handler(error_type, _hardware_error)

    @app.get("/api/status")
    def status() -> dict[str, object]:
        return scanner.status()

    @app.get("/api/printer/ports")
    def printer_ports() -> list[dict[str, str]]:
        return [asdict(port) for port in scanner.printer.list_ports()]

    @app.post("/api/printer/connect")
    def printer_connect(request: PrinterConnectRequest) -> dict[str, object]:
        return scanner.connect_printer(request.port, baud=request.baud, eol=request.eol)

    @app.post("/api/printer/disconnect")
    def printer_disconnect() -> dict[str, object]:
        return scanner.disconnect_printer()

    @app.post("/api/printer/home")
    def printer_home() -> dict[str, object]:
        return scanner.home_printer()

    @app.post("/api/printer/origin")
    def printer_origin() -> dict[str, object]:
        return scanner.set_printer_origin()

    @app.post("/api/printer/restore-position")
    def printer_restore_position() -> dict[str, object]:
        return scanner.restore_printer_position()

    @app.post("/api/printer/move")
    def printer_move(request: MoveRequest) -> dict[str, object]:
        return scanner.move_printer(
            x=request.x,
            y=request.y,
            z=request.z,
            speed_xy_mm_s=request.speed_xy_mm_s,
            speed_z_mm_s=request.speed_z_mm_s,
        )

    @app.post("/api/camera/take-control")
    def camera_take_control() -> dict[str, object]:
        return scanner.take_camera_control()

    @app.post("/api/camera/connect")
    def camera_connect() -> dict[str, object]:
        return scanner.connect_camera()

    @app.post("/api/camera/disconnect")
    def camera_disconnect() -> dict[str, object]:
        return scanner.disconnect_camera()

    @app.post("/api/camera/settings")
    def camera_settings(request: CameraSettingsRequest) -> dict[str, object]:
        return scanner.set_camera_iso(request.iso)

    @app.post("/api/camera/shutter")
    def camera_shutter(request: CameraShutterRequest) -> dict[str, object]:
        return scanner.set_camera_shutter(request.shutter)

    @app.post("/api/camera/test")
    def camera_test(request: CameraTestRequest) -> dict[str, object]:
        return scanner.test_camera(request.output_dir)

    @app.post("/api/calibration/start")
    def calibration_start(request: CalibrationStartRequest) -> dict[str, object]:
        return scanner.start_calibration(request.plan())

    @app.post("/api/calibration/exposure")
    def calibration_exposure(request: AutoExposureRequest) -> dict[str, object]:
        return scanner.start_auto_exposure(
            request.exposure_roi.normalized(),
            request.output_dir,
        )

    @app.post("/api/calibration/focus")
    def calibration_focus(request: AutoFocusRequest) -> dict[str, object]:
        return scanner.start_auto_focus(
            request.focus_roi.normalized(),
            request.output_dir,
            x_min=request.x_min,
            x_max=request.x_max,
            y_min=request.y_min,
            y_max=request.y_max,
            speed_z_mm_s=request.speed_z_mm_s,
        )

    @app.post("/api/calibration/focus-grid")
    def calibration_focus_grid(request: FocusGridStartRequest) -> dict[str, object]:
        return scanner.start_focus_grid(request.plan())

    @app.post("/api/calibration/white-balance")
    def calibration_white_balance(request: WhiteBalanceRequest) -> dict[str, object]:
        return scanner.start_white_balance(request.gray_roi.normalized(), request.output_dir)

    @app.post("/api/scan/start")
    def scan_start(request: ScanStartRequest) -> dict[str, object]:
        return scanner.start_scan(request.plan())

    @app.get("/api/editor/projects")
    def editor_projects() -> dict[str, object]:
        return {"projects": scanner.editor_projects()}

    @app.post("/api/editor/project")
    def editor_project(request: EditorProjectRequest) -> dict[str, object]:
        return scanner.editor_project(request.project_dir)

    @app.get("/api/editor/original-preview", response_class=FileResponse)
    def editor_original_preview(project_dir: str) -> FileResponse:
        preview = scanner.editor_original_preview(project_dir)
        return FileResponse(preview, media_type="image/jpeg")

    @app.post("/api/editor/preview", response_class=Response)
    def editor_preview(request: EditorPreviewRequest) -> Response:
        image = scanner.editor_preview(
            request.project_dir,
            request.recipe.value(),
            request.source,
            request.tile_index,
        )
        return Response(content=image, media_type="image/jpeg")

    @app.post("/api/editor/apply")
    def editor_apply(request: EditorApplyRequest) -> dict[str, object]:
        return scanner.start_editor_apply(request.project_dir, request.recipe.value())

    @app.post("/api/stop")
    def stop() -> dict[str, object]:
        return scanner.stop()

    @app.post("/api/emergency-stop")
    def emergency_stop() -> dict[str, object]:
        return scanner.emergency_stop()

    @app.get("/api/latest.jpg", response_class=FileResponse)
    def latest_image() -> FileResponse:
        latest = scanner.status()["latest_jpeg_path"]
        if latest is None or not Path(latest).is_file():
            raise HTTPException(status_code=404, detail="No camera image is available")
        return FileResponse(Path(latest), media_type="image/jpeg")

    @app.get("/api/scan/results/{artifact}", response_class=FileResponse)
    def scan_result(artifact: str) -> FileResponse:
        if artifact not in SCAN_RESULT_FILES:
            raise HTTPException(status_code=404, detail="Unknown scan artifact")
        scan_dir = scanner.status()["last_scan_dir"]
        result = None if scan_dir is None else Path(scan_dir) / SCAN_RESULT_FILES[artifact]
        if result is None or not result.is_file():
            raise HTTPException(status_code=404, detail="Scan artifact is not available")
        return FileResponse(result, filename=result.name)

    @app.get("/api/editor/results/{artifact}", response_class=FileResponse)
    def editor_result(artifact: str) -> FileResponse:
        if artifact not in EDITOR_RESULT_FILES:
            raise HTTPException(status_code=404, detail="Unknown editor artifact")
        revision_dir = scanner.status()["last_editor_revision_dir"]
        result = None if revision_dir is None else Path(revision_dir) / EDITOR_RESULT_FILES[artifact]
        if result is None or not result.is_file():
            raise HTTPException(status_code=404, detail="Editor artifact is not available")
        return FileResponse(result, filename=result.name)

    @app.websocket("/ws/jog")
    async def jog(websocket: WebSocket) -> None:
        await websocket.accept()
        speed_xy = 100.0
        speed_z = 10.0
        try:
            while True:
                request = JogRequest.model_validate(await websocket.receive_json())
                speed_xy = request.speed_xy_mm_s
                speed_z = request.speed_z_mm_s
                scanner.set_jog(request.dx, request.dy, request.dz, speed_xy, speed_z)
        except WebSocketDisconnect:
            pass
        except ValidationError as error:
            await websocket.close(code=1008, reason=_validation_detail(error))
        finally:
            if scanner.status()["state"] in {"idle", "jogging"}:
                scanner.set_jog(0.0, 0.0, 0.0, speed_xy, speed_z)

    if static_dir is not None:
        static_root = Path(static_dir).resolve()
        index = static_root / "index.html"
        if not index.is_file():
            raise FileNotFoundError(index)
        app.mount("/static", StaticFiles(directory=static_root), name="static")

        @app.get("/", include_in_schema=False, response_class=FileResponse)
        def root() -> FileResponse:
            return FileResponse(index)

    return app
