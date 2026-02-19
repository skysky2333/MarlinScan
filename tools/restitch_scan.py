from __future__ import annotations

import argparse
import json
import os
import sys


def main(argv: list[str] | None = None) -> int:
    # Allow running from the repo root without installing as a package.
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    ap = argparse.ArgumentParser(description="Rebuild stitched outputs for an existing scan folder (tiles.json).")
    ap.add_argument("scan_dir", help="Path to a scan folder (contains tiles.json and tile_*.tif).")
    ap.add_argument("--pyramid", action="store_true", help="Build pyramidal BigTIFF (mosaic_pyramid.tif).")
    ap.add_argument("--deepzoom", action="store_true", help="Build DeepZoom tiles (deepzoom/mosaic.dzi + files).")
    ap.add_argument("--tile-px", type=int, default=512, help="Internal TIFF tile size in pixels (default: 512).")
    ap.add_argument(
        "--compression",
        choices=["none", "lzw", "deflate"],
        default="none",
        help="TIFF compression for pyramidal output (default: none).",
    )
    ap.add_argument(
        "--method",
        choices=["bed", "opencv"],
        default="bed",
        help="Stitching method (default: bed). 'opencv' uses the stitching package and can be slow/experimental.",
    )
    ap.add_argument("--downsample", type=int, default=1, help="Stitch downsample factor (default: 1 = full-res).")
    args = ap.parse_args(argv)

    scan_dir = os.path.abspath(os.path.expanduser(str(args.scan_dir)))
    tiles_path = os.path.join(scan_dir, "tiles.json")
    if not os.path.isdir(scan_dir):
        print(f"Error: not a directory: {scan_dir}", file=sys.stderr)
        return 2
    if not os.path.exists(tiles_path):
        print(f"Error: tiles.json not found: {tiles_path}", file=sys.stderr)
        return 2

    try:
        with open(tiles_path, "r", encoding="utf-8") as f:
            tiles = json.load(f)
    except Exception as exc:
        print(f"Error: failed to read tiles.json: {exc}", file=sys.stderr)
        return 2

    build_pyramid = bool(args.pyramid)
    build_deepzoom = bool(args.deepzoom)
    if not build_pyramid and not build_deepzoom:
        # Default to both when no explicit output was requested.
        build_pyramid = True
        build_deepzoom = True

    try:
        from v3se_printer.ui.scan import ScanTabMixin

        # This stitching helper does not depend on Tk state; call it as an unbound method.
        ScanTabMixin._scan_stitch_outputs(  # type: ignore[misc]
            None,
            tiles=list(tiles) if isinstance(tiles, list) else [],
            out_dir=str(scan_dir),
            downsample=int(args.downsample),
            build_pyramidal_tiff=bool(build_pyramid),
            build_deepzoom=bool(build_deepzoom),
            pyramid_tile_px=int(args.tile_px),
            tiff_compression=str(args.compression),
            stitch_method=str(args.method),
        )
        try:
            meta_path = os.path.join(scan_dir, "stitch_meta.json")
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            if (
                str(meta.get("requested_method", "")).strip().lower() == "opencv"
                and str(meta.get("method", "")).strip().lower() == "bed"
            ):
                print("Note: OpenCV stitch failed; used bed fallback (see stitch_error.txt).")
        except Exception:
            pass
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
