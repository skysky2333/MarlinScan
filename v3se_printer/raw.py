from __future__ import annotations

from dataclasses import dataclass, fields
import json
import math
from pathlib import Path
from typing import Callable

import cv2  # type: ignore
import numpy as np  # type: ignore
import pyvips  # type: ignore
import rawpy  # type: ignore

from .calibration import CalibrationError, NormalizedROI
from .color import (
    REC2020_TO_SRGB_MATRIX,
    SRGB_TO_REC2020_MATRIX,
    SRGB_TO_XYZ_MATRIX,
    require_srgb_icc_profile,
)


REC2020_TO_SRGB = np.asarray(REC2020_TO_SRGB_MATRIX, dtype=np.float32)
SRGB_TO_REC2020 = np.asarray(SRGB_TO_REC2020_MATRIX, dtype=np.float64)
SRGB_TO_XYZ = np.asarray(SRGB_TO_XYZ_MATRIX, dtype=np.float64)
SRGB_LUMINANCE = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
RAWPY_VERSION = rawpy.__version__
LIBRAW_VERSION = ".".join(str(value) for value in rawpy.libraw_version)
RAW_SATURATION_LEVEL = 0.995
RAW_MEANINGFUL_SATURATION = 0.01


@dataclass(frozen=True)
class WhiteBalance:
    red: float
    green: float
    blue: float
    green_2: float

    def __post_init__(self) -> None:
        if not all(
            not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value > 0
            for value in self.gains
        ):
            raise ValueError("White balance gains must be positive and finite")

    @property
    def gains(self) -> tuple[float, float, float, float]:
        return self.red, self.green, self.blue, self.green_2


@dataclass(frozen=True)
class RawHeadroomReading:
    percentile_99_levels: tuple[float, float, float, float]
    saturated_fractions: tuple[float, float, float, float]

    def __post_init__(self) -> None:
        for label, values in (
            ("percentile levels", self.percentile_99_levels),
            ("saturated fractions", self.saturated_fractions),
        ):
            if len(values) != 4 or not all(
                not isinstance(value, bool)
                and isinstance(value, (int, float))
                and math.isfinite(value)
                and 0.0 <= value <= 1.0
                for value in values
            ):
                raise ValueError(f"RAW {label} must contain four finite values from zero through one")

    @property
    def highlight_level(self) -> float:
        return max(self.percentile_99_levels)

    @property
    def meaningful_saturation(self) -> bool:
        return any(value >= RAW_MEANINGFUL_SATURATION for value in self.saturated_fractions)


@dataclass(frozen=True)
class RawDevelopmentRecipe:
    version: int
    white_balance: WhiteBalance
    display_linear_gain: float
    reference_jpeg_linear_luminance: float
    reference_raw_linear_luminance: float
    reference_roi: NormalizedROI
    reference_jpeg: str
    reference_nef: str
    camera_to_working_matrix: tuple[tuple[float, float, float], ...]
    rawpy_version: str = RAWPY_VERSION
    libraw_version: str = LIBRAW_VERSION
    demosaic_algorithm: str = "AHD"
    highlight_mode: str = "Ignore"
    working_space: str = "linear-rec2020-d65"
    display_space: str = "srgb"

    def __post_init__(self) -> None:
        values = (
            self.display_linear_gain,
            self.reference_jpeg_linear_luminance,
            self.reference_raw_linear_luminance,
        )
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version != 2:
            raise ValueError("RAW development recipe version must be 2")
        if not all(
            not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value > 0
            for value in values
        ):
            raise ValueError("RAW development recipe luminance values and gain must be positive and finite")
        if self.demosaic_algorithm != "AHD":
            raise ValueError("RAW development recipe demosaic algorithm must be AHD")
        if self.highlight_mode != "Ignore":
            raise ValueError("RAW development recipe highlight mode must be Ignore")
        if self.working_space != "linear-rec2020-d65" or self.display_space != "srgb":
            raise ValueError("RAW development recipe color spaces are invalid")
        if not self.reference_jpeg or not self.reference_nef:
            raise ValueError("RAW development recipe reference paths are required")
        if self.rawpy_version != RAWPY_VERSION or self.libraw_version != LIBRAW_VERSION:
            raise RuntimeError(
                f"RAW development recipe requires rawpy {self.rawpy_version} with LibRaw {self.libraw_version}; "
                f"installed versions are rawpy {RAWPY_VERSION} with LibRaw {LIBRAW_VERSION}"
            )
        matrix = np.asarray(self.camera_to_working_matrix, dtype=np.float64)
        if matrix.shape != (3, 3) or not np.all(np.isfinite(matrix)) or abs(float(np.linalg.det(matrix))) < 1e-9:
            raise ValueError("RAW development recipe camera matrix must be finite and invertible")


