from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import cv2  # type: ignore
import numpy as np  # type: ignore
import pyvips  # type: ignore
import rawpy  # type: ignore

from v3se_printer.calibration import CalibrationError, NormalizedROI
from v3se_printer.color import require_srgb_icc_profile
from v3se_printer.raw import (
    LIBRAW_VERSION,
    RAWPY_VERSION,
    RawHeadroomReading,
    RawDevelopmentRecipe,
    WhiteBalance,
    analyze_raw_headroom,
    camera_to_rec2020_matrix,
    calculate_white_balance,
    calibrate_development_recipe,
    develop_nef,
)


IDENTITY_CAMERA_XYZ = np.array(
    [
        [1.71650918, -0.35564024, -0.25334553],
        [-0.66669379, 1.61650234, 0.01576917],
        [0.01764515, -0.04278060, 0.94230451],
        [0.0, 0.0, 0.0],
    ],
    dtype=np.float32,
)
IDENTITY_MATRIX = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


class FakeRaw:
    def __init__(self, output: np.ndarray) -> None:
        self.output = output
        self.rgb_xyz_matrix = IDENTITY_CAMERA_XYZ
        self.kwargs: dict[str, object] | None = None
        self.postprocess_count = 0

    def __enter__(self) -> "FakeRaw":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def postprocess(self, **kwargs: object) -> np.ndarray:
        self.postprocess_count += 1
        self.kwargs = kwargs
        return self.output


class FakeHeadroomRaw:
    def __init__(
        self,
        image: np.ndarray,
        pattern: np.ndarray,
        black_levels: object,
        white_levels: object,
    ) -> None:
        self.raw_image_visible = image
        self.raw_pattern = pattern
        self.black_level_per_channel = black_levels
        self.camera_white_level_per_channel = white_levels

    def __enter__(self) -> "FakeHeadroomRaw":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class RawHeadroomTests(unittest.TestCase):
    def test_selected_jpeg_roi_reports_saturation_by_cfa_index(self) -> None:
        pattern = np.array([[0, 1], [3, 2]], dtype=np.uint8)
        black_levels = [100, 200, 300, 400]
        white_levels = [1100, 1200, 1300, 1400]
        rows, cols = np.indices((24, 24))
        indices = pattern[rows % 2, cols % 2]
        image = np.empty((24, 24), dtype=np.uint16)
        for index in range(4):
            image[indices == index] = white_levels[index]
        selected = np.zeros(image.shape, dtype=bool)
        selected[7:17, 7:17] = True
        for index in range(4):
            image[selected & (indices == index)] = black_levels[index]
        image[selected & (indices == 0)] = 1095
        blue = np.argwhere(selected & (indices == 2))[0]
        image[blue[0], blue[1]] = 1295
        image[selected & (indices == 3)] = 1395
        fake = FakeHeadroomRaw(image, pattern, black_levels, white_levels)

        reading = analyze_raw_headroom(
            "selected.nef",
            NormalizedROI(0.25, 0.25, 0.5, 0.5),
            reference_size=(20, 20),
            loader=lambda _path: fake,
        )

        np.testing.assert_allclose(
            reading.percentile_99_levels,
            (0.995, 0.0, 0.7562, 0.995),
        )
        self.assertEqual(reading.highlight_level, 0.995)
        self.assertEqual(reading.saturated_fractions, (1.0, 0.0, 0.04, 1.0))
        self.assertTrue(reading.meaningful_saturation)

    def test_meaningful_saturation_starts_at_one_percent(self) -> None:
        levels = (0.8, 0.7, 0.6, 0.7)
        self.assertFalse(RawHeadroomReading(levels, (0.0099, 0.0, 0.0, 0.0)).meaningful_saturation)
        self.assertTrue(RawHeadroomReading(levels, (0.0, 0.01, 0.0, 0.0)).meaningful_saturation)
        with self.assertRaises(ValueError):
            RawHeadroomReading(levels, (0.0, 0.0, 0.0, 1.01))

    def test_missing_or_unsupported_raw_metadata_fails(self) -> None:
        pattern = np.array([[0, 1], [3, 2]], dtype=np.uint8)
        image = np.full((8, 8), 500, dtype=np.uint16)
        cases = (
            FakeHeadroomRaw(image, pattern, [100, 100, 100, 100], None),
            FakeHeadroomRaw(image, pattern, [100, 100, 100], [1000, 1000, 1000, 1000]),
            FakeHeadroomRaw(image, pattern, [100, 100, 100, 100], [100, 1000, 1000, 1000]),
            FakeHeadroomRaw(image, np.array([[0, 1], [1, 2]], dtype=np.uint8), [100] * 4, [1000] * 4),
            FakeHeadroomRaw(image.astype(np.float32), pattern, [100] * 4, [1000] * 4),
        )
        for fake in cases:
            with self.subTest(fake=fake), self.assertRaises(CalibrationError):
                analyze_raw_headroom(
                    "selected.nef",
                    NormalizedROI(),
                    reference_size=(8, 8),
                    loader=lambda _path, fake=fake: fake,
                )

    def test_roi_must_sample_every_cfa_channel(self) -> None:
        fake = FakeHeadroomRaw(
            np.full((4, 4), 500, dtype=np.uint16),
            np.array([[0, 1], [3, 2]], dtype=np.uint8),
            [100] * 4,
            [1000] * 4,
        )
        with self.assertRaisesRegex(CalibrationError, "every CFA channel"):
            analyze_raw_headroom(
                "selected.nef",
                NormalizedROI(0.0, 0.0, 0.1, 0.1),
                reference_size=(4, 4),
                loader=lambda _path: fake,
            )


