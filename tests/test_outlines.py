# Copyright 2026 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

import gzip
import json
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

try:
    import cairosvg
except Exception:  # Optional renderer may be installed without a working Cairo backend.
    cairosvg = None

from artistic.outlines import (_canvas, _def_groups, _lef_sizes, _matches, _pixel_box,
                               _options, _placed_box, _trace, _region_anchors,
                               _trace_anchors, _component_masks, _hierarchy_modules,
                               _group_placements, annotate)
from artistic.project import ProjectError, sha256, work_relative, write_json
from artistic.render import composition_hash, generation_hash


def _render_svg(source, target, width, height):
    if cairosvg:
        cairosvg.svg2png(url=str(source), write_to=str(target), output_width=width,
                        output_height=height)
    else:
        subprocess.run([shutil.which("inkscape"), str(source), f"--export-width={width}",
                        f"--export-height={height}", f"--export-filename={target}"],
                       check=True, capture_output=True)


DEF = r"""VERSION 5.8 ;
UNITS DISTANCE MICRONS 2000 ;
DIEAREA ( 100000 180000 )
        ( 300000 380000 ) ;
COMPONENTS 5 ;
- i_uart/u_mem MACRO
  + PLACED ( 120000 220000 ) E ;
- i_uart/u_cell STD
  + FIXED ( 150000 240000 ) N ;
- i_uart2/u_cell STD
  + PLACED ( 180000 240000 ) N ;
- i_outside/u_cell STD
  + PLACED ( 400000 400000 ) N ;
- i_soc/gen_sram_bank\[0\].i_sram/u_cell STD
  + PLACED ( 160000 260000 ) N ;
END COMPONENTS
END DESIGN
"""

LEF = """VERSION 5.8 ;
MACRO MACRO
  CLASS BLOCK ;
  SIZE 10 BY 4 ;
END MACRO
"""


