from __future__ import annotations

import argparse
import json
import os
import re
import sys


_TILE_RE = re.compile(
    r"^tile_r(?P<row>\d+)_c(?P<col>\d+)_x(?P<x>-?\d+(?:\.\d+)?)_y(?P<y>-?\d+(?:\.\d+)?)\.(?:tif|png)$"
)


def _infer_tiles_from_dir(scan_dir: str) -> list[dict[str, object]]:
    tiles: list[dict[str, object]] = []
    try:
        with os.scandir(scan_dir) as it:
            for ent in it:
                if not ent.is_file():
                    continue
                name = ent.name
                m = _TILE_RE.match(name)
                if not m:
                    continue
                try:
                    tiles.append(
                        {
                            "row": int(m.group("row")),
                            "col": int(m.group("col")),
                            "x_mm": float(m.group("x")),
                            "y_mm": float(m.group("y")),
                            "file": str(name),
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
    ap.add_argument("--pyramid", action="store_true", help="Keep stitched TIFF (mosaic_full.tif).")
    ap.add_argument("--crop", action="store_true", help="Crop panorama to largest interior rectangle (default: off).")
    ap.add_argument(
        "--confidence-threshold",
        type=float,
        default=None,
        help="Match confidence threshold for including edges/images (default: auto).",
    )
    ap.add_argument(
        "--range-width",
        type=int,
        default=None,
        help="Limit pairwise matching to neighbors within this index distance (default: auto).",
    )
    ap.add_argument(
        "--max-direct-tiles",
        type=int,
        default=None,
        help="Max tiles to stitch in one pass before switching to layout mode (default: auto).",
    )
    ap.add_argument(
        "--max-panorama-pixels",
        type=int,
        default=None,
        help="Safety cap for stitched mosaic pixels (default: 2000000000). Increase for full-res big scans (risk: huge RAM/disk).",
    )
    ap.add_argument(
        "--final-megapix",
        type=float,
        default=None,
        help="Final resolution per input image (MP). Use -1 for full-res (default: auto).",
    )
    ap.add_argument(
        "--lossless",
        action="store_true",
        help="Convenience flag: try full-res output (sets --final-megapix -1 and disables averaging).",
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
        "--medium-megapix",
        type=float,
        default=None,
        help="Medium resolution per input image (MP; affects features; default: auto).",
    )
    ap.add_argument("--low-megapix", type=float, default=None, help="Low resolution per input image (MP; default: auto).")
    ap.add_argument(
        "--detector",
        choices=["orb", "sift", "brisk", "akaze"],
        default=None,
        help="Feature detector (default: auto).",
    )
    ap.add_argument(
        "--finder",
        choices=["dp_color", "dp_colorgrad", "gc_color", "gc_colorgrad", "voronoi", "no"],
        default=None,
        help="Seam finder (default: auto).",
    )
    ap.add_argument("--nfeatures", type=int, default=None, help="ORB features per image (default: auto).")
    ap.add_argument(
        "--orb-fast-threshold",
        type=int,
        default=None,
        help="ORB FAST threshold (lower finds more keypoints; default: auto).",
    )
    ap.add_argument(
        "--neighbor-match",
        choices=["4", "8"],
        default=None,
        help="Match 4-neighbors (R/D) or 8-neighbors (adds diagonals) for tile grids (default: auto).",
    )
    ap.add_argument(
        "--layout-megapix",
        type=float,
        default=None,
        help="Megapix used to estimate step vectors for large scans (default: auto).",
    )
    ap.add_argument(
        "--layout-samples",
        type=int,
        default=None,
        help="Number of neighbor pairs to sample per direction for layout (default: auto).",
    )
    ap.add_argument(
        "--layout-nfeatures",
        type=int,
        default=None,
        help="ORB features per image for layout step estimation (default: auto).",
    )
    ap.add_argument(
        "--layout-min-inliers",
        type=int,
        default=None,
        help="Min RANSAC inliers to accept a layout step estimate (default: auto).",
    )
    ap.add_argument(
        "--layout-blend",
        choices=["overwrite", "average", "feather"],
        default=None,
        help="Compositing mode for layout stitching (default: overwrite).",
    )
    ap.add_argument(
        "--layout-feather-px",
        type=int,
        default=None,
        help="Feather width in px (final resolution) for --layout-blend feather (default: auto).",
    )
    ap.add_argument(
        "--layout-blend-radius",
        type=int,
        default=None,
        help="Blend against already-placed neighbors within this grid radius in layout mode (default: 2).",
    )
    ap.add_argument(
        "--no-layout-refine",
        action="store_true",
        help="Disable local position refinement in layout mode (faster, but more visible seams/misalignment).",
    )
    ap.add_argument(
        "--layout-refine-megapix",
        type=float,
        default=None,
        help="Megapix used for layout position refinement (default: auto).",
    )
    ap.add_argument(
        "--layout-refine-patch",
        type=int,
        default=None,
        help="Patch size (px, refine resolution) for phase correlation during layout refinement (default: 384).",
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
    ap.add_argument(
        "--blender-type",
        choices=["multiband", "no"],
        default=None,
        help="Blending type (default: auto).",
    )
    ap.add_argument("--blend-strength", type=float, default=None, help="Blending strength (default: auto).")
    ap.add_argument(
        "--compression",
        choices=["none", "lzw", "deflate"],
        default="lzw",
        help="TIFF compression for stitched outputs (default: lzw).",
    )
    ap.add_argument(
        "--tiff-tile",
        action="store_true",
        help="Force tiled TIFF output (default: auto).",
    )
    ap.add_argument(
        "--tiff-strip",
        action="store_true",
        help="Force strip-based TIFF output (default: auto).",
    )
    ap.add_argument(
        "--tiff-tile-width",
        type=int,
        default=None,
        help="Tile width (px) when writing tiled TIFFs (default: 256).",
    )
    ap.add_argument(
        "--tiff-tile-height",
        type=int,
        default=None,
        help="Tile height (px) when writing tiled TIFFs (default: 256).",
    )
    ap.add_argument(
        "--tiff-predictor",
        choices=["none", "horizontal", "float"],
        default=None,
        help="TIFF predictor (default: auto).",
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
            tiles = None
    if not isinstance(tiles, list) or not tiles:
        tiles = _infer_tiles_from_dir(scan_dir)
        if not tiles:
            print(f"Error: no tiles found (tiles.json missing and no tile_*.tif files parsed): {scan_dir}", file=sys.stderr)
            return 2
        # Best-effort: write tiles.json for future runs.
        try:
            with open(tiles_path, "w", encoding="utf-8") as f:
                json.dump(tiles, f, indent=2, sort_keys=False)
        except Exception:
            pass

    build_pyramid = bool(args.pyramid)
    if not build_pyramid:
        build_pyramid = True

    try:
        from v3se_printer.scan.stitch_outputs import stitch_scan_outputs

        def _progress(pct: float, msg: str) -> None:
            print(f"{pct:5.1f}% {msg}", file=sys.stderr)

        stitch_settings: dict[str, object] = {}
        if bool(args.crop):
            stitch_settings["crop"] = True
        if args.confidence_threshold is not None:
            stitch_settings["confidence_threshold"] = float(args.confidence_threshold)
        if args.range_width is not None:
            stitch_settings["range_width"] = int(args.range_width)
        if args.max_direct_tiles is not None:
            stitch_settings["max_direct_tiles"] = int(args.max_direct_tiles)
        if args.max_panorama_pixels is not None:
            stitch_settings["max_panorama_pixels"] = int(args.max_panorama_pixels)
        if args.final_megapix is not None:
            stitch_settings["final_megapix"] = float(args.final_megapix)
        if bool(getattr(args, "lossless", False)):
            stitch_settings["final_megapix"] = -1.0
            stitch_settings["layout_blend"] = "overwrite"
            stitch_settings["blender_type"] = "no"
        if bool(getattr(args, "no_memmap", False)):
            stitch_settings["use_memmap"] = False
        if args.dpi is not None:
            stitch_settings["output_dpi"] = float(args.dpi)
        if args.dpi_round_px_per_mm is not None:
            stitch_settings["dpi_round_px_per_mm"] = float(args.dpi_round_px_per_mm)
        if args.medium_megapix is not None:
            stitch_settings["medium_megapix"] = float(args.medium_megapix)
        if args.low_megapix is not None:
            stitch_settings["low_megapix"] = float(args.low_megapix)
        if args.detector is not None:
            stitch_settings["detector"] = str(args.detector)
        if args.finder is not None:
            stitch_settings["finder"] = str(args.finder)
        if args.nfeatures is not None:
            stitch_settings["nfeatures"] = int(args.nfeatures)
        if args.orb_fast_threshold is not None:
            stitch_settings["orb_fast_threshold"] = int(args.orb_fast_threshold)
        if args.neighbor_match is not None:
            stitch_settings["neighbor_match"] = str(args.neighbor_match)
        if args.layout_megapix is not None:
            stitch_settings["layout_megapix"] = float(args.layout_megapix)
        if args.layout_samples is not None:
            stitch_settings["layout_samples"] = int(args.layout_samples)
        if args.layout_nfeatures is not None:
            stitch_settings["layout_nfeatures"] = int(args.layout_nfeatures)
        if args.layout_min_inliers is not None:
            stitch_settings["layout_min_inliers"] = int(args.layout_min_inliers)
        if args.layout_blend is not None:
            stitch_settings["layout_blend"] = str(args.layout_blend)
        if args.layout_feather_px is not None:
            stitch_settings["layout_feather_px"] = int(args.layout_feather_px)
        if args.layout_blend_radius is not None:
            stitch_settings["layout_blend_radius"] = int(args.layout_blend_radius)
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
        if args.blender_type is not None:
            stitch_settings["blender_type"] = str(args.blender_type)
        if args.blend_strength is not None:
            stitch_settings["blend_strength"] = float(args.blend_strength)
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

        stitch_scan_outputs(
            tiles=list(tiles) if isinstance(tiles, list) else [],
            out_dir=str(scan_dir),
            build_pyramidal_tiff=bool(build_pyramid),
            tiff_compression=str(args.compression),
            progress_cb=_progress,
            stitch_settings=stitch_settings or None,
        )
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
