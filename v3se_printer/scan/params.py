from __future__ import annotations

from dataclasses import dataclass


def fmt_duration(seconds: float | int) -> str:
    try:
        s = max(0, int(round(float(seconds))))
    except Exception:
        return "?"
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


@dataclass(frozen=True)
class ScanParams:
    x_min: float
    x_max: float
    y_min: float
    y_max: float
    step_x_mm: float
    step_y_mm: float
    serpentine: bool
    focus_plane: bool
    mesh_nx: int
    mesh_ny: int
    autofocus_each_tile: bool
    shots_per_tile: int
    stack_mode: str  # "none" | "best" | "nlmeans"
    capture_settle_ms: int  # 0 = auto (derived from fps / camera settle)
    downsample: int  # reserved; currently always 1 (full-res)
    build_pyramidal_tiff: bool
    tiff_compression: str  # "none" | "lzw" | "deflate"
    out_base_dir: str
