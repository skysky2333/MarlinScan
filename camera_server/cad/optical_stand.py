from math import atan2, degrees, hypot

from build123d import Align, Axis, Box, Cylinder, Location, chamfer
from cadpy.assembly import AssemblyHelper, label_shape


TUBE_DIAMETER = 54.0
CLAMP_OUTER_DIAMETER = 62.0
CLAMP_TOTAL_WIDTH = 80.0
CLAMP_BOLT_SPACING = 72.0
CLAMP_CHORD_OFFSET = 17.0
LOWER_CLAMP_BASE = 198.0
LOWER_CLAMP_HEIGHT = 8.0
UPPER_CLAMP_BASE = 263.0
UPPER_CLAMP_HEIGHT = 25.0

SPINE_X_MIN = -22.0
SPINE_X_MAX = 22.0
SPINE_Y_MIN = 28.0
SPINE_Y_MAX = 38.0
SPINE_Z_MIN = LOWER_CLAMP_BASE
SPINE_Z_MAX = UPPER_CLAMP_BASE + UPPER_CLAMP_HEIGHT

HEAD_BEAM_X_MIN = -22.0
HEAD_BEAM_X_MAX = 22.0
HEAD_BEAM_Y_MIN = 37.8
HEAD_BEAM_Y_MAX = 82.2
HEAD_BEAM_Z_MIN = 213.0
HEAD_BEAM_Z_MAX = 263.0
HEAD_PAD_X_MIN = -31.3
HEAD_PAD_X_MAX = 31.3
HEAD_PAD_Y_MIN = 82.0
HEAD_PAD_Y_MAX = 90.0
HEAD_PAD_Z_MIN = 203.0
HEAD_PAD_Z_MAX = 273.0
HEAD_SADDLE_INNER_X = 25.3
HEAD_SADDLE_Y_MAX = 122.0
HEAD_SOCKET_INNER_Y_MAX = 120.3
HEAD_SOCKET_Y_MAX = 126.3
HEAD_PRESS_PAD_ZS = (220.0, 256.0)
HEAD_PRESS_PAD_RADIUS = 4.0
HEAD_PRESS_PAD_Y = 105.0
HEAD_PRESS_INTERFERENCE = 0.1
HEAD_PRESS_PAD_EMBED = 0.2
HEAD_PRESS_RUNNING_CLEARANCE = 0.3
HEAD_PRESS_BEARING_PAD_XS = (-15.0, 15.0)
HEAD_PRESS_STOP_Z = 274.0
HEAD_PRESS_STOP_THICKNESS = 5.0

BASE_THICKNESS = 16.0
RAIL_WIDTH = 24.0
FRONT_ROOT_X = 45.0
FRONT_ROOT_Y = 80.0
FRONT_ELBOW_X = 138.0
FRONT_END_Y = -120.0
REAR_ROOT_X = 35.0
REAR_ROOT_Y = 130.0
REAR_END_X = 115.0
REAR_END_Y = 145.0

HUB_X_MIN = -52.0
HUB_X_MAX = 52.0
HUB_Y_MIN = 68.0
HUB_Y_MAX = 145.0
HUB_Z_MIN = 0.0
HUB_Z_MAX = BASE_THICKNESS
SOCKET_X_MIN = -32.0
SOCKET_X_MAX = 32.0
SOCKET_Y_MIN = 80.0
SOCKET_Y_MAX = 130.0
SOCKET_Z_MIN = 15.8
SOCKET_Z_MAX = 74.0
SOCKET_CLEARANCE = 0.3
SOCKET_FLOOR_Z = 24.0

MAST_WIDTH = 50.0
MAST_DEPTH = 30.0
MAST_LENGTH = 250.0
MAST_Y = 105.0
MAST_Z_MIN = 24.0
MAST_SOCKET_HOLE_ZS = (24.0,)
MAST_PRESS_LEAD_IN = 1.0

M5_CLEARANCE = 5.5
M3_CLEARANCE = 3.2
M3_HEAD_CLEARANCE = 6.5
M3_INSERT_DIAMETER = 4.3
M3_INSERT_DEPTH = 8.0
BOOLEAN_OVERSHOOT = 0.2


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


