# Copyright 2025-2026 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
#
# Thomas Benz <tbenz@iis.ee.ethz.ch>

"""DEF placement outlines over an existing, verified render."""

from __future__ import annotations

import colorsys
import glob
import json
import math
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw

from .image_outputs import jpeg_background, jpeg_image
from .project import (ProjectError, _project_name, filename_component, input_gds,
                      recorded_path, run_checked, sha256, tool, work_relative)
from .render import _pixel_limit, composition_hash, generation_hash


SVG = "http://www.w3.org/2000/svg"
XLINK = "http://www.w3.org/1999/xlink"
ET.register_namespace("", SVG)
ET.register_namespace("xlink", XLINK)


def _project_path(config: dict, value: str) -> Path:
    path = Path(value).expanduser()
    root = Path(config.get("_root", "."))
    return (path if path.is_absolute() else root / path).resolve()


def _lef_sizes(paths: list[Path]) -> dict[str, tuple[float, float]]:
    sizes = {}
    for path in paths:
        data = path.read_text()
        for match in re.finditer(r"\bMACRO\s+(\S+)(.*?)(?=\bEND\s+\1\b)",
                                 data, re.I | re.S):
            size = re.search(r"\bSIZE\s+([\d.]+)\s+BY\s+([\d.]+)\s*;",
                             match[2], re.I)
            if size:
                sizes[match[1]] = (float(size[1]), float(size[2]))
    return sizes


def _placed_box(x: float, y: float, width: float, height: float,
                orientation: str) -> tuple[float, float, float, float]:
    # DEF placement is the lower-left corner of the box after orientation.
    if orientation in ("E", "W", "FE", "FW"):
        width, height = height, width
    elif orientation not in ("N", "S", "FN", "FS"):
        raise ProjectError(f"unsupported DEF orientation: {orientation}")
    return x, y, x + width, y + height


def _matches(pattern: str, instance: str) -> bool:
    # Match complete hierarchy components. DEF bus indices may follow a component.
    def components(value: str) -> list[str]:
        value = value.lstrip("\\").replace(r"\[", "[").replace(r"\]", "]")
        return re.split(r"[./]", value)

    wanted = components(pattern)
    actual = components(instance)
    return any(all(a == p or a.startswith(p + "[") for a, p in zip(actual[i:], wanted))
               for i in range(len(actual) - len(wanted) + 1))


def _def_groups(path: Path, modules: dict, lef_sizes: dict[str, tuple[float, float]]
                ) -> dict[str, list[tuple[float, float, float, float]]]:
    data = path.read_text()
    units = re.search(r"\bUNITS\s+DISTANCE\s+MICRONS\s+(\d+)\s*;", data, re.I)
    if not units or int(units[1]) <= 0:
        raise ProjectError(f"DEF units missing or invalid: {path}")
    dbu = int(units[1])
    diearea = re.search(r"\bDIEAREA\b(.*?);", data, re.I | re.S)
    corners = (re.findall(r"\(\s*(-?\d+)\s+(-?\d+)\s*\)", diearea[1])
               if diearea else [])
    vertices = [(int(x), int(y)) for x, y in corners]
    if (len(vertices) < 2 or len({x for x, _ in vertices}) < 2 or
            len({y for _, y in vertices}) < 2):
        raise ProjectError(f"DEF DIEAREA missing or invalid: {path}")
    section = re.search(r"\bCOMPONENTS\s+\d+\s*;(.*?)\bEND\s+COMPONENTS\b",
                        data, re.I | re.S)
    if not section:
        raise ProjectError(f"DEF COMPONENTS section missing: {path}")
    groups = defaultdict(list)
    for statement in section[1].split(";"):
        component = re.search(r"(?:^|\s)-\s+(\S+)\s+(\S+)", statement)
        if not component:
            continue
        placement = re.search(r"\+\s+(?:PLACED|FIXED|COVER)\s+\(\s*(-?\d+)\s+"
                              r"(-?\d+)\s*\)\s+(\w+)", statement, re.I)
        if not placement:
            continue  # UNPLACED components have no physical position.
        name, master = component.groups()
        x, y = int(placement[1]) / dbu, int(placement[2]) / dbu
        width, height = lef_sizes.get(master, (0.0, 0.0))
        box = _placed_box(x, y, width, height, placement[3].upper())
        for pattern in modules:
            if _matches(pattern, name):
                groups[pattern].append(box)
    return groups


