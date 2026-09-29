# Copyright 2026 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

"""Palette resolution for render and map composition."""

from __future__ import annotations

import colorsys
import math

from .project import ProjectError


def background_rgba(value: object) -> tuple[int, int, int, int]:
    """Resolve a Pillow color or the transparent background sentinel."""
    from PIL import ImageColor

    if value == "transparent":
        return (0, 0, 0, 0)
    try:
        return ImageColor.getcolor(value, "RGBA")
    except (TypeError, ValueError, AttributeError) as exc:
        raise ProjectError(f"invalid palette background color: {value!r}") from exc


def _number(value: object, label: str, minimum: float | None = None,
            maximum: float | None = None) -> float:
    try:
        number = float(value) if not isinstance(value, bool) else float("nan")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ProjectError(f"{label} must be a finite number") from exc
    if not math.isfinite(number) or (minimum is not None and number < minimum) or (
            maximum is not None and number > maximum):
        bounds = " between 0 and 1" if minimum == 0 and maximum == 1 else ""
        raise ProjectError(f"{label} must be a finite number{bounds}")
    return number


def _levels(value: object, label: str) -> list[float]:
    values = value if isinstance(value, list) else [value]
    if not values:
        raise ProjectError(f"{label} must not be empty")
    return [_number(item, label, 0, 1) for item in values]


def _rgb(value: object, label: str) -> str:
    from PIL import ImageColor

    try:
        color = ImageColor.getrgb(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ProjectError(f"{label} is not a valid color: {value!r}") from exc
    return "#%02x%02x%02x" % color[:3]


def _rotate(color: str, degrees: float) -> str:
    from PIL import ImageColor

    r, g, b = (channel / 255 for channel in ImageColor.getrgb(color)[:3])
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    rotated = colorsys.hls_to_rgb((h + degrees / 360) % 1, l, s)
    return "#%02x%02x%02x" % tuple(round(channel * 255) for channel in rotated)


def palette(config: dict, section: dict, selected: list[tuple[str, int, int]],
            routing: list[str] | None = None) -> dict:
    """Resolve colors against the complete routing order, not the selected subset."""
    palettes = config.get("palettes", {})
    name = section.get("palette", "")
    if name and name not in palettes:
        raise ProjectError(f"unknown palette {name!r}")
    base = palettes.get(name, {})
    if not isinstance(base, dict):
        raise ProjectError(f"[palettes.{name}] must be a table")
    background = base.get("background", "#ffffff")
    background_rgba(background)
    explicit = base.get("layers", {})
    overrides = section.get("colors", {})
    if not isinstance(explicit, dict) or not isinstance(overrides, dict):
        raise ProjectError("palette layers and section colors must be tables")
    order = routing if routing is not None else config.get("technology", {}).get("routing")
    if order is None:
        order = [item[0] for item in selected]
    if not isinstance(order, list) or any(not isinstance(item, str) for item in order):
        raise ProjectError("palette routing must be a list of layer names")
    routing_order = list(dict.fromkeys(order))
    order = list(dict.fromkeys([*routing_order, *(item[0] for item in selected)]))
    generate = base.get("generate")
    generated = {}
    if generate is not None:
        if not isinstance(generate, dict):
            raise ProjectError(f"[palettes.{name}.generate] must be a table")
        start = _number(generate.get("hue_start_deg", 0), "hue_start_deg")
        saturation = _number(generate.get("saturation", .75), "saturation", 0, 1)
        lightness = _levels(generate.get("lightness", [.35, .55, .7]), "lightness")
        alphas = _levels(generate.get("alpha", [.8, .2]), "alpha")
        count = len(routing_order) or len(order)
        for index, layer in enumerate(order):
            hue = ((start + 360 * index / count) / 360) % 1
            rgb = colorsys.hls_to_rgb(hue, lightness[index % len(lightness)], saturation)
            alpha = (alphas[0] if len(alphas) == 1 or count == 1 else
                     alphas[0] + (alphas[-1] - alphas[0]) * min(index, count - 1) / (count - 1))
            generated[layer] = {"color": "#%02x%02x%02x" % tuple(
                round(channel * 255) for channel in rgb), "alpha": alpha}
    rotation = _number(base.get("hue_rotation_deg", 0), "hue_rotation_deg")
    result = {"background": background}
    for layer, _, _ in selected:
        value = {"color": "#000000", "alpha": 1.0}
        value.update(generated.get(layer, {}))
        configured = explicit.get(layer, {})
        override = overrides.get(layer, {})
        if not isinstance(configured, dict) or not isinstance(override, dict):
            raise ProjectError(f"palette color for {layer} must be a table")
        value.update(configured)
        color = _rotate(_rgb(value["color"], f"palette color for {layer}"), rotation)
        value = {"color": color, "alpha": _number(value["alpha"], f"alpha for {layer}", 0, 1)}
        value.update(override)
        result[layer] = {"color": _rgb(value["color"], f"color for {layer}"),
                         "alpha": _number(value["alpha"], f"alpha for {layer}", 0, 1)}
    return result
