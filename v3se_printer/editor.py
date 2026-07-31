from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import gc
import json
import math
import os
from pathlib import Path
import re
import shutil
from typing import Callable, Literal
from urllib.parse import quote

import cv2  # type: ignore
import numpy as np  # type: ignore
import pyvips  # type: ignore

from .color import REC2020_TO_SRGB_MATRIX, SRGB_TO_REC2020_MATRIX, require_srgb_icc_profile
from .progress import ProgressCallback
from .raw import develop_nef_scene, load_raw_development_recipe
from .scan.stitching.composite import composite_tiles, feather_weight
from .scan.stitching.openexr import build_openexr_helper, write_scene_linear_exr
from .scan.stitching.output import write_preview_jpeg, write_scene_linear_mosaic_tiff
from .scan.stitching.types import Entry


EDITOR_RESULT_FILES = {
    "full_tiff": "mosaic_full.tif",
    "pyramidal_tiff": "mosaic_pyramidal.ome.tif",
    "working_linear_exr": "mosaic_working_linear.exr",
    "preview_jpeg": "mosaic_thumb_2000.jpg",
    "edit_recipe": "edit_recipe.json",
    "revision_metadata": "revision_meta.json",
}
REC2020_LUMINANCE = np.asarray([0.2627, 0.6780, 0.0593], dtype=np.float32)
REC2020_TO_SRGB = np.asarray(REC2020_TO_SRGB_MATRIX, dtype=np.float32)
SRGB_TO_REC2020 = np.asarray(SRGB_TO_REC2020_MATRIX, dtype=np.float32)
IDENTITY_TONE_CURVE = (0.0, 0.25, 0.5, 0.75, 1.0)
NEUTRAL_HSL = (0.0,) * 8
REVISION_PATTERN = re.compile(r"revision-(\d{3,})$")


@dataclass(frozen=True)
class EditRecipe:
    version: int = 2
    material: Literal["positive", "color_negative", "bw_negative"] = "positive"
    exposure_ev: float = 0.0
    temperature: float = 0.0
    tint: float = 0.0
    contrast: float = 0.0
    highlights: float = 0.0
    shadows: float = 0.0
    black_point: float = 0.0
    white_point: float = 1.0
    saturation: float = 1.0
    red_balance: float = 1.0
    green_balance: float = 1.0
    blue_balance: float = 1.0
    film_base_red: float = 1.0
    film_base_green: float = 1.0
    film_base_blue: float = 1.0
    film_density: float = 1.0
    film_dmin: float = 0.0
    film_dmax: float = 4.0
    film_red_ratio: float = 1.0
    film_blue_ratio: float = 1.0
    slide_fade: float = 0.0
    slide_black_red: float = 0.0
    slide_black_green: float = 0.0
    slide_black_blue: float = 0.0
    slide_white_red: float = 1.0
    slide_white_green: float = 1.0
    slide_white_blue: float = 1.0
    tone_curve: tuple[float, float, float, float, float] = IDENTITY_TONE_CURVE
    hsl_hue: tuple[float, float, float, float, float, float, float, float] = NEUTRAL_HSL
    hsl_saturation: tuple[float, float, float, float, float, float, float, float] = NEUTRAL_HSL
    hsl_lightness: tuple[float, float, float, float, float, float, float, float] = NEUTRAL_HSL

    def __post_init__(self) -> None:
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version != 2:
            raise ValueError("Edit recipe version must be 2")
        if self.material not in {"positive", "color_negative", "bw_negative"}:
            raise ValueError("Edit material must be positive, color_negative, or bw_negative")
        bounded = {
            "exposure_ev": (-8.0, 8.0),
            "temperature": (-1.0, 1.0),
            "tint": (-1.0, 1.0),
            "contrast": (-1.0, 1.0),
            "highlights": (-1.0, 1.0),
            "shadows": (-1.0, 1.0),
            "black_point": (-1.0, 0.95),
            "white_point": (0.01, 8.0),
            "saturation": (0.0, 3.0),
            "red_balance": (0.1, 4.0),
            "green_balance": (0.1, 4.0),
            "blue_balance": (0.1, 4.0),
            "film_base_red": (0.01, 4.0),
            "film_base_green": (0.01, 4.0),
            "film_base_blue": (0.01, 4.0),
            "film_density": (0.1, 4.0),
            "film_dmin": (-4.0, 4.0),
            "film_dmax": (-4.0, 8.0),
            "film_red_ratio": (0.1, 4.0),
            "film_blue_ratio": (0.1, 4.0),
            "slide_fade": (0.0, 1.0),
        }
        for name, (minimum, maximum) in bounded.items():
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not minimum <= value <= maximum
            ):
                raise ValueError(f"Edit recipe {name} must be from {minimum:g} through {maximum:g}")
        if self.black_point >= self.white_point:
            raise ValueError("Edit recipe black point must be below white point")
        if self.film_dmin >= self.film_dmax:
            raise ValueError("Edit recipe film dmin must be below film dmax")
        slide_black = (self.slide_black_red, self.slide_black_green, self.slide_black_blue)
        slide_white = (self.slide_white_red, self.slide_white_green, self.slide_white_blue)
        if not all(
            not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
            for value in (*slide_black, *slide_white)
        ):
            raise ValueError("Edit recipe slide points must be finite numbers")
        if any(black >= white for black, white in zip(slide_black, slide_white, strict=True)):
            raise ValueError("Edit recipe slide black points must be below slide white points")
        self._validate_tuple("tone_curve", self.tone_curve, 5, 0.0, 1.0)
        self._validate_tuple("hsl_hue", self.hsl_hue, 8, -30.0, 30.0)
        self._validate_tuple("hsl_saturation", self.hsl_saturation, 8, -1.0, 1.0)
        self._validate_tuple("hsl_lightness", self.hsl_lightness, 8, -1.0, 1.0)

    @staticmethod
    def _validate_tuple(name: str, values: tuple[float, ...], size: int, minimum: float, maximum: float) -> None:
        if not isinstance(values, tuple) or len(values) != size or not all(
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value)
            and minimum <= value <= maximum
            for value in values
        ):
            raise ValueError(
                f"Edit recipe {name} must contain {size} finite values from {minimum:g} through {maximum:g}"
            )


