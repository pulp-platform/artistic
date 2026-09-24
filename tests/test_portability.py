# Copyright 2026 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from artistic import Project
from artistic.project import ProjectError


class PortabilityTests(unittest.TestCase):
    def test_stage_records_survive_different_project_prefixes(self):
        klayout = shutil.which("klayout")
        if not klayout:
            self.skipTest("KLayout is not installed")
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is not installed")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            a, b = root / "host" / "project", root / "container" / "project"
            (a / "source").mkdir(parents=True)
            (a / "art").mkdir()
            creator = root / "create.py"
            creator.write_text("""import os
import pya
layout = pya.Layout()
layout.dbu = 0.001
top = layout.create_cell('TOP')
top.shapes(layout.layer(8, 0)).insert(pya.Box(0, 0, 200, 200))
top.shapes(layout.layer(100, 0)).insert(pya.Box(0, 0, 1000, 1000))
layout.write(os.environ['ARTISTIC_TEST_GDS'])
""")
            gds = a / "source" / "chip.gds"
            subprocess.run([klayout, "-b", "-r", str(creator)], check=True,
                           env={**os.environ, "ARTISTIC_TEST_GDS": str(gds)},
                           capture_output=True, text=True)
            shutil.copyfile(gds, a / "source" / "alternate.gds")
            shutil.copyfile(gds, a / "source" / "custom.gds")
            Image.new("RGB", (2, 2), "black").save(a / "art" / "logo.png")
            (a / "project.toml").write_text("""[design]
name = "chip"
gds = "source/chip.gds"
work_dir = "work"
[technology]
routing = ["Metal1"]
[technology.layers]
Metal1 = "8/0"
[logo]
source = "art/logo.png"
width_um = 0.2
height_um = 0.2
feature_um = 0.1
layer = "Metal1"
[render]
input = "source/custom.gds"
resolution = [16, 16]
segments = [1, 1]
formats = ["png"]
[map]
input = "design"
resolution = [16, 16]
segments = [1, 1]
tile_size = 16
""")
            Project.load(a / "project.toml").prepare_logo()
            b.parent.mkdir()
            shutil.copytree(a, b)
            shutil.rmtree(a)
            project = Project.load(b / "project.toml")
            mask = project.work_dir / "other_logo_mono.png"
            shutil.copyfile(project.work_dir / "chip_logo_mono.png", mask)
            project.config["design"]["name"] = "other"
            with self.assertRaisesRegex(ProjectError, "logo settings changed"):
                project.merge_logo()
            project.config["design"]["name"] = "chip"
            project.merge_logo()
            project.config["render"]["input"] = "design"
            project.generate_render()
            project.generate_map()
            for section in ("render", "map"):
                record = json.loads((project.work_dir / f"{section}.json").read_text())
                self.assertEqual(record["record_version"], 2)
                self.assertFalse(Path(record["input"]).is_absolute())
                self.assertFalse(Path(record["raw_dir"]).is_absolute())
            project.config["design"]["gds"] = str(b / "source" / "alternate.gds")
            with self.assertRaisesRegex(ProjectError, "settings changed"):
                project.compose_render()
            with self.assertRaisesRegex(ProjectError, "settings changed"):
                project.build_map()
            project.config["design"]["gds"] = str(b / "source" / "chip.gds")
            project.config["render"]["input"] = str(b / "source" / "custom.gds")
            project.generate_render()
            a.parent.mkdir(exist_ok=True)
            shutil.copytree(b, a)
            shutil.rmtree(b)
            project = Project.load(a / "project.toml")
            self.assertTrue(project.compose_render()[0].is_file())
            self.assertTrue((project.build_map() / "index.html").is_file())
            project.config["render"]["input"] = str(a / "source" / "alternate.gds")
            with self.assertRaisesRegex(ProjectError, "settings changed"):
                project.compose_render()
            project.config["design"]["gds"] = str(a / "source" / "alternate.gds")
            with self.assertRaisesRegex(ProjectError, "settings changed"):
                project.build_map()

    def test_launcher_uses_active_python3_or_explicit_python(self):
        script = Path(__file__).parents[1] / "bin" / "artistic"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "selected.txt"
            for name in ("python3", "override"):
                wrapper = root / name
                wrapper.write_text(f"#!/bin/sh\nprintf '{name}:%s\\n' \"${{PYTHONNOUSERSITE-unset}}\" >> \"$ARTISTIC_TEST_LOG\"\nexec {sys.executable} \"$@\"\n")
                wrapper.chmod(0o755)
            env = {**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"],
                   "ARTISTIC_TEST_LOG": str(log)}
            env.pop("PYTHON", None)
            env.pop("PYTHONNOUSERSITE", None)
            result = subprocess.run([str(script), "--help"], env=env,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(log.read_text().splitlines(), ["python3:unset"] * 2)
            log.unlink()
            env["PYTHON"] = str(root / "override")
            result = subprocess.run([str(script), "--help"], env=env,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(log.read_text().splitlines(), ["override:unset"] * 2)
            old = root / "old-python"
            old.write_text("#!/bin/sh\nexit 1\n")
            old.chmod(0o755)
            env["PYTHON"] = str(old)
            result = subprocess.run([str(script), "--help"], env=env,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("requires Python 3.11 or newer", result.stderr)


if __name__ == "__main__":
    unittest.main()
