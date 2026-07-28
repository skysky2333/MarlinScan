from build123d import Align, Box, Location
from cadpy.assembly import label_shape

from optical_stand import (
    HEAD_PAD_Y_MAX,
    HEAD_PAD_Z_MAX,
    HEAD_PAD_Z_MIN,
    MAST_DEPTH,
    MAST_WIDTH,
    MAST_Y,
    MAST_Z_MIN,
    SOCKET_CLEARANCE,
    SOCKET_FLOOR_Z,
    SOCKET_Z_MAX,
    build_base,
    build_lens_carrier,
    build_mast,
)


FUSION_OVERLAP = 0.2


def _box(x_min, x_max, y_min, y_max, z_min, z_max):
    return Box(
        x_max - x_min,
        y_max - y_min,
        z_max - z_min,
        align=(Align.CENTER, Align.CENTER, Align.CENTER),
    ).moved(
        Location(
            (
                (x_min + x_max) / 2,
                (y_min + y_max) / 2,
                (z_min + z_max) / 2,
            )
        )
    )


def build_monolithic_stand_body():
    base = build_base(with_mount_holes=False)
    mast = build_mast(with_socket_hole=False, with_top_chamfer=False).moved(Location((0.0, MAST_Y, MAST_Z_MIN)))
    carrier = build_lens_carrier(with_press_fit=False)

    socket_fill = _box(
        -MAST_WIDTH / 2 - SOCKET_CLEARANCE - FUSION_OVERLAP / 2,
        MAST_WIDTH / 2 + SOCKET_CLEARANCE + FUSION_OVERLAP / 2,
        MAST_Y - MAST_DEPTH / 2 - SOCKET_CLEARANCE - FUSION_OVERLAP / 2,
        MAST_Y + MAST_DEPTH / 2 + SOCKET_CLEARANCE + FUSION_OVERLAP / 2,
        SOCKET_FLOOR_Z - FUSION_OVERLAP,
        SOCKET_Z_MAX,
    )
    head_fill = _box(
        -MAST_WIDTH / 2,
        MAST_WIDTH / 2,
        HEAD_PAD_Y_MAX - FUSION_OVERLAP,
        MAST_Y - MAST_DEPTH / 2 + FUSION_OVERLAP,
        HEAD_PAD_Z_MIN,
        HEAD_PAD_Z_MAX,
    )
    body = base + socket_fill + mast + head_fill + carrier
    return label_shape(body, "monolithic_stand_body")


def gen_step():
    return build_monolithic_stand_body()
