# Copyright 2025 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
#
# Thomas Benz <tbenz@iis.ee.ethz.ch>

"""Annotate the module outlines of a render"""

import argparse
import colorsys
import glob
import json
import os
import re
import shutil
import sys
import tempfile
from PIL import Image
from PIL import ImageColor
from svgpathtools import parse_path


def parse_args() -> argparse.Namespace:
    """Parse and return command-line arguments for the DEF-to-SVG pipeline."""
    parser = argparse.ArgumentParser(
        description="Convert DEF placement data into grouped SVG paths and merge with a background."
    )

    # Required arguments
    parser.add_argument("-i", "--def_file", required=True, type=os.path.abspath,
                        help="Input DEF file")
    parser.add_argument("-o", "--output", required=True, type=os.path.abspath,
                        help="Output merged SVG file")
    parser.add_argument("-b", "--background", required=True, type=os.path.abspath,
                        help="Rendered background bitmap image")

    # LEF information
    parser.add_argument("--lef_files", nargs="*", default=[],
                        help="Macro LEF files")

    # Background transform
    parser.add_argument("--offset_x", type=int, default=0,
                        help="Background X origin offset")
    parser.add_argument("--offset_y", type=int, default=0,
                        help="Background Y origin offset")
    parser.add_argument("--scale", type=float, default=1.0,
                        help="Background scale factor")
    parser.add_argument("--opacity", type=float, default=1.0,
                        help="Background opacity")
    parser.add_argument("--font_size", type=float, default=0,
                        help="Label font size, 0 for no labels")
    parser.add_argument("--luminosity", type=float, default=1.0,
                        help="Luminosity of the labels")
    parser.add_argument("--color", type=str, default='#000000',
                        help="SVG background color")

    # Hierarchy depth
    parser.add_argument("--top_instance", default=None,
                        help="Top-level cell instance name for hierarchy grouping")
    parser.add_argument("--min", type=int, default=0,
                        help="Minimum hierarchy depth to include")
    parser.add_argument("--max", type=int, default=0,
                        help="Maximum hierarchy depth to include")

    # Module JSON
    parser.add_argument("--module_json", default=None,
                        help="JSON file containing instance paths of interest")

    # Rendering parameters
    parser.add_argument("--px_scale", type=int, default=10000,
                        help="Pixel size in nanometers")
    parser.add_argument("--turd_size", type=int, default=50,
                        help="Potrace turd size parameter in pt")
    parser.add_argument("--stroke_width", default=5,
                        help="Stroke width for output SVG paths in pt")

    args = parser.parse_args()

    # json file overwrites top_instance, min, and max
    if args.module_json and args.top_instance:
        print('Ignore --top_instance, --min, and --max in presence of --module_json')

    if not args.module_json and not args.top_instance:
        print('Either specify --top_instance or --module_json')
        sys.exit(-1)

    return args


def parse_lef_files(lef_file_paths: list) -> dict:
    """
    Parse a LEF file, extract cell names and respective sizes
    """

    LEF_SIZE_REGEX = r'MACRO *([0-9A-Za-z_]+)\n.*?([0-9.]+) *BY *([0-9.]+) *;'

    lef_cells = {}
    lef_files = []

    for lfp in lef_file_paths:
        lef_files.extend(glob.glob(lfp))

    for lef_file in lef_files:
        with open(lef_file) as f:
            cells = re.findall(LEF_SIZE_REGEX, f.read(), re.DOTALL)
            for cell in cells:
                lef_cells[cell[0]] = (float(cell[1]), float(cell[2]))

    return lef_cells


def parse_def_file_hier(def_file_path: str, scale: int, min_hier: int, max_hier: int,
                        top_cell: str, lef_files: list) -> list:
    """
    Parse a DEF file, extract cell placements, and group them by truncated hierarchy path.
    """

    lef_cells = parse_lef_files(lef_files)

    groups = {}
    pre_cells = True
    bbox = None
    x_size = y_size = 0

    SIZE_REGEX = r'\( *([0-9]+) *([0-9]+) *\) *\( *([0-9]+) *([0-9]+) *\)'

    with open(def_file_path, 'r') as def_file:
        for line in def_file:
            if pre_cells:
                if line.startswith('DIEAREA'):
                    bbox = [int(s) for s in re.findall(SIZE_REGEX, line)[0]]
                    x_size = (bbox[2] - bbox[0]) // scale
                    y_size = (bbox[3] - bbox[1]) // scale
                elif line.startswith('COMPONENTS'):
                    num_cells = int(re.findall(r'[0-9]+', line)[0])
                    pre_cells = False
            else:
                # extract cell info
                cell = line.split(' ')
                path = cell[5]
                master_cell_name = cell[6]
                orientation = cell[-2][-1]
                x_coord, y_coord = int(cell[-5]) // scale, y_size - (int(cell[-4]) // scale)

                # get sub path
                sub_path = re.split(r'\.|/', path)
                # iterate over hierarchies
                for pl in range(min_hier, max_hier + 1):
                    sub_path_trunc = sub_path[:-1][:pl]
                    if len(sub_path_trunc) > pl - 1:
                        group = '.'.join(sub_path_trunc).replace('\\', '')
                        if group.startswith(top_cell):

                            # if group is new
                            if group not in groups:
                                groups[group] = []

                            # check whether this is a special cell (with size)
                            if master_cell_name in lef_cells:
                                if orientation == 'N':
                                    pxs_x = int(lef_cells[master_cell_name][0] * 1000 / scale)
                                    pxs_y = int(lef_cells[master_cell_name][1] * 1000 / scale)
                                else:
                                    print(f'Orientation {orientation} not supported')
                                for x in range(0, pxs_x + 1):
                                    for y in range(0, pxs_y + 1):
                                        groups[group].append((x_coord + x, y_coord - y))

                            # normal standard cell (assumed a point)
                            else:
                                groups[group].append((x_coord, y_coord))

                num_cells -= 1
                if num_cells == 0:
                    break

    return groups, x_size, y_size, bbox


