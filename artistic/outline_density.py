# Copyright 2026 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

"""Non-overlapping module regions with shared borders."""

import math
import xml.etree.ElementTree as ET

from PIL import Image, ImageDraw

from .project import ProjectError

try:
    import numpy as np
    from scipy import ndimage as ndi
    from shapely.geometry import Point
    from shapely import __version__ as shapely_version
except ImportError as exc:
    raise ProjectError("density outlines need NumPy, SciPy and Shapely 2+") from exc

if int(shapely_version.split(".")[0]) < 2:
    raise ProjectError("density outlines need Shapely 2+")

from .outline_geometry import regions

SVG = "http://www.w3.org/2000/svg"
XLINK = "http://www.w3.org/1999/xlink"
INKSCAPE = "http://www.inkscape.org/namespaces/inkscape"
ET.register_namespace("inkscape", INKSCAPE)


def density_labels(groups, resolution, trace_size, seeds, smoothing_px, min_area, protected=None):
    """Choose the strongest smoothed occupancy, protecting macros and label seeds."""
    width, height = resolution
    tw, th = trace_size
    sx, sy = tw / width, th / height
    best = np.full((th, tw), .035)
    labels = np.zeros((th, tw), dtype=np.int32)
    hard = np.zeros((th, tw), dtype=np.int32)
    corners = set()
    for label, boxes in enumerate(groups, 1):
        mask = Image.new("L", trace_size, 0)
        draw = ImageDraw.Draw(mask)
        for (x0, y0, x1, y1), macro in boxes:
            # Sample macros at cell centers so abutting footprints do not share a cell.
            offset, edge = (.5, math.ceil) if macro else (0, math.floor)
            left = min(tw - 1, max(0, edge(x0 * sx - offset)))
            top = min(th - 1, max(0, edge(y0 * sy - offset)))
            right = min(tw, max(left + 1, math.ceil(x1 * sx - offset)))
            bottom = min(th, max(top + 1, math.ceil(y1 * sy - offset)))
            if macro:
                footprint = hard[top:bottom, left:right]
                if np.any((footprint != 0) & (footprint != label)):
                    raise ProjectError("density outlines require non-overlapping macro groups")
                footprint[:] = label
                corners.update(((left, top), (left, bottom), (right, top), (right, bottom)))
                if protected is not None:
                    protected.append((label, left, top, right, bottom))
            else:
                draw.rectangle((left, top, right - 1, bottom - 1), fill=255)
        field = ndi.gaussian_filter(np.asarray(mask, dtype=float) / 255,
                                    (smoothing_px * sy, smoothing_px * sx), mode="constant")
        stronger = field > best
        best[stronger], labels[stronger] = field[stronger], label
    yy, xx = np.ogrid[:th, :tw]
    for label, x, y in seeds:
        ix, iy = min(tw - 1, max(0, int(x * sx))), min(th - 1, max(0, int(y * sy)))
        disk = (xx - ix)**2 + (yy - iy)**2 <= max(1, round(5 * min(sx, sy)))**2
        if np.any((hard[disk] != 0) & (hard[disk] != label)):
            raise ProjectError("density outline label seeds overlap another group; "
                               "use separate module groups or method = 'placement'")
        hard[disk] = label
    labels[hard != 0] = hard[hard != 0]
    remove = np.zeros(labels.shape, dtype=bool)
    for label in range(1, len(groups) + 1):
        components, _ = ndi.label(labels == label)
        areas = np.bincount(components.ravel())
        seeded_components = np.unique(components[hard == label])
        small = np.flatnonzero(areas < min_area)
        small = small[(small != 0) & ~np.isin(small, seeded_components)]
        remove |= np.isin(components, small)
    if remove.any():
        nearest = ndi.distance_transform_edt(remove, return_distances=False, return_indices=True)
        labels[remove] = labels[tuple(nearest[:, remove])]
    return labels, corners


def _layer(root, name):
    return ET.SubElement(root, f"{{{SVG}}}g", {
        "id": name.replace(" ", "-"), f"{{{INKSCAPE}}}groupmode": "layer",
        f"{{{INKSCAPE}}}label": name})


