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
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path

from .project import (ProjectError, _project_name, project_relative, recorded_path,
                      run_checked, sha256, tool, work_relative, write_json)
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
    spacing = number("spacing_um", 2.0)
    minimum_pitch = Decimal(str(feature)) + Decimal(str(spacing))
    pitch = number("pitch_um", float(minimum_pitch))
    width = number("width_um", 0)
    height = number("height_um", 0)
    if min(feature, spacing, pitch, width, height) <= 0:
        raise ProjectError("[logo] width_um, height_um, feature_um, spacing_um, and pitch_um must be positive")
    if Decimal(str(pitch)) < minimum_pitch:
        raise ProjectError("[logo].pitch_um must be at least feature_um + spacing_um")
    maximum = number("max_feature_um", 0) if "max_feature_um" in settings else None
    if maximum is not None and (maximum <= 0 or feature > maximum):
        raise ProjectError("[logo].feature_um must not exceed positive max_feature_um")
    if width < feature or height < feature:
        raise ProjectError("[logo] width_um and height_um must fit at least one feature")
    dither = settings.get("dither", "threshold")
    if dither not in ("threshold", "floyd-steinberg", "ordered"):
        raise ProjectError("[logo].dither must be threshold, floyd-steinberg, or ordered")
    threshold = number("threshold", 0.5)
    contrast = number("contrast", 1.0)
    if not 0 <= threshold <= 1:
        raise ProjectError("[logo].threshold must be between 0 and 1")
    if contrast <= 0:
        raise ProjectError("[logo].contrast must be positive")
    width_ratio, height_ratio = (width - feature) / pitch, (height - feature) / pitch
    if not math.isfinite(width_ratio) or not math.isfinite(height_ratio):
        raise ProjectError("[logo] width_um and height_um are too large for pitch_um")
    def pixel_count(extent):
        ratio = (Decimal(str(extent)) - Decimal(str(feature))) / Decimal(str(pitch))
        return int(ratio.to_integral_value(rounding=ROUND_FLOOR)) + 1

    return {"width_px": pixel_count(width),
            "height_px": pixel_count(height),
            "width_um": width, "height_um": height,
            "feature_um": feature, "spacing_um": spacing, "pitch_um": pitch,
            "max_feature_um": maximum, "dither": dither,
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
    text = source.read_text(encoding="utf-8")
    for key, value in {"date": datetime.date.today().isoformat(),
                       "git_revision": revision,
                       "repository": config.get("design", {}).get("repository", "")}.items():
        text = text.replace("{{%s}}" % key, value)
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


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
        templated.write_text(expanded, encoding="utf-8")
        expanded_digest = sha256(templated)
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
        "record_version": 4, "chip": _project_name(config),
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
    result = work / "logo_merge.json"
    result.unlink(missing_ok=True)
    mask = work / f"{_project_name(config)}_logo_mono.png"
    record = work / "logo_prepare.json"
    if not mask.is_file() or not record.is_file():
        raise ProjectError("logo preparation not found; run logo prepare and merge again")
    try:
        prepared_digest = sha256(record)
        prepared_mask_digest = sha256(mask)
        base_gds = config.get("design", {}).get("gds")
        base_gds_digest = sha256(Path(base_gds)) if base_gds else None
    except OSError as exc:
        raise ProjectError(f"cannot read logo merge input: {exc}") from exc
    source_value = config.get("logo", {}).get("source")
    if not source_value:
        raise ProjectError("[logo].source is required; run logo prepare first")
    source = Path(source_value)
    if not source.is_file():
        raise ProjectError(f"logo source not found: {source}")
    preprocessing = _preprocessing(config["logo"])
    expected = {"record_version": 4, "chip": _project_name(config),
                "source_sha256": sha256(source),
                **preprocessing,
                "repository": config.get("design", {}).get("repository", "")}
    try:
        prepared = json.loads(record.read_text())
    except (OSError, ValueError) as exc:
        raise ProjectError("logo preparation record is missing or malformed; run logo prepare again") from exc
    if not isinstance(prepared, dict):
        raise ProjectError("logo preparation record is malformed; run logo prepare again")
    mask_digest = prepared.pop("mask_sha256", None)
    expanded_digest = prepared.pop("expanded_source_sha256", None)
    prepared.pop("source", None)
    if prepared != expected or mask_digest != prepared_mask_digest:
        raise ProjectError("logo settings changed; run logo prepare again")
    if source.suffix.lower() == ".svg":
        templated = work / f"{_project_name(config)}_logo.svg"
        if not templated.is_file() or sha256(templated) != expanded_digest:
            raise ProjectError("prepared logo SVG changed or is missing; run logo prepare again")
    if base_gds_digest is None:
        raise ProjectError("[design].gds is required for logo merge")
    width_px, height_px, rows = _mask_rows(mask)
    if (width_px, height_px) != (preprocessing["width_px"], preprocessing["height_px"]):
        raise ProjectError("prepared logo mask dimensions changed; run logo prepare again")
    offsets = {}
    for key in ("offset_x_um", "offset_y_um"):
        try:
            value = float(config["logo"].get(key, 0))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ProjectError(f"[logo].{key} must be a finite number") from exc
        if not math.isfinite(value):
            raise ProjectError(f"[logo].{key} must be a finite number")
        offsets[key] = value
    manifest = inspect_layout(config, technology)
    name, number, datatype = layer(config["logo"].get("layer"), manifest["technology"])
    logo_gds = work / f"{_project_name(config)}_logo.gds"
    logo_svg = work / f"{_project_name(config)}_logo_geometry.svg"
    chip_gds = work / f"{_project_name(config)}_chip.gds.gz"
    layout = manifest["layout"]
    run_pya({"operation": "logo_merge", "gds": config["design"]["gds"],
             "layer": number, "datatype": datatype, "bbox_dbu": layout["bbox_dbu"],
             "logo_cell": f"{_project_name(config)}_logo", "chip_cell": f"{_project_name(config)}_chip",
             "rows": rows, "width_px": width_px, "height_px": height_px,
             "width_um": preprocessing["width_um"], "height_um": preprocessing["height_um"],
             "feature_um": preprocessing["feature_um"], "spacing_um": preprocessing["spacing_um"],
             "pitch_um": preprocessing["pitch_um"], "max_feature_um": preprocessing["max_feature_um"],
             **offsets,
             "logo_gds": str(logo_gds), "logo_svg": str(logo_svg),
             "chip_gds": str(chip_gds), "result": str(result)})
    try:
        inputs_unchanged = (sha256(record) == prepared_digest and
                            sha256(mask) == prepared_mask_digest and
                            sha256(Path(config["design"]["gds"])) == base_gds_digest)
    except OSError:
        inputs_unchanged = False
    if not inputs_unchanged:
        result.unlink(missing_ok=True)
        raise ProjectError("logo merge inputs changed during generation; rerun logo prepare and merge")
    if not logo_gds.is_file() or not chip_gds.is_file() or not logo_svg.is_file():
        raise ProjectError("logo merge did not produce both GDS outputs and physical SVG")
    try:
        worker_result = json.loads(result.read_text())
    except (OSError, ValueError) as exc:
        raise ProjectError("logo merge worker did not produce a valid result record") from exc
    if not isinstance(worker_result, dict) or not worker_result:
        raise ProjectError("logo merge worker did not produce a valid result record")
    worker_result.update({
        "record_version": 1,
        "prepared_record_sha256": prepared_digest,
        "mask_sha256": prepared_mask_digest,
        "expanded_source_sha256": expanded_digest,
        "base_gds_sha256": base_gds_digest,
        "config_sha256": _merge_config_sha256(config, offsets, name, number, datatype),
        "resolved_layer": {"name": name, "layer": number, "datatype": datatype},
        "merged_gds": work_relative(config, chip_gds),
        "merged_gds_sha256": sha256(chip_gds),
        "logo_svg": work_relative(config, logo_svg),
    })
    write_json(result, worker_result)
    manifest["gds"] = str(chip_gds)
    manifest["logo"]["resolved_layer"] = {"name": name, "layer": number, "datatype": datatype}
    write_json(work / "manifest.json", manifest)
    return chip_gds


def _merge_config_sha256(config: dict, offsets: dict, name: str, number: int,
                         datatype: int) -> str:
    technology_config = dict(config.get("technology", {}))
    payload = {"logo_layer": config.get("logo", {}).get("layer"),
               "resolved_layer": [name, number, datatype], "offsets": offsets,
               "technology_config": technology_config}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     default=str).encode()).hexdigest()


