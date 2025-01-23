# Copyright 2025 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
#
# Thomas Benz <tbenz@iis.ee.ethz.ch>
# Paul Scheffler <paulsc@iis.ee.ethz.ch>
# Nils Wistoff <nwistoff@iis.ee.ethz.ch>
# Philippe Sauter <phsauter@iis.ee.ethz.ch>

"""Generate the interface file for klayout"""

import argparse


if __name__ == '__main__':
    # argparser
    parser = argparse.ArgumentParser(
                        prog='interface',
                        description='Generate the interface file for klayout')

    parser.add_argument('-i', '--in_gds', required=True,
                        help='Vanilla GDS', type=str)

    parser.add_argument('-m', '--metal_gds', required=True,
                        help='The current top-metal GDS file', type=str)

    parser.add_argument('-g', '--logo_gds', required=True,
                        help='The logo GDS file', type=str)

    parser.add_argument('-o', '--out_gds', required=True,
                        help='The merged output GDS', type=str)

    parser.add_argument('-w', '--work', required=True,
                        help='Work directory', type=str)

    parser.add_argument('-l', '--logo_layer', required=True,
                        help='The logo layer', type=int)

    parser.add_argument('-d', '--logo_datatype', default=0, required=False,
                        help='The logo data type', type=int)

    parser.add_argument('-n', '--new_top', default='chip', required=False,
                        help='The name of the new top-level cell', type=str)

    # get data
    args = parser.parse_args()

    # format the dict with the design data
    design = '{\n'
    design += f"  'in_gds': '{args.in_gds}',\n"
    design += f"  'metal_gds': '{args.metal_gds}',\n"
    design += f"  'logo_gds': '{args.logo_gds}',\n"
    design += f"  'out_gds': '{args.out_gds}',\n"
    design += f"  'layer': '{args.logo_layer}',\n"
    design += f"  'datatype': '{args.logo_datatype}',\n"
    design += f"  'new_top': '{args.new_top}'\n"
    design += '}\n'

    # write data
    with open(f'{args.work}/meerkat_design.py', 'w') as f:
        f.write(design)
