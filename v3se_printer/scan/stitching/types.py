from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Entry:
    row: int
    col: int
    alignment_path: str
    composite_path: str


@dataclass(frozen=True)
class LayoutMosaic:
    pano: object  # numpy ndarray or memmap
    out_w: int
    out_h: int
    memmap_path: str | None
    weights_memmap_path: str | None
    stage_meta: dict[str, object]
