# Copyright 2025 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
#
# Thomas Benz <tbenz@iis.ee.ethz.ch>
# Paul Scheffler <paulsc@iis.ee.ethz.ch>
# Nils Wistoff <nwistoff@iis.ee.ethz.ch>
# Philippe Sauter <phsauter@iis.ee.ethz.ch>

"""Merges vanilla and a logo GDS"""

import sys
import pya
import ast

with open('meerkat_design.py') as design:
    cfg = ast.literal_eval(design.read())

chip = pya.Layout()
chip.read(cfg['in_gds'])
chip.read(cfg['logo_gds'])

current_tops = chip.top_cells()

top_lvl_cell = chip.create_cell(cfg['new_top'])

for ct in current_tops:
    top_lvl_cell.insert(pya.CellInstArray(ct.cell_index(), pya.Trans()))

chip.write(cfg['out_gds'])

sys.exit(0)
