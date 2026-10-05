# Copyright 2026 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

"""Pure geometry helpers for selecting whole logo pixels.

The ArtistIC logo is a grid of indivisible artwork pixels.  The helpers in
this module intentionally know nothing about KLayout: callers provide a
predicate that reports whether a candidate pixel intersects a keepout.
"""


def iter_pixel_boxes(rows, width_px, height_px, bbox_dbu, feature_dbu,
                     offset_x_dbu=0, offset_y_dbu=0, pitch_dbu=None):
    """Yield requested logo pixels as ``(left, bottom, right, top)`` boxes.

    ``rows`` contains run-length encoded pixels with an exclusive run end,
    matching the mask produced by ArtistIC's host-side preparation step.
    Coordinates are integer database units.  The canvas is centered on the
    supplied layout bounding box and then translated by the requested offset.
    Every yielded box has exactly ``feature_dbu`` units on each side.  The
    lattice pitch defaults to twice that width, and must exceed the width so
    squares never touch at an edge or a corner.  Centering uses the actual
    array extent, including when its midpoint is between database units.
    """
    width_px = int(width_px)
    height_px = int(height_px)
    if int(feature_dbu) != feature_dbu:
        raise ValueError("logo feature must be an integer database-unit width")
    feature_dbu = int(feature_dbu)
    pitch_dbu = 2 * feature_dbu if pitch_dbu is None else pitch_dbu
    if int(pitch_dbu) != pitch_dbu or pitch_dbu <= feature_dbu:
        raise ValueError("logo pitch must be an integer greater than feature width")
    pitch_dbu = int(pitch_dbu)
    if width_px <= 0 or height_px <= 0:
        raise ValueError("logo dimensions must be positive")
    if feature_dbu <= 0:
        raise ValueError("logo feature must be at least one database unit")

    x0, y0, x1, y1 = [int(value) for value in bbox_dbu]
    centre_x = (x0 + x1) / 2.0 + float(offset_x_dbu)
    centre_y = (y0 + y1) / 2.0 + float(offset_y_dbu)
    width_dbu = (width_px - 1) * pitch_dbu + feature_dbu
    height_dbu = (height_px - 1) * pitch_dbu + feature_dbu
    left = int(round(centre_x - width_dbu / 2.0))
    top = int(round(centre_y + height_dbu / 2.0))

    for row in rows:
        y = int(row["y"])
        if y < 0 or y >= height_px:
            raise ValueError("logo row is outside the requested canvas")
        for start, end in row["runs"]:
            start = int(start)
            end = int(end)
            if start < 0 or end < start or end > width_px:
                raise ValueError("logo run is outside the requested canvas")
            for x in range(start, end):
                xl = left + x * pitch_dbu
                xr = xl + feature_dbu
                yt = top - y * pitch_dbu
                yb = yt - feature_dbu
                yield (xl, yb, xr, yt)


def select_pixel_boxes(rows, width_px, height_px, bbox_dbu, feature_dbu,
                       is_blocked, offset_x_dbu=0, offset_y_dbu=0, pitch_dbu=None):
    """Return whole requested pixels that do not intersect a keepout.

    ``is_blocked`` receives each candidate box and must return true when any
    part of that box intersects the keepout.  A blocked pixel is discarded in
    its entirety; no boolean subtraction or partial polygon is performed.
    """
    selected = []
    for box in iter_pixel_boxes(rows, width_px, height_px, bbox_dbu,
                                feature_dbu, offset_x_dbu, offset_y_dbu, pitch_dbu):
        if not is_blocked(box):
            selected.append(box)
    return selected


def box_is_inside(box, boundary):
    """Return true when a box lies completely inside a boundary box."""
    left, bottom, right, top = box
    bound_left, bound_bottom, bound_right, bound_top = boundary
    return (left >= bound_left and bottom >= bound_bottom and
            right <= bound_right and top <= bound_top)
