from __future__ import annotations

import math
import os
from typing import Any, Callable

from ...progress import ProgressCallback
from ..io import is_no_space_error
from .types import Entry


def auto_feather_px(*, out_w: int, out_h: int, blend_strength: float) -> int:
    bs = max(0.0, float(blend_strength))
    blend_width = (math.sqrt(float(out_w) * float(out_h)) * float(bs)) / 100.0
    return max(0, int(round(float(blend_width))))


def nonblack_bbox(img: Any, *, thr: float) -> tuple[int, int, int, int]:
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


def read_composite_image(
    *,
    cv2: Any,
    np: Any,
    path: str,
    expected_w: int,
    expected_h: int,
    expected_dtype: Any | None = None,
) -> Any:
    img = cv2.imread(str(path), int(cv2.IMREAD_ANYDEPTH) | int(cv2.IMREAD_COLOR))
    if img is None:
        raise RuntimeError(f"Failed to read composite tile: {os.path.basename(path)}")
    if img.ndim != 3 or int(img.shape[2]) != 3:
        raise RuntimeError(f"Composite tile must have three color channels: {os.path.basename(path)}")
    dtype = np.dtype(img.dtype)
    if dtype not in {np.dtype(np.uint8), np.dtype(np.uint16), np.dtype(np.float32)}:
        raise RuntimeError(f"Composite tile must be uint8, uint16, or float32: {os.path.basename(path)}")
    if int(img.shape[1]) != int(expected_w) or int(img.shape[0]) != int(expected_h):
        raise RuntimeError(
            f"Composite tile dimensions do not match alignment tile: {os.path.basename(path)} "
            f"({img.shape[1]}x{img.shape[0]} != {expected_w}x{expected_h})"
        )
    if expected_dtype is not None and dtype != np.dtype(expected_dtype):
        raise RuntimeError(f"Composite tile dtype does not match the scan: {os.path.basename(path)}")
    if dtype == np.dtype(np.float32) and not np.isfinite(img).all():
        raise RuntimeError(f"Composite tile contains non-finite values: {os.path.basename(path)}")
    return img


