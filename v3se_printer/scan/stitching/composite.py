from __future__ import annotations

import math
import os
from typing import Any, Callable

from ..io import is_no_space_error
from .types import Entry


def auto_feather_px(*, out_w: int, out_h: int, blend_strength: float) -> int:
    bs = max(0.0, float(blend_strength))
    blend_width = (math.sqrt(float(out_w) * float(out_h)) * float(bs)) / 100.0
    return max(0, int(round(float(blend_width))))


def nonblack_bbox(img: Any, *, thr: int) -> tuple[int, int, int, int]:
    """Return (x0,y0,x1,y1) inclusive pixel bounds for the non-black region."""
    h, w = img.shape[:2]
    bx0 = 0
    by0 = 0
    bx1 = int(w) - 1
    by1 = int(h) - 1
    if int(w) <= 0 or int(h) <= 0:
        return 0, 0, -1, -1

    while by0 <= by1 and not (img[int(by0)] > thr).any():
        by0 += 1
    while by1 >= by0 and not (img[int(by1)] > thr).any():
        by1 -= 1
    if by0 > by1:
        return 0, 0, -1, -1

    while bx0 <= bx1 and not (img[int(by0) : int(by1 + 1), int(bx0)] > thr).any():
        bx0 += 1
    while bx1 >= bx0 and not (img[int(by0) : int(by1 + 1), int(bx1)] > thr).any():
        bx1 -= 1
    if bx0 > bx1:
        return 0, 0, -1, -1
    return int(bx0), int(by0), int(bx1), int(by1)


def feather_weight(
    *,
    np: Any,
    w: int,
    h: int,
    feather_px: int,
    cache: dict[tuple[int, int, int], Any],
) -> Any:
    key = (int(w), int(h), int(feather_px))
    cached = cache.get(key)
    if cached is not None:
        return cached
    # Keep cache small; all tiles are usually the same size.
    if len(cache) > 8:
        cache.clear()

    fpx = max(0, int(feather_px))
    xs = np.arange(int(w), dtype=np.int32)
    dx = np.minimum(xs, (int(w) - 1) - xs)
    ys = np.arange(int(h), dtype=np.int32)
    dy = np.minimum(ys, (int(h) - 1) - ys)
    if int(fpx) > 0:
        dx = np.minimum(dx, int(fpx))
        dy = np.minimum(dy, int(fpx))
    else:
        dx = dx * 0
        dy = dy * 0
    w_new = np.minimum(dy[:, None], dx[None, :]).astype(np.uint16)
    w_new = (w_new + np.uint16(1)).astype(np.uint16)
    if int(fpx) > 0:
        cap = int(min(65535, int(fpx) + 1))
        if cap < 65535:
            w_new = np.minimum(w_new, np.uint16(cap)).astype(np.uint16)
    cache[key] = w_new
    return w_new


