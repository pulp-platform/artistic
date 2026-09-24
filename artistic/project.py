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
        source = Path(config["design"]["work_dir"]) / f"{_project_name(config)}_chip.gds.gz"
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
        manifest = inspect_layout(self.config, technology)
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

    def build_map(self) -> Path:
        from .map import build
        return build(self.config)

    def input_gds(self, section: str) -> Path:
        return input_gds(self.config, section)


__all__ = ["Project", "ProjectError", "run_checked", "sha256", "tool", "write_json"]
