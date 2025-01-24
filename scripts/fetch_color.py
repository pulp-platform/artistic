# Copyright 2025 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
#
# Thomas Benz <tbenz@iis.ee.ethz.ch>
# Paul Scheffler <paulsc@iis.ee.ethz.ch>
# Nils Wistoff <nwistoff@iis.ee.ethz.ch>
# Philippe Sauter <phsauter@iis.ee.ethz.ch>

"""Helpers to read colors from JSON"""

import sys
import json

_, chip_json, full_path, key = sys.argv

layer = full_path.split('/')[-1].split('_')[-2].split('.')[-1]

with open(chip_json, 'r') as f:
    color = json.load(f)["colors"][layer][key]
    if color.startswith('#') or color.startswith('rgb('):
        print(f'\'{color}\'')
    else:
        print(color)