def _cylinder_x(diameter, length, x, y, z):
    return Cylinder(
        diameter / 2,
        length,
        align=(Align.CENTER, Align.CENTER, Align.CENTER),
        rotation=(0, 90, 0),
    ).moved(Location((x, y, z)))


def _hole_x(diameter, length, x, y, z):
    return _cylinder_x(diameter, length, x, y, z)


def _cylinder_y(diameter, length, x, y, z):
    return Cylinder(
        diameter / 2,
        length,
        align=(Align.CENTER, Align.CENTER, Align.CENTER),
        rotation=(90, 0, 0),
    ).moved(Location((x, y, z)))


def _hole_y(diameter, length, x, y, z):
    return _cylinder_y(diameter, length, x, y, z)


def _hole_z(diameter, length, x, y, z):
    return Cylinder(
        diameter / 2,
        length,
        align=(Align.CENTER, Align.CENTER, Align.CENTER),
    ).moved(Location((x, y, z)))


def _rail_xy(x1, y1, x2, y2):
    length = hypot(x2 - x1, y2 - y1)
    angle = degrees(atan2(y2 - y1, x2 - x1))
    rail = Box(
        length,
        RAIL_WIDTH,
        BASE_THICKNESS,
        align=(Align.CENTER, Align.CENTER, Align.MIN),
    ).moved(
        Location(
            ((x1 + x2) / 2, (y1 + y2) / 2, 0.0),
            (0.0, 0.0, angle),
        )
    )
    for x, y in ((x1, y1), (x2, y2)):
        rail += Cylinder(
            RAIL_WIDTH / 2,
            BASE_THICKNESS,
            align=(Align.CENTER, Align.CENTER, Align.MIN),
        ).moved(Location((x, y, 0.0)))
    return rail


def build_base(with_mount_holes=True):
    base = _box(HUB_X_MIN, HUB_X_MAX, HUB_Y_MIN, HUB_Y_MAX, HUB_Z_MIN, HUB_Z_MAX)
    for side in (-1.0, 1.0):
        base += _rail_xy(side * FRONT_ROOT_X, FRONT_ROOT_Y, side * FRONT_ELBOW_X, FRONT_ROOT_Y)
        base += _rail_xy(side * FRONT_ELBOW_X, FRONT_ROOT_Y, side * FRONT_ELBOW_X, FRONT_END_Y)
        base += _rail_xy(side * REAR_ROOT_X, REAR_ROOT_Y, side * REAR_END_X, REAR_END_Y)

    socket = _box(
        SOCKET_X_MIN,
        SOCKET_X_MAX,
        SOCKET_Y_MIN,
        SOCKET_Y_MAX,
        SOCKET_Z_MIN,
        SOCKET_Z_MAX,
    )
    cavity = _box(
        -MAST_WIDTH / 2 - SOCKET_CLEARANCE,
        MAST_WIDTH / 2 + SOCKET_CLEARANCE,
        MAST_Y - MAST_DEPTH / 2 - SOCKET_CLEARANCE,
        MAST_Y + MAST_DEPTH / 2 + SOCKET_CLEARANCE,
        SOCKET_FLOOR_Z,
        SOCKET_Z_MAX + BOOLEAN_OVERSHOOT,
    )
    base += socket
    base -= cavity

    if with_mount_holes:
        for local_z in MAST_SOCKET_HOLE_ZS:
            base -= _hole_x(M5_CLEARANCE, SOCKET_X_MAX - SOCKET_X_MIN + 2, 0.0, MAST_Y, MAST_Z_MIN + local_z)
    return label_shape(base, "stand_base")


def build_mast(with_socket_hole=True, with_top_chamfer=True):
    mast = _box(
        -MAST_WIDTH / 2,
        MAST_WIDTH / 2,
        -MAST_DEPTH / 2,
        MAST_DEPTH / 2,
        0.0,
        MAST_LENGTH,
    )
    if with_top_chamfer:
        top_edges = mast.edges().filter_by_position(Axis.Z, MAST_LENGTH, MAST_LENGTH)
        mast = chamfer(top_edges, MAST_PRESS_LEAD_IN)
    if with_socket_hole:
        for z in MAST_SOCKET_HOLE_ZS:
            mast -= _hole_x(M5_CLEARANCE, MAST_WIDTH + 2, 0.0, 0.0, z)
    return label_shape(mast, "rear_mast")


