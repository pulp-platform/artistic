# Copyright 2025-2026 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
#
# Thomas Benz <tbenz@iis.ee.ethz.ch>

"""DEF placement outlines over an existing, verified render."""

from __future__ import annotations

import colorsys
import copy
import glob
import gzip
import hashlib
import json
import math
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from array import array
from collections import defaultdict
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw

from .image_outputs import jpeg_background, jpeg_image
from .palettes import background_rgba, palette
from .presentation import presentation_geometry, shadow_image, shadow_options
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


def _components(value: str) -> list[str]:
    value = value.lstrip("\\").replace(r"\[", "[").replace(r"\]", "]")
    return re.split(r"[./]", value)


def _matches(pattern: str, instance: str) -> bool:
    # Match complete hierarchy components. DEF bus indices may follow a component.
    wanted = _components(pattern)
    actual = _components(instance)
    return any(all(a == p or a.startswith(p + "[") for a, p in zip(actual[i:], wanted))
               for i in range(len(actual) - len(wanted) + 1))


def _def_placements(path: Path, lef_sizes: dict[str, tuple[float, float]]) -> list:
    if path.suffix.lower() == ".gz":
        with gzip.open(path, "rt") as source:
            data = source.read()
    else:
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
    placements = []
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
        placements.append((name, box))
    return placements


def _group_placements(placements: list, modules: dict, rooted: set | None = None) -> dict:
    groups = defaultdict(list)
    prefixes = {pattern: _components(pattern) for pattern in rooted or ()}
    for name, box in placements:
        actual = _components(name)
        for pattern in modules:
            matches = (actual[:len(prefixes[pattern])] == prefixes[pattern]
                       if pattern in prefixes else _matches(pattern, name))
            if matches:
                groups[pattern].append(box)
    return groups


def _offset_placements(placements: list, offset: list[float]) -> list:
    dx, dy = offset
    return [(name, (box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy))
            for name, box in placements]


def _def_groups(path: Path, modules: dict, lef_sizes: dict[str, tuple[float, float]]
                ) -> dict[str, list[tuple[float, float, float, float]]]:
    return _group_placements(_def_placements(path, lef_sizes), modules)