def _canvas(viewport: list[float], resolution: list[int]) -> tuple[float, float, float, float]:
    left, bottom, right, top = viewport
    width, height = resolution
    pitch = max((right - left) / width, (top - bottom) / height)
    cx, cy = (left + right) / 2, (bottom + top) / 2
    return cx - width * pitch / 2, cy - height * pitch / 2, pitch, pitch


def _pixel_box(box: tuple[float, float, float, float], canvas: tuple[float, ...],
               resolution: list[int]) -> tuple[float, float, float, float] | None:
    ox, oy, pitch, _ = canvas
    width, height = resolution
    raw_x0, raw_x1 = (box[0] - ox) / pitch, (box[2] - ox) / pitch
    raw_y0, raw_y1 = height - (box[3] - oy) / pitch, height - (box[1] - oy) / pitch
    if raw_x0 == raw_x1:
        raw_x0 -= .5
        raw_x1 += .5
    if raw_y0 == raw_y1:
        raw_y0 -= .5
        raw_y1 += .5
    x0, x1 = max(0.0, raw_x0), min(float(width), raw_x1)
    y0, y1 = max(0.0, raw_y0), min(float(height), raw_y1)
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1


def _options(config: dict) -> tuple[dict, Path, list[Path], dict, list[str]]:
    settings = config.get("render", {}).get("outlines")
    if not isinstance(settings, dict):
        raise ProjectError("[render.outlines] is not configured")
    if settings.get("enabled", True) is False:
        raise ProjectError("[render.outlines] is disabled")
    if not settings.get("def"):
        raise ProjectError("[render.outlines].def is required")
    def_file = _project_path(config, settings["def"])
    if not def_file.is_file():
        raise ProjectError(f"outline DEF not found: {def_file}")
    lef_files = []
    lef_patterns = settings.get("lef_files", [])
    if not isinstance(lef_patterns, list) or any(not isinstance(v, str) for v in lef_patterns):
        raise ProjectError("[render.outlines].lef_files must be a list of paths or globs")
    for value in lef_patterns:
        pattern = str(_project_path(config, value))
        matches = sorted(glob.glob(pattern, recursive=True))
        if not matches:
            raise ProjectError(f"outline LEF pattern matched no files: {value}")
        lef_files.extend(Path(match) for match in matches)
    modules = settings.get("modules")
    if not isinstance(modules, dict) or not modules:
        raise ProjectError("[render.outlines.modules] must contain at least one module")
    for pattern, spec in modules.items():
        if not isinstance(pattern, str) or not pattern or not isinstance(spec, dict):
            raise ProjectError("invalid [render.outlines.modules] entry")
        filename_component(str(spec.get("label", "")), f"outline label for {pattern}")
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(spec.get("color", ""))):
            raise ProjectError(f"outline color for {pattern} must be #RRGGBB")
    resolution = settings.get("resolution", 200)
    if type(resolution) is not int:
        raise ProjectError("[render.outlines].resolution must be a positive integer")
    min_area = settings.get("min_area_pixels", 50)
    if type(min_area) is not int or min_area < 0:
        raise ProjectError("[render.outlines].min_area_pixels must be a nonnegative integer")
    try:
        opacity = float(settings.get("background_opacity", 1))
        font_size = float(settings.get("font_size", 14))
        stroke = float(settings.get("stroke_width", 1))
        lightness = float(settings.get("label_lightness", .85))
    except (TypeError, ValueError) as exc:
        raise ProjectError("invalid numeric [render.outlines] setting") from exc
    if (resolution <= 0 or resolution > 10000 or
            not all(math.isfinite(v) for v in (opacity, font_size, stroke, lightness)) or
            not 0 <= opacity <= 1 or font_size < 0 or stroke <= 0 or not 0 <= lightness <= 1):
        raise ProjectError("[render.outlines] numeric settings are out of range")
    formats = settings.get("formats", ["svg"])
    if not isinstance(formats, list) or not formats or any(
            not isinstance(v, str) or v.lower() not in ("svg", "png", "pdf", "jpg")
            for v in formats):
        raise ProjectError("[render.outlines].formats must contain svg, png, pdf, or jpg")
    return settings, def_file, lef_files, modules, list(dict.fromkeys(v.lower() for v in formats))


