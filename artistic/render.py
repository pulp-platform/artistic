# Copyright 2026 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

"""Layer-mask generation and host-side render composition."""

from __future__ import annotations

import json
import math
import re
import hashlib
from pathlib import Path

from .project import ProjectError, _project_name, sha256, write_json
from .technology import inspect_layout, palette, selected_layers, run_pya


def generation_hash(config: dict, section: str) -> str:
    settings = config.get(section, {})
    value = {
        "input": settings.get("input", "design"),
        "resolution": [int(item) for item in settings.get("resolution", [1000, 1000])],
        "segments": [int(item) for item in settings.get("segments", [1, 1])],
        "overrender": int(settings.get("overrender", 1)),
        "margin_um": float(settings.get("margin_um", 0)),
        "layers": settings.get("layers", "routing"),
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
    viewport = _viewport(manifest["layout"], settings)
    raw_dir = Path(config["design"]["work_dir"]) / "raw" / section
    return {"project": config.get("_project"), "section": section,
            "chip": _project_name(config),
            "input": str(source.resolve()), "input_sha256": sha256(source),
            "gds": {"file": str(source), "viewport_um": viewport},
            "resolution": [int(value) for value in resolution],
            "segments": [int(value) for value in segments], "overrender": overrender,
            "layers": [{"name": name, "layer": layer, "datatype": datatype}
                       for name, layer, datatype in selected],
            "raw_dir": str(raw_dir),
            "technology": manifest["technology"], "layout": manifest["layout"],
            "generation_sha256": generation_hash(config, section)}


def generate(config: dict, technology: str | Path | None = None,
             section: str = "render") -> Path:
    source = Path(config["design"]["gds"]) if config.get(section, {}).get("input", "design") == "design" \
        else (Path(config["design"]["work_dir"]) / f"{_project_name(config)}_chip.gds.gz"
              if config[section].get("input") == "logo" else Path(config[section]["input"]))
    if not source.is_file():
        raise ProjectError(f"[{section}] input GDS not found: {source}")
    manifest = inspect_layout(config, technology, source)
    selected = selected_layers(config.get(section, {}), manifest["technology"], manifest["layout"])
    if not selected:
        raise ProjectError(f"no configured {section} layers are present in the GDS")
    settings = _settings(config, manifest, section, source, selected)
    raw = Path(settings["raw_dir"]); raw.mkdir(parents=True, exist_ok=True)
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
    manifest[section]["config"] = str(path)
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


def raw_layout(config: dict) -> dict:
    from PIL import Image
    sx, sy = config["segments"]
    paths, widths, heights = {}, None, None
    for layer in config["layers"]:
        layer_paths, layer_widths, layer_heights = {}, [None] * sx, [None] * sy
        for source_y in range(sy):
            for source_x in range(sx):
                path = _raw_path(config, layer, source_y, source_x)
                with Image.open(path) as image:
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
            with Image.open(layout["paths"][name][(source_y, source_x)]) as tile:
                image.paste(tile.convert("L"), (xoff[source_x], yoff[logical_y]))
    return image


def _colorize(mask, color: str, alpha: float):
    from PIL import Image, ImageColor
    opacity = mask.point(lambda value: int((255 - value) * max(0, min(1, alpha))))
    rgba = Image.new("RGBA", mask.size, ImageColor.getrgb(color) + (0,))
    rgba.putalpha(opacity)
    return rgba


def compose(config: dict) -> list[Path]:
    from PIL import Image
    path = Path(config["design"]["work_dir"]) / "render.json"
    if not path.is_file():
        raise ProjectError("render config not found; run render generate first")
    settings = json.loads(path.read_text())
    source = Path(settings["input"])
    if not source.is_file() or sha256(source) != settings["input_sha256"] or \
            generation_hash(config, "render") != settings.get("generation_sha256"):
        raise ProjectError("render input or generation settings changed; run render generate again")
    verify_raw(settings)
    selected = [(item["name"], item["layer"], item["datatype"])
                for item in settings["layers"]]
    colors = palette(config, config.get("render", {}), selected)
    first = _load_layer(settings, settings["layers"][0]["name"])
    background = colors.pop("background")
    image = Image.new("RGBA", first.size, background)
    for item in settings["layers"]:
        mask = first if item["name"] == settings["layers"][0]["name"] else _load_layer(settings, item["name"])
        color = colors[item["name"]]
        image = Image.alpha_composite(image, _colorize(mask, color["color"], color["alpha"]))
    image = image.resize(tuple(settings["resolution"]), getattr(getattr(Image, "Resampling", Image), "LANCZOS"))
    outputs = []
    work = Path(config["design"]["work_dir"])
    for fmt in config.get("render", {}).get("formats", ["png", "jpg", "pdf"]):
        suffix = str(fmt).lower().lstrip(".")
        target = work / f"{_project_name(config)}_render.{suffix}"
        image.convert("RGB").save(target, "PDF", resolution=300) if suffix == "pdf" else \
            image.convert("RGB").save(target, quality=95) if suffix in ("jpg", "jpeg") else image.save(target)
        outputs.append(target)
    return outputs
