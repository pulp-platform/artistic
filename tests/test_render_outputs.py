# Copyright 2026 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from artistic.image_outputs import _page_crop, poster_options
from artistic.project import ProjectError, sha256
from artistic.render import compose, generation_hash


class RenderOutputTests(unittest.TestCase):
    def setUp(self):
        try:
            from PIL import Image
            import img2pdf
            import pikepdf
        except ImportError:
            self.skipTest("Pillow, img2pdf, and pikepdf are required")
        self.Image = Image
        self.pikepdf = pikepdf
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "chip.gds"
        self.source.write_bytes(b"gds")
        self.config = {
            "design": {"name": "chip", "gds": str(self.source), "work_dir": str(self.root)},
            "render": {"formats": ["png", "pdf"], "resolution": [11, 7], "segments": [1, 1],
                       "page_width_cm": 5.08, "palette": "test"},
            "palettes": {"test": {"background": "transparent", "layers": {
                "Metal1": {"color": "#c02040", "alpha": 1.0}}}},
        }
        raw = self.root / "raw"
        raw.mkdir()
        mask = Image.new("L", (11, 7), 255)
        for y in range(7):
            for x in range(11):
                if (x + y) % 3 == 0:
                    mask.putpixel((x, y), 0)
        name = "RAW__chip_8.0.Metal1_0-0.png"
        raw_path = raw / name
        mask.save(raw_path)
        self.settings = {
            "record_version": 2, "section": "render", "chip": "chip", "input": "chip.gds",
            "input_sha256": sha256(self.source),
            "generation_sha256": generation_hash(self.config, "render"),
            "raw_dir": "raw", "raw_sha256": {name: sha256(raw_path)},
            "resolution": [11, 7], "segments": [1, 1],
            "gds": {"viewport_um": [1, 2, 12, 9]},
            "layers": [{"name": "Metal1", "layer": 8, "datatype": 0}],
            "technology": {"routing": ["Metal1"]},
        }
        (self.root / "render.json").write_text(json.dumps(self.settings))

    def _pdf_image(self, page):
        stream = next(iter(page.images.values()))
        with self.pikepdf.PdfImage(stream).as_pil_image() as image:
            rgb = image.convert("RGB")
        alpha = None
        if "/SMask" in stream:
            with self.pikepdf.PdfImage(stream.SMask).as_pil_image() as mask:
                alpha = mask.convert("L")
        return stream, rgb, alpha

    def test_alpha_png_pdf_and_receipt(self):
        png, pdf = compose(self.config)
        with self.Image.open(png) as image, self.pikepdf.Pdf.open(pdf) as document:
            self.assertEqual(image.mode, "RGBA")
            stream, rgb, alpha = self._pdf_image(document.pages[0])
            self.assertIsNotNone(alpha)
            self.assertEqual(rgb.tobytes(), image.convert("RGB").tobytes())
            self.assertEqual(alpha.tobytes(), image.getchannel("A").tobytes())
            self.assertEqual(stream.Filter, self.pikepdf.Name("/FlateDecode"))
        receipt = json.loads((self.root / "render_output.json").read_text())
        self.assertEqual(receipt["image"], "chip_render.png")
        self.assertEqual(receipt["image_sha256"], sha256(png))
        self.assertEqual(receipt["render_record_sha256"], sha256(self.root / "render.json"))
        self.assertEqual(receipt["generation_sha256"], self.settings["generation_sha256"])
        self.assertEqual(receipt["source_sha256"], self.settings["input_sha256"])
        self.assertEqual(receipt["viewport_um"], [1, 2, 12, 9])

    def test_jpeg_flattens_transparency_without_hidden_color(self):
        self.config["render"]["formats"] = ["jpg"]
        name = next(iter(self.settings["raw_sha256"]))
        raw_path = self.root / "raw" / name
        self.Image.new("L", (11, 7), 255).save(raw_path)
        self.settings["raw_sha256"][name] = sha256(raw_path)
        (self.root / "render.json").write_text(json.dumps(self.settings))
        jpg = compose(self.config)[0]
        with self.Image.open(jpg) as image:
            red, green, blue = image.getpixel((1, 0))
            self.assertGreater(min(red, green, blue), 245)
        self.assertFalse((self.root / "render_output.json").exists())
        self.config["render"]["jpeg_background"] = "#00ff00"
        jpg = compose(self.config)[0]
        with self.Image.open(jpg) as image:
            red, green, blue = image.getpixel((1, 0))
            self.assertLess(red, 15)
            self.assertGreater(green, 245)
            self.assertLess(blue, 15)

    def test_opaque_pdf_keeps_rgb_fast_path(self):
        self.config["palettes"]["test"]["background"] = "#ffffff"
        png, pdf = compose(self.config)
        with self.pikepdf.Pdf.open(pdf) as document:
            stream, rgb, alpha = self._pdf_image(document.pages[0])
            self.assertIsNone(alpha)
            self.assertEqual(stream.Filter, self.pikepdf.Name("/FlateDecode"))
            with self.Image.open(png) as image:
                self.assertEqual(rgb.tobytes(), image.convert("RGB").tobytes())

    def test_poster_geometry_row_order_and_transparency(self):
        self.config["render"]["formats"] = ["png"]
        self.config["render"]["poster"] = {
            "grid": [3, 2], "page_size_mm": [40, 30], "margin_mm": 3,
            "overlap_mm": 2,
        }
        png, pdf = compose(self.config)
        poster = poster_options(self.config["render"]["poster"])
        with self.Image.open(png) as original, self.pikepdf.Pdf.open(pdf) as document:
            self.assertEqual(len(document.pages), 6)
            boxes = []
            for row in range(2):
                for col in range(3):
                    page = document.pages[row * 3 + col]
                    self.assertAlmostEqual(float(page.MediaBox[2]), poster.page_size_pt[0], places=3)
                    self.assertAlmostEqual(float(page.MediaBox[3]), poster.page_size_pt[1], places=3)
                    box, (x, y), scale = _page_crop(original, poster, col, row)
                    boxes.append(box)
                    stream, rgb, alpha = self._pdf_image(page)
                    with original.crop(box) as expected:
                        self.assertEqual(rgb.tobytes(), expected.convert("RGB").tobytes())
                        self.assertEqual(alpha.tobytes(), expected.getchannel("A").tobytes())
                    content = page.Contents.read_bytes().decode("ascii")
                    self.assertIn(f"{poster.margin_pt:.10f} {poster.margin_pt:.10f}", content)
                    self.assertIn(f"{x:.10f} {y:.10f} cm", content)
                    self.assertIn(" re W n", content)
            self.assertEqual(boxes[0][1], 0)
            self.assertEqual(boxes[-1][3], 7)
            for row in range(2):
                for col in range(2):
                    self.assertGreaterEqual(boxes[row * 3 + col][2],
                                            boxes[row * 3 + col + 1][0])
        self.assertFalse(list(self.root.glob("tmp*.png")))

    def test_invalid_options_fail_before_composition_and_no_receipt(self):
        cases = [
            ({"poster": {"grid": [0, 2]}}, "grid"),
            ({"poster": {"grid": [True, 2]}}, "grid"),
            ({"poster": {"grid": [2, 2], "margin_mm": 110}}, "margins"),
            ({"poster": {"grid": [2, 2], "overlap_mm": 200}}, "overlap"),
            ({"poster": {"grid": [2, 2], "page_size_mm": [0.5, 30]}}, "page_size"),
            ({"jpeg_background": "transparent"}, "opaque"),
            ({"page_width_cm": 0}, "page_width_cm"),
            ({"page_width_cm": 0.01}, "PDF page"),
        ]
        for change, message in cases:
            with self.subTest(change=change):
                current = self.config["render"].copy()
                self.config["render"].update(change)
                with patch("artistic.render.raw_layout", side_effect=AssertionError("composed")):
                    with self.assertRaisesRegex(ProjectError, message):
                        compose(self.config)
                self.assertFalse((self.root / "render_output.json").exists())
                self.config["render"] = current

    def test_poster_and_palette_do_not_change_generation_hash(self):
        digest = generation_hash(self.config, "render")
        self.config["render"]["poster"] = {"grid": [2, 2]}
        self.config["render"]["jpeg_background"] = "#000000"
        self.config["palettes"]["test"]["background"] = "#000000"
        self.assertEqual(generation_hash(self.config, "render"), digest)

    def test_pdf_failure_removes_temporary_png_and_receipt(self):
        self.config["render"]["formats"] = ["png", "pdf"]
        with patch("img2pdf.convert", side_effect=RuntimeError("PDF failed")):
            with self.assertRaisesRegex(RuntimeError, "PDF failed"):
                compose(self.config)
        self.assertFalse(list(self.root.glob("tmp*")))
        self.assertFalse((self.root / "render_output.json").exists())

    def test_outlines_force_png_even_when_not_requested(self):
        self.config["render"]["formats"] = ["jpg"]
        self.config["render"]["outlines"] = {"enabled": True}
        outputs = compose(self.config)
        self.assertEqual([path.suffix for path in outputs], [".jpg", ".png"])
        self.assertTrue((self.root / "render_output.json").is_file())


if __name__ == "__main__":
    unittest.main()