def validate_merged(config: dict) -> Path:
    work = Path(config["design"]["work_dir"])
    result = work / "logo_merge.json"
    try:
        record = json.loads(result.read_text())
        prepared_path = work / "logo_prepare.json"
        prepared = json.loads(prepared_path.read_text())
        if not isinstance(record, dict) or not isinstance(prepared, dict):
            raise ProjectError("logo stage record is malformed; rerun logo prepare and merge")
        mask = work / f"{_project_name(config)}_logo_mono.png"
        source = Path(config["logo"]["source"])
        if not source.is_file():
            raise ProjectError(f"logo source not found: {source}; rerun logo prepare and merge")
        expected_preparation = {"record_version": 4, "chip": _project_name(config),
                                "source_sha256": sha256(source),
                                **_preprocessing(config["logo"]),
                                "repository": config.get("design", {}).get("repository", "")}
        actual_preparation = {key: value for key, value in prepared.items()
                              if key not in ("mask_sha256", "expanded_source_sha256")}
        actual_preparation.pop("source", None)
        if actual_preparation != expected_preparation:
            raise ProjectError("logo source or settings changed; rerun logo prepare and merge")
        if source.suffix.lower() == ".svg":
            templated = work / f"{_project_name(config)}_logo.svg"
            if (not templated.is_file() or
                    sha256(templated) != prepared.get("expanded_source_sha256")):
                raise ProjectError("prepared logo SVG changed; rerun logo prepare and merge")
        offsets = {}
        for key in ("offset_x_um", "offset_y_um"):
            value = float(config["logo"].get(key, 0))
            if not math.isfinite(value):
                raise ValueError(key)
            offsets[key] = value
        resolved_layer = record["resolved_layer"]
        name, number, datatype = (resolved_layer["name"], resolved_layer["layer"],
                                  resolved_layer["datatype"])
        merged = recorded_path(config, record["merged_gds"])
        expected_merged = work / f"{_project_name(config)}_chip.gds.gz"
        if merged.resolve() != expected_merged.resolve():
            raise ValueError("merged GDS path does not match expected output")
        if (record["record_version"] != 1 or record["prepared_record_sha256"] != sha256(prepared_path)
                or record["mask_sha256"] != sha256(mask)
                or record["expanded_source_sha256"] != prepared["expanded_source_sha256"]
                or record["base_gds_sha256"] != sha256(Path(config["design"]["gds"]))
                or record["config_sha256"] != _merge_config_sha256(
                    config, offsets, name, number, datatype)
                or not merged.is_file() or record["merged_gds_sha256"] != sha256(merged)):
            raise ValueError("stage hashes differ")
    except ProjectError:
        raise
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        raise ProjectError("merged logo is stale or invalid; rerun logo merge") from exc
    return merged
