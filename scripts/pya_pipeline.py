#!/usr/bin/env python3
# Copyright 2026 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

"""Small, non-interactive KLayout/PYA worker used by the ArtistIC CLI.

The CLI deliberately does not import :mod:`pya`: it is only available in the
KLayout Python runtime.  Requests and results are JSON files so all paths and
errors are explicit and the host-side stages remain ordinary Python.
"""

import json
import os
import sys

import pya

# KLayout does not consistently add the script directory to ``sys.path``
# when a script is launched with ``-r``.  Keep the pure geometry helper next
# to this worker and make that import explicit for both direct and KLayout
# execution.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from logo_geometry import box_is_inside, select_pixel_boxes


def _layer_index(layout, layer, datatype):
    for index in layout.layer_indexes():
        info = layout.get_info(index)
        if info.layer == int(layer) and info.datatype == int(datatype):
            return index
    return None


def _shape_count(cell, layer_index):
    if layer_index is None:
        return 0
    iterator = cell.begin_shapes_rec(layer_index)
    # Presence is all the resolver needs.  Stopping at the first recursive
    # shape avoids walking million-shape standard-cell hierarchies during
    # every stage; detailed polygon extraction is done only for the selected
    # logo metal.
    return 0 if iterator.at_end() else 1


def inspect(request):
    os.makedirs(os.path.dirname(os.path.abspath(request["result"])), exist_ok=True)
    layout = pya.Layout()
    layout.read(request["gds"])
    tops = layout.top_cells()
    if len(tops) != 1:
        names = [cell.name for cell in tops]
        raise RuntimeError("input GDS must have exactly one top cell; found " +
                           ", ".join(names))
    top = tops[0]
    bbox = top.bbox()
    layers = []
    for index in layout.layer_indexes():
        info = layout.get_info(index)
        count = _shape_count(top, index)
        if count:
            layers.append({"layer": info.layer, "datatype": info.datatype,
                           "source": "%d/%d" % (info.layer, info.datatype),
                           "shapes": count})
    result = {"dbu": layout.dbu, "top_cell": top.name,
              "bbox_dbu": [bbox.left, bbox.bottom, bbox.right, bbox.top],
              "bbox_um": [bbox.left * layout.dbu, bbox.bottom * layout.dbu,
                          bbox.right * layout.dbu, bbox.top * layout.dbu],
              "present_layers": layers}
    with open(request["result"], "w") as stream:
        json.dump(result, stream, indent=2, sort_keys=True)


def logo_merge(request):
    layout = pya.Layout()
    layout.read(request["gds"])
    tops = layout.top_cells()
    if len(tops) != 1:
        raise RuntimeError("input GDS must have exactly one top cell")
    original = tops[0]
    source_index = _layer_index(layout, request["layer"], request["datatype"])
    if source_index is None or _shape_count(original, source_index) == 0:
        raise RuntimeError("selected logo metal layer has no shapes")

    # Convert the input metal to a region once.  The region also removes all
    # hierarchy ambiguity: the generated logo is a flat set of polygons.
    metal = pya.Region(original.begin_shapes_rec(source_index))
    logo_region = pya.Region()
    feature = float(request["feature_um"]) / layout.dbu
    width_px = int(request["width_px"])
    height_px = int(request["height_px"])
    offset_x = float(request.get("offset_x_um", 0.0)) / layout.dbu
    offset_y = float(request.get("offset_y_um", 0.0)) / layout.dbu
    # Existing top metal is a keepout for the artwork.  Expand it by one
    # feature so adjacent polygons do not create sub-resolution slivers.  Each
    # requested artwork pixel is tested independently and either inserted as a
    # complete feature-sized rectangle or rejected in its entirety.
    keepout = metal.sized(max(1, int(round(feature))))

    def is_blocked(box):
        if not box_is_inside(box, request["bbox_dbu"]):
            raise RuntimeError("logo artwork extends outside the layout bounding box")
        # ``overlapping`` uses KLayout's spatial index and accepts a Box
        # directly, avoiding a full boolean Region operation for every pixel.
        return not keepout.overlapping(pya.Box(*box)).is_empty()

    for box in select_pixel_boxes(
            request["rows"], width_px, height_px, request["bbox_dbu"],
            feature, is_blocked, offset_x, offset_y):
        logo_region.insert(pya.Box(*box))
    if logo_region.is_empty():
        raise RuntimeError("logo mask is fully blocked by selected metal")

    # Standalone logo GDS.
    logo_layout = pya.Layout()
    logo_layout.dbu = layout.dbu
    logo_index = logo_layout.layer(request["layer"], request["datatype"])
    logo_cell = logo_layout.create_cell(request["logo_cell"])
    logo_cell.shapes(logo_index).insert(logo_region)
    logo_layout.write(request["logo_gds"])

    # The chip GDS has one explicit top cell containing the original design and
    # the logo.  Keeping the original cell intact makes downstream hierarchy
    # and source mapping useful.
    target_index = layout.layer(request["layer"], request["datatype"])
    logo_name = request["logo_cell"]
    suffix = 1
    while layout.has_cell(logo_name):
        logo_name = "%s_%d" % (request["logo_cell"], suffix); suffix += 1
    chip_logo = layout.create_cell(logo_name)
    chip_logo.shapes(target_index).insert(logo_region)
    chip_name = request["chip_cell"]
    suffix = 1
    while layout.has_cell(chip_name):
        chip_name = "%s_%d" % (request["chip_cell"], suffix); suffix += 1
    merged = layout.create_cell(chip_name)
    merged.insert(pya.CellInstArray(original.cell_index(), pya.Trans()))
    merged.insert(pya.CellInstArray(chip_logo.cell_index(), pya.Trans()))
    layout.write(request["chip_gds"])
    os.makedirs(os.path.dirname(os.path.abspath(request["result"])), exist_ok=True)
    with open(request["result"], "w") as stream:
        json.dump({"logo_shapes": logo_region.size(), "logo_bbox_dbu":
                   [logo_region.bbox().left, logo_region.bbox().bottom,
                    logo_region.bbox().right, logo_region.bbox().top]}, stream)


def main():
    request_path = os.environ.get("ARTISTIC_PYA_REQUEST")
    if not request_path:
        # KLayout's -rd mechanism exposes values as globals, but the path is
        # intentionally copied to an environment variable by the CLI when
        # available.  This fallback keeps direct ``klayout -r`` useful.
        request_path = globals().get("request")
    if not request_path:
        raise RuntimeError("ARTISTIC_PYA_REQUEST was not supplied")
    with open(str(request_path)) as stream:
        request = json.load(stream)
    operation = request.get("operation")
    if operation == "inspect":
        inspect(request)
    elif operation == "logo_merge":
        logo_merge(request)
    else:
        raise RuntimeError("unknown PYA operation: %s" % operation)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("ArtistIC PYA worker: %s" % exc, file=sys.stderr)
        raise
