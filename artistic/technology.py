# Copyright 2026 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

"""Technology discovery and the small KLayout worker bridge."""

from __future__ import annotations

import json
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from .project import ProjectError, run_checked, tool, write_json


def technology_path(override: str | os.PathLike[str] | None, root: str | os.PathLike[str]) -> Path:
    candidates: list[Path] = []
    if override:
        candidate = Path(override).expanduser()
        candidates.append(candidate if candidate.is_absolute() else Path(root) / candidate)
    if os.environ.get("KLAYOUT_TECH_FILE"):
        candidates.append(Path(os.environ["KLAYOUT_TECH_FILE"]).expanduser())
    if os.environ.get("KLAYOUT_PATH") and os.environ.get("KLAYOUT_TECH"):
        for search_root in os.environ["KLAYOUT_PATH"].split(os.pathsep):
            if search_root:
                candidates.append(Path(search_root).expanduser() / "tech" /
                                  f"{os.environ['KLAYOUT_TECH']}.lyt")
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate.is_file():
            return candidate
    tried = ", ".join(str(candidate) for candidate in candidates) or "none"
    raise ProjectError(f"KLayout technology file not found (tried {tried})")


def _source(value: str) -> tuple[int, int]:
    match = re.match(r"\s*(\d+)\s*/\s*(\d+)", value)
    if not match:
        raise ProjectError(f"technology symbol has no numeric source: {value!r}")
    return int(match.group(1)), int(match.group(2))


def resolve(path: Path) -> dict:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise ProjectError(f"cannot read technology file {path}: {exc}") from exc
    connectivity = root.find("connectivity")
    if connectivity is None:
        raise ProjectError(f"technology has no connectivity graph: {path}")
    symbols: dict[str, tuple[int, int]] = {}
    for item in connectivity.findall("symbols"):
        text = item.text or ""
        if "=" not in text:
            continue
        name, value = text.split("=", 1)
        try:
            symbols[name.strip()] = _source(value.strip().strip("'"))
        except ProjectError:
            continue
    metals = {name for name in symbols if re.fullmatch(r"(?:Top)?Metal\d+", name)}
    if "Metal1" not in metals:
        raise ProjectError(f"technology {path} has no Metal1 routing layer")
    links = [[token.strip() for token in (item.text or "").split(",")]
             for item in connectivity.findall("connection")]
    routing = ["Cont"] if "Cont" in symbols else []
    routing.append("Metal1")
    current, used = "Metal1", set()
    while True:
        options = []
        for index, tokens in enumerate(links):
            if index in used or current not in tokens:
                continue
            next_metals = [token for token in tokens if token in metals and token != current]
            if len(next_metals) == 1:
                vias = [token for token in tokens if re.fullmatch(r"(?:Top)?Via\d+", token)]
                options.append((index, next_metals[0], vias[0] if vias else None))
        if not options:
            break
        if len(options) > 1:
            raise ProjectError(f"ambiguous routing connectivity after {current}")
        index, current, via = options[0]
        used.add(index)
        if via and via in symbols:
            routing.append(via)
        routing.append(current)
    return {"technology": str(path.resolve()),
            "symbols": {name: {"layer": layer, "datatype": datatype}
                        for name, (layer, datatype) in symbols.items()},
            "routing": routing, "top_metal": current}


def run_pya(request: dict) -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "pya_pipeline.py"
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as stream:
        json.dump(request, stream)
        request_path = stream.name
    try:
        env = os.environ.copy()
        env["ARTISTIC_PYA_REQUEST"] = request_path
        try:
            run_checked([tool("klayout"), "-b", "-r", str(script), "-rd",
                         f"request={request_path}"], env=env)
        except OSError as exc:
            raise ProjectError(f"cannot run KLayout: {exc}") from exc
    finally:
        try:
            Path(request_path).unlink()
        except OSError:
            pass


def inspect_layout(config: dict, technology: str | os.PathLike[str] | None = None,
                   input_gds: Path | None = None) -> dict:
    configured = technology or config.get("technology", {}).get("file")
    tech_path = technology_path(configured, config["_root"])
    tech = resolve(tech_path)
    source = input_gds or Path(config["design"]["gds"])
    if not source.is_file():
        raise ProjectError(f"input GDS not found: {source}")
    work = Path(config["design"]["work_dir"])
    result = work / "layout.json"
    run_pya({"operation": "inspect", "gds": str(source), "result": str(result)})
    try:
        layout = json.loads(result.read_text())
    except (OSError, ValueError) as exc:
        raise ProjectError(f"KLayout did not produce layout inspection: {exc}") from exc
    present = {(item["layer"], item["datatype"]) for item in layout["present_layers"]}
    layout["routing_present"] = [name for name in tech["routing"]
                                  if tuple(tech["symbols"].get(name, {}).get(key)
                                           for key in ("layer", "datatype")) in present]
    layout["top_metal"] = tech["top_metal"]
    return {"project": config.get("_project"), "gds": str(source),
            "work_dir": config["design"]["work_dir"], "technology": tech,
            "layout": layout, "logo": config.get("logo", {}),
            "render": config.get("render", {}), "map": config.get("map", {})}


def layer(value: object, tech: dict) -> tuple[str, int, int]:
    if value is None or str(value).lower() == "top-metal":
        name = tech["top_metal"]
        source = tech["symbols"].get(name)
        if not source:
            raise ProjectError("technology has no terminal metal source")
        return name, source["layer"], source["datatype"]
    text = str(value)
    if text.isdigit():
        return text, int(text), 0
    match = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", text)
    if match:
        return text, int(match.group(1)), int(match.group(2))
    if text not in tech["symbols"]:
        raise ProjectError(f"unknown technology layer {text!r}")
    source = tech["symbols"][text]
    return text, source["layer"], source["datatype"]


def selected_layers(section: dict, tech: dict, layout: dict) -> list[tuple[str, int, int]]:
    configured = section.get("layers", "routing")
    if isinstance(configured, str):
        names = list(tech["routing"]) if configured == "routing" else (
            list(tech["symbols"]) if configured == "all" else [configured])
    else:
        names = list(configured)
    names = [tech["top_metal"] if str(name).lower() == "top-metal" else str(name)
             for name in names]
    present = {(item["layer"], item["datatype"]) for item in layout["present_layers"]}
    selected = []
    for name in names:
        if name not in tech["symbols"]:
            raise ProjectError(f"unknown configured layer {name!r}")
        source = tech["symbols"][name]
        if (source["layer"], source["datatype"]) in present:
            selected.append((name, source["layer"], source["datatype"]))
    return selected


def palette(config: dict, section: dict, selected: list[tuple[str, int, int]]) -> dict:
    base = config.get("palettes", {}).get(section.get("palette", ""), {})
    result = {"background": base.get("background", "#ffffff")}
    for name, _, _ in selected:
        value = {"color": "#000000", "alpha": 1.0}
        value.update(base.get("layers", {}).get(name, {}))
        value.update(section.get("colors", {}).get(name, {}))
        result[name] = {"color": str(value.get("color", "#000000")),
                        "alpha": float(value.get("alpha", 1.0))}
    return result
