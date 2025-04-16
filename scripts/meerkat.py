# Copyright 2025 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
#
# Thomas Benz <tbenz@iis.ee.ethz.ch>
# Paul Scheffler <paulsc@iis.ee.ethz.ch>
# Nils Wistoff <nwistoff@iis.ee.ethz.ch>
# Philippe Sauter <phsauter@iis.ee.ethz.ch>

"""Translate an image of the proper dimensions to a GDS."""

import sys
import argparse
import gdspy
import numpy as np

from PIL import Image

# constant for the database unit
# DB units
DB2NM = 2000.0
PIXSZ = 2000.0

# Use dithering primitives
KERNEL_DIM = 4

FULL = [[ 1,  1,  1,  1],
        [ 1,  1,  1,  1],
        [ 1,  1,  1,  1],
        [ 1,  1,  1,  1]]

EMPTY = [[-1, -1, -1, -1],
         [-1, -1, -1, -1],
         [-1, -1, -1, -1],
         [-1, -1, -1, -1]]

UPLF = [[ 1,  1, -1, -1],
        [ 1,  1, -1, -1],
        [-1, -1, -1, -1],
        [-1, -1, -1, -1]]

UPRG = [[-1, -1,  1,  1],
        [-1, -1,  1,  1],
        [-1, -1, -1, -1],
        [-1, -1, -1, -1]]

LWLF = [[-1, -1, -1, -1],
        [-1, -1, -1, -1],
        [ 1,  1, -1, -1],
        [ 1,  1, -1, -1]]

LWRG = [[-1, -1, -1, -1],
        [-1, -1, -1, -1],
        [-1, -1,  1,  1],
        [-1, -1,  1,  1]]

VERLF = [[ 1,  1, -1, -1],
         [ 1,  1, -1, -1],
         [ 1,  1, -1, -1],
         [ 1,  1, -1, -1]]

VERRG = [[-1, -1,  1,  1],
         [-1, -1,  1,  1],
         [-1, -1,  1,  1],
         [-1, -1,  1,  1]]

HORUP = [[ 1,  1,  1,  1],
         [ 1,  1,  1,  1],
         [-1, -1, -1, -1],
         [-1, -1, -1, -1]]

HORDW = [[-1, -1, -1, -1],
         [-1, -1, -1, -1],
         [ 1,  1,  1,  1],
         [ 1,  1,  1,  1]]

# All of the dithering primitives used
KERNELS = [FULL, EMPTY, UPLF, UPRG, LWLF, LWRG, VERLF, VERRG, HORUP, HORDW]


# Create primitive GDS cells which corresponds to the dithering pattern
cell_full = gdspy.Cell(name='full').add(
                gdspy.Rectangle((0, 0), (2*PIXSZ, 2*PIXSZ), layer=0))
cell_quater = gdspy.Cell(name='quater').add(
                gdspy.Rectangle((0, 0), (1*PIXSZ, 1*PIXSZ), layer=0))
cell_vert = gdspy.Cell(name='vert').add(
                gdspy.Rectangle((0, 0), (1*PIXSZ, 2*PIXSZ), layer=0))
cell_horiz = gdspy.Cell(name='horiz').add(
                gdspy.Rectangle((0, 0), (2*PIXSZ, 1*PIXSZ), layer=0))


# Define primitive functions that reference dither primitives
def full_box(cell: gdspy.Cell, row: int, col: int):
    """Full box"""
    cell.add(gdspy.CellReference(cell_full, origin=(col*PIXSZ, -row*PIXSZ)))


def empty_box(cell: gdspy.Cell, row: int, col: int):
    """Empty box"""


def uplf_box(cell: gdspy.Cell, row: int, col: int):
    """1/4 block, upper left"""
    cell.add(gdspy.CellReference(cell_quater, origin=(col*PIXSZ, -(row-1)*PIXSZ)))


def uprg_box(cell: gdspy.Cell, row: int, col: int):
    """1/4 block, upper right"""
    cell.add(gdspy.CellReference(cell_quater, origin=((col+1)*PIXSZ, -(row-1)*PIXSZ)))