def _hierarchy_modules(placements: list, hierarchy: dict, explicit: dict) -> tuple[dict, set]:
    top = _components(hierarchy["top_instance"])
    prefixes = set()
    found_top = False
    for name, _ in placements:
        parts = _components(name)
        if parts[:len(top)] != top or len(parts) <= len(top):
            continue
        found_top = True
        # The final DEF component is a placed leaf, not a hierarchy instance.
        for depth in range(hierarchy.get("min_depth", 1),
                           min(hierarchy.get("max_depth", 1), len(parts) - len(top) - 1) + 1):
            if len(top) + depth < len(parts):
                prefixes.add("/".join(parts[:len(top) + depth]))
    if not found_top:
        raise ProjectError(f"outline hierarchy top_instance not found: {hierarchy['top_instance']}")
    modules, overridden = {}, set()
    for prefix in sorted(prefixes):
        hue = int.from_bytes(hashlib.sha256(prefix.encode()).digest()[:4], "big") / 2**32
        color = "#" + "".join(f"{round(channel * 255):02x}" for channel in
                              colorsys.hls_to_rgb(hue, .48, .65))
        modules[prefix] = {"label": prefix.removeprefix("/".join(top) + "/"), "color": color}
        for pattern, spec in explicit.items():
            if _matches(pattern, prefix):
                modules[prefix] = spec
                overridden.add(pattern)
    modules.update({pattern: spec for pattern, spec in explicit.items() if pattern not in overridden})
    return modules, prefixes


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
    settings = dict(settings)
    if settings.get("enabled", True) is False:
        raise ProjectError("[render.outlines] is disabled")
    if not settings.get("def"):
        raise ProjectError("[render.outlines].def is required")
    offset = settings.get("offset_um", [0, 0])
    valid_offset = isinstance(offset, list) and len(offset) == 2
    if valid_offset:
        valid_offset = all(type(value) in (int, float) for value in offset)
    if valid_offset:
        try:
            valid_offset = all(math.isfinite(float(value)) for value in offset)
        except OverflowError:
            valid_offset = False
    if not valid_offset:
        raise ProjectError("[render.outlines].offset_um must be a pair of finite numbers")
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
    modules = settings.get("modules", {})
    hierarchy = settings.get("hierarchy")
    if not isinstance(modules, dict) or (not modules and hierarchy is None):
        raise ProjectError("configure [render.outlines.modules] or [render.outlines.hierarchy]")
    if hierarchy is not None:
        if not isinstance(hierarchy, dict):
            raise ProjectError("[render.outlines.hierarchy] must be a table")
        top = hierarchy.get("top_instance")
        if (not isinstance(top, str) or not top or
                any(not part or re.search(r"[\s*?]", part) for part in _components(top))):
            raise ProjectError("[render.outlines.hierarchy].top_instance must be a hierarchy prefix")
        minimum, maximum = hierarchy.get("min_depth", 1), hierarchy.get("max_depth", 1)
        if type(minimum) is not int or type(maximum) is not int or not 1 <= minimum <= maximum:
            raise ProjectError("outline hierarchy depths must be positive integers with min_depth <= max_depth")
    if "background" in settings:
        background_rgba(settings["background"])
    for name in ("stroke_border_width", "label_border_width"):
        value = settings.get(name, 0)
        try:
            valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
        except OverflowError:
            valid = False
        if not valid:
            raise ProjectError(f"[render.outlines].{name} must be a finite nonnegative number")
    for name in ("stroke_border_color", "label_border_color", "label_color"):
        value = settings.get(name, None if name == "label_color" else "#000000")
        if value is None and name == "label_color":
            continue
        try:
            rgba = ImageColor.getcolor(value, "RGBA") if isinstance(value, str) else None
        except (ValueError, TypeError, AttributeError) as exc:
            raise ProjectError(f"[render.outlines].{name} must be an opaque color") from exc
        if rgba is None or rgba[3] != 255:
            raise ProjectError(f"[render.outlines].{name} must be an opaque color")
        settings[name] = "#%02x%02x%02x" % rgba[:3]
    if settings.get("font_weight", "normal") not in ("normal", "bold"):
        raise ProjectError("[render.outlines].font_weight must be normal or bold")
    if type(settings.get("avoid_label_overlap", False)) is not bool:
        raise ProjectError("[render.outlines].avoid_label_overlap must be a boolean")
    for pattern, spec in modules.items():
        if not isinstance(pattern, str) or not pattern or not isinstance(spec, dict):
            raise ProjectError("invalid [render.outlines.modules] entry")
        label = spec.get("label")
        if (not isinstance(label, str) or not label.strip() or
                any(ord(char) < 32 for char in label)):
            raise ProjectError(f"outline label for {pattern} must be nonempty text without control characters")
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
    if settings.get("avoid_label_overlap", False) and resolution > 512:
        raise ProjectError("avoid_label_overlap requires outline resolution at most 512; "
                           "reduce resolution or disable overlap avoidance")
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


def _trace_raster(mask: Image.Image, path: Path, min_area_pixels: int) -> Path:
    bitmap = path.with_suffix(".bmp")
    mask.save(bitmap)
    try:
        # PGM uses the same traced curves and hole semantics as the SVG backend.
        sx, sy = (max(1.0, min(4.0, 2048 / extent)) for extent in mask.size)
        run_checked([tool("potrace"), str(bitmap), "-b", "pgm", "-t", str(min_area_pixels),
                     "-x", f"{sx}x{sy}",
                     "-o", str(path.with_suffix(".pgm"))])
    except subprocess.CalledProcessError as exc:
        raise ProjectError(f"potrace failed for {bitmap.name}") from exc
    return path.with_suffix(".pgm")


