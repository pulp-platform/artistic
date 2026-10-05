# Copyright 2026 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

"""Thin command adapter for the ArtistIC project library."""

from __future__ import annotations

import argparse
import json
import subprocess

from .project import Project, ProjectError


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="artistic", description="ArtistIC project flow")
    groups = command.add_subparsers(dest="group")
    for group, stages in {"logo": ("prepare", "merge"),
                          "render": ("generate", "compose", "annotate"),
                          "map": ("generate", "build")}.items():
        sub = groups.add_parser(group, help=f"{group} stages")
        stages_parser = sub.add_subparsers(dest="stage")
        for stage in stages:
            item = stages_parser.add_parser(stage)
            item.add_argument("project", metavar="PROJECT.toml")
            if stage in ("merge", "generate"):
                item.add_argument("--technology", help="KLayout technology file")
            if stage == "annotate":
                item.add_argument("--if-configured", action="store_true",
                                  help="skip annotation when no enabled outlines are configured")
    item = groups.add_parser("inspect", help="inspect layout and technology")
    item.add_argument("project", metavar="PROJECT.toml")
    item.add_argument("--technology", help="KLayout technology file")
    item.add_argument("--json", action="store_true", help="print the inspection report as JSON")
    return command


def main(argv: list[str] | None = None) -> int:
    command = parser()
    args = command.parse_args(argv)
    if not args.group or (args.group != "inspect" and not args.stage):
        command.error("choose inspect, logo, render, or map and a stage")
    try:
        project = Project.load(args.project)
        technology = getattr(args, "technology", None)
        if args.group == "inspect":
            from .inspection import format_summary
            report = project.inspect(technology)
            print(json.dumps(report, indent=2, sort_keys=True) if args.json else format_summary(report))
        elif args.group == "logo" and args.stage == "prepare":
            print(project.prepare_logo())
        elif args.group == "logo":
            print(project.merge_logo(technology))
            report = json.loads((project.work_dir / "logo_merge.json").read_text())
            print(f"Logo: {report['accepted_pixels']} features, "
                  f"{100 * report['logo_density']:.2f}% metal density in the logo canvas")
        elif args.group == "render" and args.stage == "generate":
            print(project.generate_render(technology))
        elif args.group == "render" and args.stage == "annotate":
            outlines = project.config.get("render", {}).get("outlines")
            if args.if_configured and (not outlines or not outlines.get("enabled", True)):
                return 0
            print(*project.annotate_render(), sep="\n")
        elif args.group == "render":
            print(*project.compose_render(), sep="\n")
        elif args.group == "map" and args.stage == "generate":
            print(project.generate_map(technology))
        else:
            print(project.build_map())
        return 0
    except ProjectError as exc:
        command.error(str(exc))
    except subprocess.CalledProcessError as exc:
        print(f"command failed ({exc.returncode}): {exc.cmd}")
        return exc.returncode or 1
    return 1