def _receipt(config: dict) -> tuple[Path, list[int], list[float]]:
    work = Path(config["design"]["work_dir"])
    record_path, receipt_path = work / "render.json", work / "render_output.json"
    if not record_path.is_file() or not receipt_path.is_file():
        raise ProjectError("render output receipt missing; run render generate and compose first")
    try:
        record, receipt = json.loads(record_path.read_text()), json.loads(receipt_path.read_text())
        if not isinstance(record, dict) or not isinstance(receipt, dict):
            raise TypeError("render record and receipt must be JSON objects")
        if receipt.get("version") != 2:
            raise ProjectError("render output receipt is outdated; run render compose again")
        image = recorded_path(config, receipt["image"])
        source = input_gds(config, "render")
        viewport = [float(v) for v in record["gds"]["viewport_um"]]
        resolution = [int(v) for v in record["resolution"]]
        valid = (record.get("record_version") == 2 and
                 record.get("chip") == _project_name(config) and
                 image.is_relative_to(work.resolve()) and image.suffix.lower() == ".png" and
                 image.is_file() and sha256(image) == receipt.get("image_sha256") and
                 sha256(record_path) == receipt.get("render_record_sha256") and
                 receipt.get("generation_sha256") == record.get("generation_sha256") ==
                 generation_hash(config, "render") and
                 receipt.get("source_sha256") == record.get("input_sha256") == sha256(source) and
                 work_relative(config, source) == record.get("input") and
                 receipt.get("resolution") == resolution and
                 receipt.get("viewport_um") == viewport and
                 len(resolution) == 2 and min(resolution) > 0 and len(viewport) == 4 and
                 all(math.isfinite(v) for v in viewport) and
                 viewport[0] < viewport[2] and viewport[1] < viewport[3])
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ProjectError("render output receipt is invalid; compose render again") from exc
    if not valid:
        raise ProjectError("render output is stale or changed; generate and compose render again")
    if receipt.get("composition_sha256") != composition_hash(config, record):
        raise ProjectError("render composition settings changed; run render compose again")
    with _pixel_limit(resolution[0] * resolution[1]):
        with Image.open(image) as background:
            if background.size != tuple(resolution):
                raise ProjectError("render image dimensions differ from render record")
    return image, resolution, viewport


def _label_color(color: str, lightness: float) -> str:
    rgb = ImageColor.getrgb(color)
    hue, _, saturation = colorsys.rgb_to_hls(*(v / 255 for v in rgb))
    adjusted = colorsys.hls_to_rgb(hue, lightness, saturation)
    return "#" + "".join(f"{round(255 * v):02x}" for v in adjusted)


def _trace(mask: Image.Image, path: Path, min_area_pixels: int) -> ET.Element:
    bitmap = path.with_suffix(".bmp")
    vector = path.with_suffix(".svg")
    mask.save(bitmap)
    try:
        run_checked([tool("potrace"), str(bitmap), "-s", "-t", str(min_area_pixels),
                     "-o", str(vector)])
    except subprocess.CalledProcessError as exc:
        raise ProjectError(f"potrace failed for {bitmap.name}") from exc
    return ET.parse(vector).getroot()


