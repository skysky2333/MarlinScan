from __future__ import annotations

import math
import os
import sys
from typing import Any, Callable
from xml.etree import ElementTree

from ...color import REC2020_TO_SRGB_MATRIX, require_srgb_icc_profile
from ...progress import ProgressCallback


def _attach_vips_progress(
    image: Any,
    progress_cb: ProgressCallback | None,
    *,
    phase: str,
    label: str,
    cancel_cb: Callable[[], None] | None,
) -> Callable[[], None]:
    failure: list[Exception] = []

    def publish(_image: Any, progress: Any) -> None:
        try:
            if cancel_cb is not None:
                cancel_cb()
            if progress_cb is not None:
                progress_cb(phase, label, int(progress.npels), int(progress.tpels), "pixels")
        except Exception as exc:
            failure.append(exc)
            image.set_kill(True)

    def check() -> None:
        if failure:
            raise failure[0]
        if cancel_cb is not None:
            cancel_cb()

    for signal in ("preeval", "eval", "posteval"):
        image.signal_connect(signal, publish)
    image.set_progress(True)
    return check


def _ome_rgb_description(width: int, height: int) -> str:
    namespace = "http://www.openmicroscopy.org/Schemas/OME/2016-06"
    ElementTree.register_namespace("", namespace)
    ome = ElementTree.Element(f"{{{namespace}}}OME")
    image = ElementTree.SubElement(ome, f"{{{namespace}}}Image", {"ID": "Image:0", "Name": "MarlinScan mosaic"})
    pixels = ElementTree.SubElement(
        image,
        f"{{{namespace}}}Pixels",
        {
            "ID": "Pixels:0",
            "DimensionOrder": "XYCZT",
            "Type": "uint16",
            "SizeX": str(width),
            "SizeY": str(height),
            "SizeC": "3",
            "SizeZ": "1",
            "SizeT": "1",
            "Interleaved": "true",
        },
    )
    ElementTree.SubElement(
        pixels,
        f"{{{namespace}}}Channel",
        {"ID": "Channel:0:0", "SamplesPerPixel": "3"},
    )
    ElementTree.SubElement(pixels, f"{{{namespace}}}TiffData", {"IFD": "0", "PlaneCount": "1"})
    return ElementTree.tostring(ome, encoding="unicode", xml_declaration=True)


def _with_ome_description(image: Any, width: int, height: int) -> Any:
    import pyvips  # type: ignore

    described = image.copy()
    described.set_type(
        pyvips.GValue.gstr_type,
        "image-description",
        _ome_rgb_description(width, height),
    )
    return described