def _annular_clamp(height, base, fixed):
    outer = Cylinder(
        CLAMP_OUTER_DIAMETER / 2,
        height,
        align=(Align.CENTER, Align.CENTER, Align.MIN),
    ).moved(Location((0.0, 0.0, base)))
    inner = Cylinder(
        TUBE_DIAMETER / 2,
        height + 2,
        align=(Align.CENTER, Align.CENTER, Align.MIN),
    ).moved(Location((0.0, 0.0, base - 1)))
    ring = outer - inner

    if fixed:
        clip = Box(
            100.0,
            100.0,
            height + 2,
            align=(Align.CENTER, Align.MIN, Align.MIN),
        ).moved(Location((0.0, CLAMP_CHORD_OFFSET, base - 1)))
        lug_y_min = CLAMP_CHORD_OFFSET
        lug_y_max = 37.0
    else:
        clip = Box(
            100.0,
            100.0,
            height + 2,
            align=(Align.CENTER, Align.MAX, Align.MIN),
        ).moved(Location((0.0, -CLAMP_CHORD_OFFSET, base - 1)))
        lug_y_min = -21.0
        lug_y_max = -CLAMP_CHORD_OFFSET

    clamp = ring & clip
    lug_width = (CLAMP_TOTAL_WIDTH - 44.0) / 2
    lug_center = 22.0 + lug_width / 2
    for x in (-lug_center, lug_center):
        clamp += _box(
            x - lug_width / 2,
            x + lug_width / 2,
            lug_y_min,
            lug_y_max,
            base,
            base + height,
        )

    screw_zs = (base + height / 3, base + 2 * height / 3) if height == UPPER_CLAMP_HEIGHT else (base + height / 2,)
    for x in (-CLAMP_BOLT_SPACING / 2, CLAMP_BOLT_SPACING / 2):
        for z in screw_zs:
            if fixed:
                clamp -= _hole_y(
                    M3_INSERT_DIAMETER,
                    M3_INSERT_DEPTH,
                    x,
                    lug_y_min + M3_INSERT_DEPTH / 2,
                    z,
                )
            else:
                clamp -= _hole_y(
                    M3_CLEARANCE,
                    lug_y_max - lug_y_min + 0.2,
                    x,
                    (lug_y_min + lug_y_max) / 2,
                    z,
                )
                clamp -= _hole_y(
                    M3_HEAD_CLEARANCE,
                    1.0,
                    x,
                    lug_y_min + 0.5,
                    z,
                )
    return clamp