class OutlineTests(unittest.TestCase):
    def test_labels_accept_path_separators_as_text(self):
        spec = self.config["render"]["outlines"]["modules"]["i_uart"]
        spec["label"] = "I/O & CPU/DMA"
        self.assertEqual(_options(self.config)[3]["i_uart"]["label"], spec["label"])
        for invalid in (None, 42, "", " ", "bad\x00label"):
            spec["label"] = invalid
            with self.subTest(label=invalid), self.assertRaises(ProjectError):
                _options(self.config)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        (self.root / "chip.gds").write_bytes(b"gds")
        (self.root / "chip.def").write_text(DEF)
        (self.root / "macro.lef").write_text(LEF)
        self.config = {
            "_root": str(self.root),
            "design": {"name": "chip", "gds": str(self.root / "chip.gds"),
                       "work_dir": str(self.work)},
            "render": {"input": "design", "resolution": [100, 100],
                       "outlines": {"def": "chip.def", "lef_files": ["*.lef"],
                                    "resolution": 50,
                                    "min_area_pixels": 0,
                                    "modules": {"i_uart": {"label": "UART & DMA",
                                                           "color": "#ff8800"}}}},
        }

    def _render_receipt(self, viewport=None, background="white"):
        viewport = viewport or [50, 90, 150, 190]
        image = self.work / "chip_render.png"
        mode = "RGBA" if isinstance(background, tuple) and len(background) == 4 else "RGB"
        Image.new(mode, (100, 100), background).save(image)
        source = self.root / "chip.gds"
        record = {"record_version": 2, "chip": "chip", "resolution": [100, 100],
                  "gds": {"viewport_um": viewport},
                  "input": work_relative(self.config, source),
                  "input_sha256": sha256(source),
                  "generation_sha256": generation_hash(self.config, "render"),
                  "layers": [{"name": "Metal1", "layer": 8, "datatype": 0}],
                  "technology": {"routing": ["Metal1", "Metal2"]}}
        record_path = write_json(self.work / "render.json", record)
        receipt = {"version": 2, "image": image.name,
                   "image_sha256": sha256(image),
                   "render_record_sha256": sha256(record_path),
                   "generation_sha256": record["generation_sha256"],
                   "composition_sha256": composition_hash(self.config, record),
                   "source_sha256": record["input_sha256"],
                   "resolution": [100, 100], "viewport_um": viewport}
        write_json(self.work / "render_output.json", receipt)

    def test_units_multiline_def_and_lef_orientation(self):
        groups = _def_groups(self.root / "chip.def", {"i_uart": {}},
                             _lef_sizes([self.root / "macro.lef"]))
        self.assertEqual(groups["i_uart"], [(60, 110, 64, 120),
                                             (75, 120, 75, 120)])
        self.assertEqual(len(groups), 1)
        self.assertTrue(_matches("gen_sram_bank", "soc/gen_sram_bank[0]/ram"))
        self.assertTrue(_matches("gen_sram_bank", r"soc/gen_sram_bank\[0\].i_sram/ram"))
        self.assertFalse(_matches("i_uart", "soc/i_uart2/cell"))
        self.assertFalse(_matches("i_uart", "soc/my_i_uart/cell"))
        expected = {"N": (0, 0, 10, 4), "S": (0, 0, 10, 4),
                    "E": (0, 0, 4, 10), "W": (0, 0, 4, 10),
                    "FN": (0, 0, 10, 4), "FS": (0, 0, 10, 4),
                    "FE": (0, 0, 4, 10), "FW": (0, 0, 4, 10)}
        for orientation, box in expected.items():
            self.assertEqual(_placed_box(0, 0, 10, 4, orientation), box)

    def test_recursive_lef_pattern_resolves_macro_dimensions(self):
        deep = self.root / "libraries" / "cells" / "macros"
        deep.mkdir(parents=True)
        (deep / "macro.lef").write_text(LEF)
        self.config["render"]["outlines"]["lef_files"] = ["libraries/**/*.lef"]
        _, def_file, lef_files, modules, _ = _options(self.config)
        self.assertEqual(lef_files, [deep / "macro.lef"])
        groups = _def_groups(def_file, modules, _lef_sizes(lef_files))
        self.assertEqual(groups["i_uart"][0], (60, 110, 64, 120))

    def test_aspect_padding_nonzero_origin_and_crop(self):
        canvas = _canvas([50, 90, 150, 140], [200, 200])
        self.assertEqual(canvas, (50, 65, .5, .5))
        self.assertEqual(_pixel_box((60, 100, 64, 110), canvas, [200, 200]),
                         (20, 110, 28, 130))
        self.assertIsNone(_pixel_box((300, 300, 320, 320), canvas, [200, 200]))
        self.assertIsNotNone(_pixel_box((75, 120, 75, 120), canvas, [200, 200]))

    @unittest.skipUnless(shutil.which("potrace"), "potrace is unavailable")
    def test_annotate_svg_and_verified_background(self):
        self._render_receipt()
        self.config["render"]["outlines"]["modules"]["gen_sram_bank"] = {
            "label": "SRAM", "color": "#39729f"}
        output = annotate(self.config)
        self.assertEqual(output, [self.work / "chip_modules.svg"])
        root = ET.parse(output[0]).getroot()
        self.assertEqual(root.attrib["viewBox"], "0 0 100 100")
        ns = {"svg": "http://www.w3.org/2000/svg"}
        self.assertEqual(root.find("svg:rect", ns).attrib["fill"], "#ffffff")
        self.assertEqual(root.find("svg:rect", ns).attrib["fill-opacity"], "1.0")
        self.assertEqual(root.find("svg:image", ns).attrib[
            "{http://www.w3.org/1999/xlink}href"], "chip_render.png")
        self.assertEqual([node.text for node in root.findall("svg:text", ns)],
                         ["UART & DMA", "UART & DMA", "SRAM"])
        self.assertTrue(root.findall(".//svg:path", ns))
        self.assertFalse(list(self.work.glob("outline-*")))

    def test_gzip_def(self):
        compressed = self.root / "chip.def.gz"
        with gzip.open(compressed, "wt") as output:
            output.write(DEF)
        self.assertEqual(_def_groups(compressed, {"i_uart": {}}, {}),
                         _def_groups(self.root / "chip.def", {"i_uart": {}}, {}))

    def test_hierarchy_relative_depth_nonleaf_and_explicit_overrides(self):
        names = ["soc/top/uart/sub/cell", "soc/top/uart/cell", "soc/top/ram/cell",
                 "soc/top/leaf", "soc/top2/not_selected/cell", "other/soc/top/ignored/cell"]
        placements = [(name, (index, 0, index + 1, 1)) for index, name in enumerate(names)]
        hierarchy = {"top_instance": "soc.top", "min_depth": 1, "max_depth": 2}
        explicit = {"uart": {"label": "UART", "color": "#ff0000"},
                    "outside": {"label": "Other", "color": "#00ff00"}}
        modules, rooted = _hierarchy_modules(placements, hierarchy, explicit)
        self.assertEqual(list(modules), ["soc/top/ram", "soc/top/uart", "soc/top/uart/sub", "outside"])
        self.assertEqual(modules["soc/top/uart"], explicit["uart"])
        self.assertEqual(modules["soc/top/uart/sub"], explicit["uart"])
        groups = _group_placements(placements, modules, rooted)
        self.assertEqual(len(groups["soc/top/uart"]), 2)
        self.assertEqual(len(groups["soc/top/uart/sub"]), 1)
        self.assertEqual(_hierarchy_modules(placements, hierarchy, {})[0],
                         _hierarchy_modules(list(reversed(placements)), hierarchy, {})[0])
        with self.assertRaisesRegex(ProjectError, "top_instance not found"):
            _hierarchy_modules(placements, {"top_instance": "top"}, {})

    def test_hierarchy_options_without_modules_and_invalid_prefixes(self):
        outlines = self.config["render"]["outlines"]
        outlines.pop("modules")
        outlines["hierarchy"] = {"top_instance": "i_soc"}
        self.assertEqual(_options(self.config)[3], {})
        for bad in ("", "soc/", "/soc", "soc//top", "soc*", "soc top", 3):
            outlines["hierarchy"] = {"top_instance": bad}
            with self.subTest(top=bad), self.assertRaisesRegex(ProjectError, "hierarchy prefix"):
                _options(self.config)
        for minimum, maximum in ((0, 1), (2, 1), (1, 0), (True, 2), (1, 2.5)):
            outlines["hierarchy"] = {"top_instance": "i_soc", "min_depth": minimum,
                                      "max_depth": maximum}
            with self.subTest(depths=(minimum, maximum)), self.assertRaisesRegex(ProjectError, "depths"):
                _options(self.config)

    @unittest.skipUnless(shutil.which("potrace"), "potrace is unavailable")
    def test_hierarchy_annotation_without_explicit_modules(self):
        self._render_receipt()
        outlines = self.config["render"]["outlines"]
        outlines.pop("modules")
        outlines["hierarchy"] = {"top_instance": "i_soc"}
        root = ET.parse(annotate(self.config)[0]).getroot()
        self.assertEqual([node.text for node in root.findall("{http://www.w3.org/2000/svg}text")],
                         ["gen_sram_bank[0]"])

    @unittest.skipUnless(shutil.which("potrace"), "potrace is unavailable")
    def test_separate_sram_macro_labels_are_inside_each_macro(self):
        data = DEF[:DEF.index("COMPONENTS")] + r"""COMPONENTS 2 ;
- i_soc/gen_sram_bank\[0\].i_sram/u_mem MACRO + FIXED ( 120000 220000 ) N ;
- i_soc/gen_sram_bank\[1\].i_sram/u_mem MACRO + FIXED ( 220000 300000 ) N ;
END COMPONENTS
END DESIGN
"""
        (self.root / "chip.def").write_text(data)
        self.config["render"]["outlines"]["modules"] = {
            "gen_sram_bank": {"label": "SRAM", "color": "#39729f"}}
        self._render_receipt()
        root = ET.parse(annotate(self.config)[0]).getroot()
        labels = root.findall("{http://www.w3.org/2000/svg}text")
        self.assertEqual([node.text for node in labels], ["SRAM", "SRAM"])
        anchors = [(float(node.attrib["x"]), float(node.attrib["y"])) for node in labels]
        for box in ((10, 76, 20, 80), (60, 36, 70, 40)):
            self.assertTrue(any(box[0] < x < box[2] and box[1] < y < box[3] for x, y in anchors))

    def test_region_anchor_clearance_concavity_and_holes(self):
        mask = Image.new("L", (100, 100), 255)
        draw = ImageDraw.Draw(mask)
        draw.rectangle((5, 5, 30, 90), fill=0)
        draw.rectangle((5, 65, 90, 90), fill=0)
        anchors = _region_anchors(mask)
        self.assertEqual(len(anchors), 1)
        x, y = anchors[0]
        self.assertEqual(mask.getpixel((int(x), int(y))), 0)
        self.assertTrue(x < 31 or y > 64)
        self.assertGreater(min(x - 5, y - 5, 91 - y), 10)
        mask = Image.new("L", (100, 100), 255)
        draw = ImageDraw.Draw(mask)
        draw.rectangle((5, 5, 94, 94), fill=0)
        draw.rectangle((25, 25, 74, 74), fill=255)
        anchors = _region_anchors(mask)
        self.assertEqual(len(anchors), 1)
        x, y = anchors[0]
        self.assertEqual(mask.getpixel((int(x), int(y))), 0)
        self.assertFalse(25 <= x < 75 and 25 <= y < 75)
        self.assertEqual(_region_anchors(Image.new("L", (10, 10), 255)), [])

    def test_component_crops_preserve_holes_exclude_nested_islands_and_add_context(self):
        mask = Image.new("1", (100, 100), 1)
        draw = ImageDraw.Draw(mask)
        draw.rectangle((10, 10, 89, 89), fill=0)
        draw.rectangle((20, 20, 79, 79), fill=1)
        draw.rectangle((40, 40, 49, 49), fill=0)
        components = list(_component_masks(mask))
        self.assertEqual(len(components), 2)
        for crop, (ox, oy) in components:
            self.assertNotEqual(crop.getpixel((0, 0)), 0)
            self.assertNotEqual(crop.getpixel((crop.width - 1, crop.height - 1)), 0)
            if ox == 8:
                self.assertNotEqual(crop.getpixel((45 - ox, 45 - oy)), 0)
                self.assertEqual(crop.getpixel((15 - ox, 15 - oy)), 0)
            else:
                self.assertEqual((ox, oy), (38, 38))
                self.assertEqual(crop.getpixel((45 - ox, 45 - oy)), 0)
            crop.close()

    @unittest.skipUnless(shutil.which("potrace"), "potrace is unavailable")
    def test_high_resolution_trace_keeps_tiny_macro_labels_and_excludes_speckles(self):
        mask = Image.new("1", (4096, 1024), 1)
        draw = ImageDraw.Draw(mask)
        draw.rectangle((10, 10, 13, 13), fill=0)
        draw.rectangle((4000, 900, 4003, 903), fill=0)
        draw.point((2000, 500), fill=0)
        trace_path = self.work / "high-resolution"
        _trace(mask, trace_path, 2)
        anchors = _trace_anchors(mask, trace_path, 2)
        self.assertEqual(len(anchors), 2)
        self.assertTrue(any(10 < x < 14 and 10 < y < 14 for x, y in anchors))
        self.assertTrue(any(4000 < x < 4004 and 900 < y < 904 for x, y in anchors))
        if cairosvg or shutil.which("inkscape"):
            target = self.work / "high-resolution.png"
            _render_svg(trace_path.with_suffix(".svg"), target, *mask.size)
            with Image.open(target) as final:
                for x, y in anchors:
                    self.assertGreater(final.convert("RGBA").getpixel((int(x), int(y)))[3], 240)
        _trace(mask, trace_path, 0)
        self.assertEqual(len(_trace_anchors(mask, trace_path, 0)), 3)
        for raster in self.work.glob("high-resolution-region-*.pgm"):
            with Image.open(raster) as image:
                self.assertLessEqual(max(image.size), 2048)

    @unittest.skipUnless(shutil.which("potrace"), "potrace is unavailable")
    def test_large_thin_component_preserves_an_interior_anchor(self):
        mask = Image.new("1", (4096, 16), 1)
        ImageDraw.Draw(mask).line((2, 8, 4093, 8), fill=0)
        trace_path = self.work / "thin"
        _trace(mask, trace_path, 0)
        anchors = _trace_anchors(mask, trace_path, 0)
        self.assertEqual(len(anchors), 1)
        x, y = anchors[0]
        self.assertTrue(2 < x < 4094 and 8 < y < 9)
        with Image.open(self.work / "thin-region-0000.pgm") as image:
            self.assertGreater(image.width, 2048)
            self.assertLessEqual(image.height, 2048)

    @unittest.skipUnless(shutil.which("potrace"), "potrace is unavailable")
    def test_large_one_pixel_ring_retains_an_interior_anchor(self):
        mask = Image.new("1", (4096, 4096), 1)
        ImageDraw.Draw(mask).rectangle((2, 2, 4093, 4093), outline=0)
        trace_path = self.work / "ring"
        _trace(mask, trace_path, 0)
        with patch.object(Image, "MAX_IMAGE_PIXELS", 1):
            anchors = _trace_anchors(mask, trace_path, 0)
            self.assertEqual(Image.MAX_IMAGE_PIXELS, 1)
        self.assertEqual(len(anchors), 1)
        x, y = anchors[0]
        self.assertEqual(mask.getpixel((int(x), int(y))), 0)
        self.assertTrue(2 <= x < 3 or 4093 <= x < 4094 or 2 <= y < 3 or 4093 <= y < 4094)

    @unittest.skipUnless(shutil.which("potrace") and (cairosvg or shutil.which("inkscape")),
                         "potrace or SVG renderer is unavailable")
    def test_trace_anchors_inside_final_svg_disconnected_concave_holes_and_speckles(self):
        mask = Image.new("1", (100, 100), 1)
        draw = ImageDraw.Draw(mask)
        draw.rectangle((5, 5, 40, 40), fill=0)
        draw.rectangle((15, 15, 30, 30), fill=1)  # A hole in the first region.
        draw.rectangle((60, 5, 72, 45), fill=0)
        draw.rectangle((60, 33, 94, 45), fill=0)  # A separate concave region.
        draw.rectangle((50, 80, 50, 80), fill=0)  # Removed by turdsize.
        trace_path = self.work / "trace"
        _trace(mask, trace_path, 2)
        with Image.open(trace_path.with_suffix(".pgm")) as filled:
            anchors = [(x * 100 / filled.width, y * 100 / filled.height)
                       for x, y in _region_anchors(filled)]
        self.assertEqual(len(anchors), 2)
        target = self.work / "trace.png"
        _render_svg(trace_path.with_suffix(".svg"), target, 100, 100)
        with Image.open(target) as final:
            for x, y in anchors:
                self.assertGreater(final.convert("RGBA").getpixel((int(x), int(y)))[3], 240)
        self.assertTrue(any(x < 45 for x, _ in anchors))
        self.assertTrue(any(x > 55 for x, _ in anchors))

    @unittest.skipUnless(cairosvg or shutil.which("inkscape"), "SVG renderer is unavailable")
    def test_background_blends_over_palette_color_and_explicit_alpha(self):
        outlines = self.config["render"]["outlines"]
        outlines.update(background_opacity=.5, font_size=0, formats=["png"])
        self.config["palettes"] = {"dark": {"background": "black"}}
        self.config["render"]["palette"] = "dark"
        self._render_receipt(background="black")

        def export(command):
            _render_svg(Path(command[1]), Path(command[-1]), 100, 100)

        for background, expected in ((None, (0, 0, 0, 255)),
                                     ("white", (127, 127, 127, 255)),
                                     ("transparent", (0, 0, 0, 128)),
                                     ("#ff000080", (85, 0, 0, 192))):
            if background is None:
                outlines.pop("background", None)
            else:
                outlines["background"] = background
            with self.subTest(background=background), patch("artistic.outlines._trace", return_value=ET.Element(
                    "{http://www.w3.org/2000/svg}svg", {"viewBox": "0 0 50 50"})), \
                    patch("artistic.outlines.tool", return_value="inkscape"), \
                    patch("artistic.outlines.run_checked", side_effect=export):
                output = annotate(self.config)[0]
            with Image.open(output) as result:
                actual = result.convert("RGBA").getpixel((0, 0))
                for value, target in zip(actual, expected):
                    self.assertAlmostEqual(value, target, delta=1)
            root = ET.parse(self.work / "chip_modules.svg").getroot()
            self.assertTrue(root[0].tag.endswith("rect"))
            self.assertTrue(root[1].tag.endswith("image"))

    def test_invalid_background_is_rejected_before_annotation(self):
        self.config["render"]["outlines"]["background"] = "not-a-color"
        with self.assertRaisesRegex(ProjectError, "background color"):
            _options(self.config)

    @unittest.skipUnless(cairosvg or shutil.which("inkscape"), "SVG renderer is unavailable")
    def test_default_translucent_background_preserves_render_alpha(self):
        outlines = self.config["render"]["outlines"]
        outlines.update(font_size=0, formats=["png"])
        self.config["palettes"] = {"translucent": {"background": "#ff000080"}}
        self.config["render"]["palette"] = "translucent"
        self._render_receipt(background=(255, 0, 0, 128))

        def export(command):
            _render_svg(Path(command[1]), Path(command[-1]), 100, 100)

        for opacity, expected_alpha in ((1, 128), (.5, 64)):
            outlines["background_opacity"] = opacity
            with self.subTest(opacity=opacity), patch("artistic.outlines._trace", return_value=ET.Element(
                    "{http://www.w3.org/2000/svg}svg", {"viewBox": "0 0 50 50"})), \
                    patch("artistic.outlines.tool", return_value="inkscape"), \
                    patch("artistic.outlines.run_checked", side_effect=export):
                output = annotate(self.config)[0]
            with Image.open(output) as result:
                self.assertEqual(result.convert("RGBA").getpixel((0, 0)), (255, 0, 0, expected_alpha))

    @unittest.skipUnless(shutil.which("potrace") and shutil.which("inkscape"),
                         "potrace or Inkscape is unavailable")
    def test_png_pdf_jpg_exports(self):
        self._render_receipt(background="#a342f5")
        self.config["render"]["outlines"]["formats"] = ["png", "pdf", "jpg"]
        with patch.object(Image, "MAX_IMAGE_PIXELS", 1):
            outputs = annotate(self.config)
            self.assertEqual(Image.MAX_IMAGE_PIXELS, 1)
        self.assertEqual([p.suffix for p in outputs], [".png", ".pdf", ".jpg"])
        with Image.open(outputs[0]) as png, Image.open(outputs[2]) as jpg:
            self.assertEqual(png.size, (100, 100))
            self.assertEqual(jpg.size, (100, 100))
            self.assertEqual(png.convert("RGBA").getpixel((0, 0)), (163, 66, 245, 255))
        self.assertTrue(outputs[1].read_bytes().startswith(b"%PDF"))

    @unittest.skipUnless(shutil.which("potrace"), "potrace is unavailable")
    def test_verified_image_respects_recorded_pixel_limit(self):
        self._render_receipt()
        with patch.object(Image, "MAX_IMAGE_PIXELS", 1):
            annotate(self.config)
            self.assertEqual(Image.MAX_IMAGE_PIXELS, 1)

    def test_receipt_and_configuration_rejection(self):
        self._render_receipt()
        receipt_path = self.work / "render_output.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["image_sha256"] = "bad"
        write_json(receipt_path, receipt)
        with self.assertRaisesRegex(ProjectError, "stale or changed"):
            annotate(self.config)
        receipt["image_sha256"] = sha256(self.work / "chip_render.png")
        receipt["image"] = "../chip.gds"
        write_json(receipt_path, receipt)
        with self.assertRaisesRegex(ProjectError, "stale or changed"):
            annotate(self.config)
        self.config["render"]["outlines"]["modules"]["i_uart"]["label"] = "bad\x00label"
        with self.assertRaisesRegex(ProjectError, "control characters"):
            annotate(self.config)
        self.config["render"]["outlines"]["modules"]["i_uart"]["label"] = "UART"
        self.config["render"]["outlines"]["enabled"] = False
        with self.assertRaisesRegex(ProjectError, "disabled"):
            annotate(self.config)
        self.config["render"]["outlines"]["enabled"] = True
        self.config["render"]["outlines"]["min_area_pixels"] = -1
        with self.assertRaisesRegex(ProjectError, "min_area_pixels"):
            annotate(self.config)

    def test_non_object_records_are_rejected(self):
        for filename in ("render.json", "render_output.json"):
            for value in ([], None, "invalid", 42):
                with self.subTest(filename=filename, value=value):
                    self._render_receipt()
                    write_json(self.work / filename, value)
                    with self.assertRaisesRegex(ProjectError, "receipt is invalid; compose render again"):
                        annotate(self.config)

    def test_receipt_version_and_composition_changes(self):
        self._render_receipt()
        receipt_path = self.work / "render_output.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["version"] = 1
        write_json(receipt_path, receipt)
        with self.assertRaisesRegex(ProjectError, "run render compose again"):
            annotate(self.config)
        receipt["version"] = 2
        write_json(receipt_path, receipt)
        render = self.config["render"]
        self.config["palettes"] = {"custom": {"background": "white", "layers": {
            "Metal1": {"color": "#123456", "alpha": .8}}}}
        render["palette"] = "custom"
        for change in (
            lambda: self.config["palettes"]["custom"]["layers"]["Metal1"].update(color="#ff0000"),
            lambda: self.config["palettes"]["custom"]["layers"]["Metal1"].update(alpha=.5),
            lambda: self.config["palettes"]["custom"].update(background="black"),
            lambda: self.config["palettes"]["custom"].update(hue_rotation_deg=20),
            lambda: render.update(colors={"Metal1": {"color": "#00ff00"}}),
        ):
            self._render_receipt()
            change()
            with self.assertRaisesRegex(ProjectError, "run render compose again"):
                annotate(self.config)
        custom = self.config["palettes"]["custom"]
        custom["layers"] = {}
        custom["generate"] = {"hue_start_deg": 0}
        render.pop("colors", None)
        self._render_receipt()
        custom["generate"]["hue_start_deg"] = 30
        with self.assertRaisesRegex(ProjectError, "run render compose again"):
            annotate(self.config)
        self._render_receipt()
        render["formats"] = ["jpg"]
        render["jpeg_background"] = "#123456"
        render["page_width_cm"] = 8
        render["poster"] = {"grid": [2, 2]}
        render["outlines"]["font_size"] = 10
        with patch("artistic.outlines._trace", return_value=ET.Element(
                "{http://www.w3.org/2000/svg}svg", {"viewBox": "0 0 50 50"})):
            self.assertEqual(annotate(self.config), [self.work / "chip_modules.svg"])

    def test_jpg_export_uses_configured_matte_and_validates_first(self):
        self._render_receipt()
        outlines = self.config["render"]["outlines"]
        outlines["formats"] = ["jpg"]
        self.config["render"]["jpeg_background"] = "transparent"
        with self.assertRaisesRegex(ProjectError, "opaque"):
            annotate(self.config)
        self.assertFalse((self.work / "chip_modules.svg").exists())
        self.config["render"]["jpeg_background"] = "#00ff00"
        outlines["background_opacity"] = .5

        def export(command):
            target = Path(command[-1])
            Image.new("RGBA", (100, 100), (255, 0, 0, 128)).save(target)

        with patch("artistic.outlines._trace", return_value=ET.Element(
                "{http://www.w3.org/2000/svg}svg", {"viewBox": "0 0 50 50"})), \
                patch("artistic.outlines.tool", return_value="inkscape"), \
                patch("artistic.outlines.run_checked", side_effect=export):
            jpg = annotate(self.config)[0]
        with Image.open(jpg) as result:
            red, green, blue = result.getpixel((50, 50))
            self.assertAlmostEqual(red, 128, delta=8)
            self.assertAlmostEqual(green, 127, delta=8)
            self.assertLess(blue, 8)


if __name__ == "__main__":
    unittest.main()
