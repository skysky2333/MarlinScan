from __future__ import annotations

import re


def extract_field(line: str, field: str) -> str | None:
    # Extract "FIELD:..." stopping before the next ALL_CAPS_FIELD: marker.
    m = re.search(rf"{re.escape(field)}:(.+?)(?=\s+[A-Z_][A-Z0-9_]*:|$)", line, flags=re.IGNORECASE)
    if not m:
        return None
    return m.group(1).strip()


def parse_m115(line: str) -> tuple[str | None, str | None] | None:
    lower = line.lower()
    if ("firmware_name:" not in lower) and ("machine_type:" not in lower) and ("marlin" not in lower):
        return None

    fw = extract_field(line, "FIRMWARE_NAME")
    machine = extract_field(line, "MACHINE_TYPE")

    # Some firmwares may not provide structured fields, but do emit "echo:Marlin ..."
    if fw is None and lower.startswith("echo:") and "marlin" in lower:
        fw = line.split(":", 1)[1].strip()

    if fw is None and machine is None:
        return None
    return (fw, machine)


def parse_m105(line: str) -> tuple[float | None, float | None, float | None, float | None] | None:
    lower = line.lower()
    if "t:" not in lower and "b:" not in lower:
        return None

    hotend_now: float | None = None
    hotend_target: float | None = None
    bed_now: float | None = None
    bed_target: float | None = None

    m = re.search(r"(?:^|\s)T(?:0)?:\s*([-+]?\d*\.?\d+)\s*/\s*([-+]?\d*\.?\d+)", line)
    if m:
        hotend_now = float(m.group(1))
        hotend_target = float(m.group(2))
    else:
        m2 = re.search(r"(?:^|\s)T(?:0)?:\s*([-+]?\d*\.?\d+)", line)
        if m2:
            hotend_now = float(m2.group(1))

    b = re.search(r"(?:^|\s)B:\s*([-+]?\d*\.?\d+)\s*/\s*([-+]?\d*\.?\d+)", line)
    if b:
        bed_now = float(b.group(1))
        bed_target = float(b.group(2))
    else:
        b2 = re.search(r"(?:^|\s)B:\s*([-+]?\d*\.?\d+)", line)
        if b2:
            bed_now = float(b2.group(1))

    return (hotend_now, hotend_target, bed_now, bed_target)


def parse_m114(line: str) -> tuple[float | None, float | None, float | None, float | None] | None:
    if "X:" not in line or "Y:" not in line or "Z:" not in line:
        return None

    def find(axis: str) -> float | None:
        m = re.search(rf"\b{axis}:\s*([-+]?\d*\.?\d+)", line)
        return float(m.group(1)) if m else None

    return (find("X"), find("Y"), find("Z"), find("E"))


def parse_m503(lines: list[str]) -> tuple[dict[str, float], float | None, dict[str, float], float | None]:
    max_feed: dict[str, float] = {}
    accel_p: float | None = None
    max_accel: dict[str, float] = {}
    junction_dev: float | None = None

    for line in lines:
        if "M203" in line:
            for axis in ("X", "Y", "Z", "E"):
                m = re.search(rf"\b{axis}([-+]?\d*\.?\d+)", line)
                if m:
                    max_feed[axis] = float(m.group(1))

        if ("M204" in line) and (accel_p is None):
            m = re.search(r"\bP([-+]?\d*\.?\d+)", line)
            if m:
                accel_p = float(m.group(1))

        if "M201" in line:
            for axis in ("X", "Y", "Z", "E"):
                m = re.search(rf"\b{axis}([-+]?\d*\.?\d+)", line)
                if m:
                    max_accel[axis] = float(m.group(1))

        if ("M205" in line) and (junction_dev is None):
            m = re.search(r"\bJ([-+]?\d*\.?\d+)", line)
            if m:
                junction_dev = float(m.group(1))

    return max_feed, accel_p, max_accel, junction_dev
