from build123d import Location

from optical_stand import build_mast


def gen_step():
    mast = build_mast().moved(Location((0.0, 0.0, 0.0), (90.0, 0.0, 0.0)))
    bounds = mast.bounding_box()
    return mast.moved(
        Location(
            (
                -(bounds.min.X + bounds.max.X) / 2,
                -(bounds.min.Y + bounds.max.Y) / 2,
                -bounds.min.Z,
            )
        )
    )
