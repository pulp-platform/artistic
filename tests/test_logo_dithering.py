# Copyright 2026 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from artistic.logo import _preprocessing, merge, prepare
from artistic.project import ProjectError

try:
    from PIL import Image
except ImportError:
    Image = None


@unittest.skipIf(Image is None, "Pillow is not installed")
class LogoDitheringTests(unittest.TestCase):
    def _config(self, root, pixels, size, **settings):
        source = root / "logo.png"
        with Image.new("RGBA", size) as image:
            image.putdata(pixels)
            image.save(source)
        return {"design": {"name": "chip", "work_dir": str(root)},
                "logo": {"source": str(source), "width_um": (size[0] - 1) * 3 + 1,
                         "height_um": (size[1] - 1) * 3 + 1, "feature_um": 1, **settings}}

    def _pixels(self, config):
        with Image.open(prepare(config)) as image:
            self.assertEqual(image.mode, "L")
            pixels = list(image.getdata())
        self.assertLessEqual(set(pixels), {0, 255})
        return pixels

    def test_gray_patch_mode_coverage_and_determinism(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, [(128, 128, 128, 255)] * 64, (8, 8))
            self.assertEqual(self._pixels(config), [255] * 64)
            config["logo"]["dither"] = "ordered"
            ordered = self._pixels(config)
            self.assertEqual(ordered.count(0), 32)
            self.assertEqual(self._pixels(config), ordered)
            config["logo"]["dither"] = "floyd-steinberg"
            floyd = self._pixels(config)
            self.assertGreater(floyd.count(0), 20)
            self.assertLess(floyd.count(0), 44)
            self.assertEqual(self._pixels(config), floyd)

    def test_gradient_and_contrast_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pixels = [(value, value, value, 255) for _ in range(4)
                      for value in range(0, 256, 16)]
            config = self._config(root, pixels, (16, 4))
            for mode in ("threshold", "floyd-steinberg", "ordered"):
                with self.subTest(mode=mode):
                    config["logo"]["dither"] = mode
                    mask = self._pixels(config)
                    self.assertGreater(mask.count(0), 0)
                    self.assertGreater(mask.count(255), 0)
            config = self._config(root, [(120, 120, 120, 255)] * 15 +
                                  [(255, 255, 255, 255)], (16, 1), dither="ordered")
            config["logo"]["contrast"] = 0.5
            low = self._pixels(config).count(0)
            config["logo"]["contrast"] = 2.0
            high = self._pixels(config).count(0)
            self.assertGreaterEqual(high, low)

    def test_resize_precedes_dither_and_transparency_is_white(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, [(0, 0, 0, 255), (0, 0, 0, 255),
                                         (0, 0, 0, 0), (0, 0, 0, 0)], (4, 1))
            config["logo"]["width_um"] = 4
            self.assertEqual(self._pixels(config), [0, 255])
            with Image.open(root / "chip_logo_mono.png") as image:
                self.assertEqual(image.size, (2, 1))

    def test_preprocessing_settings_invalidate_merge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, [(0, 0, 0, 255)] * 4, (2, 2))
            prepare(config)
            record = json.loads((root / "logo_prepare.json").read_text())
            self.assertEqual(record["record_version"], 4)
            self.assertEqual((record["dither"], record["threshold"], record["contrast"]),
                             ("threshold", 0.5, 1.0))
            with patch("artistic.logo.inspect_layout", side_effect=AssertionError("KLayout called")):
                for key, value in (("dither", "ordered"), ("threshold", 0.7),
                                   ("contrast", 1.5), ("width_um", 3),
                                   ("feature_um", 0.5), ("spacing_um", 1),
                                   ("pitch_um", 4), ("max_feature_um", 30)):
                    with self.subTest(key=key):
                        changed = {**config, "logo": {**config["logo"], key: value}}
                        with self.assertRaisesRegex(ProjectError, "logo settings changed"):
                            merge(changed)

    def test_invalid_preprocessing_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, [(0, 0, 0, 255)], (1, 1))
            cases = (("dither", "random"), ("threshold", -0.1),
                     ("threshold", 1.1), ("threshold", float("nan")),
                     ("contrast", 0), ("contrast", float("inf")),
                     ("width_um", 0), ("height_um", float("nan")),
                     ("feature_um", float("inf")),
                     ("spacing_um", 0), ("pitch_um", 2),
                     ("max_feature_um", 0.5))
            for key, value in cases:
                with self.subTest(key=key, value=value):
                    changed = {**config, "logo": {**config["logo"], key: value}}
                    with self.assertRaises(ProjectError):
                        prepare(changed)

    def test_canvas_uses_feature_and_pitch_not_feature_as_pitch(self):
        settings = _preprocessing({"width_um": 21, "height_um": 11})
        self.assertEqual((settings["feature_um"], settings["spacing_um"], settings["pitch_um"]),
                         (2, 2, 4))
        self.assertEqual((settings["width_px"], settings["height_px"]), (5, 3))
        fractional = _preprocessing({"width_um": 1, "height_um": 1,
                                     "feature_um": 0.1, "spacing_um": 0.2, "pitch_um": 0.3})
        self.assertEqual((fractional["width_px"], fractional["height_px"]), (4, 4))
        for size in (0.5, 1.9):
            with self.assertRaisesRegex(ProjectError, "fit at least one feature"):
                _preprocessing({"width_um": size, "height_um": 10})

    def test_legacy_geometry_record_is_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(Path(directory), [(0, 0, 0, 255)], (1, 1))
            prepare(config)
            record_path = Path(directory) / "logo_prepare.json"
            record = json.loads(record_path.read_text())
            record["record_version"] = 3
            record_path.write_text(json.dumps(record))
            with self.assertRaisesRegex(ProjectError, "logo settings changed"):
                merge(config)


if __name__ == "__main__":
    unittest.main()
