# Copyright 2026 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

import unittest
import xml.etree.ElementTree as ET

import numpy as np

from artistic.outline_density import annotate_regions, density_labels
from artistic.outline_geometry import regions
from artistic.project import ProjectError
from shapely.geometry import box
from shapely.ops import unary_union


class DensityTests(unittest.TestCase):
    def test_empty_modules(self):
        root = ET.Element("{http://www.w3.org/2000/svg}svg")
        self.assertEqual(annotate_regions(root, [], {}, (100, 80), (50, 40), [], {}), [])

    def test_label_sizes_vary_gently_with_region_area(self):
        root = ET.Element("{http://www.w3.org/2000/svg}svg")
        groups = [[((2, 2, 4, 4), True)], [((10, 10, 15, 20), True)],
                  [((10, 30, 20, 40), True)], [((30, 10, 50, 20), True)],
                  [((30, 30, 90, 90), True)]]
        modules = {name: {"label": name, "color": "#ff0000"}
                   for name in ("Small", "Below", "Medium", "Above", "Large")}
        entries = annotate_regions(root, groups, modules, (100, 100), (100, 100),
                                   [], {"font_size": 20})
        self.assertEqual([spec["label"] for spec, _, _, _ in entries], list(modules))
        np.testing.assert_allclose([spec["font_size"] for spec, _, _, _ in entries],
                                   [17, 20 * .5**.1, 20, 20 * 2**.1, 23])
        self.assertTrue(all("font_size" not in spec for spec in modules.values()))
        self.assertEqual(annotate_regions(ET.Element("{http://www.w3.org/2000/svg}svg"),
                                         groups, modules, (100, 100), (100, 100), [], {"font_size": 0}), [])

    def test_overlapping_macro_groups_are_rejected(self):
        groups = [[((20, 20, 40, 40), True)], [((30, 30, 50, 50), True)]]
        with self.assertRaises(ProjectError):
            density_labels(groups, (100, 100), (100, 100), [], 12, 10)

    def test_final_vectors_preserve_macro_footprints(self):
        wrapped = [((20, 20, 40, 40), True), ((47, 24, 52, 29), False),
                   ((40, 20, 45, 25), False)]
        for groups in ([wrapped], [wrapped, [((35, 25, 45, 35), False)]]):
            protected = []
            labels, corners = density_labels(groups, (100, 100), (100, 100), [], 2, 0,
                                             protected=protected)
            shapes = regions(labels, corners=corners, radius=20, tolerance=4.6875,
                             protected=protected)
            final = unary_union([shape["polygon"] for shape in shapes if shape["label"] == 1])
            self.assertAlmostEqual(box(20, 20, 40, 40).difference(final).area, 0)

    def test_off_grid_abutting_macros_on_non_square_canvas(self):
        groups = [[((40, 40, 80.6, 80), True)], [((80.6, 40, 120, 80), True)]]
        labels, _ = density_labels(groups, (200, 100), (100, 50), [], 12, 0)
        self.assertEqual(labels.shape, (50, 100))
        self.assertTrue(np.all(labels[20:40, 20:40] == 1))
        self.assertTrue(np.all(labels[20:40, 40:60] == 2))

    def test_macro_vector_keeps_rounded_geometry_and_footprint(self):
        groups = [[((20, 20, 40, 40), True), ((40, 20, 48, 26), False),
                   ((44, 26, 48, 34), False)]]
        protected = []
        labels, corners = density_labels(groups, (100, 100), (100, 100), [], 2, 0,
                                         protected=protected)
        shapes = regions(labels, corners=corners, radius=2, tolerance=.5,
                         protected=protected)
        final = unary_union([shape["polygon"] for shape in shapes])
        self.assertAlmostEqual(box(20, 20, 40, 40).difference(final).area, 0)
        self.assertTrue(any("C " in shape["path"] for shape in shapes))

    def test_seed_preserves_a_sparse_group(self):
        groups = [[((10, 10, 90, 90), False)], [((49, 49, 50, 50), False)]]
        labels, _ = density_labels(groups, (100, 100), (100, 100), [(2, 50, 50)], 12, 50)
        self.assertEqual(labels[50, 50], 2)

    def test_conflicting_seeds_do_not_silently_change_groups(self):
        with self.assertRaises(ProjectError):
            density_labels([[], []], (100, 100), (100, 100),
                           [(1, 50, 50), (2, 50, 50)], 12, 0)

    def test_merged_region_keeps_both_grouped_labels(self):
        root = ET.Element("{http://www.w3.org/2000/svg}svg")
        modules = {"i_core": {"label": "Core", "color": "#ff0000"}}
        entries = annotate_regions(root, [[((10, 10, 90, 90), True)]], modules,
                                   (200, 100), (100, 50), [(1, 30, 30), (1, 70, 70)],
                                   {"font_size": 10, "fill_opacity": .2,
                                    "avoid_label_overlap": True, "stroke_border_width": 1.6,
                                    "stroke_width": "3"})
        self.assertEqual([(x, y) for _, x, y, _ in entries], [(30, 30), (70, 70)])
        self.assertIs(entries[0][3], entries[1][3])
        for _, _, _, component in entries:
            component.close()
        ns = "{http://www.w3.org/2000/svg}"
        fills = root.find(ns + 'g[@id="Region-fills"]')
        borders = root.find(ns + 'g[@id="Modules"]')
        self.assertIsNone(fills.get("filter"))
        self.assertEqual(borders.get("filter"), "url(#module-border)")
        self.assertIsNotNone(root.find(ns + 'defs/' + ns + 'filter[@id="module-border"]'))
        paths = borders.findall(".//" + ns + "path")
        self.assertEqual(borders.find(".//" + ns + 'g[@stroke-width]').get("stroke-width"), "6.0")
        self.assertEqual(len(fills), len(paths))
        self.assertTrue(all(fill.get("fill-opacity") == "0.2" and fill.get("stroke") == "none"
                            for fill in fills))
        self.assertTrue(all(path.get("d").endswith("Z") for path in paths))

    def test_unseeded_island_does_not_add_a_label(self):
        root = ET.Element("{http://www.w3.org/2000/svg}svg")
        modules = {"i_core": {"label": "Core", "color": "#ff0000"}}
        groups = [[((10, 10, 45, 45), False), ((75, 75, 90, 90), False)]]
        entries = annotate_regions(root, groups, modules, (100, 100), (100, 100),
                                   [(1, 25, 25)], {"font_size": 10, "smoothing_px": 2,
                                                   "min_area_pixels": 0})
        self.assertEqual(len(entries), 1)
        self.assertIsNone(entries[0][3])
        paths = root.findall(".//{http://www.w3.org/2000/svg}path")
        self.assertEqual(len(paths), 2)


if __name__ == "__main__":
    unittest.main()
