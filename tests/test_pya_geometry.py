# Copyright 2026 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from logo_geometry import box_is_inside, iter_pixel_boxes, select_pixel_boxes


def intersects(box, keepout):
    """Use positive-area rectangle intersection for the test keepout."""
    left, bottom, right, top = box
    kl, kb, kr, kt = keepout
    return left < kr and right > kl and bottom < kt and top > kb


class LogoGeometryTests(unittest.TestCase):
    def test_box_boundary_check(self):
        self.assertTrue(box_is_inside((0, 0, 10, 10), (0, 0, 20, 20)))
        self.assertFalse(box_is_inside((-1, 0, 10, 10), (0, 0, 20, 20)))

    def test_intersecting_pixel_is_rejected_as_a_whole(self):
        rows = [{"y": 0, "runs": [[0, 3]]}]
        boxes = select_pixel_boxes(
            rows, 3, 1, [0, 0, 50, 10], 10,
            lambda box: intersects(box, (24, 0, 26, 10)))

        self.assertEqual(boxes, [(0, 0, 10, 10), (40, 0, 50, 10)])
        self.assertTrue(all((right - left, top - bottom) == (10, 10)
                            for left, bottom, right, top in boxes))

    def test_pixels_remain_grid_aligned_with_center_and_offset(self):
        rows = [{"y": 0, "runs": [[0, 2]]}, {"y": 1, "runs": [[1, 2]]}]
        boxes = list(iter_pixel_boxes(rows, 2, 2, [0, 0, 40, 40], 10,
                                      offset_x_dbu=5, offset_y_dbu=-5))

        self.assertEqual(boxes, [
            (10, 20, 20, 30),
            (30, 20, 40, 30),
            (30, 0, 40, 10),
        ])
        for left, bottom, right, top in boxes:
            self.assertEqual((right - left, top - bottom), (10, 10))
            self.assertEqual((left - 10) % 20, 0)
            self.assertEqual(bottom % 20, 0)

    def test_explicit_pitch_preserves_isolated_pixels(self):
        boxes = list(iter_pixel_boxes([{"y": 0, "runs": [[0, 3]]}],
                                      3, 1, [0, 0, 40, 40], 4, pitch_dbu=6))
        self.assertEqual([box[0] for box in boxes], [12, 18, 24])
        self.assertTrue(all(box[2] - box[0] == 4 for box in boxes))

    def test_touching_pitch_and_fractional_width_are_invalid(self):
        for feature, pitch in ((4, 4), (4, 3), (4.1, 6), (4, 6.1)):
            with self.subTest(feature=feature, pitch=pitch):
                with self.assertRaises(ValueError):
                    list(iter_pixel_boxes([], 1, 1, [0, 0, 40, 40],
                                          feature, pitch_dbu=pitch))


if __name__ == "__main__":
    unittest.main()
