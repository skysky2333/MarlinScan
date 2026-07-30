from __future__ import annotations

import math
from typing import Any, Callable

from ...progress import ProgressCallback
from .util import median, scale_for_megapix


def estimate_step_vectors(
    *,
    cv2: Any,
    np: Any,
    by_rc: dict[tuple[int, int], str],
    nrows: int,
    ncols: int,
    tile_w: int,
    tile_h: int,
    orig_mp: float,
    serpentine: bool | None,
    settings: dict[str, object],
    final_megapix: float,
    min_kept: int,
    progress_cb: ProgressCallback | None,
    cancel_cb: Callable[[], None] | None,
) -> dict[str, object] | None:
    """
    Estimate per-step translation vectors between neighboring tiles, scaled to the requested
    final resolution. Returns a dict containing:

    - step_col_px / step_row_px (dx, dy) at final resolution
    - (optional) step_row_px_even / step_row_px_odd for serpentine row parity
    - layout_* parameters and sampling stats
    """
    import random

    layout_seed = int(settings.get("layout_seed", 0))
    layout_megapix = float(settings.get("layout_megapix", max(float(final_megapix), 0.2)))
    layout_samples = int(settings.get("layout_samples", 250))
    layout_nfeatures = int(settings.get("layout_nfeatures", 2000))
    layout_orb_fast_threshold = int(settings.get("layout_orb_fast_threshold", 10))
    layout_ratio_test = float(settings.get("layout_ratio_test", 0.75))
    layout_ransac_thresh = float(settings.get("layout_ransac_thresh", 3.0))
    layout_min_inliers = int(settings.get("layout_min_inliers", 10))

    scale_final = float(scale_for_megapix(orig_mp=float(orig_mp), target_mp=float(final_megapix)))
    scale_match = float(scale_for_megapix(orig_mp=float(orig_mp), target_mp=float(layout_megapix)))
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

    feat_cache: dict[str, tuple[object | None, object | None]] = {}
    gray_cache: dict[str, object | None] = {}
    try:
        pc_window = cv2.createHanningWindow((int(w_match), int(h_match)), cv2.CV_32F)
    except Exception:
        pc_window = None

    def _get_orb(path: str) -> tuple[object | None, object | None]:
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

    def _get_gray(path: str) -> object | None:
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
        mx = median(xs)
        my = median(ys)
        dists = [math.hypot(float(x) - float(mx), float(y) - float(my)) for x, y in vecs]
        md = median(dists)
        mad = median([abs(float(d) - float(md)) for d in dists])
        thr = float(md) + (6.0 * max(1.0, float(mad)))
        kept = [(x, y) for (x, y), d in zip(vecs, dists) if float(d) <= float(thr)]
        if not kept:
            return float(mx), float(my), 0
        xs2 = [float(x) for x, _y in kept]
        ys2 = [float(y) for _x, y in kept]
        return float(median(xs2)), float(median(ys2)), int(len(kept))

    right_candidates = [(r, c) for r in range(int(nrows)) for c in range(int(ncols) - 1)]
    down_candidates = [(r, c) for r in range(int(nrows) - 1) for c in range(int(ncols))]
    rng.shuffle(right_candidates)
    rng.shuffle(down_candidates)
    right_candidates = right_candidates[: max(1, int(layout_samples))]
    down_candidates = down_candidates[: max(1, int(layout_samples))]
    sampled_pairs = [
        (str(by_rc[(r, c)]), str(by_rc[(r, c + 1)]), r, c, "right")
        for r, c in right_candidates
        if (r, c) in by_rc and (r, c + 1) in by_rc
    ] + [
        (str(by_rc[(r, c)]), str(by_rc[(r + 1, c)]), r, c, "down")
        for r, c in down_candidates
        if (r, c) in by_rc and (r + 1, c) in by_rc
    ]
    if progress_cb is not None:
        progress_cb("stitch-align", "Aligning neighboring tiles", 0, len(sampled_pairs), "pairs")

    right_vecs: list[tuple[float, float]] = []
    down_vecs: list[tuple[float, float]] = []
    down_vecs_even: list[tuple[float, float]] = []
    down_vecs_odd: list[tuple[float, float]] = []

    for completed, (p1, p2, r, _c, direction) in enumerate(sampled_pairs, start=1):
        if cancel_cb is not None:
            cancel_cb()
        est = _estimate_pair_shift(p1, p2)
        if est is not None:
            dxdy = (float(est[0]), float(est[1]))
            if direction == "right":
                right_vecs.append(dxdy)
            else:
                down_vecs.append(dxdy)
                if (int(r) % 2) == 0:
                    down_vecs_even.append(dxdy)
                else:
                    down_vecs_odd.append(dxdy)
        if progress_cb is not None:
            progress_cb(
                "stitch-align",
                "Aligning neighboring tiles",
                completed,
                len(sampled_pairs),
                "pairs",
            )

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
    # significant, keep separate step vectors for even/odd row transitions.
    parity_pref = settings.get("layout_row_parity", None)
    prefer_parity = bool(parity_pref) if parity_pref is not None else bool(serpentine)
    parity_min_kept = int(settings.get("layout_row_parity_min_kept", 20))
    parity_used = False
    if bool(prefer_parity) and int(n_row_even) >= int(parity_min_kept) and int(n_row_odd) >= int(parity_min_kept):
        diff = math.hypot(float(v_row_even_x) - float(v_row_odd_x), float(v_row_even_y) - float(v_row_odd_y))
        if math.isfinite(float(diff)) and float(diff) >= (0.01 * float(max(w_final, h_final))):
            parity_used = True

    out: dict[str, object] = {
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
