# Copyright 2026 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

"""Real KLayout width/spacing checks for the isolated-square logo lattice."""

import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from artistic.logo import _mask_rows, prepare

try:
    import pya
    from PIL import Image
except ImportError:
    pya = None

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


@unittest.skipIf(pya is None, "KLayout PYA and Pillow are required")
class LogoGeometryIntegrationTests(unittest.TestCase):
    def _merge(self, root, rows, width=3, height=3, dbu=0.01, feature=4,
               spacing=2, pitch=6, maximum=30, mixed_metal=False,
               canvas_width=None, canvas_height=None, offset_x=0, offset_y=0,
               bbox_dbu=(0, 0, 10000, 10000), metal_boxes=(), metal_polygons=()):
        from pya_pipeline import logo_merge

        layout = pya.Layout()
        layout.dbu = dbu
        top = layout.create_cell("original")
        metal_index = layout.layer(134, 0)
        top.shapes(metal_index).insert(pya.Box(0, 0, 400, 400))
        if mixed_metal:
            top.shapes(metal_index).insert(pya.Box(4200, 4800, 4400, 5200))
        for box in metal_boxes:
            top.shapes(metal_index).insert(pya.Box(*box))
        for points in metal_polygons:
            top.shapes(metal_index).insert(pya.Polygon([pya.Point(*point) for point in points]))
        top.shapes(layout.layer(99, 0)).insert(pya.Box(*bbox_dbu))
        source = root / "source.gds"
        layout.write(str(source))
        request = {"gds": str(source), "layer": 134, "datatype": 0,
                   "bbox_dbu": list(bbox_dbu), "rows": rows,
                   "width_px": width, "height_px": height,
                   "width_um": (width - 1) * pitch + feature if canvas_width is None else canvas_width,
                   "height_um": (height - 1) * pitch + feature if canvas_height is None else canvas_height,
                   "feature_um": feature, "spacing_um": spacing, "pitch_um": pitch,
                   "offset_x_um": offset_x, "offset_y_um": offset_y,
                   "max_feature_um": maximum, "logo_cell": "chip_logo",
                   "chip_cell": "chip", "logo_gds": str(root / "chip_logo.gds"),
                   "logo_svg": str(root / "chip_logo_geometry.svg"),
                   "chip_gds": str(root / "chip.gds.gz"),
                   "result": str(root / "logo_merge.json")}
        logo_merge(request)
        result = json.loads((root / "logo_merge.json").read_text())
        logo_layout = pya.Layout()
        logo_layout.read(request["logo_gds"])
        logo = pya.Region(logo_layout.top_cell().begin_shapes_rec(logo_layout.layer(134, 0)))
        logo.flatten()
        merged_layout = pya.Layout()
        merged_layout.read(request["chip_gds"])
        merged = pya.Region(merged_layout.top_cell().begin_shapes_rec(merged_layout.layer(134, 0)))
        merged.flatten()
        return result, logo, merged

    def _check_rules(self, result, logo, merged, dbu):
        feature = round(result["feature_um"] / dbu)
        spacing = round(result["spacing_um"] / dbu)
        self.assertTrue(logo.width_check(feature).is_empty())
        self.assertTrue(logo.space_check(spacing).is_empty())
        self.assertTrue(merged.space_check(spacing).is_empty())
        self.assertEqual(logo.merged().size(), result["accepted_pixels"])
        for polygon in logo.each():
            bbox = polygon.bbox()
            self.assertEqual((bbox.width(), bbox.height()), (feature, feature))
            self.assertEqual(polygon.area(), feature * feature)
        self.assertAlmostEqual(result["logo_area_um2"], logo.area() * dbu ** 2)

    def test_all_filled_diagonal_and_mixed_metal(self):
        for diagonal, mixed in ((False, False), (True, False), (False, True)):
            with self.subTest(diagonal=diagonal, mixed=mixed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                templated = root / "chip_logo.svg"
                templated.write_text("templated source must survive")
                rows = [{"y": y, "runs": [[y, y + 1]] if diagonal else [[0, 3]]}
                        for y in range(3)]
                result, logo, merged = self._merge(root, rows, mixed_metal=mixed)
                self._check_rules(result, logo, merged, 0.01)
                self.assertEqual(result["requested_pixels"], 3 if diagonal else 9)
                self.assertEqual(result["rejected_pixels"], 1 if mixed else 0)
                self.assertEqual(result["accepted_pixels"] + result["rejected_pixels"],
                                 result["requested_pixels"])
                self.assertAlmostEqual(result["canvas_area_um2"], 16 * 16)
                self.assertAlmostEqual(result["logo_density"], result["logo_area_um2"] / 256)
                self.assertEqual(templated.read_text(), "templated source must survive")
                svg = ET.parse(root / "chip_logo_geometry.svg").getroot()
                self.assertEqual(len(svg), result["accepted_pixels"])
                for axis, extent in (("width", logo.bbox().width()),
                                     ("height", logo.bbox().height())):
                    self.assertTrue(svg.attrib[axis].endswith("mm"))
                    self.assertAlmostEqual(float(svg.attrib[axis][:-2]), extent * 0.01 / 1000)

    def test_all_dither_modes_produce_rule_clean_regions(self):
        for mode in ("threshold", "ordered", "floyd-steinberg"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "source.png"
                with Image.new("L", (5, 5)) as image:
                    image.putdata([0 if x == y else 128 for y in range(5) for x in range(5)])
                    image.save(source)
                config = {"design": {"name": "chip", "work_dir": str(root)},
                          "logo": {"source": str(source), "feature_um": 4,
                                   "spacing_um": 2, "pitch_um": 6, "max_feature_um": 30,
                                   "width_um": 28, "height_um": 28, "dither": mode}}
                width, height, rows = _mask_rows(prepare(config))
                self.assertEqual((width, height), (5, 5))
                result, logo, merged = self._merge(root, rows, width, height)
                self._check_rules(result, logo, merged, 0.01)

    def test_fractional_database_units_round_minima_up(self):
        with tempfile.TemporaryDirectory() as directory:
            result, logo, merged = self._merge(
                Path(directory), [{"y": y, "runs": [[0, 3]]} for y in range(3)],
                dbu=0.003, feature=0.010, spacing=0.007, pitch=0.017, maximum=0.030,
                canvas_width=0.060, canvas_height=0.060)
            self._check_rules(result, logo, merged, 0.003)
            self.assertGreaterEqual(result["feature_um"], 0.010)
            self.assertGreaterEqual(result["spacing_um"], 0.007)
            self.assertGreaterEqual(result["pitch_um"], result["feature_um"] + result["spacing_um"])
            self.assertLessEqual(result["canvas_width_um"], 0.060)
            self.assertLessEqual(result["canvas_height_um"], 0.060)

    def test_rounded_array_cannot_exceed_requested_canvas(self):
        for width, height in ((0.044, 0.060), (0.060, 0.044)):
            with self.subTest(width=width, height=height), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with self.assertRaisesRegex(RuntimeError, "rounding exceeds requested.*run logo prepare again"):
                    self._merge(root, [{"y": y, "runs": [[0, 3]]} for y in range(3)],
                                dbu=0.003, feature=0.010, spacing=0.007, pitch=0.017,
                                maximum=0.030, canvas_width=width, canvas_height=height)
                self.assertFalse((root / "chip_logo.gds").exists())
                self.assertFalse((root / "chip_logo_geometry.svg").exists())

    def test_large_filled_array_does_not_merge_into_oversized_metal(self):
        with tempfile.TemporaryDirectory() as directory:
            result, logo, merged = self._merge(
                Path(directory), [{"y": y, "runs": [[0, 12]]} for y in range(12)],
                width=12, height=12)
            self._check_rules(result, logo, merged, 0.01)
            self.assertEqual(result["accepted_pixels"], 144)
            self.assertGreater(logo.bbox().width() * 0.01, 30)
            self.assertTrue(all(polygon.bbox().width() * 0.01 <= 30 and
                                polygon.bbox().height() * 0.01 <= 30
                                for polygon in logo.merged().each()))

    def test_maximum_feature_rejects_requested_or_rounded_oversize(self):
        for feature, maximum, dbu, spacing in ((31, 30, 0.01, 2),
                                                (0.010, 0.011, 0.003, 0.007)):
            with self.subTest(feature=feature), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(RuntimeError, "max_feature_um"):
                    self._merge(Path(directory), [{"y": 0, "runs": [[0, 1]]}],
                                width=1, height=1, feature=feature, maximum=maximum,
                                dbu=dbu, spacing=spacing, pitch=feature + spacing)

    def test_out_of_boundary_pixels_are_not_clipped(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "outside the layout"):
                self._merge(Path(directory), [{"y": 0, "runs": [[0, 3]]}],
                            feature=40, spacing=2, pitch=42, maximum=50)

    def test_fractional_dbu_offsets_and_half_unit_centering(self):
        with tempfile.TemporaryDirectory() as directory:
            # Odd bbox-coordinate sums put its center on a half DBU. The
            # quarter-DBU translation then exercises rounding on both axes.
            result, logo, _ = self._merge(
                Path(directory), [{"y": 0, "runs": [[0, 1]]}], width=1, height=1,
                dbu=0.01, feature=0.04, spacing=0.02, pitch=0.06,
                bbox_dbu=(1000, 1000, 1101, 1099), offset_x=0.0025, offset_y=-0.0025)
            box = next(logo.each()).bbox()
            self.assertEqual((box.left, box.bottom, box.right, box.top),
                             (1049, 1047, 1053, 1051))
            self.assertEqual(result["accepted_pixels"], 1)

    def test_offset_pixels_must_remain_inside_layout_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "outside the layout bounding box"):
                self._merge(Path(directory), [{"y": 0, "runs": [[0, 1]]}],
                            width=1, height=1, feature=4, spacing=2, pitch=6,
                            offset_x=49.0)

    def test_spacing_threshold_around_existing_metal(self):
        for gap, accepted in ((1, False), (2, True)):
            with self.subTest(gap=gap), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                # Centered pixel ends at x=5200; a metal edge one DBU inside
                # the requested spacing blocks it, while exact spacing is legal.
                call = lambda: self._merge(
                    root, [{"y": 0, "runs": [[0, 1]]}], width=1, height=1,
                    feature=4, spacing=2, pitch=6,
                    metal_boxes=[(5200 + 200 - (2 - gap), 4500, 5600, 5500)])
                if accepted:
                    result, logo, merged = call()
                    self.assertEqual(result["accepted_pixels"], 1)
                    self.assertTrue(merged.space_check(200).is_empty())
                else:
                    with self.assertRaisesRegex(RuntimeError, "fully blocked"):
                        call()

    def test_acute_metal_polygon_obeys_spacing_keepout(self):
        # For a 3-pixel array the first square is [4200, 4600] x
        # [4800, 5200]. Acute triangles remain outside it, and their nearest
        # points are tested below and safely beyond the 200-DBU spacing.
        # Extra clearance on the accepted case accounts for acute-tip sizing.
        for gap, accepted in ((199, 2), (250, 3)):
            with self.subTest(gap=gap), tempfile.TemporaryDirectory() as directory:
                tip_x = 4200 - gap
                result, logo, merged = self._merge(
                    Path(directory), [{"y": 0, "runs": [[0, 3]]}], width=3, height=1,
                    feature=4, spacing=2, pitch=6,
                    metal_polygons=[[(tip_x, 5000), (tip_x - 101, 4990),
                                     (tip_x - 101, 5010)]])
                self._check_rules(result, logo, merged, 0.01)
                self.assertEqual(result["accepted_pixels"], accepted)
                self.assertEqual(result["rejected_pixels"], 3 - accepted)

    def test_diagonal_acute_metal_near_square_corner_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            # The acute tip is outside the lower-left corner by (141, 141)
            # DBU, about 199.4 DBU Euclidean distance, below 200 DBU spacing.
            result, logo, merged = self._merge(
                Path(directory), [{"y": 0, "runs": [[0, 3]]}], width=3, height=1,
                feature=4, spacing=2, pitch=6,
                metal_polygons=[[(4059, 4659), (3940, 4650), (4050, 4540)]])
            self._check_rules(result, logo, merged, 0.01)
            self.assertEqual(result["accepted_pixels"], 2)
            self.assertEqual(result["rejected_pixels"], 1)


if __name__ == "__main__":
    unittest.main()
