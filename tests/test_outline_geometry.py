# Copyright 2026 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

import unittest
import warnings

import numpy as np
from shapely.geometry import Point

from artistic.outline_geometry import regions


class OutlineGeometryTests(unittest.TestCase):
    def test_hole_and_disconnected_components(self):
        labels = np.ones((7, 9), dtype=np.int32)
        labels[2:5, 2:5] = 0
        labels[1:3, 7:9] = 2
        shapes = regions(labels, radius=0)
        one = [shape for shape in shapes if shape["label"] == 1]
        self.assertEqual(len(one), 1)
        self.assertEqual(len(one[0]["polygon"].interiors), 1)
        self.assertAlmostEqual(one[0]["polygon"].area, 50)
        two = [shape for shape in shapes if shape["label"] == 2]
        self.assertEqual(len(two), 1)
        self.assertTrue(two[0]["polygon"].covers(Point(8, 2)))

    def test_shared_curved_border_is_identical(self):
        labels = np.ones((20, 20), dtype=np.int8)
        labels[:, 12:] = 2
        labels[10:, 8:] = 2
        shapes = {s["label"]: s for s in regions(labels, radius=1.5, tolerance=0)}
        path1, path2 = shapes[1]["path"], shapes[2]["path"]
        forward = (
            "C 12.000000 9.500000 11.500000 10.000000 10.500000 10.000000",
            "C 8.500000 10.000000 8.000000 10.500000 8.000000 11.500000",
        )
        reverse = (
            "C 11.500000 10.000000 12.000000 9.500000 12.000000 8.500000",
            "C 8.000000 10.500000 8.500000 10.000000 9.500000 10.000000",
        )
        for command in forward:
            self.assertIn(command, path1)
        for command in reverse:
            self.assertIn(command, path2)

    def test_background_is_not_exported_as_a_module(self):
        labels = np.zeros((20, 20), dtype=np.int8)
        labels[3:17, 3:17] = 1
        shapes = regions(labels, radius=2, tolerance=0)
        self.assertEqual({shape["label"] for shape in shapes}, {1})
        self.assertIn("C ", shapes[0]["path"])
        self.assertLess(shapes[0]["polygon"].area, 14 * 14)

    def test_closed_arc_keeps_its_shared_junction(self):
        labels = np.array([[1, 1, 2], [1, 3, 1], [1, 1, 1]], dtype=np.int8)
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            shapes = regions(labels, radius=.4, tolerance=0)
        one = next(shape for shape in shapes if shape["label"] == 1)
        self.assertTrue(one["polygon"].covers(Point(2.2, 1.1)))

    def test_swallowed_island_restores_partition_with_warning(self):
        labels = np.ones((100, 100), dtype=np.int8)
        labels[50:, 50:] = 0
        labels[51, 51] = 2
        with self.assertWarnsRegex(RuntimeWarning, "using raster outlines"):
            shapes = regions(labels, radius=25, tolerance=0)
        self.assertEqual([shape["polygon"].area for shape in shapes], [7500, 1])
        self.assertAlmostEqual(shapes[0]["polygon"].intersection(shapes[1]["polygon"]).area, 0)

    def test_path_output_scale_does_not_scale_grid_polygon(self):
        labels = np.ones((3, 3), dtype=np.int16)
        shape = regions(labels, radius=0.25, output_scale=(2, 3))[0]
        self.assertEqual(shape["polygon"].bounds, (0, 0, 3, 3))
        self.assertIn("6.000000 8.250000", shape["path"])
        self.assertIn("5.500000 9.000000", shape["path"])

    def test_small_rasters_keep_every_cell_without_overlap(self):
        generator = np.random.default_rng(42)
        for _ in range(30):
            labels = generator.integers(0, 4, size=(5, 6), dtype=np.int8)
            with warnings.catch_warnings():
                warnings.simplefilter("error", RuntimeWarning)
                shapes = regions(labels, radius=0, tolerance=0)
            self.assertTrue(all(shape["polygon"].is_valid for shape in shapes))
            for label in (1, 2, 3):
                area = sum(shape["polygon"].area for shape in shapes if shape["label"] == label)
                self.assertAlmostEqual(area, np.count_nonzero(labels == label))
            for i, shape in enumerate(shapes):
                for other in shapes[i + 1:]:
                    self.assertAlmostEqual(shape["polygon"].intersection(other["polygon"]).area, 0)


if __name__ == "__main__":
    unittest.main()
