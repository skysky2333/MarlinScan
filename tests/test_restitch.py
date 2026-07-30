from __future__ import annotations

from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.restitch_scan import _manifest_image_roles, main


class RestitchManifestTests(unittest.TestCase):
    def test_classifies_complete_raw_and_legacy_manifests(self) -> None:
        self.assertEqual(_manifest_image_roles([{"file": "tile.tif"}]), "single")
        self.assertEqual(
            _manifest_image_roles(
                [
                    {
                        "file": "tile.jpg",
                        "raw_file": "tile.nef",
                        "scene_linear_file": "tile_scene_linear.tif",
                        "display_file": "tile.tif",
                    }
                ]
            ),
            "raw",
        )

    def test_rejects_partial_or_mixed_raw_roles(self) -> None:
        cases = (
            [{"file": "tile.jpg", "composite_file": "tile.tif"}],
            [
                {
                    "file": "tile.jpg",
                    "raw_file": "tile.nef",
                    "scene_linear_file": "tile_scene_linear.tif",
                    "display_file": "tile.tif",
                    "composite_file": "tile.tif",
                }
            ],
            [
                {
                    "file": "a.jpg",
                    "raw_file": "a.nef",
                    "scene_linear_file": "a_scene_linear.tif",
                    "display_file": "a.tif",
                },
                {"file": "b.tif"},
            ],
        )
        for tiles in cases:
            with self.subTest(tiles=tiles), self.assertRaisesRegex(ValueError, "RAW image roles"):
                _manifest_image_roles(tiles)

    def test_rejects_empty_manifest(self) -> None:
        with self.assertRaisesRegex(ValueError, "empty"):
            _manifest_image_roles([])

    def test_existing_malformed_manifest_is_not_replaced_by_inference(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            scan_dir = Path(temp_dir)
            manifest = scan_dir / "tiles.json"
            manifest.write_text("{invalid", encoding="utf-8")
            (scan_dir / "tile_r000_c000_x0.00_y0.00.tif").write_bytes(b"tile")
            stderr = io.StringIO()

            with redirect_stderr(stderr):
                result = main([str(scan_dir)])

            self.assertEqual(result, 2)
            self.assertEqual(manifest.read_text(encoding="utf-8"), "{invalid")
            self.assertIn("failed to read tiles.json", stderr.getvalue())

    def test_existing_non_array_manifest_is_not_replaced_by_inference(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            scan_dir = Path(temp_dir)
            manifest = scan_dir / "tiles.json"
            manifest.write_text(json.dumps({"row": 0}), encoding="utf-8")
            stderr = io.StringIO()

            with redirect_stderr(stderr):
                result = main([str(scan_dir)])

            self.assertEqual(result, 2)
            self.assertEqual(json.loads(manifest.read_text(encoding="utf-8")), {"row": 0})
            self.assertIn("non-empty array", stderr.getvalue())

    def test_inferred_manifest_write_failure_stops_before_stitching(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            scan_dir = Path(temp_dir)
            (scan_dir / "tile_r000_c000_x0.00_y0.00.tif").write_bytes(b"tile")
            stderr = io.StringIO()

            with patch("builtins.open", side_effect=OSError("read-only")), redirect_stderr(stderr):
                result = main([str(scan_dir)])

            self.assertEqual(result, 2)
            self.assertIn("failed to write inferred tiles.json", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
