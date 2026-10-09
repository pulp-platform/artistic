# Copyright 2026 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

"""Chip shadows for rendered images."""

from __future__ import annotations

import math

from .palettes import background_rgba
from .project import ProjectError


def _number(value: object, label: str, minimum: float | None = None,
            maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProjectError(f"{label} must be a finite number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ProjectError(f"{label} must be a finite number") from exc
    if (not math.isfinite(number) or
            (minimum is not None and number < minimum) or
            (maximum is not None and number > maximum)):
        bounds = (f" between {minimum:g} and {maximum:g}"
                  if minimum is not None and maximum is not None else "")
        raise ProjectError(f"{label} must be a finite number{bounds}")
    return number


def _bounds(value: object, label: str) -> tuple[float, float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ProjectError(f"{label} must contain left, bottom, right, and top")
    bounds = tuple(_number(item, label) for item in value)
    left, bottom, right, top = bounds
    if (right <= left or top <= bottom or not math.isfinite(right - left) or
            not math.isfinite(top - bottom)):
        raise ProjectError(f"{label} must define a positive finite area")
    return bounds


def shadow_options(render: dict) -> dict | None:
    """Validate and normalize optional whole-chip shadow settings."""
    if not isinstance(render, dict):
        raise ProjectError("[render] must be a table")
    if "shadow" not in render:
        return None
    value = render["shadow"]
    if not isinstance(value, dict):
        raise ProjectError("[render.shadow] must be a table")
    unknown = set(value) - {"enabled", "padding_px", "blur_px", "offset_px", "opacity", "color"}
    if unknown:
        raise ProjectError("unknown [render.shadow] options: " +
                           ", ".join(str(key) for key in sorted(unknown, key=str)))
    enabled = value.get("enabled", True)
    if type(enabled) is not bool:
        raise ProjectError("[render.shadow].enabled must be a boolean")
    if not enabled:
        return None

    padding = value.get("padding_px", 80)
    if type(padding) is not int or not 0 <= padding <= 10000:
        raise ProjectError("[render.shadow].padding_px must be an integer from 0 to 10000")
    blur = _number(value.get("blur_px", 16), "[render.shadow].blur_px", 0, 10000)
    offset = value.get("offset_px", [7, 12])
    if not isinstance(offset, (list, tuple)) or len(offset) != 2:
        raise ProjectError("[render.shadow].offset_px must contain two numbers")
    offset = [_number(item, "[render.shadow].offset_px", -10000, 10000)
              for item in offset]
    opacity = _number(value.get("opacity", .28), "[render.shadow].opacity", 0, 1)
    color = value.get("color", "#000000")
    try:
        rgba = background_rgba(color)
    except ProjectError as exc:
        raise ProjectError("[render.shadow].color must be a valid opaque color") from exc
    if rgba[3] != 255:
        raise ProjectError("[render.shadow].color must be opaque")
    return {"enabled": True, "padding_px": padding, "blur_px": blur,
            "offset_px": offset, "opacity": opacity, "color": color}


def _resolution(record: dict) -> tuple[int, int]:
    resolution = record.get("resolution")
    if (not isinstance(resolution, (list, tuple)) or len(resolution) != 2 or
            any(type(value) is not int or value <= 0 for value in resolution)):
        raise ProjectError("record resolution must contain two positive integers")
    return resolution


def presentation_geometry(render: dict, record: dict) -> dict:
    """Return padding, image size, and the layout's pixel bounds."""
    options = shadow_options(render)
    width, height = _resolution(record)
    if options is None:
        return {"padding": 0, "size": [width, height],
                "box": [0, 0, width, height]}

    try:
        layout = record["layout"]
        gds = record["gds"]
        bbox = _bounds(layout["bbox_um"], "layout bbox_um")
        viewport = _bounds(gds["viewport_um"], "gds viewport_um")
    except (KeyError, TypeError) as exc:
        raise ProjectError("record must contain layout bbox_um and gds viewport_um; "
                           "run render generate again") from exc

    left, bottom, right, top = bbox
    vx0, vy0, vx1, vy1 = viewport
    ix0, iy0 = max(left, vx0), max(bottom, vy0)
    ix1, iy1 = min(right, vx1), min(top, vy1)
    if ix0 >= ix1 or iy0 >= iy1:
        raise ProjectError("layout bbox_um does not intersect gds viewport_um")

    def edge(value, limit, rounding):
        nearest = round(value)
        if math.isclose(value, nearest, rel_tol=0, abs_tol=1e-9):
            value = nearest
        return max(0, min(limit, rounding(value)))

    px_left = edge((ix0 - vx0) * width / (vx1 - vx0), width, math.floor)
    px_right = edge((ix1 - vx0) * width / (vx1 - vx0), width, math.ceil)
    px_top = edge((vy1 - iy1) * height / (vy1 - vy0), height, math.floor)
    px_bottom = edge((vy1 - iy0) * height / (vy1 - vy0), height, math.ceil)
    if px_left >= px_right or px_top >= px_bottom:
        raise ProjectError("layout bbox_um has no pixels in gds viewport_um")
    padding = options["padding_px"]
    return {"padding": padding, "size": [width + 2 * padding, height + 2 * padding],
            "box": [px_left, px_top, px_right, px_bottom]}


def shadow_image(render: dict, record: dict):
    """Render the blurred shadow layer in padded output coordinates."""
    from PIL import Image, ImageDraw, ImageFilter

    options = shadow_options(render)
    geometry = presentation_geometry(render, record)
    result = Image.new("RGBA", tuple(geometry["size"]), (0, 0, 0, 0))
    if options is None:
        return result
    left, top, right, bottom = geometry["box"]
    padding = geometry["padding"]
    dx, dy = options["offset_px"]
    rectangle = (round(padding + left + dx), round(padding + top + dy),
                 round(padding + right - 1 + dx), round(padding + bottom - 1 + dy))
    mask = Image.new("L", result.size, 0)
    try:
        ImageDraw.Draw(mask).rectangle(rectangle, fill=round(options["opacity"] * 255))
        if options["blur_px"]:
            blurred = mask.filter(ImageFilter.GaussianBlur(options["blur_px"]))
            mask.close()
            mask = blurred
        ImageDraw.Draw(mask).rectangle(
            (padding + left, padding + top, padding + right - 1, padding + bottom - 1),
            fill=0)
        color = background_rgba(options["color"])
        solid = Image.new("RGBA", result.size, color)
        try:
            solid.putalpha(mask)
            result.close()
            result = solid
            solid = None
        finally:
            if solid is not None:
                solid.close()
    finally:
        mask.close()
    return result


def decorate(image, render: dict, record: dict,
             background: tuple[int, int, int, int]):
    """Add a chip shadow without changing pixels inside the layout bounds."""
    from PIL import Image

    options = shadow_options(render)
    resolution = _resolution(record)
    if image.size != tuple(resolution):
        raise ProjectError("source image size must match record resolution")
    if options is None:
        return image.copy()
    geometry = presentation_geometry(render, record)
    canvas = Image.new("RGBA", tuple(geometry["size"]), background)
    shadow = shadow_image(render, record)
    try:
        canvas.alpha_composite(shadow)
    finally:
        shadow.close()
    left, top, right, bottom = geometry["box"]
    padding = geometry["padding"]
    crop = image.crop((left, top, right, bottom))
    try:
        canvas.paste(crop, (padding + left, padding + top))
    finally:
        crop.close()
    return canvas
