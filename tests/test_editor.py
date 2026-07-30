from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2  # type: ignore
import numpy as np  # type: ignore
import pyvips  # type: ignore

from v3se_printer.calibration import NormalizedROI
from v3se_printer.editor import (
    EditRecipe,
    apply_edit_recipe,
    apply_editor_revision,
    edit_recipe_from_dict,
    load_editor_project,
    render_editor_preview,
)
from v3se_printer.raw import RawDevelopmentRecipe, WhiteBalance


class EditorTests(unittest.TestCase):
    def test_recipe_is_strict_and_neutral_recipe_is_identity(self) -> None:
        recipe = EditRecipe()
        values = np.asarray(
            [[[0.1, 0.2, 0.3], [0.5, 1.0, 2.0]]],
            dtype=np.float32,
        )

        np.testing.assert_allclose(apply_edit_recipe(values, recipe), values, rtol=1e-6, atol=1e-6)
        self.assertEqual(edit_recipe_from_dict(asdict(recipe)), recipe)
        with self.assertRaisesRegex(ValueError, "fields"):
            edit_recipe_from_dict({**asdict(recipe), "unknown": 1})
        with self.assertRaisesRegex(ValueError, "black point"):
            EditRecipe(black_point=0.5, white_point=0.5)
        with self.assertRaisesRegex(ValueError, "exposure_ev"):
            EditRecipe(exposure_ev=9.0)
        with self.assertRaisesRegex(ValueError, "black_point"):
            EditRecipe(black_point=-1.01)
        with self.assertRaisesRegex(ValueError, "film_base_red"):
            EditRecipe(film_base_red=4.01)

    def test_recipe_bounds_match_the_editor_controls(self) -> None:
        EditRecipe(black_point=-1.0)
        EditRecipe(black_point=0.95, white_point=8.0)
        EditRecipe(white_point=0.01)
        EditRecipe(film_base_red=0.01, film_base_green=4.0, film_base_blue=4.0)

        for field, value in {
            "black_point": -1.01,
            "white_point": 8.01,
            "film_base_red": 4.01,
            "film_base_green": 4.01,
            "film_base_blue": 4.01,
        }.items():
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, field):
                    EditRecipe(**{field: value})

    def test_negative_recipe_uses_film_base_and_optical_density(self) -> None:
        source = np.asarray([[[0.5, 0.25, 0.125]]], dtype=np.float32)
        recipe = EditRecipe(
            material="negative",
            film_base_red=1.0,
            film_base_green=1.0,
            film_base_blue=1.0,
            white_point=4.0,
        )

        edited = apply_edit_recipe(source, recipe)

        np.testing.assert_allclose(edited[0, 0], [0.25, 0.5, 0.75], rtol=1e-6, atol=1e-6)

    def test_exposure_brightens_positive_and_negative_outputs(self) -> None:
        source = np.full((1, 1, 3), 0.8, dtype=np.float32)
        for material in ("positive", "negative"):
            with self.subTest(material=material):
                neutral = apply_edit_recipe(source, EditRecipe(material=material))
                brighter = apply_edit_recipe(source, EditRecipe(material=material, exposure_ev=1.0))
                self.assertTrue(np.all(brighter > neutral))

    def test_project_validation_blocks_paths_outside_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            scan = self._make_project(root)
            records = json.loads((scan / "tiles.json").read_text(encoding="utf-8"))
            records[0]["raw_file"] = "../outside.nef"
            (scan / "tiles.json").write_text(json.dumps(records), encoding="utf-8")
            (root / "outside.nef").write_bytes(b"raw")

            with self.assertRaisesRegex(ValueError, "inside the scan project"):
                load_editor_project(scan, (root,))

    def test_tile_and_mosaic_previews_are_real_jpegs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = load_editor_project(self._make_project(root), (root,))

            tile = render_editor_preview(project, EditRecipe(exposure_ev=-1.0), "tile", 0)
            mosaic = render_editor_preview(project, EditRecipe(saturation=0.0), "mosaic", None)

            tile_image = cv2.imdecode(np.frombuffer(tile, dtype=np.uint8), cv2.IMREAD_COLOR)
            mosaic_image = cv2.imdecode(np.frombuffer(mosaic, dtype=np.uint8), cv2.IMREAD_COLOR)
            self.assertEqual(tile_image.shape[:2], (6, 8))
            self.assertEqual(mosaic_image.shape[:2], (6, 12))

    def test_apply_creates_immutable_numbered_revisions_with_saved_transforms(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            scan = self._make_project(root)
            project = load_editor_project(scan, (root,))
            originals = {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in scan.glob("*.nef")
            }
            source = Path(__file__).resolve().parents[1] / "tools" / "write_openexr.cpp"

            def develop(raw_path: Path, _recipe: object, *, output_size: tuple[int, int]) -> np.ndarray:
                scene_path = scan / f"{Path(raw_path).stem}_scene_linear.tif"
                bgr = cv2.imread(str(scene_path), cv2.IMREAD_ANYDEPTH | cv2.IMREAD_COLOR)
                self.assertEqual(output_size, (8, 6))
                return np.ascontiguousarray(bgr[:, :, ::-1])

            with patch("v3se_printer.editor.develop_nef_scene", side_effect=develop) as develop_mock:
                first = apply_editor_revision(project, EditRecipe(exposure_ev=0.5), openexr_source=source)
                second = apply_editor_revision(project, EditRecipe(contrast=0.25), openexr_source=source)

            self.assertEqual(first.name, "revision-001")
            self.assertEqual(second.name, "revision-002")
            self.assertTrue((first / "mosaic_pyramidal.ome.tif").is_file())
            self.assertTrue((first / "mosaic_working_linear.exr").is_file())
            metadata = json.loads((first / "revision_meta.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["alignment"], "saved-transforms")
            self.assertEqual(metadata["working_image_role"], "edited-linear-rec2020")
            self.assertEqual(metadata["tile_transforms"], project.stage["tile_transforms"])
            self.assertEqual(
                originals,
                {
                    path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in scan.glob("*.nef")
                },
            )
            self.assertEqual(json.loads((first / "edit_recipe.json").read_text(encoding="utf-8"))["exposure_ev"], 0.5)
            self.assertEqual(develop_mock.call_count, 4)
            self.assertTrue(all(Path(call.args[0]).suffix.lower() == ".nef" for call in develop_mock.call_args_list))

    def test_cancel_removes_only_the_partial_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            scan = self._make_project(root)
            project = load_editor_project(scan, (root,))

            with self.assertRaisesRegex(InterruptedError, "cancelled"):
                apply_editor_revision(
                    project,
                    EditRecipe(),
                    openexr_source=Path(__file__).resolve().parents[1] / "tools" / "write_openexr.cpp",
                    cancel_cb=lambda: (_ for _ in ()).throw(InterruptedError("cancelled")),
                )

            revisions = scan / "revisions"
            self.assertTrue(revisions.is_dir())
            self.assertEqual(list(revisions.iterdir()), [])

    @staticmethod
    def _make_project(root: Path) -> Path:
        scan = root / "scan_20260730_000000_000000"
        scan.mkdir()
        records = []
        transforms = []
        for col, red in enumerate((0.25, 0.75)):
            stem = f"tile_r000_c{col:03d}_x{col:.2f}_y0.00"
            raw = scan / f"{stem}.nef"
            jpeg = scan / f"{stem}.jpg"
            scene_path = scan / f"{stem}_scene_linear.tif"
            display = scan / f"{stem}.tif"
            raw.write_bytes(f"raw-{col}".encode())
            self_image = np.zeros((6, 8, 3), dtype=np.uint8)
            self_image[:, :, 2] = int(red * 255)
            if not cv2.imwrite(str(jpeg), self_image):
                raise RuntimeError("Failed to create editor JPEG fixture")
            rgb = np.zeros((6, 8, 3), dtype=np.float32)
            rgb[:, :, 0] = red
            rgb[:, :, 1] = 0.25
            rgb[:, :, 2] = 0.1
            image = pyvips.Image.new_from_memory(rgb.data, 8, 6, 3, "float")
            image.tiffsave(str(scene_path), compression="deflate", predictor="float")
            image.cast("ushort").tiffsave(str(display))
            records.append(
                {
                    "row": 0,
                    "col": col,
                    "raw_file": raw.name,
                    "file": jpeg.name,
                    "scene_linear_file": scene_path.name,
                    "display_file": display.name,
                }
            )
            transforms.append(
                {
                    "row": 0,
                    "col": col,
                    "solved_position_px": [float(col * 4), 0.0],
                    "applied_position_px": [col * 4, 0],
                    "matrix": [[1.0, 0.0, float(col * 4)], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
                }
            )
        (scan / "tiles.json").write_text(json.dumps(records), encoding="utf-8")
        raw_recipe = RawDevelopmentRecipe(
            2,
            WhiteBalance(1.0, 1.0, 1.0, 1.0),
            1.0,
            0.5,
            0.5,
            NormalizedROI(),
            "/calibration/reference.jpg",
            "/calibration/reference.nef",
            ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        )
        (scan / "raw_development.json").write_text(json.dumps(asdict(raw_recipe)), encoding="utf-8")
        (scan / "scan_params.json").write_text(json.dumps({"image_roles": "raw"}), encoding="utf-8")
        (scan / "stitch_meta.json").write_text(
            json.dumps(
                {
                    "dpi": {"px_per_mm": 100.0, "dpi_x": 2540.0},
                    "stages": [
                        {
                            "name": "layout",
                            "tiles_in": 2,
                            "blend": "feather",
                            "layout_feather_px": 2,
                            "source_size_px": [8, 6],
                            "tile_size_px": [8, 6],
                            "canvas_size_px": [12, 6],
                            "tile_transforms": transforms,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return scan


if __name__ == "__main__":
    unittest.main()
