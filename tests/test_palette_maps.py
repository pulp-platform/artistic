# Copyright 2026 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from artistic.map import _safe_output, build
from artistic.palettes import background_rgba, palette
from artistic.project import ProjectError, sha256
from artistic.render import generation_hash
from artistic.technology import palette as technology_palette


LAYERS = [("M1", 1, 0), ("M2", 2, 0), ("M3", 3, 0), ("M4", 4, 0)]


class PaletteTests(unittest.TestCase):
    def test_generation_uses_complete_routing_order(self):
        config = {"technology": {"routing": [item[0] for item in LAYERS]},
                  "palettes": {"p": {"generate": {"hue_start_deg": 0,
                      "saturation": 1, "lightness": [.5, .25], "alpha": [.8, .2]}}}}
        all_colors = palette(config, {"palette": "p"}, LAYERS)
        subset = technology_palette(config, {"palette": "p"}, [LAYERS[2]])
        self.assertEqual(subset["M3"], all_colors["M3"])
        extra = palette(config, {"palette": "p"}, [*LAYERS, ("Cap", 5, 0)])
        self.assertEqual(extra["M3"], all_colors["M3"])
        self.assertEqual(all_colors["M1"], {"color": "#ff0000", "alpha": .8})
        self.assertEqual(all_colors["M2"], {"color": "#408000", "alpha": .6})
        self.assertAlmostEqual(all_colors["M4"]["alpha"], .2)

    def test_explicit_color_rotation_then_section_override(self):
        config = {"palettes": {"p": {"hue_rotation_deg": 120,
                  "layers": {"M1": {"color": "red", "alpha": .5},
                             "M2": {"color": "blue"}}}}}
        result = palette(config, {"palette": "p", "colors": {
            "M2": {"color": "#abcdef", "alpha": .25}}}, LAYERS[:2])
        self.assertEqual(result["M1"], {"color": "#00ff00", "alpha": .5})
        self.assertEqual(result["M2"], {"color": "#abcdef", "alpha": .25})

    def test_background_and_invalid_values(self):
        self.assertEqual(background_rgba("white"), (255, 255, 255, 255))
        self.assertEqual(background_rgba("black"), (0, 0, 0, 255))
        self.assertEqual(background_rgba("transparent"), (0, 0, 0, 0))
        self.assertEqual(background_rgba("#12345678"), (18, 52, 86, 120))
        for value in ("no such color", 123):
            with self.subTest(value=value), self.assertRaises(ProjectError):
                background_rgba(value)
        for key, bad in (("saturation", 2), ("lightness", [-.1]),
                         ("alpha", [float("nan")]), ("hue_start_deg", float("inf"))):
            with self.subTest(key=key):
                config = {"palettes": {"p": {"generate": {key: bad}}}}
                with self.assertRaises(ProjectError):
                    palette(config, {"palette": "p"}, LAYERS[:1])
        for bad in (-.1, 1.1, float("nan"), True):
            with self.subTest(alpha=bad):
                config = {"palettes": {"p": {"layers": {"M1": {"alpha": bad}}}}}
                with self.assertRaises(ProjectError):
                    palette(config, {"palette": "p"}, LAYERS[:1])


