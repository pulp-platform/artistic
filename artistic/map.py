# Copyright 2026 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

"""Memory-bounded map tile generation."""

from __future__ import annotations

import glob
import json
import math
import os
import re
import shutil
from pathlib import Path

from .project import ProjectError, _project_name, input_gds, recorded_path, sha256, work_relative
from .render import _colorize, _open_raw, generation_hash, raw_layout, verify_raw
from .palettes import background_rgba
from .technology import palette


_MARKER = ".artistic-map.json"


def _safe_output(config: dict, raw_dir: Path | None = None) -> Path:
    root = Path(config["design"]["work_dir"]).resolve()
    path = Path(config.get("map", {}).get("output", "map")).expanduser()
    path = path if path.is_absolute() else root / path
    lexical = Path(os.path.abspath(path))
    try:
        parts = lexical.relative_to(root).parts
    except ValueError as exc:
        raise ProjectError("[map].output must be inside [design].work_dir") from exc
    current = root
    for part in parts:
        current /= part
        if current.is_symlink():
            raise ProjectError("[map].output must not contain a symlink")
    path = lexical.resolve()
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ProjectError("[map].output must be inside [design].work_dir") from exc
    if not relative.parts:
        raise ProjectError("[map].output must not be [design].work_dir itself")
    work = root
    protected = [work / "raw", *(work / name for name in (
        "render.json", "render_output.json", "map.json", "logo_prepare.json", "logo_merge.json",
        "manifest.json", "layout.json"))]
    if raw_dir is not None:
        protected.append(raw_dir)
    if config.get("_project"):
        protected.append(Path(config["_project"]))
    if config.get("design", {}).get("gds"):
        protected.append(Path(config["design"]["gds"]))
    if config.get("logo", {}).get("source"):
        protected.append(Path(config["logo"]["source"]))
    technology_file = config.get("technology", {}).get("file")
    if technology_file:
        technology_path = Path(technology_file).expanduser()
        protected.append(technology_path if technology_path.is_absolute() else
                         Path(config.get("_root", root)) / technology_path)
    for section in ("render", "map"):
        value = config.get(section, {}).get("input")
        if value and value not in ("design", "logo"):
            protected.append(Path(value))
    outlines = config.get("render", {}).get("outlines", {})
    if isinstance(outlines, dict):
        project_root = Path(config.get("_root", root))
        if outlines.get("def"):
            value = Path(outlines["def"]).expanduser()
            protected.append(value if value.is_absolute() else project_root / value)
        for pattern in outlines.get("lef_files", []):
            value = Path(pattern).expanduser()
            absolute = value if value.is_absolute() else project_root / value
            protected.extend(Path(match) for match in glob.glob(str(absolute), recursive=True))
    chip = _project_name(config) if config.get("design", {}).get("gds") else ""
    if chip:
        protected.extend(work / f"{chip}_{suffix}" for suffix in (
            "logo_mono.png", "logo.svg", "logo_geometry.svg", "logo_render.png", "logo.gds", "chip.gds.gz",
            "palette.svg",
            "poster.pdf", "modules.svg", "modules.png", "modules.pdf", "modules.jpg"))
        protected.extend(work / f"{chip}_render.{fmt}"
                         for fmt in ("png", "jpg", "jpeg", "pdf"))
    for item in protected:
        item = item.resolve()
        if path == item or path in item.parents or item in path.parents:
            raise ProjectError(f"[map].output overlaps protected input or stage path: {item}")
    return path


def _prepare_output(config: dict, output: Path) -> None:
    if output.exists():
        if not output.is_dir():
            raise ProjectError("[map].output exists and is not a directory")
        if any(output.iterdir()):
            marker = output / _MARKER
            expected = {"format": "artistic-map", "version": 1,
                        "output": work_relative(config, output)}
            try:
                owned = not marker.is_symlink() and json.loads(marker.read_text()) == expected
            except (OSError, ValueError):
                owned = False
            if not owned:
                raise ProjectError("[map].output is nonempty and not an Artistic-generated map")
            shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)
    (output / _MARKER).write_text(json.dumps({
        "format": "artistic-map", "version": 1,
        "output": work_relative(config, output)}, sort_keys=True) + "\n")


