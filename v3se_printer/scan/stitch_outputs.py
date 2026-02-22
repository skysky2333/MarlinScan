from __future__ import annotations

import gc
import json
import math
import os
import shutil
import subprocess
import sys
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


def _median(vals: list[float]) -> float:
    vals2 = sorted(float(v) for v in vals)
    if not vals2:
        return 0.0
    mid = int(len(vals2) // 2)
    if (len(vals2) % 2) == 1:
        return float(vals2[mid])
    return 0.5 * float(vals2[mid - 1] + vals2[mid])


def _try_set_dpi(path: str, *, dpi_x: float, dpi_y: float) -> bool:
    """
    Best-effort: set TIFF DPI metadata without touching pixel data.

    On macOS we use `sips`, which (empirically) preserves pixels for TIFF.
    """
    try:
        if sys.platform != "darwin":
            return False
        if not shutil.which("sips"):
            return False
        subprocess.run(
            ["sips", "-s", "dpiWidth", str(float(dpi_x)), "-s", "dpiHeight", str(float(dpi_y)), str(path)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


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
        for fn in (
            "stitch_error.txt",
            "stitch_meta.json",
            "mosaic_full.tif",
            "_mosaic_memmap.dat",
            "mosaic_thumb_2000.jpg",
        ):
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
    mm_by_rc: dict[tuple[int, int], tuple[float, float]] = {}
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
        try:
            x_mm = float(t.get("x_mm"))  # type: ignore[arg-type]
            y_mm = float(t.get("y_mm"))  # type: ignore[arg-type]
            if math.isfinite(float(x_mm)) and math.isfinite(float(y_mm)):
                mm_by_rc[(int(r), int(c))] = (float(x_mm), float(y_mm))
        except Exception:
            pass
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

    final_megapix_user: float | None = None
    try:
        v = s_in.get("final_megapix", None)
    except Exception:
        v = None
    if v is not None:
        try:
            final_megapix_user = float(v)
        except Exception:
            final_megapix_user = None

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

    def _infer_step_mm_from_tiles() -> tuple[float | None, float | None]:
        dxs: list[float] = []
        dys: list[float] = []
        for (r, c), (x1, y1) in mm_by_rc.items():
            p2 = mm_by_rc.get((int(r), int(c + 1)))
            if p2 is not None:
                x2, _y2 = p2
                dx = abs(float(x2) - float(x1))
                if float(dx) > 1e-9 and math.isfinite(dx):
                    dxs.append(float(dx))
            p3 = mm_by_rc.get((int(r + 1), int(c)))
            if p3 is not None:
                _x3, y3 = p3
                dy = abs(float(y3) - float(y1))
                if float(dy) > 1e-9 and math.isfinite(dy):
                    dys.append(float(dy))
        sx = float(_median(dxs)) if dxs else None
        sy = float(_median(dys)) if dys else None
        if sx is not None and float(sx) <= 0:
            sx = None
        if sy is not None and float(sy) <= 0:
            sy = None
        return sx, sy

    # Physical step (mm) for computing output PPI/DPI metadata.
    step_x_mm: float | None = None
    step_y_mm: float | None = None
    serpentine: bool | None = None
    try:
        with open(os.path.join(out_dir, "scan_params.json"), "r", encoding="utf-8") as f:
            scan_params = json.load(f)
        if isinstance(scan_params, dict):
            try:
                step_x_mm = float(scan_params.get("step_x_mm"))  # type: ignore[arg-type]
            except Exception:
                step_x_mm = None
            try:
                step_y_mm = float(scan_params.get("step_y_mm"))  # type: ignore[arg-type]
            except Exception:
                step_y_mm = None
            try:
                serpentine = bool(scan_params.get("serpentine"))  # type: ignore[arg-type]
            except Exception:
                serpentine = None
    except Exception:
        pass
    if step_x_mm is not None and (not math.isfinite(step_x_mm) or float(step_x_mm) <= 0):
        step_x_mm = None
    if step_y_mm is not None and (not math.isfinite(step_y_mm) or float(step_y_mm) <= 0):
        step_y_mm = None
    if step_x_mm is None or step_y_mm is None:
        sx2, sy2 = _infer_step_mm_from_tiles()
        if step_x_mm is None:
            step_x_mm = sx2
        if step_y_mm is None:
            step_y_mm = sy2

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

    def _estimate_step_vectors(
        *,
        final_megapix: float,
        min_kept: int,
    ) -> dict[str, object] | None:
        """
        Estimate per-step translation vectors between neighboring tiles, then scale the vectors to
        the requested final resolution.

        Returns a dict containing:
        - step_col_px / step_row_px (dx, dy) at the final resolution
        - layout_* parameters and sampling stats
        """
        import random

        layout_seed = int(s_in.get("layout_seed", 0))
        layout_megapix = float(s_in.get("layout_megapix", max(float(final_megapix), 0.2)))
        layout_samples = int(s_in.get("layout_samples", 250))
        layout_nfeatures = int(s_in.get("layout_nfeatures", 2000))
        layout_orb_fast_threshold = int(s_in.get("layout_orb_fast_threshold", 10))
        layout_ratio_test = float(s_in.get("layout_ratio_test", 0.75))
        layout_ransac_thresh = float(s_in.get("layout_ransac_thresh", 3.0))
        layout_min_inliers = int(s_in.get("layout_min_inliers", 10))

        def _scale_for_mp(target_mp: float) -> float:
            if float(target_mp) <= 0:
                return 1.0
            s = math.sqrt(float(target_mp) / float(orig_mp))
            return max(0.02, min(1.0, float(s)))

        scale_final = float(_scale_for_mp(float(final_megapix)))
        scale_match = float(_scale_for_mp(float(layout_megapix)))
        if float(scale_match) <= 1e-9:
            scale_match = float(scale_final)
        ratio = float(scale_final) / float(scale_match)

        w_match = max(1, int(round(float(tile_w) * float(scale_match))))
        h_match = max(1, int(round(float(tile_h) * float(scale_match))))
        w_final = max(1.0, float(tile_w) * float(scale_final))
        h_final = max(1.0, float(tile_h) * float(scale_final))
        max_step = 1.25 * float(max(w_final, h_final))

        orb = cv2.ORB_create(nfeatures=int(layout_nfeatures), fastThreshold=int(layout_orb_fast_threshold))
        bf = cv2.BFMatcher(cv2.NORM_HAMMING)

        rng = random.Random(int(layout_seed))
        try:
            cv2.setRNGSeed(int(layout_seed))
        except Exception:
            pass

        feat_cache: dict[str, tuple[object | None, "np.ndarray | None"]] = {}
        gray_cache: dict[str, "np.ndarray | None"] = {}
        try:
            pc_window = cv2.createHanningWindow((int(w_match), int(h_match)), cv2.CV_32F)
        except Exception:
            pc_window = None

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

        def _get_gray(path: str) -> "np.ndarray | None":
            cached = gray_cache.get(path)
            if cached is not None:
                return cached
            img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
            if img is None:
                gray_cache[path] = None
                return None
            try:
                if int(img.shape[1]) != int(w_match) or int(img.shape[0]) != int(h_match):
                    img = cv2.resize(img, (int(w_match), int(h_match)), interpolation=cv2.INTER_AREA)
            except Exception:
                pass
            try:
                out = img.astype(np.float32)
            except Exception:
                out = None
            gray_cache[path] = out
            return out

        def _estimate_pair_shift(p1: str, p2: str) -> tuple[float, float, int] | None:
            k1, d1 = _get_orb(p1)
            k2, d2 = _get_orb(p2)
            # Try feature-based affine first.
            if d1 is not None and d2 is not None and k1 is not None and k2 is not None:
                try:
                    if len(d1) >= 2 and len(d2) >= 2:
                        knn = bf.knnMatch(d1, d2, k=2)
                        good = []
                        for a, b in knn:
                            try:
                                if float(a.distance) < float(layout_ratio_test) * float(b.distance):
                                    good.append(a)
                            except Exception:
                                continue
                        if len(good) >= 8:
                            pts1 = np.float32([k1[m.queryIdx].pt for m in good])
                            pts2 = np.float32([k2[m.trainIdx].pt for m in good])
                            H, inliers = cv2.estimateAffinePartial2D(
                                pts1,
                                pts2,
                                method=cv2.RANSAC,
                                ransacReprojThreshold=float(layout_ransac_thresh),
                            )
                            if H is not None:
                                try:
                                    inl = int(inliers.sum()) if inliers is not None else 0
                                except Exception:
                                    inl = 0
                                if int(inl) >= int(layout_min_inliers):
                                    dx = -float(H[0][2]) * float(ratio)
                                    dy = -float(H[1][2]) * float(ratio)
                                    if (
                                        math.isfinite(dx)
                                        and math.isfinite(dy)
                                        and math.hypot(float(dx), float(dy)) <= float(max_step)
                                    ):
                                        return float(dx), float(dy), int(inl)
                except Exception:
                    pass

            # Fallback: phase correlation (more robust for low-feature tiles).
            try:
                img1 = _get_gray(p1)
                img2 = _get_gray(p2)
                if img1 is None or img2 is None:
                    return None
                a = img2
                b = img1
                try:
                    a = a - float(a.mean())
                    b = b - float(b.mean())
                except Exception:
                    pass
                if pc_window is not None:
                    try:
                        a = a * pc_window
                        b = b * pc_window
                    except Exception:
                        pass
                (sx, sy), resp = cv2.phaseCorrelate(a, b)
                if not math.isfinite(float(resp)) or float(resp) < 0.05:
                    return None
                dx = float(sx) * float(ratio)
                dy = float(sy) * float(ratio)
                if not (math.isfinite(dx) and math.isfinite(dy)):
                    return None
                if math.hypot(float(dx), float(dy)) > float(max_step):
                    return None
                return float(dx), float(dy), int(layout_min_inliers)
            except Exception:
                return None

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
            if not kept:
                return float(mx), float(my), 0
            xs2 = [float(x) for x, _y in kept]
            ys2 = [float(y) for _x, y in kept]
            return float(_median(xs2)), float(_median(ys2)), int(len(kept))

        right_candidates = [(r, c) for r in range(int(nrows)) for c in range(int(ncols) - 1)]
        down_candidates = [(r, c) for r in range(int(nrows) - 1) for c in range(int(ncols))]
        rng.shuffle(right_candidates)
        rng.shuffle(down_candidates)

        right_vecs: list[tuple[float, float]] = []
        down_vecs: list[tuple[float, float]] = []
        down_vecs_even: list[tuple[float, float]] = []
        down_vecs_odd: list[tuple[float, float]] = []

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
            dxdy = (float(est[0]), float(est[1]))
            down_vecs.append(dxdy)
            if (int(r) % 2) == 0:
                down_vecs_even.append(dxdy)
            else:
                down_vecs_odd.append(dxdy)

        v_col_x, v_col_y, n_col = _robust_center(right_vecs)
        v_row_x, v_row_y, n_row = _robust_center(down_vecs)
        v_row_even_x, v_row_even_y, n_row_even = _robust_center(down_vecs_even)
        v_row_odd_x, v_row_odd_y, n_row_odd = _robust_center(down_vecs_odd)
        need_col = int(ncols) > 1
        need_row = int(nrows) > 1
        if bool(need_col) and int(n_col) < int(min_kept):
            return None
        if bool(need_row) and int(n_row) < int(min_kept):
            return None

        # Serpentine scans frequently show a row-parity-dependent shift (backlash). If it looks
        # significant, keep separate step vectors for even/odd row transitions and use them during
        # layout instead of a single global row-step.
        try:
            parity_pref = s_in.get("layout_row_parity", None)
        except Exception:
            parity_pref = None
        prefer_parity = bool(parity_pref) if parity_pref is not None else bool(serpentine)
        try:
            parity_min_kept = int(s_in.get("layout_row_parity_min_kept", 20))
        except Exception:
            parity_min_kept = 20
        parity_used = False
        if bool(prefer_parity) and int(n_row_even) >= int(parity_min_kept) and int(n_row_odd) >= int(parity_min_kept):
            diff = math.hypot(float(v_row_even_x) - float(v_row_odd_x), float(v_row_even_y) - float(v_row_odd_y))
            if math.isfinite(float(diff)) and float(diff) >= (0.01 * float(max(w_final, h_final))):
                parity_used = True

        out = {
            "layout_megapix": float(layout_megapix),
            "layout_samples": int(layout_samples),
            "layout_seed": int(layout_seed),
            "layout_orb_nfeatures": int(layout_nfeatures),
            "layout_orb_fast_threshold": int(layout_orb_fast_threshold),
            "layout_ratio_test": float(layout_ratio_test),
            "layout_ransac_thresh": float(layout_ransac_thresh),
            "layout_min_inliers": int(layout_min_inliers),
            "step_col_px": [float(v_col_x), float(v_col_y)],
            "step_row_px": [float(v_row_x), float(v_row_y)],
            "step_col_samples": int(len(right_vecs)),
            "step_row_samples": int(len(down_vecs)),
            "step_col_samples_kept": int(n_col),
            "step_row_samples_kept": int(n_row),
        }
        if bool(parity_used):
            out.update(
                {
                    "layout_row_parity": True,
                    "step_row_px_even": [float(v_row_even_x), float(v_row_even_y)],
                    "step_row_px_odd": [float(v_row_odd_x), float(v_row_odd_y)],
                    "step_row_even_samples": int(len(down_vecs_even)),
                    "step_row_odd_samples": int(len(down_vecs_odd)),
                    "step_row_even_samples_kept": int(n_row_even),
                    "step_row_odd_samples_kept": int(n_row_odd),
                }
            )
        return out

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
                        max_px = int(float(s_in.get("max_panorama_pixels", 2_000_000_000)))
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

    # Final resolution (per-tile megapixels).
    # - If user provides `final_megapix`: respect it (`-1` = full-res).
    # - Otherwise: auto = full-res by default (subject to `max_panorama_pixels` safety cap).
    stage1_final_megapix: float
    if final_megapix_user is not None and math.isfinite(float(final_megapix_user)):
        if float(final_megapix_user) == -1.0:
            stage1_final_megapix = -1.0
        elif float(final_megapix_user) > 0:
            stage1_final_megapix = float(final_megapix_user)
        else:
            final_megapix_user = None
    else:
        final_megapix_user = None

    if final_megapix_user is None:
        # Auto mode:
        # Default to full-resolution output. If the mosaic would exceed max_panorama_pixels,
        # we still keep going only if the user explicitly sets a larger cap.
        stage1_final_megapix = -1.0

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
    memmap_path: str | None = None
    try:
        force_layout = bool(s_in.get("force_layout", False)) or (float(stage1_final_megapix) == -1.0)
        use_layout = bool(force_layout) or (int(tile_count) > int(max_direct_tiles))

        if not bool(use_layout):
            rc_all = [(int(e.row), int(e.col)) for e in entries]
            paths_all = [str(e.path) for e in entries]
            step_meta = _estimate_step_vectors(final_megapix=float(stage1_final_megapix), min_kept=1)
            if step_meta is not None:
                strategy_settings = {"strategy": "step_vectors", "final_megapix": float(stage1_final_megapix), **step_meta}
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
            blend_mode = str(s_in.get("layout_blend", "feather")).strip().lower() or "feather"
            if blend_mode not in {"overwrite", "average", "feather"}:
                blend_mode = "overwrite"
            feather_px_in = None
            try:
                feather_px_in = s_in.get("layout_feather_px", None)
            except Exception:
                feather_px_in = None

            _emit_progress(5.0, "Stitching: estimating affine step vectors…")
            right_possible = int(nrows) * max(0, int(ncols) - 1)
            down_possible = max(0, int(nrows) - 1) * int(ncols)
            if (right_possible >= 3 and down_possible >= 3) or (right_possible >= 3 and int(nrows) <= 1) or (down_possible >= 3 and int(ncols) <= 1):
                layout_min_kept = 3
            else:
                layout_min_kept = 1
            step_meta = _estimate_step_vectors(final_megapix=float(stage1_final_megapix), min_kept=int(layout_min_kept))
            if step_meta is None:
                raise RuntimeError(
                    "Affine layout failed: could not estimate reliable neighbor shifts. "
                    "Try increasing overlap, increasing layout_megapix, or using SIFT."
                )

            v_col = (float(step_meta["step_col_px"][0]), float(step_meta["step_col_px"][1]))  # type: ignore[index]
            v_row = (float(step_meta["step_row_px"][0]), float(step_meta["step_row_px"][1]))  # type: ignore[index]
            v_row_even: tuple[float, float] | None = None
            v_row_odd: tuple[float, float] | None = None
            if "step_row_px_even" in step_meta and "step_row_px_odd" in step_meta:
                try:
                    v_row_even = (
                        float(step_meta["step_row_px_even"][0]),
                        float(step_meta["step_row_px_even"][1]),
                    )  # type: ignore[index]
                    v_row_odd = (
                        float(step_meta["step_row_px_odd"][0]),
                        float(step_meta["step_row_px_odd"][1]),
                    )  # type: ignore[index]
                except Exception:
                    v_row_even = None
                    v_row_odd = None

            def _scale_for_mp(target_mp: float) -> float:
                if float(target_mp) <= 0:
                    return 1.0
                s = math.sqrt(float(target_mp) / float(orig_mp))
                return max(0.02, min(1.0, float(s)))

            scale_final = float(_scale_for_mp(float(stage1_final_megapix)))
            w_final = max(1, int(round(float(tile_w) * float(scale_final))))
            h_final = max(1, int(round(float(tile_h) * float(scale_final))))

            # Default feather to a substantial fraction of the tile size so seams fade out across
            # large overlaps. Users can override with `layout_feather_px`.
            feather_px = None
            if feather_px_in is not None:
                try:
                    feather_px = int(float(feather_px_in))
                except Exception:
                    feather_px = None
            if feather_px is None:
                feather_px = int(round(0.4 * float(min(int(w_final), int(h_final)))))
            feather_px = max(0, int(feather_px))

            strategy_settings = {
                "strategy": "layout",
                "final_megapix": float(stage1_final_megapix),
                **step_meta,
                "blend": str(blend_mode),
                "layout_feather_px": int(feather_px),
            }

            # Precompute per-row origins (supports row-parity-dependent steps for serpentine scans).
            row_prefix: list[tuple[float, float]] = [(0.0, 0.0)]
            if v_row_even is not None and v_row_odd is not None and int(nrows) > 1:
                px0 = 0.0
                py0 = 0.0
                for rr in range(1, int(nrows)):
                    step = v_row_even if ((int(rr - 1) % 2) == 0) else v_row_odd
                    px0 += float(step[0])
                    py0 += float(step[1])
                    row_prefix.append((float(px0), float(py0)))
            else:
                for rr in range(1, int(nrows)):
                    row_prefix.append((float(rr) * float(v_row[0]), float(rr) * float(v_row[1])))

            # Initial positions from the global step vectors.
            pos0_by_rc: dict[tuple[int, int], tuple[float, float]] = {}
            for e in entries:
                base_x, base_y = row_prefix[int(e.row)]
                px = float(base_x) + (float(e.col) * float(v_col[0]))
                py = float(base_y) + (float(e.col) * float(v_col[1]))
                pos0_by_rc[(int(e.row), int(e.col))] = (float(px), float(py))

            # Optional: refine positions using overlap-based phase correlation on neighbor pairs,
            # then solve a globally-consistent pose graph (removes small motor step inaccuracies).
            try:
                refine_positions = bool(s_in.get("layout_refine_positions", True))
            except Exception:
                refine_positions = True

            refined_by_rc: dict[tuple[int, int], tuple[float, float]] | None = None
            refine_meta: dict[str, object] = {}
            if bool(refine_positions) and int(tile_count) >= 4 and int(nrows) >= 1 and int(ncols) >= 1:
                _emit_progress(7.0, "Stitching: refining tile positions…")
                try:
                    from functools import lru_cache

                    refine_megapix = float(s_in.get("layout_refine_megapix", max(0.4, 2.0 * float(step_meta.get("layout_megapix", 0.2)))))  # type: ignore[arg-type]
                except Exception:
                    refine_megapix = 0.6
                try:
                    refine_patch = int(s_in.get("layout_refine_patch", 384))
                except Exception:
                    refine_patch = 384
                try:
                    refine_resp_thresh = float(s_in.get("layout_refine_resp_thresh", 0.15))
                except Exception:
                    refine_resp_thresh = 0.15
                try:
                    refine_max_correction_px = float(s_in.get("layout_refine_max_correction_px", 25.0))
                except Exception:
                    refine_max_correction_px = 25.0
                try:
                    refine_prior_weight = float(s_in.get("layout_refine_prior_weight", 0.01))
                except Exception:
                    refine_prior_weight = 0.01
                try:
                    refine_max_edges = int(s_in.get("layout_refine_max_edges", 0))
                except Exception:
                    refine_max_edges = 0

                refine_megapix = max(0.05, min(float(refine_megapix), float(orig_mp)))
                refine_patch = max(128, min(1024, int(refine_patch)))
                refine_resp_thresh = max(0.0, min(1.0, float(refine_resp_thresh)))
                refine_max_correction_px = max(1.0, float(refine_max_correction_px))
                refine_prior_weight = max(0.0, float(refine_prior_weight))
                refine_max_edges = max(0, int(refine_max_edges))

                scale_refine = float(_scale_for_mp(float(refine_megapix)))
                if float(scale_refine) <= 1e-9:
                    scale_refine = float(scale_final)
                factor = float(scale_refine) / float(scale_final) if float(scale_final) > 1e-9 else 1.0
                if float(factor) <= 1e-9:
                    factor = 1.0
                w_ref = max(1, int(round(float(tile_w) * float(scale_refine))))
                h_ref = max(1, int(round(float(tile_h) * float(scale_refine))))

                @lru_cache(maxsize=256)
                def _load_gray_ref(path: str) -> "np.ndarray | None":
                    img0 = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
                    if img0 is None:
                        return None
                    if int(img0.shape[1]) != int(w_ref) or int(img0.shape[0]) != int(h_ref):
                        try:
                            img0 = cv2.resize(img0, (int(w_ref), int(h_ref)), interpolation=cv2.INTER_AREA)
                        except Exception:
                            return None
                    return img0

                win_cache: dict[int, "np.ndarray"] = {}

                def _hann(sz: int) -> "np.ndarray | None":
                    if int(sz) <= 0:
                        return None
                    cached = win_cache.get(int(sz))
                    if cached is not None:
                        return cached
                    try:
                        w2 = cv2.createHanningWindow((int(sz), int(sz)), cv2.CV_32F)
                    except Exception:
                        return None
                    win_cache[int(sz)] = w2
                    return w2

                def _edge_measure(
                    p1: str,
                    p2: str,
                    *,
                    dx_pred_final: float,
                    dy_pred_final: float,
                ) -> tuple[float, float, float] | None:
                    img1u = _load_gray_ref(str(p1))
                    img2u = _load_gray_ref(str(p2))
                    if img1u is None or img2u is None:
                        return None
                    dx_pred = float(dx_pred_final) * float(factor)
                    dy_pred = float(dy_pred_final) * float(factor)

                    w0 = int(img1u.shape[1])
                    h0 = int(img1u.shape[0])

                    if dx_pred >= 0:
                        ox0 = int(round(dx_pred))
                        ox1 = int(w0)
                    else:
                        ox0 = 0
                        ox1 = int(w0 + int(round(dx_pred)))
                    if dy_pred >= 0:
                        oy0 = int(round(dy_pred))
                        oy1 = int(h0)
                    else:
                        oy0 = 0
                        oy1 = int(h0 + int(round(dy_pred)))
                    ox1 = max(int(ox0), min(int(w0), int(ox1)))
                    oy1 = max(int(oy0), min(int(h0), int(oy1)))
                    ow = int(ox1 - ox0)
                    oh = int(oy1 - oy0)
                    if int(ow) < 64 or int(oh) < 64:
                        return None

                    psz = int(min(int(refine_patch), int(ow), int(oh)))
                    if int(psz) < 128:
                        return None

                    cx = int(ox0 + (ow // 2) - (psz // 2))
                    cy = int(oy0 + (oh // 2) - (psz // 2))
                    cx = max(0, min(int(w0 - psz), int(cx)))
                    cy = max(0, min(int(h0 - psz), int(cy)))

                    x2 = int(round(float(cx) - float(dx_pred)))
                    y2 = int(round(float(cy) - float(dy_pred)))
                    if x2 < 0 or y2 < 0 or (x2 + int(psz)) > int(img2u.shape[1]) or (y2 + int(psz)) > int(img2u.shape[0]):
                        return None

                    p1u = img1u[int(cy) : int(cy + psz), int(cx) : int(cx + psz)]
                    p2u = img2u[int(y2) : int(y2 + psz), int(x2) : int(x2 + psz)]
                    if p1u.size == 0 or p2u.size == 0:
                        return None

                    # Skip nearly-flat patches (phase correlation becomes unstable).
                    try:
                        if float(p1u.std()) < 6.0 or float(p2u.std()) < 6.0:
                            return None
                    except Exception:
                        pass

                    a = p1u.astype(np.float32)
                    b = p2u.astype(np.float32)
                    try:
                        a = a - float(a.mean())
                        b = b - float(b.mean())
                    except Exception:
                        pass
                    w2 = _hann(int(psz))
                    if w2 is not None:
                        try:
                            a = a * w2
                            b = b * w2
                        except Exception:
                            pass
                    try:
                        (sx, sy), resp = cv2.phaseCorrelate(a, b)
                    except Exception:
                        return None
                    if not math.isfinite(float(resp)) or float(resp) < float(refine_resp_thresh):
                        return None
                    if not (math.isfinite(float(sx)) and math.isfinite(float(sy))):
                        return None

                    corr_x = (-float(sx)) / float(factor)
                    corr_y = (-float(sy)) / float(factor)
                    if abs(float(corr_x)) > float(refine_max_correction_px) or abs(float(corr_y)) > float(refine_max_correction_px):
                        return None

                    dx_meas = float(dx_pred_final) + float(corr_x)
                    dy_meas = float(dy_pred_final) + float(corr_y)
                    wgt = max(0.05, min(1.0, float(resp)))
                    return float(dx_meas), float(dy_meas), float(wgt)

                # Build edge list.
                idx_by_rc: dict[tuple[int, int], int] = {(int(e.row), int(e.col)): int(i) for i, e in enumerate(entries)}
                srcs: list[int] = []
                dsts: list[int] = []
                ws: list[float] = []
                dxs: list[float] = []
                dys: list[float] = []

                def _add_edge(i: int, j: int, dx: float, dy: float, wgt: float) -> None:
                    srcs.append(int(i))
                    dsts.append(int(j))
                    ws.append(float(wgt))
                    dxs.append(float(dx))
                    dys.append(float(dy))

                # Candidate neighbor edges: right and down in the grid.
                candidates: list[tuple[int, int, float, float]] = []
                for e in entries:
                    r = int(e.row)
                    c = int(e.col)
                    i = idx_by_rc.get((int(r), int(c)))
                    if i is None:
                        continue
                    j = idx_by_rc.get((int(r), int(c + 1)))
                    if j is not None:
                        candidates.append((int(i), int(j), float(v_col[0]), float(v_col[1])))
                    k = idx_by_rc.get((int(r + 1), int(c)))
                    if k is not None:
                        stepv = v_row
                        if v_row_even is not None and v_row_odd is not None:
                            stepv = v_row_even if (int(r) % 2) == 0 else v_row_odd
                        candidates.append((int(i), int(k), float(stepv[0]), float(stepv[1])))

                if int(refine_max_edges) > 0 and int(len(candidates)) > int(refine_max_edges):
                    import random

                    rng = random.Random(int(s_in.get("layout_seed", 0)))
                    rng.shuffle(candidates)
                    candidates = candidates[: int(refine_max_edges)]

                measured = 0
                for idx_edge, (i, j, dx_pred, dy_pred) in enumerate(candidates):
                    p1 = str(entries[int(i)].path)
                    p2 = str(entries[int(j)].path)
                    meas = _edge_measure(p1, p2, dx_pred_final=float(dx_pred), dy_pred_final=float(dy_pred))
                    if meas is not None:
                        dx_m, dy_m, wgt = meas
                        measured += 1
                        _add_edge(int(i), int(j), float(dx_m), float(dy_m), float(wgt))
                    else:
                        # Keep a weak prior edge so the graph stays connected even through blank areas.
                        _add_edge(int(i), int(j), float(dx_pred), float(dy_pred), 0.02)
                    if (idx_edge % 200) == 0 and (idx_edge + 1) < int(len(candidates)):
                        frac = float(idx_edge + 1) / float(max(1, int(len(candidates))))
                        _emit_progress(7.0 + (2.0 * frac), f"Stitching: refining positions ({idx_edge + 1}/{len(candidates)})…")

                n_nodes = int(len(entries))
                src = np.asarray(srcs, dtype=np.int32)
                dst = np.asarray(dsts, dtype=np.int32)
                wts = np.asarray(ws, dtype=np.float64)
                dx_arr = np.asarray(dxs, dtype=np.float64)
                dy_arr = np.asarray(dys, dtype=np.float64)

                # Right-hand sides.
                bx = np.bincount(src, -wts * dx_arr, minlength=int(n_nodes)) + np.bincount(dst, wts * dx_arr, minlength=int(n_nodes))
                by = np.bincount(src, -wts * dy_arr, minlength=int(n_nodes)) + np.bincount(dst, wts * dy_arr, minlength=int(n_nodes))

                # Soft prior to the initial grid positions to prevent drift in low-feature regions.
                prior_x = np.zeros(int(n_nodes), dtype=np.float64)
                prior_y = np.zeros(int(n_nodes), dtype=np.float64)
                for e in entries:
                    i = idx_by_rc.get((int(e.row), int(e.col)))
                    if i is None:
                        continue
                    px0, py0 = pos0_by_rc[(int(e.row), int(e.col))]
                    prior_x[int(i)] = float(px0)
                    prior_y[int(i)] = float(py0)
                diag = np.full(int(n_nodes), float(refine_prior_weight), dtype=np.float64) if float(refine_prior_weight) > 0 else np.zeros(int(n_nodes), dtype=np.float64)
                bx = bx + (diag * prior_x)
                by = by + (diag * prior_y)

                anchor = 0
                anchor_w = 1e6

                def _matvec(x: "np.ndarray") -> "np.ndarray":
                    diff = wts * (x[src] - x[dst])
                    y = np.bincount(src, diff, minlength=int(n_nodes)) + np.bincount(dst, -diff, minlength=int(n_nodes))
                    if diag is not None and float(refine_prior_weight) > 0:
                        y = y + (diag * x)
                    y[int(anchor)] += float(anchor_w) * float(x[int(anchor)])
                    return y

                def _cg(b: "np.ndarray", *, max_iter: int = 400, tol: float = 1e-4) -> tuple["np.ndarray", int, float]:
                    x = np.zeros_like(b, dtype=np.float64)
                    r = b - _matvec(x)
                    p = r.copy()
                    rs = float(np.dot(r, r))
                    if not math.isfinite(float(rs)) or float(rs) <= 0:
                        return x, 0, float(rs)
                    for it in range(int(max_iter)):
                        Ap = _matvec(p)
                        denom = float(np.dot(p, Ap))
                        if abs(float(denom)) <= 1e-12:
                            break
                        a = float(rs) / float(denom)
                        x = x + (a * p)
                        r = r - (a * Ap)
                        rs2 = float(np.dot(r, r))
                        if math.sqrt(float(rs2)) <= float(tol):
                            rs = rs2
                            return x, int(it + 1), float(rs)
                        p = r + ((rs2 / rs) * p)
                        rs = rs2
                    return x, int(max_iter), float(rs)

                x_sol, itx, rx = _cg(bx, max_iter=500, tol=1e-3)
                y_sol, ity, ry = _cg(by, max_iter=500, tol=1e-3)

                refined_by_rc = {}
                for e in entries:
                    i = idx_by_rc.get((int(e.row), int(e.col)))
                    if i is None:
                        continue
                    refined_by_rc[(int(e.row), int(e.col))] = (float(x_sol[int(i)]), float(y_sol[int(i)]))

                refine_meta = {
                    "enabled": True,
                    "refine_megapix": float(refine_megapix),
                    "refine_patch": int(refine_patch),
                    "refine_resp_thresh": float(refine_resp_thresh),
                    "refine_max_correction_px": float(refine_max_correction_px),
                    "refine_prior_weight": float(refine_prior_weight),
                    "edges_total": int(len(candidates)),
                    "edges_measured": int(measured),
                    "cg_iters_x": int(itx),
                    "cg_iters_y": int(ity),
                    "cg_resid_x": float(rx),
                    "cg_resid_y": float(ry),
                }
            else:
                refine_meta = {"enabled": False}

            pos_by_rc_f: dict[tuple[int, int], tuple[float, float]] = refined_by_rc if refined_by_rc is not None else pos0_by_rc

            # Compute output bounds.
            min_x = float("inf")
            min_y = float("inf")
            max_x = float("-inf")
            max_y = float("-inf")
            for _rc, (px, py) in pos_by_rc_f.items():
                min_x = min(min_x, float(px))
                min_y = min(min_y, float(py))
                max_x = max(max_x, float(px))
                max_y = max(max_y, float(py))

            out_w = int(math.ceil(float(max_x - min_x) + float(w_final)))
            out_h = int(math.ceil(float(max_y - min_y) + float(h_final)))
            if out_w <= 0 or out_h <= 0:
                raise RuntimeError("Affine layout failed: invalid output canvas size.")
            max_px = int(float(s_in.get("max_panorama_pixels", 2_000_000_000)))
            area = int(out_w) * int(out_h)
            if int(max_px) > 0 and int(area) > int(max_px):
                raise RuntimeError(
                    f"Affine layout would be too large ({out_w}x{out_h} px). "
                    "Lower final_megapix, scan a smaller area, or increase max_panorama_pixels (expect huge RAM/disk)."
                )

            strategy_settings["layout_refine_positions"] = dict(refine_meta)
            _emit_progress(10.0, f"Stitching: compositing {tile_count} tiles…")
            try:
                pano_bytes = int(area) * 3
            except Exception:
                pano_bytes = 0
            try:
                inmem_max_bytes = int(float(s_in.get("in_memory_max_bytes", 1.5 * 1024 * 1024 * 1024)))
            except Exception:
                inmem_max_bytes = int(1.5 * 1024 * 1024 * 1024)
            use_memmap = bool(s_in.get("use_memmap", True))
            if int(pano_bytes) > int(inmem_max_bytes):
                if not bool(use_memmap):
                    raise RuntimeError(
                        f"Panorama canvas is too large for in-memory composition (~{pano_bytes / (1024**3):.1f} GiB). "
                        "Enable use_memmap or lower final_megapix."
                    )
                memmap_path = os.path.join(out_dir, "_mosaic_memmap.dat")
                try:
                    if os.path.exists(memmap_path):
                        os.remove(memmap_path)
                except Exception:
                    pass
                _emit_progress(9.0, f"Stitching: allocating scratch canvas (~{pano_bytes / (1024**3):.1f} GiB)…")
                try:
                    pano = np.memmap(memmap_path, dtype=np.uint8, mode="w+", shape=(int(out_h), int(out_w), 3))
                except Exception as exc:
                    _write_err("memmap_alloc", exc)
                    if is_no_space_error(exc):
                        raise RuntimeError(
                            "No space left on device while allocating the scratch canvas for full-res stitching. "
                            "Free disk space, choose a different output folder, or lower final_megapix."
                        ) from exc
                    raise RuntimeError(
                        f"Failed to allocate scratch canvas for full-res stitching: {exc} (path: {memmap_path})"
                    ) from exc
            else:
                pano = np.zeros((int(out_h), int(out_w), 3), dtype=np.uint8)

            pos_by_rc: dict[tuple[int, int], tuple[int, int]] = {}
            for e in entries:
                px, py = pos_by_rc_f.get((int(e.row), int(e.col)), (None, None))  # type: ignore[assignment]
                if px is None or py is None:
                    continue
                x = int(round(float(px) - float(min_x)))
                y = int(round(float(py) - float(min_y)))
                pos_by_rc[(int(e.row), int(e.col))] = (int(x), int(y))

            def _feather_overlap_inplace(
                *,
                pano_view: "np.ndarray",
                new_view: "np.ndarray",
                dx: int,
                dy: int,
                feather: int,
            ) -> None:
                """
                Blend new_view onto pano_view within their overlapping ROI, keeping the neighbor
                (existing) pixels on one side and the new pixels on the other, with a feather band
                around the overlap center. Operates in-place on pano_view.
                """
                oh, ow = pano_view.shape[:2]
                if oh <= 0 or ow <= 0:
                    return
                if abs(int(dx)) >= abs(int(dy)):
                    axis = "x"
                    sign = 1 if int(dx) >= 0 else -1
                    extent = int(ow)
                else:
                    axis = "y"
                    sign = 1 if int(dy) >= 0 else -1
                    extent = int(oh)
                if extent <= 0:
                    return
                f = max(0, min(int(feather), int(extent // 2)))
                if int(f) <= 0:
                    pano_view[:, :] = new_view
                    return
                center = int(extent // 2)
                blend0 = max(0, int(center - f))
                blend1 = min(int(extent), int(center + f))
                if blend1 <= blend0:
                    pano_view[:, :] = new_view
                    return

                if axis == "x":
                    # Overlap is a vertical strip; seam is vertical.
                    if sign > 0:
                        # current tile is to the right; keep neighbor on left side.
                        pano_view[:, int(blend1) :] = new_view[:, int(blend1) :]
                        a = np.linspace(0.0, 1.0, int(blend1 - blend0), dtype=np.float32)
                    else:
                        # current tile is to the left; keep neighbor on right side.
                        pano_view[:, : int(blend0)] = new_view[:, : int(blend0)]
                        a = np.linspace(1.0, 0.0, int(blend1 - blend0), dtype=np.float32)
                    a2 = a[None, :, None]
                    old_band = pano_view[:, int(blend0) : int(blend1)].astype(np.float32)
                    new_band = new_view[:, int(blend0) : int(blend1)].astype(np.float32)
                    out = (old_band * (1.0 - a2)) + (new_band * a2)
                    pano_view[:, int(blend0) : int(blend1)] = np.clip(out, 0, 255).astype(np.uint8)
                else:
                    # Overlap is a horizontal strip; seam is horizontal.
                    if sign > 0:
                        # current tile is below; keep neighbor on top side.
                        pano_view[int(blend1) :, :] = new_view[int(blend1) :, :]
                        a = np.linspace(0.0, 1.0, int(blend1 - blend0), dtype=np.float32)
                    else:
                        # current tile is above; keep neighbor on bottom side.
                        pano_view[: int(blend0), :] = new_view[: int(blend0), :]
                        a = np.linspace(1.0, 0.0, int(blend1 - blend0), dtype=np.float32)
                    a2 = a[:, None, None]
                    old_band = pano_view[int(blend0) : int(blend1), :].astype(np.float32)
                    new_band = new_view[int(blend0) : int(blend1), :].astype(np.float32)
                    out = (old_band * (1.0 - a2)) + (new_band * a2)
                    pano_view[int(blend0) : int(blend1), :] = np.clip(out, 0, 255).astype(np.uint8)

            try:
                layout_blend_radius = int(s_in.get("layout_blend_radius", 2))
            except Exception:
                layout_blend_radius = 2
            layout_blend_radius = max(1, min(4, int(layout_blend_radius)))
            strategy_settings["layout_blend_radius"] = int(layout_blend_radius)

            for i, e in enumerate(entries):
                img = cv2.imread(str(e.path), cv2.IMREAD_COLOR)
                if img is None:
                    continue
                if int(img.shape[1]) != int(w_final) or int(img.shape[0]) != int(h_final):
                    try:
                        img = cv2.resize(img, (int(w_final), int(h_final)), interpolation=cv2.INTER_AREA)
                    except Exception:
                        continue

                x, y = pos_by_rc.get((int(e.row), int(e.col)), (None, None))  # type: ignore[assignment]
                if x is None or y is None:
                    continue
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
                elif blend_mode == "feather":
                    # Feather seams against already-placed tiles using a local overlap blend. We allow
                    # blending against more than just left/top neighbors because skewed scan lattices
                    # can produce large diagonal overlaps.
                    fpx = max(0, min(int(feather_px), int(min(w_final, h_final) // 2)))

                    x0 = int(x)
                    y0 = int(y)
                    x1 = int(x + w_final)
                    y1 = int(y + h_final)

                    # First, fill any still-empty pixels (non-overlap) directly.
                    roi0 = pano[int(y0) : int(y1), int(x0) : int(x1)]
                    try:
                        empty = (roi0.sum(axis=2) == 0)  # type: ignore[call-arg]
                        if empty.any():
                            roi0[empty] = img[empty]
                    except Exception:
                        pano[int(y0) : int(y1), int(x0) : int(x1)] = img
                        empty = None  # type: ignore[assignment]

                    # Then blend overlaps against already-placed neighbors within a small radius.
                    if int(fpx) > 0:
                        r0 = int(e.row)
                        c0 = int(e.col)
                        for dr in range(-int(layout_blend_radius), 1):
                            for dc in range(-int(layout_blend_radius), int(layout_blend_radius) + 1):
                                if int(dr) == 0 and int(dc) >= 0:
                                    continue
                                if int(dr) == 0 and int(dc) == 0:
                                    continue
                                nb = pos_by_rc.get((int(r0 + dr), int(c0 + dc)))
                                if nb is None:
                                    continue
                                nb_x, nb_y = nb
                                ox0 = max(int(x0), int(nb_x))
                                oy0 = max(int(y0), int(nb_y))
                                ox1 = min(int(x1), int(nb_x + w_final))
                                oy1 = min(int(y1), int(nb_y + h_final))
                                if ox1 <= ox0 or oy1 <= oy0:
                                    continue
                                pano_view = pano[int(oy0) : int(oy1), int(ox0) : int(ox1)]
                                try:
                                    if (pano_view.sum(axis=2) == 0).all():  # type: ignore[call-arg]
                                        continue
                                except Exception:
                                    pass
                                new_view = img[int(oy0 - y0) : int(oy1 - y0), int(ox0 - x0) : int(ox1 - x0)]
                                _feather_overlap_inplace(
                                    pano_view=pano_view,
                                    new_view=new_view,
                                    dx=int(x0 - nb_x),
                                    dy=int(y0 - nb_y),
                                    feather=int(fpx),
                                )
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
        H = int(out_h)
        W = int(out_w)

        # Estimate output DPI (PPI) metadata so stitched outputs can be combined in external tools
        # without manual scaling. If we're writing via libvips, we set xres/yres at save time to
        # avoid rewriting pyramid TIFFs.
        px_per_mm_target: float | None = None
        dpi_meta: dict[str, object] | None = None
        try:
            override_dpi = None
            try:
                override_dpi = s_in.get("output_dpi", None)
            except Exception:
                override_dpi = None
            if override_dpi is not None:
                try:
                    dpi0 = float(override_dpi)
                except Exception:
                    dpi0 = None
                if dpi0 is not None and math.isfinite(float(dpi0)) and float(dpi0) > 0:
                    px_per_mm_target = float(dpi0) / 25.4
                    dpi_meta = {
                        "dpi_x": float(dpi0),
                        "dpi_y": float(dpi0),
                        "mode": "override",
                        "set_in_file": False,
                    }

            if dpi_meta is None:
                step_vecs = None
                if (
                    isinstance(strategy_settings, dict)
                    and "step_col_px" in strategy_settings
                    and "step_row_px" in strategy_settings
                ):
                    step_vecs = strategy_settings
                if step_vecs is None:
                    step_vecs = _estimate_step_vectors(final_megapix=float(stage1_final_megapix), min_kept=1)

                if step_vecs is not None:
                    vx, vy = step_vecs.get("step_col_px", [0.0, 0.0])  # type: ignore[assignment]

                    row_vecs: list[tuple[float, float]] = []
                    if "step_row_px_even" in step_vecs and "step_row_px_odd" in step_vecs:
                        try:
                            row_vecs.append(
                                (
                                    float(step_vecs.get("step_row_px_even")[0]),  # type: ignore[index]
                                    float(step_vecs.get("step_row_px_even")[1]),  # type: ignore[index]
                                )
                            )
                            row_vecs.append(
                                (
                                    float(step_vecs.get("step_row_px_odd")[0]),  # type: ignore[index]
                                    float(step_vecs.get("step_row_px_odd")[1]),  # type: ignore[index]
                                )
                            )
                        except Exception:
                            row_vecs = []
                    if not row_vecs:
                        wx, wy = step_vecs.get("step_row_px", [0.0, 0.0])  # type: ignore[assignment]
                        row_vecs.append((float(wx), float(wy)))

                    col_mag = math.hypot(float(vx), float(vy))
                    row_mag = 0.0
                    if row_vecs:
                        row_mag = float(sum(math.hypot(float(a), float(b)) for a, b in row_vecs)) / float(len(row_vecs))

                    try:
                        n_col = int(step_vecs.get("step_col_samples_kept", 0) or 0)  # type: ignore[union-attr]
                    except Exception:
                        n_col = 0
                    try:
                        n_row = int(step_vecs.get("step_row_samples_kept", 0) or 0)  # type: ignore[union-attr]
                    except Exception:
                        n_row = 0

                    px_per_mm_x = None
                    px_per_mm_y = None
                    if int(n_col) > 0 and step_x_mm and float(step_x_mm) > 0 and float(col_mag) > 1e-6:
                        px_per_mm_x = float(col_mag) / float(step_x_mm)
                    if int(n_row) > 0 and step_y_mm and float(step_y_mm) > 0 and float(row_mag) > 1e-6:
                        px_per_mm_y = float(row_mag) / float(step_y_mm)

                    if px_per_mm_x is None and px_per_mm_y is not None:
                        px_per_mm_x = float(px_per_mm_y)
                    if px_per_mm_y is None and px_per_mm_x is not None:
                        px_per_mm_y = float(px_per_mm_x)

                    px_per_mm = None
                    if px_per_mm_x is not None and px_per_mm_y is not None:
                        px_per_mm = 0.5 * (float(px_per_mm_x) + float(px_per_mm_y))
                    elif px_per_mm_x is not None:
                        px_per_mm = float(px_per_mm_x)
                    elif px_per_mm_y is not None:
                        px_per_mm = float(px_per_mm_y)

                    if px_per_mm is not None:
                        try:
                            inc = float(s_in.get("dpi_round_px_per_mm", 0.0))
                        except Exception:
                            inc = 0.0
                        if float(inc) > 1e-9:
                            px_per_mm = float(round(float(px_per_mm) / float(inc)) * float(inc))

                        px_per_mm_target = float(px_per_mm)
                        dpi = float(px_per_mm_target) * 25.4
                        dpi_meta = {
                            "dpi_x": float(dpi),
                            "dpi_y": float(dpi),
                            "mode": "estimated",
                            "px_per_mm": float(px_per_mm_target),
                            "px_per_mm_x": float(px_per_mm_x),
                            "px_per_mm_y": float(px_per_mm_y),
                            "step_x_mm": float(step_x_mm) if step_x_mm is not None else None,
                            "step_y_mm": float(step_y_mm) if step_y_mm is not None else None,
                            "set_in_file": False,
                        }
        except Exception:
            px_per_mm_target = None
            dpi_meta = None

        # Write mosaic.
        try:
            did_set = False
            try:
                if hasattr(pano, "flush"):
                    pano.flush()  # type: ignore[union-attr]
            except Exception:
                pass

            if memmap_path is not None:
                import pyvips  # type: ignore

                vimg = pyvips.Image.rawload(memmap_path, int(out_w), int(out_h), 3, format="uchar")
                # cv2 uses BGR; write standard RGB.
                vimg = vimg[2].bandjoin([vimg[1], vimg[0]])
                # macOS Preview/QuickLook can render huge tiled TIFFs incorrectly on some systems.
                # Default to strip-based output for very large mosaics unless the user forces tiling.
                tiff_tile_pref = None
                try:
                    tiff_tile_pref = s_in.get("tiff_tile", None)
                except Exception:
                    tiff_tile_pref = None
                if tiff_tile_pref is None:
                    tiff_tile = not (sys.platform == "darwin" and (int(out_w) > 16000 or int(out_h) > 16000))
                else:
                    tiff_tile = bool(tiff_tile_pref)

                predictor_pref = None
                try:
                    predictor_pref = s_in.get("tiff_predictor", None)
                except Exception:
                    predictor_pref = None
                predictor = "horizontal"
                if predictor_pref is not None:
                    predictor = str(predictor_pref).strip().lower() or "horizontal"
                if predictor not in {"none", "horizontal", "float"}:
                    predictor = "horizontal"

                save_kwargs: dict[str, object] = {
                    "compression": str(tiff_compression or "none").strip().lower() or "none",
                    "predictor": str(predictor),
                    "tile": bool(tiff_tile),
                }
                if bool(tiff_tile):
                    save_kwargs["tile_width"] = int(s_in.get("tiff_tile_width", 256) or 256)
                    save_kwargs["tile_height"] = int(s_in.get("tiff_tile_height", 256) or 256)
                    save_kwargs["tile_width"] = max(64, min(2048, int(save_kwargs["tile_width"])))
                    save_kwargs["tile_height"] = max(64, min(2048, int(save_kwargs["tile_height"])))
                if px_per_mm_target is not None and math.isfinite(float(px_per_mm_target)) and float(px_per_mm_target) > 0:
                    save_kwargs["xres"] = float(px_per_mm_target)
                    save_kwargs["yres"] = float(px_per_mm_target)
                    save_kwargs["resunit"] = "inch"
                    did_set = True
                try:
                    vimg.tiffsave(mosaic_path, **save_kwargs)
                except Exception as exc:
                    if is_no_space_error(exc):
                        raise
                    msg = str(exc).lower()
                    if not any(k in msg for k in ("bigtiff", "big tiff", "too large", "4gb", "offset")):
                        raise
                    # Retry with BigTIFF only if needed. Many tools have better compatibility with classic TIFF.
                    try:
                        if os.path.exists(mosaic_path):
                            os.remove(mosaic_path)
                    except Exception:
                        pass
                    save_kwargs["bigtiff"] = True
                    vimg.tiffsave(mosaic_path, **save_kwargs)
            else:
                imwrite(cv2, mosaic_path, pano, tiff_params)
                if dpi_meta is not None and "dpi_x" in dpi_meta and "dpi_y" in dpi_meta:
                    try:
                        did_set = _try_set_dpi(
                            mosaic_path, dpi_x=float(dpi_meta["dpi_x"]), dpi_y=float(dpi_meta["dpi_y"])  # type: ignore[arg-type]
                        )
                    except Exception:
                        did_set = False

            if dpi_meta is not None:
                dpi_meta["set_in_file"] = bool(did_set)
        except Exception as exc:
            _write_err("final_write", exc)
            if is_no_space_error(exc):
                raise RuntimeError(
                    "No space left on device while writing mosaic_full.tif. Free disk space or choose a different output folder."
                ) from exc
            raise

        if memmap_path is not None:
            try:
                del pano
            except Exception:
                pass
            gc.collect()
            try:
                if os.path.exists(memmap_path):
                    os.remove(memmap_path)
            except Exception:
                pass
            memmap_path = None

        # Always emit a JPEG preview for quick sanity-checking.
        try:
            preview_max_dim = int(s_in.get("preview_max_dim", 2000))
        except Exception:
            preview_max_dim = 2000
        try:
            preview_quality = int(s_in.get("preview_quality", 85))
        except Exception:
            preview_quality = 85
        preview_max_dim = max(256, min(8000, int(preview_max_dim)))
        preview_quality = max(30, min(95, int(preview_quality)))
        try:
            import pyvips  # type: ignore

            img_prev = pyvips.Image.new_from_file(mosaic_path, access="sequential")
            thumb = img_prev.thumbnail_image(int(preview_max_dim))
            thumb_path = os.path.join(out_dir, f"mosaic_thumb_{preview_max_dim}.jpg")
            thumb.write_to_file(thumb_path, Q=int(preview_quality))
        except Exception:
            pass

        try:
            max_px_meta = int(float(s_in.get("max_panorama_pixels", 2_000_000_000)))
        except Exception:
            max_px_meta = 2_000_000_000
        try:
            use_memmap_meta = bool(s_in.get("use_memmap", True))
        except Exception:
            use_memmap_meta = True

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
                "max_panorama_pixels": int(max_px_meta),
                "use_memmap": bool(use_memmap_meta),
                "final_megapix": float(stage1_final_megapix),
                "stitcher": dict(base_stitcher_settings),
                "orb_fast_threshold": orb_fast_threshold,
                "strategy_settings": dict(strategy_settings),
            },
            "versions": {
                "opencv": str(getattr(cv2, "__version__", "?")),
            },
        }
        if dpi_meta is not None:
            meta["dpi"] = dict(dpi_meta)
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
        if memmap_path is not None:
            try:
                if os.path.exists(memmap_path):
                    os.remove(memmap_path)
            except Exception:
                pass
        _write_err("stitch", exc)
        raise
