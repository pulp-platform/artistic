# Copyright 2026 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

"""ArtistIC's project-oriented layout artwork flow."""

import sys

if sys.version_info < (3, 11):
    raise RuntimeError("ArtistIC requires Python 3.11 or newer")

from .project import Project, ProjectError

__all__ = ["Project", "ProjectError"]
