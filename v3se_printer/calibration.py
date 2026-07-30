from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import math
from pathlib import Path
import re
from typing import Callable, Sequence

import cv2  # type: ignore
import numpy as np  # type: ignore

from .uvc import compute_sharpness


class CalibrationError(RuntimeError):
    pass


EXPOSURE_TARGET_LUMINANCE = 128.0
EXPOSURE_TOLERANCE_EV = 1.0 / 3.0
EXPOSURE_TRIM_LOW = 2.0
EXPOSURE_TRIM_HIGH = 98.0
RAW_HIGHLIGHT_TARGET = 0.85
FOCUS_MIN_PROMINENCE = 0.10


@dataclass(frozen=True)
class NormalizedROI:
    x: float = 0.2
    y: float = 0.2
    width: float = 0.6
    height: float = 0.6

    def __post_init__(self) -> None:
        values = (self.x, self.y, self.width, self.height)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("ROI values must be finite")
        if self.x < 0 or self.y < 0 or self.width <= 0 or self.height <= 0:
            raise ValueError("ROI must have positive dimensions inside the image")
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError("ROI must fit inside the image")

    def pixels(self, image: np.ndarray) -> tuple[int, int, int, int]:
        height, width = image.shape[:2]
        x0 = min(width - 1, int(math.floor(self.x * width)))
        y0 = min(height - 1, int(math.floor(self.y * height)))
        x1 = max(x0 + 1, min(width, int(math.ceil((self.x + self.width) * width))))
        y1 = max(y0 + 1, min(height, int(math.ceil((self.y + self.height) * height))))
        return x0, y0, x1, y1

    def crop(self, image: np.ndarray) -> np.ndarray:
        x0, y0, x1, y1 = self.pixels(image)
        return image[y0:y1, x0:x1]


@dataclass(frozen=True)
class ExposureReading:
    metered_luminance: float
    percentile_99: float
    clipped_fraction: float
    raw_saturated_fraction: float | None = None
    raw_highlight_level: float | None = None
    warning: str | None = None

    @property
    def accepted(self) -> bool:
        if self.raw_highlight_level is not None:
            return True
        return abs(exposure_error_ev(self)) <= EXPOSURE_TOLERANCE_EV


@dataclass(frozen=True)
class FocusSample:
    z: float
    score: float


@dataclass(frozen=True)
class FocusMesh:
    x_min: float
    x_max: float
    y_min: float
    y_max: float
    z00: float
    z10: float
    z01: float
    z11: float

    @property
    def node_xs(self) -> tuple[float, float]:
        span = self.x_max - self.x_min
        return self.x_min + span * 0.25, self.x_min + span * 0.75

    @property
    def node_ys(self) -> tuple[float, float]:
        span = self.y_max - self.y_min
        return self.y_min + span * 0.25, self.y_min + span * 0.75

    def z_at(self, x: float, y: float) -> float:
        if not self.x_min <= x <= self.x_max or not self.y_min <= y <= self.y_max:
            raise ValueError("Position lies outside the calibrated focus-grid coverage")
        x0, x1 = self.node_xs
        y0, y1 = self.node_ys
        tx = (x - x0) / (x1 - x0)
        ty = (y - y0) / (y1 - y0)
        return (
            (1.0 - tx) * (1.0 - ty) * self.z00
            + tx * (1.0 - ty) * self.z10
            + (1.0 - tx) * ty * self.z01
            + tx * ty * self.z11
        )


def fit_focus_mesh(
    x_min: float,
    x_max: float,
    y_min: float,
    y_max: float,
    measurements: Sequence[tuple[float, float, float]],
) -> FocusMesh:
    bounds = (x_min, x_max, y_min, y_max)
    if not all(math.isfinite(value) for value in bounds):
        raise ValueError("Focus-grid coverage must be finite")
    if x_min >= x_max or y_min >= y_max:
        raise ValueError("Focus-grid coverage must have positive dimensions")
    if len(measurements) != 5:
        raise ValueError("A focus surface fit requires exactly five measurements")

    x0 = x_min + (x_max - x_min) * 0.25
    x1 = x_min + (x_max - x_min) * 0.75
    y0 = y_min + (y_max - y_min) * 0.25
    y1 = y_min + (y_max - y_min) * 0.75
    rows: list[list[float]] = []
    zs: list[float] = []
    for x, y, z in measurements:
        if not all(math.isfinite(value) for value in (x, y, z)):
            raise ValueError("Focus measurements must be finite")
        if not x_min <= x <= x_max or not y_min <= y <= y_max:
            raise ValueError("Focus measurement lies outside the coverage")
        tx = (x - x0) / (x1 - x0)
        ty = (y - y0) / (y1 - y0)
        rows.append([1.0, tx, ty, tx * ty])
        zs.append(z)

    coefficients, _, rank, _ = np.linalg.lstsq(
        np.asarray(rows, dtype=np.float64),
        np.asarray(zs, dtype=np.float64),
        rcond=None,
    )
    if rank != 4:
        raise ValueError("Focus measurements do not span a bilinear surface")
    a, b, c, d = (float(value) for value in coefficients)
    return FocusMesh(
        x_min,
        x_max,
        y_min,
        y_max,
        a,
        a + b,
        a + c,
        a + b + c + d,
    )


