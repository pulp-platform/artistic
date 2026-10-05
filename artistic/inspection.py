# Copyright 2026 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

"""Resolved render geometry and palette previews without raw-image generation."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from .image_outputs import pdf_geometry
from .palettes import background_rgba, palette
from .project import _project_name
from .render import _viewport, resolve_dimensions, resolve_viewport
from .technology import selected_layers


def analyze(config: dict, manifest: dict) -> dict:
    """Augment layout inspection with the resolved render and map geometry."""
    report = dict(manifest)
    for section in ("render", "map"):
        settings = config.get(section, {})
        section_layout = manifest.get("section_layouts", {}).get(section,
                                                                 manifest["layout"])
        dimensions = resolve_dimensions(settings, section)
        resolution = dimensions["resolution"]
        viewport = resolve_viewport(section_layout, settings, resolution)
        selected = selected_layers(settings, manifest["technology"], section_layout)
        metrics = {**settings, **dimensions,
                   "requested_viewport_um": _viewport(section_layout, settings),
                   "viewport_um": viewport,
                   "input_gds": manifest.get("section_gds", {}).get(section,
                                                                     manifest.get("gds", "")),
                   "geometry_source": manifest.get("geometry_source", {}).get(
                       section, "inspected"),
                   "layout_bbox_um": section_layout["bbox_um"],
                   "nm_per_px": (viewport[2] - viewport[0]) * 1000 / resolution[0],
                   "raw_nm_per_px": (viewport[2] - viewport[0]) * 1000 /
                                     dimensions["raw_resolution"][0],
                   "layers_resolved": [name for name, _, _ in selected]}
        if section == "render":
            page = pdf_geometry(resolution, settings)
            metrics.update({name: page[name] for name in ("dpi", "page_cm", "image_cm")})
        report[section] = metrics
    return report


def format_summary(report: dict) -> str:
    """Format inspected layout and resolved dimensions for terminal output."""
    layout = report["layout"]
    lines = [f"GDS: {report.get('gds', '')}"]
    if "top_cell" in layout:
        lines.append(f"Top cell: {layout['top_cell']}")
    bbox = layout["bbox_um"]
    lines.append("Layout bounds (um): " + ", ".join(f"{value:g}" for value in bbox))
    for section in ("render", "map"):
        metrics = report[section]
        resolution = metrics["resolution"]
        raw = metrics["raw_resolution"]
        segments = metrics["segments"]
        lines.extend([
            f"{section.capitalize()}:",
            f"  Input GDS: {metrics['input_gds']} "
            f"({metrics['geometry_source']}; bounds: " +
            ", ".join(f"{value:g}" for value in metrics["layout_bbox_um"]) + " um)",
            "  Viewport (um): " + ", ".join(f"{value:g}" for value in metrics["viewport_um"]),
            f"  Resolution: {resolution[0]} x {resolution[1]} px; "
            f"raw: {raw[0]} x {raw[1]} px; segments: {segments[0]} x {segments[1]}",
            f"  Scale: {metrics['nm_per_px']:g} nm/px "
            f"(raw: {metrics['raw_nm_per_px']:g} nm/px)",
            "  Layers: " + (", ".join(metrics["layers_resolved"]) or "none"),
        ])
        if section == "render":
            width, height = metrics["page_cm"]
            lines.append(f"  PDF page: {width:g} x {height:g} cm; {metrics['dpi']:g} dpi")
    if report.get("palette_preview"):
        lines.append(f"Palette preview: {report['palette_preview']}")
    return "\n".join(lines)


def write_palette_preview(config: dict, manifest: dict) -> Path:
    """Write selected render and map colors and alpha values as an SVG legend."""
    namespace = "http://www.w3.org/2000/svg"
    ET.register_namespace("", namespace)

    def node(parent, tag, **attributes):
        return ET.SubElement(parent, f"{{{namespace}}}{tag}",
                             {name.replace("_", "-"): str(value)
                              for name, value in attributes.items()})

    sections = []
    for section in ("render", "map"):
        settings = config.get(section, {})
        section_layout = manifest.get("section_layouts", {}).get(section,
                                                                 manifest["layout"])
        selected = selected_layers(settings, manifest["technology"], section_layout)
        colors = palette(config, settings, selected, routing=manifest["technology"]["routing"])
        mask = section == "map" and settings.get("layer_style", "mask") == "mask"
        background = (255, 255, 255, 255) if mask else background_rgba(colors["background"])
        entries = [("background", "#%02x%02x%02x" % background[:3], background[3] / 255)]
        if mask:
            entries.extend((name, "#000000", 1) for name, _, _ in selected)
        else:
            entries.extend((name, colors[name]["color"], colors[name]["alpha"])
                           for name, _, _ in selected)
        title = ("mask layers (black on white)" if mask else
                 f"palette {settings.get('palette', 'default')}")
        sections.append((section, title, entries))
    height = 24 + sum(42 + len(entries) * 32 for _, _, entries in sections)
    svg = ET.Element(f"{{{namespace}}}svg", {"width": "640", "height": str(height),
                                           "viewBox": f"0 0 640 {height}"})
    node(svg, "title").text = f"{_project_name(config)} palettes"
    defs = node(svg, "defs")
    checker = node(defs, "pattern", id="checker", width=12, height=12,
                   patternUnits="userSpaceOnUse")
    node(checker, "rect", width=12, height=12, fill="#ffffff")
    node(checker, "path", d="M0 0h6v6H0zM6 6h6v6H6z", fill="#d8d8d8")
    node(svg, "rect", width=640, height=height, fill="#ffffff")
    y = 24
    for section, palette_name, entries in sections:
        node(svg, "text", x=16, y=y, font_family="sans-serif", font_size=17,
             fill="#111111").text = f"{section.capitalize()} / {palette_name}"
        y += 18
        for name, color, alpha in entries:
            group = node(svg, "g", **{"data-section": section, "data-layer": name})
            node(group, "rect", x=16, y=y, width=88, height=24, fill="url(#checker)")
            node(group, "rect", x=16, y=y, width=88, height=24,
                 fill=color, fill_opacity=f"{alpha:.10g}")
            node(group, "text", x=120, y=y + 17, font_family="sans-serif", font_size=14,
                 fill="#111111").text = f"{name}  {color}  alpha={alpha:g}"
            y += 32
        y += 24
    target = Path(config["design"]["work_dir"]) / f"{_project_name(config)}_palette.svg"
    target.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(svg).write(target, encoding="utf-8", xml_declaration=True)
    return target