@dataclass(frozen=True)
class EditorTile:
    row: int
    col: int
    raw_path: Path
    alignment_path: Path
    scene_linear_path: Path


@dataclass(frozen=True)
class EditorProject:
    root: Path
    tiles: tuple[EditorTile, ...]
    source_size: tuple[int, int]
    tile_size: tuple[int, int]
    canvas_size: tuple[int, int]
    positions: dict[tuple[int, int], tuple[int, int]]
    stage: dict[str, object]
    stitch_metadata: dict[str, object]


def edit_recipe_from_dict(payload: dict[str, object]) -> EditRecipe:
    if not isinstance(payload, dict):
        raise ValueError("Edit recipe must be an object")
    expected = {field.name for field in fields(EditRecipe)}
    if set(payload) != expected:
        raise ValueError("Edit recipe fields do not match version 2")
    values = dict(payload)
    for name, size in (("tone_curve", 5), ("hsl_hue", 8), ("hsl_saturation", 8), ("hsl_lightness", 8)):
        value = values[name]
        if not isinstance(value, (list, tuple)) or len(value) != size:
            raise ValueError(f"Edit recipe {name} must contain {size} values")
        values[name] = tuple(value)
    return EditRecipe(**values)  # type: ignore[arg-type]


def editor_results(revision_dir: str | Path | None) -> dict[str, dict[str, str] | None]:
    root = None if revision_dir is None else Path(revision_dir)
    return {
        artifact: (
            {"name": filename, "download_url": f"/api/editor/results/{artifact}"}
            if root is not None and (root / filename).is_file()
            else None
        )
        for artifact, filename in EDITOR_RESULT_FILES.items()
    }


def discover_editor_projects(scan_roots: tuple[str | Path, ...]) -> list[dict[str, object]]:
    roots = tuple(dict.fromkeys(Path(root).expanduser().resolve() for root in scan_roots))
    candidates = list(
        dict.fromkeys(
            candidate
            for root in roots
            if root.is_dir()
            for candidate in root.glob("scan_*")
        )
    )
    projects: list[dict[str, object]] = []
    for candidate in candidates:
        if not candidate.is_dir() or not all(
            (candidate / name).is_file()
            for name in ("tiles.json", "stitch_meta.json", "raw_development.json", "scan_params.json")
        ):
            continue
        project = load_editor_project(candidate, roots)
        projects.append(editor_project_summary(project))
    return sorted(projects, key=lambda item: str(item["name"]), reverse=True)


def editor_project_summary(project: EditorProject) -> dict[str, object]:
    revisions_root = project.root / "revisions"
    revisions = [] if not revisions_root.is_dir() else sorted(
        path.name
        for path in revisions_root.iterdir()
        if path.is_dir() and REVISION_PATTERN.fullmatch(path.name) and (path / "revision_meta.json").is_file()
    )
    return {
        "directory": str(project.root),
        "name": project.root.name,
        "tile_count": len(project.tiles),
        "size": [project.canvas_size[0], project.canvas_size[1]],
        "canvas_size": [project.canvas_size[0], project.canvas_size[1]],
        "tile_size": [project.tile_size[0], project.tile_size[1]],
        "revision_count": len(revisions),
    }


