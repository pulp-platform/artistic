# Copyright 2026 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

"""Logo preparation on the host and physical logo merge in KLayout."""

from __future__ import annotations

import datetime
import hashlib
import json
import math
import subprocess
from pathlib import Path

from .project import (ProjectError, _project_name, project_relative, run_checked,
                      sha256, tool, write_json)
from .technology import layer, inspect_layout, run_pya


def _preprocessing(settings: dict) -> dict:
    def number(key: str, default: float) -> float:
        try:
            value = float(settings.get(key, default))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ProjectError(f"[logo].{key} must be a finite number") from exc
        if not math.isfinite(value):
            raise ProjectError(f"[logo].{key} must be a finite number")
        return value

    feature = number("feature_um", 2.0)
    width = number("width_um", 0)
    height = number("height_um", 0)
    if min(feature, width, height) <= 0:
        raise ProjectError("[logo] width_um, height_um, and feature_um must be positive")
    dither = settings.get("dither", "threshold")
    if dither not in ("threshold", "floyd-steinberg", "ordered"):
        raise ProjectError("[logo].dither must be threshold, floyd-steinberg, or ordered")
    threshold = number("threshold", 0.5)
    contrast = number("contrast", 1.0)
    if not 0 <= threshold <= 1:
        raise ProjectError("[logo].threshold must be between 0 and 1")
    if contrast <= 0:
        raise ProjectError("[logo].contrast must be positive")
    width_ratio, height_ratio = width / feature, height / feature
    if not math.isfinite(width_ratio) or not math.isfinite(height_ratio):
        raise ProjectError("[logo] width_um and height_um are too large for feature_um")
    return {"width_px": max(1, round(width_ratio)),
            "height_px": max(1, round(height_ratio)),
            "feature_um": feature, "dither": dither,
            "threshold": threshold, "contrast": contrast}


