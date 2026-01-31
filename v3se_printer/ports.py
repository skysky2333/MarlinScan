from __future__ import annotations

import os

from serial.tools import list_ports  # type: ignore

from .models import PortItem


def _dialin_for_callout(device: str) -> str | None:
    if device.startswith("/dev/cu."):
        tty = "/dev/tty." + device[len("/dev/cu.") :]
        if os.path.exists(tty):
            return tty
    return None


def _format_vidpid(port: object) -> str:
    vid = getattr(port, "vid", None)
    pid = getattr(port, "pid", None)
    if vid is None or pid is None:
        return ""
    return f"{vid:04X}:{pid:04X}"


def list_serial_ports(*, include_dialin: bool = True) -> list[PortItem]:
    items: list[PortItem] = []
    for p in list_ports.comports():
        dev = p.device
        desc = (getattr(p, "description", "") or "").strip()
        vidpid = _format_vidpid(p)
        extra = f" ({vidpid})" if vidpid else ""
        label = f"{dev} — {desc}{extra}" if desc else f"{dev}{extra}"
        items.append(PortItem(label=label, device=dev))

        if include_dialin:
            dialin = _dialin_for_callout(dev)
            if dialin:
                items.append(PortItem(label=f"{dialin} — dialin for {dev}", device=dialin))

    # De-dupe by device while preserving order.
    seen: set[str] = set()
    out: list[PortItem] = []
    for it in items:
        if it.device in seen:
            continue
        seen.add(it.device)
        out.append(it)
    return out