def editor_project_details(project: EditorProject) -> dict[str, object]:
    summary = editor_project_summary(project)
    revisions_root = project.root / "revisions"
    revisions = [] if not revisions_root.is_dir() else sorted(
        path.name
        for path in revisions_root.iterdir()
        if path.is_dir() and REVISION_PATTERN.fullmatch(path.name) and (path / "revision_meta.json").is_file()
    )
    tiles = []
    canvas_width, canvas_height = project.canvas_size
    tile_width, tile_height = project.tile_size
    for index, tile in enumerate(project.tiles):
        x, y = project.positions[(tile.row, tile.col)]
        tiles.append(
            {
                "index": index,
                "row": tile.row,
                "col": tile.col,
                "label": f"R{tile.row + 1} C{tile.col + 1} · {tile.raw_path.name}",
                "bounds": [
                    x / canvas_width,
                    y / canvas_height,
                    min(x + tile_width, canvas_width) / canvas_width,
                    min(y + tile_height, canvas_height) / canvas_height,
                ],
            }
        )
    return {
        **summary,
        "revisions": revisions,
        "tiles": tiles,
        "preview_url": f"/api/editor/original-preview?project_dir={quote(str(project.root), safe='')}",
    }


def load_editor_project(project_dir: str | Path, allowed_roots: tuple[str | Path, ...]) -> EditorProject:
    root = Path(project_dir).expanduser().resolve()
    resolved_allowed = tuple(Path(path).expanduser().resolve() for path in allowed_roots)
    if not any(root == allowed or allowed in root.parents for allowed in resolved_allowed):
        raise ValueError("Editor project must be inside an allowed scan folder")
    if not root.is_dir():
        raise FileNotFoundError(root)
    tile_records = _json_array(root / "tiles.json")
    stitch_metadata = _json_object(root / "stitch_meta.json")
    _json_object(root / "raw_development.json")
    scan_params = _json_object(root / "scan_params.json")
    if scan_params.get("image_roles") != "raw":
        raise ValueError("Editor projects must contain RAW image roles")
    stages = stitch_metadata.get("stages")
    if not isinstance(stages, list) or len(stages) != 1 or not isinstance(stages[0], dict):
        raise ValueError("Editor project must contain one saved stitch stage")
    stage = stages[0]
    source_size = _size(stage.get("source_size_px"), "source tile")
    tile_size = _size(stage.get("tile_size_px"), "rendered tile")
    canvas_size = _size(stage.get("canvas_size_px"), "mosaic canvas")
    transforms = stage.get("tile_transforms")
    if not isinstance(transforms, list) or len(transforms) != len(tile_records):
        raise ValueError("Saved tile transforms must match the tile manifest")
    positions: dict[tuple[int, int], tuple[int, int]] = {}
    for transform in transforms:
        if not isinstance(transform, dict):
            raise ValueError("Saved tile transform must be an object")
        row = _nonnegative_integer(transform.get("row"), "transform row")
        col = _nonnegative_integer(transform.get("col"), "transform column")
        applied = transform.get("applied_position_px")
        if (
            not isinstance(applied, list)
            or len(applied) != 2
            or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in applied)
        ):
            raise ValueError("Saved tile position must contain two non-negative integers")
        if (row, col) in positions:
            raise ValueError("Saved tile transforms contain a duplicate grid cell")
        positions[(row, col)] = (applied[0], applied[1])
    tiles: list[EditorTile] = []
    seen: set[tuple[int, int]] = set()
    for index, record in enumerate(tile_records):
        if not isinstance(record, dict):
            raise ValueError(f"Tile {index} must be an object")
        row = _nonnegative_integer(record.get("row"), f"tile {index} row")
        col = _nonnegative_integer(record.get("col"), f"tile {index} column")
        key = (row, col)
        if key in seen:
            raise ValueError("Tile manifest contains a duplicate grid cell")
        seen.add(key)
        raw_path = _project_file(root, record.get("raw_file"), f"tile {index} RAW")
        alignment_path = _project_file(root, record.get("file"), f"tile {index} alignment")
        scene_path = _project_file(root, record.get("scene_linear_file"), f"tile {index} scene-linear")
        for path in (raw_path, alignment_path, scene_path):
            if not path.is_file():
                raise FileNotFoundError(path)
        tiles.append(EditorTile(row, col, raw_path, alignment_path, scene_path))
    if seen != set(positions):
        raise ValueError("Saved tile transforms do not match the tile manifest grid")
    tiles.sort(key=lambda tile: (tile.row, tile.col))
    return EditorProject(
        root,
        tuple(tiles),
        source_size,
        tile_size,
        canvas_size,
        positions,
        dict(stage),
        stitch_metadata,
    )


