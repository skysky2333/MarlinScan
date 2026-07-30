from __future__ import annotations

import gc
import json
import math
import os
from pathlib import Path
import tempfile
import traceback
from typing import Callable, Literal

from ..progress import ProgressCallback
from ..raw import load_raw_development_recipe
from .io import is_no_space_error
from .stitching.composite import read_composite_image
from .stitching.layout import stitch_layout_mosaic
from .stitching.openexr import write_scene_linear_exr
from .stitching.output import (
    estimate_output_dpi,
    write_mosaic_tiff,
    write_preview_jpeg,
    write_scene_linear_mosaic_tiff,
)
from .stitching.types import Entry
from .stitching.util import median


def stitch_scan_outputs(
    *,
    tiles: list[dict[str, object]],
    out_dir: str,
    build_pyramidal_tiff: bool,
    tiff_compression: str,
    image_roles: Literal["single", "raw"],
    openexr_helper: str | os.PathLike[str] | None = None,
    progress_cb: ProgressCallback | None = None,
    cancel_cb: Callable[[], None] | None = None,
    stitch_settings: dict[str, object] | None = None,
) -> None:
    if image_roles not in {"single", "raw"}:
        raise ValueError("image_roles must be 'single' or 'raw'")
    if not bool(build_pyramidal_tiff):
        return
    if image_roles == "single":
        _stitch_scan_outputs(
            tiles=tiles,
            out_dir=out_dir,
            build_pyramidal_tiff=build_pyramidal_tiff,
            tiff_compression=tiff_compression,
            image_roles=image_roles,
            openexr_helper=None,
            progress_cb=progress_cb,
            cancel_cb=cancel_cb,
            stitch_settings=stitch_settings,
            scan_params=_load_scan_params(Path(out_dir), required=False),
        )
        return

    recipe_path = Path(out_dir) / "raw_development.json"
    if not recipe_path.is_file():
        raise RuntimeError("RAW stitching requires raw_development.json")
    load_raw_development_recipe(recipe_path)
    scan_params = _load_scan_params(Path(out_dir), required=True)
    _validate_raw_scan_params(scan_params)
    if openexr_helper is None:
        raise RuntimeError("RAW stitching requires a prebuilt OpenEXR helper")
    helper = Path(openexr_helper).expanduser().resolve()
    if not helper.is_file() or not os.access(helper, os.X_OK):
        raise RuntimeError(f"OpenEXR helper is not executable: {helper}")
    _stitch_scan_outputs(
        tiles=tiles,
        out_dir=out_dir,
        build_pyramidal_tiff=build_pyramidal_tiff,
        tiff_compression=tiff_compression,
        image_roles=image_roles,
        openexr_helper=helper,
        progress_cb=progress_cb,
        cancel_cb=cancel_cb,
        stitch_settings=stitch_settings,
        scan_params=scan_params,
    )


def _load_scan_params(out_dir: Path, *, required: bool) -> dict[str, object] | None:
    path = out_dir / "scan_params.json"
    if not path.is_file():
        if required:
            raise RuntimeError("RAW stitching requires scan_params.json")
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Scan parameters must be a JSON object")
    return payload


def _validate_raw_scan_params(scan_params: dict[str, object]) -> None:
    if scan_params.get("image_roles") != "raw":
        raise ValueError("RAW scan parameters must declare raw image roles")
    if scan_params.get("raw_development_recipe") != "raw_development.json":
        raise ValueError("RAW scan parameters must reference raw_development.json")
    for name in ("step_x_mm", "step_y_mm"):
        value = scan_params.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"RAW scan parameter {name} must be a finite non-negative number")
    if not isinstance(scan_params.get("serpentine"), bool):
        raise ValueError("RAW scan parameter serpentine must be boolean")


def _optional_positive_number(scan_params: dict[str, object] | None, name: str) -> float | None:
    if scan_params is None or name not in scan_params:
        return None
    value = scan_params[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"Scan parameter {name} must be a finite positive number")
    return float(value)


