# Copyright 2026 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

"""Pure geometry helpers for selecting whole logo pixels.

The ArtistIC logo is a grid of indivisible artwork pixels.  The helpers in
this module intentionally know nothing about KLayout: callers provide a
predicate that reports whether a candidate pixel intersects a keepout.
"""


def iter_pixel_boxes(rows, width_px, height_px, bbox_dbu, feature_dbu,
                     offset_x_dbu=0, offset_y_dbu=0):
    """Yield requested logo pixels as ``(left, bottom, right, top)`` boxes.

    ``rows`` contains run-length encoded pixels with an exclusive run end,
    matching the mask produced by ArtistIC's host-side preparation step.
    Coordinates are integer database units.  The canvas is centered on the
    supplied layout bounding box and then translated by the requested offset.
    Every yielded box has exactly ``feature_dbu`` units on each side and is
    therefore aligned to the same grid, including when the canvas midpoint is
    between database units.
    """
    width_px = int(width_px)
    height_px = int(height_px)
    feature_dbu = int(round(float(feature_dbu)))
    if width_px < 0 or height_px < 0:
        raise ValueError("logo dimensions must not be negative")
    if feature_dbu <= 0:
        raise ValueError("logo feature must be at least one database unit")

    x0, y0, x1, y1 = [int(value) for value in bbox_dbu]
    centre_x = (x0 + x1) / 2.0 + float(offset_x_dbu)
    centre_y = (y0 + y1) / 2.0 + float(offset_y_dbu)
    left = int(round(centre_x - width_px * feature_dbu / 2.0))
    top = int(round(centre_y + height_px * feature_dbu / 2.0))

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
                xl = left + x * feature_dbu
                xr = xl + feature_dbu
                yt = top - y * feature_dbu
                yb = yt - feature_dbu
                yield (xl, yb, xr, yt)


def select_pixel_boxes(rows, width_px, height_px, bbox_dbu, feature_dbu,
                       is_blocked, offset_x_dbu=0, offset_y_dbu=0):
    """Return whole requested pixels that do not intersect a keepout.

    ``is_blocked`` receives each candidate box and must return true when any
    part of that box intersects the keepout.  A blocked pixel is discarded in
    its entirety; no boolean subtraction or partial polygon is performed.
    """
    selected = []
    for box in iter_pixel_boxes(rows, width_px, height_px, bbox_dbu,
                                feature_dbu, offset_x_dbu, offset_y_dbu):
        if not is_blocked(box):
            selected.append(box)
    return selected


def box_is_inside(box, boundary):
    """Return true when a box lies completely inside a boundary box."""
    left, bottom, right, top = box
    bound_left, bound_bottom, bound_right, bound_top = boundary
    return (left >= bound_left and bottom >= bound_bottom and
            right <= bound_right and top <= bound_top)
