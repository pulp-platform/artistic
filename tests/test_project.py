# Copyright 2026 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

import os
import json
import hashlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from artistic import Project
from artistic.project import ProjectError, sha256
from artistic.technology import (inspect_layout, resolve, resolve_project,
                                 selected_layers, technology_path)
from artistic.logo import prepare as prepare_logo


TECH = """<technology><connectivity>
<connection>Cont,Metal1</connection><connection>Metal1,Via1,Metal2</connection>
<connection>Metal2,TopVia1,TopMetal1</connection>
<symbols>Cont='6/0'</symbols><symbols>Metal1='8/0'</symbols>
<symbols>Via1='19/0'</symbols><symbols>Metal2='10/0'</symbols>
<symbols>TopVia1='125/0'</symbols><symbols>TopMetal1='126/0'</symbols>
</connectivity></technology>"""


class ProjectTests(unittest.TestCase):
    def test_load_resolves_paths_and_exposes_facade(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tech.lyt").write_text(TECH)
            (root / "chip.gds").touch()
            project_file = root / "nested" / "project.toml"
            project_file.parent.mkdir()
            project_file.write_text("""[design]
gds = "../chip.gds"
work_dir = "out"
[technology]
file = "../tech.lyt"
""")
            project = Project.load(project_file)
            self.assertEqual(project.config["design"]["gds"], str((root / "chip.gds").resolve()))
            self.assertEqual(project.config["technology"]["file"], "../tech.lyt")

    def test_technology_stack_and_priority(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root / "first.lyt", root / "second.lyt"
            first.write_text(TECH); second.write_text(TECH)
            os.environ["KLAYOUT_TECH_FILE"] = str(second)
            try:
                self.assertEqual(technology_path(first, root), first.resolve())
                tech = resolve(first)
                self.assertEqual(tech["routing"], ["Cont", "Metal1", "Via1", "Metal2", "TopVia1", "TopMetal1"])
            finally:
                os.environ.pop("KLAYOUT_TECH_FILE", None)

    def test_technology_path_searches_each_klayout_path_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root / "first", root / "second"
            technology = second / "tech" / "example.lyt"
            technology.parent.mkdir(parents=True)
            technology.write_text(TECH)
            with patch.dict(os.environ, {
                    "KLAYOUT_PATH": os.pathsep.join((str(first), str(second))),
                    "KLAYOUT_TECH": "example",
            }, clear=True):
                self.assertEqual(technology_path(None, root), technology.resolve())

    def test_project_defined_technology_needs_no_technology_file(self):
        config = {
            "_root": "/missing",
            "technology": {
                "layers": {
                    "M0": "180/250",
                    "V0": {"layer": 159, "datatype": 250},
                    "M1": "31/250",
                },
                "routing": ["M0", "V0", "M1"],
            },
        }
        with patch.dict(os.environ, {}, clear=True):
            technology = resolve_project(config)
        self.assertIsNone(technology["technology"])
        self.assertEqual(technology["symbols"]["M0"],
                         {"layer": 180, "datatype": 250})
        self.assertEqual(technology["symbols"]["V0"],
                         {"layer": 159, "datatype": 250})
        self.assertEqual(technology["top_metal"], "M1")

    def test_project_technology_overrides_file_symbols_and_stack(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            technology = root / "tech.lyt"
            technology.write_text(TECH)
            config = {
                "_root": str(root),
                "technology": {
                    "file": "tech.lyt",
                    "layers": {"Metal2": "99/7", "Cap": "101/3"},
                    "routing": ["Metal1", "Metal2", "Cap"],
                    "top_metal": "Cap",
                },
            }
            resolved = resolve_project(config)
        self.assertEqual(resolved["symbols"]["Metal2"],
                         {"layer": 99, "datatype": 7})
        self.assertEqual(resolved["routing"], ["Metal1", "Metal2", "Cap"])
        self.assertEqual(resolved["top_metal"], "Cap")

    def test_explicit_routing_defaults_top_metal_with_technology_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tech.lyt"
            path.write_text(TECH)
            config = {"_root": directory, "technology": {
                "file": "tech.lyt", "routing": ["Metal1", "Via1", "Metal2"],
            }}
            self.assertEqual(resolve_project(config)["top_metal"], "Metal2")
            config["technology"]["top_metal"] = "TopMetal1"
            self.assertEqual(resolve_project(config)["top_metal"], "TopMetal1")

    def test_project_technology_rejects_invalid_definitions(self):
        cases = [
            ({"layers": {"M1": "bad"}, "routing": ["M1"]},
             "numeric source"),
            ({"layers": {"M1": {"layer": -1}}, "routing": ["M1"]},
             "nonnegative integer"),
            ({"layers": {"M1": "1/0"}}, "routing is required"),
            ({"layers": {"M1": "1/0"}, "routing": ["missing"]},
             "undefined layers"),
            ({"layers": {"M1": "1/0"}, "routing": ["M1"],
              "top_metal": "missing"}, "undefined layer"),
        ]
        for settings, message in cases:
            with self.subTest(settings=settings), patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ProjectError, message):
                    resolve_project({"_root": "/missing", "technology": settings})

    def test_explicit_missing_technology_file_is_not_ignored(self):
        config = {"_root": "/missing", "technology": {
            "file": "missing.lyt", "layers": {"M1": "1/0"},
            "routing": ["M1"],
        }}
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ProjectError, "not found"):
                resolve_project(config)

    def test_inspection_does_not_report_absent_terminal_metal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            technology, gds = root / "tech.lyt", root / "chip.gds"
            technology.write_text(TECH)
            gds.touch()
            config = {"_root": str(root), "_project": str(root / "project.toml"),
                      "design": {"gds": str(gds), "work_dir": str(root / "work")}}

            def fake_inspect(request):
                Path(request["result"]).parent.mkdir(parents=True)
                Path(request["result"]).write_text(json.dumps({
                    "dbu": 0.001, "top_cell": "chip", "bbox_dbu": [0, 0, 10, 10],
                    "bbox_um": [0, 0, 0.01, 0.01],
                    "present_layers": [{"layer": 8, "datatype": 0,
                                        "source": "8/0", "shapes": 1}],
                }))

            with patch("artistic.technology.run_pya", side_effect=fake_inspect):
                manifest = inspect_layout(config, technology)
            self.assertEqual(manifest["layout"]["routing_present"], ["Metal1"])
            self.assertEqual(manifest["layout"]["top_metal"], "TopMetal1")

    def test_bundled_ihp_routing_stacks(self):
        root = Path(__file__).resolve().parents[3]
        cases = [
            (root / "ihp13/sg13cmos5l/libs.tech/klayout/tech/sg13cmos5l.lyt",
             "TopMetal1"),
            (root / "ihp13/sg13g2/ihp-sg13g2/libs.tech/klayout/tech/sg13g2.lyt",
             "TopMetal2"),
        ]
        for technology, terminal in cases:
            if not technology.is_file():
                self.skipTest("IHP technology checkout is not present")
            resolved = resolve(technology)
            self.assertEqual(resolved["top_metal"], terminal)
            self.assertIn("Metal1", resolved["routing"])

    def test_layer_alias_and_help(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".lyt") as stream:
            stream.write(TECH)
            stream.flush()
            tech = resolve(Path(stream.name))
            self.assertIn("Metal1", tech["routing"])
        script = Path(__file__).parents[1] / "bin" / "artistic"
        result = subprocess.run([str(script), "render", "compose", "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)

    def test_logo_prepare_bitmap_flattens_thresholds_and_resizes(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is not installed")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "logo.png"
            with Image.new("RGBA", (3, 1)) as artwork:
                artwork.putdata(((0, 0, 0, 255), (0, 0, 0, 0),
                                 (128, 128, 128, 255)))
                artwork.save(source)
            config = {"design": {"name": "chip", "work_dir": str(root)},
                      "logo": {"source": str(source), "width_um": 3,
                               "height_um": 1, "feature_um": 1}}
            with patch("artistic.logo.tool", side_effect=AssertionError("external tool")):
                output = prepare_logo(config)
            with Image.open(output) as mask:
                self.assertEqual(mask.size, (3, 1))
                self.assertEqual(list(mask.getdata()), [0, 255, 255])
            record = json.loads((root / "logo_prepare.json").read_text())
            self.assertEqual(record["mask_sha256"], sha256(output))

            config["logo"]["width_um"] = 6
            output = prepare_logo(config)
            with Image.open(output) as mask:
                self.assertEqual(mask.size, (6, 1))
                self.assertEqual(set(mask.getdata()), {0, 255})

    def test_logo_prepare_svg_keeps_template_and_inkscape(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is not installed")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "logo.svg"
            source.write_text("<svg>{{repository}}</svg>")
            config = {"_root": str(root),
                      "design": {"name": "chip", "work_dir": str(root),
                                 "repository": "example/repo"},
                      "logo": {"source": str(source), "width_um": 2,
                               "height_um": 2, "feature_um": 1}}
            def rasterize(command):
                self.assertEqual(command[0], "inkscape")
                self.assertIn("--export-width=2", command)
                self.assertIn("--export-height=2", command)
                rendered = Path(next(value.split("=", 1)[1] for value in command
                                     if value.startswith("--export-filename=")))
                Image.new("RGB", (2, 2), "black").save(rendered)

            with (patch("artistic.logo.tool", return_value="inkscape") as tool,
                  patch("artistic.logo.run_checked", side_effect=rasterize) as run):
                output = prepare_logo(config)
            tool.assert_called_once_with("inkscape")
            run.assert_called_once()
            self.assertIn("example/repo", (root / "chip_logo.svg").read_text())
            with Image.open(output) as mask:
                self.assertEqual(list(mask.getdata()), [0] * 4)

