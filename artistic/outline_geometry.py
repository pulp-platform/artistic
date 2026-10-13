# Copyright 2026 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
"""Shared-boundary Bezier geometry for labeled rasters."""

from __future__ import annotations

import math
import warnings
from collections import defaultdict

import numpy as np
from scipy import ndimage as ndi
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import unary_union
from shapely.strtree import STRtree


def _rounded(raw, radius, tolerance, round_start):
    points = np.asarray(LineString(raw).simplify(tolerance, preserve_topology=True).coords)
    closed = round_start and np.array_equal(points[0], points[-1])
    if closed:
        points = points[:-1]
    if len(points) < 3:
        return [points[[0, -1]]]
    corners = range(len(points)) if closed else range(1, len(points) - 1)
    cuts = []
    for i in corners:
        prev, corner, nxt = points[i - 1], points[i], points[(i + 1) % len(points)]
        before, after = np.linalg.norm(corner - prev), np.linalg.norm(nxt - corner)
        distance = min(radius, before / 2, after / 2)
        entry = corner + distance / before * (prev - corner)
        leave = corner + distance / after * (nxt - corner)
        cuts.append(np.array([entry, entry + 2 / 3 * (corner - entry),
                              leave + 2 / 3 * (corner - leave), leave]))
    result, current = [], cuts[0][0] if closed else points[0]
    start = current.copy()
    for cut in cuts:
        if np.linalg.norm(cut[0] - current) > 1e-12:
            result.append(np.array([current, cut[0]]))
        result.append(cut)
        current = cut[-1]
    end = start if closed else points[-1]
    if np.linalg.norm(end - current) > 1e-12:
        result.append(np.array([current, end]))
    return result


def _sample(segments):
    points = [segments[0][0]]
    for seg in segments:
        if len(seg) == 2:
            points.append(seg[-1])
            continue
        length = np.linalg.norm(np.diff(seg, axis=0), axis=1).sum()
        t = np.linspace(0, 1, max(12, math.ceil(length / .05)) + 1)[1:]
        u = 1 - t
        curve = (u[:, None]**3 * seg[0] + 3*(u*u*t)[:, None]*seg[1]
                 + 3*(u*t*t)[:, None]*seg[2] + t[:, None]**3*seg[3])
        curve[-1] = seg[-1]
        points.extend(curve)
    return np.asarray(points)


def _arcs(labels, corners):
    padded = np.pad(labels, 1)
    graph, edges = defaultdict(list), set()

    def add(a, b):
        a, b = (a[0] - 1, a[1] - 1), (b[0] - 1, b[1] - 1)
        key = tuple(sorted((a, b)))
        edges.add(key)
        graph[a].append(key)
        graph[b].append(key)

    for y, x in zip(*np.nonzero(padded[:, :-1] != padded[:, 1:])):
        add((x + 1, y), (x + 1, y + 1))
    for y, x in zip(*np.nonzero(padded[:-1] != padded[1:])):
        add((x, y + 1), (x + 1, y + 1))
    pinned = set(corners)
    junction = lambda node: len(graph[node]) != 2 or node in pinned
    seen, arcs = set(), []
    for first in sorted(edges, key=lambda edge: (not any(junction(node) for node in edge), edge)):
        if first in seen:
            continue
        ends = [node for node in first if junction(node)]
        start = ends[0] if ends else first[0]
        points, node, edge = [start], start, first
        while True:
            seen.add(edge)
            node = edge[1] if node == edge[0] else edge[0]
            points.append(node)
            if node == start or junction(node):
                break
            choices = [candidate for candidate in graph[node]
                       if candidate not in seen]
            edge = choices[0]
        arcs.append((np.asarray(points, dtype=float), not ends))
    return arcs


def _exact_region(labels, components, label, component, scale):
    sx, sy = scale
    cells = [box(x, y, x + 1, y + 1)
             for y, x in zip(*np.nonzero((labels == label) & (components == component)))]
    polygon = unary_union(cells)
    commands = []
    for ring in [polygon.exterior, *polygon.interiors]:
        coords = list(ring.coords)
        commands.append(f"M {coords[0][0]*sx:.6f} {coords[0][1]*sy:.6f}")
        commands.extend(f"L {x*sx:.6f} {y*sy:.6f}" for x, y in coords[1:])
        commands.append("Z")
    return {"label": label, "polygon": polygon, "path": " ".join(commands)}