def _publish_stitch_outputs(
    *,
    stage_dir: Path,
    output_dir: Path,
    artifact_names: list[str],
    progress_cb: ProgressCallback | None,
    cancel_cb: Callable[[], None] | None,
) -> None:
    for name in artifact_names:
        if Path(name).name != name or not (stage_dir / name).is_file():
            raise RuntimeError(f"Staged stitch artifact is missing: {name}")

    obsolete = [
        path.name
        for path in output_dir.iterdir()
        if path.is_file()
        and (
            (path.name.startswith("mosaic_thumb_") and path.name.endswith(".jpg") and path.name not in artifact_names)
            or path.name in {"mosaic_pyramidal.tif", "stitch_error.txt"}
        )
    ]
    backup_dir = stage_dir / ".previous"
    backup_dir.mkdir()
    previous: dict[str, Path] = {}
    published: list[str] = []
    removed: list[str] = []
    try:
        for name in [*artifact_names, *obsolete]:
            destination = output_dir / name
            if destination.is_file():
                backup = backup_dir / name
                os.link(destination, backup)
                previous[name] = backup

        if progress_cb is not None:
            progress_cb("publish-outputs", "Publishing stitch outputs", 0, len(artifact_names), "files")
        for index, name in enumerate(artifact_names, start=1):
            if cancel_cb is not None:
                cancel_cb()
            os.replace(stage_dir / name, output_dir / name)
            published.append(name)
            if progress_cb is not None:
                progress_cb("publish-outputs", "Publishing stitch outputs", index, len(artifact_names), "files")
        for name in obsolete:
            (output_dir / name).unlink()
            removed.append(name)
    except Exception:
        for name in reversed(published):
            destination = output_dir / name
            backup = previous.get(name)
            if backup is None:
                destination.unlink(missing_ok=True)
            else:
                os.replace(backup, destination)
        for name in removed:
            os.replace(previous[name], output_dir / name)
        raise


