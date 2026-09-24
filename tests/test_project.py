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
from artistic.render import (_BorderStripReader, _colorize, _viewport, compose, generation_hash,
                             preserve_logo_resolution, verify_raw, _settings)


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

    def test_filename_components_and_segment_pixel_limits(self):
        from artistic.project import _project_name
        for name in ("../chip", "foo/bar", "foo\\bar", ".", "..", "a\0b"):
            with self.subTest(name=name), self.assertRaises(ProjectError):
                _project_name({"design": {"name": name, "gds": "chip.gds"}})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "chip.gds"
            source.write_bytes(b"gds")
            config = {"design": {"name": "chip", "gds": str(source),
                                 "work_dir": str(root)},
                      "render": {"resolution": [2, 2], "segments": [3, 1]}}
            manifest = {"layout": {"bbox_um": [0, 0, 1, 1]}, "technology": {}}
            with self.assertRaisesRegex(ProjectError, "segments exceed"):
                _settings(config, manifest, "render", source, [("Metal1", 8, 0)])
            config["render"]["segments"] = [1, 1]
            with self.assertRaisesRegex(ProjectError, "filename component"):
                _settings(config, manifest, "render", source, [("../Metal1", 8, 0)])

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
        config["render"]["page_width_cm"] = 12
        config["render"].update({"palette": "second", "formats": ["pdf"]})
        config["render"]["colors"]["Metal1"]["alpha"] = 0.9
        config["palettes"]["second"] = {"background": "#000000"}
        self.assertEqual(generation_hash(config, "render"), original)
        config["render"]["resolution"] = [2000, 1000]
        self.assertNotEqual(generation_hash(config, "render"), original)

    def test_generation_hash_tracks_project_technology_without_resolving_file(self):
        config = {"render": {}, "map": {}, "technology": {
            "file": "/unavailable/technology.lyt",
            "layers": {"Metal1": "8/0"}, "routing": ["Metal1"],
            "top_metal": "Metal1",
        }}
        for section in ("render", "map"):
            with self.subTest(section=section):
                original = generation_hash(config, section)
                for field, changed in (("file", "/other/technology.lyt"),
                                       ("layers", {"Metal1": "9/0"}),
                                       ("routing", ["Metal1", "Metal2"]),
                                       ("top_metal", "Metal2")):
                    previous = config["technology"][field]
                    config["technology"][field] = changed
                    self.assertNotEqual(generation_hash(config, section), original)
                    config["technology"][field] = previous

    def test_explicit_render_viewport_overrides_layout_bounds(self):
        layout = {"bbox_um": [-10, -20, 110, 220]}
        self.assertEqual(_viewport(layout, {"viewport_um": [0, 1, 100, 200]}),
                         [0.0, 1.0, 100.0, 200.0])
        self.assertEqual(_viewport(layout, {"margin_um": 5}),
                         [-15, -25, 115, 225])
        for value in ([0, 0, 0, 1], [0, 0, 1], [0, 0, float("inf"), 1]):
            with self.subTest(value=value), self.assertRaises(ProjectError):
                _viewport(layout, {"viewport_um": value})

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

    def test_render_compose_resizes_uneven_segments_without_stitching_layers(self):
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
                "render": {"formats": ["png"], "resolution": [5, 5], "segments": [2, 2],
                           "palette": "test"},
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
            for source_y, height in ((0, 2), (1, 3)):
                for source_x, width in ((0, 3), (1, 2)):
                    for layer in layers:
                        image = Image.new("L", (width, height), 255)
                        active = (layer["name"], source_y, source_x) in {
                            ("Metal1", 1, 0), ("Metal2", 1, 1),
                        }
                        if active:
                            image.paste(0, (0, 0, width, height))
                        name = (f"RAW__chip_{layer['layer']}.0.{layer['name']}_"
                                f"{source_y}-{source_x}.png")
                        path = raw / name
                        image.save(path)
                        raw_hashes[name] = sha256(path)
            settings = {
                "record_version": 2, "section": "render", "chip": "chip", "input": "chip.gds",
                "input_sha256": sha256(source),
                "generation_sha256": generation_hash(config, "render"),
                "raw_dir": "raw", "raw_sha256": raw_hashes,
                "resolution": [5, 5], "segments": [2, 2], "layers": layers,
            }
            (root / "render.json").write_text(json.dumps(settings))

            with (patch("artistic.render._load_layer",
                        side_effect=AssertionError("full layer load")),
                  patch.object(Image, "MAX_IMAGE_PIXELS", 1)):
                outputs = compose(config)
                self.assertEqual(Image.MAX_IMAGE_PIXELS, 1)

            with Image.open(outputs[0]) as output:
                result = output.convert("RGB")
            self.assertEqual(result.size, (5, 5))
            self.assertEqual(result.getpixel((0, 0)), (255, 0, 0))
            self.assertEqual(result.getpixel((4, 0)), (0, 0, 255))
            self.assertEqual(result.getpixel((0, 4)), (255, 255, 255))
            self.assertEqual(result.getpixel((4, 4)), (255, 255, 255))

    def test_render_compose_lanczos_matches_whole_image_across_boundaries(self):
        try:
            from PIL import Image, ImageChops
        except ImportError:
            self.skipTest("Pillow is not installed")

        for resolution in ((9, 7), (19, 15)):
            with self.subTest(resolution=resolution), tempfile.TemporaryDirectory() as directory:
                root, raw = Path(directory), Path(directory) / "raw"
                raw.mkdir()
                source = root / "chip.gds"
                source.write_bytes(b"gds")
                config = {
                    "design": {"name": "chip", "gds": str(source), "work_dir": str(root)},
                    "technology": {"file": "/unavailable/technology.lyt"},
                    "render": {"formats": ["png"], "resolution": list(resolution),
                               "segments": [2, 2], "palette": "test"},
                    "palettes": {"test": {"background": "#f7f8e9", "layers": {
                        "Metal1": {"color": "#e02b31", "alpha": 0.8},
                        "Metal2": {"color": "#246bb3", "alpha": 0.6},
                    }}},
                }
                layers = [{"name": "Metal1", "layer": 8, "datatype": 0},
                          {"name": "Metal2", "layer": 10, "datatype": 0}]
                masks = {}
                for layer in layers:
                    mask = Image.new("L", (13, 11), 255)
                    for y in range(11):
                        for x in range(13):
                            if layer["name"] == "Metal1" and (x in (4, 5) or y in (4, 5)):
                                mask.putpixel((x, y), 0 if (x + y) % 2 else 80)
                            if layer["name"] == "Metal2" and (x in (5, 6) or y in (5, 6)):
                                mask.putpixel((x, y), 0 if (x * y) % 2 else 130)
                    masks[layer["name"]] = mask
                raw_hashes = {}
                for source_y, (y0, y1) in enumerate(((5, 11), (0, 5))):
                    for source_x, (x0, x1) in enumerate(((0, 5), (5, 13))):
                        for layer in layers:
                            name = (f"RAW__chip_{layer['layer']}.0.{layer['name']}_"
                                    f"{source_y}-{source_x}.png")
                            path = raw / name
                            masks[layer["name"]].crop((x0, y0, x1, y1)).save(path)
                            raw_hashes[name] = sha256(path)
                settings = {
                    "record_version": 2, "section": "render", "chip": "chip", "input": "chip.gds",
                    "input_sha256": sha256(source),
                    "generation_sha256": generation_hash(config, "render"),
                    "raw_dir": "raw", "raw_sha256": raw_hashes,
                    "resolution": list(resolution), "segments": [2, 2], "layers": layers,
                }
                (root / "render.json").write_text(json.dumps(settings))
                reference = Image.new("RGBA", (13, 11), "#f7f8e9")
                reference.alpha_composite(_colorize(masks["Metal1"], "#e02b31", 0.8))
                reference.alpha_composite(_colorize(masks["Metal2"], "#246bb3", 0.6))
                lanczos = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
                expected = reference.resize(resolution, lanczos)
                with patch("artistic.render._load_layer",
                           side_effect=AssertionError("full layer load")):
                    output = compose(config)[0]
                with Image.open(output) as actual:
                    difference = ImageChops.difference(actual.convert("RGBA"), expected)
                    self.assertLessEqual(max(channel[1] for channel in difference.getextrema()), 1)
                config["technology"]["file"] = "/changed/technology.lyt"
                with self.assertRaisesRegex(ProjectError, "generation settings changed"):
                    compose(config)

    def test_border_strip_reader_reuses_exact_edges_and_corners(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is not installed")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raw.png"
            source = Image.new("RGB", (12, 10))
            for y in range(10):
                for x in range(12):
                    source.putpixel((x, y), (x * 17, y * 23, (x + y) * 11))
            source.save(path)
            expected = source.convert("L")
            settings = {"resolution": [12, 10], "segments": [1, 1]}
            with patch.object(Image, "open", wraps=Image.open) as open_image:
                with _BorderStripReader(settings, 3, 3) as reader:
                    with reader.crop(path, (4, 4, 6, 6)) as middle:
                        self.assertEqual(middle.tobytes(), expected.crop((4, 4, 6, 6)).tobytes())
                    self.assertEqual(reader.bytes, 2 * 12 * 3 + 2 * 10 * 3)
                    for box in ((0, 0, 2, 2), (10, 8, 12, 10),
                                (4, 0, 6, 2), (0, 4, 2, 6),
                                (10, 4, 12, 6), (4, 8, 6, 10)):
                        with reader.crop(path, box) as crop:
                            self.assertEqual(crop.tobytes(), expected.crop(box).tobytes())
                    self.assertEqual(open_image.call_count, 1)
                self.assertEqual(reader.bytes, 0)
                self.assertFalse(reader._entries)

    def test_border_strip_reader_deduplicates_and_closes_on_eviction_or_failure(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is not installed")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raw.png"
            Image.new("L", (2, 2), 37).save(path)
            settings = {"resolution": [2, 2], "segments": [1, 1]}
            with _BorderStripReader(settings, 3, 3) as reader:
                with reader.crop(path, (0, 0, 1, 1)) as crop:
                    self.assertEqual(crop.getpixel((0, 0)), 37)
                self.assertEqual(len(reader._entries), 1)
                self.assertEqual(reader.bytes, 4)
            self.assertEqual(reader.bytes, 0)

            first = Image.new("L", (3, 3))
            second = Image.new("L", (2, 3))
            third = Image.new("L", (2, 3))
            oversized = Image.new("L", (4, 4))
            with (patch.object(first, "close", wraps=first.close) as close_first,
                  patch.object(second, "close", wraps=second.close) as close_second,
                  patch.object(third, "close", wraps=third.close) as close_third,
                  patch.object(oversized, "close", wraps=oversized.close) as close_oversized):
                reader = _BorderStripReader(settings, 1, 1, max_bytes=15)
                with self.assertRaisesRegex(RuntimeError, "test failure"):
                    with reader:
                        reader._remember(path, (0, 0, 3, 3), first)
                        reader._remember(path, (3, 0, 5, 3), second)
                        with reader.crop(path, (0, 0, 1, 1)):
                            pass
                        reader._remember(path, (5, 0, 7, 3), third)
                        self.assertEqual(reader.bytes, 15)
                        self.assertEqual(close_second.call_count, 1)
                        self.assertEqual(close_first.call_count, 0)
                        reader._remember(path, (0, 0, 4, 4), oversized)
                        self.assertEqual(close_oversized.call_count, 1)
                        raise RuntimeError("test failure")
                self.assertEqual(close_first.call_count, 1)
                self.assertEqual(close_third.call_count, 1)
                self.assertEqual(reader.bytes, 0)
                self.assertFalse(reader._entries)

    def test_render_pdf_is_lossless_and_has_configured_page_width(self):
        try:
            from PIL import Image
            import img2pdf
            import pikepdf
        except ImportError:
            self.skipTest("Pillow, img2pdf, or pikepdf is not installed")

        with tempfile.TemporaryDirectory() as directory:
            root, raw = Path(directory), Path(directory) / "raw"
            raw.mkdir()
            source = root / "chip.gds"
            source.write_bytes(b"gds")
            config = {"design": {"name": "chip", "gds": str(source), "work_dir": str(root)},
                      "render": {"formats": ["png", "pdf"], "page_width_cm": 5.08,
                                 "resolution": [20, 20], "segments": [1, 1]},
                      "palettes": {}}
            raw_image = Image.new("L", (20, 20), 255)
            raw_image.putpixel((0, 0), 0)
            raw_name = "RAW__chip_8.0.Metal1_0-0.png"
            raw_path = raw / raw_name
            raw_image.save(raw_path)
            settings = {
                "record_version": 2, "section": "render", "chip": "chip", "input": "chip.gds",
                "input_sha256": sha256(source),
                "generation_sha256": generation_hash(config, "render"),
                "raw_dir": "raw", "raw_sha256": {raw_name: sha256(raw_path)},
                "resolution": [20, 20], "segments": [1, 1],
                "layers": [{"name": "Metal1", "layer": 8, "datatype": 0}],
            }
            (root / "render.json").write_text(json.dumps(settings))
            with patch.object(Image, "MAX_IMAGE_PIXELS", 1):
                png, pdf = compose(config)
                self.assertEqual(Image.MAX_IMAGE_PIXELS, 1)
            with pikepdf.Pdf.open(pdf) as document:
                page = document.pages[0]
                self.assertEqual([float(value) for value in page.MediaBox],
                                 [0, 0, 144, 144])
                self.assertEqual(len(page.images), 1)
                embedded_stream = next(iter(page.images.values()))
                self.assertEqual(embedded_stream.Filter, pikepdf.Name("/FlateDecode"))
                with Image.open(png) as original, \
                        pikepdf.PdfImage(embedded_stream).as_pil_image() as embedded:
                    self.assertEqual(embedded.size, original.size)
                    self.assertEqual(embedded.convert("RGB").tobytes(),
                                     original.convert("RGB").tobytes())

            del config["render"]["page_width_cm"]
            compose(config)
            with pikepdf.Pdf.open(pdf) as document:
                self.assertEqual([float(value) for value in document.pages[0].MediaBox],
                                 [0, 0, 4.8, 4.8])
            self.assertFalse(list(root.glob("tmp*.png")))
            with patch.object(Image, "MAX_IMAGE_PIXELS", 1):
                with patch("img2pdf.convert", side_effect=RuntimeError("PDF failure")):
                    with self.assertRaisesRegex(RuntimeError, "PDF failure"):
                        compose(config)
                self.assertEqual(Image.MAX_IMAGE_PIXELS, 1)
            self.assertFalse(list(root.glob("tmp*.png")))

            config["render"]["page_width_cm"] = 0
            with self.assertRaises(ProjectError):
                compose(config)