def annotate_regions(root, groups, modules, resolution, trace_size, seeds, settings):
    protected = []
    labels, corners = density_labels(groups, resolution, trace_size, seeds,
                                     settings.get("smoothing_px", 12),
                                     settings.get("min_area_pixels", 50), protected=protected)
    width, height = resolution
    tw, th = trace_size
    sx, sy = tw / width, th / height
    shapes = regions(labels, corners=corners, radius=20 * min(sx, sy),
                     tolerance=4.6875 * min(sx, sy), output_scale=(1 / sx, 1 / sy),
                     protected=protected)
    defs = ET.SubElement(root, f"{{{SVG}}}defs")
    fills = _layer(root, "Region fills")
    borders = _layer(root, "Modules")
    border = settings.get("stroke_border_width", 0)
    if border:
        filter_node = ET.SubElement(defs, f"{{{SVG}}}filter", {
            "id": "module-border", "filterUnits": "userSpaceOnUse",
            "x": str(-border - 1), "y": str(-border - 1),
            "width": str(width + 2 * border + 2), "height": str(height + 2 * border + 2),
            "color-interpolation-filters": "sRGB"})
        ET.SubElement(filter_node, f"{{{SVG}}}feMorphology", {
            "in": "SourceAlpha", "operator": "dilate", "radius": str(border), "result": "expanded"})
        ET.SubElement(filter_node, f"{{{SVG}}}feFlood", {
            "flood-color": settings.get("stroke_border_color", "#000000"), "result": "border"})
        ET.SubElement(filter_node, f"{{{SVG}}}feComposite", {
            "in": "border", "in2": "expanded", "operator": "in", "result": "halo"})
        merge = ET.SubElement(filter_node, f"{{{SVG}}}feMerge")
        for source in ("halo", "SourceGraphic"):
            ET.SubElement(merge, f"{{{SVG}}}feMergeNode", {"in": source})
        borders.set("filter", "url(#module-border)")
    specs = list(modules.values())
    median_area = np.median([shape["polygon"].area for shape in shapes]) if shapes else 1
    module_groups = {}
    for label, (pattern, spec) in enumerate(modules.items(), 1):
        module_groups[label] = ET.SubElement(borders, f"{{{SVG}}}g", {
            "id": f"module-{label}", f"{{{INKSCAPE}}}label": spec["label"], "data-instance": pattern})
    entries = []
    for index, shape in enumerate(shapes):
        label, polygon = shape["label"], shape["polygon"]
        # Area has only a mild influence; keep small-region labels readable.
        font_scale = min(1.15, max(.85, (polygon.area / median_area)**.1))
        spec = dict(specs[label - 1], font_size=float(settings.get("font_size", 14)) * font_scale)
        path_id = f"module-{label}-region-{index}"
        clip_id = path_id + "-clip"
        clip = ET.SubElement(defs, f"{{{SVG}}}clipPath", {
            "id": clip_id, "clipPathUnits": "userSpaceOnUse"})
        ET.SubElement(clip, f"{{{SVG}}}use", {f"{{{XLINK}}}href": "#" + path_id})
        group = ET.SubElement(module_groups[label], f"{{{SVG}}}g", {
            "stroke": spec["color"], "stroke-width": str(2 * float(settings.get("stroke_width", 1))),
            "stroke-linejoin": "round", "stroke-linecap": "round", "fill": "none",
            "fill-rule": "evenodd", "clip-path": f"url(#{clip_id})"})
        ET.SubElement(group, f"{{{SVG}}}path", {
            "id": path_id, "d": shape["path"], "clip-rule": "evenodd"})
        ET.SubElement(fills, f"{{{SVG}}}use", {
            f"{{{XLINK}}}href": "#" + path_id, "fill": spec["color"],
            "fill-opacity": str(settings.get("fill_opacity", 0)),
            "fill-rule": "evenodd", "stroke": "none"})
        if not float(settings.get("font_size", 14)):
            continue
        anchors = [(x, y) for owner, x, y in seeds
                   if owner == label and polygon.contains(Point(x * sx, y * sy))]
        if not anchors and any(owner == label for owner, _, _ in seeds):
            continue
        if anchors and not settings.get("avoid_label_overlap", False):
            entries.extend((spec, x, y, None) for x, y in anchors)
            continue
        mask = Image.new("L", (tw * 4, th * 4), 255)
        draw = ImageDraw.Draw(mask)
        draw.polygon([(x * 4, y * 4) for x, y in polygon.exterior.coords], fill=0)
        for hole in polygon.interiors:
            draw.polygon([(x * 4, y * 4) for x, y in hole.coords], fill=255)
        if not anchors:
            inside = np.asarray(mask) < 128
            distance = ndi.distance_transform_edt(np.pad(inside, 1))[1:-1, 1:-1]
            if distance.max() == 0:
                raise ProjectError(f"density outline {spec['label']} has no label interior")
            ay, ax = np.unravel_index(np.argmax(distance), distance.shape)
            anchors = [((ax + .5) / (4 * sx), (ay + .5) / (4 * sy))]
        component = (mask.point(lambda value: 255 if value < 128 else 0)
                     if settings.get("avoid_label_overlap", False) else None)
        entries.extend((spec, x, y, component) for x, y in anchors)
    return entries
