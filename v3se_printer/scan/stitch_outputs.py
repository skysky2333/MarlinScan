from __future__ import annotations

import gc
import json
import math
import os
import time
import traceback
from typing import Callable

from .io import is_no_space_error
from .stitching.layout import stitch_layout_mosaic
from .stitching.output import estimate_output_dpi, write_mosaic_tiff, write_preview_jpeg
from .stitching.types import Entry
from .stitching.util import median


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
    Stitch scan tiles into a full-resolution mosaic TIFF + JPEG preview.

    This uses a layout-based affine pipeline:
    - estimate neighbor step vectors on downscaled tiles
    - (optional) refine tile positions and per-tile exposure gains on overlaps
    - composite at final resolution with weighted feather blending

    Outputs (in out_dir):
    - mosaic_full.tif
    - mosaic_thumb_2000.jpg (size configurable)
    - stitch_meta.json
    - stitch_error.txt (only on failure)
    """
    if not bool(build_pyramidal_tiff):
        return

    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except Exception as exc:
        raise RuntimeError(
            "Missing dependency for stitching. Install: `python -m pip install opencv-python pyvips` "
            f"(original error: {exc})"
        ) from exc

    try:
        if hasattr(cv2, "ocl") and hasattr(cv2.ocl, "setUseOpenCL"):
            cv2.ocl.setUseOpenCL(False)
    except Exception:
        pass

    s_in = stitch_settings if isinstance(stitch_settings, dict) else {}

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

    # Clear stale outputs.
    try:
        for fn in (
            "stitch_error.txt",
            "stitch_meta.json",
            "mosaic_full.tif",
            "_mosaic_memmap.dat",
            "_mosaic_weights.dat",
        ):
            p = os.path.join(out_dir, fn)
            if os.path.exists(p):
                os.remove(p)
        # Remove old previews (we now allow configurable sizes).
        for ent in os.listdir(out_dir):
            if ent.startswith("mosaic_thumb_") and ent.endswith(".jpg"):
                try:
                    os.remove(os.path.join(out_dir, ent))
                except Exception:
                    pass
    except Exception:
        pass

    # Parse tiles.
    entries: list[Entry] = []
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
        entries.append(Entry(int(r), int(c), str(p)))
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

    # Tile size.
    im0 = cv2.imread(str(entries[0].path), cv2.IMREAD_COLOR)
    if im0 is None:
        raise RuntimeError("Failed to read a tile image.")
    tile_h, tile_w = im0.shape[:2]
    if int(tile_w) <= 0 or int(tile_h) <= 0:
        raise RuntimeError("Invalid tile size.")
    orig_mp = (float(tile_w) * float(tile_h)) / 1_000_000.0

    # Final resolution (per-tile megapixels).
    # - If user provides `final_megapix`: respect it (`-1` = full-res).
    # - Otherwise: default to full-res.
    stage1_final_megapix: float
    try:
        v = s_in.get("final_megapix", None)
    except Exception:
        v = None
    if v is None:
        stage1_final_megapix = -1.0
    else:
        try:
            v2 = float(v)
        except Exception:
            v2 = -1.0
        stage1_final_megapix = float(v2) if (float(v2) == -1.0 or float(v2) > 0) else -1.0

    # Physical step (mm) for output DPI metadata.
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
        sx = float(median(dxs)) if dxs else None
        sy = float(median(dys)) if dys else None
        if sx is not None and float(sx) <= 0:
            sx = None
        if sy is not None and float(sy) <= 0:
            sy = None
        return sx, sy

    if step_x_mm is None or step_y_mm is None:
        sx2, sy2 = _infer_step_mm_from_tiles()
        if step_x_mm is None:
            step_x_mm = sx2
        if step_y_mm is None:
            step_y_mm = sy2

    tile_count = int(len(entries))
    _emit_progress(0.0, f"Stitching (layout affine): {tile_count} tiles…")

    mosaic = None
    try:
        mosaic = stitch_layout_mosaic(
            cv2=cv2,
            np=np,
            entries=entries,
            by_rc=by_rc,
            nrows=int(nrows),
            ncols=int(ncols),
            tile_w=int(tile_w),
            tile_h=int(tile_h),
            orig_mp=float(orig_mp),
            serpentine=serpentine,
            out_dir=str(out_dir),
            settings=s_in,
            final_megapix=float(stage1_final_megapix),
            progress_cb=_emit_progress,
        )

        _emit_progress(92.0, "Stitching: writing mosaic_full.tif…")
        mosaic_path = os.path.join(out_dir, "mosaic_full.tif")

        override_dpi = None
        try:
            override_dpi = s_in.get("output_dpi", None)
        except Exception:
            override_dpi = None
        round_px_per_mm = None
        try:
            round_px_per_mm = s_in.get("dpi_round_px_per_mm", None)
        except Exception:
            round_px_per_mm = None

        px_per_mm_target, dpi_meta = estimate_output_dpi(
            strategy_settings=mosaic.stage_meta,
            step_x_mm=step_x_mm,
            step_y_mm=step_y_mm,
            override_dpi=float(override_dpi) if override_dpi is not None else None,
            round_px_per_mm=float(round_px_per_mm) if round_px_per_mm is not None else None,
        )

        try:
            if hasattr(mosaic.pano, "flush"):
                mosaic.pano.flush()  # type: ignore[union-attr]
        except Exception:
            pass

        tiff_tile_pref = None
        try:
            tiff_tile_pref = s_in.get("tiff_tile", None)
        except Exception:
            tiff_tile_pref = None
        predictor_pref = None
        try:
            predictor_pref = s_in.get("tiff_predictor", None)
        except Exception:
            predictor_pref = None
        did_set = write_mosaic_tiff(
            cv2,
            pano=mosaic.pano,
            memmap_path=mosaic.memmap_path,
            out_w=int(mosaic.out_w),
            out_h=int(mosaic.out_h),
            mosaic_path=str(mosaic_path),
            tiff_compression=str(tiff_compression),
            px_per_mm_target=px_per_mm_target,
            tiff_tile=bool(tiff_tile_pref) if tiff_tile_pref is not None else None,
            tiff_tile_width=int(s_in.get("tiff_tile_width", 256) or 256),
            tiff_tile_height=int(s_in.get("tiff_tile_height", 256) or 256),
            tiff_predictor=str(predictor_pref) if predictor_pref is not None else None,
        )
        if dpi_meta is not None:
            dpi_meta["set_in_file"] = bool(did_set)

        stage_meta = dict(mosaic.stage_meta)
        out_w = int(mosaic.out_w)
        out_h = int(mosaic.out_h)
        memmap_path = str(mosaic.memmap_path) if mosaic.memmap_path is not None else None
        weights_memmap_path = str(mosaic.weights_memmap_path) if mosaic.weights_memmap_path is not None else None

        # Release big arrays before deleting scratch files / writing meta.
        try:
            del mosaic
        except Exception:
            pass
        gc.collect()

        # Clean up scratch buffers (if any).
        for p in (memmap_path, weights_memmap_path):
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass

        # Always emit a JPEG preview for quick sanity-checking.
        preview_max_dim = int(s_in.get("preview_max_dim", 2000) or 2000)
        preview_quality = int(s_in.get("preview_quality", 85) or 85)
        preview_max_dim = max(256, min(8000, int(preview_max_dim)))
        preview_quality = max(30, min(95, int(preview_quality)))
        write_preview_jpeg(
            mosaic_path=str(mosaic_path),
            out_dir=str(out_dir),
            max_dim=int(preview_max_dim),
            quality=int(preview_quality),
        )

        meta: dict[str, object] = {
            "method": "affine-layout",
            "tiles": int(tile_count),
            "rows": int(nrows),
            "cols": int(ncols),
            "tile_size_px": [int(tile_w), int(tile_h)],
            "mosaic_size_px": [int(out_w), int(out_h)],
            "stages": [stage_meta],
            "settings": dict(s_in),
            "versions": {
                "opencv": str(getattr(cv2, "__version__", "?")),
            },
        }
        if dpi_meta is not None:
            meta["dpi"] = dict(dpi_meta)
        try:
            with open(os.path.join(out_dir, "stitch_meta.json"), "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2, sort_keys=False)
        except Exception:
            pass

        _emit_progress(100.0, "Stitching: done.")
    except Exception as exc:
        # Best-effort cleanup of scratch buffers on failure.
        try:
            for fn in ("_mosaic_memmap.dat", "_mosaic_weights.dat"):
                p = os.path.join(out_dir, fn)
                if os.path.exists(p):
                    os.remove(p)
        except Exception:
            pass
        _write_err("stitch", exc)
        if is_no_space_error(exc):
            raise RuntimeError(
                "No space left on device while stitching. Free disk space or choose a different output folder."
            ) from exc
        raise