class MapStyleTests(unittest.TestCase):
    def _fixture(self, root: Path, background="white"):
        raw = root / "raw"
        raw.mkdir()
        source = root / "chip.gds"
        source.write_bytes(b"gds")
        config = {"design": {"name": "chip", "gds": str(source),
                             "work_dir": str(root)},
                  "technology": {"routing": ["M1"]},
                  "map": {"resolution": [4, 4], "segments": [1, 1],
                          "tile_size": 2, "views": ["M1"], "palette": "p"},
                  "palettes": {"p": {"background": background,
                                     "layers": {"M1": {"color": "red", "alpha": .5}}}}}
        image = Image.new("L", (4, 4), 255)
        image.putpixel((0, 0), 0)
        image.putpixel((3, 3), 0)
        raw_path = raw / "RAW__chip_1.0.M1_0-0.png"
        image.save(raw_path)
        settings = {"record_version": 2, "section": "map", "chip": "chip",
                    "input": "chip.gds", "input_sha256": sha256(source),
                    "generation_sha256": generation_hash(config, "map"),
                    "raw_dir": "raw", "raw_sha256": {raw_path.name: sha256(raw_path)},
                    "resolution": [4, 4], "segments": [1, 1],
                    "layers": [{"name": "M1", "layer": 1, "datatype": 0}],
                    "technology": {"top_metal": "M1", "routing": ["M1"]}}
        (root / "map.json").write_text(json.dumps(settings))
        return config

    def _pixel(self, output: Path, view: str, zoom: int, x: int, y: int):
        with Image.open(output / view / str(zoom) / "0" / "0.png") as image:
            return image.convert("RGBA").getpixel((x, y))

    def test_default_mask_and_composite_backgrounds(self):
        for background, expected_blank in (("white", (255, 255, 255, 255)),
                                           ("black", (0, 0, 0, 255)),
                                           ("transparent", (0, 0, 0, 0))):
            with self.subTest(background=background), tempfile.TemporaryDirectory() as directory:
                config = self._fixture(Path(directory), background)
                output = build(config)
                self.assertEqual(self._pixel(output, "M1", 1, 0, 0), (0, 0, 0, 255))
                self.assertEqual(self._pixel(output, "M1", 1, 1, 1), (255, 255, 255, 255))
                self.assertEqual(self._pixel(output, "composite", 1, 1, 1), expected_blank)
                self.assertEqual(self._pixel(output, "M1", 0, 1, 1)[3], 255)
                if background == "transparent":
                    self.assertEqual(self._pixel(output, "composite", 1, 0, 0),
                                     (255, 0, 0, 127))
                    self.assertEqual(self._pixel(output, "composite", 0, 1, 0)[3], 0)

    def test_color_view_opt_in_and_validation_before_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._fixture(Path(directory))
            config["map"]["layer_style"] = "color"
            output = build(config)
            self.assertEqual(self._pixel(output, "M1", 1, 0, 0), (255, 0, 0, 127))
            self.assertEqual(self._pixel(output, "M1", 1, 1, 1)[3], 0)
            self.assertEqual(self._pixel(output, "M1", 0, 1, 0)[3], 0)
            marker = output / "sentinel"
            marker.write_text("kept")
            config["map"]["layer_style"] = "invalid"
            with self.assertRaisesRegex(ProjectError, "layer_style"):
                build(config)
            self.assertTrue(marker.exists())
            config["map"]["layer_style"] = "mask"
            config["palettes"]["p"]["background"] = "invalid"
            with self.assertRaisesRegex(ProjectError, "background"):
                build(config)
            self.assertTrue(marker.exists())

    def test_partial_background_across_segments_and_zoom(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._fixture(root, "#ffffff80")
            config["map"].update({"segments": [2, 1], "tile_size": 3})
            raw = root / "raw"
            left = raw / "RAW__chip_1.0.M1_0-0.png"
            right = raw / "RAW__chip_1.0.M1_0-1.png"
            Image.new("L", (2, 4), 255).save(left)
            Image.new("L", (2, 4), 255).save(right)
            settings = json.loads((root / "map.json").read_text())
            settings["segments"] = [2, 1]
            settings["generation_sha256"] = generation_hash(config, "map")
            settings["raw_sha256"] = {path.name: sha256(path) for path in (left, right)}
            (root / "map.json").write_text(json.dumps(settings))
            output = build(config)
            self.assertEqual(self._pixel(output, "composite", 1, 2, 1),
                             (255, 255, 255, 128))
            self.assertEqual(self._pixel(output, "composite", 0, 0, 0),
                             (255, 255, 255, 128))
            with Image.open(right) as image:
                feature = image.copy()
            feature.putpixel((0, 1), 0)
            feature.save(right)
            settings["raw_sha256"][right.name] = sha256(right)
            (root / "map.json").write_text(json.dumps(settings))
            output = build(config)
            self.assertEqual(self._pixel(output, "composite", 1, 2, 1)[3], 191)
            self.assertEqual(self._pixel(output, "composite", 1, 1, 1)[3], 128)

    def test_palette_and_style_do_not_change_generation_hash(self):
        config = {"map": {"resolution": [10, 10]},
                  "palettes": {"p": {"background": "white"}}}
        original = generation_hash(config, "map")
        config["map"].update({"palette": "p", "layer_style": "color"})
        config["palettes"]["p"]["generate"] = {"hue_rotation_deg": 30}
        self.assertEqual(generation_hash(config, "map"), original)

    def test_output_guard_protects_new_render_outputs_and_outline_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / "work"
            work.mkdir()
            source = root / "chip.gds"
            source.write_bytes(b"gds")
            def_file = work / "placement.def"
            def_file.write_text("DEF")
            lef_dir = work / "lef"
            lef_dir.mkdir()
            lef = lef_dir / "macro.lef"
            lef.write_text("LEF")
            config = {"_root": str(root), "design": {
                "name": "chip", "gds": str(source), "work_dir": str(work)},
                "render": {"outlines": {"def": "work/placement.def",
                                        "lef_files": ["work/lef/*.lef"]}},
                "map": {}}
            config["render"]["formats"] = ["jpg"]
            for output in ("render_output.json", "chip_poster.pdf",
                           "chip_modules.svg", "chip_modules.png",
                           "chip_modules.pdf", "chip_modules.jpg",
                           "chip_render.png", "chip_render.pdf", "chip_render.jpeg",
                           "placement.def", "lef", "lef/macro.lef"):
                with self.subTest(output=output):
                    config["map"]["output"] = output
                    with self.assertRaisesRegex(ProjectError, "overlaps protected"):
                        _safe_output(config)
            self.assertEqual(def_file.read_text(), "DEF")
            self.assertEqual(lef.read_text(), "LEF")


if __name__ == "__main__":
    unittest.main()