def _stitch_scan_outputs(
    *,
    tiles: list[dict[str, object]],
    out_dir: str,
    build_pyramidal_tiff: bool,
    tiff_compression: str,
    image_roles: Literal["single", "raw"],
    openexr_helper: Path | None,
    progress_cb: ProgressCallback | None,
    cancel_cb: Callable[[], None] | None,
    stitch_settings: dict[str, object] | None,
    scan_params: dict[str, object] | None,
) -> None:
    """
    Stitch scan tiles into full-resolution and pyramidal mosaic TIFFs + JPEG preview.

    This uses a layout-based affine pipeline:
    - estimate neighbor step vectors on downscaled tiles
    - (optional) refine tile positions and per-tile exposure gains on overlaps
    - composite at final resolution with weighted feather blending

    Outputs (in out_dir):
    - mosaic_scene_linear.exr (RAW projects)
    - mosaic_full.tif
    - mosaic_pyramidal.ome.tif
    - mosaic_thumb_2000.jpg (size configurable)
    - stitch_meta.json
    - stitch_error.txt (only on failure)
    """
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

    def _emit_progress(
        phase: str,
        label: str,
        completed: int,
        total: int | None,
        unit: str,
    ) -> None:
        if progress_cb is not None:
            progress_cb(phase, label, completed, total, unit)

    def _check_cancel() -> None:
        if cancel_cb is not None:
            cancel_cb()

    def _write_err(stage: str, exc: Exception) -> None:
        try:
            with open(os.path.join(out_dir, "stitch_error.txt"), "a", encoding="utf-8") as f:
                f.write(f"[{stage}] {exc}\n")
                f.write(traceback.format_exc())
                f.write("\n\n")
        except Exception:
            pass

    # Parse tiles.
    entries: list[Entry] = []
    display_by_rc: dict[tuple[int, int], str] = {}
    max_r = -1
    max_c = -1
    mm_by_rc: dict[tuple[int, int], tuple[float, float]] = {}
    if image_roles not in {"single", "raw"}:
        raise ValueError("image_roles must be 'single' or 'raw'")
    if not tiles:
        raise RuntimeError("No tiles to stitch")
    seen: set[tuple[int, int]] = set()
    for index, t in enumerate(tiles):
        if not isinstance(t, dict):
            raise RuntimeError(f"Tile {index} must be an object")
        r = t.get("row")
        c = t.get("col")
        if isinstance(r, bool) or not isinstance(r, int) or r < 0:
            raise RuntimeError(f"Tile {index} has an invalid row")
        if isinstance(c, bool) or not isinstance(c, int) or c < 0:
            raise RuntimeError(f"Tile {index} has an invalid column")
        rc = (r, c)
        if rc in seen:
            raise RuntimeError(f"Duplicate tile grid cell: row {r}, column {c}")
        seen.add(rc)
        fn = str(t.get("file", "")).strip()
        if not fn:
            raise RuntimeError(f"Tile {index} is missing file")
        if image_roles == "raw":
            if "composite_file" in t:
                raise RuntimeError(f"Tile {index} uses the obsolete composite_file role")
            raw_fn = str(t.get("raw_file", "")).strip()
            scene_linear_fn = str(t.get("scene_linear_file", "")).strip()
            display_fn = str(t.get("display_file", "")).strip()
            if not raw_fn or not scene_linear_fn or not display_fn:
                raise RuntimeError(
                    f"Tile {index} must include file, raw_file, scene_linear_file, and display_file"
                )
            capture_stem = os.path.splitext(os.path.basename(fn))[0].casefold()
            if (
                os.path.splitext(os.path.basename(raw_fn))[0].casefold() != capture_stem
                or os.path.splitext(os.path.basename(display_fn))[0].casefold() != capture_stem
                or os.path.splitext(os.path.basename(scene_linear_fn))[0].casefold()
                != f"{capture_stem}_scene_linear"
            ):
                raise RuntimeError(f"Tile {index} image roles do not share a matching capture stem")
            raw_path = os.path.join(out_dir, raw_fn)
            if not os.path.exists(raw_path):
                raise RuntimeError(f"RAW tile does not exist: {raw_fn}")
            display_path = os.path.join(out_dir, display_fn)
            if not os.path.exists(display_path):
                raise RuntimeError(f"Display tile does not exist: {display_fn}")
            display_by_rc[rc] = display_path
            composite_fn = scene_linear_fn
        else:
            composite_fn = fn
        alignment_path = os.path.join(out_dir, fn)
        composite_path = os.path.join(out_dir, composite_fn)
        if not os.path.exists(alignment_path):
            raise RuntimeError(f"Alignment tile does not exist: {fn}")
        if not os.path.exists(composite_path):
            raise RuntimeError(f"Composite tile does not exist: {composite_fn}")
        entries.append(Entry(r, c, alignment_path, composite_path))
        if "x_mm" in t or "y_mm" in t or image_roles == "raw":
            x_mm = float(t["x_mm"])
            y_mm = float(t["y_mm"])
            if not math.isfinite(x_mm) or not math.isfinite(y_mm):
                raise RuntimeError(f"Tile {index} has invalid motion coordinates")
            mm_by_rc[rc] = (x_mm, y_mm)
        max_r = max(max_r, r)
        max_c = max(max_c, c)

    expected = {(row, col) for row in range(max_r + 1) for col in range(max_c + 1)}
    if seen != expected:
        missing = min(expected - seen)
        raise RuntimeError(f"Missing tile grid cell: row {missing[0]}, column {missing[1]}")

    entries.sort(key=lambda e: (int(e.row), int(e.col)))
    by_rc: dict[tuple[int, int], str] = {(int(e.row), int(e.col)): str(e.alignment_path) for e in entries}
    nrows = int(max_r) + 1
    ncols = int(max_c) + 1

    tile_w = 0
    tile_h = 0
    composite_dtype: object | None = None
    _emit_progress("stitch-validate", "Validating stitch tiles", 0, len(entries), "tiles")
    for entry_index, entry in enumerate(entries):
        _check_cancel()
        alignment = cv2.imread(str(entry.alignment_path), cv2.IMREAD_COLOR)
        if alignment is None:
            raise RuntimeError(f"Failed to read alignment tile: {os.path.basename(entry.alignment_path)}")
        if alignment.ndim != 3 or int(alignment.shape[2]) != 3 or np.dtype(alignment.dtype) != np.dtype(np.uint8):
            raise RuntimeError(f"Alignment tile must be an 8-bit color image: {os.path.basename(entry.alignment_path)}")
        if tile_w == 0:
            tile_h, tile_w = (int(alignment.shape[0]), int(alignment.shape[1]))
        elif int(alignment.shape[1]) != tile_w or int(alignment.shape[0]) != tile_h:
            raise RuntimeError(f"Alignment tile dimensions do not match the scan: {os.path.basename(entry.alignment_path)}")
        composite = read_composite_image(
            cv2=cv2,
            np=np,
            path=entry.composite_path,
            expected_w=tile_w,
            expected_h=tile_h,
            expected_dtype=composite_dtype,
        )
        if composite_dtype is None:
            composite_dtype = np.dtype(composite.dtype)
        if image_roles == "raw":
            read_composite_image(
                cv2=cv2,
                np=np,
                path=display_by_rc[(int(entry.row), int(entry.col))],
                expected_w=tile_w,
                expected_h=tile_h,
                expected_dtype=np.uint16,
            )
        _emit_progress(
            "stitch-validate",
            "Validating stitch tiles",
            entry_index + 1,
            len(entries),
            "tiles",
        )
    if tile_w <= 0 or tile_h <= 0 or composite_dtype is None:
        raise RuntimeError("Invalid tile images.")
    if image_roles == "raw" and np.dtype(composite_dtype) != np.dtype(np.float32):
        raise RuntimeError("RAW scene-linear tiles must be float32")
    composite_dtype_name = str(np.dtype(composite_dtype).name)
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
    if scan_params is not None:
        if "step_x_mm" in scan_params:
            value = scan_params["step_x_mm"]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("Scan parameter step_x_mm must be finite")
            step_x_mm = float(value)
        if "step_y_mm" in scan_params:
            value = scan_params["step_y_mm"]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("Scan parameter step_y_mm must be finite")
            step_y_mm = float(value)
        if "serpentine" in scan_params:
            value = scan_params["serpentine"]
            if not isinstance(value, bool):
                raise ValueError("Scan parameter serpentine must be boolean")
            serpentine = value
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
    layout_settings = dict(s_in)
    if image_roles == "raw":
        layout_settings["layout_black_transparent"] = False
        layout_settings["layout_exposure_compensate"] = False

    stage_context = tempfile.TemporaryDirectory(prefix=".stitch-", dir=out_dir)
    stage_dir = Path(stage_context.name)
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
            out_dir=str(stage_dir),
            settings=layout_settings,
            final_megapix=float(stage1_final_megapix),
            composite_dtype=str(composite_dtype_name),
            progress_cb=_emit_progress,
            cancel_cb=_check_cancel,
        )

        mosaic_path = str(stage_dir / "mosaic_full.tif")
        pyramidal_path = str(stage_dir / "mosaic_pyramidal.ome.tif")

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
            frame_width_mm=_optional_positive_number(scan_params, "frame_width_mm"),
            frame_height_mm=_optional_positive_number(scan_params, "frame_height_mm"),
        )

        if hasattr(mosaic.pano, "flush"):
            mosaic.pano.flush()  # type: ignore[union-attr]

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
        scene_linear_path: str | None = None
        temporary_backing_path: str | None = None
        if image_roles == "raw":
            if openexr_helper is None:
                raise RuntimeError("RAW stitching requires an OpenEXR helper")
            if mosaic.memmap_path is not None:
                backing_path = Path(mosaic.memmap_path)
            else:
                backing_path = stage_dir / "_mosaic_scene_linear.f32"
                contiguous = np.ascontiguousarray(mosaic.pano)
                if contiguous.shape != (int(mosaic.out_h), int(mosaic.out_w), 3):
                    raise RuntimeError("Scene-linear mosaic dimensions do not match its output canvas")
                contiguous.tofile(backing_path)
                temporary_backing_path = str(backing_path)
            scene_linear_path = str(stage_dir / "mosaic_scene_linear.exr")
            write_scene_linear_exr(
                helper_path=openexr_helper,
                backing_path=backing_path,
                output_path=scene_linear_path,
                shape=(int(mosaic.out_h), int(mosaic.out_w), 3),
                dtype=mosaic.pano.dtype,
                compression=str(s_in.get("openexr_compression", "zip")).strip().lower(),
                tile_size=int(s_in.get("openexr_tile_size", 256)),
                working_space="linear-rec2020",
                input_order="bgr",
                progress_cb=_emit_progress,
                cancel_cb=_check_cancel,
            )

        tiff_writer = write_scene_linear_mosaic_tiff if image_roles == "raw" else write_mosaic_tiff
        _check_cancel()
        did_set = tiff_writer(
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
            progress_cb=_emit_progress,
            cancel_cb=_check_cancel,
        )
        _check_cancel()
        if dpi_meta is not None:
            dpi_meta["set_in_file"] = bool(did_set)

        _check_cancel()
        tiff_writer(
            pano=mosaic.pano,
            memmap_path=mosaic.memmap_path,
            out_w=int(mosaic.out_w),
            out_h=int(mosaic.out_h),
            mosaic_path=str(pyramidal_path),
            tiff_compression=str(tiff_compression),
            px_per_mm_target=px_per_mm_target,
            tiff_tile=True,
            tiff_tile_width=int(s_in.get("tiff_tile_width", 256) or 256),
            tiff_tile_height=int(s_in.get("tiff_tile_height", 256) or 256),
            tiff_predictor=str(predictor_pref) if predictor_pref is not None else None,
            pyramidal=True,
            progress_cb=_emit_progress,
            cancel_cb=_check_cancel,
        )
        _check_cancel()

        stage_meta = dict(mosaic.stage_meta)
        out_w = int(mosaic.out_w)
        out_h = int(mosaic.out_h)
        memmap_path = str(mosaic.memmap_path) if mosaic.memmap_path is not None else None
        weights_memmap_path = str(mosaic.weights_memmap_path) if mosaic.weights_memmap_path is not None else None

        del mosaic
        gc.collect()
        scratch_paths = [
            path
            for path in (memmap_path, weights_memmap_path, temporary_backing_path)
            if path is not None and os.path.exists(path)
        ]

        preview_max_dim = int(s_in.get("preview_max_dim", 2000) or 2000)
        preview_quality = int(s_in.get("preview_quality", 85) or 85)
        preview_max_dim = max(256, min(8000, int(preview_max_dim)))
        preview_quality = max(30, min(95, int(preview_quality)))
        _check_cancel()
        preview_path = write_preview_jpeg(
            mosaic_path=str(mosaic_path),
            out_dir=str(stage_dir),
            max_dim=int(preview_max_dim),
            quality=int(preview_quality),
            progress_cb=_emit_progress,
            cancel_cb=_check_cancel,
        )
        _check_cancel()

        meta: dict[str, object] = {
            "method": "affine-layout",
            "tiles": int(tile_count),
            "rows": int(nrows),
            "cols": int(ncols),
            "tile_size_px": [int(tile_w), int(tile_h)],
            "mosaic_size_px": [int(out_w), int(out_h)],
            "composite_dtype": str(composite_dtype_name),
            "outputs": {
                "full_tiff": os.path.basename(mosaic_path),
                "pyramidal_tiff": os.path.basename(pyramidal_path),
                "preview_jpeg": os.path.basename(preview_path),
            },
            "stages": [stage_meta],
            "settings": dict(layout_settings),
            "versions": {
                "opencv": str(getattr(cv2, "__version__", "?")),
            },
        }
        if scene_linear_path is not None:
            outputs = meta["outputs"]
            if not isinstance(outputs, dict):
                raise RuntimeError("Stitch output metadata is invalid")
            outputs["scene_linear_exr"] = os.path.basename(scene_linear_path)
            meta["raw_development_recipe"] = "raw_development.json"
        if dpi_meta is not None:
            meta["dpi"] = dict(dpi_meta)
        metadata_path = str(stage_dir / "stitch_meta.json")
        metadata_temp_path = f"{metadata_path}.tmp"
        _emit_progress("write-metadata", "Writing stitch metadata", 0, 1, "files")
        _check_cancel()
        with open(metadata_temp_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, sort_keys=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(metadata_temp_path, metadata_path)
        _emit_progress("write-metadata", "Writing stitch metadata", 1, 1, "files")
        _check_cancel()

        if scratch_paths:
            _emit_progress("stitch-cleanup", "Cleaning stitch scratch files", 0, len(scratch_paths), "files")
            for index, path in enumerate(scratch_paths, start=1):
                _check_cancel()
                os.remove(path)
                _emit_progress(
                    "stitch-cleanup",
                    "Cleaning stitch scratch files",
                    index,
                    len(scratch_paths),
                    "files",
                )

        artifact_names = [
            os.path.basename(mosaic_path),
            os.path.basename(pyramidal_path),
            os.path.basename(preview_path),
        ]
        if scene_linear_path is not None:
            artifact_names.insert(0, os.path.basename(scene_linear_path))
        artifact_names.append(os.path.basename(metadata_path))
        _publish_stitch_outputs(
            stage_dir=stage_dir,
            output_dir=Path(out_dir),
            artifact_names=artifact_names,
            progress_cb=progress_cb,
            cancel_cb=cancel_cb,
        )
        stage_context.cleanup()
    except Exception as exc:
        stage_context.cleanup()
        _write_err("stitch", exc)
        if is_no_space_error(exc):
            raise RuntimeError(
                "No space left on device while stitching. Free disk space or choose a different output folder."
            ) from exc
        raise
