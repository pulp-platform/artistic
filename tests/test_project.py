# Copyright 2026 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

import os
import json
import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from artistic import Project
from artistic.map import _safe_output, _viewer, build as build_map
from artistic.project import ProjectError, sha256
from artistic.render import generation_hash, preserve_logo_resolution, verify_raw
from artistic.technology import inspect_layout, resolve, selected_layers, technology_path


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

    def test_map_viewer_exposes_generated_views(self):
        html = _viewer({"layers": ["composite", "Metal1"], "height": 100,
                        "width": 200, "max_zoom": 2, "tile_size": 50})
        self.assertIn("L.control.layers(layers)", html)
        self.assertIn("map.unproject([200,0],maxZoom)", html)
        self.assertIn('"composite", "Metal1"', html)

    def test_map_output_cannot_follow_a_symlink_outside_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work, outside = root / "work", root / "outside"
            work.mkdir(); outside.mkdir()
            (work / "link").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ProjectError):
                _safe_output({"design": {"work_dir": str(work)},
                              "map": {"output": "link/map"}})

    def test_raw_hash_rejects_modified_stage_output(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory)
            image = raw / "RAW__chip_8.0.Metal1_0-0.png"
            image.write_bytes(b"first")
            settings = {"section": "render", "raw_dir": str(raw),
                        "raw_sha256": {image.name: hashlib.sha256(b"first").hexdigest()}}
            verify_raw(settings)
            image.write_bytes(b"changed")
            with self.assertRaises(ProjectError):
                verify_raw(settings)

    def test_generation_hash_ignores_host_side_options(self):
        config = {
            "render": {
                "input": "design", "resolution": [1000, 1000], "segments": [1, 1],
                "overrender": 2, "margin_um": 10, "layers": "routing",
                "palette": "first", "formats": ["png"],
                "colors": {"Metal1": {"alpha": 0.5}},
            },
            "palettes": {"first": {"background": "#ffffff"}},
        }
        original = generation_hash(config, "render")
        config["render"].update({"palette": "second", "formats": ["pdf"]})
        config["render"]["colors"]["Metal1"]["alpha"] = 0.9
        config["palettes"]["second"] = {"background": "#000000"}
        self.assertEqual(generation_hash(config, "render"), original)
        config["render"]["resolution"] = [2000, 1000]
        self.assertNotEqual(generation_hash(config, "render"), original)

        map_config = {"map": {"resolution": [100, 100], "views": ["metals"],
                              "tile_size": 512, "output": "first", "composite": True}}
        original = generation_hash(map_config, "map")
        map_config["map"].update({"views": ["composite"], "tile_size": 256,
                                  "output": "second", "composite": False})
        self.assertEqual(generation_hash(map_config, "map"), original)

    def test_later_stage_preserves_resolved_logo_layer(self):
        with tempfile.TemporaryDirectory() as directory:
            resolved = {"name": "TopMetal1", "layer": 126, "datatype": 0}
            (Path(directory) / "manifest.json").write_text(json.dumps(
                {"logo": {"resolved_layer": resolved}}
            ))
            manifest = {"logo": {}}
            preserve_logo_resolution({"design": {"work_dir": directory}}, manifest)
            self.assertEqual(manifest["logo"]["resolved_layer"], resolved)

    def test_map_build_reverses_klayout_y_and_writes_pyramid(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is not installed")

        with tempfile.TemporaryDirectory() as directory:
            root, raw = Path(directory), Path(directory) / "raw"
            raw.mkdir()
            source = root / "chip.gds"
            source.write_bytes(b"gds")
            config = {
                "design": {"name": "chip", "gds": str(source), "work_dir": str(root)},
                "map": {"input": "design", "resolution": [4, 4], "segments": [2, 2],
                        "layers": "routing", "output": "map", "tile_size": 2,
                        "views": ["metals"], "composite": True, "palette": "test"},
                "palettes": {"test": {"background": "#ffffff", "layers": {
                    "Metal1": {"color": "#ff0000", "alpha": 1.0},
                    "Metal2": {"color": "#0000ff", "alpha": 1.0},
                }}},
            }
            layers = [
                {"name": "Metal1", "layer": 8, "datatype": 0},
                {"name": "Metal2", "layer": 10, "datatype": 0},
            ]
            raw_hashes = {}
            for source_y in range(2):
                for source_x in range(2):
                    for layer in layers:
                        image = Image.new("L", (2, 2), 255)
                        if layer["name"] == "Metal1":
                            if (source_y, source_x) == (1, 0):
                                image.putpixel((0, 0), 0)
                            if (source_y, source_x) == (0, 0):
                                image.putpixel((1, 1), 0)
                        name = (f"RAW__chip_{layer['layer']}.0.{layer['name']}_"
                                f"{source_y}-{source_x}.png")
                        path = raw / name
                        image.save(path)
                        raw_hashes[name] = sha256(path)
            settings = {
                "section": "map", "chip": "chip", "input": str(source),
                "input_sha256": sha256(source),
                "generation_sha256": generation_hash(config, "map"),
                "raw_dir": str(raw), "raw_sha256": raw_hashes,
                "resolution": [4, 4], "segments": [2, 2], "layers": layers,
                "technology": {"top_metal": "Metal2"},
            }
            (root / "map.json").write_text(json.dumps(settings))
            stale = root / "map" / "stale.txt"
            stale.parent.mkdir()
            stale.write_text("old")
            output = build_map(config)
            self.assertFalse(stale.exists())
            metadata = json.loads((output / "map.json").read_text())
            self.assertEqual(metadata["layers"], ["composite", "Metal1", "Metal2"])
            self.assertTrue((output / "composite" / "0" / "0" / "0.png").is_file())
            top = Image.open(output / "composite" / "1" / "0" / "0.png").convert("RGB")
            bottom = Image.open(output / "composite" / "1" / "0" / "1.png").convert("RGB")
            self.assertEqual(top.getpixel((0, 0)), (255, 0, 0))
            self.assertEqual(bottom.getpixel((1, 1)), (255, 0, 0))
            config["palettes"]["test"]["layers"]["Metal1"]["color"] = "#00ff00"
            output = build_map(config)
            recolored = Image.open(output / "composite" / "1" / "0" / "0.png").convert("RGB")
            self.assertEqual(recolored.getpixel((0, 0)), (0, 255, 0))
            config["map"].update({"views": ["missing"], "composite": False})
            with self.assertRaises(ProjectError):
                build_map(config)


if __name__ == "__main__":
    unittest.main()