def apply_edit_recipe(rgb: np.ndarray, recipe: EditRecipe) -> np.ndarray:
    if rgb.ndim != 3 or rgb.shape[2] != 3 or rgb.dtype != np.float32 or not np.isfinite(rgb).all():
        raise ValueError("Editor input must be finite float32 RGB")
    result = _convert_material(rgb.copy(), recipe)
    if (
        recipe.temperature != 0.0
        or recipe.tint != 0.0
        or recipe.red_balance != 1.0
        or recipe.green_balance != 1.0
        or recipe.blue_balance != 1.0
    ):
        temperature = np.float32(2.0**recipe.temperature)
        tint = np.float32(2.0**recipe.tint)
        result *= np.asarray(
            [
                recipe.red_balance * temperature * math.sqrt(float(tint)),
                recipe.green_balance / tint,
                recipe.blue_balance / temperature * math.sqrt(float(tint)),
            ],
            dtype=np.float32,
        )
    if recipe.exposure_ev != 0.0:
        result *= np.float32(2.0**recipe.exposure_ev)
    if recipe.black_point != 0.0 or recipe.white_point != 1.0:
        result -= np.float32(recipe.black_point)
        result /= np.float32(recipe.white_point - recipe.black_point)
    if recipe.shadows != 0.0 or recipe.highlights != 0.0:
        unit = np.clip(result, 0.0, 1.0)
        if recipe.shadows != 0.0:
            result += np.float32(recipe.shadows * 0.25) * np.square(1.0 - unit)
        if recipe.highlights != 0.0:
            result += np.float32(recipe.highlights * 0.25) * np.square(unit)
    if recipe.contrast != 0.0:
        result -= np.float32(0.18)
        result *= np.float32(2.0**recipe.contrast)
        result += np.float32(0.18)
    if recipe.saturation != 1.0:
        luminance = result @ REC2020_LUMINANCE
        result = luminance[:, :, None] + (result - luminance[:, :, None]) * np.float32(recipe.saturation)
    result = _apply_tone_curve(result, recipe.tone_curve)
    result = _apply_hsl(result, recipe)
    if not np.isfinite(result).all():
        raise RuntimeError("Edit recipe produced non-finite scene-linear values")
    return result.astype(np.float32, copy=False)


def _convert_material(rgb: np.ndarray, recipe: EditRecipe) -> np.ndarray:
    if recipe.material == "positive":
        if recipe.slide_fade == 0.0:
            return rgb
        black = np.asarray(
            [recipe.slide_black_red, recipe.slide_black_green, recipe.slide_black_blue],
            dtype=np.float32,
        )
        white = np.asarray(
            [recipe.slide_white_red, recipe.slide_white_green, recipe.slide_white_blue],
            dtype=np.float32,
        )
        normalized = (rgb - black) / (white - black)
        rgb += np.float32(recipe.slide_fade) * (normalized - rgb)
        return rgb
    base = np.asarray(
        [recipe.film_base_red, recipe.film_base_green, recipe.film_base_blue],
        dtype=np.float32,
    )
    np.maximum(rgb / base, np.float32(2.0**-16), out=rgb)
    np.log2(rgb, out=rgb)
    rgb *= np.asarray(
        [
            -recipe.film_density * recipe.film_red_ratio,
            -recipe.film_density,
            -recipe.film_density * recipe.film_blue_ratio,
        ],
        dtype=np.float32,
    )
    rgb -= np.float32(recipe.film_dmin)
    rgb /= np.float32(recipe.film_dmax - recipe.film_dmin)
    if recipe.material == "bw_negative":
        luminance = rgb @ REC2020_LUMINANCE
        rgb = np.repeat(luminance[:, :, None], 3, axis=2)
    return rgb


def _apply_tone_curve(rgb: np.ndarray, curve: tuple[float, float, float, float, float]) -> np.ndarray:
    if curve == IDENTITY_TONE_CURVE:
        return rgb
    luminance = rgb @ REC2020_LUMINANCE
    bounded = np.clip(luminance, 0.0, 1.0)
    mapped = np.interp(
        bounded,
        np.asarray(IDENTITY_TONE_CURVE, dtype=np.float32),
        np.asarray(curve, dtype=np.float32),
    ).astype(np.float32)
    rgb += (mapped - bounded)[:, :, None]
    return rgb