def parse_def_file_json(def_file_path: str, scale: int, modules: dict, lef_files: list) -> list:
    """
    Parse a DEF file, extract cell placements, and group them by truncated hierarchy path.
    """

    lef_cells = parse_lef_files(lef_files)

    groups = {}
    pre_cells = True
    bbox = None
    x_size = y_size = 0

    SIZE_REGEX = r'\( *([0-9]+) *([0-9]+) *\) *\( *([0-9]+) *([0-9]+) *\)'

    with open(def_file_path, 'r') as def_file:
        for line in def_file:
            if pre_cells:
                if line.startswith('DIEAREA'):
                    bbox = [int(s) for s in re.findall(SIZE_REGEX, line)[0]]
                    x_size = (bbox[2] - bbox[0]) // scale
                    y_size = (bbox[3] - bbox[1]) // scale
                elif line.startswith('COMPONENTS'):
                    num_cells = int(re.findall(r'[0-9]+', line)[0])
                    pre_cells = False
            else:
                # extract cell info
                cell = line.split(' ')
                path = cell[5]
                master_cell_name = cell[6]
                orientation = cell[-2][-1]
                x_coord, y_coord = int(cell[-5]) // scale, y_size - (int(cell[-4]) // scale)

                # get sub path
                sub_path = re.split(r'\.|/', path)
                for tgt_module in modules:
                    if tgt_module in '.'.join(sub_path):
                        group = modules[tgt_module]['name']
                        # if group is new
                        if group not in groups:
                            groups[group] = []

                        # check whether this is a special cell (with size)
                        if master_cell_name in lef_cells:
                            if orientation == 'N':
                                pxs_x = int(lef_cells[master_cell_name][0] * 1000 / scale)
                                pxs_y = int(lef_cells[master_cell_name][1] * 1000 / scale)
                            else:
                                print(f'Orientation {orientation} not supported')
                            for x in range(0, pxs_x + 1):
                                for y in range(0, pxs_y + 1):
                                    groups[group].append((x_coord + x, y_coord - y))

                        # normal standard cell (assumed a point)
                        else:
                            groups[group].append((x_coord, y_coord))

                num_cells -= 1
                if num_cells == 0:
                    break

    return groups, x_size, y_size, bbox


def generate_svgs(groups: dict, x_size: int, y_size: int, bbox: list, tmp_dir: str,
                  turd_size: int) -> list:
    """
    Rasterize grouped cell placements to bitmaps and convert them to SVG using potrace.
    """

    # point to pixel
    pt_x = int((bbox[2] - bbox[0]) * 0.0008)
    pt_y = int((bbox[3] - bbox[1]) * 0.0008)

    path_svgs = []

    for group, pixels in groups.items():
        img = Image.new('1', (x_size, y_size), 1)
        for px in pixels:
            img.putpixel(px, 0)

        bmp_path = f"{tmp_dir}/{group}.bmp"
        svg_path = f"{tmp_dir}/{group}.svg"

        img.save(bmp_path)
        os.system(
            f'potrace {bmp_path} -s -W {pt_x}pt -H {pt_y}pt -t {turd_size} -o {svg_path}'
        )

        path_svgs.append(svg_path)

    return path_svgs, pt_x, pt_y


def approximate_centroid(path, samples: int = 10000) -> list:
    """
    Approximate centroid of a closed path
    """

    pts = [path.point(i / samples) for i in range(samples)]

    xs = [p.real for p in pts]
    ys = [p.imag for p in pts]

    return sum(xs) / len(xs), sum(ys) / len(ys)


