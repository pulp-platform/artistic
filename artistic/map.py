# Copyright 2026 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

"""Memory-bounded map tile generation."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
from pathlib import Path

from .project import ProjectError, sha256
from .render import _colorize, generation_hash, raw_layout, verify_raw
from .technology import palette


def _safe_output(config: dict) -> Path:
    root = Path(config["design"]["work_dir"]).resolve()
    path = Path(config.get("map", {}).get("output", "map")).expanduser()
    path = path if path.is_absolute() else root / path
    path = path.resolve()
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ProjectError("[map].output must be inside [design].work_dir") from exc
    if not relative.parts:
        raise ProjectError("[map].output must not be [design].work_dir itself")
    return path


def _tile_writer(output: Path, tile_size: int, zoom: int, background):
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
                    fill = background + (255,) if name == "composite" else (0, 0, 0, 0)
                    tile = Image.new("RGBA", (tile_size, tile_size), fill)
                left_i, top_i = max(left, tx * tile_size), max(top, ty * tile_size)
                right_i, bottom_i = min(right, (tx + 1) * tile_size), min(bottom, (ty + 1) * tile_size)
                crop = image.crop((left_i - left, top_i - top, right_i - left, bottom_i - top))
                tile.alpha_composite(crop, (left_i - tx * tile_size, top_i - ty * tile_size))
                tile.save(target)
                tile.close()
    return write


def build(config: dict) -> Path:
    from PIL import Image, ImageColor
    work = Path(config["design"]["work_dir"])
    path = work / "map.json"
    if not path.is_file():
        raise ProjectError("map config not found; run map generate first")
    settings = json.loads(path.read_text())
    source = Path(settings["input"])
    if (not source.is_file() or sha256(source) != settings["input_sha256"] or
            generation_hash(config, "map") != settings.get("generation_sha256")):
        raise ProjectError("map input or generation settings changed; run map generate again")
    verify_raw(settings)
    raw = raw_layout(settings)
    tile_size = int(config.get("map", {}).get("tile_size", 512))
    if tile_size <= 0:
        raise ProjectError("[map].tile_size must be positive")
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
    colors = palette(config, config.get("map", {}), resolved_layers)
    output = _safe_output(config)
    if output.is_symlink() or (output.exists() and not output.is_dir()): output.unlink()
    elif output.is_dir(): shutil.rmtree(output)
    output.mkdir(parents=True)
    max_zoom = max(0, math.ceil(math.log(max(raw["width"], raw["height"]) / tile_size, 2)))
    background = colors.pop("background")
    bg = ImageColor.getrgb(background)[:3]
    write_tile = _tile_writer(output, tile_size, max_zoom, bg)
    xoffset = [0]
    for width in raw["widths"][:-1]: xoffset.append(xoffset[-1] + width)
    yoffset, logical = [0], list(reversed(range(raw["segments"][1])))
    for source_y in logical[:-1]: yoffset.append(yoffset[-1] + raw["heights"][source_y])
    for logical_y, source_y in enumerate(logical):
        for source_x in range(raw["segments"][0]):
            composite = Image.new("RGBA", (raw["widths"][source_x], raw["heights"][source_y]), (0, 0, 0, 0))
            for item in settings["layers"]:
                with Image.open(raw["paths"][item["name"]][(source_y, source_x)]) as source_image:
                    mask = source_image.convert("L")
                color = colors[item["name"]]
                layer = _colorize(mask, color["color"], color["alpha"])
                mask.close()
                if item["name"] in names: write_tile(item["name"], layer, xoffset[source_x], yoffset[logical_y])
                composite.alpha_composite(layer); layer.close()
            if "composite" in names: write_tile("composite", composite, xoffset[source_x], yoffset[logical_y])
            composite.close()
    # Build the lower zoom levels from four children at a time.  Only one
    # parent canvas is kept in memory, so large maps do not scale with the
    # number of tiles.
    nx = max(1, math.ceil(raw["width"] / tile_size))
    ny = max(1, math.ceil(raw["height"] / tile_size))
    for name in names:
        layer_root = output / name
        for zoom in range(max_zoom - 1, -1, -1):
            factor = 2 ** (max_zoom - zoom)
            for tx in range(max(1, math.ceil(nx / factor))):
                for ty in range(max(1, math.ceil(ny / factor))):
                    canvas = Image.new("RGBA", (tile_size * 2, tile_size * 2),
                                       bg + (255,) if name == "composite" else (0, 0, 0, 0))
                    for dx in (0, 1):
                        for dy in (0, 1):
                            child = layer_root / str(zoom + 1) / str(2 * tx + dx) / f"{2 * ty + dy}.png"
                            if child.is_file():
                                with Image.open(child) as image:
                                    canvas.alpha_composite(image.convert("RGBA"),
                                                           (dx * tile_size, dy * tile_size))
                    target = layer_root / str(zoom) / str(tx) / f"{ty}.png"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    canvas.resize((tile_size, tile_size), Image.Resampling.LANCZOS).save(target)
                    canvas.close()
    metadata = {"layers": names, "tile_size": tile_size, "max_zoom": max_zoom,
                "width": raw["width"], "height": raw["height"]}
    (output / "map.json").write_text(json.dumps(metadata, indent=2) + "\n")
    (output / "index.html").write_text(_viewer(metadata))
    return output


def _viewer(metadata: dict) -> str:
    return """<!doctype html><meta charset="utf-8"><title>ArtistIC map</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<div id="map" style="height:100vh"></div><script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>const names=%s,maxZoom=%d,map=L.map('map',{crs:L.CRS.Simple,minZoom:0,maxZoom});
const bounds=L.latLngBounds(map.unproject([0,%s],maxZoom),map.unproject([%s,0],maxZoom));
const layers=Object.fromEntries(names.map(n=>[n,L.tileLayer(`${n}/{z}/{x}/{y}.png`,{tileSize:%d,noWrap:true,bounds,maxNativeZoom:maxZoom})]));
layers[names[0]].addTo(map);L.control.layers(layers).addTo(map);map.fitBounds(bounds);</script>""" % (
        json.dumps(metadata["layers"]), metadata["max_zoom"], metadata["height"],
        metadata["width"], metadata["tile_size"])
