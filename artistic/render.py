# Copyright 2026 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

"""Layer-mask generation and host-side render composition."""

from __future__ import annotations

import json
import math
import re
import hashlib
import tempfile
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path

from .project import (ProjectError, _project_name, filename_component, input_gds,
                      project_relative, recorded_path, sha256, work_relative, write_json)
from .technology import inspect_layout, palette, selected_layers, run_pya


def generation_hash(config: dict, section: str) -> str:
    settings = config.get(section, {})
    technology = config.get("technology", {})
    input_value = settings.get("input", "design")
    if input_value == "design":
        identity = ["design", project_relative(config, Path(config["design"]["gds"]))] \
            if "design" in config else ["design"]
    elif input_value == "logo":
        identity = ["logo"]
    else:
        identity = ["explicit", project_relative(config, Path(input_value))] \
            if "design" in config else ["explicit", str(input_value)]
    tech_file = technology.get("file")
    if tech_file and Path(tech_file).is_absolute() and "design" in config:
        tech_file = project_relative(config, Path(tech_file))
    value = {
        "input": identity,
        "resolution": [int(item) for item in settings.get("resolution", [1000, 1000])],
        "segments": [int(item) for item in settings.get("segments", [1, 1])],
        "overrender": int(settings.get("overrender", 1)),
        "margin_um": float(settings.get("margin_um", 0)),
        "viewport_um": settings.get("viewport_um"),
        "layers": settings.get("layers", "routing"),
        "technology": {"file": tech_file, **{name: technology.get(name)
                       for name in ("layers", "routing", "top_metal")}},
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def verify_raw(settings: dict) -> None:
    recorded = settings.get("raw_sha256")
    if not isinstance(recorded, dict) or not recorded:
        raise ProjectError(f"{settings['section']} raw-image hashes are missing; generate it again")
    raw_dir = Path(settings["raw_dir"])
    for name, digest in recorded.items():
        path = raw_dir / name
        if name != Path(name).name or not path.is_file() or sha256(path) != digest:
            raise ProjectError(f"{settings['section']} raw images changed; generate them again")


def preserve_logo_resolution(config: dict, manifest: dict) -> None:
    path = Path(config["design"]["work_dir"]) / "manifest.json"
    if not path.is_file():
        return
    try:
        resolved = json.loads(path.read_text()).get("logo", {}).get("resolved_layer")
    except (OSError, ValueError):
        return
    if resolved:
        manifest["logo"]["resolved_layer"] = resolved


def _viewport(layout: dict, settings: dict) -> list[float]:
    configured = settings.get("viewport_um")
    if configured is not None:
        if not isinstance(configured, list) or len(configured) != 4:
            raise ProjectError("viewport_um must contain left, bottom, right, and top")
        try:
            viewport = [float(value) for value in configured]
        except (TypeError, ValueError) as exc:
            raise ProjectError("viewport_um values must be finite numbers") from exc
        if (not all(math.isfinite(value) for value in viewport) or
                viewport[0] >= viewport[2] or viewport[1] >= viewport[3]):
            raise ProjectError("viewport_um must define a positive finite area")
        return viewport
    bbox = layout["bbox_um"]
    margin = float(settings.get("margin_um", 0))
    return [bbox[0] - margin, bbox[1] - margin, bbox[2] + margin, bbox[3] + margin]


def _settings(config: dict, manifest: dict, section: str, source: Path,
             selected: list[tuple[str, int, int]]) -> dict:
    settings = config.get(section, {})
    resolution = settings.get("resolution", [1000, 1000])
    segments = settings.get("segments", [1, 1])
    if len(resolution) != 2 or min(map(int, resolution)) <= 0:
        raise ProjectError(f"[{section}].resolution must contain two positive integers")
    if len(segments) != 2 or min(map(int, segments)) <= 0:
        raise ProjectError(f"[{section}].segments must contain two positive integers")
    overrender = int(settings.get("overrender", 1))
    if overrender <= 0:
        raise ProjectError(f"[{section}].overrender must be positive")
    if segments[0] > int(resolution[0]) * overrender or \
            segments[1] > int(resolution[1]) * overrender:
        raise ProjectError(f"[{section}].segments exceed raw pixel dimensions")
    for name, _, _ in selected:
        filename_component(name, f"[{section}].layers name")
    viewport = _viewport(manifest["layout"], settings)
    raw_dir = Path(config["design"]["work_dir"]) / "raw" / section
    technology = dict(manifest["technology"])
    if technology.get("technology"):
        technology["technology"] = work_relative(config, Path(technology["technology"]))
    return {"record_version": 2,
            "project": (work_relative(config, Path(config["_project"]))
                        if config.get("_project") else None), "section": section,
            "chip": _project_name(config),
            "input": work_relative(config, source), "input_sha256": sha256(source),
            "gds": {"file": work_relative(config, source), "viewport_um": viewport},
            "resolution": [int(value) for value in resolution],
            "segments": [int(value) for value in segments], "overrender": overrender,
            "layers": [{"name": name, "layer": layer, "datatype": datatype}
                       for name, layer, datatype in selected],
            "raw_dir": work_relative(config, raw_dir),
            "technology": technology, "layout": manifest["layout"],
            "generation_sha256": generation_hash(config, section)}


def generate(config: dict, technology: str | Path | None = None,
             section: str = "render") -> Path:
    source = input_gds(config, section)
    manifest = inspect_layout(config, technology, source)
    selected = selected_layers(config.get(section, {}), manifest["technology"], manifest["layout"])
    if not selected:
        raise ProjectError(f"no configured {section} layers are present in the GDS")
    settings = _settings(config, manifest, section, source, selected)
    raw = recorded_path(config, settings["raw_dir"]); raw.mkdir(parents=True, exist_ok=True)
    for stale in raw.glob(f"RAW__{_project_name(config)}_*.png"):
        stale.unlink()
    run_pya({"operation": "render", "gds": str(source), "raw_dir": str(raw),
             "chip": _project_name(config), "viewport_um": settings["gds"]["viewport_um"],
             "resolution": settings["resolution"], "segments": settings["segments"],
             "overrender": settings["overrender"], "layers": settings["layers"]})
    expected = len(selected) * settings["segments"][0] * settings["segments"][1]
    raw_files = sorted(raw.glob(f"RAW__{_project_name(config)}_*.png"))
    if len(raw_files) != expected:
        raise ProjectError(f"KLayout produced {len(raw_files)} instead of {expected} raw {section} images")
    settings["raw_sha256"] = {path.name: sha256(path) for path in raw_files}
    path = write_json(Path(config["design"]["work_dir"]) / f"{section}.json", settings)
    manifest_path = Path(config["design"]["work_dir"]) / "manifest.json"
    preserve_logo_resolution(config, manifest)
    manifest[section]["config"] = work_relative(config, path)
    manifest[section]["layers_resolved"] = [item["name"] for item in settings["layers"]]
    write_json(manifest_path, manifest)
    return path


def _raw_path(config: dict, layer: dict, source_y: int, source_x: int) -> Path:
    source = f"{layer['layer']}.{layer['datatype']}"
    paths = sorted(Path(config["raw_dir"]).glob(
        f"RAW__{config['chip']}_"
        f"{source}.{layer['name']}_{source_y}-{source_x}.png"))
    if len(paths) != 1:
        raise ProjectError(f"raw image missing or ambiguous for {layer['name']} ({source_x},{source_y})")
    return paths[0]


@contextmanager
def _pixel_limit(configured_limit: int):
    from PIL import Image
    previous_limit = Image.MAX_IMAGE_PIXELS
    if previous_limit is not None and configured_limit > previous_limit:
        Image.MAX_IMAGE_PIXELS = configured_limit
    try:
        yield
    finally:
        Image.MAX_IMAGE_PIXELS = previous_limit


@contextmanager
def _open_raw(config: dict, path: Path):
    """Open a verified raw segment with the configured size as its limit."""
    from PIL import Image
    resolution = [int(value) for value in config["resolution"]]
    segments = [int(value) for value in config.get("segments", [1, 1])]
    overrender = int(config.get("overrender", 1))
    configured_limit = (math.ceil(resolution[0] * overrender / segments[0]) *
                        math.ceil(resolution[1] * overrender / segments[1]))
    with _pixel_limit(configured_limit):
        with Image.open(path) as image:
            yield image


class _BorderStripReader:
    """Read raw crops, retaining only bounded grayscale edge strips."""

    def __init__(self, config: dict, strip_x: int, strip_y: int,
                 max_bytes: int = 1 << 30):
        self.config = config
        self.strip_x = strip_x
        self.strip_y = strip_y
        self.max_bytes = max_bytes
        self.bytes = 0
        self._entries = OrderedDict()
        self._by_path = {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        for strip in self._entries.values():
            strip.close()
        self._entries.clear()
        self._by_path.clear()
        self.bytes = 0

    def _remember(self, path: Path, box: tuple[int, int, int, int], strip):
        size = strip.width * strip.height
        if size > self.max_bytes:
            strip.close()
            return
        while self.bytes + size > self.max_bytes:
            old_key, old_strip = self._entries.popitem(last=False)
            self._by_path[old_key[0]].remove(old_key)
            if not self._by_path[old_key[0]]:
                del self._by_path[old_key[0]]
            self.bytes -= old_strip.width * old_strip.height
            old_strip.close()
        key = (path, box)
        self._entries[key] = strip
        self._by_path.setdefault(path, []).append(key)
        self.bytes += size

    def crop(self, path: Path, box: tuple[int, int, int, int]):
        for key in self._by_path.get(path, ()):
            edge, strip = key[1], self._entries[key]
            if (edge[0] <= box[0] and edge[1] <= box[1] and
                    edge[2] >= box[2] and edge[3] >= box[3]):
                self._entries.move_to_end(key)
                with _pixel_limit(strip.width * strip.height):
                    return strip.crop((box[0] - edge[0], box[1] - edge[1],
                                       box[2] - edge[0], box[3] - edge[1]))

        with _open_raw(self.config, path) as source:
            mask = source.crop(box).convert("L")
            try:
                width, height = source.size
                x = min(width, self.strip_x)
                y = min(height, self.strip_y)
                edges = dict.fromkeys(((0, 0, width, y),
                                       (0, height - y, width, height),
                                       (0, 0, x, height),
                                       (width - x, 0, width, height)))
                for edge in edges:
                    if (path, edge) not in self._entries:
                        self._remember(path, edge, source.crop(edge).convert("L"))
            except BaseException:
                mask.close()
                raise
        return mask


def raw_layout(config: dict) -> dict:
    sx, sy = config["segments"]
    paths, widths, heights = {}, None, None
    for layer in config["layers"]:
        layer_paths, layer_widths, layer_heights = {}, [None] * sx, [None] * sy
        for source_y in range(sy):
            for source_x in range(sx):
                path = _raw_path(config, layer, source_y, source_x)
                with _open_raw(config, path) as image:
                    width, height = image.size
                if layer_widths[source_x] not in (None, width):
                    raise ProjectError("inconsistent raw segment width")
                if layer_heights[source_y] not in (None, height):
                    raise ProjectError("inconsistent raw segment height")
                layer_widths[source_x], layer_heights[source_y] = width, height
                layer_paths[(source_y, source_x)] = path
        if widths is None:
            widths, heights = layer_widths, layer_heights
        elif widths != layer_widths or heights != layer_heights:
            raise ProjectError("raw segment dimensions differ between layers")
        paths[layer["name"]] = layer_paths
    return {"paths": paths, "widths": widths, "heights": heights,
            "width": sum(widths), "height": sum(heights), "segments": (sx, sy)}


def _load_layer(config: dict, name: str):
    from PIL import Image
    layer = next(item for item in config["layers"] if item["name"] == name)
    layout = raw_layout(config)
    image = Image.new("L", (layout["width"], layout["height"]), 255)
    xoff, yoff = [0], [0]
    for width in layout["widths"][:-1]: xoff.append(xoff[-1] + width)
    for height in list(reversed(layout["heights"]))[:-1]: yoff.append(yoff[-1] + height)
    sx, sy = layout["segments"]
    for logical_y in range(sy):
        source_y = sy - 1 - logical_y
        for source_x in range(sx):
            with _open_raw(config, layout["paths"][name][(source_y, source_x)]) as tile:
                image.paste(tile.convert("L"), (xoff[source_x], yoff[logical_y]))
    return image


def _colorize(mask, color: str, alpha: float):
    from PIL import Image, ImageColor
    opacity = mask.point(lambda value: int((255 - value) * max(0, min(1, alpha))))
    rgba = Image.new("RGBA", mask.size, ImageColor.getrgb(color) + (0,))
    rgba.putalpha(opacity)
    return rgba


def _pdf_resolution(width: int, page_width_cm: object) -> float:
    try:
        page_width = float(page_width_cm)
    except (TypeError, ValueError) as exc:
        raise ProjectError("[render].page_width_cm must be a positive number") from exc
    if not math.isfinite(page_width) or page_width <= 0:
        raise ProjectError("[render].page_width_cm must be a positive number")
    return width * 2.54 / page_width


def compose(config: dict) -> list[Path]:
    from PIL import Image
    path = Path(config["design"]["work_dir"]) / "render.json"
    if not path.is_file():
        raise ProjectError("render config not found; run render generate first")
    settings = json.loads(path.read_text())
    if settings.get("record_version") != 2:
        raise ProjectError("render record is outdated; run render generate again")
    source = input_gds(config, "render")
    if (work_relative(config, source) != settings.get("input") or
            sha256(source) != settings.get("input_sha256") or
            generation_hash(config, "render") != settings.get("generation_sha256") or
            _project_name(config) != settings.get("chip")):
        raise ProjectError("render input or generation settings changed; run render generate again")
    settings["raw_dir"] = str(recorded_path(config, settings["raw_dir"]))
    verify_raw(settings)
    selected = [(item["name"], item["layer"], item["datatype"])
                for item in settings["layers"]]
    colors = palette(config, config.get("render", {}), selected)
    background = colors.pop("background")
    resolution = tuple(settings["resolution"])
    page_width_cm = config.get("render", {}).get("page_width_cm")
    pdf_resolution = (_pdf_resolution(resolution[0], page_width_cm)
                      if page_width_cm is not None else 300)
    raw = raw_layout(settings)
    image = Image.new("RGBA", resolution, background)
    sx, sy = raw["segments"]
    xoffset = [0]
    for width in raw["widths"][:-1]:
        xoffset.append(xoffset[-1] + width)
    logical_y_sources = list(reversed(range(sy)))
    yoffset = [0]
    for source_y in logical_y_sources[:-1]:
        yoffset.append(yoffset[-1] + raw["heights"][source_y])
    resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
    xedges = xoffset + [raw["width"]]
    yedges = yoffset + [raw["height"]]
    xscale = raw["width"] / resolution[0]
    yscale = raw["height"] / resolution[1]
    xradius = 3 * max(1, xscale) + 1
    yradius = 3 * max(1, yscale) + 1
    with _BorderStripReader(settings, math.ceil(xradius + xscale),
                            math.ceil(yradius + yscale)) as reader:
        for logical_y, source_y in enumerate(logical_y_sources):
            raw_height = raw["heights"][source_y]
            top = round(yoffset[logical_y] * resolution[1] / raw["height"])
            bottom = round((yoffset[logical_y] + raw_height) * resolution[1] / raw["height"])
            for source_x in range(sx):
                raw_width = raw["widths"][source_x]
                left = round(xoffset[source_x] * resolution[0] / raw["width"])
                right = round((xoffset[source_x] + raw_width) * resolution[0] / raw["width"])
                if left == right or top == bottom:
                    continue
                source_left = left * xscale
                source_right = right * xscale
                source_top = top * yscale
                source_bottom = bottom * yscale
                halo_left = max(0, math.floor(source_left - xradius))
                halo_right = min(raw["width"], math.ceil(source_right + xradius))
                halo_top = max(0, math.floor(source_top - yradius))
                halo_bottom = min(raw["height"], math.ceil(source_bottom + yradius))
                segment = Image.new("RGBA", (halo_right - halo_left,
                                             halo_bottom - halo_top), background)
                try:
                    for item in settings["layers"]:
                        color = colors[item["name"]]
                        for tile_y, tile_source_y in enumerate(logical_y_sources):
                            tile_top = max(halo_top, yedges[tile_y])
                            tile_bottom = min(halo_bottom, yedges[tile_y + 1])
                            if tile_top >= tile_bottom:
                                continue
                            for tile_x in range(sx):
                                tile_left = max(halo_left, xedges[tile_x])
                                tile_right = min(halo_right, xedges[tile_x + 1])
                                if tile_left >= tile_right:
                                    continue
                                mask = reader.crop(raw["paths"][item["name"]]
                                                   [(tile_source_y, tile_x)],
                                                   (tile_left - xedges[tile_x],
                                                    tile_top - yedges[tile_y],
                                                    tile_right - xedges[tile_x],
                                                    tile_bottom - yedges[tile_y]))
                                layer = _colorize(mask, color["color"], color["alpha"])
                                mask.close()
                                with _pixel_limit(segment.width * segment.height):
                                    segment.alpha_composite(layer, (tile_left - halo_left,
                                                                    tile_top - halo_top))
                                layer.close()
                    with _pixel_limit(segment.width * segment.height):
                        resized = segment.resize((right - left, bottom - top), resampling,
                                                 box=(source_left - halo_left, source_top - halo_top,
                                                      source_right - halo_left,
                                                      source_bottom - halo_top))
                    try:
                        image.paste(resized, (left, top))
                    finally:
                        resized.close()
                finally:
                    segment.close()
    outputs = []
    work = Path(config["design"]["work_dir"])
    try:
        for fmt in config.get("render", {}).get("formats", ["png", "jpg", "pdf"]):
            suffix = str(fmt).lower().lstrip(".")
            target = work / f"{_project_name(config)}_render.{suffix}"
            if suffix in ("pdf", "jpg", "jpeg"):
                converted = image.convert("RGB")
                try:
                    if suffix == "pdf":
                        try:
                            import img2pdf
                        except ImportError as exc:
                            raise ProjectError("PDF output requires img2pdf") from exc
                        with tempfile.NamedTemporaryFile(suffix=".png", dir=work) as png:
                            converted.save(png, "PNG")
                            png.flush()
                            layout = img2pdf.get_fixed_dpi_layout_fun(
                                (pdf_resolution, pdf_resolution))
                            with target.open("wb") as output:
                                with _pixel_limit(converted.width * converted.height):
                                    img2pdf.convert(png.name, layout_fun=layout,
                                                    outputstream=output)
                    else:
                        converted.save(target, quality=95)
                finally:
                    converted.close()
            else:
                image.save(target)
            outputs.append(target)
    finally:
        image.close()
    return outputs