def _binary_mask(grayscale, settings: dict):
    from PIL import Image

    if settings["dither"] == "threshold":
        return grayscale.point(lambda value: 255 if value >= 255 * settings["threshold"] else 0)
    if settings["dither"] == "floyd-steinberg":
        dither = getattr(getattr(Image, "Dither", Image), "FLOYDSTEINBERG")
        with grayscale.convert("1", dither=dither) as binary:
            return binary.convert("L")
    bayer = ((0, 8, 2, 10), (12, 4, 14, 6),
             (3, 11, 1, 9), (15, 7, 13, 5))
    mask = Image.new("L", grayscale.size)
    mask.putdata([255 if value >= (bayer[(index // grayscale.width) % 4]
                                  [index % grayscale.width % 4] + 0.5) * 255 / 16 else 0
                  for index, value in enumerate(grayscale.getdata())])
    return mask


def _expanded_source(config: dict, source: Path) -> tuple[str | None, str]:
    if source.suffix.lower() != ".svg":
        return None, sha256(source)
    try:
        revision = subprocess.check_output(
            ["git", "-C", config["_root"], "rev-parse", "--short", "HEAD"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        revision = "unknown"
    text = source.read_text()
    for key, value in {"date": datetime.date.today().isoformat(),
                       "git_revision": revision,
                       "repository": config.get("design", {}).get("repository", "")}.items():
        text = text.replace("{{%s}}" % key, value)
    return text, hashlib.sha256(text.encode()).hexdigest()


def prepare(config: dict) -> Path:
    settings = config.get("logo", {})
    source = settings.get("source")
    if not source:
        raise ProjectError("[logo].source is required for logo prepare")
    preprocessing = _preprocessing(settings)
    width_px, height_px = preprocessing["width_px"], preprocessing["height_px"]
    work = Path(config["design"]["work_dir"])
    work.mkdir(parents=True, exist_ok=True)
    output = work / f"{_project_name(config)}_logo_mono.png"
    source_path = Path(source)
    if not source_path.is_file():
        raise ProjectError(f"logo source not found: {source_path}")
    input_path = source_path
    expanded, expanded_digest = _expanded_source(config, source_path)
    if expanded is not None:
        templated = work / f"{_project_name(config)}_logo.svg"
        templated.write_text(expanded)
        rendered = work / f"{_project_name(config)}_logo_render.png"
        run_checked([tool("inkscape"), str(templated), f"--export-filename={rendered}",
                     f"--export-width={width_px}", f"--export-height={height_px}"])
        input_path = rendered
    from PIL import Image, ImageEnhance
    resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
    with Image.open(input_path) as source_image:
        with source_image.convert("RGBA") as rgba:
            with rgba.resize((width_px, height_px), resampling) as resized:
                with Image.new("RGB", resized.size, "white") as flattened:
                    with resized.getchannel("A") as alpha:
                        flattened.paste(resized, mask=alpha)
                    with flattened.convert("L") as grayscale:
                        with ImageEnhance.Contrast(grayscale).enhance(preprocessing["contrast"]) as contrasted:
                            with _binary_mask(contrasted, preprocessing) as mask:
                                mask.save(output)
    if not output.is_file() or output.stat().st_size == 0:
        raise ProjectError(f"logo prepare produced no mask: {output}")
    write_json(work / "logo_prepare.json", {
        "record_version": 3, "chip": _project_name(config),
        "source": project_relative(config, source_path), "source_sha256": sha256(source_path),
        "expanded_source_sha256": expanded_digest, "mask_sha256": sha256(output),
        **preprocessing,
        "repository": config.get("design", {}).get("repository", "")})
    return output


def _mask_rows(path: Path) -> tuple[int, int, list[dict]]:
    try:
        from PIL import Image
        with Image.open(path) as source:
            image = source.convert("L")
    except (OSError, ImportError) as exc:
        raise ProjectError(f"cannot read prepared logo mask {path}: {exc}") from exc
    with image:
        rows = []
        for y in range(image.height):
            pixels = [value < 128 for value in image.crop((0, y, image.width, y + 1)).getdata()]
            runs, start = [], None
            for x, foreground in enumerate(pixels + [False]):
                if foreground and start is None:
                    start = x
                elif not foreground and start is not None:
                    runs.append([start, x]); start = None
            if runs:
                rows.append({"y": y, "runs": runs})
        width, height = image.size
    if not rows:
        raise ProjectError(f"prepared logo mask has no foreground pixels: {path}")
    return width, height, rows


def merge(config: dict, technology: str | Path | None = None) -> Path:
    work = Path(config["design"]["work_dir"])
    mask = work / f"{_project_name(config)}_logo_mono.png"
    record = work / "logo_prepare.json"
    if not mask.is_file() or not record.is_file():
        raise ProjectError("logo preparation not found; run logo prepare first")
    source = Path(config["logo"]["source"])
    if not source.is_file():
        raise ProjectError(f"logo source not found: {source}")
    preprocessing = _preprocessing(config["logo"])
    expected = {"record_version": 3, "chip": _project_name(config),
                "source": project_relative(config, source), "source_sha256": sha256(source),
                "expanded_source_sha256": _expanded_source(config, source)[1],
                **preprocessing,
                "repository": config.get("design", {}).get("repository", "")}
    prepared = json.loads(record.read_text())
    mask_digest = prepared.pop("mask_sha256", None)
    if prepared != expected or mask_digest != sha256(mask):
        raise ProjectError("logo settings changed; run logo prepare again")
    width_px, height_px, rows = _mask_rows(mask)
    manifest = inspect_layout(config, technology)
    name, number, datatype = layer(config["logo"].get("layer"), manifest["technology"])
    logo_gds = work / f"{_project_name(config)}_logo.gds"
    chip_gds = work / f"{_project_name(config)}_chip.gds.gz"
    result = work / "logo_merge.json"
    layout = manifest["layout"]
    run_pya({"operation": "logo_merge", "gds": config["design"]["gds"],
             "layer": number, "datatype": datatype, "bbox_dbu": layout["bbox_dbu"],
             "logo_cell": f"{_project_name(config)}_logo", "chip_cell": f"{_project_name(config)}_chip",
             "rows": rows, "width_px": width_px, "height_px": height_px,
             "feature_um": preprocessing["feature_um"], "offset_x_um": float(config["logo"].get("offset_x_um", 0)),
             "offset_y_um": float(config["logo"].get("offset_y_um", 0)),
             "logo_gds": str(logo_gds), "chip_gds": str(chip_gds), "result": str(result)})
    if not logo_gds.is_file() or not chip_gds.is_file():
        raise ProjectError("logo merge did not produce both GDS outputs")
    manifest["gds"] = str(chip_gds)
    manifest["logo"]["resolved_layer"] = {"name": name, "layer": number, "datatype": datatype}
    write_json(work / "manifest.json", manifest)
    return chip_gds