@dataclass(frozen=True)
class DevelopedRaw:
    display_path: Path
    scene_linear_path: Path


def load_raw_development_recipe(path: str | Path) -> RawDevelopmentRecipe:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("RAW development recipe must be a JSON object")
    expected = {field.name for field in fields(RawDevelopmentRecipe)}
    if set(payload) != expected:
        raise ValueError("RAW development recipe fields do not match version 2")
    white_balance = payload["white_balance"]
    reference_roi = payload["reference_roi"]
    camera_matrix = payload["camera_to_working_matrix"]
    if (
        not isinstance(white_balance, dict)
        or not isinstance(reference_roi, dict)
        or not isinstance(camera_matrix, list)
        or not all(isinstance(row, list) for row in camera_matrix)
    ):
        raise ValueError("RAW development recipe calibration fields are invalid")
    values = dict(payload)
    values["white_balance"] = WhiteBalance(**white_balance)
    values["reference_roi"] = NormalizedROI(**reference_roi)
    values["camera_to_working_matrix"] = tuple(tuple(row) for row in camera_matrix)
    return RawDevelopmentRecipe(**values)


def camera_to_rec2020_matrix(rgb_xyz_matrix: np.ndarray) -> np.ndarray:
    camera_xyz = np.asarray(rgb_xyz_matrix, dtype=np.float64)
    if camera_xyz.shape != (4, 3) or not np.all(np.isfinite(camera_xyz)):
        raise ValueError("RAW camera-to-XYZ matrix must be finite with shape 4x3")
    if np.any(camera_xyz[3] != 0):
        raise ValueError("Only three-color RAW cameras are supported")
    camera_rgb = camera_xyz[:3] @ SRGB_TO_XYZ
    row_sums = camera_rgb.sum(axis=1)
    if np.any(row_sums <= 1e-5):
        raise ValueError("RAW camera color matrix cannot be normalized")
    camera_rgb /= row_sums[:, None]
    rgb_from_camera = np.linalg.inv(camera_rgb).astype(np.float32)
    return (SRGB_TO_REC2020 @ rgb_from_camera.astype(np.float64)).astype(np.float32)


def calculate_white_balance(
    *,
    raw_image: np.ndarray,
    raw_pattern: np.ndarray,
    color_desc: bytes,
    black_levels: list[int],
    white_levels: list[int],
    roi: NormalizedROI,
    reference_size: tuple[int, int] | None = None,
) -> WhiteBalance:
    if raw_image.ndim != 2 or raw_pattern.shape != (2, 2):
        raise CalibrationError("Unsupported RAW sensor layout")
    if len(black_levels) != 4 or len(white_levels) != 4 or not all(
        white > black
        for black, white in zip(black_levels, white_levels, strict=True)
    ):
        raise CalibrationError("RAW per-channel black and camera white levels are invalid")
    reference = raw_image if reference_size is None else _center_crop(raw_image, reference_size)
    x0, y0, x1, y1 = roi.pixels(reference)
    if reference_size is not None:
        x0 += (raw_image.shape[1] - reference.shape[1]) // 2
        x1 += (raw_image.shape[1] - reference.shape[1]) // 2
        y0 += (raw_image.shape[0] - reference.shape[0]) // 2
        y1 += (raw_image.shape[0] - reference.shape[0]) // 2
    patch = raw_image[y0:y1, x0:x1].astype(np.float64)
    rows, cols = np.indices(patch.shape)
    sensor_rows = rows + y0
    sensor_cols = cols + x0
    indices = raw_pattern[sensor_rows % 2, sensor_cols % 2]
    desc = color_desc.decode("ascii").rstrip("\x00")
    means: dict[int, float] = {}
    for index in sorted(int(value) for value in np.unique(raw_pattern)):
        black = float(black_levels[index])
        values = patch[indices == index] - black
        ceiling = 0.98 * (float(white_levels[index]) - black)
        values = values[(values > max(16.0, ceiling * 0.01)) & (values < ceiling)]
        if values.size < 64:
            raise CalibrationError("Gray reference is too dark, clipped, or small")
        low, high = np.percentile(values, [10.0, 90.0])
        trimmed = values[(values >= low) & (values <= high)]
        means[index] = float(trimmed.mean())
    green_indices = [index for index in means if index < len(desc) and desc[index] == "G"]
    red_indices = [index for index in means if index < len(desc) and desc[index] == "R"]
    blue_indices = [index for index in means if index < len(desc) and desc[index] == "B"]
    if len(green_indices) != 2 or len(red_indices) != 1 or len(blue_indices) != 1:
        raise CalibrationError("Expected an RGBG Bayer sensor")
    green_mean = float(np.mean([means[index] for index in green_indices]))
    gains = {index: green_mean / mean for index, mean in means.items()}
    return WhiteBalance(
        red=gains[red_indices[0]],
        green=gains[green_indices[0]],
        blue=gains[blue_indices[0]],
        green_2=gains[green_indices[1]],
    )


