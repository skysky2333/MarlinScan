from pathlib import Path


SRGB_ICC_PROFILE = Path("/System/Library/ColorSync/Profiles/sRGB Profile.icc")
SRGB_TO_XYZ_MATRIX = (
    (0.4124564, 0.3575761, 0.1804375),
    (0.2126729, 0.7151522, 0.0721750),
    (0.0193339, 0.1191920, 0.9503041),
)
SRGB_TO_REC2020_MATRIX = (
    (0.627452, 0.329249, 0.043299),
    (0.069109, 0.919531, 0.011360),
    (0.016398, 0.088030, 0.895572),
)
REC2020_TO_SRGB_MATRIX = (
    (1.660491, -0.587641, -0.072850),
    (-0.124550, 1.132900, -0.008349),
    (-0.018151, -0.100579, 1.118730),
)


def require_srgb_icc_profile() -> Path:
    if not SRGB_ICC_PROFILE.is_file():
        raise RuntimeError(f"sRGB ICC profile is missing: {SRGB_ICC_PROFILE}")
    return SRGB_ICC_PROFILE
