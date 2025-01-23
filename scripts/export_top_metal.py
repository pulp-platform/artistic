# Copyright 2025 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
#
# Thomas Benz <tbenz@iis.ee.ethz.ch>
# Paul Scheffler <paulsc@iis.ee.ethz.ch>
# Nils Wistoff <nwistoff@iis.ee.ethz.ch>
# Philippe Sauter <phsauter@iis.ee.ethz.ch>

"""Export top-metal layer from a GDS"""

import sys
import pya
import ast

with open('meerkat_design.py') as design:
    cfg = ast.literal_eval(design.read())

ly = pya.Layout()
ly.read(cfg['in_gds'])

for li in ly.layer_indexes():
    lp = ly.get_info(li)
    if lp.layer == int(cfg['layer']) and lp.datatype == int(cfg['datatype']):

        # create save properties
        opt = pya.SaveLayoutOptions()

        # drop empty cells
        opt.no_empty_cells = True
        opt.keep_instances = False

        # only add matching layers
        opt.add_layer(li, lp)

        # write GDS out
        ly.write(cfg['metal_gds'], opt)

        sys.exit(0)
