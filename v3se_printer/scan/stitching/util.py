from __future__ import annotations


def median(values: list[float]) -> float:
    vals = sorted(float(v) for v in values)
    if not vals:
        return 0.0
    mid = int(len(vals) // 2)
    if (len(vals) % 2) == 1:
        return float(vals[mid])
    return 0.5 * float(vals[mid - 1] + vals[mid])


def scale_for_megapix(*, orig_mp: float, target_mp: float) -> float:
    if float(target_mp) <= 0:
        return 1.0
    # Preserve aspect; clamp aggressive downsamples.
    import math

    s = math.sqrt(float(target_mp) / float(orig_mp))
    return max(0.02, min(1.0, float(s)))
