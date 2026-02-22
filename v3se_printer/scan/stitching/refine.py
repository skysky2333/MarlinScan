from __future__ import annotations

import gc
import math
from typing import Any, Callable

from .types import Entry
from .util import scale_for_megapix


def refine_positions_and_gains(
    *,
    cv2: Any,
    np: Any,
    entries: list[Entry],
    pos0_by_rc: dict[tuple[int, int], tuple[float, float]],
    v_col: tuple[float, float],
    v_row: tuple[float, float],
    v_row_even: tuple[float, float] | None,
    v_row_odd: tuple[float, float] | None,
    tile_w: int,
    tile_h: int,
    orig_mp: float,
    scale_final: float,
    step_meta: dict[str, object],
    settings: dict[str, object],
    progress_cb: Callable[[float, str], None] | None,
    progress_base: float = 7.0,
    progress_span: float = 2.0,
) -> tuple[dict[tuple[int, int], tuple[float, float]], list[float] | None, dict[str, object]]:
    from functools import lru_cache

    refine_megapix = float(settings.get("layout_refine_megapix", max(0.4, 2.0 * float(step_meta.get("layout_megapix", 0.2)))))
    refine_patch = int(settings.get("layout_refine_patch", 384))
    refine_resp_thresh = float(settings.get("layout_refine_resp_thresh", 0.15))
    refine_max_correction_px = float(settings.get("layout_refine_max_correction_px", 25.0))
    refine_prior_weight = float(settings.get("layout_refine_prior_weight", 0.01))
    refine_max_edges = int(settings.get("layout_refine_max_edges", 0))

    exposure_comp = bool(settings.get("layout_exposure_compensate", True))
    gain_min = float(settings.get("layout_gain_min", 0.5))
    gain_max = float(settings.get("layout_gain_max", 2.0))
    gain_eps = float(settings.get("layout_gain_eps", 1.0))
    gain_prior_weight = float(settings.get("layout_gain_prior_weight", 0.01))
    gain_stride = int(settings.get("layout_gain_stride", 4))

    refine_megapix = max(0.05, min(float(refine_megapix), float(orig_mp)))
    refine_patch = max(128, min(1024, int(refine_patch)))
    refine_resp_thresh = max(0.0, min(1.0, float(refine_resp_thresh)))
    refine_max_correction_px = max(1.0, float(refine_max_correction_px))
    refine_prior_weight = max(0.0, float(refine_prior_weight))
    refine_max_edges = max(0, int(refine_max_edges))

    gain_eps = max(0.0, float(gain_eps))
    gain_prior_weight = max(0.0, float(gain_prior_weight))
    gain_stride = max(1, min(16, int(gain_stride)))
    if not (math.isfinite(float(gain_min)) and math.isfinite(float(gain_max))):
        gain_min, gain_max = 0.5, 2.0
    gain_min = max(1e-3, float(gain_min))
    gain_max = max(float(gain_min), float(gain_max))

    scale_refine = float(scale_for_megapix(orig_mp=float(orig_mp), target_mp=float(refine_megapix)))
    if float(scale_refine) <= 1e-9:
        scale_refine = float(scale_final)
    factor = float(scale_refine) / float(scale_final) if float(scale_final) > 1e-9 else 1.0
    if float(factor) <= 1e-9:
        factor = 1.0
    w_ref = max(1, int(round(float(tile_w) * float(scale_refine))))
    h_ref = max(1, int(round(float(tile_h) * float(scale_refine))))

    @lru_cache(maxsize=256)
    def _load_gray_ref(path: str) -> object | None:
        img0 = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if img0 is None:
            return None
        if int(img0.shape[1]) != int(w_ref) or int(img0.shape[0]) != int(h_ref):
            try:
                img0 = cv2.resize(img0, (int(w_ref), int(h_ref)), interpolation=cv2.INTER_AREA)
            except Exception:
                return None
        return img0

    win_cache: dict[int, object] = {}

    def _hann(sz: int) -> object | None:
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
    ) -> tuple[tuple[float, float, float] | None, tuple[float, float] | None] | None:
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

        gain_meas = None
        if bool(exposure_comp):
            try:
                p1s = p1u[:: int(gain_stride), :: int(gain_stride)]
                p2s = p2u[:: int(gain_stride), :: int(gain_stride)]
                m1 = float(p1s.mean())
                m2 = float(p2s.mean())
                ratio = (float(m1) + float(gain_eps)) / (float(m2) + float(gain_eps))
                if math.isfinite(float(ratio)) and float(ratio) > 0:
                    dg = math.log(float(ratio))
                    max_edge_ratio = max(1.01, float(gain_max) / max(1e-9, float(gain_min)))
                    lo = -math.log(float(max_edge_ratio))
                    hi = math.log(float(max_edge_ratio))
                    dg = float(max(lo, min(hi, float(dg))))
                    gain_meas = (float(dg), 0.2)
            except Exception:
                gain_meas = None

        # Skip nearly-flat patches (phase correlation becomes unstable).
        try:
            if float(p1u.std()) < 6.0 or float(p2u.std()) < 6.0:
                return None, gain_meas
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
            return None, gain_meas
        if not math.isfinite(float(resp)) or float(resp) < float(refine_resp_thresh):
            return None, gain_meas
        if not (math.isfinite(float(sx)) and math.isfinite(float(sy))):
            return None, gain_meas

        corr_x = (-float(sx)) / float(factor)
        corr_y = (-float(sy)) / float(factor)
        if abs(float(corr_x)) > float(refine_max_correction_px) or abs(float(corr_y)) > float(refine_max_correction_px):
            return None, gain_meas

        dx_meas = float(dx_pred_final) + float(corr_x)
        dy_meas = float(dy_pred_final) + float(corr_y)
        wgt = max(0.05, min(1.0, float(resp)))
        if gain_meas is not None:
            gain_meas = (float(gain_meas[0]), float(wgt))
        return (float(dx_meas), float(dy_meas), float(wgt)), gain_meas

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

    g_srcs: list[int] = []
    g_dsts: list[int] = []
    g_ws: list[float] = []
    g_dgs: list[float] = []

    def _add_gain_edge(i: int, j: int, dg: float, wgt: float) -> None:
        g_srcs.append(int(i))
        g_dsts.append(int(j))
        g_ws.append(float(wgt))
        g_dgs.append(float(dg))

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

        rng = random.Random(int(settings.get("layout_seed", 0)))
        rng.shuffle(candidates)
        candidates = candidates[: int(refine_max_edges)]

    measured = 0
    gain_measured = 0
    for idx_edge, (i, j, dx_pred, dy_pred) in enumerate(candidates):
        p1 = str(entries[int(i)].path)
        p2 = str(entries[int(j)].path)
        meas = _edge_measure(p1, p2, dx_pred_final=float(dx_pred), dy_pred_final=float(dy_pred))
        if meas is not None:
            pos_meas, gain_meas = meas
            if pos_meas is not None:
                dx_m, dy_m, wgt = pos_meas
                measured += 1
                _add_edge(int(i), int(j), float(dx_m), float(dy_m), float(wgt))
            else:
                _add_edge(int(i), int(j), float(dx_pred), float(dy_pred), 0.02)
            if gain_meas is not None:
                dg, wgt_g = gain_meas
                gain_measured += 1
                _add_gain_edge(int(i), int(j), float(dg), float(wgt_g))
        else:
            _add_edge(int(i), int(j), float(dx_pred), float(dy_pred), 0.02)
        if (idx_edge % 200) == 0 and (idx_edge + 1) < int(len(candidates)) and progress_cb is not None:
            frac = float(idx_edge + 1) / float(max(1, int(len(candidates))))
            progress_cb(
                float(progress_base) + (float(progress_span) * float(frac)),
                f"Stitching: refining positions ({idx_edge + 1}/{len(candidates)})…",
            )

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

    def _matvec(x: object) -> object:
        diff = wts * (x[src] - x[dst])
        y = np.bincount(src, diff, minlength=int(n_nodes)) + np.bincount(dst, -diff, minlength=int(n_nodes))
        if diag is not None and float(refine_prior_weight) > 0:
            y = y + (diag * x)
        y[int(anchor)] += float(anchor_w) * float(x[int(anchor)])
        return y

    def _cg(b: object, *, max_iter: int = 400, tol: float = 1e-4) -> tuple[object, int, float]:
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

    refined_by_rc: dict[tuple[int, int], tuple[float, float]] = {}
    for e in entries:
        i = idx_by_rc.get((int(e.row), int(e.col)))
        if i is None:
            continue
        refined_by_rc[(int(e.row), int(e.col))] = (float(x_sol[int(i)]), float(y_sol[int(i)]))

    refined_gains: list[float] | None = None
    gain_meta: dict[str, object] = {"enabled": bool(exposure_comp)}
    if bool(exposure_comp) and g_srcs and g_dsts and g_dgs:
        try:
            g_src = np.asarray(g_srcs, dtype=np.int32)
            g_dst = np.asarray(g_dsts, dtype=np.int32)
            g_wts = np.asarray(g_ws, dtype=np.float64)
            dg_arr = np.asarray(g_dgs, dtype=np.float64)

            bg = np.bincount(g_src, -g_wts * dg_arr, minlength=int(n_nodes)) + np.bincount(g_dst, g_wts * dg_arr, minlength=int(n_nodes))

            diag_g = np.full(int(n_nodes), float(gain_prior_weight), dtype=np.float64) if float(gain_prior_weight) > 0 else np.zeros(int(n_nodes), dtype=np.float64)
            anchor_g = 0
            anchor_w_g = 1e6

            def _matvec_g(x: object) -> object:
                diff = g_wts * (x[g_src] - x[g_dst])
                y = np.bincount(g_src, diff, minlength=int(n_nodes)) + np.bincount(g_dst, -diff, minlength=int(n_nodes))
                if diag_g is not None and float(gain_prior_weight) > 0:
                    y = y + (diag_g * x)
                y[int(anchor_g)] += float(anchor_w_g) * float(x[int(anchor_g)])
                return y

            def _cg_g(b: object, *, max_iter: int = 400, tol: float = 1e-4) -> tuple[object, int, float]:
                x = np.zeros_like(b, dtype=np.float64)
                r = b - _matvec_g(x)
                p = r.copy()
                rs = float(np.dot(r, r))
                if not math.isfinite(float(rs)) or float(rs) <= 0:
                    return x, 0, float(rs)
                for it in range(int(max_iter)):
                    Ap = _matvec_g(p)
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

            xg_sol, itg, rg = _cg_g(bg, max_iter=500, tol=1e-4)
            try:
                xg_sol = xg_sol - float(np.median(xg_sol))
            except Exception:
                pass
            gains = np.exp(xg_sol)
            gains = np.clip(gains, float(gain_min), float(gain_max))
            refined_gains = [float(g) if math.isfinite(float(g)) else 1.0 for g in gains.tolist()]

            try:
                gmin0 = float(np.min(gains))
                gmax0 = float(np.max(gains))
                gmean0 = float(np.mean(gains))
                gstd0 = float(np.std(gains))
            except Exception:
                gmin0 = float(gain_min)
                gmax0 = float(gain_max)
                gmean0 = 1.0
                gstd0 = 0.0

            gain_meta = {
                "enabled": True,
                "gain_min": float(gain_min),
                "gain_max": float(gain_max),
                "gain_eps": float(gain_eps),
                "gain_prior_weight": float(gain_prior_weight),
                "gain_stride": int(gain_stride),
                "edges_total": int(len(candidates)),
                "edges_measured": int(gain_measured),
                "cg_iters": int(itg),
                "cg_resid": float(rg),
                "gain_result_min": float(gmin0),
                "gain_result_max": float(gmax0),
                "gain_result_mean": float(gmean0),
                "gain_result_std": float(gstd0),
            }
        except Exception as exc:
            gain_meta = {"enabled": False, "error": str(exc)}

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
        "exposure_comp": dict(gain_meta),
    }

    gc.collect()
    return refined_by_rc, refined_gains, refine_meta

