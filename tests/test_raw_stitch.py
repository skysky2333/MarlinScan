from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

import cv2  # type: ignore
import numpy as np  # type: ignore
import pyvips  # type: ignore

from v3se_printer.color import require_srgb_icc_profile
from v3se_printer.progress import StepProgressTracker
from v3se_printer.raw import LIBRAW_VERSION, RAWPY_VERSION
from v3se_printer.scan.stitch_outputs import stitch_scan_outputs
from v3se_printer.scan.stitching.composite import composite_tiles
from v3se_printer.scan.stitching.layout import stitch_layout_mosaic
from v3se_printer.scan.stitching.openexr import build_openexr_helper
from v3se_printer.scan.stitching.output import (
    estimate_output_dpi,
    write_mosaic_tiff,
    write_preview_jpeg,
    write_scene_linear_mosaic_tiff,
)
from v3se_printer.scan.stitching.types import Entry


class RawStitchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.build_dir = tempfile.TemporaryDirectory()
        cls.openexr_helper = Path(cls.build_dir.name) / "write_openexr"
        cls.vips = shutil.which("vips")
        if cls.vips is None:
            raise RuntimeError("vips is required to verify OpenEXR pixels")
        build_openexr_helper(
            source_path=Path(__file__).resolve().parents[1] / "tools" / "write_openexr.cpp",
            output_path=cls.openexr_helper,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.build_dir.cleanup()

    def test_single_tile_dpi_uses_source_pixels_and_camera_footprint(self) -> None:
        pixels_per_mm, metadata = estimate_output_dpi(
            strategy_settings={"tile_size_px": [5232, 3488]},
            step_x_mm=0.0,
            step_y_mm=0.0,
            override_dpi=None,
            round_px_per_mm=None,
            frame_width_mm=25.0,
            frame_height_mm=17.0,
        )

        expected = ((5232 / 25.0) + (3488 / 17.0)) / 2.0
        self.assertAlmostEqual(pixels_per_mm, expected)
        self.assertAlmostEqual(metadata["dpi_x"], expected * 25.4)

    @staticmethod
    def _write_recipe(scan_dir: Path) -> None:
        (scan_dir / "raw_development.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "white_balance": {"red": 1.2, "green": 1.0, "blue": 1.4, "green_2": 1.0},
                    "display_linear_gain": 1.5,
                    "reference_jpeg_linear_luminance": 0.3,
                    "reference_raw_linear_luminance": 0.2,
                    "reference_roi": {"x": 0.2, "y": 0.2, "width": 0.6, "height": 0.6},
                    "reference_jpeg": "/calibration/reference.jpg",
                    "reference_nef": "/calibration/reference.nef",
                    "camera_to_working_matrix": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
                    "rawpy_version": RAWPY_VERSION,
                    "libraw_version": LIBRAW_VERSION,
                    "demosaic_algorithm": "AHD",
                    "highlight_mode": "Ignore",
                    "working_space": "linear-rec2020-d65",
                    "display_space": "srgb",
                }
            ),
            encoding="utf-8",
        )
        (scan_dir / "scan_params.json").write_text(
            json.dumps(
                {
                    "image_roles": "raw",
                    "raw_development_recipe": "raw_development.json",
                    "step_x_mm": 1.0,
                    "step_y_mm": 1.0,
                    "serpentine": True,
                }
            ),
            encoding="utf-8",
        )

    @staticmethod
    def _write_float_rgb(path: Path, rgb: np.ndarray) -> None:
        height, width = rgb.shape[:2]
        pyvips.Image.new_from_memory(rgb.data, width, height, 3, "float").tiffsave(
            str(path),
            compression="deflate",
            predictor="float",
        )

    def test_existing_file_role_uses_same_uint8_image_for_composite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            scan_dir = Path(temp_dir)
            tile_path = scan_dir / "tile.tif"
            tile = np.full((18, 24, 3), [20, 80, 160], dtype=np.uint8)
            self.assertTrue(cv2.imwrite(str(tile_path), tile))
            stitch_scan_outputs(
                tiles=[{"row": 0, "col": 0, "file": tile_path.name}],
                out_dir=str(scan_dir),
                build_pyramidal_tiff=True,
                tiff_compression="lzw",
                image_roles="single",
                stitch_settings={"final_megapix": -1.0, "layout_blend": "overwrite", "use_memmap": False},
            )
            mosaic = cv2.imread(str(scan_dir / "mosaic_full.tif"), cv2.IMREAD_UNCHANGED)
            profile = pyvips.Image.new_from_file(str(scan_dir / "mosaic_full.tif")).get("icc-profile-data")
            self.assertEqual(mosaic.dtype, np.uint8)
            self.assertTrue(np.array_equal(mosaic, tile))
            self.assertEqual(profile, require_srgb_icc_profile().read_bytes())

    def test_raw_single_tile_writes_scene_linear_exr_and_srgb_outputs(self) -> None:
        progress: list[tuple[str, str, int, int | None, str]] = []
        tracker = StepProgressTracker()

        def publish(phase: str, label: str, completed: int, total: int | None, unit: str) -> None:
            progress.append((phase, label, completed, total, unit))
            tracker.update(phase, label, completed, total, unit)

        with tempfile.TemporaryDirectory() as temp_dir:
            scan_dir = Path(temp_dir)
            alignment_path = scan_dir / "tile.jpg"
            raw_path = scan_dir / "tile.nef"
            display_path = scan_dir / "tile.tif"
            scene_path = scan_dir / "tile_scene_linear.tif"
            self.assertTrue(cv2.imwrite(str(alignment_path), np.full((512, 512, 3), 17, dtype=np.uint8)))
            raw_path.write_bytes(b"raw")
            display = np.full((512, 512, 3), [1000, 2000, 3000], dtype=np.uint16)
            self.assertTrue(cv2.imwrite(str(display_path), display))
            scene_rgb = np.full((512, 512, 3), [0.4, 0.2, 1.3], dtype=np.float32)
            self._write_float_rgb(scene_path, scene_rgb)
            self._write_recipe(scan_dir)

            stitch_scan_outputs(
                tiles=[
                    {
                        "row": 0,
                        "col": 0,
                        "x_mm": 0.0,
                        "y_mm": 0.0,
                        "file": alignment_path.name,
                        "raw_file": raw_path.name,
                        "display_file": display_path.name,
                        "scene_linear_file": scene_path.name,
                    }
                ],
                out_dir=str(scan_dir),
                build_pyramidal_tiff=True,
                tiff_compression="lzw",
                image_roles="raw",
                openexr_helper=self.openexr_helper,
                progress_cb=publish,
                stitch_settings={"final_megapix": -1.0, "layout_blend": "overwrite", "use_memmap": False},
            )

            mosaic = cv2.imread(str(scan_dir / "mosaic_full.tif"), cv2.IMREAD_UNCHANGED)
            pyramidal = cv2.imread(str(scan_dir / "mosaic_pyramidal.ome.tif"), cv2.IMREAD_UNCHANGED)
            preview = cv2.imread(str(scan_dir / "mosaic_thumb_2000.jpg"), cv2.IMREAD_UNCHANGED)
            metadata = json.loads((scan_dir / "stitch_meta.json").read_text(encoding="utf-8"))
            decoded_path = scan_dir / "mosaic_scene_linear.f32"
            subprocess.run(
                [self.vips, "rawsave", str(scan_dir / "mosaic_scene_linear.exr"), str(decoded_path)],
                check=True,
                capture_output=True,
            )
            exr_pixels = np.fromfile(decoded_path, dtype=np.float32).reshape(512, 512, 4)
            pyramidal_image = pyvips.Image.new_from_file(str(scan_dir / "mosaic_pyramidal.ome.tif"))
            matrix = np.array(
                [
                    [1.660491, -0.587641, -0.072850],
                    [-0.124550, 1.132900, -0.008349],
                    [-0.018151, -0.100579, 1.118730],
                ],
                dtype=np.float32,
            )
            linear_srgb = np.clip(scene_rgb @ matrix.T, 0.0, 1.0)
            encoded = np.where(
                linear_srgb <= 0.0031308,
                linear_srgb * 12.92,
                1.055 * np.power(linear_srgb, 1.0 / 2.4) - 0.055,
            )
            expected_bgr = np.floor(encoded[:, :, ::-1] * 65535.0 + 0.5).astype(np.uint16)
            self.assertEqual(mosaic.dtype, np.uint16)
            np.testing.assert_allclose(mosaic, expected_bgr, rtol=0.0, atol=1.0)
            np.testing.assert_array_equal(pyramidal, mosaic)
            np.testing.assert_allclose(exr_pixels[:, :, :3], scene_rgb, rtol=5e-4, atol=5e-5)
            self.assertGreater(float(exr_pixels[:, :, 2].max()), 1.0)
            phase_order = list(dict.fromkeys(record[0] for record in progress))
            self.assertEqual(
                phase_order,
                [
                    "stitch-validate",
                    "stitch-composite",
                    "write-exr",
                    "write-flat-tiff",
                    "write-pyramidal-tiff",
                    "write-preview",
                    "write-metadata",
                    "stitch-cleanup",
                    "publish-outputs",
                ],
            )
            for phase in phase_order:
                records = [record for record in progress if record[0] == phase]
                self.assertEqual(records[0][2], 0)
                self.assertEqual([record[2] for record in records], sorted(record[2] for record in records))
                self.assertEqual(len({record[3] for record in records}), 1)
                self.assertEqual(records[-1][2], records[-1][3])
            self.assertFalse(np.array_equal(mosaic, display))
            self.assertEqual(preview.dtype, np.uint8)
            self.assertGreater(pyramidal_image.get("n-subifds"), 0)
            self.assertEqual(pyramidal_image.get("icc-profile-data"), require_srgb_icc_profile().read_bytes())
            ome = ElementTree.fromstring(pyramidal_image.get("image-description"))
            pixels = ome.find("{http://www.openmicroscopy.org/Schemas/OME/2016-06}Image/{http://www.openmicroscopy.org/Schemas/OME/2016-06}Pixels")
            self.assertIsNotNone(pixels)
            self.assertEqual((pixels.get("SizeX"), pixels.get("SizeY"), pixels.get("SizeC")), ("512", "512", "3"))
            self.assertEqual(metadata["composite_dtype"], "float32")
            self.assertEqual(metadata["outputs"]["pyramidal_tiff"], "mosaic_pyramidal.ome.tif")
            self.assertEqual(metadata["outputs"]["scene_linear_exr"], "mosaic_scene_linear.exr")
            self.assertEqual(metadata["raw_development_recipe"], "raw_development.json")
            self.assertFalse(metadata["settings"]["layout_black_transparent"])
            self.assertFalse(metadata["settings"]["layout_exposure_compensate"])
            self.assertEqual(
                metadata["stages"][0]["tile_transforms"],
                [
                    {
                        "row": 0,
                        "col": 0,
                        "solved_position_px": [0.0, 0.0],
                        "applied_position_px": [0, 0],
                        "matrix": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
                    }
                ],
            )

    def test_raw_multitile_memmap_persists_composited_transforms(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            scan_dir = Path(temp_dir)
            tiles: list[dict[str, object]] = []
            scenes = ([0.25, 0.5, 1.25], [0.5, 0.75, 1.5])
            for col, stem in enumerate(("left", "right")):
                alignment_path = scan_dir / f"{stem}.jpg"
                raw_path = scan_dir / f"{stem}.nef"
                display_path = scan_dir / f"{stem}.tif"
                scene_path = scan_dir / f"{stem}_scene_linear.tif"
                self.assertTrue(cv2.imwrite(str(alignment_path), np.full((512, 512, 3), 40 + col, dtype=np.uint8)))
                raw_path.write_bytes(b"raw")
                self.assertTrue(cv2.imwrite(str(display_path), np.full((512, 512, 3), 10000 + col, dtype=np.uint16)))
                self._write_float_rgb(scene_path, np.full((512, 512, 3), scenes[col], dtype=np.float32))
                tiles.append(
                    {
                        "row": 0,
                        "col": col,
                        "x_mm": float(col),
                        "y_mm": 0.0,
                        "file": alignment_path.name,
                        "raw_file": raw_path.name,
                        "display_file": display_path.name,
                        "scene_linear_file": scene_path.name,
                    }
                )
            self._write_recipe(scan_dir)
            step_meta = {
                "step_col_px": [256.0, 0.0],
                "step_row_px": [0.0, 512.0],
                "step_col_samples_kept": 1,
                "step_row_samples_kept": 0,
            }

            with patch(
                "v3se_printer.scan.stitching.layout.estimate_step_vectors",
                return_value=step_meta,
            ) as estimate:
                stitch_scan_outputs(
                    tiles=tiles,
                    out_dir=str(scan_dir),
                    build_pyramidal_tiff=True,
                    tiff_compression="lzw",
                    image_roles="raw",
                    openexr_helper=self.openexr_helper,
                    stitch_settings={
                        "final_megapix": -1.0,
                        "layout_blend": "overwrite",
                        "layout_refine_positions": False,
                        "in_memory_max_bytes": 0,
                        "use_memmap": True,
                    },
                )

            estimate.assert_called_once()
            metadata = json.loads((scan_dir / "stitch_meta.json").read_text(encoding="utf-8"))
            transforms = metadata["stages"][0]["tile_transforms"]
            self.assertEqual(metadata["mosaic_size_px"], [768, 512])
            self.assertEqual([item["applied_position_px"] for item in transforms], [[0, 0], [256, 0]])
            self.assertEqual([item["matrix"][0][2] for item in transforms], [0.0, 256.0])
            decoded_path = scan_dir / "multitile_scene_linear.f32"
            subprocess.run(
                [self.vips, "rawsave", str(scan_dir / "mosaic_scene_linear.exr"), str(decoded_path)],
                check=True,
                capture_output=True,
            )
            exr = np.fromfile(decoded_path, dtype=np.float32).reshape(512, 768, 4)
            np.testing.assert_allclose(exr[0, 0, :3], scenes[0], rtol=5e-4, atol=5e-5)
            np.testing.assert_allclose(exr[0, 256, :3], scenes[1], rtol=5e-4, atol=5e-5)
            matrix = np.array(
                [
                    [1.660491, -0.587641, -0.072850],
                    [-0.124550, 1.132900, -0.008349],
                    [-0.018151, -0.100579, 1.118730],
                ],
                dtype=np.float32,
            )
            expected_linear = np.clip(np.asarray(scenes, dtype=np.float32) @ matrix.T, 0.0, 1.0)
            expected_display = np.where(
                expected_linear <= 0.0031308,
                expected_linear * 12.92,
                1.055 * np.power(expected_linear, 1.0 / 2.4) - 0.055,
            )
            expected_bgr = np.floor(expected_display[:, ::-1] * 65535.0 + 0.5).astype(np.uint16)
            mosaic = cv2.imread(str(scan_dir / "mosaic_full.tif"), cv2.IMREAD_UNCHANGED)
            np.testing.assert_allclose(mosaic[0, 0], expected_bgr[0], rtol=0.0, atol=1.0)
            np.testing.assert_allclose(mosaic[0, 256], expected_bgr[1], rtol=0.0, atol=1.0)
            self.assertFalse((scan_dir / "_mosaic_memmap.dat").exists())

    def test_forced_memmap_preserves_uint16_feather_blend_and_preview(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            scan_dir = Path(temp_dir)
            alignment_path = scan_dir / "alignment.jpg"
            first_path = scan_dir / "first.tif"
            second_path = scan_dir / "second.tif"
            self.assertTrue(cv2.imwrite(str(alignment_path), np.full((16, 20, 3), 80, dtype=np.uint8)))
            first = np.full((16, 20, 3), [10000, 20000, 30000], dtype=np.uint16)
            second = np.full((16, 20, 3), [50000, 40000, 10000], dtype=np.uint16)
            expected = np.array([30000, 30000, 20000], dtype=np.uint16)
            self.assertTrue(cv2.imwrite(str(first_path), first))
            self.assertTrue(cv2.imwrite(str(second_path), second))
            entries = [
                Entry(0, 0, str(alignment_path), str(first_path)),
                Entry(0, 1, str(alignment_path), str(second_path)),
            ]

            pano, memmap_path, weights_path = composite_tiles(
                cv2=cv2,
                np=np,
                entries=entries,
                pos_by_rc_f={(0, 0): (0.0, 0.0), (0, 1): (0.0, 0.0)},
                min_x=0.0,
                min_y=0.0,
                out_w=20,
                out_h=16,
                w_final=20,
                h_final=16,
                source_w=20,
                source_h=16,
                composite_dtype="uint16",
                out_dir=str(scan_dir),
                blend_mode="feather",
                feather_px=4,
                inmem_max_bytes=0,
                use_memmap=True,
                black_transparent=False,
                black_threshold=2,
                refined_gains=None,
                progress_cb=None,
                cancel_cb=None,
            )
            self.assertIsInstance(pano, np.memmap)
            self.assertIsNotNone(memmap_path)
            self.assertIsNotNone(weights_path)
            self.assertEqual(pano.dtype, np.uint16)
            self.assertTrue(np.all(pano == expected))
            pano.flush()

            mosaic_path = scan_dir / "mosaic_full.tif"
            write_mosaic_tiff(
                pano=pano,
                memmap_path=memmap_path,
                out_w=20,
                out_h=16,
                mosaic_path=str(mosaic_path),
                tiff_compression="lzw",
                px_per_mm_target=None,
                tiff_tile=False,
                tiff_tile_width=None,
                tiff_tile_height=None,
                tiff_predictor="horizontal",
            )
            preview_path = write_preview_jpeg(
                mosaic_path=str(mosaic_path),
                out_dir=str(scan_dir),
                max_dim=64,
                quality=85,
            )
            mosaic = cv2.imread(str(mosaic_path), cv2.IMREAD_UNCHANGED)
            preview = cv2.imread(preview_path, cv2.IMREAD_UNCHANGED)
            mosaic_profile = pyvips.Image.new_from_file(str(mosaic_path)).get("icc-profile-data")
            preview_profile = pyvips.Image.new_from_file(preview_path).get("icc-profile-data")
            self.assertEqual(mosaic.dtype, np.uint16)
            self.assertTrue(np.all(mosaic == expected))
            self.assertEqual(preview.dtype, np.uint8)
            expected_profile = require_srgb_icc_profile().read_bytes()
            self.assertEqual(mosaic_profile, expected_profile)
            self.assertEqual(preview_profile, expected_profile)

    def test_two_tile_layout_persists_solved_and_applied_geometry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            scan_dir = Path(temp_dir)
            entries = []
            by_rc = {}
            for col, value in enumerate((0.25, 0.75)):
                alignment_path = scan_dir / f"tile_{col}.jpg"
                scene_path = scan_dir / f"tile_{col}_scene_linear.tif"
                self.assertTrue(cv2.imwrite(str(alignment_path), np.full((8, 10, 3), 80, dtype=np.uint8)))
                self._write_float_rgb(scene_path, np.full((8, 10, 3), value, dtype=np.float32))
                entries.append(Entry(0, col, str(alignment_path), str(scene_path)))
                by_rc[(0, col)] = str(alignment_path)
            step_meta = {
                "step_col_px": [6.25, 0.0],
                "step_row_px": [0.0, 8.0],
                "step_col_samples_kept": 1,
                "step_row_samples_kept": 0,
            }
            with patch(
                "v3se_printer.scan.stitching.layout.estimate_step_vectors",
                return_value=step_meta,
            ):
                mosaic = stitch_layout_mosaic(
                    cv2=cv2,
                    np=np,
                    entries=entries,
                    by_rc=by_rc,
                    nrows=1,
                    ncols=2,
                    tile_w=10,
                    tile_h=8,
                    orig_mp=0.00008,
                    serpentine=True,
                    out_dir=str(scan_dir),
                    settings={
                        "layout_blend": "overwrite",
                        "layout_refine_positions": False,
                        "layout_black_transparent": False,
                        "use_memmap": False,
                    },
                    final_megapix=-1.0,
                    composite_dtype="float32",
                    progress_cb=None,
                    cancel_cb=None,
                )

        self.assertEqual((mosaic.out_w, mosaic.out_h), (17, 8))
        transforms = mosaic.stage_meta["tile_transforms"]
        self.assertEqual(transforms[0]["solved_position_px"], [0.0, 0.0])
        self.assertEqual(transforms[0]["applied_position_px"], [0, 0])
        self.assertEqual(transforms[1]["solved_position_px"], [6.25, 0.0])
        self.assertEqual(transforms[1]["applied_position_px"], [6, 0])
        self.assertEqual(
            transforms[1]["matrix"],
            [[1.0, 0.0, 6.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
        )

    def test_pyramidal_tiff_contains_reduced_subifd_levels(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "mosaic_pyramidal.ome.tif"
            pano = np.full((512, 512, 3), 32000, dtype=np.uint16)
            write_mosaic_tiff(
                pano=pano,
                memmap_path=None,
                out_w=512,
                out_h=512,
                mosaic_path=str(path),
                tiff_compression="deflate",
                px_per_mm_target=100.0,
                tiff_tile=True,
                tiff_tile_width=128,
                tiff_tile_height=128,
                tiff_predictor="horizontal",
                pyramidal=True,
            )

            base = pyvips.Image.new_from_file(str(path))
            reduced = pyvips.Image.new_from_file(str(path), subifd=0)
            self.assertEqual((base.width, base.height, base.format), (512, 512, "ushort"))
            self.assertEqual((reduced.width, reduced.height, reduced.format), (256, 256, "ushort"))
            self.assertGreater(base.get("n-subifds"), 0)
            self.assertEqual((base.get("tile-width"), base.get("tile-height")), (128, 128))
            self.assertEqual((base.xres, base.yres), (100.0, 100.0))
            self.assertEqual(base.get("icc-profile-data"), require_srgb_icc_profile().read_bytes())
            ome = ElementTree.fromstring(base.get("image-description"))
            pixels = ome.find("{http://www.openmicroscopy.org/Schemas/OME/2016-06}Image/{http://www.openmicroscopy.org/Schemas/OME/2016-06}Pixels")
            self.assertIsNotNone(pixels)
            self.assertEqual((pixels.get("SizeX"), pixels.get("SizeY"), pixels.get("SizeC")), ("512", "512", "3"))

    def test_libvips_outputs_report_real_pixel_progress(self) -> None:
        progress: list[tuple[str, str, int, int | None, str]] = []

        def callback(phase: str, label: str, completed: int, total: int | None, unit: str) -> None:
            progress.append((phase, label, completed, total, unit))

        height, width = 96, 128
        with tempfile.TemporaryDirectory() as temp_dir:
            flat_path = Path(temp_dir) / "flat.tif"
            pyramidal_path = Path(temp_dir) / "pyramidal.ome.tif"
            write_mosaic_tiff(
                pano=np.full((height, width, 3), 32000, dtype=np.uint16),
                memmap_path=None,
                out_w=width,
                out_h=height,
                mosaic_path=str(flat_path),
                tiff_compression="deflate",
                px_per_mm_target=None,
                tiff_tile=True,
                tiff_tile_width=64,
                tiff_tile_height=64,
                tiff_predictor="horizontal",
                progress_cb=callback,
            )
            write_scene_linear_mosaic_tiff(
                pano=np.full((height, width, 3), [0.1, 0.2, 0.3], dtype=np.float32),
                memmap_path=None,
                out_w=width,
                out_h=height,
                mosaic_path=str(pyramidal_path),
                tiff_compression="deflate",
                px_per_mm_target=None,
                tiff_tile=True,
                tiff_tile_width=64,
                tiff_tile_height=64,
                tiff_predictor="horizontal",
                pyramidal=True,
                progress_cb=callback,
            )
            preview_path = write_preview_jpeg(
                mosaic_path=str(flat_path),
                out_dir=temp_dir,
                max_dim=64,
                quality=85,
                progress_cb=callback,
            )
            preview = pyvips.Image.new_from_file(preview_path)
            preview_pixels = preview.width * preview.height

        for phase, label, total in (
            ("write-flat-tiff", "Writing flat TIFF", width * height),
            ("write-pyramidal-tiff", "Writing pyramidal TIFF", width * height),
            ("write-preview", "Writing preview", preview_pixels),
        ):
            records = [record for record in progress if record[0] == phase]
            self.assertGreaterEqual(len(records), 2)
            self.assertEqual(records[0], (phase, label, 0, total, "pixels"))
            self.assertEqual(records[-1], (phase, label, total, total, "pixels"))
            self.assertEqual([record[2] for record in records], sorted(record[2] for record in records))
            self.assertEqual({record[3] for record in records}, {total})

    def test_libvips_writer_honors_cancellation_during_evaluation(self) -> None:
        checks = 0

        def cancel() -> None:
            nonlocal checks
            checks += 1
            if checks >= 2:
                raise InterruptedError("cancelled")

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cancelled.tif"
            with self.assertRaisesRegex(InterruptedError, "cancelled"):
                write_mosaic_tiff(
                    pano=np.full((1024, 1024, 3), 32000, dtype=np.uint16),
                    memmap_path=None,
                    out_w=1024,
                    out_h=1024,
                    mosaic_path=str(path),
                    tiff_compression="deflate",
                    px_per_mm_target=None,
                    tiff_tile=True,
                    tiff_tile_width=128,
                    tiff_tile_height=128,
                    tiff_predictor="horizontal",
                    cancel_cb=cancel,
                )

        self.assertGreaterEqual(checks, 2)

    def test_float32_feather_blend_preserves_scene_values_above_one(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            scan_dir = Path(temp_dir)
            alignment_path = scan_dir / "alignment.jpg"
            first_path = scan_dir / "first.tif"
            second_path = scan_dir / "second.tif"
            self.assertTrue(cv2.imwrite(str(alignment_path), np.full((8, 10, 3), 80, dtype=np.uint8)))
            first = np.full((8, 10, 3), [0.25, 1.0, 2.0], dtype=np.float32)
            second = np.full((8, 10, 3), [1.75, 3.0, 4.0], dtype=np.float32)
            self.assertTrue(cv2.imwrite(str(first_path), first))
            self.assertTrue(cv2.imwrite(str(second_path), second))
            entries = [
                Entry(0, 0, str(alignment_path), str(first_path)),
                Entry(0, 1, str(alignment_path), str(second_path)),
            ]

            pano, memmap_path, weights_path = composite_tiles(
                cv2=cv2,
                np=np,
                entries=entries,
                pos_by_rc_f={(0, 0): (0.0, 0.0), (0, 1): (0.0, 0.0)},
                min_x=0.0,
                min_y=0.0,
                out_w=10,
                out_h=8,
                w_final=10,
                h_final=8,
                source_w=10,
                source_h=8,
                composite_dtype="float32",
                out_dir=str(scan_dir),
                blend_mode="feather",
                feather_px=2,
                inmem_max_bytes=0,
                use_memmap=True,
                black_transparent=False,
                black_threshold=2,
                refined_gains=None,
                progress_cb=None,
                cancel_cb=None,
            )

            self.assertIsInstance(pano, np.memmap)
            self.assertIsNotNone(memmap_path)
            self.assertIsNotNone(weights_path)
            self.assertEqual(pano.dtype, np.float32)
            np.testing.assert_array_equal(pano, np.full((8, 10, 3), [1.0, 2.0, 3.0], dtype=np.float32))

    def test_average_blend_uses_true_counts_for_black_and_signed_pixels(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            scan_dir = Path(temp_dir)
            alignment_path = scan_dir / "alignment.jpg"
            self.assertTrue(cv2.imwrite(str(alignment_path), np.full((4, 6, 3), 80, dtype=np.uint8)))
            values = ([-0.6, 0.0, 0.6], [0.0, 0.0, 0.0], [0.9, 0.3, -0.3])
            entries = []
            positions = {}
            for col, value in enumerate(values):
                path = scan_dir / f"scene_{col}.tif"
                self._write_float_rgb(path, np.full((4, 6, 3), value, dtype=np.float32))
                entries.append(Entry(0, col, str(alignment_path), str(path)))
                positions[(0, col)] = (0.0, 0.0)

            pano, _memmap_path, _weights_path = composite_tiles(
                cv2=cv2,
                np=np,
                entries=entries,
                pos_by_rc_f=positions,
                min_x=0.0,
                min_y=0.0,
                out_w=6,
                out_h=4,
                w_final=6,
                h_final=4,
                source_w=6,
                source_h=4,
                composite_dtype="float32",
                out_dir=str(scan_dir),
                blend_mode="average",
                feather_px=None,
                inmem_max_bytes=1_000_000,
                use_memmap=False,
                black_transparent=False,
                black_threshold=0,
                refined_gains=None,
                progress_cb=None,
                cancel_cb=None,
            )

            np.testing.assert_allclose(pano, np.full((4, 6, 3), [0.1, 0.1, 0.1], dtype=np.float32), atol=1e-7)

    def test_rejects_composite_dimensions_that_do_not_match_alignment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            scan_dir = Path(temp_dir)
            alignment_path = scan_dir / "tile.jpg"
            raw_path = scan_dir / "tile.nef"
            display_path = scan_dir / "tile.tif"
            scene_path = scan_dir / "tile_scene_linear.tif"
            self.assertTrue(cv2.imwrite(str(alignment_path), np.zeros((16, 20, 3), dtype=np.uint8)))
            raw_path.write_bytes(b"raw")
            self.assertTrue(cv2.imwrite(str(display_path), np.zeros((16, 20, 3), dtype=np.uint16)))
            self._write_float_rgb(scene_path, np.zeros((16, 21, 3), dtype=np.float32))
            self._write_recipe(scan_dir)
            with self.assertRaisesRegex(RuntimeError, "dimensions do not match"):
                stitch_scan_outputs(
                    tiles=[
                        {
                            "row": 0,
                            "col": 0,
                            "x_mm": 0.0,
                            "y_mm": 0.0,
                            "file": alignment_path.name,
                            "raw_file": raw_path.name,
                            "display_file": display_path.name,
                            "scene_linear_file": scene_path.name,
                        }
                    ],
                    out_dir=str(scan_dir),
                    build_pyramidal_tiff=True,
                    tiff_compression="lzw",
                    image_roles="raw",
                    openexr_helper=self.openexr_helper,
                )

    def test_validation_failure_preserves_existing_stitch_outputs(self) -> None:
        existing = {
            "mosaic_full.tif": b"old-flat",
            "mosaic_pyramidal.ome.tif": b"old-pyramid",
            "mosaic_thumb_2000.jpg": b"old-preview",
            "stitch_meta.json": b"old-metadata",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            scan_dir = Path(temp_dir)
            for name, content in existing.items():
                (scan_dir / name).write_bytes(content)

            with self.assertRaisesRegex(RuntimeError, "does not exist"):
                stitch_scan_outputs(
                    tiles=[{"row": 0, "col": 0, "file": "missing.tif"}],
                    out_dir=str(scan_dir),
                    build_pyramidal_tiff=True,
                    tiff_compression="deflate",
                    image_roles="single",
                )

            self.assertEqual(
                {name: (scan_dir / name).read_bytes() for name in existing},
                existing,
            )

    def test_write_failure_preserves_existing_stitch_outputs(self) -> None:
        existing = {
            "mosaic_full.tif": b"old-flat",
            "mosaic_pyramidal.ome.tif": b"old-pyramid",
            "mosaic_thumb_2000.jpg": b"old-preview",
            "stitch_meta.json": b"old-metadata",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            scan_dir = Path(temp_dir)
            tile_path = scan_dir / "tile.tif"
            self.assertTrue(cv2.imwrite(str(tile_path), np.full((16, 20, 3), 80, dtype=np.uint8)))
            for name, content in existing.items():
                (scan_dir / name).write_bytes(content)

            with patch(
                "v3se_printer.scan.stitch_outputs.write_preview_jpeg",
                side_effect=RuntimeError("preview write failed"),
            ), self.assertRaisesRegex(RuntimeError, "preview write failed"):
                stitch_scan_outputs(
                    tiles=[{"row": 0, "col": 0, "file": tile_path.name}],
                    out_dir=str(scan_dir),
                    build_pyramidal_tiff=True,
                    tiff_compression="deflate",
                    image_roles="single",
                    stitch_settings={"final_megapix": -1.0, "layout_blend": "overwrite"},
                )

            self.assertEqual(
                {name: (scan_dir / name).read_bytes() for name in existing},
                existing,
            )
            self.assertIn("preview write failed", (scan_dir / "stitch_error.txt").read_text(encoding="utf-8"))

    def test_cancellation_preserves_existing_stitch_outputs(self) -> None:
        existing = {
            "mosaic_full.tif": b"old-flat",
            "mosaic_pyramidal.ome.tif": b"old-pyramid",
            "mosaic_thumb_2000.jpg": b"old-preview",
            "stitch_meta.json": b"old-metadata",
        }
        cancel_during_publish = False

        def cancel() -> None:
            if cancel_during_publish:
                raise InterruptedError("cancelled")

        def progress(phase: str, _label: str, completed: int, _total: int | None, _unit: str) -> None:
            nonlocal cancel_during_publish
            if phase == "publish-outputs" and completed == 1:
                cancel_during_publish = True

        with tempfile.TemporaryDirectory() as temp_dir:
            scan_dir = Path(temp_dir)
            tile_path = scan_dir / "tile.tif"
            self.assertTrue(cv2.imwrite(str(tile_path), np.full((16, 20, 3), 80, dtype=np.uint8)))
            for name, content in existing.items():
                (scan_dir / name).write_bytes(content)

            with self.assertRaisesRegex(InterruptedError, "cancelled"):
                stitch_scan_outputs(
                    tiles=[{"row": 0, "col": 0, "file": tile_path.name}],
                    out_dir=str(scan_dir),
                    build_pyramidal_tiff=True,
                    tiff_compression="deflate",
                    image_roles="single",
                    cancel_cb=cancel,
                    progress_cb=progress,
                    stitch_settings={"final_megapix": -1.0, "layout_blend": "overwrite"},
                )

            self.assertEqual(
                {name: (scan_dir / name).read_bytes() for name in existing},
                existing,
            )

    def test_raw_mode_rejects_incomplete_tile_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            scan_dir = Path(temp_dir)
            tile_path = scan_dir / "tile.jpg"
            self.assertTrue(cv2.imwrite(str(tile_path), np.zeros((8, 8, 3), dtype=np.uint8)))
            self._write_recipe(scan_dir)
            with self.assertRaisesRegex(RuntimeError, "file, raw_file, scene_linear_file, and display_file"):
                stitch_scan_outputs(
                    tiles=[{"row": 0, "col": 0, "x_mm": 0.0, "y_mm": 0.0, "file": tile_path.name}],
                    out_dir=str(scan_dir),
                    build_pyramidal_tiff=True,
                    tiff_compression="lzw",
                    image_roles="raw",
                    openexr_helper=self.openexr_helper,
                )

    def test_raw_mode_requires_prebuilt_openexr_helper(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            scan_dir = Path(temp_dir)
            self._write_recipe(scan_dir)
            with self.assertRaisesRegex(RuntimeError, "prebuilt OpenEXR helper"):
                stitch_scan_outputs(
                    tiles=[],
                    out_dir=str(scan_dir),
                    build_pyramidal_tiff=True,
                    tiff_compression="lzw",
                    image_roles="raw",
                )

    def test_raw_mode_rejects_malformed_recipe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            scan_dir = Path(temp_dir)
            self._write_recipe(scan_dir)
            (scan_dir / "raw_development.json").write_text('{"version": 2}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "fields do not match"):
                stitch_scan_outputs(
                    tiles=[],
                    out_dir=str(scan_dir),
                    build_pyramidal_tiff=True,
                    tiff_compression="lzw",
                    image_roles="raw",
                    openexr_helper=self.openexr_helper,
                )

    def test_raw_mode_rejects_malformed_scan_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            scan_dir = Path(temp_dir)
            self._write_recipe(scan_dir)
            (scan_dir / "scan_params.json").write_text('{"image_roles": "raw"}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "reference raw_development.json"):
                stitch_scan_outputs(
                    tiles=[],
                    out_dir=str(scan_dir),
                    build_pyramidal_tiff=True,
                    tiff_compression="lzw",
                    image_roles="raw",
                    openexr_helper=self.openexr_helper,
                )

    def test_raw_mode_rejects_obsolete_composite_role(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            scan_dir = Path(temp_dir)
            self._write_recipe(scan_dir)
            with self.assertRaisesRegex(RuntimeError, "obsolete composite_file"):
                stitch_scan_outputs(
                    tiles=[{"row": 0, "col": 0, "file": "tile.jpg", "composite_file": "tile.tif"}],
                    out_dir=str(scan_dir),
                    build_pyramidal_tiff=True,
                    tiff_compression="lzw",
                    image_roles="raw",
                    openexr_helper=self.openexr_helper,
                )

    def test_raw_mode_rejects_mismatched_capture_stems(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            scan_dir = Path(temp_dir)
            alignment_path = scan_dir / "tile_a.jpg"
            raw_path = scan_dir / "tile_a.nef"
            display_path = scan_dir / "tile_a.tif"
            scene_path = scan_dir / "tile_b_scene_linear.tif"
            self.assertTrue(cv2.imwrite(str(alignment_path), np.zeros((8, 8, 3), dtype=np.uint8)))
            raw_path.write_bytes(b"raw")
            self.assertTrue(cv2.imwrite(str(display_path), np.zeros((8, 8, 3), dtype=np.uint16)))
            self._write_float_rgb(scene_path, np.zeros((8, 8, 3), dtype=np.float32))
            self._write_recipe(scan_dir)
            with self.assertRaisesRegex(RuntimeError, "matching capture stem"):
                stitch_scan_outputs(
                    tiles=[
                        {
                            "row": 0,
                            "col": 0,
                            "x_mm": 0.0,
                            "y_mm": 0.0,
                            "file": alignment_path.name,
                            "raw_file": raw_path.name,
                            "display_file": display_path.name,
                            "scene_linear_file": scene_path.name,
                        }
                    ],
                    out_dir=str(scan_dir),
                    build_pyramidal_tiff=True,
                    tiff_compression="lzw",
                    image_roles="raw",
                    openexr_helper=self.openexr_helper,
                )

    def test_rejects_missing_grid_cell(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            scan_dir = Path(temp_dir)
            tile_path = scan_dir / "tile.tif"
            self.assertTrue(cv2.imwrite(str(tile_path), np.zeros((8, 8, 3), dtype=np.uint8)))
            with self.assertRaisesRegex(RuntimeError, "Missing tile grid cell"):
                stitch_scan_outputs(
                    tiles=[
                        {"row": 0, "col": 0, "file": tile_path.name},
                        {"row": 0, "col": 2, "file": tile_path.name},
                    ],
                    out_dir=str(scan_dir),
                    build_pyramidal_tiff=True,
                    tiff_compression="lzw",
                    image_roles="single",
                )


if __name__ == "__main__":
    unittest.main()
