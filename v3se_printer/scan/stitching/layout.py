from __future__ import annotations

import math
from typing import Any, Callable

from .composite import auto_feather_px, composite_tiles
from .refine import refine_positions_and_gains
from .step_estimation import estimate_step_vectors
from .types import Entry, LayoutMosaic
from .util import scale_for_megapix


def stitch_layout_mosaic(
    *,
    cv2: Any,
    np: Any,
    entries: list[Entry],
    by_rc: dict[tuple[int, int], str],
    nrows: int,
    ncols: int,
    tile_w: int,
    tile_h: int,
    orig_mp: float,
    serpentine: bool | None,
    out_dir: str,
    settings: dict[str, object],
    final_megapix: float,
    progress_cb: Callable[[float, str], None] | None,
) -> LayoutMosaic:
    tile_count = int(len(entries))

    blend_mode = str(settings.get("layout_blend", "feather")).strip().lower() or "feather"
    if blend_mode not in {"overwrite", "average", "feather"}:
        blend_mode = "overwrite"

    feather_px_in = settings.get("layout_feather_px", None)
    feather_px: int | None = None
    if feather_px_in is not None:
        try:
            feather_px = max(0, int(float(feather_px_in)))
        except Exception:
            feather_px = None

    if int(tile_count) <= 0:
        raise RuntimeError("No tiles to stitch.")

    # Final scale per tile (MP).
    scale_final = float(scale_for_megapix(orig_mp=float(orig_mp), target_mp=float(final_megapix)))
    w_final = max(1, int(round(float(tile_w) * float(scale_final))))
    h_final = max(1, int(round(float(tile_h) * float(scale_final))))

    if int(tile_count) == 1:
        img0 = cv2.imread(str(entries[0].path), cv2.IMREAD_COLOR)
        if img0 is None:
            raise RuntimeError("Failed to read the only tile.")
        if int(img0.shape[1]) != int(w_final) or int(img0.shape[0]) != int(h_final):
            img0 = cv2.resize(img0, (int(w_final), int(h_final)), interpolation=cv2.INTER_AREA)
        stage = {
            "name": "layout",
            "tiles_in": 1,
            "tiles_used": 1,
            "strategy": "single_tile",
            "final_megapix": float(final_megapix),
            "blend": str(blend_mode),
            "blend_strength": float(settings.get("blend_strength", 5.0)),
        }
        return LayoutMosaic(
            pano=img0,
            out_w=int(w_final),
            out_h=int(h_final),
            memmap_path=None,
            weights_memmap_path=None,
            stage_meta=stage,
        )

    if progress_cb is not None:
        progress_cb(5.0, "Stitching: estimating affine step vectors…")

    right_possible = int(nrows) * max(0, int(ncols) - 1)
    down_possible = max(0, int(nrows) - 1) * int(ncols)
    if (right_possible >= 3 and down_possible >= 3) or (right_possible >= 3 and int(nrows) <= 1) or (
        down_possible >= 3 and int(ncols) <= 1
    ):
        layout_min_kept = 3
    else:
        layout_min_kept = 1

    step_meta = estimate_step_vectors(
        cv2=cv2,
        np=np,
        by_rc=by_rc,
        nrows=int(nrows),
        ncols=int(ncols),
        tile_w=int(tile_w),
        tile_h=int(tile_h),
        orig_mp=float(orig_mp),
        serpentine=serpentine,
        settings=settings,
        final_megapix=float(final_megapix),
        min_kept=int(layout_min_kept),
    )
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
            v_row_even = (float(step_meta["step_row_px_even"][0]), float(step_meta["step_row_px_even"][1]))  # type: ignore[index]
            v_row_odd = (float(step_meta["step_row_px_odd"][0]), float(step_meta["step_row_px_odd"][1]))  # type: ignore[index]
        except Exception:
            v_row_even = None
            v_row_odd = None

    strategy_settings: dict[str, object] = {
        "strategy": "layout",
        "final_megapix": float(final_megapix),
        **dict(step_meta),
        "blend": str(blend_mode),
        "blend_strength": float(settings.get("blend_strength", 5.0)),
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

    # Optional: refine positions and exposure gains using overlap-based phase correlation.
    refine_positions = bool(settings.get("layout_refine_positions", True))
    refined_by_rc: dict[tuple[int, int], tuple[float, float]] | None = None
    refined_gains: list[float] | None = None
    refine_meta: dict[str, object] = {"enabled": False}
    if bool(refine_positions) and int(tile_count) >= 4 and int(nrows) >= 1 and int(ncols) >= 1:
        if progress_cb is not None:
            progress_cb(7.0, "Stitching: refining tile positions…")

        refined_by_rc, refined_gains, refine_meta = refine_positions_and_gains(
            cv2=cv2,
            np=np,
            entries=entries,
            pos0_by_rc=pos0_by_rc,
            v_col=v_col,
            v_row=v_row,
            v_row_even=v_row_even,
            v_row_odd=v_row_odd,
            tile_w=int(tile_w),
            tile_h=int(tile_h),
            orig_mp=float(orig_mp),
            scale_final=float(scale_final),
            step_meta=dict(step_meta),
            settings=settings,
            progress_cb=progress_cb,
            progress_base=7.0,
            progress_span=2.0,
        )

    strategy_settings["layout_refine_positions"] = dict(refine_meta)
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
    max_px = int(float(settings.get("max_panorama_pixels", 2_000_000_000)))
    area = int(out_w) * int(out_h)
    if int(max_px) > 0 and int(area) > int(max_px):
        raise RuntimeError(
            f"Affine layout would be too large ({out_w}x{out_h} px). "
            "Lower final_megapix, scan a smaller area, or increase max_panorama_pixels (expect huge RAM/disk)."
        )

    if blend_mode == "feather":
        feather_mode = "user" if feather_px is not None else "auto"
        if feather_px is None:
            feather_px = auto_feather_px(
                out_w=int(out_w),
                out_h=int(out_h),
                blend_strength=float(settings.get("blend_strength", 5.0)),
            )
        feather_px = max(0, int(feather_px))
        strategy_settings["layout_feather_px"] = int(feather_px)
        strategy_settings["layout_feather_px_mode"] = str(feather_mode)
        strategy_settings["layout_blend_impl"] = "weighted_feather"

    try:
        inmem_max_bytes = int(float(settings.get("in_memory_max_bytes", 1.5 * 1024 * 1024 * 1024)))
    except Exception:
        inmem_max_bytes = int(1.5 * 1024 * 1024 * 1024)
    use_memmap = bool(settings.get("use_memmap", True))

    black_transparent = bool(settings.get("layout_black_transparent", True))
    black_threshold = int(settings.get("layout_black_threshold", 2))
    black_threshold = max(0, min(32, int(black_threshold)))
    strategy_settings["layout_black_transparent"] = bool(black_transparent)
    strategy_settings["layout_black_threshold"] = int(black_threshold)

    pano, memmap_path, weights_memmap_path = composite_tiles(
        cv2=cv2,
        np=np,
        entries=entries,
        pos_by_rc_f=pos_by_rc_f,
        min_x=float(min_x),
        min_y=float(min_y),
        out_w=int(out_w),
        out_h=int(out_h),
        w_final=int(w_final),
        h_final=int(h_final),
        out_dir=str(out_dir),
        blend_mode=str(blend_mode),
        feather_px=int(feather_px or 0) if blend_mode == "feather" else None,
        inmem_max_bytes=int(inmem_max_bytes),
        use_memmap=bool(use_memmap),
        black_transparent=bool(black_transparent),
        black_threshold=int(black_threshold),
        refined_gains=refined_gains,
        progress_cb=progress_cb,
        progress_base=10.0,
        progress_span=80.0,
    )

    stage = {"name": "layout", "tiles_in": int(tile_count), "tiles_used": int(tile_count), **strategy_settings}
    return LayoutMosaic(
        pano=pano,
        out_w=int(out_w),
        out_h=int(out_h),
        memmap_path=str(memmap_path) if memmap_path is not None else None,
        weights_memmap_path=str(weights_memmap_path) if weights_memmap_path is not None else None,
        stage_meta=stage,
    )