def tile_positions(
    *,
    entries: list[Entry],
    pos_by_rc_f: dict[tuple[int, int], tuple[float, float]],
    min_x: float,
    min_y: float,
) -> dict[tuple[int, int], tuple[int, int]]:
    positions: dict[tuple[int, int], tuple[int, int]] = {}
    for entry in entries:
        key = (int(entry.row), int(entry.col))
        if key not in pos_by_rc_f:
            raise RuntimeError(f"Composite position is missing for row {entry.row}, column {entry.col}")
        px, py = pos_by_rc_f[key]
        positions[key] = (
            int(round(float(px) - float(min_x))),
            int(round(float(py) - float(min_y))),
        )
    return positions


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
    source_w: int,
    source_h: int,
    composite_dtype: str,
    out_dir: str,
    blend_mode: str,
    feather_px: int | None,
    inmem_max_bytes: int,
    use_memmap: bool,
    black_transparent: bool,
    black_threshold: int,
    refined_gains: list[float] | None,
    progress_cb: ProgressCallback | None,
    cancel_cb: Callable[[], None] | None,
) -> tuple[Any, str | None, str | None]:
    tile_count = int(len(entries))
    if tile_count <= 0:
        raise RuntimeError("No tiles to composite.")
    pano_dtype = np.dtype(composite_dtype)
    if pano_dtype not in {np.dtype(np.uint8), np.dtype(np.uint16), np.dtype(np.float32)}:
        raise RuntimeError(f"Unsupported composite dtype: {composite_dtype}")
    first_img = read_composite_image(
        cv2=cv2,
        np=np,
        path=entries[0].composite_path,
        expected_w=int(source_w),
        expected_h=int(source_h),
        expected_dtype=pano_dtype,
    )
    area = int(out_w) * int(out_h)
    pano_bytes = int(area) * 3 * int(pano_dtype.itemsize)
    weights_bytes = int(area) * int(np.dtype(np.uint16).itemsize)

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
        try:
            pano = np.memmap(memmap_path, dtype=pano_dtype, mode="w+", shape=(int(out_h), int(out_w), 3))
        except Exception as exc:
            if is_no_space_error(exc):
                raise RuntimeError(
                    "No space left on device while allocating the scratch canvas for full-res stitching. "
                    "Free disk space, choose a different output folder, or lower final_megapix."
                ) from exc
            raise RuntimeError(f"Failed to allocate scratch canvas: {exc} (path: {memmap_path})") from exc
    else:
        pano = np.zeros((int(out_h), int(out_w), 3), dtype=pano_dtype)

    weights = None
    weights_memmap_path: str | None = None
    if blend_mode in {"average", "feather"}:
        need_weights_memmap = False
        if int(weights_bytes) > int(inmem_max_bytes):
            need_weights_memmap = True
        if memmap_path is None and (int(pano_bytes) + int(weights_bytes)) > int(inmem_max_bytes):
            need_weights_memmap = True
        if need_weights_memmap and not bool(use_memmap):
            raise RuntimeError(
                f"Blend weight map is too large for in-memory composition (~{weights_bytes / (1024**3):.1f} GiB). "
                "Enable use_memmap or disable weighted blending."
            )
        if need_weights_memmap:
            weights_memmap_path = os.path.join(out_dir, "_mosaic_weights.dat")
            try:
                if os.path.exists(weights_memmap_path):
                    os.remove(weights_memmap_path)
            except Exception:
                pass
            try:
                weights = np.memmap(weights_memmap_path, dtype=np.uint16, mode="w+", shape=(int(out_h), int(out_w)))
            except Exception as exc:
                if is_no_space_error(exc):
                    raise RuntimeError(
                        "No space left on device while allocating the blend weight map for full-res stitching. "
                        "Free disk space, choose a different output folder, or disable weighted blending."
                    ) from exc
                raise RuntimeError(f"Failed to allocate blend weight map: {exc} (path: {weights_memmap_path})") from exc
        else:
            weights = np.zeros((int(out_h), int(out_w)), dtype=np.uint16)

    if progress_cb is not None:
        progress_cb("stitch-composite", "Compositing tiles", 0, tile_count, "tiles")

    pos_by_rc = tile_positions(
        entries=entries,
        pos_by_rc_f=pos_by_rc_f,
        min_x=min_x,
        min_y=min_y,
    )

    weight_cache: dict[tuple[int, int, int], Any] = {}
    if refined_gains is not None and pano_dtype != np.dtype(np.uint8):
        raise RuntimeError("JPEG-derived exposure gains are disabled for RAW-derived composites.")
    if refined_gains is not None and len(refined_gains) != tile_count:
        raise RuntimeError("Exposure gain count does not match tile count.")
    max_value = int(np.iinfo(pano_dtype).max) if np.issubdtype(pano_dtype, np.integer) else None
    native_black_threshold = (
        float(black_threshold) / 255.0
        if max_value is None
        else float(round(float(black_threshold) * float(max_value) / 255.0))
    )

    for i, e in enumerate(entries):
        if cancel_cb is not None:
            cancel_cb()
        img = first_img if i == 0 else read_composite_image(
            cv2=cv2,
            np=np,
            path=e.composite_path,
            expected_w=int(source_w),
            expected_h=int(source_h),
            expected_dtype=pano_dtype,
        )
        if int(img.shape[1]) != int(w_final) or int(img.shape[0]) != int(h_final):
            img = cv2.resize(img, (int(w_final), int(h_final)), interpolation=cv2.INTER_AREA)
            if np.dtype(img.dtype) != pano_dtype or img.ndim != 3 or int(img.shape[2]) != 3:
                raise RuntimeError(f"Failed to resize composite tile without changing its format: {os.path.basename(e.composite_path)}")

        if refined_gains is not None:
            g = float(refined_gains[int(i)])
            if not math.isfinite(g) or g <= 0:
                raise RuntimeError(f"Invalid exposure gain for tile {i}.")
            if abs(g - 1.0) > 1e-3:
                img = np.clip(np.rint(img.astype(np.float32) * g), 0, max_value).astype(pano_dtype)

        x, y = pos_by_rc[(int(e.row), int(e.col))]
        if x < 0 or y < 0 or (x + int(w_final)) > int(out_w) or (y + int(h_final)) > int(out_h):
            raise RuntimeError(f"Composite tile is outside the mosaic bounds: row {e.row}, column {e.col}")

        bx0 = 0
        by0 = 0
        bx1 = int(w_final) - 1
        by1 = int(h_final) - 1
        if bool(black_transparent) and int(black_threshold) > 0:
            bx0, by0, bx1, by1 = nonblack_bbox(img, thr=native_black_threshold)
            if bx1 < bx0 or by1 < by0:
                bx0, by0, bx1, by1 = 0, 0, int(w_final) - 1, int(h_final) - 1

        ax0 = int(x) + int(bx0)
        ay0 = int(y) + int(by0)
        ax1 = int(x) + int(bx1) + 1
        ay1 = int(y) + int(by1) + 1
        if ax1 <= ax0 or ay1 <= ay0:
            if progress_cb is not None:
                progress_cb("stitch-composite", "Compositing tiles", i + 1, tile_count, "tiles")
            continue

        if blend_mode == "average":
            if weights is None:
                raise RuntimeError("Average blending requires an overlap count map")
            roi = pano[int(ay0) : int(ay1), int(ax0) : int(ax1)]
            img_roi = img[int(by0) : int(by1 + 1), int(bx0) : int(bx1 + 1)]
            roi_w = weights[int(ay0) : int(ay1), int(ax0) : int(ax1)]
            old_count = roi_w.astype(np.float32)
            new_count = old_count + np.float32(1.0)
            out = ((roi.astype(np.float32) * old_count[:, :, None]) + img_roi.astype(np.float32)) / new_count[:, :, None]
            if pano_dtype == np.dtype(np.float32):
                roi[:, :, :] = out
            else:
                roi[:, :, :] = np.clip(np.rint(out), 0, max_value).astype(pano_dtype)
            roi_w[:, :] = np.minimum(new_count, np.float32(65535.0)).astype(np.uint16)
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
                if pano_dtype == np.dtype(np.float32):
                    roi_img[:, :, :] = out
                else:
                    roi_img[:, :, :] = np.clip(np.rint(out), 0, max_value).astype(pano_dtype)
                roi_w[:, :] = np.clip(w_sum_f, 0, 65535).astype(np.uint16)
        else:
            pano[int(ay0) : int(ay1), int(ax0) : int(ax1)] = img[int(by0) : int(by1 + 1), int(bx0) : int(bx1 + 1)]

        if progress_cb is not None:
            progress_cb("stitch-composite", "Compositing tiles", i + 1, tile_count, "tiles")

    return pano, (str(memmap_path) if memmap_path is not None else None), (str(weights_memmap_path) if weights_memmap_path is not None else None)