def calibrate_white_balance(
    nef_path: str | Path,
    roi: NormalizedROI,
    *,
    reference_size: tuple[int, int] | None = None,
    loader: Callable[[str], object] = rawpy.imread,
) -> WhiteBalance:
    with loader(str(nef_path)) as raw:
        if raw.camera_white_level_per_channel is None:
            raise CalibrationError("RAW per-channel camera white levels are required")
        return calculate_white_balance(
            raw_image=raw.raw_image_visible,
            raw_pattern=raw.raw_pattern,
            color_desc=raw.color_desc,
            black_levels=list(raw.black_level_per_channel),
            white_levels=list(raw.camera_white_level_per_channel),
            roi=roi,
            reference_size=reference_size,
        )


def analyze_raw_headroom(
    nef_path: str | Path,
    roi: NormalizedROI,
    *,
    reference_size: tuple[int, int],
    loader: Callable[[str], object] = rawpy.imread,
) -> RawHeadroomReading:
    with loader(str(nef_path)) as raw:
        raw_image = np.asarray(raw.raw_image_visible)
        raw_pattern = np.asarray(raw.raw_pattern)
        black_levels = tuple(float(value) for value in raw.black_level_per_channel)
        if raw.camera_white_level_per_channel is None:
            raise CalibrationError("RAW per-channel camera white levels are required")
        white_levels = tuple(float(value) for value in raw.camera_white_level_per_channel)

        if raw_image.ndim != 2 or raw_image.dtype.kind != "u":
            raise CalibrationError("RAW headroom analysis requires an unsigned two-dimensional sensor image")
        if raw_pattern.shape != (2, 2) or raw_pattern.dtype.kind not in "iu":
            raise CalibrationError("RAW headroom analysis requires a 2x2 Bayer pattern")
        if set(int(value) for value in raw_pattern.flat) != {0, 1, 2, 3}:
            raise CalibrationError("RAW headroom analysis requires four CFA channel indices")
        if len(black_levels) != 4 or len(white_levels) != 4 or not all(
            math.isfinite(black) and math.isfinite(white) and white > black
            for black, white in zip(black_levels, white_levels, strict=True)
        ):
            raise CalibrationError("RAW per-channel black and camera white levels are invalid")

        reference = _center_crop(raw_image, reference_size)
        x0, y0, x1, y1 = roi.pixels(reference)
        offset_x = (raw_image.shape[1] - reference.shape[1]) // 2
        offset_y = (raw_image.shape[0] - reference.shape[0]) // 2
        x0 += offset_x
        x1 += offset_x
        y0 += offset_y
        y1 += offset_y
        patch = raw_image[y0:y1, x0:x1]
        percentile_levels: list[float] = []
        fractions: list[float] = []
        for index in range(4):
            pattern_row, pattern_col = np.argwhere(raw_pattern == index)[0]
            row_offset = (int(pattern_row) - y0) % 2
            col_offset = (int(pattern_col) - x0) % 2
            values = patch[row_offset::2, col_offset::2]
            if values.size == 0:
                raise CalibrationError("RAW exposure ROI must sample every CFA channel")
            black = black_levels[index]
            normalized = np.clip(
                (values.astype(np.float64) - black) / (white_levels[index] - black),
                0.0,
                1.0,
            )
            percentile_levels.append(float(np.percentile(normalized, 99.0)))
            threshold = black + RAW_SATURATION_LEVEL * (white_levels[index] - black)
            fractions.append(float(np.count_nonzero(values >= threshold)) / float(values.size))
        return RawHeadroomReading(
            (percentile_levels[0], percentile_levels[1], percentile_levels[2], percentile_levels[3]),
            (fractions[0], fractions[1], fractions[2], fractions[3]),
        )