def annotate(config: dict) -> list[Path]:
    """Annotate a verified composed render without re-rendering its GDS."""
    settings, def_file, lef_files, modules, formats = _options(config)
    image, resolution, viewport = _receipt(config)
    jpeg_matte = jpeg_background(config.get("render", {})) if "jpg" in formats else None
    groups = _def_groups(def_file, modules, _lef_sizes(lef_files))
    width, height = resolution
    canvas = _canvas(viewport, resolution)
    trace_limit = settings.get("resolution", 200)
    scale = min(1.0, trace_limit / max(width, height))
    trace_size = (max(1, round(width * scale)), max(1, round(height * scale)))
    root = ET.Element(f"{{{SVG}}}svg", {"version": "1.1", "width": str(width),
                      "height": str(height), "viewBox": f"0 0 {width} {height}"})
    ET.SubElement(root, f"{{{SVG}}}image", {f"{{{XLINK}}}href": image.name,
                  "width": str(width), "height": str(height),
                  "opacity": str(settings.get("background_opacity", 1))})
    labels = []
    work = Path(config["design"]["work_dir"])
    with tempfile.TemporaryDirectory(prefix="outline-", dir=work) as temporary:
        for index, (pattern, spec) in enumerate(modules.items()):
            boxes = [_pixel_box(box, canvas, resolution) for box in groups.get(pattern, ())]
            boxes = [box for box in boxes if box is not None]
            if not boxes:
                continue
            mask = Image.new("1", trace_size, 1)
            draw = ImageDraw.Draw(mask)
            for x0, y0, x1, y1 in boxes:
                # Unknown standard-cell sizes are represented by a pixel at placement.
                draw.rectangle((math.floor(x0 * scale), math.floor(y0 * scale),
                                max(math.floor(x0 * scale), math.ceil(x1 * scale) - 1),
                                max(math.floor(y0 * scale), math.ceil(y1 * scale) - 1)), fill=0)
            traced = _trace(mask, Path(temporary) / f"module-{index:04d}",
                            settings.get("min_area_pixels", 50))
            viewbox = [float(v) for v in traced.attrib["viewBox"].split()]
            outer = ET.SubElement(root, f"{{{SVG}}}g", {
                "transform": f"scale({width / viewbox[2]} {height / viewbox[3]})",
                "fill": "none", "stroke": spec["color"],
                "stroke-width": str(settings.get("stroke_width", 1)),
            })
            for child in traced:
                if child.tag == f"{{{SVG}}}g":
                    outer.append(child)
            for path in outer.iter(f"{{{SVG}}}path"):
                path.set("fill", "none")
                path.set("stroke", spec["color"])
                path.set("stroke-width", str(settings.get("stroke_width", 1)))
                path.set("vector-effect", "non-scaling-stroke")
            labels.append((spec, (min(b[0] for b in boxes) + max(b[2] for b in boxes)) / 2,
                           (min(b[1] for b in boxes) + max(b[3] for b in boxes)) / 2))
            mask.close()
        for spec, x, y in labels:
            if float(settings.get("font_size", 14)):
                text = ET.SubElement(root, f"{{{SVG}}}text", {
                    "x": str(x), "y": str(y), "text-anchor": "middle",
                    "dominant-baseline": "middle", "font-family": "sans-serif",
                    "font-size": str(settings.get("font_size", 14)),
                    "fill": _label_color(spec["color"], float(settings.get("label_lightness", .85))),
                })
                text.text = spec["label"]
        stem = filename_component(_project_name(config) + "_modules", "outline output")
        svg = work / f"{stem}.svg"
        ET.ElementTree(root).write(svg, encoding="utf-8", xml_declaration=True)
        outputs = []
        generated_png = False
        for fmt in formats:
            target = work / f"{stem}.{fmt}"
            if fmt == "svg":
                outputs.append(svg)
            elif fmt in ("png", "pdf"):
                if fmt != "png" or not generated_png:
                    try:
                        run_checked([tool("inkscape"), str(svg), "--export-filename", str(target)])
                    except subprocess.CalledProcessError as exc:
                        raise ProjectError(f"Inkscape failed to export {fmt}") from exc
                if fmt == "png":
                    generated_png = True
                outputs.append(target)
            else:
                png = work / f"{stem}.png"
                if not generated_png:
                    try:
                        run_checked([tool("inkscape"), str(svg), "--export-filename", str(png)])
                    except subprocess.CalledProcessError as exc:
                        raise ProjectError("Inkscape failed to export JPG source") from exc
                    generated_png = True
                with _pixel_limit(width * height):
                    with Image.open(png) as rendered:
                        if rendered.size != (width, height):
                            raise ProjectError("annotated PNG dimensions differ from render record")
                        rgba = rendered.convert("RGBA")
                        try:
                            flattened = jpeg_image(rgba, jpeg_matte)
                            try:
                                flattened.save(target, quality=95)
                            finally:
                                flattened.close()
                        finally:
                            rgba.close()
                outputs.append(target)
                if "png" not in formats:
                    png.unlink()
        return outputs
