from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from v3se_printer.calibration import NormalizedROI
from v3se_printer.models import PortItem
from v3se_printer.service import ServiceStateError, scan_results
from v3se_printer.web.server import DEFAULT_STATIC_DIR, create_app


@dataclass(frozen=True)
class FakePrinterStatus:
    connected: bool = False
    initialized: bool = False
    faulted: bool = False
    port: str | None = None
    baud: int | None = None
    position: object | None = None
    firmware: str | None = None
    machine: str | None = None
    error: str | None = None
    remembered_position: object | None = None


class FakePrinter:
    def list_ports(self) -> list[PortItem]:
        return [PortItem("Test printer", "/dev/test")]


class FakeService:
    def __init__(self) -> None:
        self.printer = FakePrinter()
        self.state = "idle"
        self.latest: str | None = None
        self.last_scan_dir: str | None = None
        self.step_progress: dict[str, object] | None = None
        self.scan_progress: dict[str, object] | None = None
        self.editor_project_records: list[dict[str, object]] = []
        self.editor_project_detail: dict[str, object] = {}
        self.editor_preview_path: Path | None = None
        self.editor_revision_dir: str | None = None
        self.calls: list[tuple[object, ...]] = []
        self.shutdown_count = 0

    def status(self) -> dict[str, object]:
        return {
            "state": self.state,
            "message": "Ready",
            "step_progress": self.step_progress,
            "error": None,
            "printer": FakePrinterStatus().__dict__,
            "camera": {
                "control_taken": False,
                "connected": False,
                "model": None,
                "image_quality": None,
                "configured_profile": None,
                "latest_capture_profile": None,
                "iso": "160",
                "iso_choices": ["160"],
                "shutter": "1/6",
                "shutter_choices": ["1/6"],
                "latest_jpeg_path": None,
            },
            "calibration": None,
            "focus_grid": None,
            "quick_calibration": None,
            "measurements": [],
            "last_scan_dir": self.last_scan_dir,
            "scan_results": scan_results(self.last_scan_dir),
            "last_editor_revision_dir": self.editor_revision_dir,
            "editor_result": None if self.editor_revision_dir is None else {
                "revision": Path(self.editor_revision_dir).name,
                "directory": self.editor_revision_dir,
            },
            "editor_results": {},
            "scan_progress": self.scan_progress,
            "latest_jpeg_path": self.latest,
        }

    def connect_printer(self, port: str, *, baud: int, eol: str) -> dict[str, object]:
        if self.state != "idle":
            raise ServiceStateError(f"Cannot start while {self.state}")
        self.calls.append(("connect_printer", port, baud, eol))
        return self.status()

    def move_printer(
        self,
        *,
        x: float | None,
        y: float | None,
        z: float | None,
        speed_xy_mm_s: float,
        speed_z_mm_s: float,
    ) -> dict[str, object]:
        self.calls.append(("move_printer", x, y, z, speed_xy_mm_s, speed_z_mm_s))
        return self.status()

    def restore_printer_position(self) -> dict[str, object]:
        self.calls.append(("restore_printer_position",))
        return self.status()

    def set_camera_iso(self, iso: str) -> dict[str, object]:
        self.calls.append(("set_camera_iso", iso))
        return self.status()

    def set_camera_shutter(self, shutter: str) -> dict[str, object]:
        self.calls.append(("set_camera_shutter", shutter))
        return self.status()

    def test_camera(self, output_dir: str) -> dict[str, object]:
        self.calls.append(("test_camera", output_dir))
        return self.status()

    def start_auto_exposure(
        self,
        roi: NormalizedROI,
        output_dir: str,
    ) -> dict[str, object]:
        self.calls.append(("start_auto_exposure", roi, output_dir))
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
        speed_z_mm_s: float,
    ) -> dict[str, object]:
        self.calls.append(
            (
                "start_auto_focus",
                roi,
                output_dir,
                x_min,
                x_max,
                y_min,
                y_max,
                speed_z_mm_s,
            )
        )
        return self.status()

    def start_focus_grid(self, plan: object) -> dict[str, object]:
        self.calls.append(("start_focus_grid", plan))
        return self.status()

    def start_white_balance(self, roi: NormalizedROI, output_dir: str) -> dict[str, object]:
        self.calls.append(("start_white_balance", roi, output_dir))
        return self.status()

    def start_calibration(self, plan: object) -> dict[str, object]:
        self.calls.append(("start_calibration", plan))
        return self.status()

    def start_scan(self, plan: object) -> dict[str, object]:
        self.calls.append(("start_scan", plan))
        return self.status()

    def editor_projects(self) -> list[dict[str, object]]:
        self.calls.append(("editor_projects",))
        return self.editor_project_records

    def editor_project(self, project_dir: str) -> dict[str, object]:
        self.calls.append(("editor_project", project_dir))
        return self.editor_project_detail

    def editor_original_preview(self, project_dir: str) -> Path:
        self.calls.append(("editor_original_preview", project_dir))
        if self.editor_preview_path is None:
            raise FileNotFoundError(project_dir)
        return self.editor_preview_path

    def editor_tile_preview(self, project_dir: str, tile_index: int) -> bytes:
        self.calls.append(("editor_tile_preview", project_dir, tile_index))
        return b"editor-jpeg"

    def start_editor_apply(self, project_dir: str, recipe: object) -> dict[str, object]:
        self.calls.append(("start_editor_apply", project_dir, recipe))
        return self.status()

    def set_jog(
        self,
        dx: float,
        dy: float,
        dz: float,
        speed_xy_mm_s: float,
        speed_z_mm_s: float,
    ) -> dict[str, object]:
        self.calls.append(("set_jog", dx, dy, dz, speed_xy_mm_s, speed_z_mm_s))
        self.state = "jogging" if any((dx, dy, dz)) else "idle"
        return self.status()

    def shutdown(self) -> None:
        self.shutdown_count += 1


class WebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = FakeService()
        self.client = TestClient(create_app(self.service, static_dir=None))
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)

    def test_status_and_ports(self) -> None:
        self.assertEqual(self.client.get("/api/status").json()["state"], "idle")
        self.assertEqual(
            self.client.get("/api/printer/ports").json(),
            [{"label": "Test printer", "device": "/dev/test"}],
        )

    def test_status_exposes_structured_step_progress_and_scan_geometry(self) -> None:
        self.service.step_progress = {
            "phase": "capture",
            "label": "Capturing",
            "completed": 2,
            "total": 4,
            "unit": "tiles",
            "eta_seconds": 6.0,
        }
        self.service.scan_progress = {
            "points": [{"x": 10.0, "y": 20.0, "row": 0, "col": 0}],
            "completed": 1,
            "current_index": 0,
        }

        status = self.client.get("/api/status").json()

        self.assertEqual(status["step_progress"], self.service.step_progress)
        self.assertEqual(status["scan_progress"], self.service.scan_progress)
        self.assertNotIn("progress", status)

    def test_state_error_is_a_concise_conflict(self) -> None:
        self.service.state = "scanning"
        response = self.client.post("/api/printer/connect", json={"port": "/dev/test"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"detail": "Cannot start while scanning"})

    def test_request_validation_is_strict_and_concise(self) -> None:
        response = self.client.post("/api/printer/connect", json={"port": "/dev/test", "baud": "115200"})
        self.assertEqual(response.status_code, 422)
        self.assertIsInstance(response.json()["detail"], str)
        self.assertIn("baud", response.json()["detail"])

        response = self.client.post("/api/printer/connect", json={"port": "/dev/test", "unknown": 1})
        self.assertEqual(response.status_code, 422)
        self.assertIn("unknown", response.json()["detail"])

        response = self.client.post("/api/printer/move", json={})
        self.assertEqual(response.status_code, 422)
        self.assertIn("At least one target axis", response.json()["detail"])

    def test_latest_image_serves_only_an_available_file(self) -> None:
        with TemporaryDirectory() as directory:
            image = Path(directory) / "latest.jpg"
            image.write_bytes(b"jpeg-data")
            self.service.latest = str(image)
            response = self.client.get("/api/latest.jpg")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b"jpeg-data")
            self.assertEqual(response.headers["content-type"], "image/jpeg")

            other = Path(directory) / "other.jpg"
            other.write_bytes(b"other-data")
            response = self.client.get("/api/latest.jpg", params={"path": str(other)})
            self.assertEqual(response.content, b"jpeg-data")

            self.service.latest = directory
            self.assertEqual(self.client.get("/api/latest.jpg").status_code, 404)
            self.service.latest = None
            self.assertEqual(self.client.get("/api/latest.jpg", params={"path": str(other)}).status_code, 404)

    def test_scan_result_downloads_are_available_and_allowlisted(self) -> None:
        with TemporaryDirectory() as directory:
            scan_dir = Path(directory) / "scan"
            scan_dir.mkdir()
            other = Path(directory) / "other.jpg"
            other.write_bytes(b"other-data")
            self.service.last_scan_dir = str(scan_dir)
            expected = {
                "full_tiff": "mosaic_full.tif",
                "pyramidal_tiff": "mosaic_pyramidal.ome.tif",
                "scene_linear_exr": "mosaic_scene_linear.exr",
                "preview_jpeg": "mosaic_thumb_2000.jpg",
                "project_metadata": "scan_params.json",
                "recipe_metadata": "raw_development.json",
                "stitch_metadata": "stitch_meta.json",
            }
            for artifact, filename in expected.items():
                with self.subTest(artifact=artifact):
                    payload = artifact.encode()
                    (scan_dir / filename).write_bytes(payload)
                    result = self.client.get(
                        f"/api/scan/results/{artifact}",
                        params={"path": str(other)},
                    )
                    self.assertEqual(result.status_code, 200)
                    self.assertEqual(result.content, payload)
                    self.assertIn(filename, result.headers["content-disposition"])
                    status = self.client.get("/api/status").json()["scan_results"][artifact]
                    self.assertEqual(
                        status,
                        {
                            "name": filename,
                            "download_url": f"/api/scan/results/{artifact}",
                        },
                    )
            self.assertEqual(self.client.get("/api/scan/results/other").status_code, 404)
            self.service.last_scan_dir = None
            self.assertEqual(self.client.get("/api/scan/results/preview_jpeg").status_code, 404)

    def test_results_ui_uses_only_available_scan_artifacts(self) -> None:
        index = (DEFAULT_STATIC_DIR / "index.html").read_text(encoding="utf-8")
        script = (DEFAULT_STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="result-files"', index)
        for artifact in (
            "full_tiff",
            "pyramidal_tiff",
            "scene_linear_exr",
            "preview_jpeg",
            "project_metadata",
            "recipe_metadata",
            "stitch_metadata",
        ):
            self.assertIn(f"{artifact}:", script)
        self.assertIn('project_metadata: "Scan parameters"', script)
        self.assertIn('stitch_metadata: "Stitch metadata"', script)
        self.assertIn("if (result === null) return;", script)
        self.assertIn("download.href = result.download_url;", script)
        self.assertIn("download.download = result.name;", script)
        self.assertIn("if (status.error !== null) {", script)

    def test_connect_mutation_uses_service_defaults(self) -> None:
        response = self.client.post("/api/printer/connect", json={"port": " /dev/test "})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.service.calls, [("connect_printer", "/dev/test", 115200, "crlf")])

    def test_move_uses_separate_xy_and_z_speeds(self) -> None:
        response = self.client.post(
            "/api/printer/move",
            json={"x": 10.0, "y": 20.0, "z": 3.0, "speed_xy_mm_s": 40.0, "speed_z_mm_s": 4.0},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.service.calls, [("move_printer", 10.0, 20.0, 3.0, 40.0, 4.0)])

    def test_move_defaults_and_restore_position(self) -> None:
        self.assertEqual(self.client.post("/api/printer/move", json={"z": 12.0}).status_code, 200)
        self.assertEqual(self.client.post("/api/printer/restore-position").status_code, 200)
        self.assertEqual(
            self.service.calls,
            [
                ("move_printer", None, None, 12.0, 200.0, 10.0),
                ("restore_printer_position",),
            ],
        )

    def test_camera_and_individual_calibration_routes(self) -> None:
        roi = {"x": 0.1, "y": 0.2, "width": 0.4, "height": 0.5}
        normalized = NormalizedROI(**roi)
        responses = [
            self.client.post("/api/camera/settings", json={"iso": " 160 "}),
            self.client.post("/api/camera/shutter", json={"shutter": " 1/6 "}),
            self.client.post("/api/camera/test", json={"output_dir": " /tmp/captures "}),
            self.client.post(
                "/api/calibration/exposure",
                json={"exposure_roi": roi, "output_dir": " /tmp/output "},
            ),
            self.client.post(
                "/api/calibration/focus",
                json={
                    "x_min": 0.0,
                    "x_max": 50.0,
                    "y_min": 0.0,
                    "y_max": 34.0,
                    "focus_roi": roi,
                    "output_dir": "/tmp/output",
                    "speed_z_mm_s": 12.0,
                },
            ),
            self.client.post(
                "/api/calibration/white-balance",
                json={"gray_roi": roi, "output_dir": "/tmp/output"},
            ),
        ]
        self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertEqual(
            self.service.calls,
            [
                ("set_camera_iso", "160"),
                ("set_camera_shutter", "1/6"),
                ("test_camera", "/tmp/captures"),
                ("start_auto_exposure", normalized, "/tmp/output"),
                (
                    "start_auto_focus",
                    normalized,
                    "/tmp/output",
                    0.0,
                    50.0,
                    0.0,
                    34.0,
                    12.0,
                ),
                ("start_white_balance", normalized, "/tmp/output"),
            ],
        )

    def test_full_calibration_uses_current_camera_settings_and_speed_defaults(self) -> None:
        response = self.client.post(
            "/api/calibration/start",
            json={
                "x_min": 0.0,
                "x_max": 100.0,
                "y_min": 0.0,
                "y_max": 100.0,
                "exposure_roi": {},
                "focus_roi": {},
                "gray_roi": {},
                "output_dir": "/tmp/output",
            },
        )
        self.assertEqual(response.status_code, 200)
        plan = self.service.calls[0][1]
        self.assertEqual((plan.x_min, plan.x_max, plan.y_min, plan.y_max), (0.0, 100.0, 0.0, 100.0))
        self.assertEqual(plan.speed_xy_mm_s, 200.0)
        self.assertEqual(plan.speed_z_mm_s, 10.0)

    def test_focus_grid_route_uses_coverage_bounds_and_focus_settings(self) -> None:
        roi = {"x": 0.1, "y": 0.2, "width": 0.4, "height": 0.5}
        response = self.client.post(
            "/api/calibration/focus-grid",
            json={
                "x_min": 0.0,
                "x_max": 50.0,
                "y_min": 0.0,
                "y_max": 34.0,
                "focus_roi": roi,
                "output_dir": "/tmp/output",
                "speed_xy_mm_s": 120.0,
                "speed_z_mm_s": 8.0,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.service.calls[0][0], "start_focus_grid")
        plan = self.service.calls[0][1]
        self.assertEqual((plan.x_min, plan.x_max, plan.y_min, plan.y_max), (0.0, 50.0, 0.0, 34.0))
        self.assertEqual(plan.focus_roi, NormalizedROI(**roi))
        self.assertEqual(plan.output_dir, "/tmp/output")
        self.assertEqual((plan.speed_xy_mm_s, plan.speed_z_mm_s), (120.0, 8.0))

    def test_scan_route_accepts_explicit_quick_acquisition(self) -> None:
        response = self.client.post(
            "/api/scan/start",
            json={
                "x_min": 15.0,
                "x_max": 205.0,
                "y_min": 25.0,
                "y_max": 205.0,
                "frame_width_mm": 25.0,
                "frame_height_mm": 17.0,
                "overlap_percent": 25.0,
                "output_dir": "/tmp/output",
                "quick_acquisition": True,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.service.calls[0][0], "start_scan")
        plan = self.service.calls[0][1]
        self.assertTrue(plan.quick_acquisition)
        self.assertEqual(plan.settle_ms, 1000)

    def test_editor_routes_use_strict_global_recipe_and_allowlisted_files(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            preview = root / "source.jpg"
            preview.write_bytes(b"source-jpeg")
            self.service.editor_preview_path = preview
            self.service.editor_project_records = [
                {
                    "directory": directory,
                    "name": "scan-1",
                    "tile_count": 2,
                    "size": [12, 6],
                    "revision_count": 0,
                }
            ]
            self.service.editor_project_detail = {
                **self.service.editor_project_records[0],
                "revisions": [],
                "canvas_size": [12, 6],
                "tile_size": [8, 6],
                "tiles": [{"index": 0, "row": 0, "col": 0, "label": "R1 C1", "bounds": [0, 0, 2 / 3, 1]}],
                "preview_url": "/api/editor/original-preview?project_dir=scan-1",
            }

            projects = self.client.get("/api/editor/projects")
            project = self.client.post("/api/editor/project", json={"project_dir": directory})
            original = self.client.get("/api/editor/original-preview", params={"project_dir": directory})
            tile_preview = self.client.get(
                "/api/editor/tile-preview",
                params={"project_dir": directory, "tile_index": 0},
            )
            apply = self.client.post(
                "/api/editor/apply",
                json={"project_dir": directory, "recipe": {"material": "color_negative", "film_density": 1.5}},
            )

            self.assertEqual(projects.json(), {"projects": self.service.editor_project_records})
            self.assertEqual(project.json(), self.service.editor_project_detail)
            self.assertEqual(original.content, b"source-jpeg")
            self.assertEqual(tile_preview.content, b"editor-jpeg")
            self.assertEqual(tile_preview.headers["content-type"], "image/jpeg")
            self.assertEqual(apply.status_code, 200)
            preview_call = next(call for call in self.service.calls if call[0] == "editor_tile_preview")
            self.assertEqual(preview_call[1:], (directory, 0))
            apply_call = next(call for call in self.service.calls if call[0] == "start_editor_apply")
            self.assertEqual(apply_call[2].material, "color_negative")
            self.assertEqual(apply_call[2].film_density, 1.5)

            self.assertEqual(
                self.client.get("/api/editor/tile-preview", params={"project_dir": directory}).status_code,
                422,
            )
            self.assertEqual(
                self.client.post(
                    "/api/editor/apply",
                    json={"project_dir": directory, "recipe": {"exposure_ev": 9.0}},
                ).status_code,
                422,
            )

    def test_editor_result_download_is_bound_to_last_revision(self) -> None:
        with TemporaryDirectory() as directory:
            revision = Path(directory) / "revision-001"
            revision.mkdir()
            result = revision / "edit_recipe.json"
            result.write_bytes(b"recipe")
            self.service.editor_revision_dir = str(revision)

            response = self.client.get("/api/editor/results/edit_recipe", params={"path": "/tmp/other"})

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b"recipe")
            self.assertEqual(self.client.get("/api/editor/results/unknown").status_code, 404)

    def test_acknowledgement_route_is_removed(self) -> None:
        self.assertEqual(self.client.post("/api/acknowledge").status_code, 404)

    def test_removed_calibration_fields_are_rejected(self) -> None:
        full = {
            "x_min": 0.0,
            "x_max": 100.0,
            "y_min": 0.0,
            "y_max": 100.0,
            "exposure_roi": {},
            "focus_roi": {},
            "gray_roi": {},
            "output_dir": "/tmp/output",
        }
        for field, value in {
            "exposure_x": 50.0,
            "exposure_y": 50.0,
            "focus_center_z": 200.0,
            "focus_half_range_mm": 20.0,
            "starting_shutter": "1/15",
        }.items():
            with self.subTest(field=field):
                response = self.client.post("/api/calibration/start", json={**full, field: value})
                self.assertEqual(response.status_code, 422)
                self.assertIn(field, response.json()["detail"])

        response = self.client.post(
            "/api/calibration/exposure",
            json={"exposure_roi": {}, "output_dir": "/tmp/output", "starting_shutter": "1/15"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("starting_shutter", response.json()["detail"])

        response = self.client.post(
            "/api/calibration/focus",
            json={
                "x_min": 0.0,
                "x_max": 50.0,
                "y_min": 0.0,
                "y_max": 34.0,
                "focus_roi": {},
                "output_dir": "/tmp/output",
                "center_z": 200.0,
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("center_z", response.json()["detail"])

    def test_jog_disconnect_sends_zero_vector(self) -> None:
        with self.client.websocket_connect("/ws/jog") as websocket:
            websocket.send_json(
                {"dx": 1.0, "dy": 0.0, "dz": 0.0, "speed_xy_mm_s": 8.0, "speed_z_mm_s": 2.0}
            )
        self.assertEqual(
            self.service.calls,
            [
                ("set_jog", 1.0, 0.0, 0.0, 8.0, 2.0),
                ("set_jog", 0.0, 0.0, 0.0, 8.0, 2.0),
            ],
        )

    def test_jog_uses_faster_defaults(self) -> None:
        with self.client.websocket_connect("/ws/jog") as websocket:
            websocket.send_json({"dx": 0.0, "dy": 0.0, "dz": 1.0})
        self.assertEqual(
            self.service.calls,
            [
                ("set_jog", 0.0, 0.0, 1.0, 100.0, 10.0),
                ("set_jog", 0.0, 0.0, 0.0, 100.0, 10.0),
            ],
        )

    def test_lifespan_shuts_down_service(self) -> None:
        service = FakeService()
        with TestClient(create_app(service, static_dir=None)):
            self.assertEqual(service.shutdown_count, 0)
        self.assertEqual(service.shutdown_count, 1)

    def test_static_root_and_assets_use_selected_directory(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "index.html").write_text("<h1>MarlinScan test</h1>", encoding="utf-8")
            (root / "app.js").write_text("window.test = true;", encoding="utf-8")
            with TestClient(create_app(FakeService(), static_dir=root)) as client:
                self.assertEqual(client.get("/").text, "<h1>MarlinScan test</h1>")
                self.assertEqual(client.get("/static/app.js").text, "window.test = true;")

    def test_mobile_layout_stacks_panels_and_keeps_header_actions_clear(self) -> None:
        styles = (DEFAULT_STATIC_DIR / "styles.css").read_text(encoding="utf-8")
        mobile = styles[styles.index("@media (max-width: 760px)") :]
        narrow = styles[styles.index("@media (max-width: 500px)") :]
        self.assertIn(".workspace { width: 100%; max-width: 100%; display: flex; flex-direction: column;", mobile)
        self.assertIn(".hardware-panel { order: 1; width: 100%; }", mobile)
        self.assertIn(".stage-panel { order: 2; width: 100%; }", mobile)
        self.assertIn(".workflow-panel { order: 3; width: 100%; }", mobile)
        self.assertIn(".brand div { display: none; }", narrow)

    def test_default_camera_footprint_matches_measured_frame(self) -> None:
        index = (DEFAULT_STATIC_DIR / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="scan-frame-width" type="number" min="0.01" max="500" step="0.01" value="25"', index)
        self.assertIn('id="scan-frame-height" type="number" min="0.01" max="500" step="0.01" value="17"', index)

    def test_default_output_folders_are_under_working_directory(self) -> None:
        index = (DEFAULT_STATIC_DIR / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="test-output" type="text" value="./output/test-captures"', index)
        self.assertIn('id="cal-output" type="text" value="./output/calibration"', index)
        self.assertIn('id="scan-output" type="text" value="./output/scans"', index)
        self.assertNotIn("~/MarlinScan", index)

    def test_unconfigured_camera_does_not_invent_a_shutter_or_enable_capture(self) -> None:
        index = (DEFAULT_STATIC_DIR / "index.html").read_text(encoding="utf-8")
        script = (DEFAULT_STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="test-shutter" required disabled><option value="">Select shutter</option>', index)
        self.assertIn('syncChoiceSelect("test-shutter", shutterChoices, camera.shutter, "Select shutter", true, formatShutter);', script)
        self.assertNotIn('camera.shutter ?? "1/6"', script)
        self.assertIn('(preferredValue === "" && blankLabel !== null)', script)
        self.assertIn('const cameraReady = cameraConnected && camera.shutter !== null && camera.configured_profile !== null;', script)
        self.assertIn('else if (camera.connected) setDeviceState(byId("camera-state"), "Needs setup", "warning");', script)

    def test_estimated_dpi_uses_large_capture_and_live_footprint(self) -> None:
        index = (DEFAULT_STATIC_DIR / "index.html").read_text(encoding="utf-8")
        script = (DEFAULT_STATIC_DIR / "app.js").read_text(encoding="utf-8")
        footprint = index.index("<legend>Camera footprint</legend>")
        density = index.index('id="scan-estimated-dpi"')
        motion = index.index("<legend>Motion</legend>")
        self.assertLess(footprint, density)
        self.assertLess(density, motion)
        self.assertIn('const SCAN_DPI_PROFILES = new Set(["analysis", "raw"]);', script)
        self.assertIn("loadLatestImage(status.latest_jpeg_path, camera.latest_capture_profile);", script)
        self.assertIn("if (SCAN_DPI_PROFILES.has(profile)) {", script)
        self.assertIn("scanCaptureSize = { width: image.naturalWidth, height: image.naturalHeight };", script)
        self.assertIn("scanCaptureSize.width / frameWidth * 25.4", script)
        self.assertIn("scanCaptureSize.height / frameHeight * 25.4", script)
        self.assertIn("const nominalDpi = (dpiX + dpiY) / 2;", script)
        self.assertIn("`Nominal ${Math.round(nominalDpi).toLocaleString()} DPI", script)
        self.assertIn("renderEstimatedDpi();", script)

    def test_exposure_meter_uses_winsorized_mean_and_keeps_clipping_diagnostic(self) -> None:
        index = (DEFAULT_STATIC_DIR / "index.html").read_text(encoding="utf-8")
        script = (DEFAULT_STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn('Meter <output id="exposure-metered">', index)
        self.assertIn('P99 <output id="exposure-p99">', index)
        self.assertIn('JPEG clip <output id="exposure-clipped">', index)
        self.assertIn("<dt>Exposure reading</dt>", index)
        self.assertIn("histogramPercentile(histogram, count, .02)", script)
        self.assertIn("histogramPercentile(histogram, count, .98)", script)
        self.assertIn("exposureSampleCanvas.width = sourceWidth;", script)
        self.assertIn("const count = sourceWidth * sourceHeight;", script)
        self.assertIn("const waveformWidth = Math.min(320, sourceWidth);", script)
        self.assertIn("meteredLuminance: winsorizedTotal / count", script)
        self.assertIn("2.2 * Math.log2(metered / 128)", script)
        self.assertIn('const state = ev < -1 / 3 ? "Low" : ev > 1 / 3 ? "High" : "On target";', script)
        self.assertIn("RAW saturation", script)
        self.assertNotIn('const state = exposureAnalysis.clippedFraction', script)

    def test_default_calibration_and_scan_coverage_matches_stage_area(self) -> None:
        index = (DEFAULT_STATIC_DIR / "index.html").read_text(encoding="utf-8")
        for field, value in {
            "cal-x-min": "15",
            "cal-x-max": "205",
            "cal-y-min": "25",
            "cal-y-max": "205",
            "scan-x-min": "15",
            "scan-x-max": "205",
            "scan-y-min": "25",
            "scan-y-max": "205",
        }.items():
            bound = field.split("-", 1)[1]
            self.assertIn(f'id="{field}" data-coverage-bound=', index)
            self.assertIn(f'id="{field}" data-coverage-bound="{bound}" type="number" min="0" max="220" step="0.01" value="{value}"', index)

    def test_calibration_and_scan_bounds_are_linked_with_current_position_controls(self) -> None:
        index = (DEFAULT_STATIC_DIR / "index.html").read_text(encoding="utf-8")
        script = (DEFAULT_STATIC_DIR / "app.js").read_text(encoding="utf-8")
        for bound in ("x-min", "x-max", "y-min", "y-max"):
            self.assertEqual(index.count(f'data-coverage-bound="{bound}"'), 2)
            self.assertEqual(index.count(f'data-current-bound="{bound}"'), 2)
        self.assertIn('function setCoverageBound(name, value) {', script)
        self.assertIn('document.querySelectorAll(`[data-coverage-bound="${name}"]`)', script)
        self.assertIn('setCoverageBound(input.dataset.coverageBound, input.value)', script)
        self.assertIn('setCoverageBound(button.dataset.currentBound, position[button.dataset.axis].toFixed(2))', script)

    def test_focus_actions_send_bounds_and_map_uses_only_measured_points(self) -> None:
        index = (DEFAULT_STATIC_DIR / "index.html").read_text(encoding="utf-8")
        script = (DEFAULT_STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="move-z" type="number" min="0" max="250" step="0.001" value="203"', index)
        self.assertIn('action(byId("auto-focus"), "/api/calibration/focus", {\n    ...coverageBoundsPayload(),', script)
        self.assertIn('function focusGridPayload() {\n  return {\n    ...coverageBoundsPayload(),', script)
        self.assertIn('drawFocusMeasurements(focusSurface.measurements, focusSurface.mesh', script)
        self.assertIn('const observations = focusSurface.measurements.map(', script)
        self.assertNotIn("function drawMeshNodes", script)

    def test_progress_ui_uses_structured_steps_and_geometry_only_scan_progress(self) -> None:
        index = (DEFAULT_STATIC_DIR / "index.html").read_text(encoding="utf-8")
        script = (DEFAULT_STATIC_DIR / "app.js").read_text(encoding="utf-8")
        styles = (DEFAULT_STATIC_DIR / "styles.css").read_text(encoding="utf-8")
        self.assertIn('id="step-progress-line"', index)
        self.assertIn('id="operation-progress" max="1" value="0"', index)
        self.assertIn("renderOperationProgress(status.step_progress);", script)
        self.assertNotIn("status.progress", script)
        self.assertIn('if (progress === null) return "No active step · 0 units · ETA unavailable";', script)
        self.assertIn('`${progress.completed} ${progress.unit}`', script)
        self.assertIn('`${progress.completed}/${progress.total} ${progress.unit}`', script)
        self.assertIn('bar.removeAttribute("value");', script)
        self.assertIn("progress.completed / progress.total", script)
        self.assertIn("const geometry = snapshot.scan_progress;", script)
        self.assertIn("points: geometry === null ? local.points : geometry.points", script)
        self.assertIn("completed: geometry === null ? 0 : geometry.completed", script)
        self.assertIn("currentIndex: geometry === null ? null : geometry.current_index", script)
        self.assertNotIn("geometry.phase", script)
        self.assertNotIn("geometry.eta_seconds", script)
        self.assertIn('`${total} tiles · ${completed}/${total} captured · ${formatStepProgress(plan.stepProgress)}`', script)
        self.assertIn('`${total} tiles · ${plan.phase}`', script)
        self.assertIn("const mapLabel = renderMeshLegend(focusSurface);", script)
        self.assertIn("renderScanPlanReadout(scanPlan, mapLabel);", script)
        self.assertNotIn('bedCanvas.getAttribute("aria-label")', script)
        self.assertNotIn(".run-status { display: none; }", styles)

    def test_editor_workspace_uses_global_recipe_contract(self) -> None:
        index = (DEFAULT_STATIC_DIR / "index.html").read_text(encoding="utf-8")
        script = (DEFAULT_STATIC_DIR / "app.js").read_text(encoding="utf-8")
        renderer = (DEFAULT_STATIC_DIR / "editor-preview.js").read_text(encoding="utf-8")
        styles = (DEFAULT_STATIC_DIR / "styles.css").read_text(encoding="utf-8")
        editor = index[index.index('id="editor-workspace"') :]
        self.assertIn('data-workspace="capture"', index)
        self.assertIn('data-workspace="editor"', index)
        self.assertIn('data-editor-source="mosaic"', editor)
        self.assertIn('data-editor-source="tile"', editor)
        self.assertIn('data-editor-material="positive"', editor)
        self.assertIn('data-editor-material="color_negative"', editor)
        self.assertIn('data-editor-material="bw_negative"', editor)
        self.assertIn('id="editor-film-controls" role="tabpanel" aria-labelledby="editor-film-tab" hidden', editor)
        self.assertIn('id="editor-tile-map"', editor)
        self.assertIn('id="editor-preview-canvas"', editor)
        self.assertIn('id="editor-pick-white-balance"', editor)
        self.assertIn('id="editor-tone-curve"', editor)
        self.assertIn('data-hsl-band="7"', editor)
        self.assertIn('id="editor-apply-progress-bar"', editor)
        for field, minimum, maximum in (
            ("editor-black-point", "-1", "0.95"),
            ("editor-white-point", "0.01", "8"),
            ("editor-film-base-red", "0.01", "4"),
            ("editor-film-base-green", "0.01", "4"),
            ("editor-film-base-blue", "0.01", "4"),
        ):
            self.assertIn(f'id="{field}" data-editor-field=', editor)
            control = editor[editor.index(f'id="{field}"') :]
            self.assertLess(control.index(f'min="{minimum}"'), control.index(f'max="{maximum}"'))
        for field in (
            "exposure_ev",
            "temperature",
            "tint",
            "contrast",
            "highlights",
            "shadows",
            "black_point",
            "white_point",
            "saturation",
            "red_balance",
            "green_balance",
            "blue_balance",
            "film_base_red",
            "film_base_green",
            "film_base_blue",
            "film_density",
            "film_dmin",
            "film_dmax",
            "film_red_ratio",
            "film_blue_ratio",
            "slide_fade",
            "slide_black_red",
            "slide_black_green",
            "slide_black_blue",
            "slide_white_red",
            "slide_white_green",
            "slide_white_blue",
        ):
            self.assertIn(f'data-editor-field="{field}"', editor)
            self.assertIn(f"{field}: numberValue(", script)
        for forbidden in ("brush", "mask", "selection", "dodge", "burn"):
            self.assertNotIn(forbidden, editor.lower())
        self.assertIn('requestJson("/api/editor/projects")', script)
        self.assertIn('requestJson("/api/editor/project", "POST", { project_dir: directory })', script)
        self.assertIn("let editorRenderer = null;", script)
        self.assertIn("function requireEditorRenderer()", script)
        self.assertIn('renderer.loadSource("full", editorProject.preview_url)', script)
        self.assertIn('renderer.loadSource("local", `/api/editor/tile-preview?${params}`)', script)
        self.assertIn('editorRenderer.setRecipe(recipe);', script)
        self.assertIn("if (!event.persisted && editorRenderer !== null)", script)
        self.assertIn('result.directory.startsWith(`${editorProject.directory}/revisions/`)', script)
        self.assertNotIn('/api/editor/preview', script)
        self.assertIn("export class EditorPreviewRenderer", renderer)
        self.assertIn("requestAnimationFrame", renderer)
        self.assertIn('action(byId("editor-apply"), "/api/editor/apply"', script)
        self.assertIn("Object.entries(EDITOR_RESULT_LABELS)", script)
        self.assertIn("download.href = file.download_url;", script)
        self.assertIn("download.download = file.name;", script)
        self.assertIn('"editing"', script)
        self.assertIn(".editor-workspace {", styles)
        self.assertIn(".editor-actions {", styles)
        self.assertIn(".editor-actions { position: static; padding-inline: 13px; }", styles)

    def test_normal_operation_control_is_labeled_as_cooperative_cancel(self) -> None:
        index = (DEFAULT_STATIC_DIR / "index.html").read_text(encoding="utf-8")
        script = (DEFAULT_STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="stop-operation" type="button" disabled>Cancel</button>', index)
        self.assertIn('byId("stop-operation").addEventListener("click", () => action(byId("stop-operation"), "/api/stop"));', script)


if __name__ == "__main__":
    unittest.main()