def calibrate_development_recipe(
    nef_path: str | Path,
    jpeg_path: str | Path,
    white_balance: WhiteBalance,
    roi: NormalizedROI,
    *,
    reference_size: tuple[int, int],
    loader: Callable[[str], object] = rawpy.imread,
) -> RawDevelopmentRecipe:
    jpeg_bgr = cv2.imread(str(jpeg_path), cv2.IMREAD_COLOR)
    if jpeg_bgr is None:
        raise CalibrationError(f"Failed to read calibration JPEG: {jpeg_path}")
    if (jpeg_bgr.shape[1], jpeg_bgr.shape[0]) != reference_size:
        raise CalibrationError("Calibration JPEG dimensions changed during RAW calibration")
    with loader(str(nef_path)) as raw:
        camera_matrix = camera_to_rec2020_matrix(raw.rgb_xyz_matrix)
        scene = _linear_rec2020(raw, white_balance, camera_matrix)
    scene = _center_crop(scene, reference_size)
    raw_linear_srgb = _linear_rec2020_to_linear_srgb(scene)
    jpeg_rgb = jpeg_bgr[:, :, ::-1].astype(np.float32) / np.float32(255.0)
    jpeg_linear_srgb = _srgb_decode(jpeg_rgb)
    raw_luminance = roi.crop(raw_linear_srgb) @ SRGB_LUMINANCE
    jpeg_luminance = roi.crop(jpeg_linear_srgb) @ SRGB_LUMINANCE
    raw_reference = float(np.median(raw_luminance))
    jpeg_reference = float(np.median(jpeg_luminance))
    if not math.isfinite(raw_reference) or raw_reference <= 0:
        raise CalibrationError("RAW gray reference has no usable linear luminance")
    if not math.isfinite(jpeg_reference) or jpeg_reference <= 0:
        raise CalibrationError("JPEG gray reference has no usable linear luminance")
    return RawDevelopmentRecipe(
        version=2,
        white_balance=white_balance,
        display_linear_gain=jpeg_reference / raw_reference,
        reference_jpeg_linear_luminance=jpeg_reference,
        reference_raw_linear_luminance=raw_reference,
        reference_roi=roi,
        reference_jpeg=str(Path(jpeg_path).expanduser().resolve()),
        reference_nef=str(Path(nef_path).expanduser().resolve()),
        camera_to_working_matrix=tuple(tuple(float(value) for value in row) for row in camera_matrix),
    )


def develop_nef(
    nef_path: str | Path,
    display_output_path: str | Path,
    scene_linear_output_path: str | Path,
    recipe: RawDevelopmentRecipe,
    *,
    output_size: tuple[int, int] | None = None,
    loader: Callable[[str], object] = rawpy.imread,
) -> DevelopedRaw:
    display_output = Path(display_output_path)
    scene_output = Path(scene_linear_output_path)
    if display_output.suffix.lower() not in {".tif", ".tiff"}:
        raise ValueError("Developed display output must be TIFF")
    if scene_output.suffix.lower() not in {".tif", ".tiff"}:
        raise ValueError("Scene-linear working output must be TIFF")
    if display_output == scene_output:
        raise ValueError("Display and scene-linear outputs must be different files")
    scene = develop_nef_scene(
        nef_path,
        recipe,
        output_size=output_size,
        loader=loader,
    )
    display = _scene_to_srgb16(scene)
    _write_rgb_tiff(scene_output, scene, "float", predictor="float")
    _write_rgb_tiff(
        display_output,
        display,
        "ushort",
        predictor="horizontal",
        profile=require_srgb_icc_profile(),
    )
    return DevelopedRaw(display_output, scene_output)