def _apply_hsl(rgb: np.ndarray, recipe: EditRecipe) -> np.ndarray:
    if (
        recipe.hsl_hue == NEUTRAL_HSL
        and recipe.hsl_saturation == NEUTRAL_HSL
        and recipe.hsl_lightness == NEUTRAL_HSL
    ):
        return rgb
    linear_srgb = rgb @ REC2020_TO_SRGB.T
    encoded = _encode_srgb(linear_srgb)
    bounded = np.clip(encoded, 0.0, 1.0)
    hls = cv2.cvtColor(np.ascontiguousarray(bounded), cv2.COLOR_RGB2HLS)
    position = hls[:, :, 0] / np.float32(45.0)
    lower = np.floor(position).astype(np.intp) % 8
    fraction = position - np.floor(position)

    def interpolate(values: tuple[float, ...]) -> np.ndarray:
        controls = np.asarray(values, dtype=np.float32)
        return controls[lower] * (1.0 - fraction) + controls[(lower + 1) % 8] * fraction

    hls[:, :, 0] = np.mod(hls[:, :, 0] + interpolate(recipe.hsl_hue), np.float32(360.0))
    hls[:, :, 1] = _adjust_unit_axis(hls[:, :, 1], interpolate(recipe.hsl_lightness))
    hls[:, :, 2] = _adjust_unit_axis(hls[:, :, 2], interpolate(recipe.hsl_saturation))
    adjusted = cv2.cvtColor(hls, cv2.COLOR_HLS2RGB)
    encoded += adjusted - bounded
    return _decode_srgb(encoded) @ SRGB_TO_REC2020.T


def _adjust_unit_axis(values: np.ndarray, adjustment: np.ndarray) -> np.ndarray:
    return np.where(
        adjustment < 0.0,
        values * (1.0 + adjustment),
        values + (1.0 - values) * adjustment,
    ).astype(np.float32)


def _encode_srgb(linear: np.ndarray) -> np.ndarray:
    encoded = linear * np.float32(12.92)
    high = linear > np.float32(0.0031308)
    encoded[high] = (
        np.float32(1.055) * np.power(linear[high], np.float32(1.0 / 2.4)) - np.float32(0.055)
    )
    return encoded


def _decode_srgb(encoded: np.ndarray) -> np.ndarray:
    linear = encoded / np.float32(12.92)
    high = encoded > np.float32(0.04045)
    linear[high] = np.power(
        (encoded[high] + np.float32(0.055)) / np.float32(1.055),
        np.float32(2.4),
    )
    return linear


