from build123d import Location
from cadpy.assembly import AssemblyHelper

from optical_stand import (
    build_lens_carrier,
    build_lower_clamp,
    build_upper_clamp,
)


def _place(shape, x, y, rotation=(0.0, 0.0, 0.0)):
    rotated = shape.moved(Location((0.0, 0.0, 0.0), rotation))
    bounds = rotated.bounding_box()
    return rotated.moved(
        Location(
            (
                x - (bounds.min.X + bounds.max.X) / 2,
                y - (bounds.min.Y + bounds.max.Y) / 2,
                -bounds.min.Z,
            )
        )
    )


def gen_step():
    assembly = AssemblyHelper("optical_head_print_layout")
    assembly.add(_place(build_lens_carrier(), -45.0, 22.2, (0.0, 90.0, 0.0)), "lens_carrier")
    assembly.add(_place(build_lower_clamp(), -50.0, -45.0), "lower_clamp")
    assembly.add(_place(build_upper_clamp(), 50.0, -45.0), "upper_clamp")
    return assembly.build()
