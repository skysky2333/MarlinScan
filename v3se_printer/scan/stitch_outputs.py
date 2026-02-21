from __future__ import annotations

import gc
import json
import math
import os
import shutil
import time
import traceback
from dataclasses import dataclass
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


@dataclass(frozen=True)
class _Entry:
    row: int
    col: int
    path: str


def stitch_scan_outputs(
    *,
    tiles: list[dict[str, object]],
    out_dir: str,
    build_pyramidal_tiff: bool,
    tiff_compression: str,
    progress_cb: Callable[[float, str], None] | None = None,
    stitch_settings: dict[str, object] | None = None,
) -> None:
    """
    Affine stitcher for scan tiles.

    Strategy:
    - Small/medium scans: use OpenStitching's `AffineStitcher` (OpenCV stitching pipeline).
    - Large scans: estimate a global affine grid layout from sampled neighbor-to-neighbor affine shifts,
      then composite all tiles into a single canvas. This avoids global bundle-adjustment instability
      and large intermediate allocations.

    Produces:
    - mosaic_full.tif (optional; name kept for compatibility)
    - stitch_meta.json (always when stitching runs)
    """
    if not bool(build_pyramidal_tiff):
        return

    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
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

    # Clear stale outputs.
    try:
        for fn in ("stitch_error.txt", "stitch_meta.json", "mosaic_full.tif"):
            p = os.path.join(out_dir, fn)
            if os.path.exists(p):
                os.remove(p)
    except Exception:
        pass
    _cleanup_dir(os.path.join(out_dir, "_stitch_strips"))
    _cleanup_dir(os.path.join(out_dir, "deepzoom"))

    # Parse tiles.
    entries: list[_Entry] = []
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
        entries.append(_Entry(int(r), int(c), str(p)))
        max_r = max(int(max_r), int(r))
        max_c = max(int(max_c), int(c))

    if not entries:
        return

    entries.sort(key=lambda e: (int(e.row), int(e.col)))
    by_rc: dict[tuple[int, int], str] = {(int(e.row), int(e.col)): str(e.path) for e in entries}

    nrows = int(max_r) + 1
    ncols = int(max_c) + 1

    im0 = cv2.imread(str(entries[0].path), cv2.IMREAD_COLOR)
    if im0 is None:
        raise RuntimeError("Failed to read a tile image.")
    tile_h, tile_w = im0.shape[:2]
    if int(tile_w) <= 0 or int(tile_h) <= 0:
        raise RuntimeError("Invalid tile size.")
    orig_mp = (float(tile_w) * float(tile_h)) / 1_000_000.0

    tiff_params = _tiff_imwrite_params(cv2, compression=str(tiff_compression))

    # Settings / defaults.
    s_in = stitch_settings if isinstance(stitch_settings, dict) else {}
    max_direct_tiles = int(s_in.get("max_direct_tiles", 600))
    neighbor_match = str(s_in.get("neighbor_match", "4")).strip() or "4"

    tile_count_total = int(len(entries))
    is_small = bool(int(tile_count_total) <= int(max_direct_tiles))

    default_final_megapix = 0.05 if is_small else 0.03
    stage1_final_megapix = float(s_in.get("final_megapix", default_final_megapix))
    if stage1_final_megapix <= 0:
        stage1_final_megapix = float(default_final_megapix)

    # Stitcher knobs (passed through to `AffineStitcher`).
    base_stitcher_settings: dict[str, object] = {
        "crop": bool(s_in.get("crop", False)),
        "confidence_threshold": float(s_in.get("confidence_threshold", 0.2)),
        "detector": str(s_in.get("detector", "orb")),
        "nfeatures": int(s_in.get("nfeatures", 1500 if is_small else 600)),
        "range_width": int(s_in.get("range_width", -1)),
        "match_conf": s_in.get("match_conf", None),
        "finder": str(s_in.get("finder", "no")),
        "blender_type": str(s_in.get("blender_type", "no")),
        "blend_strength": float(s_in.get("blend_strength", 5.0)),
        "medium_megapix": float(s_in.get("medium_megapix", 0.6 if is_small else 0.2)),
        "low_megapix": float(s_in.get("low_megapix", 0.1 if is_small else 0.05)),
    }
    orb_fast_threshold = s_in.get("orb_fast_threshold", None)

    def _build_match_mask(rc_list: list[tuple[int, int]], *, use_diag: bool) -> "np.ndarray | None":
        if len(rc_list) <= 1:
            return None
        rc_to_idx: dict[tuple[int, int], int] = {tuple(rc): int(i) for i, rc in enumerate(rc_list)}
        n = int(len(rc_list))
        m = np.zeros((int(n), int(n)), np.uint8)
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
        return m if int(m.sum()) > 0 else None

    def _resize_to_megapix(img: "np.ndarray", target_mp: float) -> "np.ndarray":
        if target_mp <= 0:
            return img
        h, w = img.shape[:2]
        mp = (float(w) * float(h)) / 1_000_000.0
        if mp <= 1e-9:
            return img
        scale = math.sqrt(float(target_mp) / float(mp))
        scale = max(0.02, min(1.0, float(scale)))
        nw = max(1, int(round(float(w) * float(scale))))
        nh = max(1, int(round(float(h) * float(scale))))
        if int(nw) == int(w) and int(nh) == int(h):
            return img
        return cv2.resize(img, (int(nw), int(nh)), interpolation=cv2.INTER_AREA)

    def _run_affine(
        image_paths: list[str],
        rc_list: list[tuple[int, int]] | None,
        *,
        final_megapix: float,
        stage_label: str,
        overrides: dict[str, object] | None = None,
        safety: bool = True,
    ) -> tuple["np.ndarray", dict[str, object]]:
        try:
            import warnings
            from stitching import AffineStitcher  # type: ignore
            from stitching.images import Images  # type: ignore
        except Exception as exc:
            raise RuntimeError(
                "Missing `stitching` package. Install: `python -m pip install stitching` "
                f"(original error: {exc})"
            ) from exc

        local_settings = dict(base_stitcher_settings)
        if overrides:
            for k, v in dict(overrides).items():
                local_settings[k] = v
        local_settings["final_megapix"] = float(final_megapix)

        pano = None
        tiles_used = None
        warnings_txt = ""
        match_pairs = None

        with warnings.catch_warnings(record=True) as rec:
            try:
                stitcher = AffineStitcher(**local_settings)

                if orb_fast_threshold is not None:
                    try:
                        thr = int(float(orb_fast_threshold))
                        det = getattr(getattr(stitcher, "finder", None), "detector", None)
                        if det is not None and hasattr(det, "setFastThreshold"):
                            det.setFastThreshold(int(thr))
                    except Exception:
                        pass

                match_mask = None
                if rc_list is not None:
                    use_diag = neighbor_match != "4"
                    match_mask = _build_match_mask(list(rc_list), use_diag=bool(use_diag))
                    if match_mask is not None:
                        match_pairs = int(int(match_mask.sum()) // 2)

                stitcher.images = Images.of(
                    list(image_paths), stitcher.medium_megapix, stitcher.low_megapix, stitcher.final_megapix
                )

                imgs = stitcher.resize_medium_resolution()
                try:
                    ref_h, ref_w = imgs[0].shape[:2]
                    expected_focal = float(max(int(ref_w), int(ref_h)))
                except Exception:
                    expected_focal = 1000.0
                features = stitcher.find_features(imgs)
                if match_mask is not None:
                    # Prevent OpenCV's matcher from crashing when an image has too few features
                    # (e.g. blank/blurred tiles can produce <2 keypoints).
                    try:
                        bad: list[int] = []
                        for i, feat in enumerate(features):
                            try:
                                kps = feat.getKeypoints()
                                if kps is None or len(kps) < 2:
                                    bad.append(int(i))
                            except Exception:
                                continue
                        if bad:
                            mm = match_mask.copy()
                            for i in bad:
                                mm[int(i), :] = 0
                                mm[:, int(i)] = 0
                            match_mask = mm
                    except Exception:
                        pass
                    matches = stitcher.matcher.match_features(features, match_mask)
                else:
                    matches = stitcher.match_features(features)

                if bool(safety):
                    # Drop clearly-bad pairwise transforms early (prevents pathological camera solutions).
                    try:
                        max_dx = float(ref_w) * 1.5
                        max_dy = float(ref_h) * 1.5
                        min_s = 0.8
                        max_s = 1.25
                        max_angle_rad = math.radians(20.0)
                        min_inliers = 6
                        for mi in matches:
                            try:
                                if int(getattr(mi, "num_inliers", 0)) < int(min_inliers):
                                    mi.confidence = 0.0
                                    mi.num_inliers = 0
                                    continue
                                H = mi.H
                                if H is None:
                                    continue
                                dx = float(H[0][2])
                                dy = float(H[1][2])
                                if abs(dx) > max_dx or abs(dy) > max_dy:
                                    mi.confidence = 0.0
                                    mi.num_inliers = 0
                                    continue
                                a00 = float(H[0][0])
                                a01 = float(H[0][1])
                                a10 = float(H[1][0])
                                a11 = float(H[1][1])
                                try:
                                    ang = float(math.atan2(a10, a00))
                                    if abs(ang) > max_angle_rad:
                                        mi.confidence = 0.0
                                        mi.num_inliers = 0
                                        continue
                                except Exception:
                                    pass
                                sx = math.hypot(a00, a10)
                                sy = math.hypot(a01, a11)
                                if sx < min_s or sx > max_s or sy < min_s or sy > max_s:
                                    mi.confidence = 0.0
                                    mi.num_inliers = 0
                            except Exception:
                                continue

                        # Robustly clamp translations to a per-edge median (scanner tiles should have
                        # near-constant neighbor motion). This helps prevent blown-up panoramas.
                        if rc_list is not None:
                            try:
                                rc_by_idx = list(rc_list)

                                def _med(vals: list[float]) -> float:
                                    vals2 = sorted(float(v) for v in vals)
                                    if not vals2:
                                        return 0.0
                                    mid = len(vals2) // 2
                                    if len(vals2) % 2 == 1:
                                        return float(vals2[mid])
                                    return 0.5 * float(vals2[mid - 1] + vals2[mid])

                                def _mad(vals: list[float], m: float) -> float:
                                    return float(_med([abs(float(v) - float(m)) for v in vals]))

                                samples_r: list[tuple[float, float]] = []
                                samples_d: list[tuple[float, float]] = []
                                for mi in matches:
                                    try:
                                        if float(getattr(mi, "confidence", 0.0)) < 0.5:
                                            continue
                                        if int(getattr(mi, "num_inliers", 0)) < int(min_inliers):
                                            continue
                                        H = mi.H
                                        if H is None:
                                            continue
                                        s = int(getattr(mi, "src_img_idx", -1))
                                        d = int(getattr(mi, "dst_img_idx", -1))
                                        if s < 0 or d < 0 or s >= len(rc_by_idx) or d >= len(rc_by_idx):
                                            continue
                                        rs, cs = rc_by_idx[s]
                                        rd, cd = rc_by_idx[d]
                                        dr = int(rd) - int(rs)
                                        dc = int(cd) - int(cs)
                                        if abs(int(dr)) + abs(int(dc)) != 1:
                                            continue
                                        dx = float(H[0][2])
                                        dy = float(H[1][2])
                                        # Normalize direction so medians are comparable.
                                        if dr == 0 and dc != 0:
                                            if dc < 0:
                                                dx, dy = -dx, -dy
                                            samples_r.append((dx, dy))
                                        elif dc == 0 and dr != 0:
                                            if dr < 0:
                                                dx, dy = -dx, -dy
                                            samples_d.append((dx, dy))
                                    except Exception:
                                        continue

                                def _thr(samples: list[tuple[float, float]]) -> tuple[float, float, float] | None:
                                    if len(samples) < 8:
                                        return None
                                    xs = [float(x) for x, _y in samples]
                                    ys = [float(y) for _x, y in samples]
                                    mx = _med(xs)
                                    my = _med(ys)
                                    dists = [math.hypot(float(x) - float(mx), float(y) - float(my)) for x, y in samples]
                                    md = _med(dists)
                                    mad = _mad(dists, md)
                                    # Allow some variation, but keep a hard cap proportional to image size.
                                    cap = 0.6 * float(max(ref_w, ref_h))
                                    t = float(min(float(md) + (6.0 * max(1.0, float(mad))), float(cap)))
                                    return mx, my, t

                                thr_r = _thr(samples_r)
                                thr_d = _thr(samples_d)
                                for mi in matches:
                                    try:
                                        H = mi.H
                                        if H is None:
                                            continue
                                        s = int(getattr(mi, "src_img_idx", -1))
                                        d = int(getattr(mi, "dst_img_idx", -1))
                                        if s < 0 or d < 0 or s >= len(rc_by_idx) or d >= len(rc_by_idx):
                                            continue
                                        rs, cs = rc_by_idx[s]
                                        rd, cd = rc_by_idx[d]
                                        dr = int(rd) - int(rs)
                                        dc = int(cd) - int(cs)
                                        if abs(int(dr)) + abs(int(dc)) != 1:
                                            continue
                                        dx = float(H[0][2])
                                        dy = float(H[1][2])
                                        if dr == 0 and dc != 0 and thr_r is not None:
                                            mx, my, t = thr_r
                                            if dc < 0:
                                                dx, dy = -dx, -dy
                                            if math.hypot(float(dx) - float(mx), float(dy) - float(my)) > float(t):
                                                mi.confidence = 0.0
                                                mi.num_inliers = 0
                                        elif dc == 0 and dr != 0 and thr_d is not None:
                                            mx, my, t = thr_d
                                            if dr < 0:
                                                dx, dy = -dx, -dy
                                            if math.hypot(float(dx) - float(mx), float(dy) - float(my)) > float(t):
                                                mi.confidence = 0.0
                                                mi.num_inliers = 0
                                    except Exception:
                                        continue
                            except Exception:
                                pass
                    except Exception:
                        pass

                imgs, features, matches = stitcher.subset(imgs, features, matches)
                try:
                    tiles_used = int(len(getattr(stitcher, "images").names))
                except Exception:
                    tiles_used = None

                cameras = stitcher.estimate_camera_parameters(features, matches)
                cameras = stitcher.refine_camera_parameters(features, matches, cameras)
                cameras = stitcher.perform_wave_correction(cameras)
                if bool(safety):
                    # Guard against pathological camera estimates (can explode warp sizes and crash in cv2.remap).
                    try:
                        lo = max(1.0, float(expected_focal) * 0.2)
                        hi = max(lo, float(expected_focal) * 10.0)
                        for cam in cameras:
                            try:
                                f = float(getattr(cam, "focal", expected_focal))
                                if not math.isfinite(f):
                                    f = float(expected_focal)
                                cam.focal = float(max(lo, min(hi, f)))
                            except Exception:
                                continue
                    except Exception:
                        pass
                stitcher.estimate_scale(cameras)

                if bool(safety):
                    # Preflight warp ROIs to avoid OOM / SHRT_MAX crashes inside cv2.remap.
                    try:
                        max_dim = int(float(s_in.get("max_warp_dim", 12000)))
                        low_sizes = stitcher.images.get_scaled_img_sizes(Images.Resolution.LOW)
                        camera_aspect = stitcher.images.get_ratio(Images.Resolution.MEDIUM, Images.Resolution.LOW)
                        for size, cam in zip(low_sizes, cameras):
                            x, y, w, h = stitcher.warper.warp_roi(size, cam, camera_aspect)
                            if int(w) > int(max_dim) or int(h) > int(max_dim):
                                raise RuntimeError(
                                    f"{stage_label}: warp ROI too large ({int(w)}x{int(h)}); try smaller blocks, stricter matching, or SIFT."
                                )
                    except RuntimeError:
                        raise
                    except Exception:
                        # If the preflight itself fails, continue and let the real warp raise.
                        pass

                imgs = stitcher.resize_low_resolution(imgs)
                imgs, masks, corners, sizes = stitcher.warp_low_resolution(imgs, cameras)
                stitcher.prepare_cropper(imgs, masks, corners, sizes)
                imgs, masks, corners, sizes = stitcher.crop_low_resolution(imgs, masks, corners, sizes)

                stitcher.estimate_exposure_errors(corners, imgs, masks)
                seam_masks = stitcher.find_seam_masks(imgs, corners, masks)

                imgs = stitcher.resize_final_resolution()
                imgs, masks, corners, sizes = stitcher.warp_final_resolution(imgs, cameras)
                imgs, masks, corners, sizes = stitcher.crop_final_resolution(imgs, masks, corners, sizes)

                stitcher.set_masks(masks)
                imgs = stitcher.compensate_exposure_errors(corners, imgs)
                seam_masks = stitcher.resize_seam_masks(seam_masks)

                if bool(safety):
                    # Sanity-check the final panorama ROI before allocating a huge blender output.
                    try:
                        max_px = int(float(s_in.get("max_panorama_pixels", 250_000_000)))
                        min_x = min(int(c[0]) for c in corners)
                        min_y = min(int(c[1]) for c in corners)
                        max_x = max(int(c[0]) + int(s[0]) for c, s in zip(corners, sizes))
                        max_y = max(int(c[1]) + int(s[1]) for c, s in zip(corners, sizes))
                        out_w = int(max_x - min_x)
                        out_h = int(max_y - min_y)
                        if out_w <= 0 or out_h <= 0:
                            raise RuntimeError(f"{stage_label}: invalid panorama ROI.")
                        if (int(out_w) * int(out_h)) > int(max_px):
                            raise RuntimeError(
                                f"{stage_label}: panorama too large ({out_w}x{out_h} px). "
                                "Try smaller strips, lower final_megapix, or stricter matching."
                            )
                    except RuntimeError:
                        raise
                    except Exception:
                        pass

                stitcher.initialize_composition(corners, sizes)
                stitcher.blend_images(imgs, seam_masks, corners)
                pano = stitcher.create_final_panorama()
            finally:
                try:
                    warnings_txt = "\n".join(str(w.message) for w in rec if getattr(w, "message", None) is not None)
                except Exception:
                    warnings_txt = ""

        if pano is None:
            raise RuntimeError(f"{stage_label}: stitcher returned None.")

        meta = {
            "tiles_in": int(len(image_paths)),
            "tiles_used": int(tiles_used) if tiles_used is not None else None,
            "final_megapix": float(final_megapix),
            "match_mask_pairs": int(match_pairs) if match_pairs is not None else None,
            "warnings": str(warnings_txt) if warnings_txt else None,
        }
        return pano, meta

    # Strategy:
    # - If tiles are small enough: stitch directly (OpenStitching AffineStitcher; neighbor mask).
    # - Otherwise: estimate a global affine grid layout and composite tiles.
    tile_count = int(len(entries))
    _emit_progress(0.0, f"Stitching (affine): {tile_count} tiles…")

    pano = None
    stages: list[dict[str, object]] = []
    strategy_name = "single-pass"
    strategy_settings: dict[str, object] = {}
    method_name = "openstitching-affine"
    try:
        if int(tile_count) <= int(max_direct_tiles):
            rc_all = [(int(e.row), int(e.col)) for e in entries]
            paths_all = [str(e.path) for e in entries]
            _emit_progress(5.0, "Stitching: affine (single pass)…")
            pano, meta0 = _run_affine(
                paths_all, rc_all, final_megapix=float(stage1_final_megapix), stage_label="full", safety=False
            )
            stages.append({"name": "full", **meta0})
        else:
            # Large scans: use an affine grid layout derived from sampled pairwise affine shifts.
            # This avoids the global bundle-adjustment instability/OOM issues seen with thousands of tiles.
            method_name = "affine-layout"
            strategy_name = "layout"

            import random

            layout_seed = int(s_in.get("layout_seed", 0))
            layout_megapix = float(s_in.get("layout_megapix", max(float(stage1_final_megapix), 0.2)))
            layout_samples = int(s_in.get("layout_samples", 250))
            layout_nfeatures = int(s_in.get("layout_nfeatures", 2000))
            layout_orb_fast_threshold = int(s_in.get("layout_orb_fast_threshold", 10))
            layout_ratio_test = float(s_in.get("layout_ratio_test", 0.75))
            layout_ransac_thresh = float(s_in.get("layout_ransac_thresh", 3.0))
            layout_min_inliers = int(s_in.get("layout_min_inliers", 10))
            blend_mode = str(s_in.get("layout_blend", "overwrite")).strip().lower() or "overwrite"
            if blend_mode not in {"overwrite", "average"}:
                blend_mode = "overwrite"

            rng = random.Random(int(layout_seed))
            try:
                cv2.setRNGSeed(int(layout_seed))
            except Exception:
                pass

            def _scale_for_mp(target_mp: float) -> float:
                if float(target_mp) <= 0:
                    return 1.0
                s = math.sqrt(float(target_mp) / float(orig_mp))
                return max(0.02, min(1.0, float(s)))

            scale_final = float(_scale_for_mp(float(stage1_final_megapix)))
            scale_match = float(_scale_for_mp(float(layout_megapix)))
            if float(scale_match) <= 1e-9:
                scale_match = float(scale_final)
            ratio = float(scale_final) / float(scale_match)

            w_final = max(1, int(round(float(tile_w) * float(scale_final))))
            h_final = max(1, int(round(float(tile_h) * float(scale_final))))
            w_match = max(1, int(round(float(tile_w) * float(scale_match))))
            h_match = max(1, int(round(float(tile_h) * float(scale_match))))

            orb = cv2.ORB_create(nfeatures=int(layout_nfeatures), fastThreshold=int(layout_orb_fast_threshold))
            bf = cv2.BFMatcher(cv2.NORM_HAMMING)

            feat_cache: dict[str, tuple[object | None, "np.ndarray | None"]] = {}

            def _get_orb(path: str) -> tuple[object | None, "np.ndarray | None"]:
                cached = feat_cache.get(path)
                if cached is not None:
                    return cached
                img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
                if img is None:
                    feat_cache[path] = (None, None)
                    return (None, None)
                try:
                    if int(img.shape[1]) != int(w_match) or int(img.shape[0]) != int(h_match):
                        img = cv2.resize(img, (int(w_match), int(h_match)), interpolation=cv2.INTER_AREA)
                except Exception:
                    pass
                try:
                    kps, des = orb.detectAndCompute(img, None)
                except Exception:
                    kps, des = None, None
                feat_cache[path] = (kps, des)
                return kps, des

            def _estimate_pair_shift(p1: str, p2: str) -> tuple[float, float, int] | None:
                k1, d1 = _get_orb(p1)
                k2, d2 = _get_orb(p2)
                if d1 is None or d2 is None or k1 is None or k2 is None:
                    return None
                try:
                    if len(d1) < 2 or len(d2) < 2:
                        return None
                except Exception:
                    return None
                try:
                    knn = bf.knnMatch(d1, d2, k=2)
                except Exception:
                    return None
                good = []
                for a, b in knn:
                    try:
                        if float(a.distance) < float(layout_ratio_test) * float(b.distance):
                            good.append(a)
                    except Exception:
                        continue
                if len(good) < 8:
                    return None
                try:
                    pts1 = np.float32([k1[m.queryIdx].pt for m in good])
                    pts2 = np.float32([k2[m.trainIdx].pt for m in good])
                    H, inliers = cv2.estimateAffinePartial2D(
                        pts1,
                        pts2,
                        method=cv2.RANSAC,
                        ransacReprojThreshold=float(layout_ransac_thresh),
                    )
                except Exception:
                    return None
                if H is None:
                    return None
                try:
                    inl = int(inliers.sum()) if inliers is not None else 0
                except Exception:
                    inl = 0
                if int(inl) < int(layout_min_inliers):
                    return None
                dx = -float(H[0][2]) * float(ratio)
                dy = -float(H[1][2]) * float(ratio)
                if not (math.isfinite(dx) and math.isfinite(dy)):
                    return None
                return float(dx), float(dy), int(inl)

            def _median(vals: list[float]) -> float:
                vals2 = sorted(float(v) for v in vals)
                if not vals2:
                    return 0.0
                mid = len(vals2) // 2
                if len(vals2) % 2 == 1:
                    return float(vals2[mid])
                return 0.5 * float(vals2[mid - 1] + vals2[mid])

            def _robust_center(vecs: list[tuple[float, float]]) -> tuple[float, float, int]:
                if not vecs:
                    return (0.0, 0.0, 0)
                xs = [float(x) for x, _y in vecs]
                ys = [float(y) for _x, y in vecs]
                mx = _median(xs)
                my = _median(ys)
                dists = [math.hypot(float(x) - float(mx), float(y) - float(my)) for x, y in vecs]
                md = _median(dists)
                mad = _median([abs(float(d) - float(md)) for d in dists])
                thr = float(md) + (6.0 * max(1.0, float(mad)))
                kept = [(x, y) for (x, y), d in zip(vecs, dists) if float(d) <= float(thr)]
                if len(kept) < 3:
                    return float(mx), float(my), int(len(kept))
                xs2 = [float(x) for x, _y in kept]
                ys2 = [float(y) for _x, y in kept]
                return float(_median(xs2)), float(_median(ys2)), int(len(kept))

            # Sample neighbor pairs to estimate the per-step translation vectors.
            _emit_progress(5.0, "Stitching: estimating affine step vectors…")
            right_candidates = [(r, c) for r in range(int(nrows)) for c in range(int(ncols) - 1)]
            down_candidates = [(r, c) for r in range(int(nrows) - 1) for c in range(int(ncols))]
            rng.shuffle(right_candidates)
            rng.shuffle(down_candidates)

            right_vecs: list[tuple[float, float]] = []
            down_vecs: list[tuple[float, float]] = []

            for r, c in right_candidates[: max(1, int(layout_samples))]:
                p1 = by_rc.get((int(r), int(c)))
                p2 = by_rc.get((int(r), int(c + 1)))
                if not p1 or not p2:
                    continue
                est = _estimate_pair_shift(str(p1), str(p2))
                if est is None:
                    continue
                right_vecs.append((float(est[0]), float(est[1])))

            for r, c in down_candidates[: max(1, int(layout_samples))]:
                p1 = by_rc.get((int(r), int(c)))
                p2 = by_rc.get((int(r + 1), int(c)))
                if not p1 or not p2:
                    continue
                est = _estimate_pair_shift(str(p1), str(p2))
                if est is None:
                    continue
                down_vecs.append((float(est[0]), float(est[1])))

            v_col_x, v_col_y, n_col = _robust_center(right_vecs)
            v_row_x, v_row_y, n_row = _robust_center(down_vecs)
            if int(n_col) < 3 or int(n_row) < 3:
                raise RuntimeError(
                    "Affine layout failed: could not estimate reliable neighbor shifts. "
                    "Try increasing overlap, increasing layout_megapix, or using SIFT."
                )

            v_col = (float(v_col_x), float(v_col_y))
            v_row = (float(v_row_x), float(v_row_y))

            strategy_settings = {
                "strategy": "layout",
                "final_megapix": float(stage1_final_megapix),
                "layout_megapix": float(layout_megapix),
                "layout_samples": int(layout_samples),
                "layout_seed": int(layout_seed),
                "layout_orb_nfeatures": int(layout_nfeatures),
                "layout_orb_fast_threshold": int(layout_orb_fast_threshold),
                "layout_ratio_test": float(layout_ratio_test),
                "layout_ransac_thresh": float(layout_ransac_thresh),
                "layout_min_inliers": int(layout_min_inliers),
                "step_col_px": [float(v_col[0]), float(v_col[1])],
                "step_row_px": [float(v_row[0]), float(v_row[1])],
                "step_col_samples": int(len(right_vecs)),
                "step_row_samples": int(len(down_vecs)),
                "step_col_samples_kept": int(n_col),
                "step_row_samples_kept": int(n_row),
                "blend": str(blend_mode),
            }

            # Compute output bounds.
            min_x = float("inf")
            min_y = float("inf")
            max_x = float("-inf")
            max_y = float("-inf")
            for e in entries:
                px = (float(e.col) * float(v_col[0])) + (float(e.row) * float(v_row[0]))
                py = (float(e.col) * float(v_col[1])) + (float(e.row) * float(v_row[1]))
                min_x = min(min_x, float(px))
                min_y = min(min_y, float(py))
                max_x = max(max_x, float(px))
                max_y = max(max_y, float(py))

            out_w = int(math.ceil(float(max_x - min_x) + float(w_final)))
            out_h = int(math.ceil(float(max_y - min_y) + float(h_final)))
            if out_w <= 0 or out_h <= 0:
                raise RuntimeError("Affine layout failed: invalid output canvas size.")
            max_px = int(float(s_in.get("max_panorama_pixels", 250_000_000)))
            if (int(out_w) * int(out_h)) > int(max_px):
                raise RuntimeError(
                    f"Affine layout would be too large ({out_w}x{out_h} px). "
                    "Lower final_megapix or scan a smaller area."
                )

            _emit_progress(10.0, f"Stitching: compositing {tile_count} tiles…")
            pano = np.zeros((int(out_h), int(out_w), 3), dtype=np.uint8)

            for i, e in enumerate(entries):
                img = cv2.imread(str(e.path), cv2.IMREAD_COLOR)
                if img is None:
                    continue
                if int(img.shape[1]) != int(w_final) or int(img.shape[0]) != int(h_final):
                    try:
                        img = cv2.resize(img, (int(w_final), int(h_final)), interpolation=cv2.INTER_AREA)
                    except Exception:
                        continue

                px = (float(e.col) * float(v_col[0])) + (float(e.row) * float(v_row[0]))
                py = (float(e.col) * float(v_col[1])) + (float(e.row) * float(v_row[1]))
                x = int(round(float(px) - float(min_x)))
                y = int(round(float(py) - float(min_y)))
                if x < 0 or y < 0 or (x + int(w_final)) > int(out_w) or (y + int(h_final)) > int(out_h):
                    continue

                if blend_mode == "average":
                    roi = pano[int(y) : int(y + h_final), int(x) : int(x + w_final)]
                    try:
                        mask = (roi.sum(axis=2) > 0)  # type: ignore[call-arg]
                        if mask.any():
                            roi[mask] = ((roi[mask].astype(np.uint16) + img[mask].astype(np.uint16)) // 2).astype(
                                np.uint8
                            )
                        roi[~mask] = img[~mask]
                    except Exception:
                        pano[int(y) : int(y + h_final), int(x) : int(x + w_final)] = img
                else:
                    pano[int(y) : int(y + h_final), int(x) : int(x + w_final)] = img

                if (i % 50) == 0 or (i + 1) == int(tile_count):
                    frac = float(i + 1) / float(max(1, int(tile_count)))
                    _emit_progress(10.0 + (80.0 * frac), f"Stitching: compositing ({i + 1}/{tile_count})…")

            stages.append({"name": "layout", "tiles_in": int(tile_count), "tiles_used": int(tile_count), **strategy_settings})

        if pano is None:
            raise RuntimeError("Stitching failed: no panorama produced.")

        _emit_progress(92.0, "Stitching: writing mosaic_full.tif…")
        mosaic_path = os.path.join(out_dir, "mosaic_full.tif")
        try:
            imwrite(cv2, mosaic_path, pano, tiff_params)
        except Exception as exc:
            _write_err("final_write", exc)
            if is_no_space_error(exc):
                raise RuntimeError(
                    "No space left on device while writing mosaic_full.tif. Free disk space or choose a different output folder."
                ) from exc
            raise

        try:
            H, W = pano.shape[:2]
        except Exception:
            H, W = 0, 0

        meta: dict[str, object] = {
            "method": str(method_name),
            "tiles": int(tile_count),
            "rows": int(nrows),
            "cols": int(ncols),
            "tile_size_px": [int(tile_w), int(tile_h)],
            "mosaic_size_px": [int(W), int(H)],
            "stages": list(stages),
            "settings": {
                "strategy": str(strategy_name),
                "max_direct_tiles": int(max_direct_tiles),
                "neighbor_match": str(neighbor_match),
                "final_megapix": float(stage1_final_megapix),
                "stitcher": dict(base_stitcher_settings),
                "orb_fast_threshold": orb_fast_threshold,
                "strategy_settings": dict(strategy_settings),
            },
            "versions": {
                "opencv": str(getattr(cv2, "__version__", "?")),
            },
        }
        try:
            import stitching as stitching_pkg  # type: ignore

            meta["versions"]["stitching"] = str(getattr(stitching_pkg, "__version__", "?"))
        except Exception:
            pass

        try:
            with open(os.path.join(out_dir, "stitch_meta.json"), "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2, sort_keys=False)
        except Exception:
            pass

        _emit_progress(100.0, "Stitching: done.")
    except Exception as exc:
        _write_err("stitch", exc)
        raise