def _tile_writer(output: Path, tile_size: int, zoom: int, background, layer_style: str):
    from PIL import Image

    def write(name, image, left, top):
        right, bottom = left + image.width, top + image.height
        for ty in range(top // tile_size, (bottom - 1) // tile_size + 1):
            for tx in range(left // tile_size, (right - 1) // tile_size + 1):
                target = output / name / str(zoom) / str(tx) / f"{ty}.png"
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.is_file():
                    with Image.open(target) as existing:
                        tile = existing.convert("RGBA")
                else:
                    fill = (background if name == "composite" else
                            (255, 255, 255, 255) if layer_style == "mask" else
                            (0, 0, 0, 0))
                    tile = Image.new("RGBA", (tile_size, tile_size), fill)
                left_i, top_i = max(left, tx * tile_size), max(top, ty * tile_size)
                right_i, bottom_i = min(right, (tx + 1) * tile_size), min(bottom, (ty + 1) * tile_size)
                with image.crop((left_i - left, top_i - top,
                                 right_i - left, bottom_i - top)) as crop:
                    tile.paste(crop, (left_i - tx * tile_size, top_i - ty * tile_size))
                tile.save(target)
                tile.close()
    return write


def _parent_tile(layer_root: Path, child_zoom: int, tx: int, ty: int,
                 tile_size: int, child_shape: tuple[int, int], fill):
    from PIL import Image

    # LANCZOS at half scale reaches six source pixels beyond a tile edge.
    # An even eight-pixel halo also keeps every parent's sampling grid aligned.
    halo = 8
    left, top = 2 * tx * tile_size - halo, 2 * ty * tile_size - halo
    size = 2 * tile_size + 2 * halo
    right, bottom = left + size, top + size
    with Image.new("RGBA", (size, size), fill) as canvas:
        for child_x in range(max(0, left // tile_size),
                             min(child_shape[0], (right - 1) // tile_size + 1)):
            for child_y in range(max(0, top // tile_size),
                                 min(child_shape[1], (bottom - 1) // tile_size + 1)):
                child = layer_root / str(child_zoom) / str(child_x) / f"{child_y}.png"
                if not child.is_file():
                    continue
                child_left, child_top = child_x * tile_size, child_y * tile_size
                crop_left, crop_top = max(left, child_left), max(top, child_top)
                crop_right = min(right, child_left + tile_size)
                crop_bottom = min(bottom, child_top + tile_size)
                with Image.open(child) as image:
                    with image.crop((crop_left - child_left, crop_top - child_top,
                                     crop_right - child_left, crop_bottom - child_top)) as crop:
                        with crop.convert("RGBA") as rgba:
                            canvas.paste(rgba, (crop_left - left, crop_top - top))
        # Pillow's RGBA resize filters premultiplied alpha, avoiding color fringes.
        with canvas.resize((size // 2, size // 2), Image.Resampling.LANCZOS) as resized:
            margin = halo // 2
            return resized.crop((margin, margin, margin + tile_size, margin + tile_size))


def build(config: dict) -> Path:
    from PIL import Image
    work = Path(config["design"]["work_dir"])
    path = work / "map.json"
    if not path.is_file():
        raise ProjectError("map config not found; run map generate first")
    settings = json.loads(path.read_text())
    if settings.get("record_version") != 2:
        raise ProjectError("map record is outdated; run map generate again")
    source = input_gds(config, "map")
    if (work_relative(config, source) != settings.get("input") or
            sha256(source) != settings.get("input_sha256") or
            generation_hash(config, "map") != settings.get("generation_sha256") or
            _project_name(config) != settings.get("chip")):
        raise ProjectError("map input or generation settings changed; run map generate again")
    settings["raw_dir"] = str(recorded_path(config, settings["raw_dir"]))
    verify_raw(settings)
    raw = raw_layout(settings)
    tile_size = int(config.get("map", {}).get("tile_size", 512))
    if tile_size <= 0:
        raise ProjectError("[map].tile_size must be positive")
    layer_style = config.get("map", {}).get("layer_style", "mask")
    if layer_style not in ("mask", "color"):
        raise ProjectError("[map].layer_style must be 'mask' or 'color'")
    selected = [item["name"] for item in settings["layers"]]
    requested = config.get("map", {}).get("views", config.get("map", {}).get("layers", "routing"))
    names = list(selected) if requested in ("routing", "all") else ([requested] if isinstance(requested, str) else list(requested))
    expanded = []
    for name in names:
        if str(name).lower() == "metals":
            expanded.extend(layer for layer in selected if re.fullmatch(r"(?:Top)?Metal\d+", layer))
        elif str(name).lower() == "top-metal":
            expanded.append(settings["technology"]["top_metal"])
        else:
            expanded.append(name)
    names = list(dict.fromkeys(
        name for name in expanded if name in selected or name == "composite"
    ))
    if bool(config.get("map", {}).get("composite", True)) and "composite" not in names:
        names.insert(0, "composite")
    if not names:
        raise ProjectError("[map].views selects no generated views")
    resolved_layers = [(item["name"], item["layer"], item["datatype"])
                       for item in settings["layers"]]
    colors = palette(config, config.get("map", {}), resolved_layers,
                     routing=settings.get("technology", {}).get("routing"))
    bg = background_rgba(colors.pop("background"))
    output = _safe_output(config, Path(settings["raw_dir"]))
    _prepare_output(config, output)
    max_zoom = max(0, math.ceil(math.log(max(raw["width"], raw["height"]) / tile_size, 2)))
    write_tile = _tile_writer(output, tile_size, max_zoom, bg, layer_style)
    xoffset = [0]
    for width in raw["widths"][:-1]: xoffset.append(xoffset[-1] + width)
    yoffset, logical = [0], list(reversed(range(raw["segments"][1])))
    for source_y in logical[:-1]: yoffset.append(yoffset[-1] + raw["heights"][source_y])
    for logical_y, source_y in enumerate(logical):
        for source_x in range(raw["segments"][0]):
            composite = (Image.new("RGBA", (raw["widths"][source_x], raw["heights"][source_y]),
                                   bg) if "composite" in names else None)
            for item in settings["layers"]:
                name = item["name"]
                if composite is None and name not in names:
                    continue
                with _open_raw(settings, raw["paths"][item["name"]][(source_y, source_x)]) as source_image:
                    mask = source_image.convert("L")
                if name in names and layer_style == "mask":
                    mask_view = mask.convert("RGB").convert("RGBA")
                    write_tile(name, mask_view, xoffset[source_x], yoffset[logical_y])
                    mask_view.close()
                if composite is not None or (name in names and layer_style == "color"):
                    color = colors[name]
                    layer = _colorize(mask, color["color"], color["alpha"])
                    if name in names and layer_style == "color":
                        write_tile(name, layer, xoffset[source_x], yoffset[logical_y])
                    if composite is not None:
                        composite.alpha_composite(layer)
                    layer.close()
                mask.close()
            if composite is not None:
                write_tile("composite", composite, xoffset[source_x], yoffset[logical_y])
                composite.close()
    # Keep one parent canvas and its neighboring halo in memory, independent
    # of the number of tiles in the map.
    nx = max(1, math.ceil(raw["width"] / tile_size))
    ny = max(1, math.ceil(raw["height"] / tile_size))
    for name in names:
        layer_root = output / name
        for zoom in range(max_zoom - 1, -1, -1):
            factor = 2 ** (max_zoom - zoom)
            child_shape = (math.ceil(nx / (factor // 2)), math.ceil(ny / (factor // 2)))
            for tx in range(max(1, math.ceil(nx / factor))):
                for ty in range(max(1, math.ceil(ny / factor))):
                    fill = (bg if name == "composite" else
                            (255, 255, 255, 255) if layer_style == "mask" else
                            (0, 0, 0, 0))
                    target = layer_root / str(zoom) / str(tx) / f"{ty}.png"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with _parent_tile(layer_root, zoom + 1, tx, ty, tile_size,
                                      child_shape, fill) as parent:
                        parent.save(target)
    metadata = {"layers": names, "tile_size": tile_size, "max_zoom": max_zoom,
                "width": raw["width"], "height": raw["height"]}
    (output / "map.json").write_text(json.dumps(metadata, indent=2) + "\n")
    (output / "index.html").write_text(_viewer(metadata))
    return output


def _viewer(metadata: dict) -> str:
    return """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>ArtistIC map</title>
<style>body{margin:0}#map{position:fixed;inset:0}</style>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
 integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="">
<div id="map"></div><script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
 integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
<script>const names=%s,maxZoom=%d,map=L.map('map',{crs:L.CRS.Simple,minZoom:-5,maxZoom});
const bounds=L.latLngBounds(map.unproject([0,%s],maxZoom),map.unproject([%s,0],maxZoom));
const layers=Object.fromEntries(names.map(n=>[n,L.tileLayer(`${n}/{z}/{x}/{y}.png`,{tileSize:%d,noWrap:true,bounds,minZoom:-5,minNativeZoom:0,maxNativeZoom:maxZoom})]));
layers[names[0]].addTo(map);L.control.layers(layers,null,{collapsed:!L.Browser.touch}).addTo(map);map.fitBounds(bounds);</script>""" % (
        json.dumps(metadata["layers"]), metadata["max_zoom"], metadata["height"],
        metadata["width"], metadata["tile_size"])