def build_lens_carrier(with_press_fit=True):
    spine = _box(SPINE_X_MIN, SPINE_X_MAX, SPINE_Y_MIN, SPINE_Y_MAX, SPINE_Z_MIN, SPINE_Z_MAX)
    beam = _box(
        HEAD_BEAM_X_MIN,
        HEAD_BEAM_X_MAX,
        HEAD_BEAM_Y_MIN,
        HEAD_BEAM_Y_MAX,
        HEAD_BEAM_Z_MIN,
        HEAD_BEAM_Z_MAX,
    )
    saddle_z_max = HEAD_PRESS_STOP_Z + BOOLEAN_OVERSHOOT if with_press_fit else HEAD_PAD_Z_MAX
    saddle_y_max = HEAD_SOCKET_Y_MAX if with_press_fit else HEAD_SADDLE_Y_MAX
    pad_y_max = HEAD_PAD_Y_MAX - HEAD_PRESS_RUNNING_CLEARANCE if with_press_fit else HEAD_PAD_Y_MAX
    pad = _box(
        HEAD_PAD_X_MIN,
        HEAD_PAD_X_MAX,
        HEAD_PAD_Y_MIN,
        pad_y_max,
        HEAD_PAD_Z_MIN,
        saddle_z_max,
    )
    left_cheek = _box(
        HEAD_PAD_X_MIN,
        -HEAD_SADDLE_INNER_X,
        HEAD_PAD_Y_MIN,
        saddle_y_max,
        HEAD_PAD_Z_MIN,
        saddle_z_max,
    )
    right_cheek = _box(
        HEAD_SADDLE_INNER_X,
        HEAD_PAD_X_MAX,
        HEAD_PAD_Y_MIN,
        saddle_y_max,
        HEAD_PAD_Z_MIN,
        saddle_z_max,
    )
    carrier = spine + beam + pad + left_cheek + right_cheek
    if with_press_fit:
        carrier += _box(
            HEAD_PAD_X_MIN,
            HEAD_PAD_X_MAX,
            HEAD_SOCKET_INNER_Y_MAX,
            HEAD_SOCKET_Y_MAX,
            HEAD_PAD_Z_MIN,
            saddle_z_max,
        )
        carrier += _box(
            HEAD_PAD_X_MIN,
            HEAD_PAD_X_MAX,
            pad_y_max,
            HEAD_SOCKET_Y_MAX,
            HEAD_PRESS_STOP_Z,
            HEAD_PRESS_STOP_Z + HEAD_PRESS_STOP_THICKNESS,
        )
        pad_inner_x = MAST_WIDTH / 2 - HEAD_PRESS_INTERFERENCE
        pad_outer_x = HEAD_SADDLE_INNER_X + HEAD_PRESS_PAD_EMBED
        for side in (-1.0, 1.0):
            x_inner = side * pad_inner_x
            x_outer = side * pad_outer_x
            for z in HEAD_PRESS_PAD_ZS:
                carrier += _cylinder_x(
                    2 * HEAD_PRESS_PAD_RADIUS,
                    abs(x_outer - x_inner),
                    (x_inner + x_outer) / 2,
                    HEAD_PRESS_PAD_Y,
                    z,
                )
        front_pad_y_min = pad_y_max - HEAD_PRESS_PAD_EMBED
        front_pad_y_max = MAST_Y - MAST_DEPTH / 2
        for x in HEAD_PRESS_BEARING_PAD_XS:
            for z in HEAD_PRESS_PAD_ZS:
                carrier += _cylinder_y(
                    2 * HEAD_PRESS_PAD_RADIUS,
                    front_pad_y_max - front_pad_y_min,
                    x,
                    (front_pad_y_min + front_pad_y_max) / 2,
                    z,
                )
        rear_pad_y_min = MAST_Y + MAST_DEPTH / 2
        rear_pad_y_max = HEAD_SOCKET_INNER_Y_MAX + HEAD_PRESS_PAD_EMBED
        for x in HEAD_PRESS_BEARING_PAD_XS:
            for z in HEAD_PRESS_PAD_ZS:
                carrier += _cylinder_y(
                    2 * HEAD_PRESS_PAD_RADIUS,
                    rear_pad_y_max - rear_pad_y_min,
                    x,
                    (rear_pad_y_min + rear_pad_y_max) / 2,
                    z,
                )
    carrier += _annular_clamp(LOWER_CLAMP_HEIGHT, LOWER_CLAMP_BASE, True)
    carrier += _annular_clamp(UPPER_CLAMP_HEIGHT, UPPER_CLAMP_BASE, True)
    return label_shape(carrier, "lens_carrier")


def build_lower_clamp():
    return label_shape(_annular_clamp(LOWER_CLAMP_HEIGHT, LOWER_CLAMP_BASE, False), "lower_clamp_cap")


def build_upper_clamp():
    return label_shape(_annular_clamp(UPPER_CLAMP_HEIGHT, UPPER_CLAMP_BASE, False), "upper_clamp_cap")


def build_parts():
    parts = {"stand_base": build_base()}
    parts["rear_mast"] = build_mast().moved(Location((0.0, MAST_Y, MAST_Z_MIN)))
    parts["lens_carrier"] = build_lens_carrier()
    parts["lower_clamp"] = build_lower_clamp()
    parts["upper_clamp"] = build_upper_clamp()
    return parts


def gen_step():
    assembly = AssemblyHelper("rear_mount_optical_stand")
    for name, part in build_parts().items():
        assembly.add(part, name)
    return assembly.build()
