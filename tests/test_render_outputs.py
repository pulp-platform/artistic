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
from artistic.render import compose, composition_hash, generation_hash


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
        self.assertEqual(receipt["version"], 2)
        self.assertEqual(receipt["image"], "chip_render.png")
        self.assertEqual(receipt["image_sha256"], sha256(png))
        self.assertEqual(receipt["render_record_sha256"], sha256(self.root / "render.json"))
        self.assertEqual(receipt["generation_sha256"], self.settings["generation_sha256"])
        self.assertEqual(receipt["source_sha256"], self.settings["input_sha256"])
        self.assertEqual(receipt["composition_sha256"], composition_hash(self.config, self.settings))
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

    def test_optional_shadow_preserves_base_receipt_and_pdf_pixels(self):
        self.config["render"]["shadow"] = {"padding_px": 4, "blur_px": 1,
                                             "offset_px": [1, 2]}
        self.config["palettes"]["test"]["background"] = "#ffffff"
        self.settings["layout"] = {"bbox_um": [1, 2, 12, 9]}
        (self.root / "render.json").write_text(json.dumps(self.settings))
        png, pdf = compose(self.config)
        receipt = json.loads((self.root / "render_output.json").read_text())
        self.assertEqual(receipt["image"], "chip_render_base.png")
        self.assertEqual(receipt["resolution"], [11, 7])
        with self.Image.open(png) as presented, self.Image.open(self.root / receipt["image"]) as base:
            self.assertEqual(presented.size, (19, 15))
            self.assertEqual(base.size, (11, 7))
            self.assertEqual(presented.crop((4, 4, 15, 11)).tobytes(), base.tobytes())
            with self.pikepdf.Pdf.open(pdf) as document:
                _, rgb, alpha = self._pdf_image(document.pages[0])
                self.assertIsNone(alpha)
                self.assertEqual(rgb.tobytes(), presented.convert("RGB").tobytes())
        self.assertEqual(receipt["image_sha256"], sha256(self.root / receipt["image"]))
        self.assertEqual(generation_hash(self.config, "render"),
                         self.settings["generation_sha256"])

    def test_disabled_shadow_retains_original_filenames_and_dimensions(self):
        self.config["render"]["shadow"] = {"enabled": False}
        png, _ = compose(self.config)
        with self.Image.open(png) as image:
            self.assertEqual(image.size, (11, 7))
        self.assertEqual(json.loads((self.root / "render_output.json").read_text())["image"],
                         "chip_render.png")
        self.assertFalse((self.root / "chip_render_base.png").exists())

    def test_shadow_crop_respects_configured_pixel_limit(self):
        self.config["render"]["shadow"] = {"padding_px": 4}
        self.settings["layout"] = {"bbox_um": [1, 2, 12, 9]}
        (self.root / "render.json").write_text(json.dumps(self.settings))
        with patch.object(self.Image, "MAX_IMAGE_PIXELS", 1):
            png, _ = compose(self.config)
            self.assertEqual(self.Image.MAX_IMAGE_PIXELS, 1)
        with self.Image.open(png) as image:
            self.assertEqual(image.size, (19, 15))

    def test_shadow_transparent_and_translucent_png_and_pdf(self):
        self.config["render"]["shadow"] = {"padding_px": 4, "blur_px": 1}
        self.settings["layout"] = {"bbox_um": [1, 2, 12, 9]}
        (self.root / "render.json").write_text(json.dumps(self.settings))
        for background in ("transparent", "#33669980"):
            with self.subTest(background=background):
                self.config["palettes"]["test"]["background"] = background
                png, pdf = compose(self.config)
                with self.Image.open(png) as presented, \
                        self.Image.open(self.root / "chip_render_base.png") as base:
                    self.assertEqual(presented.crop((4, 4, 15, 11)).tobytes(), base.tobytes())
                    with self.pikepdf.Pdf.open(pdf) as document:
                        _, rgb, alpha = self._pdf_image(document.pages[0])
                        self.assertEqual(rgb.tobytes(), presented.convert("RGB").tobytes())
                        self.assertEqual(alpha.tobytes(), presented.getchannel("A").tobytes())

    def test_invalid_shadow_fails_before_composition_and_removes_receipt(self):
        compose(self.config)
        for shadow in ({"padding_px": -1}, {}):
            self.config["render"]["shadow"] = shadow
            with self.subTest(shadow=shadow), patch("artistic.render.raw_layout") as raw:
                with self.assertRaises(ProjectError):
                    compose(self.config)
                raw.assert_not_called()
                self.assertFalse((self.root / "render_output.json").exists())

    def test_presentation_does_not_change_generation_or_base_composition_hash(self):
        generation = generation_hash(self.config, "render")
        composition = composition_hash(self.config, self.settings)
        self.config["render"]["shadow"] = {"padding_px": 4, "blur_px": 1}
        self.config["render"]["outlines"] = {"stroke_border_width": 2, "font_weight": "bold"}
        self.assertEqual(generation_hash(self.config, "render"), generation)
        self.assertEqual(composition_hash(self.config, self.settings), composition)

    def test_pdf_physical_height_and_aspect_fit_page(self):
        for change, expected_cm, expected_image_cm in (
                ({"page_height_cm": 5.08}, (5.08 * 11 / 7, 5.08), (5.08 * 11 / 7, 5.08)),
                ({"page_width_cm": 5.08, "page_height_cm": 5.08},
                 (5.08, 5.08), (5.08, 5.08 * 7 / 11)),
                ({"page_width_cm": 5.08, "page_height_cm": 1.27},
                 (5.08, 1.27), (1.27 * 11 / 7, 1.27))):
            with self.subTest(change=change):
                self.config["render"].pop("page_width_cm", None)
                self.config["render"].pop("page_height_cm", None)
                self.config["render"].update(change)
                png, pdf = compose(self.config)
                with self.Image.open(png) as image, self.pikepdf.Pdf.open(pdf) as document:
                    self.assertEqual(image.size, (11, 7))
                    page = document.pages[0]
                    page_width, page_height = (float(page.MediaBox[index]) for index in (2, 3))
                    self.assertAlmostEqual(page_width, expected_cm[0] * 72 / 2.54, places=3)
                    self.assertAlmostEqual(page_height, expected_cm[1] * 72 / 2.54, places=3)
                    instructions = self.pikepdf.parse_content_stream(page)
                    matrix = next(operands for operands, operator in instructions
                                  if str(operator) == "cm")
                    width, height = expected_image_cm[0] * 72 / 2.54, expected_image_cm[1] * 72 / 2.54
                    self.assertAlmostEqual(float(matrix[0]), width, places=3)
                    self.assertAlmostEqual(float(matrix[3]), height, places=3)
                    self.assertAlmostEqual(float(matrix[4]), (page_width - width) / 2, places=3)
                    self.assertAlmostEqual(float(matrix[5]), (page_height - height) / 2, places=3)
                    _, rgb, alpha = self._pdf_image(page)
                    self.assertEqual(rgb.tobytes(), image.convert("RGB").tobytes())
                    self.assertEqual(alpha.tobytes(), image.getchannel("A").tobytes())

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
            ({"page_height_cm": 0}, "page_height_cm"),
            ({"page_height_cm": -1}, "page_height_cm"),
            ({"page_height_cm": True}, "page_height_cm"),
            ({"page_height_cm": float("nan")}, "page_height_cm"),
            ({"page_height_cm": 0.01}, "PDF page"),
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

    def test_composition_hash_uses_resolved_png_palette_only(self):
        baseline = composition_hash(self.config, self.settings)
        self.config["render"].update(formats=["jpg"], jpeg_background="#123456",
                                     page_width_cm=10, poster={"grid": [2, 2]},
                                     outlines={"enabled": True})
        self.assertEqual(composition_hash(self.config, self.settings), baseline)
        self.config["palettes"]["test"]["background"] = "#00000000"
        self.assertEqual(composition_hash(self.config, self.settings), baseline)
        self.config["palettes"]["test"]["background"] = "#ff0000"
        self.assertNotEqual(composition_hash(self.config, self.settings), baseline)
        self.config["palettes"]["test"]["background"] = "transparent"
        self.config["palettes"]["test"]["layers"]["Metal1"]["alpha"] = .5
        self.assertNotEqual(composition_hash(self.config, self.settings), baseline)

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
