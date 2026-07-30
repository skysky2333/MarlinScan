from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Callable


ProgressCallback = Callable[[str, str, int, int | None, str], None]


@dataclass(frozen=True)
class StepProgress:
    phase: str
    label: str
    completed: int
    total: int | None
    unit: str
    eta_seconds: float | None

    def __post_init__(self) -> None:
        for name, value in (("phase", self.phase), ("label", self.label), ("unit", self.unit)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Progress {name} must be non-empty")
        if isinstance(self.completed, bool) or not isinstance(self.completed, int) or self.completed < 0:
            raise ValueError("Progress completed count must be a non-negative integer")
        if self.total is not None:
            if isinstance(self.total, bool) or not isinstance(self.total, int) or self.total < 0:
                raise ValueError("Progress total must be a non-negative integer or None")
            if self.completed > self.total:
                raise ValueError("Progress completed count must not exceed total")
        if self.eta_seconds is not None:
            if (
                isinstance(self.eta_seconds, bool)
                or not isinstance(self.eta_seconds, (int, float))
                or not math.isfinite(self.eta_seconds)
                or self.eta_seconds < 0
            ):
                raise ValueError("Progress ETA must be a finite non-negative number or None")
            if self.total is None:
                raise ValueError("Progress ETA requires a finite total")
            if self.completed == 0:
                raise ValueError("Progress ETA requires completed work")


class StepProgressTracker:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._current: StepProgress | None = None
        self._phase_started_at: float | None = None
        self._last_completed_at: float | None = None
        self._last_clock: float | None = None

    @property
    def current(self) -> StepProgress | None:
        return self._current

    def reset(self) -> None:
        self._current = None
        self._phase_started_at = None
        self._last_completed_at = None
        self._last_clock = None

    def update(
        self,
        phase: str,
        label: str,
        completed: int,
        total: int | None,
        unit: str,
    ) -> StepProgress:
        candidate = StepProgress(phase, label, completed, total, unit, None)
        current = self._current
        phase_changed = current is None or candidate.phase != current.phase
        if phase_changed:
            if candidate.completed != 0:
                raise ValueError("A progress phase must start with zero completed work")
        else:
            if candidate.label != current.label or candidate.unit != current.unit:
                raise ValueError("Progress phase label and unit must remain unchanged")
            if candidate.completed < current.completed:
                raise ValueError("Progress completed count must not decrease within a phase")
            if current.total is not None and candidate.total != current.total:
                raise ValueError("A finite progress total must remain unchanged within a phase")

        now = self._read_clock()
        if phase_changed:
            self._phase_started_at = now
            self._last_completed_at = None
        elif candidate.completed > current.completed:
            self._last_completed_at = now

        eta = None
        if candidate.total is not None and candidate.completed > 0:
            if self._phase_started_at is None or self._last_completed_at is None:
                raise RuntimeError("Progress timing evidence is unavailable")
            elapsed = self._last_completed_at - self._phase_started_at
            eta = max(0.0, elapsed / candidate.completed * (candidate.total - candidate.completed))
        self._current = StepProgress(
            candidate.phase,
            candidate.label,
            candidate.completed,
            candidate.total,
            candidate.unit,
            eta,
        )
        return self._current

    def _read_clock(self) -> float:
        value = self._clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("Progress clock must return a finite number")
        value = float(value)
        if self._last_clock is not None and value < self._last_clock:
            raise ValueError("Progress clock must not move backward")
        self._last_clock = value
        return value


def format_step_progress(progress: StepProgress) -> str:
    count = (
        f"{progress.completed} {progress.unit}"
        if progress.total is None
        else f"{progress.completed}/{progress.total} {progress.unit}"
    )
    if progress.eta_seconds is None:
        eta = "unavailable"
    else:
        seconds = int(math.ceil(progress.eta_seconds))
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        eta = (
            f"{hours}h {minutes}m"
            if hours
            else f"{minutes}m {seconds}s"
            if minutes
            else f"{seconds}s"
        )
    return f"{progress.label}: {count}; ETA {eta}"
