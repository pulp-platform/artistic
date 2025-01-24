# Copyright 2025 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
#
# Thomas Benz <tbenz@iis.ee.ethz.ch>
# Paul Scheffler <paulsc@iis.ee.ethz.ch>
# Nils Wistoff <nwistoff@iis.ee.ethz.ch>
# Philippe Sauter <phsauter@iis.ee.ethz.ch>

"""Helpers to read values from JSON"""

import sys
import json

if len(sys.argv) == 3:
    with open(sys.argv[1], 'r') as f:
        print(json.load(f)[sys.argv[2]])

elif len(sys.argv) == 4:
    with open(sys.argv[1], 'r') as f:
        print(json.load(f)[sys.argv[2]][sys.argv[3]])

elif len(sys.argv) == 5:
    with open(sys.argv[1], 'r') as f:
        print(json.load(f)[sys.argv[2]][sys.argv[3]][sys.argv[4]])