def render_editor_preview(
    project: EditorProject,
    recipe: EditRecipe,
    source: Literal["mosaic", "tile"],
    tile_index: int | None,
    *,
    max_dim: int = 1600,
    quality: int = 90,
) -> bytes:
    if isinstance(max_dim, bool) or not isinstance(max_dim, int) or not 256 <= max_dim <= 2400:
        raise ValueError("Editor preview size must be from 256 through 2400 pixels")
    if isinstance(quality, bool) or not isinstance(quality, int) or not 30 <= quality <= 95:
        raise ValueError("Editor preview quality must be from 30 through 95")
    if source == "tile":
        if isinstance(tile_index, bool) or not isinstance(tile_index, int) or not 0 <= tile_index < len(project.tiles):
            raise ValueError("Editor tile preview requires a valid tile index")
        rgb = _scaled_scene(project.tiles[tile_index].scene_linear_path, project.source_size, max_dim)
        edited = apply_edit_recipe(rgb, recipe)
    elif source == "mosaic":
        if tile_index is not None:
            raise ValueError("Mosaic preview does not accept a tile index")
        edited = _mosaic_preview(project, recipe, max_dim)
    else:
        raise ValueError("Editor preview source must be mosaic or tile")
    display = _scene_to_srgb8(edited)
    encoded, data = cv2.imencode(".jpg", display[:, :, ::-1], [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not encoded:
        raise RuntimeError("Failed to encode editor preview")
    return data.tobytes()


def apply_editor_revision(
    project: EditorProject,
    recipe: EditRecipe,
    *,
    openexr_source: str | Path,
    progress_cb: ProgressCallback | None = None,
    cancel_cb: Callable[[], None] | None = None,
) -> Path:
    revisions_root = project.root / "revisions"
    revisions_root.mkdir(exist_ok=True)
    numbers = [
        int(match.group(1))
        for path in revisions_root.iterdir()
        if path.is_dir() and (match := REVISION_PATTERN.fullmatch(path.name)) is not None
    ]
    name = f"revision-{max(numbers, default=0) + 1:03d}"
    final = revisions_root / name
    partial = revisions_root / f".{name}.partial"
    if final.exists() or partial.exists():
        raise FileExistsError(final if final.exists() else partial)
    partial.mkdir()
    completed = False
    try:
        _check_cancel(cancel_cb)
        raw_recipe = load_raw_development_recipe(project.root / "raw_development.json")
        _write_json(partial / "edit_recipe.json", asdict(recipe))
        _progress(progress_cb, "editor-writer", "Preparing revision writer", 0, 1, "writers")
        helper = build_openexr_helper(source_path=openexr_source, output_path=partial / "_write_openexr")
        _progress(progress_cb, "editor-writer", "Preparing revision writer", 1, 1, "writers")
        tile_dir = partial / "tiles"
        tile_dir.mkdir()
        revision_tiles: list[dict[str, object]] = []
        entries: list[Entry] = []
        _progress(progress_cb, "editor-tiles", "Developing and editing original NEFs", 0, len(project.tiles), "NEFs")
        for index, tile in enumerate(project.tiles, start=1):
            _check_cancel(cancel_cb)
            stem = tile.raw_path.stem
            scene_path = tile_dir / f"{stem}_scene_linear.tif"
            display_path = tile_dir / f"{stem}.tif"
            scene = develop_nef_scene(tile.raw_path, raw_recipe, output_size=project.source_size)
            _edit_scene_array(scene, scene_path, recipe)
            _write_display_tile(scene_path, display_path)
            revision_tiles.append(
                {
                    "row": tile.row,
                    "col": tile.col,
                    "source_raw_file": os.path.relpath(tile.raw_path, partial),
                    "scene_linear_file": os.path.relpath(scene_path, partial),
                    "display_file": os.path.relpath(display_path, partial),
                }
            )
            entries.append(Entry(tile.row, tile.col, str(tile.alignment_path), str(scene_path)))
            _progress(progress_cb, "editor-tiles", "Developing and editing original NEFs", index, len(project.tiles), "NEFs")
        _write_json(partial / "tiles.json", revision_tiles)
        _check_cancel(cancel_cb)
        mosaic, mosaic_memmap_path, weights_memmap_path = composite_tiles(
            cv2=cv2,
            np=np,
            entries=entries,
            pos_by_rc_f={key: (float(value[0]), float(value[1])) for key, value in project.positions.items()},
            min_x=0.0,
            min_y=0.0,
            out_w=project.canvas_size[0],
            out_h=project.canvas_size[1],
            w_final=project.tile_size[0],
            h_final=project.tile_size[1],
            source_w=project.source_size[0],
            source_h=project.source_size[1],
            composite_dtype="float32",
            out_dir=str(partial),
            blend_mode=_blend_mode(project.stage),
            feather_px=_feather_pixels(project.stage),
            inmem_max_bytes=int(1.5 * 1024**3),
            use_memmap=True,
            black_transparent=False,
            black_threshold=0,
            refined_gains=None,
            progress_cb=_prefixed_progress(progress_cb),
            cancel_cb=cancel_cb,
        )
        if hasattr(mosaic, "flush"):
            mosaic.flush()
        backing_path = Path(mosaic_memmap_path) if mosaic_memmap_path is not None else partial / "_mosaic_scene_linear.f32"
        temporary_backing = not isinstance(mosaic, np.memmap)
        if temporary_backing:
            np.ascontiguousarray(mosaic).tofile(backing_path)
        write_scene_linear_exr(
            helper_path=helper,
            backing_path=backing_path,
            output_path=partial / EDITOR_RESULT_FILES["working_linear_exr"],
            shape=(project.canvas_size[1], project.canvas_size[0], 3),
            dtype=mosaic.dtype,
            compression="zip",
            tile_size=256,
            working_space="linear-rec2020",
            color_encoding="working-linear",
            input_order="bgr",
            progress_cb=_prefixed_progress(progress_cb),
            cancel_cb=cancel_cb,
        )
        px_per_mm = _pixels_per_mm(project.stitch_metadata)
        write_scene_linear_mosaic_tiff(
            pano=mosaic,
            memmap_path=mosaic_memmap_path,
            out_w=project.canvas_size[0],
            out_h=project.canvas_size[1],
            mosaic_path=str(partial / EDITOR_RESULT_FILES["full_tiff"]),
            tiff_compression="deflate",
            px_per_mm_target=px_per_mm,
            tiff_tile=None,
            tiff_tile_width=256,
            tiff_tile_height=256,
            tiff_predictor="horizontal",
            progress_cb=_prefixed_progress(progress_cb),
            cancel_cb=cancel_cb,
        )
        _check_cancel(cancel_cb)
        write_scene_linear_mosaic_tiff(
            pano=mosaic,
            memmap_path=mosaic_memmap_path,
            out_w=project.canvas_size[0],
            out_h=project.canvas_size[1],
            mosaic_path=str(partial / EDITOR_RESULT_FILES["pyramidal_tiff"]),
            tiff_compression="deflate",
            px_per_mm_target=px_per_mm,
            tiff_tile=True,
            tiff_tile_width=256,
            tiff_tile_height=256,
            tiff_predictor="horizontal",
            pyramidal=True,
            progress_cb=_prefixed_progress(progress_cb),
            cancel_cb=cancel_cb,
        )
        _check_cancel(cancel_cb)
        write_preview_jpeg(
            mosaic_path=str(partial / EDITOR_RESULT_FILES["full_tiff"]),
            out_dir=str(partial),
            max_dim=2000,
            quality=88,
            progress_cb=_prefixed_progress(progress_cb),
            cancel_cb=cancel_cb,
        )
        metadata = {
            "version": 1,
            "revision": name,
            "source_project": str(project.root),
            "source_raw_development_recipe": "../../raw_development.json",
            "source_stitch_metadata": "../../stitch_meta.json",
            "alignment": "saved-transforms",
            "working_image_role": "edited-linear-rec2020",
            "canonical_editable_source": "original-nefs-plus-recipes-and-transforms",
            "tile_transforms": project.stage["tile_transforms"],
            "outputs": {key: value for key, value in EDITOR_RESULT_FILES.items() if key not in {"edit_recipe", "revision_metadata"}},
        }
        _progress(progress_cb, "editor-metadata", "Writing revision metadata", 0, 1, "files")
        _write_json(partial / "revision_meta.json", metadata)
        _progress(progress_cb, "editor-metadata", "Writing revision metadata", 1, 1, "files")
        del mosaic
        gc.collect()
        scratch = [partial / "_write_openexr"]
        if mosaic_memmap_path is not None:
            scratch.append(Path(mosaic_memmap_path))
        if weights_memmap_path is not None:
            scratch.append(Path(weights_memmap_path))
        if temporary_backing:
            scratch.append(backing_path)
        existing = [path for path in scratch if path.exists()]
        _progress(progress_cb, "editor-cleanup", "Cleaning revision scratch files", 0, len(existing), "files")
        for index, path in enumerate(existing, start=1):
            _check_cancel(cancel_cb)
            path.unlink()
            _progress(progress_cb, "editor-cleanup", "Cleaning revision scratch files", index, len(existing), "files")
        _check_cancel(cancel_cb)
        partial.rename(final)
        completed = True
    finally:
        if not completed and partial.exists():
            shutil.rmtree(partial)
    return final


def _mosaic_preview(project: EditorProject, recipe: EditRecipe, max_dim: int) -> np.ndarray:
    scale = min(1.0, max_dim / max(project.canvas_size))
    out_w = max(1, int(math.ceil(project.canvas_size[0] * scale)))
    out_h = max(1, int(math.ceil(project.canvas_size[1] * scale)))
    tile_w = max(1, int(round(project.tile_size[0] * scale)))
    tile_h = max(1, int(round(project.tile_size[1] * scale)))
    blend = _blend_mode(project.stage)
    accumulator = np.zeros((out_h, out_w, 3), dtype=np.float32)
    weights = np.zeros((out_h, out_w), dtype=np.float32) if blend != "overwrite" else None
    feather_cache: dict[tuple[int, int, int], np.ndarray] = {}
    scaled_feather = max(0, int(round(_feather_pixels(project.stage) * scale)))
    for tile in project.tiles:
        scene = _scaled_scene_exact(tile.scene_linear_path, project.source_size, tile_w, tile_h)
        scene = apply_edit_recipe(scene, recipe)
        x = int(round(project.positions[(tile.row, tile.col)][0] * scale))
        y = int(round(project.positions[(tile.row, tile.col)][1] * scale))
        x1 = min(out_w, x + tile_w)
        y1 = min(out_h, y + tile_h)
        if x1 <= x or y1 <= y:
            raise RuntimeError("Saved tile transform lies outside the preview canvas")
        image = scene[: y1 - y, : x1 - x]
        if blend == "overwrite":
            accumulator[y:y1, x:x1] = image
            continue
        if blend == "feather":
            weight = feather_weight(
                np=np,
                w=tile_w,
                h=tile_h,
                feather_px=scaled_feather,
                cache=feather_cache,
            )[: y1 - y, : x1 - x].astype(np.float32)
        else:
            weight = np.ones((y1 - y, x1 - x), dtype=np.float32)
        accumulator[y:y1, x:x1] += image * weight[:, :, None]
        weights[y:y1, x:x1] += weight
    if weights is not None:
        covered = weights > 0
        accumulator[covered] /= weights[covered, None]
    return accumulator


def _scaled_scene(path: Path, expected_size: tuple[int, int], max_dim: int) -> np.ndarray:
    scale = min(1.0, max_dim / max(expected_size))
    return _scaled_scene_exact(
        path,
        expected_size,
        max(1, int(round(expected_size[0] * scale))),
        max(1, int(round(expected_size[1] * scale))),
    )


def _scaled_scene_exact(path: Path, expected_size: tuple[int, int], width: int, height: int) -> np.ndarray:
    image = pyvips.Image.new_from_file(str(path), access="sequential")
    if (image.width, image.height, image.bands, image.format) != (*expected_size, 3, "float"):
        raise ValueError(f"Scene-linear tile does not match project dimensions: {path.name}")
    resized = image.resize(width / image.width, vscale=height / image.height, kernel="lanczos3")
    if resized.width != width or resized.height != height:
        raise RuntimeError("Scene-linear preview resize produced unexpected dimensions")
    return np.frombuffer(resized.write_to_memory(), dtype=np.float32).reshape(height, width, 3).copy()


def _edit_scene_array(scene: np.ndarray, destination: Path, recipe: EditRecipe) -> None:
    if scene.ndim != 3 or scene.shape[2] != 3 or scene.dtype != np.float32 or not np.isfinite(scene).all():
        raise ValueError("Developed scene must be finite float32 RGB")
    for start in range(0, scene.shape[0], 128):
        rows = slice(start, min(start + 128, scene.shape[0]))
        scene[rows] = apply_edit_recipe(scene[rows], recipe)
    image = pyvips.Image.new_from_memory(scene.data, scene.shape[1], scene.shape[0], 3, "float")
    image.tiffsave(str(destination), compression="deflate", predictor="float", tile=True)


def _write_display_tile(scene_path: Path, display_path: Path) -> None:
    image = pyvips.Image.new_from_file(str(scene_path), access="sequential")
    linear_srgb = image.recomb(pyvips.Image.new_from_array(REC2020_TO_SRGB))
    clipped = (linear_srgb < 0.0).ifthenelse(0.0, linear_srgb)
    clipped = (clipped > 1.0).ifthenelse(1.0, clipped)
    encoded = (clipped <= 0.0031308).ifthenelse(
        clipped * 12.92,
        clipped ** (1.0 / 2.4) * 1.055 - 0.055,
    )
    display = (encoded * 65535.0 + 0.5).cast("ushort")
    display.tiffsave(
        str(display_path),
        compression="deflate",
        predictor="horizontal",
        tile=True,
        profile=str(require_srgb_icc_profile()),
    )


def _scene_to_srgb8(scene: np.ndarray) -> np.ndarray:
    linear = scene @ REC2020_TO_SRGB.T
    np.clip(linear, 0.0, 1.0, out=linear)
    encoded = np.where(
        linear <= np.float32(0.0031308),
        linear * np.float32(12.92),
        np.float32(1.055) * np.power(linear, np.float32(1.0 / 2.4)) - np.float32(0.055),
    )
    return np.rint(encoded * np.float32(255.0)).astype(np.uint8)


def _blend_mode(stage: dict[str, object]) -> str:
    value = stage.get("blend")
    if value not in {"overwrite", "average", "feather"}:
        raise ValueError("Saved stitch blend mode is invalid")
    return str(value)


def _feather_pixels(stage: dict[str, object]) -> int:
    if _blend_mode(stage) != "feather":
        return 0
    if stage.get("tiles_in") == 1:
        return 0
    value = stage.get("layout_feather_px")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("Saved stitch feather width is invalid")
    return value


def _pixels_per_mm(stitch_metadata: dict[str, object]) -> float | None:
    dpi = stitch_metadata.get("dpi")
    if dpi is None:
        return None
    if not isinstance(dpi, dict):
        raise ValueError("Saved stitch DPI metadata is invalid")
    value = dpi.get("px_per_mm")
    if value is None:
        dpi_x = dpi.get("dpi_x")
        if isinstance(dpi_x, bool) or not isinstance(dpi_x, (int, float)):
            raise ValueError("Saved stitch DPI metadata is invalid")
        value = float(dpi_x) / 25.4
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError("Saved stitch DPI metadata is invalid")
    return float(value)


def _prefixed_progress(progress_cb: ProgressCallback | None) -> ProgressCallback | None:
    if progress_cb is None:
        return None

    def publish(phase: str, label: str, completed: int, total: int | None, unit: str) -> None:
        progress_cb(f"editor-{phase}", label, completed, total, unit)

    return publish


def _progress(
    progress_cb: ProgressCallback | None,
    phase: str,
    label: str,
    completed: int,
    total: int | None,
    unit: str,
) -> None:
    if progress_cb is not None:
        progress_cb(phase, label, completed, total, unit)


def _check_cancel(cancel_cb: Callable[[], None] | None) -> None:
    if cancel_cb is not None:
        cancel_cb()


def _json_object(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain an object")
    return payload


def _json_array(path: Path) -> list[object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not payload:
        raise ValueError(f"{path.name} must contain a non-empty array")
    return payload


def _write_json(path: Path, payload: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _size(value: object, label: str) -> tuple[int, int]:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in value)
    ):
        raise ValueError(f"Saved {label} size must contain two positive integers")
    return value[0], value[1]


def _nonnegative_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _project_file(root: Path, value: object, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} path is required")
    path = (root / value).resolve()
    if root not in path.parents:
        raise ValueError(f"{label} path must stay inside the scan project")
    return path
