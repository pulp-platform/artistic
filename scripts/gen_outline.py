# Copyright 2026 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

"""Compatibility entry point for ArtistIC's project-based outline stage."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from artistic import Project, ProjectError  # noqa: E402
from artistic.outlines import annotate  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Annotate an ArtistIC render")
    parser.add_argument("--project", required=True, help="ArtistIC project TOML file")
    args = parser.parse_args()
    try:
        for path in annotate(Project.load(args.project).config):
            print(path)
    except ProjectError as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    main()
