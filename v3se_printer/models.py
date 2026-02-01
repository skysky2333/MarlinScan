from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Priority = Literal["high", "low"]


@dataclass(frozen=True)
class PortItem:
    label: str
    device: str


@dataclass(frozen=True)
class GCodeJob:
    command: str
    tag: str = ""
    show_in_log: bool = True
    timeout_s: float = 10.0
    priority: Priority = "high"


@dataclass
class PrinterConfig:
    max_feedrate_mm_s: dict[str, float] = field(default_factory=dict)  # from M203, mm/s
    accel_print_mm_s2: float | None = None  # from M204 P, mm/s^2
    max_accel_mm_s2: dict[str, float] = field(default_factory=dict)  # from M201, mm/s^2
    junction_deviation: float | None = None  # from M205 J