def develop_nef_scene(
    nef_path: str | Path,
    recipe: RawDevelopmentRecipe,
    *,
    output_size: tuple[int, int] | None = None,
    loader: Callable[[str], object] = rawpy.imread,
) -> np.ndarray:
    recipe_matrix = np.asarray(recipe.camera_to_working_matrix, dtype=np.float32)
    with loader(str(nef_path)) as raw:
        camera_matrix = camera_to_rec2020_matrix(raw.rgb_xyz_matrix)
        if not np.allclose(camera_matrix, recipe_matrix, rtol=0.0, atol=1e-6):
            raise RuntimeError("RAW tile camera matrix does not match the calibration recipe")
        scene = _linear_rec2020(raw, recipe.white_balance, recipe_matrix)
    if output_size is not None:
        scene = _center_crop(scene, output_size)
    scene *= np.float32(recipe.display_linear_gain)
    return scene if scene.flags.c_contiguous else np.ascontiguousarray(scene)


def _linear_rec2020(raw: object, white_balance: WhiteBalance, camera_matrix: np.ndarray) -> np.ndarray:
    camera_rgb = raw.postprocess(
        demosaic_algorithm=rawpy.DemosaicAlgorithm.AHD,
        use_camera_wb=False,
        user_wb=list(white_balance.gains),
        output_color=rawpy.ColorSpace.raw,
        output_bps=16,
        no_auto_bright=True,
        adjust_maximum_thr=0.0,
        gamma=(1.0, 1.0),
        highlight_mode=rawpy.HighlightMode.Ignore,
    )
    if camera_rgb.dtype != np.uint16 or camera_rgb.ndim != 3 or camera_rgb.shape[2] != 3:
        raise RuntimeError("RAW developer did not return 16-bit linear camera RGB")
    scene = np.empty(camera_rgb.shape, dtype=np.float32)
    np.matmul(camera_rgb, (camera_matrix / np.float32(65535.0)).T, out=scene)
    return scene


def _linear_rec2020_to_linear_srgb(rgb: np.ndarray) -> np.ndarray:
    return rgb @ REC2020_TO_SRGB.T


def _scene_to_srgb16(scene: np.ndarray) -> np.ndarray:
    display = np.empty(scene.shape, dtype=np.uint16)
    for start in range(0, scene.shape[0], 256):
        rows = slice(start, start + 256)
        linear = _linear_rec2020_to_linear_srgb(scene[rows])
        np.clip(linear, 0.0, 1.0, out=linear)
        encoded = _srgb_encode(linear)
        np.multiply(encoded, np.float32(65535.0), out=encoded)
        np.rint(encoded, out=encoded)
        display[rows] = encoded
    return display


def _srgb_decode(rgb: np.ndarray) -> np.ndarray:
    return np.where(
        rgb <= np.float32(0.04045),
        rgb / np.float32(12.92),
        np.power((rgb + np.float32(0.055)) / np.float32(1.055), np.float32(2.4)),
    )


def _srgb_encode(rgb: np.ndarray) -> np.ndarray:
    return np.where(
        rgb <= np.float32(0.0031308),
        rgb * np.float32(12.92),
        np.float32(1.055) * np.power(rgb, np.float32(1.0 / 2.4)) - np.float32(0.055),
    )


def _write_rgb_tiff(
    path: Path,
    rgb: np.ndarray,
    pixel_format: str,
    *,
    predictor: str,
    profile: Path | None = None,
) -> None:
    height, width = rgb.shape[:2]
    image = pyvips.Image.new_from_memory(rgb.data, width, height, 3, pixel_format)
    options: dict[str, object] = {
        "compression": "deflate",
        "predictor": predictor,
    }
    if profile is not None:
        options["profile"] = str(profile)
    image.tiffsave(str(path), **options)


def _center_crop(image: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    width, height = size
    if isinstance(width, bool) or isinstance(height, bool) or not isinstance(width, int) or not isinstance(height, int):
        raise ValueError("Crop dimensions must be integers")
    source_height, source_width = image.shape[:2]
    delta_x = source_width - width
    delta_y = source_height - height
    if width <= 0 or height <= 0 or delta_x < 0 or delta_y < 0:
        raise ValueError("Crop dimensions must fit inside the RAW image")
    if delta_x % 2 or delta_y % 2:
        raise ValueError("JPEG and RAW dimensions do not define a symmetric center crop")
    left = delta_x // 2
    top = delta_y // 2
    return image[top : top + height, left : left + width]
