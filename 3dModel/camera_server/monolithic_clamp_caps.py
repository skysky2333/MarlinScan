from build123d import Location
from cadpy.assembly import AssemblyHelper

from optical_stand import build_lower_clamp, build_upper_clamp


def _place(shape, x):
    bounds = shape.bounding_box()
    return shape.moved(
        Location(
            (
                x - (bounds.min.X + bounds.max.X) / 2,
                -(bounds.min.Y + bounds.max.Y) / 2,
                -bounds.min.Z,
            )
        )
    )


def gen_step():
    assembly = AssemblyHelper("monolithic_stand_clamp_caps")
    assembly.add(_place(build_lower_clamp(), -50.0), "lower_clamp_cap")
    assembly.add(_place(build_upper_clamp(), 50.0), "upper_clamp_cap")
    return assembly.build()