def read_jpeg(path: str | Path) -> np.ndarray:
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        raise CalibrationError(f"Failed to read JPEG: {path}")
    return image


def analyze_exposure(image: np.ndarray, roi: NormalizedROI) -> ExposureReading:
    crop = roi.crop(image)
    if crop.ndim == 3:
        luminance = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    else:
        luminance = crop
    low, high = np.percentile(luminance, [EXPOSURE_TRIM_LOW, EXPOSURE_TRIM_HIGH])
    return ExposureReading(
        metered_luminance=float(np.clip(luminance, low, high).mean()),
        percentile_99=float(np.percentile(luminance, 99.0)),
        clipped_fraction=float(np.count_nonzero(luminance >= 250)) / float(luminance.size),
    )


def exposure_error_ev(reading: ExposureReading) -> float:
    if reading.metered_luminance <= 0:
        return -math.inf
    return 2.2 * math.log2(reading.metered_luminance / EXPOSURE_TARGET_LUMINANCE)


def parse_shutter_seconds(label: str) -> float | None:
    value = label.strip().lower()
    if not value or value == "bulb":
        return None
    unknown = re.fullmatch(r"unknown value ([0-9a-f]+)", value)
    if unknown is not None:
        # Nikon 1 encodes missing table entries as signed low-byte sixth-stop values.
        code = int(unknown.group(1), 16) & 0xFF
        signed_code = code - 0x100 if code & 0x80 else code
        return 2.0 ** (-signed_code / 6.0)
    if value.startswith("unknown"):
        return None
    try:
        seconds = float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        return None
    return seconds if seconds > 0 else None


def shutter_choices(labels: Sequence[str], max_seconds: float = 1.0) -> list[tuple[str, float]]:
    choices = [(label, seconds) for label in labels if (seconds := parse_shutter_seconds(label)) is not None]
    choices = [(label, seconds) for label, seconds in choices if seconds <= max_seconds]
    if not choices:
        raise CalibrationError("Camera exposes no usable shutter speeds")
    return sorted(choices, key=lambda item: item[1])


def choose_shutter(choices: Sequence[tuple[str, float]], target_seconds: float) -> tuple[str, float]:
    if target_seconds >= choices[-1][1]:
        return choices[-1]
    return min(choices, key=lambda item: abs(math.log(item[1] / target_seconds)))


def next_shutter(
    reading: ExposureReading,
    current_seconds: float,
    choices: Sequence[tuple[str, float]],
) -> tuple[str, float] | None:
    error_ev = exposure_error_ev(reading)
    if abs(error_ev) <= EXPOSURE_TOLERANCE_EV:
        return None
    current_index = next(index for index, choice in enumerate(choices) if choice[1] == current_seconds)
    if reading.metered_luminance <= 0:
        target = choices[-1][1]
    else:
        target = current_seconds * math.pow(EXPOSURE_TARGET_LUMINANCE / reading.metered_luminance, 2.2)
    selected = choose_shutter(choices, target)
    if error_ev < 0.0:
        if current_index == len(choices) - 1:
            return None
        return selected if selected[1] > current_seconds else choices[current_index + 1]
    if current_index == 0:
        return None
    if selected[1] >= current_seconds:
        return choices[current_index - 1]
    return selected


