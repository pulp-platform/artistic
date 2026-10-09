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
                               _group_placements, _offset_placements, annotate)
from artistic.outlines import (_anchor_component, _place_labels, _query_label_bounds,
                               _style_paths, _transform_scale)
from artistic.project import ProjectError, sha256, work_relative, write_json
from artistic.presentation import decorate
from artistic.render import composition_hash, generation_hash


def _render_svg(source, target, width, height):
    inkscape = shutil.which("inkscape")
    if inkscape:
        subprocess.run([inkscape, str(source), f"--export-width={width}",
                        f"--export-height={height}", f"--export-filename={target}"],
                       check=True, capture_output=True)
    else:
        cairosvg.svg2png(url=str(source), write_to=str(target), output_width=width,
                        output_height=height)


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
    def test_contrast_option_validation(self):
        settings = self.config["render"]["outlines"]
        for name, values in {
            "stroke_border_width": [-1, float("inf"), float("nan"), True, "2"],
            "label_border_width": [-1, None],
            "stroke_border_color": ["none", "#00000000", 3],
            "label_border_color": ["invalid"],
            "label_color": ["#ffffff80"],
            "font_weight": ["heavy", 2],
            "avoid_label_overlap": [1, "true"],
        }.items():
            for value in values:
                with self.subTest(name=name, value=value):
                    settings[name] = value
                    with self.assertRaises(ProjectError):
                        _options(self.config)
            settings.pop(name)
        settings.update(label_color="white", font_weight="bold", label_border_width=2,
                        stroke_border_width=1.6, avoid_label_overlap=True)
        _options(self.config)

    def test_label_query_validates_numeric_bounds_and_accepts_signed_positions(self):
        result = subprocess.CompletedProcess([], 0, "label-0,-2.5,-4,12,8\n", "")
        with patch("artistic.outlines.tool", return_value="inkscape"), \
                patch("artistic.outlines.subprocess.run", return_value=result):
            self.assertEqual(_query_label_bounds(self.work / "test.svg", ["label-0"]),
                             {"label-0": (-2.5, -4., 12., 8.)})
            for output in ("label-0,0,0,nan,8", "label-0,0,0,-1,8", "label-0,1,2,3", ""):
                result.stdout = output
                with self.subTest(output=output), self.assertRaises(ProjectError):
                    _query_label_bounds(self.work / "test.svg", ["label-0"])

    def test_path_widths_compensate_nested_transforms(self):
        root = ET.fromstring('<g xmlns="http://www.w3.org/2000/svg" transform="scale(5 5)">'
                             '<g transform="translate(0 40) scale(0.1 -0.1)">'
                             '<path d="M0 0L10 10" vector-effect="non-scaling-stroke"/></g></g>')
        _style_paths(root, "#ffffff", 3.2, 1.6, "#000000")
        paths = list(root.iter("{http://www.w3.org/2000/svg}path"))
        self.assertEqual(len(paths), 2)
        self.assertAlmostEqual(float(paths[0].get("stroke-width")) * .5, 6.4)
        self.assertAlmostEqual(float(paths[1].get("stroke-width")) * .5, 3.2)
        self.assertTrue(all("vector-effect" not in p.attrib for p in paths))
        self.assertAlmostEqual(_transform_scale("scale(1 4)"), 2)
        for transform in ("rotate(10)", "scale(0)", "scale(nan)"):
            with self.subTest(transform=transform), self.assertRaises(ProjectError):
                _transform_scale(transform)

    @unittest.skipUnless(shutil.which("inkscape"), "Inkscape required")
    def test_compensated_strokes_have_actual_output_pixel_width(self):
        ns = "{http://www.w3.org/2000/svg}"
        root = ET.Element(ns + "svg", {"width": "100", "height": "100",
                                       "viewBox": "0 0 100 100"})
        outer = ET.SubElement(root, ns + "g", {"transform": "scale(5 5)"})
        inner = ET.SubElement(outer, ns + "g", {"transform": "translate(0 20) scale(.1 -.1)"})
        ET.SubElement(inner, ns + "path", {"d": "M40 100 L160 100"})
        _style_paths(outer, "#ffffff", 4, 2, "#000000")
        svg, png = self.work / "pixel-width.svg", self.work / "pixel-width.png"
        ET.ElementTree(root).write(svg)
        _render_svg(svg, png, 100, 100)
        with Image.open(png) as image:
            pixels = image.convert("RGBA")
            colored = sum(pixels.getpixel((50, y))[0] > 200 and
                          pixels.getpixel((50, y))[3] > 200 for y in range(100))
            occupied = sum(pixels.getpixel((50, y))[3] > 200 for y in range(100))
            self.assertEqual(colored, 4)
            self.assertEqual(occupied, 8)

    @unittest.skipUnless(shutil.which("inkscape"), "Inkscape required")
    def test_real_inkscape_measures_halo_and_overlap_repositions(self):
        ns = "{http://www.w3.org/2000/svg}"
        root = ET.Element(ns + "svg", {"width": "200", "height": "200",
                                       "viewBox": "0 0 200 200"})
        for i in range(2):
            ET.SubElement(root, ns + "use", {"id": f"label-halo-{i}",
                          "{http://www.w3.org/1999/xlink}href": f"#label-{i}",
                          "stroke": "black", "stroke-width": "4"})
            ET.SubElement(root, ns + "text", {"id": f"label-{i}", "x": "100", "y": "100",
                          "font-size": "10", "font-weight": "bold", "fill": "white",
                          "text-anchor": "middle"}).text = "CPU"
        svg, png = self.work / "real-halo.svg", self.work / "real-halo.png"
        ET.ElementTree(root).write(svg)
        component = Image.new("L", (200, 200), 255)
        try:
            _place_labels(root, svg, [({}, 100, 100, component)] * 2, [200, 200], 2)
        finally:
            component.close()
        ET.ElementTree(root).write(svg)
        boxes = _query_label_bounds(svg, ["label-halo-0", "label-halo-1"])
        a, b = boxes.values()
        self.assertTrue(a[0] + a[2] + 1.9 <= b[0] or b[0] + b[2] + 1.9 <= a[0] or
                        a[1] + a[3] + 1.9 <= b[1] or b[1] + b[3] + 1.9 <= a[1])
        _render_svg(svg, png, 200, 200)
        with Image.open(png) as image:
            self.assertIsNotNone(image.getbbox())

    def test_overlap_search_preserves_component_and_holes(self):
        mask = Image.new("L", (100, 100), 255)
        draw = ImageDraw.Draw(mask)
        draw.rectangle((10, 10, 60, 90), fill=0)
        draw.rectangle((30, 30, 40, 40), fill=255)
        draw.rectangle((70, 10, 90, 90), fill=0)
        component = _anchor_component(mask, 25, 50, 100, 100)
        self.addCleanup(component.close)
        self.assertEqual(component.getpixel((35, 35)), 0)
        self.assertEqual(component.getpixel((80, 50)), 0)
        root = ET.Element("{http://www.w3.org/2000/svg}svg")
        for i in range(2):
            ET.SubElement(root, "{http://www.w3.org/2000/svg}text",
                          {"id": f"label-{i}", "x": "25", "y": "50"}).text = "CPU"
        labels = [({}, 25, 50, component)] * 2
        measured = {f"label-{i}": (20, 46, 10, 8) for i in range(2)}
        with patch("artistic.outlines._query_label_bounds", return_value=measured):
            _place_labels(root, self.work / "placed.svg", labels, [100, 100], 0)
        texts = list(root)
        self.assertNotEqual(texts[0].get("y"), texts[1].get("y"))
        for text in texts:
            self.assertTrue(component.getpixel((int(float(text.get("x"))),
                                                int(float(text.get("y"))))))
        measured = {"label-0": (0, 0, 101, 101)}
        with patch("artistic.outlines._query_label_bounds", return_value=measured):
            with self.assertRaisesRegex(ProjectError, "reduce font_size"):
                _place_labels(root, self.work / "failed.svg", labels[:1], [100, 100], 0)

    @unittest.skipUnless(shutil.which("potrace"), "potrace required")
    def test_contrast_svg_has_single_primary_text_and_reference_halo(self):
        self._render_receipt()
        settings = self.config["render"]["outlines"]
        settings.update(stroke_width=3.2, stroke_border_width=1.6, label_color="#ffffff",
                        label_border_width=2, font_weight="bold")
        root = ET.parse(annotate(self.config)[0]).getroot()
        ns = "{http://www.w3.org/2000/svg}"
        texts, halos = root.findall(ns + "text"), root.findall(ns + "use")
        self.assertEqual(len(texts), len(halos))
        self.assertGreater(len(texts), 0)
        for i, (text, halo) in enumerate(zip(texts, halos)):
            self.assertEqual(text.get("font-weight"), "bold")
            self.assertEqual(text.get("fill"), "#ffffff")
            self.assertEqual(halo.get("{http://www.w3.org/1999/xlink}href"), f"#label-{i}")
            self.assertEqual(float(halo.get("stroke-width")), 4)
            self.assertLess(list(root).index(halo), list(root).index(text))
        paths = list(root.iter(ns + "path"))
        self.assertTrue(any(p.get("stroke") == "#000000" for p in paths))

    @unittest.skipUnless(shutil.which("potrace"), "potrace required")
    def test_contrast_colors_are_normalized_to_svg_rgb(self):
        self._render_receipt()
        settings = self.config["render"]["outlines"]
        settings.update(label_color="hsv(120,100%,100%)", label_border_color="#ffff",
                        stroke_border_color="#000000ff", stroke_border_width=2,
                        label_border_width=2)
        root = ET.parse(annotate(self.config)[0]).getroot()
        ns = "{http://www.w3.org/2000/svg}"
        self.assertTrue(all(t.get("fill") == "#00ff00" for t in root.findall(ns + "text")))
        self.assertTrue(all(t.get("stroke") == "#ffffff" for t in root.findall(ns + "use")))
        self.assertTrue(any(p.get("stroke") == "#000000" for p in root.iter(ns + "path")))
        self.assertEqual(settings["label_color"], "hsv(120,100%,100%)")

    @unittest.skipUnless(shutil.which("potrace"), "potrace required")
    def test_failed_label_placement_does_not_replace_final_svg(self):
        self._render_receipt()
        settings = self.config["render"]["outlines"]
        settings["avoid_label_overlap"] = True
        svg = self.work / "chip_modules.svg"
        previous = b"previous successful SVG"
        svg.write_bytes(previous)
        with patch("artistic.outlines._place_labels", side_effect=ProjectError("cannot place label")):
            with self.assertRaisesRegex(ProjectError, "cannot place label"):
                annotate(self.config)
        self.assertEqual(svg.read_bytes(), previous)

    @unittest.skipUnless(shutil.which("potrace"), "potrace required")
    def test_overlap_anchors_use_the_component_raster(self):
        self.config["render"]["resolution"] = [512, 512]
        self._render_receipt(resolution=[512, 512])
        self.config["render"]["outlines"].update(resolution=512, avoid_label_overlap=True)

        def diagonal_trace(mask, path, minimum):
            ImageDraw.Draw(mask).rectangle((0, 0, 511, 511), fill=1)
            ImageDraw.Draw(mask).line((128, 128, 384, 384), fill=0)
            return _trace(mask, path, minimum)

        # Label placement is tested separately: this isolates actual Potrace sampling.
        with patch("artistic.outlines._trace", side_effect=diagonal_trace), \
                patch("artistic.outlines._trace_anchors", side_effect=AssertionError("different raster")), \
                patch("artistic.outlines._place_labels") as place:
            annotate(self.config)
        labels = place.call_args.args[2]
        self.assertGreater(len(labels), 1)

    def test_overlap_avoidance_rejects_unbounded_trace_resolution(self):
        self.config["render"]["outlines"].update(resolution=513, avoid_label_overlap=True)
        with self.assertRaisesRegex(ProjectError, "at most 512"):
            _options(self.config)
        self.config["render"]["outlines"]["avoid_label_overlap"] = False
        _options(self.config)

    @unittest.skipUnless(shutil.which("potrace"), "potrace required")
    def test_failed_svg_serialization_keeps_previous_shadow_and_svg(self):
        self._render_receipt()
        record_path = self.work / "render.json"
        record = json.loads(record_path.read_text())
        record["layout"] = {"bbox_um": [50, 90, 150, 190]}
        write_json(record_path, record)
        receipt_path = self.work / "render_output.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["render_record_sha256"] = sha256(record_path)
        write_json(receipt_path, receipt)
        self.config["render"]["shadow"] = {"padding_px": 10}
        svg, shadow = self.work / "chip_modules.svg", self.work / "chip_shadow.png"
        svg.write_bytes(b"previous SVG")
        shadow.write_bytes(b"previous shadow")
        with patch("artistic.outlines.ET.ElementTree.write", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                annotate(self.config)
        self.assertEqual(svg.read_bytes(), b"previous SVG")
        self.assertEqual(shadow.read_bytes(), b"previous shadow")

    @unittest.skipUnless(shutil.which("potrace"), "potrace required")
    def test_shadow_can_toggle_after_composition_with_either_base_filename(self):
        self._render_receipt()
        record_path = self.work / "render.json"
        record = json.loads(record_path.read_text())
        record["layout"] = {"bbox_um": [50, 90, 150, 190]}
        write_json(record_path, record)
        receipt_path = self.work / "render_output.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["render_record_sha256"] = sha256(record_path)
        shutil.copyfile(self.work / "chip_render.png", self.work / "chip_render_base.png")
        for image in ("chip_render.png", "chip_render_base.png"):
            receipt["image"] = image
            write_json(receipt_path, receipt)
            for enabled in (True, False):
                self.config["render"]["shadow"] = {"enabled": enabled, "padding_px": 10}
                root = ET.parse(annotate(self.config)[0]).getroot()
                self.assertEqual(int(root.get("width")), 120 if enabled else 100)

    @unittest.skipUnless(shutil.which("inkscape") and shutil.which("potrace"),
                         "Inkscape and potrace required")
    def test_advanced_annotation_exports_png_and_keeps_labels_in_region(self):
        self._render_receipt(background="#445566")
        (self.root / "macro.lef").write_text(LEF.replace("SIZE 10 BY 4", "SIZE 60 BY 60"))
        settings = self.config["render"]["outlines"]
        settings.update(formats=["svg", "png"], font_size=10, stroke_width=3.2,
                        stroke_border_width=1.6, label_color="white",
                        label_border_width=2, font_weight="bold", avoid_label_overlap=True)
        outputs = annotate(self.config)
        self.assertEqual([p.suffix for p in outputs], [".svg", ".png"])
        root = ET.parse(outputs[0]).getroot()
        ns = "{http://www.w3.org/2000/svg}"
        texts = root.findall(ns + "text")
        self.assertEqual(len(texts), 1)
        x, y = float(texts[0].get("x")), float(texts[0].get("y"))
        self.assertTrue(10 < x < 70 and 10 < y < 80)
        bounds = _query_label_bounds(outputs[0], ["label-halo-0"])["label-halo-0"]
        self.assertTrue(bounds[0] >= 2 and bounds[1] >= 2)
        self.assertTrue(bounds[0] + bounds[2] <= 98 and bounds[1] + bounds[3] <= 98)
        with Image.open(outputs[1]) as image:
            self.assertEqual(image.size, (100, 100))

    @unittest.skipUnless(shutil.which("potrace"), "potrace required")
    def test_non_square_render_with_rounded_trace_dimensions(self):
        self.config["render"]["resolution"] = [1234, 987]
        self._render_receipt()
        record_path = self.work / "render.json"
        record = json.loads(record_path.read_text())
        record["resolution"] = [1234, 987]
        write_json(record_path, record)
        image = self.work / "chip_render.png"
        Image.new("RGB", (1234, 987), "white").save(image)
        receipt_path = self.work / "render_output.json"
        receipt = json.loads(receipt_path.read_text())
        receipt.update(resolution=[1234, 987], image_sha256=sha256(image),
                       render_record_sha256=sha256(record_path),
                       composition_sha256=composition_hash(self.config, record))
        write_json(receipt_path, receipt)
        self.config["render"]["outlines"].update(resolution=140, stroke_border_width=1.6)
        root = ET.parse(annotate(self.config)[0]).getroot()
        self.assertEqual(root.get("viewBox"), "0 0 1234 987")

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

    def _render_receipt(self, viewport=None, background="white", resolution=None):
        viewport = viewport or [50, 90, 150, 190]
        resolution = resolution or [100, 100]
        image = self.work / "chip_render.png"
        mode = "RGBA" if isinstance(background, tuple) and len(background) == 4 else "RGB"
        Image.new(mode, tuple(resolution), background).save(image)
        source = self.root / "chip.gds"
        record = {"record_version": 2, "chip": "chip", "resolution": resolution,
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
                   "resolution": resolution, "viewport_um": viewport}
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

    def test_placement_offset_defaults_and_signed_translation(self):
        _, _, _, _, _ = _options(self.config)
        placements = [("i_uart/u_cell", (10, 20, 30, 40))]
        self.assertEqual(_offset_placements(placements, [0, 0]), placements)
        self.assertEqual(_offset_placements(placements, [5.5, -7]),
                         [("i_uart/u_cell", (15.5, 13, 35.5, 33))])
        self.config["render"]["outlines"]["offset_um"] = [5, -7]
        self.assertEqual(_options(self.config)[0]["offset_um"], [5, -7])

    def test_invalid_placement_offsets_are_rejected(self):
        offsets = (None, "1,2", (1, 2), [], [1], [1, 2, 3], [True, 0],
                   ["1", 0], [float("nan"), 0], [0, float("inf")],
                   [float("-inf"), 0])
        for offset in offsets:
            self.config["render"]["outlines"]["offset_um"] = offset
            with self.subTest(offset=offset), self.assertRaisesRegex(
                    ProjectError, r"offset_um must be a pair of finite numbers"):
                _options(self.config)

    def test_annotation_translates_placement_before_viewport_clipping(self):
        self._render_receipt()
        self.config["render"]["outlines"].update(offset_um=[88, 20], resolution=100)
        traced = []

        def trace(mask, path, min_area_pixels):
            pixels = mask.convert("L")
            black = [(x, y) for y in range(pixels.height) for x in range(pixels.width)
                     if pixels.getpixel((x, y)) == 0]
            traced.append((min(x for x, _ in black), min(y for _, y in black),
                           max(x for x, _ in black), max(y for _, y in black)))
            svg = ET.Element("{http://www.w3.org/2000/svg}svg", {"viewBox": "0 0 50 50"})
            group = ET.SubElement(svg, "{http://www.w3.org/2000/svg}g")
            ET.SubElement(group, "{http://www.w3.org/2000/svg}path", {"d": "M 1 1"})
            return svg

        def anchor(mask, path, min_area_pixels):
            left, top, right, bottom = traced[-1]
            return [((left + right + 1) / 2, (top + bottom + 1) / 2)]

        with patch("artistic.outlines._trace", side_effect=trace), \
                patch("artistic.outlines._trace_anchors", side_effect=anchor):
            output = annotate(self.config)[0]
        root = ET.parse(output).getroot()
        self.assertEqual(root.attrib["viewBox"], "0 0 100 100")
        self.assertEqual(traced, [(98, 50, 99, 59)])
        labels = root.findall("{http://www.w3.org/2000/svg}text")
        self.assertEqual(len(labels), 1)
        self.assertAlmostEqual(float(labels[0].attrib["x"]), 99)
        self.assertAlmostEqual(float(labels[0].attrib["y"]), 55)
        self.assertTrue(root.findall(".//{http://www.w3.org/2000/svg}path"))

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

    @unittest.skipUnless(shutil.which("potrace") and shutil.which("inkscape"),
                         "potrace and Inkscape are required")
    def test_chip_shadow_exports_and_unshifted_label_coordinates(self):
        self._render_receipt(background="#a342f5")
        self.config["render"]["shadow"] = {
            "padding_px": 10, "blur_px": 2, "offset_px": [2, 4]}
        self.config["render"]["outlines"]["formats"] = ["svg", "png", "jpg", "pdf"]
        record_path = self.work / "render.json"
        record = json.loads(record_path.read_text())
        record["layout"] = {"bbox_um": [50, 90, 150, 190]}
        write_json(record_path, record)
        receipt_path = self.work / "render_output.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["render_record_sha256"] = sha256(record_path)
        write_json(receipt_path, receipt)
        outputs = annotate(self.config)
        root = ET.parse(outputs[0]).getroot()
        self.assertEqual(root.get("viewBox"), "-10 -10 120 120")
        self.assertEqual(root.get("width"), "120")
        images = root.findall("{http://www.w3.org/2000/svg}image")
        self.assertEqual(images[0].get("{http://www.w3.org/1999/xlink}href"), "chip_shadow.png")
        self.assertEqual(images[1].get("{http://www.w3.org/1999/xlink}href"), "chip_render.png")
        anchors = [(node.get("x"), node.get("y"))
                   for node in root.findall("{http://www.w3.org/2000/svg}text")]
        for path in outputs[1:3]:
            with Image.open(path) as image:
                self.assertEqual(image.size, (120, 120))
        self.assertTrue(outputs[3].read_bytes().startswith(b"%PDF"))
        self.config["render"]["shadow"]["enabled"] = False
        plain = ET.parse(annotate(self.config)[0]).getroot()
        self.assertEqual(plain.get("viewBox"), "0 0 100 100")
        self.assertEqual(anchors, [(node.get("x"), node.get("y"))
                                  for node in plain.findall("{http://www.w3.org/2000/svg}text")])

    @unittest.skipUnless(shutil.which("inkscape"), "Inkscape is required")
    def test_shadow_alpha_matches_composition_inside_and_outside_footprint(self):
        self.config["palettes"] = {"test": {"background": "#33669980"}}
        self.config["render"]["palette"] = "test"
        self.config["render"]["shadow"] = {
            "padding_px": 10, "blur_px": 2, "offset_px": [2, 4]}
        self.config["render"]["outlines"].update(font_size=0, formats=["png", "pdf"])
        self._render_receipt(background=(51, 102, 153, 128))
        record_path = self.work / "render.json"
        record = json.loads(record_path.read_text())
        record["layout"] = {"bbox_um": [70, 110, 130, 170]}
        write_json(record_path, record)
        receipt_path = self.work / "render_output.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["render_record_sha256"] = sha256(record_path)
        write_json(receipt_path, receipt)
        with patch("artistic.outlines._trace", return_value=ET.Element(
                "{http://www.w3.org/2000/svg}svg", {"viewBox": "0 0 50 50"})):
            png = annotate(self.config)[0]
        with Image.open(self.work / "chip_render.png") as base:
            expected = decorate(base, self.config["render"], record, (51, 102, 153, 128))
        try:
            with Image.open(png) as actual:
                # Inkscape's premultiplied colors can round by two; alpha by one.
                for point in ((1, 1), (20, 20), (29, 50), (30, 50), (60, 60), (95, 60)):
                    for channel, (observed, target) in enumerate(zip(
                            actual.convert("RGBA").getpixel(point), expected.getpixel(point))):
                        self.assertLessEqual(abs(observed - target), 1 if channel == 3 else 2, point)
            if shutil.which("pdftoppm"):
                raster = self.work / "pdf-shadow"
                subprocess.run(["pdftoppm", "-r", "96", "-png", "-singlefile",
                                str(png.with_suffix(".pdf")), str(raster)],
                               check=True, capture_output=True)
                flattened = Image.new("RGBA", expected.size, "white")
                flattened.alpha_composite(expected)
                try:
                    with Image.open(raster.with_suffix(".png")) as pdf_image:
                        for point in ((5, 5), (20, 20), (60, 60), (110, 110)):
                            for observed, target in zip(pdf_image.convert("RGB").getpixel(point),
                                                        flattened.convert("RGB").getpixel(point)):
                                self.assertLessEqual(abs(observed - target), 1, point)
                finally:
                    flattened.close()
        finally:
            expected.close()

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
