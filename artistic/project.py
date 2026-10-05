# Copyright 2026 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

"""Project loading and the importable ArtistIC facade."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import tomllib


class ProjectError(RuntimeError):
    """An actionable project or environment error."""


def _resolve(root: Path, value: str | os.PathLike[str]) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tool(*names: str) -> str:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    raise ProjectError("required tool not found: " + " or ".join(names))


def run_checked(command: list[str], cwd: Path | None = None,
                env: dict[str, str] | None = None) -> None:
    try:
        subprocess.run(command, cwd=cwd, check=True, env=env)
    except OSError as exc:
        raise ProjectError(f"cannot run {command[0]}: {exc}") from exc


def write_json(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    return path


def _project_name(config: dict) -> str:
    name = config.get("design", {}).get("name")
    return filename_component(str(name) if name else Path(config["design"]["gds"]).name.split(".")[0],
                              "[design].name")


def filename_component(value: str, label: str) -> str:
    if not value or value in (".", "..") or any(char in value for char in ("/", "\\", "\0")):
        raise ProjectError(f"{label} must be a filename component without path separators")
    return value


def input_gds(config: dict, section: str) -> Path:
    value = config.get(section, {}).get("input", "design")
    if value == "design":
        source = Path(config["design"]["gds"])
    elif value == "logo":
        from .logo import validate_merged
        source = validate_merged(config)
    else:
        source = Path(value)
    if not source.is_file():
        raise ProjectError(f"[{section}].input GDS not found: {source}")
    return source.resolve()


def work_relative(config: dict, path: Path) -> str:
    return os.path.relpath(path.resolve(), Path(config["design"]["work_dir"]).resolve())


def recorded_path(config: dict, value: str) -> Path:
    if Path(value).is_absolute():
        raise ProjectError("stage record uses absolute paths; generate it again")
    return (Path(config["design"]["work_dir"]) / value).resolve()


def project_relative(config: dict, path: Path) -> str:
    design = config["design"]
    root = Path(config["_root"] if "_root" in config else
                Path(design["gds"]).parent if "gds" in design else design["work_dir"]).resolve()
    return os.path.relpath(path.resolve(), root)


class Project:
    """A loaded ArtistIC project.

    The class is the public library interface.  Command parsing only loads a
    project and dispatches to these methods.
    """

    def __init__(self, path: Path, config: dict):
        self.path = path
        self.root = path.parent
        self.config = config

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "Project":
        project = Path(path).expanduser().resolve()
        if not project.is_file():
            raise ProjectError(f"project file not found: {project}")
        with project.open("rb") as stream:
            config = tomllib.load(stream)
        design = config.setdefault("design", {})
        if "gds" not in design:
            raise ProjectError("[design].gds is required")
        design["gds"] = str(_resolve(project.parent, design["gds"]))
        design["work_dir"] = str(_resolve(project.parent, design.get("work_dir", "work")))
        for section in ("logo", "render", "map"):
            values = config.setdefault(section, {})
            for key in ("source", "input"):
                value = values.get(key)
                if value and value not in ("design", "logo"):
                    values[key] = str(_resolve(project.parent, value))
        outlines = config.get("render", {}).get("outlines", {})
        if not isinstance(outlines, dict):
            raise ProjectError("[render.outlines] must be a table")
        if "def" in outlines:
            outlines["def"] = str(_resolve(project.parent, outlines["def"]))
        if "lef_files" in outlines:
            if not isinstance(outlines["lef_files"], list) or any(
                    not isinstance(value, str) for value in outlines["lef_files"]):
                raise ProjectError("[render.outlines].lef_files must be a list of paths or globs")
            outlines["lef_files"] = [str(_resolve(project.parent, value))
                                     for value in outlines["lef_files"]]
        config["_project"] = str(project)
        config["_root"] = str(project.parent)
        return cls(project, config)

    @property
    def work_dir(self) -> Path:
        return Path(self.config["design"]["work_dir"])

    @property
    def name(self) -> str:
        return _project_name(self.config)

    def inspect(self, technology: str | os.PathLike[str] | None = None) -> dict:
        from .technology import inspect_layout
        from .inspection import analyze, write_palette_preview
        manifest = inspect_layout(self.config, technology)
        layouts = {"render": {}, "map": {}}
        section_gds = {}
        geometry_source = {}
        design_gds = Path(self.config["design"]["gds"]).resolve()
        inspected = {design_gds: manifest["layout"]}
        logo_gds = self.work_dir / f"{self.name}_chip.gds.gz"
        merge_record = self.work_dir / "logo_merge.json"
        logo_requested = any(self.config.get(section, {}).get("input", "design") == "logo"
                             for section in ("render", "map"))
        logo_source = None
        resolved_layer = None
        if logo_requested or merge_record.is_file():
            logo_config = dict(self.config)
            logo_config["render"] = {**self.config.get("render", {}), "input": "logo"}
            try:
                logo_source = input_gds(logo_config, "render")
            except ProjectError:
                logo_source = None
            else:
                try:
                    record = json.loads(merge_record.read_text())
                except (OSError, ValueError):
                    record = None
                candidate = record.get("resolved_layer") if isinstance(record, dict) else None
                if (isinstance(candidate, dict) and isinstance(candidate.get("name"), str) and
                        isinstance(candidate.get("layer"), int) and
                        not isinstance(candidate.get("layer"), bool) and
                        isinstance(candidate.get("datatype"), int) and
                        not isinstance(candidate.get("datatype"), bool)):
                    resolved_layer = candidate
        for section in ("render", "map"):
            configured_input = self.config.get(section, {}).get("input", "design")
            if configured_input == "logo" and logo_source is None:
                source = design_gds
                source_layout = manifest["layout"]
                geometry_source[section] = (
                    "estimate (logo GDS is stale; prepare and merge required)" if logo_gds.is_file()
                    else "estimate (logo GDS not prepared)")
                layouts[section] = source_layout
                section_gds[section] = str(source)
                continue
            source = logo_source if configured_input == "logo" else input_gds(self.config, section)
            source_layout = inspected.get(source)
            if source_layout is None:
                source_layout = inspect_layout(self.config, technology, source)["layout"]
                inspected[source] = source_layout
            geometry_source[section] = "inspected"
            layouts[section] = source_layout
            section_gds[section] = str(source)
        manifest["section_layouts"] = layouts
        manifest["section_gds"] = section_gds
        manifest["geometry_source"] = geometry_source
        manifest = analyze(self.config, manifest)
        manifest["palette_preview"] = work_relative(
            self.config, write_palette_preview(self.config, manifest))
        manifest.pop("section_layouts", None)
        if resolved_layer is not None:
            manifest.setdefault("logo", {})["resolved_layer"] = resolved_layer
        write_json(self.work_dir / "manifest.json", manifest)
        return manifest

    def prepare_logo(self) -> Path:
        from .logo import prepare
        return prepare(self.config)

    def merge_logo(self, technology: str | os.PathLike[str] | None = None) -> Path:
        from .logo import merge
        return merge(self.config, technology)

    def generate_render(self, technology: str | os.PathLike[str] | None = None) -> Path:
        from .render import generate
        return generate(self.config, technology)

    def compose_render(self) -> list[Path]:
        from .render import compose
        return compose(self.config)

    def generate_map(self, technology: str | os.PathLike[str] | None = None) -> Path:
        from .render import generate
        return generate(self.config, technology, section="map")

    def annotate_render(self) -> list[Path]:
        from .outlines import annotate
        return annotate(self.config)

    def build_map(self) -> Path:
        from .map import build
        return build(self.config)

    def input_gds(self, section: str) -> Path:
        return input_gds(self.config, section)


__all__ = ["Project", "ProjectError", "run_checked", "sha256", "tool", "write_json"]