def merge_svgs(path_svgs: list, merged_svg: str, background: str, bg_orig_x: int, bg_orig_y: int,
               bg_scale: float, bg_opacity: float, bg_color: str, pt_x: int, pt_y: int,
               stroke_width: str, font_size: int, tgt_label_luminosity: int, modules: dict):
    """
    Merge generated SVG paths into a single SVG and overlay them on a background image.
    """

    render_bg_img = Image.open(background)
    bg_x, bg_y = render_bg_img.size

    bg_x_pt = bg_x * 0.8 * bg_scale
    bg_y_pt = bg_y * 0.8 * bg_scale
    bg_orig_x_pt = bg_orig_x * 0.8 * bg_scale
    bg_orig_y_pt = bg_orig_y * 0.8 * bg_scale

    PATH_REGEX = r'translate\(([0-9.,-]+)\) *scale\(([0-9.,-]+)\).*d="(.*?)"/>'

    svg_paths = ''
    svg_labels = ''

    svg_out = f'''<?xml version="1.0" standalone="no"?>
<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 20010904//EN"
 "http://www.w3.org/TR/2001/REC-SVG-20010904/DTD/svg10.dtd">
<svg version="1.0" xmlns="http://www.w3.org/2000/svg"
 width="{pt_x}pt" height="{pt_y}pt" viewBox="0 0 {pt_x} {pt_y}"
 preserveAspectRatio="xMidYMid meet">
<metadata>
Merged SVG output
</metadata>
<rect x="0" y="0" width="{bg_x_pt}" height="{bg_y_pt}" fill="{bg_color}"/>
<image
     width="{bg_x_pt}"
     height="{bg_y_pt}"
     preserveAspectRatio="none"
     xlink:href="{os.path.basename(background)}"
     id="render_bg"
     style="opacity:{bg_opacity}"
     x="-{bg_orig_x_pt}"
     y="-{bg_orig_y_pt}" />
'''

    # read path SVG
    for curr_svg in path_svgs:
        obj_id = os.path.basename(curr_svg)[:-4]

        # default color
        color = '#aaaaaa'

        # overwrite colors in there is a module json
        if modules:
            for m in modules:
                if modules[m]['name'] == obj_id:
                    color = modules[m]['color']

        # calculate label color, change the luminosity
        (ri, gi, bi) = ImageColor.getcolor(color, 'RGB')
        h, l, s = colorsys.rgb_to_hls(ri/255.0, gi/255.0, bi/255.0)
        ro, go, bo = colorsys.hls_to_rgb(h, tgt_label_luminosity, s)
        label_color = f'#{int(ro*255):02x}{int(go*255):02x}{int(bo*255):02x}'

        # extract paths
        with open(curr_svg, 'r') as svg_file:
            paths = re.findall(r'<g(.*?)</g>', svg_file.read(), re.DOTALL)

        # iterate now over all paths
        for p in paths:

            # modify paths to outlines, set thickness and color
            p = p.replace('fill="#000000"', f'fill="none" id="{obj_id}"')
            p = p.replace(
                'stroke="none"',
                f'stroke="{color}" stroke-width="{stroke_width}pt"'
            )
            svg_paths += '<g' + p + '</g>'

            # calculate label center, first extract path only
            path_shape = re.findall(PATH_REGEX, p, re.DOTALL)
            if len(path_shape) > 0 and font_size > 0:

                # find center
                text_x, text_y = approximate_centroid(parse_path(path_shape[0][2]))

                # adapt text element
                svg_labels += f'''
<g transform="translate({path_shape[0][0]}) scale({path_shape[0][1]})">
<text
    id="{obj_id}_id"
    x="{text_x}px"
    y="{-text_y + font_size * 0.8 / 2}px"
    transform="scale(1,-1)"
    text-anchor="middle"
    style="font-size:{font_size}pt;font-family:sans-serif;fill:{label_color};stroke-width:0.75">{obj_id}
</text>
</g>
'''

    # merge SVG objects, ensure labels are drawn on top
    svg_out += svg_paths + svg_labels + '</svg>\n'

    # check out dir exists
    dest_dir = os.path.dirname(merged_svg)
    os.makedirs(dest_dir, exist_ok=True)

    # copy background
    shutil.copy(background, dest_dir)

    # write merged svgs
    with open(merged_svg, 'w') as out_file:
        out_file.write(svg_out)


def main() -> None:
    """
    Entry point: parse arguments, generate SVGs, and merge them into final output.
    """
    args = parse_args()

    if args.module_json:
        with open(args.module_json, 'r') as jf:
            module_data = json.load(jf)
        groups, x_size, y_size, bbox = parse_def_file_json(args.def_file, args.px_scale,
                                                           module_data, args.lef_files)
    else:
        module_data = None
        groups, x_size, y_size, bbox = parse_def_file_hier(args.def_file, args.px_scale, args.min,
                                                           args.max, args.top_instance,
                                                           args.lef_files)

    # create a temp dir used to store the intermittent files
    with tempfile.TemporaryDirectory() as tmp_dir:

        path_svgs, pt_x, pt_y = generate_svgs(groups, x_size, y_size, bbox, tmp_dir,
                                              args.turd_size)

        merge_svgs(path_svgs, args.output, args.background, args.offset_x, args.offset_y,
                   args.scale, args.opacity, args.color, pt_x, pt_y, args.stroke_width,
                   args.font_size, args.luminosity, module_data)

        # keep temp dir open for debug purposes
        # input(f'{tmp_dir}')


if __name__ == "__main__":
    main()