def lwlf_box(cell: gdspy.Cell, row: int, col: int):
    """1/4 block, lower left"""
    cell.add(gdspy.CellReference(cell_quater, origin=(col*PIXSZ, -row*PIXSZ)))


def lwrg_box(cell: gdspy.Cell, row: int, col: int):
    """1/4 block, lower right"""
    cell.add(gdspy.CellReference(cell_quater, origin=((col+1)*PIXSZ, -row*PIXSZ)))


def verlf_box(cell: gdspy.Cell, row: int, col: int):
    """1/2 block, vertical left"""
    cell.add(gdspy.CellReference(cell_vert, origin=(col*PIXSZ, -row*PIXSZ)))


def verrg_box(cell: gdspy.Cell, row: int, col: int):
    """1/2 block, vertical right"""
    cell.add(gdspy.CellReference(cell_vert, origin=((col+1)*PIXSZ, -row*PIXSZ)))


def horup_box(cell: gdspy.Cell, row: int, col: int):
    """1/2 block, horizontal up"""
    cell.add(gdspy.CellReference(cell_horiz, origin=(col*PIXSZ, -(row-1)*PIXSZ)))


def hordw_box(cell: gdspy.Cell, row: int, col: int):
    """1/2 block, horizontal up"""
    cell.add(gdspy.CellReference(cell_horiz, origin=(col*PIXSZ, -row*PIXSZ)))


# list of the primitive functions
FUNCS = [full_box, empty_box, uplf_box, uprg_box, lwlf_box,
         lwrg_box, verlf_box, verrg_box, horup_box, hordw_box]


