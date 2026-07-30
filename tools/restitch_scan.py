from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path


_TILE_RE = re.compile(
    r"^tile_r(?P<row>\d+)_c(?P<col>\d+)_x(?P<x>-?\d+(?:\.\d+)?)_y(?P<y>-?\d+(?:\.\d+)?)\.(?:tif|png)$"
)


def _manifest_image_roles(tiles: list[dict[str, object]]) -> str:
    if not tiles:
        raise ValueError("Tile manifest is empty")
    if not all(isinstance(tile, dict) for tile in tiles):
        raise ValueError("Every tile manifest entry must be an object")
    raw_complete = [
        bool(str(tile.get("raw_file", "")).strip())
        and bool(str(tile.get("scene_linear_file", "")).strip())
        and bool(str(tile.get("display_file", "")).strip())
        and "composite_file" not in tile
        for tile in tiles
    ]
    raw_fields = {"raw_file", "scene_linear_file", "display_file", "composite_file"}
    raw_marked = [bool(raw_fields.intersection(tile)) for tile in tiles]
    if all(raw_complete):
        return "raw"
    if not any(raw_marked):
        return "single"
    raise ValueError("Tile manifest mixes or omits RAW image roles")


def _infer_tiles_from_dir(scan_dir: str) -> list[dict[str, object]]:
    tiles: list[dict[str, object]] = []
    try:
        with os.scandir(scan_dir) as it:
            for ent in it:
                if not ent.is_file():
                    continue
                m = _TILE_RE.match(ent.name)
                if not m:
                    continue
                try:
                    tiles.append(
                        {
                            "row": int(m.group("row")),
                            "col": int(m.group("col")),
                            "x_mm": float(m.group("x")),
                            "y_mm": float(m.group("y")),
                            "file": str(ent.name),
                        }
                    )
                except Exception:
                    continue
    except Exception:
        pass
    tiles.sort(key=lambda t: (int(t.get("row", 0)), int(t.get("col", 0))))
    return tiles


