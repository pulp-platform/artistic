# Copyright 2026 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

import json
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from artistic.outlines import (_canvas, _def_groups, _lef_sizes, _matches, _pixel_box,
                               _placed_box, annotate)
from artistic.project import ProjectError, sha256, work_relative, write_json
from artistic.render import generation_hash


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
        Image.new("RGB", (100, 100), background).save(image)
        source = self.root / "chip.gds"
        record = {"record_version": 2, "chip": "chip", "resolution": [100, 100],
                  "gds": {"viewport_um": viewport},
                  "input": work_relative(self.config, source),
                  "input_sha256": sha256(source),
                  "generation_sha256": generation_hash(self.config, "render")}
        record_path = write_json(self.work / "render.json", record)
        receipt = {"version": 1, "image": image.name,
                   "image_sha256": sha256(image),
                   "render_record_sha256": sha256(record_path),
                   "generation_sha256": record["generation_sha256"],
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
        self.assertEqual(root.find("svg:image", ns).attrib[
            "{http://www.w3.org/1999/xlink}href"], "chip_render.png")
        self.assertEqual([node.text for node in root.findall("svg:text", ns)],
                         ["UART & DMA", "SRAM"])
        self.assertTrue(root.findall(".//svg:path", ns))
        self.assertFalse(list(self.work.glob("outline-*")))

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
        self.config["render"]["outlines"]["modules"]["i_uart"]["label"] = "../unsafe"
        with self.assertRaisesRegex(ProjectError, "filename component"):
            annotate(self.config)
        self.config["render"]["outlines"]["modules"]["i_uart"]["label"] = "UART"
        self.config["render"]["outlines"]["enabled"] = False
        with self.assertRaisesRegex(ProjectError, "disabled"):
            annotate(self.config)
        self.config["render"]["outlines"]["enabled"] = True
        self.config["render"]["outlines"]["min_area_pixels"] = -1
        with self.assertRaisesRegex(ProjectError, "min_area_pixels"):
            annotate(self.config)


if __name__ == "__main__":
    unittest.main()
