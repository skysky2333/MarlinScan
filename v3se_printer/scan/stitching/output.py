from __future__ import annotations

import math
import os
import shutil
import subprocess
import sys
from typing import Any

from ..io import imwrite


def tiff_imwrite_params(cv2: Any, *, compression: str) -> list[int]:
    if not hasattr(cv2, "IMWRITE_TIFF_COMPRESSION"):
        return []
    comp = (compression or "none").strip().lower()
    code = 1  # none
    if comp == "lzw":
        code = 5
    elif comp == "deflate":
        code = 8
    return [int(getattr(cv2, "IMWRITE_TIFF_COMPRESSION")), int(code)]


def try_set_dpi(path: str, *, dpi_x: float, dpi_y: float) -> bool:
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


def estimate_output_dpi(
    *,
    strategy_settings: dict[str, object] | None,
    step_x_mm: float | None,
    step_y_mm: float | None,
    override_dpi: float | None,
    round_px_per_mm: float | None,
) -> tuple[float | None, dict[str, object] | None]:
    """
    Returns (px_per_mm_target, dpi_meta).

    dpi_meta includes:
    - dpi_x / dpi_y
    - mode: override|estimated
    - set_in_file: updated by the caller after writing
    """
    if override_dpi is not None:
        try:
            dpi0 = float(override_dpi)
        except Exception:
            dpi0 = None
        if dpi0 is not None and math.isfinite(float(dpi0)) and float(dpi0) > 0:
            px_per_mm_target = float(dpi0) / 25.4
            return px_per_mm_target, {
                "dpi_x": float(dpi0),
                "dpi_y": float(dpi0),
                "mode": "override",
                "set_in_file": False,
            }

    step_vecs = strategy_settings if isinstance(strategy_settings, dict) else None
    if step_vecs is None:
        return None, None
    if "step_col_px" not in step_vecs and "step_row_px" not in step_vecs:
        return None, None

    try:
        vx, vy = step_vecs.get("step_col_px", [0.0, 0.0])  # type: ignore[assignment]
        col_mag = math.hypot(float(vx), float(vy))
    except Exception:
        col_mag = 0.0

    row_vecs: list[tuple[float, float]] = []
    if "step_row_px_even" in step_vecs and "step_row_px_odd" in step_vecs:
        try:
            row_vecs.append((float(step_vecs.get("step_row_px_even")[0]), float(step_vecs.get("step_row_px_even")[1])))  # type: ignore[index]
            row_vecs.append((float(step_vecs.get("step_row_px_odd")[0]), float(step_vecs.get("step_row_px_odd")[1])))  # type: ignore[index]
        except Exception:
            row_vecs = []
    if not row_vecs:
        try:
            wx, wy = step_vecs.get("step_row_px", [0.0, 0.0])  # type: ignore[assignment]
            row_vecs.append((float(wx), float(wy)))
        except Exception:
            row_vecs = []

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

    if px_per_mm is None:
        return None, None

    if round_px_per_mm is not None:
        try:
            inc = float(round_px_per_mm)
        except Exception:
            inc = 0.0
        if float(inc) > 1e-9:
            px_per_mm = float(round(float(px_per_mm) / float(inc)) * float(inc))

    px_per_mm_target = float(px_per_mm)
    dpi = float(px_per_mm_target) * 25.4
    return px_per_mm_target, {
        "dpi_x": float(dpi),
        "dpi_y": float(dpi),
        "mode": "estimated",
        "px_per_mm": float(px_per_mm_target),
        "px_per_mm_x": float(px_per_mm_x) if px_per_mm_x is not None else None,
        "px_per_mm_y": float(px_per_mm_y) if px_per_mm_y is not None else None,
        "step_x_mm": float(step_x_mm) if step_x_mm is not None else None,
        "step_y_mm": float(step_y_mm) if step_y_mm is not None else None,
        "set_in_file": False,
    }


def write_mosaic_tiff(
    cv2: Any,
    *,
    pano: Any,
    memmap_path: str | None,
    out_w: int,
    out_h: int,
    mosaic_path: str,
    tiff_compression: str,
    px_per_mm_target: float | None,
    tiff_tile: bool | None,
    tiff_tile_width: int | None,
    tiff_tile_height: int | None,
    tiff_predictor: str | None,
) -> bool:
    """
    Write mosaic TIFF. Returns whether DPI was set in-file (either via vips save args or post-write metadata edit).
    """
    did_set = False
    if memmap_path is not None:
        import pyvips  # type: ignore

        vimg = pyvips.Image.rawload(memmap_path, int(out_w), int(out_h), 3, format="uchar")
        # cv2 uses BGR; write standard RGB.
        vimg = vimg[2].bandjoin([vimg[1], vimg[0]])

        if tiff_tile is None:
            # macOS Preview/QuickLook can render huge tiled TIFFs incorrectly on some systems.
            tiff_tile2 = not (sys.platform == "darwin" and (int(out_w) > 16000 or int(out_h) > 16000))
        else:
            tiff_tile2 = bool(tiff_tile)

        predictor = "horizontal"
        if tiff_predictor is not None:
            predictor = str(tiff_predictor).strip().lower() or "horizontal"
        if predictor not in {"none", "horizontal", "float"}:
            predictor = "horizontal"

        save_kwargs: dict[str, object] = {
            "compression": str(tiff_compression or "none").strip().lower() or "none",
            "predictor": str(predictor),
            "tile": bool(tiff_tile2),
        }
        if bool(tiff_tile2):
            save_kwargs["tile_width"] = int(tiff_tile_width or 256)
            save_kwargs["tile_height"] = int(tiff_tile_height or 256)
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
            msg = str(exc).lower()
            if not any(k in msg for k in ("bigtiff", "big tiff", "too large", "4gb", "offset")):
                raise
            try:
                if os.path.exists(mosaic_path):
                    os.remove(mosaic_path)
            except Exception:
                pass
            save_kwargs["bigtiff"] = True
            vimg.tiffsave(mosaic_path, **save_kwargs)
        return bool(did_set)

    # In-memory mosaic: use cv2.imwrite
    params = tiff_imwrite_params(cv2, compression=str(tiff_compression))
    imwrite(cv2, mosaic_path, pano, params)
    if px_per_mm_target is not None and math.isfinite(float(px_per_mm_target)) and float(px_per_mm_target) > 0:
        dpi = float(px_per_mm_target) * 25.4
        did_set = try_set_dpi(mosaic_path, dpi_x=float(dpi), dpi_y=float(dpi))
    return bool(did_set)


def write_preview_jpeg(
    *,
    mosaic_path: str,
    out_dir: str,
    max_dim: int,
    quality: int,
) -> str | None:
    """Best-effort: write a JPEG preview next to the mosaic."""
    try:
        import pyvips  # type: ignore

        img_prev = pyvips.Image.new_from_file(mosaic_path, access="sequential")
        thumb = img_prev.thumbnail_image(int(max_dim))
        thumb_path = os.path.join(out_dir, f"mosaic_thumb_{int(max_dim)}.jpg")
        thumb.write_to_file(thumb_path, Q=int(quality))
        return str(thumb_path)
    except Exception:
        return None

