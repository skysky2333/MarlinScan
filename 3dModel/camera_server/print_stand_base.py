from build123d import Location

from optical_stand import build_base


def gen_step():
    base = build_base()
    bounds = base.bounding_box()
    return base.moved(
        Location(
            (
                -(bounds.min.X + bounds.max.X) / 2,
                -(bounds.min.Y + bounds.max.Y) / 2,
                -bounds.min.Z,
            )
        )
    )