def next_raw_shutter(
    highlight_level: float,
    current_seconds: float,
    choices: Sequence[tuple[str, float]],
) -> tuple[str, float] | None:
    if not math.isfinite(highlight_level) or not 0.0 <= highlight_level <= 1.0:
        raise ValueError("RAW highlight level must be finite from zero through one")
    if highlight_level >= RAW_HIGHLIGHT_TARGET:
        return None
    target_seconds = (
        choices[-1][1]
        if highlight_level == 0.0
        else current_seconds * RAW_HIGHLIGHT_TARGET / highlight_level
    )
    selected = choose_shutter(choices, target_seconds)
    return selected if selected[1] > current_seconds else None


def focus_score(image: np.ndarray, roi: NormalizedROI, minimum_contrast: float = 4.0) -> float:
    crop = roi.crop(image)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    if float(gray.std()) < minimum_contrast:
        raise CalibrationError("Focus region does not contain enough contrast")
    return float(compute_sharpness(crop, max_width=None, method="tenengrad"))


def focus_sweep_positions(start_z: float, z_min: float, z_max: float) -> list[float]:
    if not all(math.isfinite(value) for value in (start_z, z_min, z_max)):
        raise ValueError("Focus Z values must be finite")
    if z_max <= z_min or not z_min <= start_z <= z_max:
        raise ValueError("Current Z must be inside positive Z bounds")
    lowest_sample = z_min + 0.5
    if z_max - lowest_sample < 2.0:
        raise CalibrationError("Z range is too small for an autofocus sweep")
    first = min(max(start_z - 1.0, lowest_sample), z_max - 2.0)
    return [first, first + 1.0, first + 2.0]


def _focus_peak_span(
    samples: Sequence[FocusSample],
    phase: str,
    on_rejected: Callable[[str], None] | None = None,
) -> tuple[list[FocusSample], int, int]:
    def reject(message: str) -> None:
        if on_rejected is not None:
            on_rejected(message)
        raise CalibrationError(message)

    if len(samples) < 3:
        raise ValueError("A focus sweep requires at least three samples")
    ordered = sorted(samples, key=lambda item: item.z)
    peak_score = max(sample.score for sample in ordered)
    peak_indices = [index for index, sample in enumerate(ordered) if sample.score == peak_score]
    first, last = peak_indices[0], peak_indices[-1]
    if peak_indices != list(range(first, last + 1)):
        reject(f"{phase} focus sweep produced multiple equal peaks")
    if first == 0 or last == len(ordered) - 1:
        reject(f"{phase} focus sweep did not bracket a peak")
    if last - first > 1:
        reject(f"{phase} focus sweep peak is too broad")
    return ordered, first, last


def _focus_expansion(
    samples: Sequence[FocusSample],
    minimum_prominence: float = FOCUS_MIN_PROMINENCE,
) -> tuple[bool, bool]:
    ordered = sorted(samples, key=lambda item: item.z)
    peak_score = max(sample.score for sample in ordered)
    if peak_score <= 0:
        return True, True
    return (
        (peak_score - ordered[0].score) / peak_score < minimum_prominence,
        (peak_score - ordered[-1].score) / peak_score < minimum_prominence,
    )


def fine_focus_positions(
    coarse: Sequence[FocusSample],
    on_rejected: Callable[[str], None] | None = None,
) -> list[float]:
    ordered, first, last = _focus_peak_span(coarse, "Coarse", on_rejected)
    left, right = ordered[first - 1], ordered[last + 1]
    step_count = round((right.z - left.z) / 0.25)
    if step_count < 2 or not math.isclose(left.z + step_count * 0.25, right.z):
        raise CalibrationError("Coarse focus bracket is incompatible with 0.25 mm refinement")
    return [left.z + index * 0.25 for index in range(step_count + 1)]


def resolve_focus_peak(
    samples: Sequence[FocusSample],
    minimum_prominence: float = FOCUS_MIN_PROMINENCE,
    on_rejected: Callable[[str], None] | None = None,
) -> float:
    def reject(message: str) -> None:
        if on_rejected is not None:
            on_rejected(message)
        raise CalibrationError(message)

    if len(samples) < 3:
        raise ValueError("Fine focus requires at least three samples")
    if not 0.0 < minimum_prominence < 1.0:
        raise ValueError("Focus prominence must be between zero and one")
    ordered, first, last = _focus_peak_span(samples, "Fine", on_rejected)
    peak_score = ordered[first].score
    endpoint_score = max(ordered[0].score, ordered[-1].score)
    if peak_score <= 0 or (peak_score - endpoint_score) / peak_score < minimum_prominence:
        reject("Fine focus peak is not prominent across the sweep")
    if last > first:
        return (ordered[first].z + ordered[last].z) / 2.0
    left, center, right = ordered[first - 1 : first + 2]
    denominator = left.score - 2.0 * center.score + right.score
    if denominator >= 0:
        reject("Fine focus sweep did not produce a valid peak")
    spacing = right.z - center.z
    offset = 0.5 * (left.score - right.score) / denominator
    if abs(offset) > 1.0:
        reject("Calculated focus peak lies outside the fine sweep")
    return center.z + offset * spacing