def _trace(mask: Image.Image, path: Path, min_area_pixels: int) -> ET.Element:
    bitmap = path.with_suffix(".bmp")
    vector = path.with_suffix(".svg")
    mask.save(bitmap)
    try:
        run_checked([tool("potrace"), str(bitmap), "-s", "-t", str(min_area_pixels),
                     "-o", str(vector)])
    except subprocess.CalledProcessError as exc:
        raise ProjectError(f"potrace failed for {bitmap.name}") from exc
    if max(mask.size) <= 512:
        _trace_raster(mask, path, min_area_pixels)
    return ET.parse(vector).getroot()


def _component_masks(mask: Image.Image):
    """Yield isolated source components with holes and white tracing context."""
    width, height = mask.size
    with mask.convert("L") as grayscale:
        pixels = bytearray(grayscale.tobytes())
    for seed in range(len(pixels)):
        if pixels[seed] != 0:
            continue
        pending, runs = array("I", [seed]), array("I")
        left, top, right, bottom = width, height, 0, 0
        while pending:
            index = pending.pop()
            if pixels[index] != 0:
                continue
            row = index // width
            start, end = index, index + 1
            while start > row * width and pixels[start - 1] == 0:
                start -= 1
            while end < (row + 1) * width and pixels[end] == 0:
                end += 1
            pixels[start:end] = b"\xff" * (end - start)
            runs.extend((start, end))
            left, right = min(left, start % width), max(right, (end - 1) % width + 1)
            top, bottom = min(top, row), max(bottom, row + 1)
            # Include diagonal neighbors so Potrace decides ambiguous corner connectivity.
            for neighbor_row in (row - 1, row + 1):
                if not 0 <= neighbor_row < height:
                    continue
                cursor = neighbor_row * width + max(0, start % width - 1)
                limit = neighbor_row * width + min(width, (end - 1) % width + 2)
                while cursor < limit:
                    if pixels[cursor] == 0:
                        pending.append(cursor)
                        while cursor < limit and pixels[cursor] == 0:
                            cursor += 1
                    else:
                        cursor += 1
        padding = 2
        crop = Image.new("1", (right - left + 2 * padding, bottom - top + 2 * padding), 1)
        draw = ImageDraw.Draw(crop)
        for i in range(0, len(runs), 2):
            row, x0 = divmod(runs[i], width)
            x1 = (runs[i + 1] - 1) % width
            draw.line((x0 - left + padding, row - top + padding,
                       x1 - left + padding, row - top + padding), fill=0)
        yield crop, (left - padding, top - padding)


def _trace_anchors(mask: Image.Image, path: Path, min_area_pixels: int) -> list:
    if max(mask.size) <= 512:
        with _pixel_limit(mask.width * mask.height * 16):
            with Image.open(path.with_suffix(".pgm")) as filled:
                return [(x * mask.width / filled.width, y * mask.height / filled.height)
                        for x, y in _region_anchors(filled)]
    anchors = []
    for index, (crop, (ox, oy)) in enumerate(_component_masks(mask)):
        try:
            raster = _trace_raster(crop, path.with_name(f"{path.name}-region-{index:04d}"),
                                   min_area_pixels)
            raster_limit = (max(crop.width, min(4 * crop.width, 2048)) *
                            max(crop.height, min(4 * crop.height, 2048)))
            with _pixel_limit(raster_limit):
                with Image.open(raster) as filled:
                    anchors.extend((ox + x * crop.width / filled.width,
                                    oy + y * crop.height / filled.height)
                                   for x, y in _region_anchors(filled))
        finally:
            crop.close()
    return anchors


