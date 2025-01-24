# Copyright 2025 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
#
# Thomas Benz <tbenz@iis.ee.ethz.ch>
# Paul Scheffler <paulsc@iis.ee.ethz.ch>
# Nils Wistoff <nwistoff@iis.ee.ethz.ch>
# Philippe Sauter <phsauter@iis.ee.ethz.ch>

"""Template to generate layer properties file from JSON"""

import sys
import json

HEADER = '''<?xml version="1.0" encoding="utf-8"?>
<layer-properties>'''

FOOTER = '''</layer-properties>'''

LAYER_TPL = ''' <properties>
  <expanded>false</expanded>
  <frame-color>#000000</frame-color>
  <fill-color>#000000</fill-color>
  <frame-brightness>0</frame-brightness>
  <fill-brightness>0</fill-brightness>
  <dither-pattern>I0</dither-pattern>
  <line-style/>
  <valid>true</valid>
  <visible>true</visible>
  <transparent>false</transparent>
  <width/>
  <marked>false</marked>
  <xfill>false</xfill>
  <animation>0</animation>
  <name>{:1}</name>
  <source>{:2}@1</source>
 </properties>'''

_, chip_json = sys.argv

with open(chip_json, 'r') as f:
    data = json.load(f)

print(HEADER)

for layer in data["colors"]:
    print(LAYER_TPL.format(layer, data["colors"][layer]["layer"]))

print(FOOTER)