class WhiteBalanceTests(unittest.TestCase):
    def test_gray_patch_produces_fixed_rgbg_gains(self) -> None:
        pattern = np.array([[0, 1], [3, 2]], dtype=np.uint8)
        image = np.empty((100, 100), dtype=np.uint16)
        signals = {0: 900, 1: 1900, 2: 400, 3: 1900}
        for row in range(100):
            for col in range(100):
                image[row, col] = 100 + signals[int(pattern[row % 2, col % 2])]
        balance = calculate_white_balance(
            raw_image=image,
            raw_pattern=pattern,
            color_desc=b"RGBG",
            black_levels=[100, 100, 100, 100],
            white_levels=[4095, 4095, 4095, 4095],
            roi=NormalizedROI(),
        )
        self.assertAlmostEqual(balance.red, 1900 / 900)
        self.assertAlmostEqual(balance.green, 1.0)
        self.assertAlmostEqual(balance.blue, 1900 / 400)
        self.assertAlmostEqual(balance.green_2, 1.0)

    def test_dark_gray_patch_is_rejected(self) -> None:
        with self.assertRaises(CalibrationError):
            calculate_white_balance(
                raw_image=np.full((100, 100), 101, dtype=np.uint16),
                raw_pattern=np.array([[0, 1], [3, 2]], dtype=np.uint8),
                color_desc=b"RGBG",
                black_levels=[100, 100, 100, 100],
                white_levels=[4095, 4095, 4095, 4095],
                roi=NormalizedROI(),
            )

    def test_sensor_saturated_gray_channel_is_rejected(self) -> None:
        pattern = np.array([[0, 1], [3, 2]], dtype=np.uint8)
        image = np.full((100, 100), 1200, dtype=np.uint16)
        rows, cols = np.indices(image.shape)
        indices = pattern[rows % 2, cols % 2]
        image[indices == 0] = 3827

        with self.assertRaisesRegex(CalibrationError, "clipped"):
            calculate_white_balance(
                raw_image=image,
                raw_pattern=pattern,
                color_desc=b"RGBG",
                black_levels=[200, 200, 200, 200],
                white_levels=[3827, 3827, 3827, 3827],
                roi=NormalizedROI(),
            )

    def test_developer_writes_paired_scene_linear_and_display_tiles(self) -> None:
        developed = np.full((14, 20, 3), 16384, dtype=np.uint16)
        fake = FakeRaw(developed)
        balance = WhiteBalance(2.0, 1.0, 1.5, 1.0)
        recipe = RawDevelopmentRecipe(
            2,
            balance,
            2.0,
            0.5,
            0.25,
            NormalizedROI(),
            "/calibration/white_balance.jpg",
            "/calibration/white_balance.nef",
            IDENTITY_MATRIX,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            display_path = Path(temp_dir) / "tile.tif"
            scene_path = Path(temp_dir) / "tile_scene_linear.tif"
            result = develop_nef(
                "source.nef",
                display_path,
                scene_path,
                recipe,
                output_size=(18, 12),
                loader=lambda _path: fake,
            )
            display = cv2.imread(str(display_path), cv2.IMREAD_UNCHANGED)
            scene = cv2.imread(str(scene_path), cv2.IMREAD_UNCHANGED)
            profile = pyvips.Image.new_from_file(str(display_path)).get("icc-profile-data")
        self.assertEqual(result.display_path, display_path)
        self.assertEqual(result.scene_linear_path, scene_path)
        self.assertEqual(display.dtype, np.uint16)
        self.assertEqual(scene.dtype, np.float32)
        self.assertEqual(display.shape, (12, 18, 3))
        np.testing.assert_allclose(scene, 2.0 * 16384.0 / 65535.0, rtol=1e-6)
        self.assertTrue(np.all((display >= 48190) & (display <= 48195)))
        self.assertEqual(profile, require_srgb_icc_profile().read_bytes())
        self.assertEqual(fake.kwargs["user_wb"], list(balance.gains))
        self.assertEqual(fake.kwargs["demosaic_algorithm"], rawpy.DemosaicAlgorithm.AHD)
        self.assertFalse(fake.kwargs["use_camera_wb"])
        self.assertEqual(fake.kwargs["output_color"], rawpy.ColorSpace.raw)
        self.assertEqual(fake.kwargs["output_bps"], 16)
        self.assertTrue(fake.kwargs["no_auto_bright"])
        self.assertEqual(fake.kwargs["adjust_maximum_thr"], 0.0)
        self.assertEqual(fake.kwargs["gamma"], (1.0, 1.0))
        self.assertEqual(fake.kwargs["highlight_mode"], rawpy.HighlightMode.Ignore)
        self.assertEqual(fake.postprocess_count, 1)

    def test_display_transform_preserves_rgb_channel_order(self) -> None:
        source = np.zeros((300, 2, 3), dtype=np.uint16)
        source[:, :, 0] = 65535
        fake = FakeRaw(source)
        balance = WhiteBalance(1.0, 1.0, 1.0, 1.0)
        recipe = RawDevelopmentRecipe(
            2,
            balance,
            1.0,
            0.25,
            0.25,
            NormalizedROI(),
            "/calibration/white_balance.jpg",
            "/calibration/white_balance.nef",
            IDENTITY_MATRIX,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            display_path = Path(temp_dir) / "tile.tif"
            scene_path = Path(temp_dir) / "tile_scene_linear.tif"
            develop_nef("source.nef", display_path, scene_path, recipe, loader=lambda _path: fake)
            display = cv2.imread(str(display_path), cv2.IMREAD_UNCHANGED)
            scene = cv2.imread(str(scene_path), cv2.IMREAD_UNCHANGED)
        np.testing.assert_array_equal(display[0, 0], [0, 0, 65535])
        np.testing.assert_array_equal(display[-1, 0], [0, 0, 65535])
        np.testing.assert_array_equal(scene[0, 0], [0.0, 0.0, 1.0])
        self.assertEqual(fake.postprocess_count, 1)

    def test_calibration_derives_one_linear_gain_from_matching_jpeg_and_nef(self) -> None:
        raw_linear = np.full((12, 18, 3), 16384, dtype=np.uint16)
        fake = FakeRaw(raw_linear)
        with tempfile.TemporaryDirectory() as temp_dir:
            jpeg_path = Path(temp_dir) / "white_balance.jpg"
            self.assertTrue(cv2.imwrite(str(jpeg_path), np.full((12, 18, 3), 188, dtype=np.uint8)))
            recipe = calibrate_development_recipe(
                "white_balance.nef",
                jpeg_path,
                WhiteBalance(1.0, 1.0, 1.0, 1.0),
                NormalizedROI(),
                reference_size=(18, 12),
                loader=lambda _path: fake,
            )
        self.assertAlmostEqual(recipe.reference_raw_linear_luminance, 16384 / 65535, places=5)
        self.assertAlmostEqual(recipe.reference_jpeg_linear_luminance, 0.5029, places=3)
        self.assertAlmostEqual(recipe.display_linear_gain, 2.011, places=2)
        self.assertEqual(recipe.version, 2)
        self.assertEqual(recipe.rawpy_version, RAWPY_VERSION)
        self.assertEqual(recipe.libraw_version, LIBRAW_VERSION)
        np.testing.assert_allclose(recipe.camera_to_working_matrix, IDENTITY_MATRIX, rtol=1e-6, atol=1e-6)

    def test_scene_linear_tile_preserves_values_above_display_white(self) -> None:
        fake = FakeRaw(np.full((4, 6, 3), 60000, dtype=np.uint16))
        recipe = RawDevelopmentRecipe(
            2,
            WhiteBalance(1.0, 1.0, 1.0, 1.0),
            2.0,
            0.5,
            0.25,
            NormalizedROI(),
            "/calibration/white_balance.jpg",
            "/calibration/white_balance.nef",
            IDENTITY_MATRIX,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            display_path = Path(temp_dir) / "tile.tif"
            scene_path = Path(temp_dir) / "tile_scene_linear.tif"
            develop_nef("source.nef", display_path, scene_path, recipe, loader=lambda _path: fake)
            display = cv2.imread(str(display_path), cv2.IMREAD_UNCHANGED)
            scene = cv2.imread(str(scene_path), cv2.IMREAD_UNCHANGED)
        self.assertGreater(float(scene.max()), 1.8)
        self.assertTrue(np.all(display == 65535))

    def test_shared_recipe_preserves_inter_tile_exposure_ratio(self) -> None:
        recipe = RawDevelopmentRecipe(
            2,
            WhiteBalance(1.0, 1.0, 1.0, 1.0),
            1.5,
            0.3,
            0.2,
            NormalizedROI(),
            "/calibration/white_balance.jpg",
            "/calibration/white_balance.nef",
            IDENTITY_MATRIX,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            scenes = []
            for index, value in enumerate((12000, 24000)):
                scene_path = root / f"tile_{index}_scene_linear.tif"
                develop_nef(
                    f"source_{index}.nef",
                    root / f"tile_{index}.tif",
                    scene_path,
                    recipe,
                    loader=lambda _path, value=value: FakeRaw(np.full((4, 6, 3), value, dtype=np.uint16)),
                )
                scenes.append(cv2.imread(str(scene_path), cv2.IMREAD_UNCHANGED))
        np.testing.assert_allclose(scenes[1], scenes[0] * 2.0, rtol=1e-6)

    def test_developer_rejects_asymmetric_raw_margin(self) -> None:
        fake = FakeRaw(np.zeros((13, 20, 3), dtype=np.uint16))
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ValueError, "symmetric"):
                develop_nef(
                    "source.nef",
                    Path(temp_dir) / "tile.tif",
                    Path(temp_dir) / "tile_scene_linear.tif",
                    RawDevelopmentRecipe(
                        2,
                        WhiteBalance(1.0, 1.0, 1.0, 1.0),
                        1.0,
                        0.25,
                        0.25,
                        NormalizedROI(),
                        "/calibration/white_balance.jpg",
                        "/calibration/white_balance.nef",
                        IDENTITY_MATRIX,
                    ),
                    output_size=(18, 12),
                    loader=lambda _path: fake,
                )

    def test_camera_matrix_conversion_preserves_signed_working_values(self) -> None:
        camera_xyz = np.array(
            [
                [0.5958, -0.1559, -0.0571],
                [-0.4021, 1.1453, 0.2939],
                [-0.0634, 0.1548, 0.5087],
                [0.0, 0.0, 0.0],
            ],
            dtype=np.float32,
        )
        matrix = camera_to_rec2020_matrix(camera_xyz)
        np.testing.assert_allclose(
            matrix,
            [
                [1.02296023, 0.13171161, -0.15467183],
                [-0.06881649, 1.62522939, -0.55641289],
                [0.01353933, -0.33251076, 1.31897143],
            ],
            rtol=1e-6,
        )
        fake = FakeRaw(np.full((2, 2, 3), [65535, 0, 0], dtype=np.uint16))
        fake.rgb_xyz_matrix = camera_xyz
        recipe = RawDevelopmentRecipe(
            2,
            WhiteBalance(1.0, 1.0, 1.0, 1.0),
            1.0,
            0.25,
            0.25,
            NormalizedROI(),
            "/calibration/white_balance.jpg",
            "/calibration/white_balance.nef",
            tuple(tuple(float(value) for value in row) for row in matrix),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            scene_path = Path(temp_dir) / "tile_scene_linear.tif"
            develop_nef(
                "source.nef",
                Path(temp_dir) / "tile.tif",
                scene_path,
                recipe,
                loader=lambda _path: fake,
            )
            scene = cv2.imread(str(scene_path), cv2.IMREAD_UNCHANGED)
        self.assertLess(float(scene.min()), 0.0)
        self.assertGreater(float(scene.max()), 1.0)
        np.testing.assert_allclose(scene[0, 0], matrix[:, 0][::-1], rtol=1e-6)

    def test_recipe_rejects_different_raw_engine_versions(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "requires rawpy"):
            RawDevelopmentRecipe(
                2,
                WhiteBalance(1.0, 1.0, 1.0, 1.0),
                1.0,
                0.25,
                0.25,
                NormalizedROI(),
                "/calibration/white_balance.jpg",
                "/calibration/white_balance.nef",
                IDENTITY_MATRIX,
                rawpy_version="different",
            )

    def test_camera_matrix_rejects_unsupported_or_invalid_inputs(self) -> None:
        values = (
            np.zeros((3, 3), dtype=np.float32),
            np.vstack([IDENTITY_CAMERA_XYZ[:3], [1.0, 0.0, 0.0]]),
            np.full((4, 3), np.nan, dtype=np.float32),
            np.zeros((4, 3), dtype=np.float32),
        )
        for value in values:
            with self.subTest(value=value):
                with self.assertRaises((ValueError, np.linalg.LinAlgError)):
                    camera_to_rec2020_matrix(value)


if __name__ == "__main__":
    unittest.main()