def regions(labels: np.ndarray, corners=(), radius=2.0, tolerance=0.9,
            output_scale=(1.0, 1.0), protected=()):
    """Return grid-coordinate polygons and closed SVG paths at output_scale."""
    sx, sy = output_scale
    components = {int(v): ndi.label(labels == v)[0] for v in np.unique(labels) if v}

    def exact():
        warnings.warn("Could not round module boundaries safely; using raster outlines.",
                      RuntimeWarning, stacklevel=3)
        return [_exact_region(labels, raster, owner, part, (sx, sy))
                for owner, raster in components.items()
                for part in range(1, int(raster.max()) + 1)]

    arcs = _arcs(labels, set(corners))
    curves = [_rounded(raw, radius, tolerance, free) for raw, free in arcs]
    samples = [_sample(curve) for curve in curves]
    # An unsafe shared arc is restored for both incident regions at once.
    for _ in range(len(arcs) + 1):
        lines = [LineString(points) for points in samples]
        tree, bad = STRtree(lines), set()
        for i, line in enumerate(lines):
            if not line.is_simple:
                bad.add(i)
            for other in tree.query(line, predicate="intersects"):
                j = int(other)
                if j <= i:
                    continue
                crossing = line.intersection(lines[j])
                common = set(map(tuple, samples[i][[0, -1]])) & set(map(tuple, samples[j][[0, -1]]))
                for point in common:
                    crossing = crossing.difference(Point(point).buffer(1e-7))
                if not crossing.is_empty:
                    bad.update((i, j))
        if not bad:
            break
        for i in bad:
            raw = arcs[i][0]
            curves[i] = [np.array([a, b]) for a, b in zip(raw, raw[1:])]
            samples[i] = raw
    else:
        raise RuntimeError("shared boundaries remain invalid after raw fallback")
    directed = defaultdict(list)
    for i, (raw, _) in enumerate(arcs):
        a, b = raw[:2]
        delta = b - a
        normal = np.array([-delta[1], delta[0]])
        for side, forward in ((1, True), (-1, False)):
            x, y = np.floor((a + b) / 2 + side * .25 * normal).astype(int)
            if 0 <= y < labels.shape[0] and 0 <= x < labels.shape[1]:
                label = int(labels[y, x])
                if not label:
                    continue
                component = int(components[label][y, x])
                directed[label, component].append((i, forward, raw if forward else raw[::-1]))
    result = []
    for (label, component), entries in sorted(directed.items()):
        outgoing = defaultdict(list)
        for index, entry in enumerate(entries):
            outgoing[tuple(entry[2][0])].append(index)
        used, rings = set(), []
        for first in range(len(entries)):
            if first in used:
                continue
            index, start = first, tuple(entries[first][2][0])
            raw_ring, segments = [], []
            while True:
                used.add(index)
                arc, forward, raw = entries[index]
                raw_ring.extend(raw if not raw_ring else raw[1:])
                part = curves[arc] if forward else [seg[::-1] for seg in curves[arc][::-1]]
                segments.extend(part)
                end = tuple(raw[-1])
                if end == start:
                    break
                choices = [candidate for candidate in outgoing[end] if candidate not in used]
                if not choices:
                    raise RuntimeError("open region boundary")
                incoming = raw[-2] - raw[-1]
                angle = math.atan2(incoming[1], incoming[0])
                # Keep a hole touching the outer boundary at a point as a separate ring.
                index = max(choices, key=lambda k: (angle - math.atan2(
                    entries[k][2][1][1] - entries[k][2][0][1],
                    entries[k][2][1][0] - entries[k][2][0][0])) % (2 * math.pi))
            raw_ring = np.asarray(raw_ring)
            area = np.sum(raw_ring[:-1, 0] * raw_ring[1:, 1]
                          - raw_ring[1:, 0] * raw_ring[:-1, 1]) / 2
            rings.append((float(area), raw_ring, _sample(segments), segments))
        shells = [ring for ring in rings if ring[0] > 0]
        holes = [ring for ring in rings if ring[0] < 0]
        expected = np.count_nonzero((labels == label) & (components[label] == component))
        if not shells or abs(sum(ring[0] for ring in rings) - expected) > 1e-8:
            raise RuntimeError("region boundaries do not cover their raster component")
        assignments = defaultdict(list)
        for hole in holes:
            point = Polygon(hole[1]).representative_point()
            parents = [shell for shell in shells if Polygon(shell[1]).covers(point)]
            if not parents:
                raise RuntimeError("region hole has no enclosing boundary")
            assignments[id(min(parents, key=lambda shell: shell[0]))].append(hole)
        for shell in shells:
            inner = assignments[id(shell)]
            polygon = Polygon(shell[2], [hole[2] for hole in inner])
            # Restore the whole partition together, never only one side of a border.
            if not polygon.is_valid:
                return exact()
            commands = []
            for ring in [shell] + inner:
                segs = ring[3]
                x, y = segs[0][0]
                commands.append(f"M {x*sx:.6f} {y*sy:.6f}")
                for seg in segs:
                    coords = seg[1:] * np.array([sx, sy])
                    commands.append(("C" if len(seg) == 4 else "L") + " "
                                    + " ".join(f"{value:.6f}" for value in coords.ravel()))
                commands.append("Z")
            result.append({"label": label, "polygon": polygon, "path": " ".join(commands)})
    polygons = [record["polygon"] for record in result]
    tree = STRtree(polygons)
    for i, polygon in enumerate(polygons):
        for j in tree.query(polygon, predicate="intersects"):
            if j > i and polygon.intersection(polygons[j]).area > 1e-8:
                return exact()
    for label, left, top, right, bottom in protected:
        owner = unary_union([record["polygon"] for record in result if record["label"] == label])
        if box(left, top, right, bottom).difference(owner).area > 1e-8:
            return exact()
    return result
