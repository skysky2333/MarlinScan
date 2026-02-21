from __future__ import annotations

import json
import math
import os
import shutil
import time
import traceback
from typing import Callable

from .io import imwrite, is_no_space_error


def _tiff_imwrite_params(cv2: object, *, compression: str) -> list[int]:
    if not hasattr(cv2, "IMWRITE_TIFF_COMPRESSION"):
        return []
    comp = (compression or "none").strip().lower()
    code = 1  # none
    if comp == "lzw":
        code = 5
    elif comp == "deflate":
        code = 8
    return [int(getattr(cv2, "IMWRITE_TIFF_COMPRESSION")), int(code)]


def stitch_scan_outputs(
    *,
    tiles: list[dict[str, object]],
    out_dir: str,
    build_pyramidal_tiff: bool,
    build_deepzoom: bool,
    tiff_compression: str,
    deepzoom_tile_px: int,
    deepzoom_format: str,
    deepzoom_jpeg_quality: int,
    progress_cb: Callable[[float, str], None] | None = None,
    stitch_settings: dict[str, object] | None = None,
) -> None:
    """
    Affine (feature-based) stitcher using the `stitching` package (OpenCV stitching pipeline).

    Produces:
    - mosaic_full.tif (optional)
    - deepzoom/ (optional): mosaic.dzi + mosaic_files/ + manifest.json
    - stitch_meta.json (always when stitching runs)
    """
    if not bool(build_pyramidal_tiff) and not bool(build_deepzoom):
        return

    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
        from stitching import AffineStitcher  # type: ignore
    except Exception as exc:
        raise RuntimeError(
            "Missing dependency for stitching. Install: `python -m pip install opencv-python stitching` "
            f"(original error: {exc})"
        ) from exc

    try:
        if hasattr(cv2, "ocl") and hasattr(cv2.ocl, "setUseOpenCL"):
            cv2.ocl.setUseOpenCL(False)
    except Exception:
        pass

    dz_tile_px = max(128, min(4096, int(deepzoom_tile_px)))
    dz_fmt = (deepzoom_format or "jpg").strip().lower()
    if dz_fmt == "jpeg":
        dz_fmt = "jpg"
    if dz_fmt not in {"jpg", "png"}:
        dz_fmt = "jpg"
    dz_jpeg_q = max(1, min(100, int(deepzoom_jpeg_quality)))

    last_emit_ts = 0.0
    last_emit_pct = -1.0

    def _emit_progress(pct: float, msg: str) -> None:
        nonlocal last_emit_ts, last_emit_pct
        if progress_cb is None:
            return
        pct_f = max(0.0, min(100.0, float(pct)))
        now = time.monotonic()
        if pct_f in {0.0, 100.0} or (pct_f - float(last_emit_pct)) >= 0.5 or (now - float(last_emit_ts)) >= 0.5:
            last_emit_pct = float(pct_f)
            last_emit_ts = float(now)
            try:
                progress_cb(float(pct_f), str(msg))
            except Exception:
                pass

    def _write_err(stage: str, exc: Exception) -> None:
        try:
            with open(os.path.join(out_dir, "stitch_error.txt"), "a", encoding="utf-8") as f:
                f.write(f"[{stage}] {exc}\n")
                f.write(traceback.format_exc())
                f.write("\n\n")
        except Exception:
            pass

    def _cleanup_dir(path: str) -> None:
        if os.path.isdir(path):
            try:
                shutil.rmtree(path, ignore_errors=True)
            except Exception:
                pass

    def _dz_write_from_array(
        img: "np.ndarray",
        *,
        dz_base: str,
        tile_size: int,
        fmt: str,
        jpeg_quality: int,
        png_compression: int = 3,
        progress: Callable[[int], None] | None = None,
    ) -> int:
        h0, w0 = img.shape[:2]
        w0 = int(w0)
        h0 = int(h0)
        if w0 <= 0 or h0 <= 0:
            raise RuntimeError("Invalid image size for DeepZoom.")
        max_dim = max(w0, h0)
        max_level = int(math.ceil(math.log(max_dim, 2))) if max_dim > 1 else 0

        dzi_path = dz_base + ".dzi"
        files_root = dz_base + "_files"
        os.makedirs(files_root, exist_ok=True)

        fmt_norm = (fmt or "jpg").strip().lower()
        if fmt_norm == "jpeg":
            fmt_norm = "jpg"
        if fmt_norm not in {"jpg", "png"}:
            fmt_norm = "jpg"

        dzi = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Image TileSize="{ts}" Overlap="0" Format="{fmt}" '
            'xmlns="http://schemas.microsoft.com/deepzoom/2008">\n'
            '  <Size Width="{w}" Height="{h}"/>\n'
            "</Image>\n"
        ).format(ts=int(tile_size), fmt=str(fmt_norm), w=int(w0), h=int(h0))
        with open(dzi_path, "w", encoding="utf-8") as f:
            f.write(dzi)

        total_tiles = 0
        cur = img
        for level in range(int(max_level), -1, -1):
            ch, cw = cur.shape[:2]
            level_dir = os.path.join(files_root, str(level))
            os.makedirs(level_dir, exist_ok=True)
            tiles_x = int((int(cw) + int(tile_size) - 1) // int(tile_size))
            tiles_y = int((int(ch) + int(tile_size) - 1) // int(tile_size))
            if fmt_norm == "jpg" and hasattr(cv2, "IMWRITE_JPEG_QUALITY"):
                params_write = [int(cv2.IMWRITE_JPEG_QUALITY), max(1, min(100, int(jpeg_quality)))]
            elif fmt_norm == "png" and hasattr(cv2, "IMWRITE_PNG_COMPRESSION"):
                params_write = [int(cv2.IMWRITE_PNG_COMPRESSION), max(0, min(9, int(png_compression)))]
            else:
                params_write = []
            for ty in range(int(tiles_y)):
                y0 = int(ty) * int(tile_size)
                y1 = min(int(ch), int(y0) + int(tile_size))
                for tx in range(int(tiles_x)):
                    x0 = int(tx) * int(tile_size)
                    x1 = min(int(cw), int(x0) + int(tile_size))
                    tile = cur[int(y0) : int(y1), int(x0) : int(x1)]
                    out_path = os.path.join(level_dir, f"{tx}_{ty}.{fmt_norm}")
                    imwrite(cv2, out_path, tile, params_write)
                    total_tiles += 1
                    if progress is not None:
                        progress(1)
            if level > 0:
                new_w = max(1, (int(cw) + 1) // 2)
                new_h = max(1, (int(ch) + 1) // 2)
                cur = cv2.resize(cur, (int(new_w), int(new_h)), interpolation=cv2.INTER_AREA)
        return int(total_tiles)

    # Clear stale outputs.
    try:
        for fn in ("stitch_error.txt", "stitch_meta.json", "mosaic_full.tif"):
            p = os.path.join(out_dir, fn)
            if os.path.exists(p):
                os.remove(p)
    except Exception:
        pass
    dz_root = os.path.join(out_dir, "deepzoom")
    _cleanup_dir(dz_root)

    # Parse tiles.
    entries: list[tuple[int, int, str]] = []
    max_r = -1
    max_c = -1
    for t in tiles:
        try:
            r = int(t["row"])
            c = int(t["col"])
            fn = str(t["file"])
        except Exception:
            continue
        p = os.path.join(out_dir, fn)
        if not os.path.exists(p):
            continue
        entries.append((int(r), int(c), str(p)))
        max_r = max(int(max_r), int(r))
        max_c = max(int(max_c), int(c))

    if not entries:
        return

    entries.sort(key=lambda x: (int(x[0]), int(x[1])))
    image_paths = [p for (_r, _c, p) in entries]

    nrows = int(max_r) + 1
    ncols = int(max_c) + 1

    tiff_params = _tiff_imwrite_params(cv2, compression=str(tiff_compression))

    # Defaults tuned for low-contrast scan tiles:
    # - ORB with a lower FAST threshold yields far more keypoints (stitching's default is often too strict).
    # - Match only grid neighbors to avoid O(N^2) matching for large scans.
    settings: dict[str, object] = {
        "crop": False,  # scanning: preserve full stitched plane
        "confidence_threshold": 0.15,
        "detector": "orb",
        "nfeatures": 2000,
        "match_conf": 0.3,
        "blender_type": "multiband",
        "blend_strength": 5,
    }
    local_settings: dict[str, object] = {
        "orb_fast_threshold": 5,
        "neighbor_match": "4",  # "4" or "8"
    }
    if isinstance(stitch_settings, dict):
        for k, v in stitch_settings.items():
            if k in {"orb_fast_threshold", "neighbor_match"}:
                local_settings[k] = v
            else:
                settings[k] = v

    warnings_txt: str | None = None
    match_mask_pairs: int | None = None
    tiles_used: int | None = None
    _emit_progress(0.0, "Stitching: affine feature stitch…")
    try:
        import warnings

        with warnings.catch_warnings(record=True) as rec:
            warnings.simplefilter("always")
            from stitching.images import Images  # type: ignore

            stitcher = AffineStitcher(**settings)

            # Override ORB params (stitching's API only exposes nfeatures).
            if str(settings.get("detector") or "orb").strip().lower() == "orb":
                try:
                    fast_thr = int(local_settings.get("orb_fast_threshold", 5))
                except Exception:
                    fast_thr = 5
                fast_thr = max(0, min(100, int(fast_thr)))
                try:
                    stitcher.detector.detector = cv2.ORB_create(
                        nfeatures=int(stitcher.settings.get("nfeatures", 2000)),
                        fastThreshold=int(fast_thr),
                    )
                except Exception:
                    # If ORB override fails, keep default detector.
                    pass

            # Restrict pairwise matching to grid neighbors (massive speedup for tile scans).
            match_mask: "np.ndarray | None" = None
            try:
                n = int(len(image_paths))
                if n >= 2:
                    rc_to_idx = {(int(r), int(c)): i for i, (r, c, _p) in enumerate(entries)}
                    m = np.zeros((int(n), int(n)), np.uint8)

                    neigh = str(local_settings.get("neighbor_match", "8")).strip()
                    use_diag = neigh != "4"

                    for (r, c), i in rc_to_idx.items():
                        for dr, dc in ((0, 1), (1, 0)):
                            j = rc_to_idx.get((int(r + dr), int(c + dc)))
                            if j is not None:
                                m[int(i), int(j)] = 1
                                m[int(j), int(i)] = 1
                        if use_diag:
                            for dr, dc in ((1, 1), (1, -1)):
                                j = rc_to_idx.get((int(r + dr), int(c + dc)))
                                if j is not None:
                                    m[int(i), int(j)] = 1
                                    m[int(j), int(i)] = 1

                    if int(m.sum()) > 0:
                        match_mask = m
                        match_mask_pairs = int(int(m.sum()) // 2)
            except Exception:
                match_mask = None

            stitcher.images = Images.of(
                image_paths, stitcher.medium_megapix, stitcher.low_megapix, stitcher.final_megapix
            )

            _emit_progress(5.0, "Stitching: resize (medium)…")
            imgs = stitcher.resize_medium_resolution()

            _emit_progress(12.0, "Stitching: detect features…")
            features = stitcher.find_features(imgs)

            _emit_progress(28.0, "Stitching: match features…")
            if match_mask is not None:
                matches = stitcher.matcher.match_features(features, match_mask)
            else:
                matches = stitcher.match_features(features)

            _emit_progress(35.0, "Stitching: subset…")
            imgs, features, matches = stitcher.subset(imgs, features, matches)
            try:
                tiles_used = int(len(getattr(stitcher, "images").names))
            except Exception:
                tiles_used = None

            _emit_progress(45.0, "Stitching: estimate cameras…")
            cameras = stitcher.estimate_camera_parameters(features, matches)

            _emit_progress(50.0, "Stitching: adjust cameras…")
            cameras = stitcher.refine_camera_parameters(features, matches, cameras)
            cameras = stitcher.perform_wave_correction(cameras)
            stitcher.estimate_scale(cameras)

            _emit_progress(58.0, "Stitching: warp (low)…")
            imgs = stitcher.resize_low_resolution(imgs)
            imgs, masks, corners, sizes = stitcher.warp_low_resolution(imgs, cameras)

            stitcher.prepare_cropper(imgs, masks, corners, sizes)
            imgs, masks, corners, sizes = stitcher.crop_low_resolution(imgs, masks, corners, sizes)

            _emit_progress(62.0, "Stitching: seams…")
            stitcher.estimate_exposure_errors(corners, imgs, masks)
            seam_masks = stitcher.find_seam_masks(imgs, corners, masks)

            _emit_progress(70.0, "Stitching: warp (final)…")
            imgs = stitcher.resize_final_resolution()
            imgs, masks, corners, sizes = stitcher.warp_final_resolution(imgs, cameras)
            imgs, masks, corners, sizes = stitcher.crop_final_resolution(imgs, masks, corners, sizes)

            stitcher.set_masks(masks)
            imgs = stitcher.compensate_exposure_errors(corners, imgs)
            seam_masks = stitcher.resize_seam_masks(seam_masks)

            _emit_progress(74.0, "Stitching: blend…")
            stitcher.initialize_composition(corners, sizes)
            stitcher.blend_images(imgs, seam_masks, corners)
            panorama = stitcher.create_final_panorama()

            if rec:
                warnings_txt = "\n".join(str(w.message) for w in rec if getattr(w, "message", None) is not None)
    except Exception as exc:
        _write_err("affine_stitch", exc)
        raise

    if panorama is None:
        exc = RuntimeError("Stitching failed: stitcher returned None.")
        _write_err("affine_stitch", exc)
        raise exc

    try:
        H, W = panorama.shape[:2]
    except Exception as exc:
        _write_err("affine_stitch", exc)
        raise RuntimeError("Stitching returned an invalid panorama array.") from exc

    mosaic_path = os.path.join(out_dir, "mosaic_full.tif")
    if bool(build_pyramidal_tiff):
        _emit_progress(75.0, "Stitching: writing mosaic_full.tif…")
        try:
            imwrite(cv2, mosaic_path, panorama, tiff_params)
        except Exception as exc:
            _write_err("final_write", exc)
            if is_no_space_error(exc):
                raise RuntimeError(
                    "No space left on device while writing mosaic_full.tif. Free disk space or choose a different output folder."
                ) from exc
            raise

    meta: dict[str, object] = {
        "method": "openstitching-affine",
        "tiles": int(len(image_paths)),
        "tiles_used": int(tiles_used) if tiles_used is not None else None,
        "rows": int(nrows),
        "cols": int(ncols),
        "tile_paths_sorted": True,
        "match_mask_pairs": int(match_mask_pairs) if match_mask_pairs is not None else None,
        "mosaic_size_px": [int(W), int(H)],
        "settings": dict(settings),
        "local_settings": dict(local_settings),
        "versions": {
            "opencv": str(getattr(cv2, "__version__", "?")),
        },
    }
    try:
        import stitching as stitching_pkg  # type: ignore

        meta["versions"]["stitching"] = str(getattr(stitching_pkg, "__version__", "?"))
    except Exception:
        pass
    if warnings_txt:
        meta["warnings"] = str(warnings_txt)
    try:
        with open(os.path.join(out_dir, "stitch_meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, sort_keys=False)
    except Exception:
        pass

    if bool(build_deepzoom):
        _emit_progress(82.0, "Stitching: DeepZoom…")
        os.makedirs(dz_root, exist_ok=True)

        def _dz_tiles_count(w: int, h: int, tile_size: int) -> int:
            if w <= 0 or h <= 0:
                return 0
            max_dim = max(int(w), int(h))
            max_level = int(math.ceil(math.log(max_dim, 2))) if max_dim > 1 else 0
            total = 0
            cw = int(w)
            ch = int(h)
            for _level in range(int(max_level), -1, -1):
                total += int((cw + int(tile_size) - 1) // int(tile_size)) * int(
                    (ch + int(tile_size) - 1) // int(tile_size)
                )
                cw = max(1, (int(cw) + 1) // 2)
                ch = max(1, (int(ch) + 1) // 2)
            return int(total)

        total_tiles = max(1, _dz_tiles_count(int(W), int(H), int(dz_tile_px)))
        written_tiles = 0

        def _tile_progress(n: int) -> None:
            nonlocal written_tiles
            written_tiles += int(n)
            frac = float(written_tiles) / float(total_tiles)
            _emit_progress(82.0 + (18.0 * float(frac)), f"Stitching: DeepZoom tiles {written_tiles}/{total_tiles}")

        dz_base = os.path.join(dz_root, "mosaic")
        try:
            _dz_write_from_array(
                panorama,
                dz_base=str(dz_base),
                tile_size=int(dz_tile_px),
                fmt=str(dz_fmt),
                jpeg_quality=int(dz_jpeg_q),
                progress=_tile_progress,
            )
        except Exception as exc:
            _write_err("deepzoom", exc)
            if is_no_space_error(exc):
                raise RuntimeError(
                    "No space left on device while writing DeepZoom tiles. Free disk space or choose a different output folder."
                ) from exc
            raise

        manifest: dict[str, object] = {
            "version": 1,
            "size": [int(W), int(H)],
            "chunks": [{"dzi": "mosaic.dzi", "x": 0, "y": 0, "w": int(W), "h": int(H)}],
        }
        with open(os.path.join(dz_root, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, sort_keys=False)

    _emit_progress(100.0, "Stitching: done.")
