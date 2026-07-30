from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np  # type: ignore

from v3se_printer.scan.stitching.openexr import build_openexr_helper, write_scene_linear_exr


class OpenExrWriterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.build_dir = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.build_dir.name) / "write_openexr"
        cls.vips = shutil.which("vips")
        if cls.vips is None:
            raise RuntimeError("vips is required to verify OpenEXR pixels")
        repo_root = Path(__file__).resolve().parents[1]
        build_openexr_helper(
            source_path=repo_root / "tools" / "write_openexr.cpp",
            output_path=cls.binary,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.build_dir.cleanup()

    def test_writes_lossless_tiled_scene_linear_rgb(self) -> None:
        exrheader = shutil.which("exrheader")
        self.assertIsNotNone(exrheader)
        rgb = np.array(
            [
                [[-0.125, 0.0, 0.5], [0.25, 1.0, 2.0], [4.0, 0.75, 0.125]],
                [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6], [0.7, 0.8, 0.9]],
            ],
            dtype=np.float32,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "linear_rgb.f32"
            output_path = Path(temp_dir) / "linear_rgb.exr"
            rgb.tofile(input_path)
            write_scene_linear_exr(
                helper_path=self.binary,
                backing_path=input_path,
                output_path=output_path,
                shape=rgb.shape,
                dtype=rgb.dtype,
                tile_size=16,
            )

            decoded_path = Path(temp_dir) / "decoded.f32"
            subprocess.run(
                [str(self.vips), "rawsave", str(output_path), str(decoded_path)],
                check=True,
                capture_output=True,
            )
            decoded = np.fromfile(decoded_path, dtype=np.float32).reshape(2, 3, 4)
            actual = decoded[:, :, :3]
            header = subprocess.run(
                [str(exrheader), str(output_path)],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            np.testing.assert_allclose(actual, rgb, rtol=5e-4, atol=5e-5)
            np.testing.assert_array_equal(decoded[:, :, 3], np.ones((2, 3), dtype=np.float32))
            self.assertIn("R, 32-bit floating-point", header)
            self.assertIn("G, 32-bit floating-point", header)
            self.assertIn("B, 32-bit floating-point", header)
            self.assertIn(
                "compression (type compression): zip: zlib compression, in blocks of 16 scan lines.",
                header,
            )
            self.assertIn("tiles (type tiledesc):", header)
            self.assertIn("tile size 16 by 16 pixels", header)
            self.assertIn('marlinScanColorEncoding (type string): "scene-linear"', header)
            self.assertIn('marlinScanWorkingSpace (type string): "Linear Rec.2020 (D65)"', header)
            self.assertIn("red   (0.708 0.292)", header)
            self.assertIn("white (0.3127 0.329)", header)

    def test_supports_piz_acescg_and_bgr_input(self) -> None:
        exrheader = shutil.which("exrheader")
        self.assertIsNotNone(exrheader)
        bgr = np.array([[[0.75, 0.5, 0.25]]], dtype=np.float32)
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "linear_bgr.f32"
            output_path = Path(temp_dir) / "linear_bgr.exr"
            bgr.tofile(input_path)
            write_scene_linear_exr(
                helper_path=self.binary,
                backing_path=input_path,
                output_path=output_path,
                shape=bgr.shape,
                dtype=bgr.dtype,
                tile_size=16,
                compression="piz",
                working_space="acescg",
                input_order="bgr",
            )
            decoded_path = Path(temp_dir) / "decoded.f32"
            subprocess.run(
                [str(self.vips), "rawsave", str(output_path), str(decoded_path)],
                check=True,
                capture_output=True,
            )
            actual = np.fromfile(decoded_path, dtype=np.float32).reshape(1, 1, 4)[:, :, :3]
            header = subprocess.run(
                [str(exrheader), str(output_path)],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            np.testing.assert_array_equal(actual, bgr[:, :, ::-1])
            self.assertIn("compression (type compression): piz: piz-based wavelet compression", header)
            self.assertIn('marlinScanWorkingSpace (type string): "ACEScg (AP1, D60)"', header)
            self.assertIn("red   (0.713 0.293)", header)

    def test_marks_edited_pixels_as_working_linear(self) -> None:
        exrheader = shutil.which("exrheader")
        self.assertIsNotNone(exrheader)
        rgb = np.zeros((1, 1, 3), dtype=np.float32)
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "edited.f32"
            output_path = Path(temp_dir) / "edited.exr"
            rgb.tofile(input_path)
            write_scene_linear_exr(
                helper_path=self.binary,
                backing_path=input_path,
                output_path=output_path,
                shape=rgb.shape,
                dtype=rgb.dtype,
                tile_size=16,
                color_encoding="working-linear",
            )

            header = subprocess.run(
                [str(exrheader), str(output_path)],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertIn('marlinScanColorEncoding (type string): "working-linear"', header)

    def test_reports_exact_tile_row_progress(self) -> None:
        rgb = np.zeros((35, 17, 3), dtype=np.float32)
        progress: list[tuple[str, str, int, int | None, str]] = []
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "progress.f32"
            output_path = Path(temp_dir) / "progress.exr"
            rgb.tofile(input_path)
            write_scene_linear_exr(
                helper_path=self.binary,
                backing_path=input_path,
                output_path=output_path,
                shape=rgb.shape,
                dtype=rgb.dtype,
                tile_size=16,
                progress_cb=lambda phase, label, completed, total, unit: progress.append(
                    (phase, label, completed, total, unit)
                ),
            )

            expected_prefix = ("write-exr", "Writing scene-linear OpenEXR")
            self.assertEqual(
                progress,
                [
                    (*expected_prefix, 0, 3, "tile rows"),
                    (*expected_prefix, 1, 3, "tile rows"),
                    (*expected_prefix, 2, 3, "tile rows"),
                    (*expected_prefix, 3, 3, "tile rows"),
                ],
            )
            self.assertTrue(output_path.is_file())
            self.assertFalse(Path(f"{output_path}.partial").exists())

    def test_cancellation_removes_partial_and_final_output(self) -> None:
        rgb = np.zeros((64, 64, 3), dtype=np.float32)
        checks = 0

        def cancel() -> None:
            nonlocal checks
            checks += 1
            if checks == 2:
                raise InterruptedError("cancelled")

        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "cancel.f32"
            output_path = Path(temp_dir) / "cancel.exr"
            rgb.tofile(input_path)
            with self.assertRaisesRegex(InterruptedError, "cancelled"):
                write_scene_linear_exr(
                    helper_path=self.binary,
                    backing_path=input_path,
                    output_path=output_path,
                    shape=rgb.shape,
                    dtype=rgb.dtype,
                    tile_size=16,
                    cancel_cb=cancel,
                )

            self.assertEqual(checks, 2)
            self.assertFalse(output_path.exists())
            self.assertFalse(Path(f"{output_path}.partial").exists())

    def test_rejects_wrong_backing_size_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "short.f32"
            output_path = Path(temp_dir) / "short.exr"
            np.zeros((1, 1, 3), dtype=np.float32).tofile(input_path)
            result = subprocess.run(
                [
                    str(self.binary),
                    "--input",
                    str(input_path),
                    "--output",
                    str(output_path),
                    "--width",
                    "2",
                    "--height",
                    "1",
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("size does not match", result.stderr)
            self.assertFalse(output_path.exists())
            self.assertFalse(Path(f"{output_path}.partial").exists())

    def test_wrapper_rejects_dtype_and_shape_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            backing_path = Path(temp_dir) / "backing.f32"
            output_path = Path(temp_dir) / "backing.exr"
            np.zeros((1, 1, 3), dtype=np.float32).tofile(backing_path)
            with self.assertRaisesRegex(ValueError, "dtype must be float32"):
                write_scene_linear_exr(
                    helper_path=self.binary,
                    backing_path=backing_path,
                    output_path=output_path,
                    shape=(1, 1, 3),
                    dtype=np.uint16,
                )
            with self.assertRaisesRegex(ValueError, "shape must be"):
                write_scene_linear_exr(
                    helper_path=self.binary,
                    backing_path=backing_path,
                    output_path=output_path,
                    shape=(1, 3, 1),
                    dtype=np.float32,
                )

    def test_wrapper_preserves_preexisting_partial_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            backing_path = Path(temp_dir) / "backing.f32"
            output_path = Path(temp_dir) / "backing.exr"
            partial_path = Path(f"{output_path}.partial")
            np.zeros((1, 1, 3), dtype=np.float32).tofile(backing_path)
            partial_path.write_bytes(b"existing")

            with self.assertRaisesRegex(ValueError, "partial output already exists"):
                write_scene_linear_exr(
                    helper_path=self.binary,
                    backing_path=backing_path,
                    output_path=output_path,
                    shape=(1, 1, 3),
                    dtype=np.float32,
                )

            self.assertEqual(partial_path.read_bytes(), b"existing")
            self.assertFalse(output_path.exists())


if __name__ == "__main__":
    unittest.main()