def composite_tiles(
    *,
    cv2: Any,
    np: Any,
    entries: list[Entry],
    pos_by_rc_f: dict[tuple[int, int], tuple[float, float]],
    min_x: float,
    min_y: float,
    out_w: int,
    out_h: int,
    w_final: int,
    h_final: int,
    out_dir: str,
    blend_mode: str,
    feather_px: int | None,
    inmem_max_bytes: int,
    use_memmap: bool,
    black_transparent: bool,
    black_threshold: int,
    refined_gains: list[float] | None,
    progress_cb: Callable[[float, str], None] | None,
    progress_base: float = 10.0,
    progress_span: float = 80.0,
) -> tuple[Any, str | None, str | None]:
    tile_count = int(len(entries))
    area = int(out_w) * int(out_h)

    try:
        pano_bytes = int(area) * 3
    except Exception:
        pano_bytes = 0
    try:
        weights_bytes = int(area) * 2
    except Exception:
        weights_bytes = 0

    pano = None
    memmap_path: str | None = None
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
        if progress_cb is not None:
            progress_cb(9.0, f"Stitching: allocating scratch canvas (~{pano_bytes / (1024**3):.1f} GiB)…")
        try:
            pano = np.memmap(memmap_path, dtype=np.uint8, mode="w+", shape=(int(out_h), int(out_w), 3))
        except Exception as exc:
            if is_no_space_error(exc):
                raise RuntimeError(
                    "No space left on device while allocating the scratch canvas for full-res stitching. "
                    "Free disk space, choose a different output folder, or lower final_megapix."
                ) from exc
            raise RuntimeError(f"Failed to allocate scratch canvas: {exc} (path: {memmap_path})") from exc
    else:
        pano = np.zeros((int(out_h), int(out_w), 3), dtype=np.uint8)

    weights = None
    weights_memmap_path: str | None = None
    if blend_mode == "feather":
        need_weights_memmap = False
        if int(weights_bytes) > int(inmem_max_bytes):
            need_weights_memmap = True
        if memmap_path is None and (int(pano_bytes) + int(weights_bytes)) > int(inmem_max_bytes):
            need_weights_memmap = True
        if need_weights_memmap and not bool(use_memmap):
            raise RuntimeError(
                f"Blend weight map is too large for in-memory composition (~{weights_bytes / (1024**3):.1f} GiB). "
                "Enable use_memmap or disable feather blending."
            )
        if need_weights_memmap:
            weights_memmap_path = os.path.join(out_dir, "_mosaic_weights.dat")
            try:
                if os.path.exists(weights_memmap_path):
                    os.remove(weights_memmap_path)
            except Exception:
                pass
            if progress_cb is not None:
                progress_cb(9.2, f"Stitching: allocating blend weights (~{weights_bytes / (1024**3):.1f} GiB)…")
            try:
                weights = np.memmap(weights_memmap_path, dtype=np.uint16, mode="w+", shape=(int(out_h), int(out_w)))
            except Exception as exc:
                if is_no_space_error(exc):
                    raise RuntimeError(
                        "No space left on device while allocating the blend weight map for full-res stitching. "
                        "Free disk space, choose a different output folder, or disable feather blending."
                    ) from exc
                raise RuntimeError(f"Failed to allocate blend weight map: {exc} (path: {weights_memmap_path})") from exc
        else:
            weights = np.zeros((int(out_h), int(out_w)), dtype=np.uint16)

    if progress_cb is not None:
        progress_cb(float(progress_base), f"Stitching: compositing {tile_count} tiles…")

    pos_by_rc: dict[tuple[int, int], tuple[int, int]] = {}
    for e in entries:
        px, py = pos_by_rc_f.get((int(e.row), int(e.col)), (None, None))  # type: ignore[assignment]
        if px is None or py is None:
            continue
        x = int(round(float(px) - float(min_x)))
        y = int(round(float(py) - float(min_y)))
        pos_by_rc[(int(e.row), int(e.col))] = (int(x), int(y))

    weight_cache: dict[tuple[int, int, int], Any] = {}

    for i, e in enumerate(entries):
        img = cv2.imread(str(e.path), cv2.IMREAD_COLOR)
        if img is None:
            continue
        if int(img.shape[1]) != int(w_final) or int(img.shape[0]) != int(h_final):
            try:
                img = cv2.resize(img, (int(w_final), int(h_final)), interpolation=cv2.INTER_AREA)
            except Exception:
                continue

        if refined_gains is not None and int(i) < int(len(refined_gains)):
            try:
                g = float(refined_gains[int(i)])
                if math.isfinite(float(g)) and abs(float(g) - 1.0) > 1e-3:
                    img = cv2.convertScaleAbs(img, alpha=float(g))
            except Exception:
                pass

        x, y = pos_by_rc.get((int(e.row), int(e.col)), (None, None))  # type: ignore[assignment]
        if x is None or y is None:
            continue
        if x < 0 or y < 0 or (x + int(w_final)) > int(out_w) or (y + int(h_final)) > int(out_h):
            continue

        bx0 = 0
        by0 = 0
        bx1 = int(w_final) - 1
        by1 = int(h_final) - 1
        if bool(black_transparent) and int(black_threshold) > 0:
            try:
                bx0, by0, bx1, by1 = nonblack_bbox(img, thr=int(black_threshold))
                if bx1 < bx0 or by1 < by0:
                    bx0, by0, bx1, by1 = 0, 0, int(w_final) - 1, int(h_final) - 1
            except Exception:
                bx0, by0, bx1, by1 = 0, 0, int(w_final) - 1, int(h_final) - 1

        ax0 = int(x) + int(bx0)
        ay0 = int(y) + int(by0)
        ax1 = int(x) + int(bx1) + 1
        ay1 = int(y) + int(by1) + 1
        if ax1 <= ax0 or ay1 <= ay0:
            continue

        if blend_mode == "average":
            roi = pano[int(ay0) : int(ay1), int(ax0) : int(ax1)]
            img_roi = img[int(by0) : int(by1 + 1), int(bx0) : int(bx1 + 1)]
            try:
                mask = roi.sum(axis=2) > 0
                if mask.any():
                    roi[mask] = ((roi[mask].astype(np.uint16) + img_roi[mask].astype(np.uint16)) // 2).astype(np.uint8)
                roi[~mask] = img_roi[~mask]
            except Exception:
                pano[int(ay0) : int(ay1), int(ax0) : int(ax1)] = img_roi
        elif blend_mode == "feather":
            if weights is None:
                pano[int(ay0) : int(ay1), int(ax0) : int(ax1)] = img[int(by0) : int(by1 + 1), int(bx0) : int(bx1 + 1)]
            else:
                img_roi = img[int(by0) : int(by1 + 1), int(bx0) : int(bx1 + 1)]
                roi_img = pano[int(ay0) : int(ay1), int(ax0) : int(ax1)]
                roi_w = weights[int(ay0) : int(ay1), int(ax0) : int(ax1)]

                fpx = max(0, int(feather_px or 0))
                rect_w = int(bx1 - bx0 + 1)
                rect_h = int(by1 - by0 + 1)

                w_new = feather_weight(np=np, w=int(rect_w), h=int(rect_h), feather_px=int(fpx), cache=weight_cache)
                w_old_f = roi_w.astype(np.float32)
                w_new_f = w_new.astype(np.float32)
                w_sum_f = w_old_f + w_new_f
                out = ((roi_img.astype(np.float32) * w_old_f[:, :, None]) + (img_roi.astype(np.float32) * w_new_f[:, :, None])) / w_sum_f[:, :, None]
                roi_img[:, :, :] = np.clip(out, 0, 255).astype(np.uint8)
                roi_w[:, :] = np.clip(w_sum_f, 0, 65535).astype(np.uint16)
        else:
            pano[int(ay0) : int(ay1), int(ax0) : int(ax1)] = img[int(by0) : int(by1 + 1), int(bx0) : int(bx1 + 1)]

        if (i % 50) == 0 or (i + 1) == int(tile_count):
            frac = float(i + 1) / float(max(1, int(tile_count)))
            if progress_cb is not None:
                progress_cb(float(progress_base) + (float(progress_span) * float(frac)), f"Stitching: compositing ({i + 1}/{tile_count})…")

    return pano, (str(memmap_path) if memmap_path is not None else None), (str(weights_memmap_path) if weights_memmap_path is not None else None)

