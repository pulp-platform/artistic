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
    else:
        raise RuntimeError("unknown PYA operation: %s" % operation)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("ArtistIC PYA worker: %s" % exc, file=sys.stderr)
        raise
