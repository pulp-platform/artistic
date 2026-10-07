# Copyright 2026 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from artistic.map import _parent_tile, _safe_output, _viewer, build
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

    def test_build_parents_match_shared_downsample(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._fixture(root, "transparent")
            config["map"].update({"resolution": [128, 128], "tile_size": 32,
                                  "layer_style": "color"})
            raw_path = root / "raw" / "RAW__chip_1.0.M1_0-0.png"
            with Image.new("L", (128, 128), 255) as source:
                draw = ImageDraw.Draw(source)
                draw.line((63, 0, 65, 127), fill=0, width=3)
                draw.line((0, 64, 127, 62), fill=0, width=3)
                source.save(raw_path)
            settings = json.loads((root / "map.json").read_text())
            settings["resolution"] = [128, 128]
            settings["generation_sha256"] = generation_hash(config, "map")
            settings["raw_sha256"] = {raw_path.name: sha256(raw_path)}
            (root / "map.json").write_text(json.dumps(settings))
            output = build(config)
            metadata = json.loads((output / "map.json").read_text())
            self.assertEqual(metadata, {"layers": ["composite", "M1"], "tile_size": 32,
                                        "max_zoom": 2, "width": 128, "height": 128})
            for view in metadata["layers"]:
                with self.subTest(view=view), Image.new("RGBA", (144, 144)) as shared:
                    for tx in range(4):
                        for ty in range(4):
                            with Image.open(output / view / "2" / str(tx) / f"{ty}.png") as child:
                                shared.paste(child, (8 + tx * 32, 8 + ty * 32))
                    with shared.resize((72, 72), Image.Resampling.LANCZOS) as reduced:
                        with reduced.crop((4, 4, 68, 68)) as expected:
                            with Image.new("RGBA", (64, 64)) as stitched:
                                for tx in range(2):
                                    for ty in range(2):
                                        with Image.open(output / view / "1" / str(tx) / f"{ty}.png") as parent:
                                            stitched.paste(parent, (tx * 32, ty * 32))
                                self.assertTrue(stitched.tobytes() == expected.tobytes(),
                                                "Built parent tiles differ from shared downsample")
                                with Image.new("RGBA", (80, 80)) as final_shared:
                                    final_shared.paste(stitched, (8, 8))
                                    with final_shared.resize((40, 40), Image.Resampling.LANCZOS) as final_reduced:
                                        with final_reduced.crop((4, 4, 36, 36)) as final_expected:
                                            with Image.open(output / view / "0" / "0" / "0.png") as final_parent:
                                                self.assertTrue(final_parent.tobytes() == final_expected.tobytes(),
                                                                "Final parent differs from shared downsample")
                self.assertEqual(len(list((output / view).rglob("*.png"))), 21)

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


class MapPyramidTests(unittest.TestCase):
    def _assert_shared_downsample(self, tile_size, fill, child_shape=(4, 4), missing=()):
        with tempfile.TemporaryDirectory() as directory:
            layer_root = Path(directory)
            extent = 4 * tile_size
            halo = 8
            children = []
            with Image.new("RGBA", (extent, extent), fill) as source:
                draw = ImageDraw.Draw(source)
                boundary = 2 * tile_size
                draw.rectangle((boundary - 3, 0, boundary + 1, extent - 1),
                               fill=(25, 115, 240, 96))
                draw.rectangle((0, boundary - 1, extent - 1, boundary + 2),
                               fill=(230, 40, 70, 255))
                draw.line((0, extent - 1, extent - 1, 0), fill=(50, 200, 30, 180), width=3)
                draw.rectangle((boundary + 2, boundary - 8, boundary + 3, boundary + 8),
                               fill=(0, 255, 0, 0))
                with Image.new("RGBA", (extent + 2 * halo, extent + 2 * halo), fill) as shared:
                    for child_x in range(child_shape[0]):
                        for child_y in range(child_shape[1]):
                            if (child_x, child_y) in missing:
                                continue
                            path = layer_root / "1" / str(child_x) / f"{child_y}.png"
                            path.parent.mkdir(parents=True, exist_ok=True)
                            with source.crop((child_x * tile_size, child_y * tile_size,
                                              (child_x + 1) * tile_size,
                                              (child_y + 1) * tile_size)) as child:
                                child.save(path)
                                children.append(path)
                                shared.paste(child, (halo + child_x * tile_size,
                                                     halo + child_y * tile_size))
                    # Files outside the declared child grid must not enter the halo.
                    for child_x, child_y in ((-1, 0), (0, -1), (child_shape[0], 0),
                                              (0, child_shape[1])):
                        path = layer_root / "1" / str(child_x) / f"{child_y}.png"
                        path.parent.mkdir(parents=True, exist_ok=True)
                        with Image.new("RGBA", (tile_size, tile_size), "magenta") as decoy:
                            decoy.save(path)
                    with shared.resize((extent // 2 + halo, extent // 2 + halo),
                                       Image.Resampling.LANCZOS) as reduced:
                        with reduced.crop((halo // 2, halo // 2,
                                           halo // 2 + extent // 2,
                                           halo // 2 + extent // 2)) as expected:
                            with Image.new("RGBA", expected.size) as stitched:
                                for tx in range(2):
                                    for ty in range(2):
                                        with _parent_tile(layer_root, 1, tx, ty, tile_size,
                                                          child_shape, fill) as parent:
                                            self.assertEqual(parent.size, (tile_size, tile_size))
                                            stitched.paste(parent, (tx * tile_size, ty * tile_size))
                                self.assertTrue(stitched.tobytes() == expected.tobytes(),
                                                "Stitched parents differ from shared downsample")
                                # Hidden RGB must not affect premultiplied-alpha filtering.
                                for path in children:
                                    with Image.open(path) as child:
                                        with child.convert("RGBA") as rgba:
                                            with rgba.getchannel("A") as alpha:
                                                with alpha.point(lambda value: 255 if value == 0 else 0) as hidden:
                                                    rgba.paste((0, 0, 0, 0), (0, 0, tile_size, tile_size), hidden)
                                            rgba.save(path)
                                for tx in range(2):
                                    for ty in range(2):
                                        with _parent_tile(layer_root, 1, tx, ty, tile_size,
                                                          child_shape, fill) as parent:
                                            with expected.crop((tx * tile_size, ty * tile_size,
                                                                (tx + 1) * tile_size,
                                                                (ty + 1) * tile_size)) as reference:
                                                self.assertTrue(parent.tobytes() == reference.tobytes(),
                                                                "Hidden RGB changes parent downsample")

    def test_adjacent_parents_match_shared_rgba_downsample(self):
        for tile_size in (31, 32, 512):
            for fill in ((255, 255, 255, 255), (0, 0, 0, 0), (15, 30, 45, 128)):
                with self.subTest(tile_size=tile_size, fill=fill):
                    self._assert_shared_downsample(tile_size, fill)

    def test_sparse_edges_match_shared_downsample(self):
        for fill in ((255, 255, 255, 255), (0, 0, 0, 0), (15, 30, 45, 128)):
            with self.subTest(fill=fill):
                self._assert_shared_downsample(32, fill, child_shape=(3, 3), missing=((1, 1),))

    def test_viewer_fits_small_viewports_without_changing_layer_names(self):
        html = _viewer({"layers": ["composite", "TopMetal2"], "tile_size": 512,
                        "max_zoom": 3, "width": 3500, "height": 2800})
        self.assertIn('<meta name="viewport" content="width=device-width, initial-scale=1">', html)
        self.assertIn("body{margin:0}#map{position:fixed;inset:0}", html)
        self.assertIn("crs:L.CRS.Simple,minZoom:-5,maxZoom", html)
        self.assertIn("minZoom:-5,minNativeZoom:0,maxNativeZoom:maxZoom", html)
        self.assertIn("collapsed:!L.Browser.touch", html)
        self.assertIn('names=["composite", "TopMetal2"]', html)
        self.assertIn("`${n}/{z}/{x}/{y}.png`", html)
        self.assertIn("map.fitBounds(bounds)", html)


if __name__ == "__main__":
    unittest.main()