def main(argv: list[str] | None = None) -> int:
    # Allow running from the repo root without installing as a package.
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    ap = argparse.ArgumentParser(description="Rebuild stitched outputs for an existing scan folder (tiles.json).")
    ap.add_argument("scan_dir", help="Path to a scan folder (contains tiles.json and tile_*.tif/.png).")

    ap.add_argument(
        "--compression",
        choices=["none", "lzw", "deflate"],
        default="lzw",
        help="TIFF compression for stitched outputs (default: lzw).",
    )
    ap.add_argument(
        "--max-panorama-pixels",
        type=int,
        default=None,
        help="Safety cap for stitched mosaic pixels (default: 2000000000). Increase for big scans (risk: huge RAM/disk).",
    )
    ap.add_argument(
        "--final-megapix",
        type=float,
        default=None,
        help="Final resolution per input image (MP). Use -1 for full-res (default: full-res).",
    )
    ap.add_argument(
        "--lossless",
        action="store_true",
        help="Convenience flag: full-res output with no blending or exposure changes.",
    )
    ap.add_argument(
        "--dpi",
        type=float,
        default=None,
        help="Override output DPI/PPI metadata written into mosaic_full.tif (default: auto).",
    )
    ap.add_argument(
        "--dpi-round-px-per-mm",
        type=float,
        default=None,
        help="Round estimated px/mm to this increment before writing DPI (helps match PPI across patches).",
    )
    ap.add_argument(
        "--no-memmap",
        action="store_true",
        help="Disable scratch-file backing for huge mosaics (not recommended).",
    )

    ap.add_argument(
        "--layout-megapix",
        type=float,
        default=None,
        help="Megapix used to estimate step vectors (default: auto).",
    )
    ap.add_argument(
        "--layout-samples",
        type=int,
        default=None,
        help="Number of neighbor pairs to sample per direction for step estimation (default: 250).",
    )
    ap.add_argument(
        "--layout-nfeatures",
        type=int,
        default=None,
        help="ORB features per image for step estimation (default: 2000).",
    )
    ap.add_argument(
        "--layout-orb-fast-threshold",
        type=int,
        default=None,
        help="ORB FAST threshold for step estimation (default: 10).",
    )
    ap.add_argument(
        "--layout-min-inliers",
        type=int,
        default=None,
        help="Min inliers to accept an estimated step (default: 10).",
    )
    ap.add_argument(
        "--layout-blend",
        choices=["overwrite", "average", "feather"],
        default=None,
        help="Compositing mode (default: feather).",
    )
    ap.add_argument(
        "--layout-feather-px",
        type=int,
        default=None,
        help="Feather width in px (final resolution) for feather blending (default: auto).",
    )
    ap.add_argument("--blend-strength", type=float, default=None, help="Blend strength (default: 5).")

    ap.add_argument(
        "--no-layout-exposure",
        action="store_true",
        help="Disable per-tile exposure compensation (default: enabled).",
    )
    ap.add_argument(
        "--layout-gain-min",
        type=float,
        default=None,
        help="Minimum per-tile exposure gain (default: 0.5).",
    )
    ap.add_argument(
        "--layout-gain-max",
        type=float,
        default=None,
        help="Maximum per-tile exposure gain (default: 2.0).",
    )

    ap.add_argument(
        "--no-layout-black-transparent",
        action="store_true",
        help="Disable treating pure-black tile borders as transparent (default: enabled).",
    )
    ap.add_argument(
        "--layout-black-threshold",
        type=int,
        default=None,
        help="Threshold (0–32) for detecting black borders to treat as transparent (default: 2).",
    )

    ap.add_argument(
        "--no-layout-refine",
        action="store_true",
        help="Disable local position refinement (faster, but more visible seams/misalignment).",
    )
    ap.add_argument(
        "--layout-refine-megapix",
        type=float,
        default=None,
        help="Megapix used for refinement (default: auto).",
    )
    ap.add_argument(
        "--layout-refine-patch",
        type=int,
        default=None,
        help="Patch size (px, refine resolution) for phase correlation during refinement (default: 384).",
    )
    ap.add_argument(
        "--layout-refine-resp-thresh",
        type=float,
        default=None,
        help="Min phase correlation response to accept an edge during refinement (default: 0.15).",
    )
    ap.add_argument(
        "--layout-refine-max-correction-px",
        type=float,
        default=None,
        help="Max per-edge correction (px at final resolution) during refinement (default: 25).",
    )
    ap.add_argument(
        "--layout-refine-prior-weight",
        type=float,
        default=None,
        help="Soft prior weight towards the initial grid positions (default: 0.01).",
    )
    ap.add_argument(
        "--layout-refine-max-edges",
        type=int,
        default=None,
        help="Limit number of refined edges (0 = all; default: 0).",
    )
    ap.add_argument(
        "--layout-seed",
        type=int,
        default=None,
        help="RNG seed for layout sampling (default: 0).",
    )

    ap.add_argument("--preview-max-dim", type=int, default=None, help="Preview JPEG max dimension (default: 2000).")
    ap.add_argument("--preview-quality", type=int, default=None, help="Preview JPEG quality (default: 85).")

    ap.add_argument("--tiff-tile", action="store_true", help="Force tiled TIFF output (default: auto).")
    ap.add_argument("--tiff-strip", action="store_true", help="Force strip-based TIFF output (default: auto).")
    ap.add_argument("--tiff-tile-width", type=int, default=None, help="Tile width (px) for tiled TIFFs (default: 256).")
    ap.add_argument("--tiff-tile-height", type=int, default=None, help="Tile height (px) for tiled TIFFs (default: 256).")
    ap.add_argument(
        "--tiff-predictor",
        choices=["none", "horizontal", "float"],
        default=None,
        help="TIFF predictor (default: horizontal).",
    )

    args = ap.parse_args(argv)

    scan_dir = os.path.abspath(os.path.expanduser(str(args.scan_dir)))
    tiles_path = os.path.join(scan_dir, "tiles.json")
    if not os.path.isdir(scan_dir):
        print(f"Error: not a directory: {scan_dir}", file=sys.stderr)
        return 2

    tiles = None
    if os.path.exists(tiles_path):
        try:
            with open(tiles_path, "r", encoding="utf-8") as f:
                tiles = json.load(f)
        except Exception as exc:
            print(f"Error: failed to read tiles.json: {exc}", file=sys.stderr)
            return 2
        if not isinstance(tiles, list) or not tiles:
            print("Error: tiles.json must contain a non-empty array", file=sys.stderr)
            return 2
    else:
        tiles = _infer_tiles_from_dir(scan_dir)
        if not tiles:
            print(f"Error: no tiles found (tiles.json missing and no tile_*.tif files parsed): {scan_dir}", file=sys.stderr)
            return 2
        try:
            with open(tiles_path, "w", encoding="utf-8") as f:
                json.dump(tiles, f, indent=2, sort_keys=False)
        except Exception as exc:
            print(f"Error: failed to write inferred tiles.json: {exc}", file=sys.stderr)
            return 2
    try:
        image_roles = _manifest_image_roles(tiles)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    try:
        from v3se_printer.scan.stitch_outputs import stitch_scan_outputs
        from v3se_printer.scan.stitching.openexr import build_openexr_helper
        from v3se_printer.progress import StepProgressTracker, format_step_progress

        progress_tracker = StepProgressTracker()

        def _progress(phase: str, label: str, completed: int, total: int | None, unit: str) -> None:
            current = progress_tracker.update(phase, label, completed, total, unit)
            print(format_step_progress(current), file=sys.stderr)

        stitch_settings: dict[str, object] = {}
        if args.max_panorama_pixels is not None:
            stitch_settings["max_panorama_pixels"] = int(args.max_panorama_pixels)
        if args.final_megapix is not None:
            stitch_settings["final_megapix"] = float(args.final_megapix)
        if bool(getattr(args, "lossless", False)):
            stitch_settings["final_megapix"] = -1.0
            stitch_settings["layout_blend"] = "overwrite"
            stitch_settings["layout_exposure_compensate"] = False
        if bool(getattr(args, "no_memmap", False)):
            stitch_settings["use_memmap"] = False
        if args.dpi is not None:
            stitch_settings["output_dpi"] = float(args.dpi)
        if args.dpi_round_px_per_mm is not None:
            stitch_settings["dpi_round_px_per_mm"] = float(args.dpi_round_px_per_mm)

        if args.layout_megapix is not None:
            stitch_settings["layout_megapix"] = float(args.layout_megapix)
        if args.layout_samples is not None:
            stitch_settings["layout_samples"] = int(args.layout_samples)
        if args.layout_nfeatures is not None:
            stitch_settings["layout_nfeatures"] = int(args.layout_nfeatures)
        if args.layout_orb_fast_threshold is not None:
            stitch_settings["layout_orb_fast_threshold"] = int(args.layout_orb_fast_threshold)
        if args.layout_min_inliers is not None:
            stitch_settings["layout_min_inliers"] = int(args.layout_min_inliers)
        if args.layout_blend is not None:
            stitch_settings["layout_blend"] = str(args.layout_blend)
        if args.layout_feather_px is not None:
            stitch_settings["layout_feather_px"] = int(args.layout_feather_px)
        if args.blend_strength is not None:
            stitch_settings["blend_strength"] = float(args.blend_strength)

        if bool(getattr(args, "no_layout_exposure", False)):
            stitch_settings["layout_exposure_compensate"] = False
        if args.layout_gain_min is not None:
            stitch_settings["layout_gain_min"] = float(args.layout_gain_min)
        if args.layout_gain_max is not None:
            stitch_settings["layout_gain_max"] = float(args.layout_gain_max)

        if bool(getattr(args, "no_layout_black_transparent", False)):
            stitch_settings["layout_black_transparent"] = False
        if args.layout_black_threshold is not None:
            stitch_settings["layout_black_threshold"] = int(args.layout_black_threshold)

        if args.layout_seed is not None:
            stitch_settings["layout_seed"] = int(args.layout_seed)
        if bool(getattr(args, "no_layout_refine", False)):
            stitch_settings["layout_refine_positions"] = False
        if args.layout_refine_megapix is not None:
            stitch_settings["layout_refine_megapix"] = float(args.layout_refine_megapix)
        if args.layout_refine_patch is not None:
            stitch_settings["layout_refine_patch"] = int(args.layout_refine_patch)
        if args.layout_refine_resp_thresh is not None:
            stitch_settings["layout_refine_resp_thresh"] = float(args.layout_refine_resp_thresh)
        if args.layout_refine_max_correction_px is not None:
            stitch_settings["layout_refine_max_correction_px"] = float(args.layout_refine_max_correction_px)
        if args.layout_refine_prior_weight is not None:
            stitch_settings["layout_refine_prior_weight"] = float(args.layout_refine_prior_weight)
        if args.layout_refine_max_edges is not None:
            stitch_settings["layout_refine_max_edges"] = int(args.layout_refine_max_edges)

        if args.preview_max_dim is not None:
            stitch_settings["preview_max_dim"] = int(args.preview_max_dim)
        if args.preview_quality is not None:
            stitch_settings["preview_quality"] = int(args.preview_quality)

        if bool(getattr(args, "tiff_tile", False)) and bool(getattr(args, "tiff_strip", False)):
            print("Error: pass only one of --tiff-tile / --tiff-strip", file=sys.stderr)
            return 2
        if bool(getattr(args, "tiff_tile", False)):
            stitch_settings["tiff_tile"] = True
        elif bool(getattr(args, "tiff_strip", False)):
            stitch_settings["tiff_tile"] = False
        if args.tiff_tile_width is not None:
            stitch_settings["tiff_tile_width"] = int(args.tiff_tile_width)
        if args.tiff_tile_height is not None:
            stitch_settings["tiff_tile_height"] = int(args.tiff_tile_height)
        if args.tiff_predictor is not None:
            stitch_settings["tiff_predictor"] = str(args.tiff_predictor)

        stitch_args = {
            "tiles": list(tiles) if isinstance(tiles, list) else [],
            "out_dir": str(scan_dir),
            "build_pyramidal_tiff": True,
            "tiff_compression": str(args.compression),
            "image_roles": image_roles,
            "progress_cb": _progress,
            "stitch_settings": stitch_settings or None,
        }
        if image_roles == "raw":
            source = Path(repo_root) / "tools" / "write_openexr.cpp"
            with tempfile.TemporaryDirectory(prefix="marlinscan-openexr-") as build_dir:
                _progress("prepare-openexr", "Preparing OpenEXR writer", 0, 1, "writers")
                helper = build_openexr_helper(
                    source_path=source,
                    output_path=Path(build_dir) / "write_openexr",
                )
                _progress("prepare-openexr", "Preparing OpenEXR writer", 1, 1, "writers")
                stitch_scan_outputs(**stitch_args, openexr_helper=helper)
        else:
            stitch_scan_outputs(**stitch_args)
    except Exception as exc:
        print(f"Stitch failed: {exc}", file=sys.stderr)
        err_path = os.path.join(scan_dir, "stitch_error.txt")
        if os.path.exists(err_path):
            print(f"(See {err_path})", file=sys.stderr)
        return 1

    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
