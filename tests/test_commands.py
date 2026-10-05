# Copyright 2026 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

import contextlib
import io
import json
import unittest
from unittest.mock import Mock, patch

from artistic.commands import main


class CommandTests(unittest.TestCase):
    def test_optional_annotation_does_not_hide_configured_errors(self):
        project = Mock()
        for settings in ({}, {"outlines": {"enabled": False}}):
            project.config = {"render": settings}
            with patch("artistic.commands.Project.load", return_value=project):
                self.assertEqual(main(["render", "annotate", "project.toml", "--if-configured"]), 0)
        project.annotate_render.assert_not_called()
        project.config = {"render": {"outlines": {"def": "chip.def"}}}
        project.annotate_render.return_value = []
        with patch("artistic.commands.Project.load", return_value=project):
            self.assertEqual(main(["render", "annotate", "project.toml", "--if-configured"]), 0)
        project.annotate_render.assert_called_once()

    def test_inspect_text_and_json(self):
        project = Mock()
        project.inspect.return_value = {"layout": {"top_cell": "chip"}}
        with patch("artistic.commands.Project.load", return_value=project):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["inspect", "project.toml", "--json"]), 0)
            self.assertEqual(json.loads(output.getvalue()), project.inspect.return_value)
            with patch("artistic.inspection.format_summary", return_value="Scale: 1000 nm/px"), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["inspect", "project.toml"]), 0)
            self.assertIn("1000 nm/px", output.getvalue())