def estimate_output_dpi(
    *,
    strategy_settings: dict[str, object] | None,
    step_x_mm: float | None,
    step_y_mm: float | None,
    override_dpi: float | None,
    round_px_per_mm: float | None,
    frame_width_mm: float | None = None,
    frame_height_mm: float | None = None,
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

    tile_size = step_vecs.get("tile_size_px")
    if isinstance(tile_size, list) and len(tile_size) == 2:
        tile_width, tile_height = tile_size
        if (
            px_per_mm_x is None
            and frame_width_mm is not None
            and math.isfinite(frame_width_mm)
            and frame_width_mm > 0
            and isinstance(tile_width, int)
            and not isinstance(tile_width, bool)
            and tile_width > 0
        ):
            px_per_mm_x = float(tile_width) / frame_width_mm
        if (
            px_per_mm_y is None
            and frame_height_mm is not None
            and math.isfinite(frame_height_mm)
            and frame_height_mm > 0
            and isinstance(tile_height, int)
            and not isinstance(tile_height, bool)
            and tile_height > 0
        ):
            px_per_mm_y = float(tile_height) / frame_height_mm

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
    pyramidal: bool = False,
    progress_cb: ProgressCallback | None = None,
    cancel_cb: Callable[[], None] | None = None,
) -> bool:
    """
    Write mosaic TIFF and return whether DPI was set in-file.
    """
    dtype_name = str(getattr(pano, "dtype", ""))
    vips_format_by_dtype = {"uint8": "uchar", "uint16": "ushort"}
    if dtype_name not in vips_format_by_dtype:
        raise RuntimeError(f"Unsupported mosaic dtype: {dtype_name or '?'}")
    import numpy as np  # type: ignore
    import pyvips  # type: ignore

    if memmap_path is not None:
        vimg = pyvips.Image.rawload(
            memmap_path,
            int(out_w),
            int(out_h),
            3,
            format=vips_format_by_dtype[dtype_name],
        )
    else:
        contiguous = np.ascontiguousarray(pano)
        if contiguous.shape != (int(out_h), int(out_w), 3):
            raise RuntimeError("Mosaic dimensions do not match its output canvas")
        vimg = pyvips.Image.new_from_memory(
            contiguous.data,
            int(out_w),
            int(out_h),
            3,
            vips_format_by_dtype[dtype_name],
        )
    vimg = vimg[2].bandjoin([vimg[1], vimg[0]])

    if pyramidal:
        tiff_tile2 = True
    elif tiff_tile is None:
        tiff_tile2 = not (sys.platform == "darwin" and (int(out_w) > 16000 or int(out_h) > 16000))
    else:
        tiff_tile2 = bool(tiff_tile)

    predictor = "horizontal" if tiff_predictor is None else str(tiff_predictor).strip().lower() or "horizontal"
    if predictor not in {"none", "horizontal", "float"}:
        predictor = "horizontal"

    save_kwargs: dict[str, object] = {
        "compression": str(tiff_compression or "none").strip().lower() or "none",
        "predictor": predictor,
        "tile": tiff_tile2,
        "profile": str(require_srgb_icc_profile()),
    }
    if tiff_tile2:
        save_kwargs["tile_width"] = max(64, min(2048, int(tiff_tile_width or 256)))
        save_kwargs["tile_height"] = max(64, min(2048, int(tiff_tile_height or 256)))
    if pyramidal:
        vimg = _with_ome_description(vimg, int(out_w), int(out_h))
        save_kwargs["pyramid"] = True
        save_kwargs["subifd"] = True
        save_kwargs["depth"] = "onetile"
    estimated_bytes = int(out_w) * int(out_h) * 3 * int(np.dtype(dtype_name).itemsize)
    if pyramidal:
        estimated_bytes = math.ceil(estimated_bytes * 4 / 3)
    if estimated_bytes >= 4_000_000_000:
        save_kwargs["bigtiff"] = True
    did_set = px_per_mm_target is not None and math.isfinite(float(px_per_mm_target)) and float(px_per_mm_target) > 0
    if did_set:
        save_kwargs["xres"] = float(px_per_mm_target)
        save_kwargs["yres"] = float(px_per_mm_target)
        save_kwargs["resunit"] = "inch"

    phase = "write-pyramidal-tiff" if pyramidal else "write-flat-tiff"
    label = "Writing pyramidal TIFF" if pyramidal else "Writing flat TIFF"
    check = _attach_vips_progress(vimg, progress_cb, phase=phase, label=label, cancel_cb=cancel_cb)
    check()
    try:
        vimg.tiffsave(mosaic_path, **save_kwargs)
    except Exception:
        check()
        raise
    check()
    return bool(did_set)