def _region_anchors(mask: Image.Image) -> list[tuple[float, float]]:
    """Find a high-clearance interior pixel in every connected traced region."""
    width, height = mask.size
    stride = width + 2
    count = stride * (height + 2)
    distance = array("H", [0]) * count
    pixels = mask.convert("L").tobytes()
    for y in range(height):
        for x in range(width):
            if pixels[y * width + x] < 128:
                distance[(y + 1) * stride + x + 1] = 65535
    # A 3-4 chamfer transform approximates Euclidean boundary clearance, including holes.
    for y in range(1, height + 1):
        for x in range(1, width + 1):
            i = y * stride + x
            if distance[i]:
                distance[i] = min(distance[i], distance[i - 1] + 3,
                                  distance[i - stride] + 3, distance[i - stride - 1] + 4,
                                  distance[i - stride + 1] + 4)
    for y in range(height, 0, -1):
        for x in range(width, 0, -1):
            i = y * stride + x
            if distance[i]:
                distance[i] = min(distance[i], distance[i + 1] + 3,
                                  distance[i + stride] + 3, distance[i + stride - 1] + 4,
                                  distance[i + stride + 1] + 4)
    visited, anchors = bytearray(count), []
    for y in range(1, height + 1):
        for x in range(1, width + 1):
            start = y * stride + x
            if not distance[start] or visited[start]:
                continue
            queue = array("I", [start])
            visited[start] = 1
            best, candidates, sx, sy = 0, [], 0, 0
            for i in queue:
                px, py = i % stride, i // stride
                sx, sy = sx + px, sy + py
                if distance[i] > best:
                    best, candidates = distance[i], [i]
                elif distance[i] == best:
                    candidates.append(i)
                for neighbor in (i - 1, i + 1, i - stride, i + stride):
                    if distance[neighbor] and not visited[neighbor]:
                        visited[neighbor] = 1
                        queue.append(neighbor)
            cx, cy = sx / len(queue), sy / len(queue)
            selected = min(candidates, key=lambda i: ((i % stride - cx)**2 +
                                                      (i // stride - cy)**2, i))
            anchors.append((selected % stride - .5, selected // stride - .5))
    return anchors


def _transform_scale(transform: str) -> float:
    """Measure the combined scale of Potrace transforms."""
    scale, end = 1.0, 0
    for match in re.finditer(r"(translate|scale)\s*\(([^)]*)\)", transform):
        if transform[end:match.start()].strip(" ,\t\n"):
            raise ProjectError(f"unsupported outline transform: {transform}")
        try:
            values = [float(v) for v in re.split(r"[\s,]+", match[2].strip())]
        except ValueError as exc:
            raise ProjectError(f"invalid outline transform: {transform}") from exc
        if len(values) not in (1, 2) or not all(math.isfinite(v) for v in values):
            raise ProjectError(f"invalid outline transform: {transform}")
        if match[1] == "scale":
            sx, sy = values[0], values[-1]
            if not sx or not sy:
                raise ProjectError(f"zero outline scale is unsupported: {transform}")
            # Raster rounding can give slightly different x/y scales.
            scale *= math.sqrt(abs(sx * sy))
        end = match.end()
    if transform[end:].strip(" ,\t\n") or not math.isfinite(scale) or not scale:
        raise ProjectError(f"unsupported outline transform: {transform}")
    return scale


def _style_paths(node: ET.Element, color: str, stroke: float, border: float,
                 border_color: str, scale: float = 1) -> None:
    scale *= _transform_scale(node.get("transform", ""))
    for child in list(node):
        if child.tag == f"{{{SVG}}}path":
            path_scale = scale * _transform_scale(child.get("transform", ""))
            child.set("fill", "none")
            child.set("stroke", color)
            child.set("stroke-width", str(stroke / path_scale))
            child.set("stroke-linejoin", "round")
            child.set("stroke-linecap", "round")
            child.attrib.pop("vector-effect", None)
            if border:
                under = copy.deepcopy(child)
                under.attrib.pop("id", None)
                under.set("stroke", border_color)
                under.set("stroke-width", str((stroke + 2 * border) / path_scale))
                node.insert(list(node).index(child), under)
        else:
            _style_paths(child, color, stroke, border, border_color, scale)


def _anchor_component(mask: Image.Image, x: float, y: float,
                      width: int, height: int) -> Image.Image:
    """Retain only the anchor's traced region, including its holes."""
    region = mask.convert("L")
    seed = (min(region.width - 1, max(0, int(x * region.width / width))),
            min(region.height - 1, max(0, int(y * region.height / height))))
    if region.getpixel(seed) >= 128:
        region.close()
        raise ProjectError("outline label anchor is outside its traced region")
    binary = region.point(lambda v: 0 if v < 128 else 255)
    region.close()
    region = binary
    ImageDraw.floodfill(region, seed, 128)
    component = region.point(lambda v: 255 if v == 128 else 0)
    region.close()
    return component


def _query_label_bounds(svg: Path, identifiers: list[str]) -> dict:
    try:
        result = subprocess.run([tool("inkscape"), str(svg), "--query-all"],
                                check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ProjectError("Inkscape could not measure outline labels") from exc
    bounds = {}
    for line in result.stdout.splitlines():
        fields = line.split(",")
        if fields[0] not in identifiers:
            continue
        try:
            values = tuple(float(v) for v in fields[1:])
        except ValueError as exc:
            raise ProjectError("Inkscape returned invalid outline label bounds") from exc
        if (len(values) != 4 or not all(math.isfinite(v) for v in values) or
                values[2] <= 0 or values[3] <= 0):
            raise ProjectError("Inkscape returned invalid outline label bounds")
        bounds[fields[0]] = values
    if set(bounds) != set(identifiers):
        raise ProjectError("Inkscape did not return bounds for every outline label")
    return bounds


def _place_labels(root: ET.Element, svg: Path, labels: list, resolution: list[int],
                  border: float) -> None:
    identifiers = [f"label-halo-{i}" if border else f"label-{i}" for i in range(len(labels))]
    bounds = _query_label_bounds(svg, identifiers)
    width, height = resolution
    accepted = []
    radius = max(width, height) / 10
    step = max(1, 4 * max(width, height) / 1000)
    offsets = [(0., 0.)]
    offsets.extend((dx * step, dy * step)
                   for dy in range(-math.ceil(radius / step), math.ceil(radius / step) + 1)
                   for dx in range(-math.ceil(radius / step), math.ceil(radius / step) + 1)
                   if 0 < (dx * step)**2 + (dy * step)**2 <= radius**2)
    offsets.sort(key=lambda p: (p[0]**2 + p[1]**2, p[1], p[0]))
    texts = root.findall(f"{{{SVG}}}text")
    for i, (_, x, y, component) in enumerate(labels):
        bx, by, bw, bh = bounds[identifiers[i]]
        for dx, dy in offsets:
            nx, ny = x + dx, y + dy
            if not 0 <= nx < width or not 0 <= ny < height:
                continue
            if not component.getpixel((int(nx * component.width / width),
                                       int(ny * component.height / height))):
                continue
            box = (bx + dx, by + dy, bx + dx + bw, by + dy + bh)
            if box[0] < 2 or box[1] < 2 or box[2] > width - 2 or box[3] > height - 2:
                continue
            if any(not (box[2] + 2 <= other[0] or other[2] + 2 <= box[0] or
                        box[3] + 2 <= other[1] or other[3] + 2 <= box[1]) for other in accepted):
                continue
            texts[i].set("x", str(nx))
            texts[i].set("y", str(ny))
            accepted.append(box)
            break
        else:
            raise ProjectError(f"cannot place outline label {texts[i].text!r} without overlap "
                               "inside its traced region; reduce font_size or label_border_width")


def _add_chip_shadow(root: ET.Element, config: dict, record: dict,
                     temporary: Path) -> list[int]:
    render = config.get("render", {})
    geometry = presentation_geometry(render, record)
    if shadow_options(render) is None:
        return geometry["size"]
    padding = geometry["padding"]
    width, height = geometry["size"]
    root.set("width", str(width))
    root.set("height", str(height))
    root.set("viewBox", f"{-padding} {-padding} {width} {height}")
    background = root.find(f"{{{SVG}}}rect")
    background.attrib.update(x=str(-padding), y=str(-padding),
                             width=str(width), height=str(height))
    image = root.find(f"{{{SVG}}}image")
    left, top, right, bottom = geometry["box"]
    definitions = ET.SubElement(root, f"{{{SVG}}}defs")
    clip = ET.SubElement(definitions, f"{{{SVG}}}clipPath", {"id": "chip-footprint"})
    ET.SubElement(clip, f"{{{SVG}}}rect", {"x": str(left), "y": str(top),
                  "width": str(right - left), "height": str(bottom - top)})
    settings = render.get("outlines", {})
    backdrop = background_rgba(palette(config, render, [], routing=[])["background"])
    if "background" not in settings and 0 < backdrop[3] < 255:
        # Avoid blending the translucent background into the chip twice.
        outside = ET.SubElement(definitions, f"{{{SVG}}}clipPath", {"id": "chip-surround"})
        ET.SubElement(outside, f"{{{SVG}}}path", {
            "d": (f"M {-padding} {-padding} h {width} v {height} h {-width} Z "
                  f"M {left} {top} v {bottom - top} h {right - left} v {top - bottom} Z"),
            "clip-rule": "evenodd", "fill-rule": "evenodd",
        })
        background.set("fill", "#" + "".join(f"{value:02x}" for value in backdrop[:3]))
        background.set("fill-opacity", str(backdrop[3] / 255))
        background.set("clip-path", "url(#chip-surround)")
    image.set("clip-path", "url(#chip-footprint)")
    path = temporary / f"{_project_name(config)}_shadow.png"
    shadow = shadow_image(render, record)
    try:
        shadow.save(path)
    finally:
        shadow.close()
    root.insert(list(root).index(image), ET.Element(f"{{{SVG}}}image", {
        f"{{{XLINK}}}href": path.name, "x": str(-padding), "y": str(-padding),
        "width": str(width), "height": str(height),
    }))
    return geometry["size"]


def annotate(config: dict) -> list[Path]:
    """Annotate a verified composed render without re-rendering its GDS."""
    settings, def_file, lef_files, modules, formats = _options(config)
    image, resolution, viewport = _receipt(config)
    work = Path(config["design"]["work_dir"])
    record = json.loads((work / "render.json").read_text())
    output_size = presentation_geometry(config.get("render", {}), record)["size"]
    jpeg_matte = jpeg_background(config.get("render", {})) if "jpg" in formats else None
    placements = _offset_placements(_def_placements(def_file, _lef_sizes(lef_files)),
                                    settings.get("offset_um", [0, 0]))
    rooted = set()
    if settings.get("hierarchy") is not None:
        modules, rooted = _hierarchy_modules(placements, settings["hierarchy"], modules)
    groups = _group_placements(placements, modules, rooted)
    width, height = resolution
    canvas = _canvas(viewport, resolution)
    trace_limit = settings.get("resolution", 200)
    scale = min(1.0, trace_limit / max(width, height))
    trace_size = (max(1, round(width * scale)), max(1, round(height * scale)))
    root = ET.Element(f"{{{SVG}}}svg", {"version": "1.1", "width": str(width),
                      "height": str(height), "viewBox": f"0 0 {width} {height}"})
    background = settings.get("background", palette(config, config.get("render", {}),
                                                     [], routing=[])["background"])
    red, green, blue, alpha = background_rgba(background)
    if "background" not in settings and alpha < 255:
        red, green, blue, alpha = 0, 0, 0, 0
    ET.SubElement(root, f"{{{SVG}}}rect", {"width": str(width), "height": str(height),
                  "fill": f"#{red:02x}{green:02x}{blue:02x}", "fill-opacity": str(alpha / 255)})
    ET.SubElement(root, f"{{{SVG}}}image", {f"{{{XLINK}}}href": image.name,
                  "width": str(width), "height": str(height),
                  "opacity": str(settings.get("background_opacity", 1))})
    labels = []
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
            trace_path = Path(temporary) / f"module-{index:04d}"
            traced = _trace(mask, trace_path,
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
            _style_paths(outer, spec["color"], float(settings.get("stroke_width", 1)),
                         settings.get("stroke_border_width", 0),
                         settings.get("stroke_border_color", "#000000"))
            if traced.find(f".//{{{SVG}}}path") is not None and float(settings.get("font_size", 14)):
                filled = None
                if settings.get("avoid_label_overlap", False):
                    raster = trace_path.with_suffix(".pgm")
                    if not raster.is_file():
                        raster = _trace_raster(mask, trace_path, settings.get("min_area_pixels", 50))
                    with _pixel_limit(mask.width * mask.height * 16):
                        with Image.open(raster) as source:
                            filled = source.convert("L")
                    # Derive anchors and component masks from the same raster.
                    anchors = [(ax * mask.width / filled.width, ay * mask.height / filled.height)
                               for ax, ay in _region_anchors(filled)]
                else:
                    anchors = _trace_anchors(mask, trace_path, settings.get("min_area_pixels", 50))
                try:
                    for ax, ay in anchors:
                        x, y = ax * width / mask.width, ay * height / mask.height
                        component = (_anchor_component(filled, x, y, width, height)
                                     if filled is not None else None)
                        labels.append((spec, x, y, component))
                finally:
                    if filled is not None:
                        filled.close()
            mask.close()
        for index, (spec, x, y, _) in enumerate(labels):
            if float(settings.get("font_size", 14)):
                border = settings.get("label_border_width", 0)
                if border:
                    ET.SubElement(root, f"{{{SVG}}}use", {
                        "id": f"label-halo-{index}", f"{{{XLINK}}}href": f"#label-{index}",
                        "stroke": settings.get("label_border_color", "#000000"),
                        "stroke-width": str(2 * border), "stroke-linejoin": "round",
                    })
                text = ET.SubElement(root, f"{{{SVG}}}text", {
                    "id": f"label-{index}",
                    "x": str(x), "y": str(y), "text-anchor": "middle",
                    "dominant-baseline": "middle", "font-family": "sans-serif",
                    "font-weight": settings.get("font_weight", "normal"),
                    "font-size": str(settings.get("font_size", 14)),
                    "fill": settings.get("label_color") or _label_color(
                        spec["color"], float(settings.get("label_lightness", .85))),
                })
                text.text = spec["label"]
        stem = filename_component(_project_name(config) + "_modules", "outline output")
        svg = work / f"{stem}.svg"
        try:
            if settings.get("avoid_label_overlap", False) and labels:
                measurement = Path(temporary) / "labels.svg"
                measured_root = copy.deepcopy(root)
                measured_root.find(f"{{{SVG}}}image").set(f"{{{XLINK}}}href", str(image.resolve()))
                ET.ElementTree(measured_root).write(measurement, encoding="utf-8", xml_declaration=True)
                _place_labels(root, measurement, labels, resolution,
                              settings.get("label_border_width", 0))
        finally:
            for _, _, _, component in labels:
                if component is not None:
                    component.close()
        _add_chip_shadow(root, config, record, Path(temporary))
        staged_svg = Path(temporary) / svg.name
        ET.ElementTree(root).write(staged_svg, encoding="utf-8", xml_declaration=True)
        staged_shadow = Path(temporary) / f"{_project_name(config)}_shadow.png"
        if staged_shadow.is_file():
            staged_shadow.replace(work / staged_shadow.name)
        staged_svg.replace(svg)
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
                with _pixel_limit(output_size[0] * output_size[1]):
                    with Image.open(png) as rendered:
                        if rendered.size != tuple(output_size):
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
