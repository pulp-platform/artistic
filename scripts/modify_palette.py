# Copyright 2025 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
#
# Thomas Benz <tbenz@iis.ee.ethz.ch>
# Paul Scheffler <paulsc@iis.ee.ethz.ch>
# Nils Wistoff <nwistoff@iis.ee.ethz.ch>
# Philippe Sauter <phsauter@iis.ee.ethz.ch>

"""Modifies a color palette in a JSON file"""

import argparse
import sys
import json
import colorsys
import numpy
from PIL import ImageColor
from primePy import primes


def gen_palette(num_layers: int, num_lights: int, min_light: float, max_light: float) -> list:

    repeat = [num_layers % p == 0 for p in primes.factors(num_lights)]

    if (any(repeat) and num_lights != 1):
        print('#Layers should not be divisible by a factor of number of lights!', file=sys.stderr)

    # equally space light shades
    lights = numpy.linspace(min_light, max_light, num=num_lights)

    # generate at constant intensity
    palette = []
    for i in range(0, num_layers):
        ang_tot = i / num_layers * float(num_lights)
        angle = ang_tot % 1.0
        light = int(ang_tot)
        ro, go, bo = colorsys.hls_to_rgb(angle, lights[light], 1.0)
        palette.append(f'#{int(ro*255):02x}{int(go*255):02x}{int(bo*255):02x}')

    # return
    return palette


def gen_alpha(num_alpha: int, min_alpha: int, max_alpha: int) -> list:
    return list(numpy.logspace(min_alpha, max_alpha, num_alpha) / max_alpha / 10.0)


def change_hue(rot_degrees: float, palette: list) -> list:
    res = []
    for col in palette:
        (ri, gi, bi) = ImageColor.getcolor(col, 'RGB')
        h, l, s = colorsys.rgb_to_hls(ri/255.0, gi/255.0, bi/255.0)
        ro, go, bo = colorsys.hls_to_rgb((h + rot_degrees/360.0) % 1.0, l, s)
        res.append(f'#{int(ro*255):02x}{int(go*255):02x}{int(bo*255):02x}')
    return res


def write_palette(data: dict, hue_rot: float, num_lights: int, min_light: float,
                  max_light: float, min_alpha: int, max_alpha: int) -> dict:

    layers = data["tech"]["layer_order"]
    num_cols = len(layers)

    alphas = gen_alpha(num_cols, min_alpha, max_alpha)

    if hue_rot == 0.0:
        colors = gen_palette(num_cols, int(num_lights), min_light, max_light)
    else:
        colors = change_hue(hue_rot, gen_palette(num_cols, int(num_lights), min_light, max_light))

    lay_idx = 0
    for layer in layers:
        data["colors"][layer]["color"] = colors[lay_idx]
        data["colors"][layer]["alpha"] = str(round(alphas[num_cols - 1 - lay_idx], 4))
        lay_idx += 1

    return data


def modify_palette(data: dict, hue_rot: float) -> dict:

    layers = data["tech"]["layer_order"]

    for layer in layers:
        data["colors"][layer]["color"] = change_hue(hue_rot, [data["colors"][layer]["color"]])[0]

    return data


if __name__ == '__main__':

    parser = argparse.ArgumentParser(prog='modify_palette.py',
                                     description='Generates or modifies palettes')
    parser.add_argument('--in_file', metavar='file', required=True)
    parser.add_argument('--change_hue', metavar='degrees', type=float)
    parser.add_argument('--generate', nargs=5,
                        metavar=('#lights', 'min_light', 'max_light', 'min_alpha', 'max_alpha'),
                        type=float)
    parser.add_argument('--overwrite_chip', metavar='new_chip_name'),
    parser.add_argument('--overwrite_work', metavar='new_work_dir')
    args = parser.parse_args()

    # read data
    with open(args.in_file, 'r') as f:
        data = json.load(f)

    # overwrites
    if args.overwrite_chip:
        data['general']['chip'] = args.overwrite_chip

    if args.overwrite_work:
        data['work']['dir'] = args.overwrite_work

    # generate
    if args.generate:
        data = write_palette(data, 0.0, *args.generate)

    # change hue
    if args.change_hue:
        data = modify_palette(data, args.change_hue)

    # write json
    print(json.dumps(data, indent=4))