def create_logo(margins: list, img_file: str, contrast: float, metal_gds_file: str,
                logo_layer: int, logo_datatype: int, logo_name: str, out_svg_file: str,
                out_gds_file: str):
    """Translate an image of the proper dimensions to a GDS."""

    # fetch and pre-processes image file
    logo_image = 255 - np.array(Image.open(img_file).convert('1')) * 255 - int(255 * contrast)
    [img_height, img_width] = logo_image.shape

    # create a new lib holding the logo
    logo_lib = gdspy.GdsLibrary(name=logo_name, physical_unit=1e-6, unit=1e-9, precision=1e-9)

    # read-in the metal GDS to mask the logo eventually, convert to get consistent DB units
    chip_lib = gdspy.GdsLibrary(name='chip', infile=metal_gds_file, unit=1e-9,
                                precision=1e-9, units='convert')

    # determine the canvas height available to the logo
    bbox_chip = chip_lib.top_level()[0].get_bounding_box()
    bbox = [bbox_chip[0][0] + margins[0], bbox_chip[0][1] + margins[1],
            bbox_chip[1][0] - margins[2], bbox_chip[1][1] - margins[3]]

    canvas_height, canvas_width = int((bbox[2] - bbox[0])//PIXSZ), int((bbox[3] - bbox[1])//PIXSZ)

    # fail here if the logo is not sized properly
    if img_width != canvas_width or img_height != canvas_height:
        print(f'Logo does not fit: {img_width}x{img_height} in {canvas_width}x{canvas_height}')
        pad_b = int(margins[0] / DB2NM) + int((canvas_height - img_height) / 2)
        pad_l = int(margins[1] / DB2NM) + int((canvas_width - img_width) / 2)
        pad_t = int(margins[2] / DB2NM) + int((canvas_height - img_height) / 2)
        pad_r = int(margins[3] / DB2NM) + int((canvas_width - img_width) / 2)
        if (canvas_height - img_height) > 0:
            pad_b = pad_b + 1
        if (canvas_width - img_width) > 0:
            pad_l = pad_b + 1
        print(f'To center the logo: use -m {pad_b},{pad_l},{pad_t},{pad_r}')
        sys.exit(1)

    # create the mask for the existing metalization, use one pixel size spacing
    mask = gdspy.Cell(name='mask')
    for top_cell in chip_lib.top_level():
        mask.add(gdspy.CellReference(top_cell, origin=(0, 0)))
        mask.add(gdspy.CellReference(top_cell, origin=(-PIXSZ, 0)))
        mask.add(gdspy.CellReference(top_cell, origin=(PIXSZ, 0)))
        mask.add(gdspy.CellReference(top_cell, origin=(0, -PIXSZ)))
        mask.add(gdspy.CellReference(top_cell, origin=(0, PIXSZ)))

    # create a new cell for the pre-logo
    pre_logo = gdspy.Cell(name='pre_logo')

    # use 2D-convolution to find the most suitable dithering primitive
    for col in range(0, img_width - KERNEL_DIM, KERNEL_DIM - 1):
        for row in range(0, img_height - KERNEL_DIM, KERNEL_DIM - 1):
            sub_img = logo_image[row:row+KERNEL_DIM, col:col+KERNEL_DIM]
            kernel_val = []
            for kernel in KERNELS:
                kernel_val.append(np.sum(sub_img * kernel))
            sel = kernel_val.index(max(kernel_val))
            FUNCS[sel](pre_logo, row, col)
        print(f'{round(col/img_width*100, 2)} %')

    # shift logo and do the boolean subtraction of the mask
    shifted = pre_logo.copy(name='shifted', deep_copy=False,
                            translation=(margins[1], margins[0] + img_height*PIXSZ))

    logo = gdspy.Cell(name=logo_name)
    # Add offset for not violate DRC TM2.b
    offset = gdspy.offset(shifted, 2.0, join_first=True)
    logo.add(gdspy.boolean(offset, mask, operation='not',
                           layer=logo_layer, datatype=logo_datatype))

    # cleanup
    clean_logo = gdspy.Cell(name=f'{logo_name}_logo')
    for poly in logo.get_polygons():
        if len(poly) != 4:
            print(f'Reject {poly} as it has {len(poly)} points')
        else:
            p_height = poly[0][0] - poly[1][0]
            p_width = poly[0][1] - poly[3][1]
            if (p_height < DB2NM) or (p_width < DB2NM):
                continue
            else:
                current_shape = gdspy.Polygon(poly, layer=logo_layer, datatype=logo_datatype)
                clean_logo.add(current_shape)

    logo_lib.add(clean_logo)

    # calculate metal layer density
    polygons = clean_logo.get_polygons()
    total_metal_area = sum(gdspy.Polygon(polygon).area() for polygon in polygons)
    density = total_metal_area / (bbox[2] - bbox[0]) / (bbox[3] - bbox[1]) * 100.0
    print(f'Logo density: {density}')

    # write to file
    logo_lib.write_gds(out_gds_file)

    # if SVG export is requested:
    if out_svg_file is not None:
        # scale and write to file
        svg_export = gdspy.Cell(name='svg_export')
        svg_export.add(gdspy.CellReference(logo, magnification=1./DB2NM))
        svg_export.write_svg(out_svg_file, scaling=1, background='#ffffff')


if __name__ == '__main__':
    # argparser
    parser = argparse.ArgumentParser(
                        prog='Meerkat',
                        description='Translate an image of the proper dimensions to a GDS')

    parser.add_argument('-m', '--margins', required=True,
                        help='Margins in nm: bottom,left,top,right', type=str)

    parser.add_argument('-i', '--image_file', required=True,
                        help='The image file to use', type=str)

    parser.add_argument('-c', '--contrast', default=0.5, required=False,
                        help='Contrast in range [0, 1]', type=float)

    parser.add_argument('-g', '--metal_gds', required=True,
                        help='The current top-metal GDS file', type=str)

    parser.add_argument('-l', '--logo_layer', required=True,
                        help='The logo layer', type=int)

    parser.add_argument('-d', '--logo_datatype', default=0, required=False,
                        help='The logo data type', type=int)

    parser.add_argument('-n', '--logo_name', default='logo', required=False,
                        help='The name of the logo cell', type=str)

    parser.add_argument('-s', '--logo_svg', default=None, required=False,
                        help='If present, the name of the svg file to write', type=str)

    parser.add_argument('-o', '--logo_gds', required=True,
                        help='The name of the logo GDS', type=str)

    # get the args and process them
    args = parser.parse_args()
    margin_list = [DB2NM * float(item) for item in args.margins.split(',')]

    # create the logo
    create_logo(margin_list, args.image_file, args.contrast, args.metal_gds, args.logo_layer,
                args.logo_datatype, args.logo_name, args.logo_svg, args.logo_gds)
