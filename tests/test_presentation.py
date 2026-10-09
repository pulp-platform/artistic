# Copyright 2026 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

import unittest

from PIL import Image

from artistic.presentation import decorate, presentation_geometry, shadow_image, shadow_options
from artistic.project import ProjectError


class PresentationTests(unittest.TestCase):
    def setUp(self):
        self.record = {"resolution": [20, 12],
                       "layout": {"bbox_um": [12, 23, 32, 35]},
                       "gds": {"viewport_um": [10, 20, 40, 38]}}

    def test_disabled_keeps_original_geometry_without_layout(self):
        render = {"shadow": {"enabled": False}}
        record = {"resolution": [5, 3]}
        with Image.new("RGBA", (5, 3), (1, 2, 3, 4)) as source:
            result = decorate(source, render, record, (255, 255, 255, 255))
            try:
                self.assertEqual(result.size, source.size)
                self.assertEqual(result.tobytes(), source.tobytes())
                self.assertIsNot(result, source)
            finally:
                result.close()
        self.assertIsNone(shadow_options({}))
        self.assertIsNone(shadow_options(render))
        self.assertEqual(presentation_geometry({}, record),
                         {"padding": 0, "size": [5, 3], "box": [0, 0, 5, 3]})

    def test_defaults_and_nonzero_origin_mapping(self):
        options = shadow_options({"shadow": {}})
        self.assertEqual(options, {"enabled": True, "padding_px": 80, "blur_px": 16.0,
                                   "offset_px": [7.0, 12.0], "opacity": .28,
                                   "color": "#000000"})
        geometry = presentation_geometry({"shadow": {"padding_px": 2}}, self.record)
        self.assertEqual(geometry, {"padding": 2, "size": [24, 16],
                                    "box": [1, 2, 15, 10]})

    def test_viewport_clips_layout_and_rejects_no_intersection(self):
        record = {"resolution": [10, 6],
                  "layout": {"bbox_um": [0, 0, 20, 12]},
                  "gds": {"viewport_um": [5, 3, 15, 9]}}
        self.assertEqual(presentation_geometry({"shadow": {"padding_px": 0}}, record)["box"],
                         [0, 0, 10, 6])
        record["layout"]["bbox_um"] = [20, 0, 30, 12]
        with self.assertRaisesRegex(ProjectError, "does not intersect"):
            presentation_geometry({"shadow": {}}, record)

    def test_integral_footprint_edges_do_not_gain_a_rounding_pixel(self):
        record = {"resolution": [10, 10],
                  "layout": {"bbox_um": [.3, .2, .7, .8]},
                  "gds": {"viewport_um": [.1, 0, 1.1, 1]}}
        self.assertEqual(presentation_geometry({"shadow": {}}, record)["box"], [2, 2, 6, 8])

    def test_decorate_preserves_pixels_only_inside_layout_box(self):
        render = {"shadow": {"padding_px": 2, "blur_px": 0,
                              "offset_px": [2, 1], "opacity": 1,
                              "color": "#123456"}}
        with Image.new("RGBA", (20, 12), (200, 10, 20, 0)) as source:
            source.putpixel((1, 2), (9, 8, 7, 6))
            source.putpixel((19, 11), (5, 4, 3, 2))
            result = decorate(source, render, self.record, (240, 241, 242, 255))
            try:
                self.assertEqual(result.size, (24, 16))
                self.assertEqual(result.getpixel((3, 4)), (9, 8, 7, 6))
                self.assertEqual(result.getpixel((17, 11)), (18, 52, 86, 255))
                self.assertEqual(result.getpixel((18, 12)), (18, 52, 86, 255))
                self.assertEqual(result.getpixel((21, 13)), (240, 241, 242, 255))
                self.assertEqual(source.getpixel((1, 2)), (9, 8, 7, 6))
            finally:
                result.close()

    def test_signed_offsets_and_shadow_opacity_alpha(self):
        render = {"shadow": {"padding_px": 4, "blur_px": 0,
                              "offset_px": [-2, -1], "opacity": .5,
                              "color": "#336699"}}
        shadow = shadow_image(render, self.record)
        try:
            # Layout box [1, 2, 15, 10] shifted left/up and padded.
            self.assertEqual(shadow.getpixel((3, 5)), (51, 102, 153, 128))
            self.assertEqual(shadow.getpixel((18, 12))[3], 0)
        finally:
            shadow.close()

    def test_transparent_footprint_hides_shadow_and_source_size_is_checked(self):
        render = {"shadow": {"padding_px": 2, "blur_px": 2,
                              "offset_px": [2, 1], "opacity": 1}}
        shadow = shadow_image(render, self.record)
        try:
            self.assertEqual(shadow.getpixel((3, 4))[3], 0)
            self.assertGreater(shadow.getpixel((18, 12))[3], 0)
        finally:
            shadow.close()
        with Image.new("RGBA", (19, 12), (1, 2, 3, 4)) as wrong_size:
            with self.assertRaisesRegex(ProjectError, "match record resolution"):
                decorate(wrong_size, render, self.record, (255, 255, 255, 255))

    def test_invalid_options(self):
        invalid = [
            {"blur": 16}, {"enabled": False, "blur": 16},
            {"enabled": 1}, {"enabled": "yes"}, {"padding_px": True},
            {"padding_px": -1}, {"padding_px": 10001}, {"blur_px": True},
            {"blur_px": float("nan")}, {"blur_px": -1}, {"offset_px": [1]},
            {"offset_px": [True, 0]}, {"offset_px": [0, 10001]},
            {"blur_px": "1"}, {"offset_px": ["1", 0]},
            {"opacity": True}, {"opacity": 1.1}, {"opacity": float("inf")},
            {"opacity": "0.5"},
            {"color": "transparent"}, {"color": "#12345678"},
            {"color": "not-a-color"},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ProjectError):
                shadow_options({"shadow": value})
        for value in (None, False, "yes"):
            with self.subTest(table=value), self.assertRaises(ProjectError):
                shadow_options({"shadow": value})
        with self.assertRaisesRegex(ProjectError, r"\[render.shadow\].color"):
            shadow_options({"shadow": {"color": "not-a-color"}})
        with self.assertRaisesRegex(ProjectError, r"\[render.shadow\].opacity.*between 0 and 1"):
            shadow_options({"shadow": {"opacity": 2}})

    def test_invalid_resolution_and_bounds(self):
        for resolution in ([0, 2], [2.0, 2], [True, 2], [2], [2, 3, 4]):
            with self.subTest(resolution=resolution), self.assertRaises(ProjectError):
                presentation_geometry({}, {"resolution": resolution})
        render = {"shadow": {}}
        for bbox, viewport in (([0, 0, 0, 1], [0, 0, 2, 2]),
                               (["0", 0, 1, 1], [0, 0, 2, 2]),
                               ([0, 0, float("inf"), 1], [0, 0, 2, 2]),
                               ([0, 0, 1, 1], [0, 0, float("nan"), 2]),
                               ([0, 0, 1, 1], [2, 0, 1, 2])):
            record = {"resolution": [4, 4], "layout": {"bbox_um": bbox},
                      "gds": {"viewport_um": viewport}}
            with self.subTest(bbox=bbox, viewport=viewport), self.assertRaises(ProjectError):
                presentation_geometry(render, record)


if __name__ == "__main__":
    unittest.main()