def write_scene_linear_mosaic_tiff(
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
    pyramidal: bool = False,
    progress_cb: ProgressCallback | None = None,
    cancel_cb: Callable[[], None] | None = None,
) -> bool:
    if str(getattr(pano, "dtype", "")) != "float32":
        raise RuntimeError("Scene-linear mosaic must be float32")
    import numpy as np  # type: ignore
    import pyvips  # type: ignore

    if memmap_path is not None:
        image = pyvips.Image.rawload(memmap_path, int(out_w), int(out_h), 3, format="float")
    else:
        contiguous = np.ascontiguousarray(pano)
        if contiguous.shape != (int(out_h), int(out_w), 3):
            raise RuntimeError("Scene-linear mosaic dimensions do not match its output canvas")
        image = pyvips.Image.new_from_memory(contiguous.data, int(out_w), int(out_h), 3, "float")

    rgb = image[2].bandjoin([image[1], image[0]])
    linear_srgb = rgb.recomb(pyvips.Image.new_from_array(np.asarray(REC2020_TO_SRGB_MATRIX)))
    clipped = (linear_srgb < 0.0).ifthenelse(0.0, linear_srgb)
    clipped = (clipped > 1.0).ifthenelse(1.0, clipped)
    encoded = (clipped <= 0.0031308).ifthenelse(
        clipped * 12.92,
        clipped ** (1.0 / 2.4) * 1.055 - 0.055,
    )
    display = (encoded * 65535.0 + 0.5).cast("ushort")

    if pyramidal:
        tiled = True
    elif tiff_tile is None:
        tiled = not (sys.platform == "darwin" and (int(out_w) > 16000 or int(out_h) > 16000))
    else:
        tiled = bool(tiff_tile)
    predictor = "horizontal" if tiff_predictor is None else str(tiff_predictor).strip().lower() or "horizontal"
    if predictor not in {"none", "horizontal", "float"}:
        predictor = "horizontal"
    save_kwargs: dict[str, object] = {
        "compression": str(tiff_compression or "none").strip().lower() or "none",
        "predictor": predictor,
        "tile": tiled,
        "profile": str(require_srgb_icc_profile()),
    }
    if tiled:
        save_kwargs["tile_width"] = max(64, min(2048, int(tiff_tile_width or 256)))
        save_kwargs["tile_height"] = max(64, min(2048, int(tiff_tile_height or 256)))
    if pyramidal:
        display = _with_ome_description(display, int(out_w), int(out_h))
        save_kwargs["pyramid"] = True
        save_kwargs["subifd"] = True
        save_kwargs["depth"] = "onetile"
    estimated_bytes = int(out_w) * int(out_h) * 3 * int(np.dtype(np.uint16).itemsize)
    if pyramidal:
        estimated_bytes = math.ceil(estimated_bytes * 4 / 3)
    if estimated_bytes >= 4_000_000_000:
        save_kwargs["bigtiff"] = True
    did_set = px_per_mm_target is not None and math.isfinite(float(px_per_mm_target)) and float(px_per_mm_target) > 0
    if did_set:
        save_kwargs["xres"] = float(px_per_mm_target)
        save_kwargs["yres"] = float(px_per_mm_target)
        save_kwargs["resunit"] = "inch"
    phase = "write-pyramidal-tiff" if pyramidal else "write-flat-tiff"
    label = "Writing pyramidal TIFF" if pyramidal else "Writing flat TIFF"
    check = _attach_vips_progress(display, progress_cb, phase=phase, label=label, cancel_cb=cancel_cb)
    check()
    try:
        display.tiffsave(mosaic_path, **save_kwargs)
    except Exception:
        check()
        raise
    check()
    return bool(did_set)


def write_preview_jpeg(
    *,
    mosaic_path: str,
    out_dir: str,
    max_dim: int,
    quality: int,
    progress_cb: ProgressCallback | None = None,
    cancel_cb: Callable[[], None] | None = None,
) -> str:
    import pyvips  # type: ignore

    img_prev = pyvips.Image.new_from_file(mosaic_path, access="sequential")
    thumb = img_prev.thumbnail_image(int(max_dim))
    if thumb.format == "ushort":
        thumb = (thumb / 257.0).cast("uchar")
    elif thumb.format != "uchar":
        raise RuntimeError(f"Unsupported mosaic preview format: {thumb.format}")
    thumb_path = os.path.join(out_dir, f"mosaic_thumb_{int(max_dim)}.jpg")
    check = _attach_vips_progress(
        thumb,
        progress_cb,
        phase="write-preview",
        label="Writing preview",
        cancel_cb=cancel_cb,
    )
    check()
    try:
        thumb.write_to_file(thumb_path, Q=int(quality))
    except Exception:
        check()
        raise
    check()
    return str(thumb_path)