def run_focus_sweep(
    *,
    start_z: float,
    z_min: float,
    z_max: float,
    roi: NormalizedROI,
    move_z: Callable[[float], None],
    capture: Callable[[int, float], str | Path],
    on_sample: Callable[[str, int, FocusSample], None] | None = None,
    on_event: Callable[[str, str, bool | None], None] | None = None,
) -> tuple[float, list[FocusSample]]:
    samples: list[FocusSample] = []
    capture_index = 0

    def measure(phase: str, z: float) -> FocusSample:
        nonlocal capture_index
        move_z(z)
        sample = FocusSample(z, focus_score(read_jpeg(capture(capture_index, z)), roi))
        if on_sample is not None:
            on_sample(phase, capture_index, sample)
        samples.append(sample)
        capture_index += 1
        return sample

    def report(phase: str, message: str, accepted: bool | None = None) -> None:
        if on_event is not None:
            on_event(phase, message, accepted)

    def expand_until_bracketed(
        phase: str,
        phase_samples: list[FocusSample],
        step: float,
        approach: float,
    ) -> None:
        while True:
            expand_lower, expand_upper = _focus_expansion(phase_samples)
            if not expand_lower and not expand_upper:
                report(
                    phase,
                    f"Peak bracketed from Z {phase_samples[0].z:.4f} to {phase_samples[-1].z:.4f} mm",
                )
                return
            lower_target = phase_samples[0].z - step
            upper_target = phase_samples[-1].z + step
            can_expand_lower = expand_lower and lower_target - approach >= z_min
            can_expand_upper = expand_upper and upper_target <= z_max
            if not can_expand_lower and not can_expand_upper:
                limits = (
                    "lower and upper hard Z limits"
                    if expand_lower and expand_upper
                    else "lower hard Z limit"
                    if expand_lower
                    else "upper hard Z limit"
                )
                message = (
                    f"{phase.title()} focus sweep reached the {limits} without finding a prominent peak"
                )
                report(phase, message, False)
                raise CalibrationError(message)
            direction = (
                "both directions"
                if can_expand_lower and can_expand_upper
                else "lower Z"
                if can_expand_lower
                else "upper Z"
            )
            report(phase, f"Peak not bracketed; expanding {direction}")
            if can_expand_lower:
                move_z(lower_target - approach)
                phase_samples.insert(0, measure(phase, lower_target))
            if can_expand_upper:
                move_z(upper_target - approach)
                phase_samples.append(measure(phase, upper_target))

    coarse_positions = focus_sweep_positions(start_z, z_min, z_max)
    report(
        "coarse",
        f"Starting Z {start_z:.4f} mm; probing Z {coarse_positions[0]:.4f} to {coarse_positions[-1]:.4f} mm",
    )
    move_z(coarse_positions[0] - 0.5)
    coarse = [measure("coarse", z) for z in coarse_positions]
    expand_until_bracketed("coarse", coarse, 1.0, 0.5)
    fine_positions = fine_focus_positions(
        coarse,
        lambda message: report("coarse", message, False),
    )

    report(
        "fine",
        f"Refining Z {fine_positions[0]:.4f} to {fine_positions[-1]:.4f} mm at 0.25 mm",
    )
    move_z(fine_positions[0] - 0.25)
    fine = [measure("fine", z) for z in fine_positions]
    expand_until_bracketed("fine", fine, 0.25, 0.25)
    peak = resolve_focus_peak(
        fine,
        on_rejected=lambda message: report("fine", message, False),
    )
    peak_score = max(sample.score for sample in fine)
    endpoint_score = max(fine[0].score, fine[-1].score)
    prominence = (peak_score - endpoint_score) / peak_score
    report("fine", f"Resolved peak Z {peak:.4f} mm with {prominence * 100:.1f}% endpoint prominence")
    move_z(peak - 0.25)
    move_z(peak)
    return peak, samples
