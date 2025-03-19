# Copyright 2025 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
#
# Thomas Benz <tbenz@iis.ee.ethz.ch>
# Paul Scheffler <paulsc@iis.ee.ethz.ch>
# Nils Wistoff <nwistoff@iis.ee.ethz.ch>
# Philippe Sauter <phsauter@iis.ee.ethz.ch>

"""Analyzes the JSON configuration"""

import sys
import math
import json

BASE_SVG = '''<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<svg
  width="250mm"
  height="{height}mm"
  viewBox="0 0 250 {height}"
  version="1.1"
  id="svg8">
  <rect width="100%" height="100%" fill="black"/>
  {text}
</svg>
'''

TEXT_SVG = '''  <text
     x="2mm"
     y="{y}mm"
     id="text{id}"
     style="font-size:2mm;stroke-width:0.15;font-family:monospace;fill:{color}"
   >{content}</text>
'''


def analyze(data: dict) -> dict:
    res = {}

    # tile calculation
    if data["tech"]["max_px_tile"] == "-":
        res['tiles_w'] = data["image"]["num_tiles_width"]
        res['tiles_h'] = data["image"]["num_tiles_height"]
    else:
        num_tiles = data['image']['px_width'] * data['image']['px_height'] * \
            data['image']['overrender_factor']**2 / data["tech"]["max_px_tile"]
        tiles = math.ceil(math.sqrt(num_tiles))
        res['tiles_w'] = math.lcm(tiles, data['image']['num_segs_width'])
        res['tiles_h'] = math.lcm(tiles, data['image']['num_segs_height'])

    # image calc
    res['image_w'] = math.ceil(data['image']['px_width'] / res['tiles_w']) * res['tiles_w']
    res['image_h'] = math.ceil(data['image']['px_height'] / res['tiles_h']) * res['tiles_h']

    # scale factor
    res['scale'] = 100.0 / data['image']['overrender_factor']

    # image per segment resolution
    res['seg_w'] = res['image_w'] / data['image']['num_segs_width']
    res['seg_h'] = res['image_h'] / data['image']['num_segs_height']

    # page width
    res['page_px'] = f'{int(res["seg_w"])}x{int(res["seg_h"])}'

    # get requested paper size in cm
    if data["paper"]['width_cm'] == '-':
        res['paper_w'] = float(data['paper']['height_cm']) / res['seg_h'] * res['seg_w']
        res['paper_h'] = float(data['paper']['height_cm'])
    elif data['paper']['height_cm'] == '-':
        res['paper_w'] = float(data['paper']['width_cm'])
        res['paper_h'] = float(data['paper']['width_cm']) / res['seg_w'] * res['seg_h']
    else:
        print('No paper width given', file=sys.stderr)
        sys.exit(-2)

    # TODO: handle case where aspect ratio of image does not match ratio of paper; this is
    # supported by the resize command in the Makefile given the proper DPI number here...
    # dpi
    res['dpi'] = res['seg_w'] / (res['paper_w'] / 2.54)

    # GDS calc
    res['dbu'] = data['tech']['db_unit_nm'] / 1000.0

    # GDS requested size
    gds_x_offset = round(data['gds']['x_offset_um'] / res['dbu'])
    gds_y_offset = round(data['gds']['y_offset_um'] / res['dbu'])
    gds_width = round(data['gds']['width_um'] / res['dbu'])
    gds_height = round(data['gds']['height_um'] / res['dbu'])

    # density
    dbu_p_px_x = gds_width / res['image_w']
    dbu_p_px_y = gds_height / res['image_h']
    res['dbu_p_px'] = max([dbu_p_px_x, dbu_p_px_y])

    # scale gds are to image size
    tot_gds_width = res['image_w'] * res['dbu_p_px']
    tot_gds_height = res['image_h'] * res['dbu_p_px']

    # make divisible by zoom factor
    res['tot_gds_width'] = math.ceil(math.ceil(tot_gds_width / res['tiles_w']) * res['tiles_w'])
    res['tot_gds_height'] = math.ceil(math.ceil(tot_gds_height / res['tiles_h']) * res['tiles_h'])

    # extra padding
    res['extra_pad_x'] = math.ceil((res['tot_gds_width'] - gds_width) / 2.0)
    res['extra_pad_y'] = math.ceil((res['tot_gds_height'] - gds_height) / 2.0)

    # calculate offsets
    res['tot_gds_x_offset'] = gds_x_offset - res['extra_pad_x']
    res['tot_gds_y_offset'] = gds_y_offset - res['extra_pad_y']

    return res


def emit_color_preview(data: dict, colors: dict) -> str:
    svg_body = ''
    y = 4

    # format text elements
    for layer in data['tech']['layer_order']:
        color = data["colors"][layer]["color"]
        layer_num = data["colors"][layer]["layer"]
        # match color names
        if color in colors:
            name = f' ({colors[color]})'
        else:
            name = ''
        color_str = f'{layer}: ({layer_num}) {color}{name} - {data["colors"][layer]["alpha"]}'
        svg_body += TEXT_SVG.format(**{'y': y, 'id': y, 'color': color, 'content': color_str})
        y += 2

    # format svg file
    num_layers = len(data['tech']['layer_order'])
    return BASE_SVG.format(**{'height': 24 + 7.5 * num_layers, 'text': svg_body})


if __name__ == '__main__':

    # parse command line args
    _, chip_json = sys.argv

    # read data
    with open(chip_json, 'r') as f:
        data = json.load(f)

    # read imagemagick color data
    with open('scripts/magick_named_colors.json', 'r') as f:
        colors = json.load(f)

    # write stack preview file
    with open(f'{data["work"]["dir"]}/colors_{data["general"]["chip"]}.svg', 'w') as f:
        f.write(emit_color_preview(data, colors))

    info = analyze(data)

    # extract data from dict
    image_w = info["image_w"]
    image_h = info["image_h"]
    render_w = image_w * data["image"]["overrender_factor"]
    render_h = image_h * data["image"]["overrender_factor"]
    render_r = round(render_w * render_h / 1000000000.0, 3)

    tiles_w = info["tiles_w"]
    tiles_h = info["tiles_h"]
    t_size_w = int(data["image"]["overrender_factor"] * info["image_w"] / info["tiles_w"])
    t_size_h = int(data["image"]["overrender_factor"] * info["image_h"] / info["tiles_h"])

    gds_dbu_w = int(info["tot_gds_width"])
    gds_dbu_h = int(info["tot_gds_height"])
    gds_nm_w = int(info["tot_gds_width"] * info["dbu"])
    gds_nm_h = int(info["tot_gds_height"] * info["dbu"])

    gds_t_size_w = int(info["tot_gds_width"] / info["tiles_w"])
    gds_t_size_h = int(info["tot_gds_height"] / info["tiles_h"])
    gds_offset_w = int(info["tot_gds_x_offset"])
    gds_offset_h = int(info["tot_gds_y_offset"])

    p_res_dbu_w = info["tot_gds_width"] / info["image_w"]
    p_res_dbu_h = info["tot_gds_height"] / info["image_h"]
    p_res_nm_w = info["tot_gds_width"] * info["dbu"] * 1000 / info["image_w"]
    p_res_nm_h = info["tot_gds_height"] * info["dbu"] * 1000 / info["image_h"]

    r_res_dbu_w = p_res_dbu_w / data["image"]["overrender_factor"]
    r_res_dbu_h = p_res_dbu_h / data["image"]["overrender_factor"]
    r_res_nm_w = p_res_nm_w / data["image"]["overrender_factor"]
    r_res_nm_h = p_res_nm_h / data["image"]["overrender_factor"]

    paper_cm_w = info["paper_w"]
    paper_cm_h = info["paper_h"]
    paper_in_w = round(paper_cm_w / 2.54, 2)
    paper_in_h = round(paper_cm_h / 2.54, 2)

    tot_paper_w = paper_cm_w * data["image"]["num_segs_width"]
    tot_paper_h = paper_cm_h * data["image"]["num_segs_height"]

    # print data
    print('Tapete Summary')
    print('------------------')
    print(f'Image resolution:    {image_w} px x {image_h} px')
    print(f'Number of tiles:     {tiles_w} x {tiles_h}')
    print(f'Tile size:           {t_size_w} px x {t_size_h} px')
    print(f'Render resolution:   {render_w} px x {render_h} px')
    print(f'                     {render_r} Gpx')
    print('---')
    print(f'GDS size:            {gds_dbu_w} dbu x {gds_dbu_h} dbu')
    print(f'                     {gds_nm_w} nm x {gds_nm_h} nm')
    print(f'GDS tile size:       {gds_t_size_w} dbu x {gds_t_size_h} dbu')
    print(f'GDS offsets:         x: {gds_offset_w} dbu,  y: {gds_offset_h} dbu')
    print('---')
    print(f'Resolution x:        {p_res_dbu_w} dbu/px {p_res_nm_w} nm/px')
    print(f'Resolution y:        {p_res_dbu_h} dbu/px {p_res_nm_h} nm/px')
    print('---')
    print(f'Render resolution x: {r_res_dbu_w} dbu/px {r_res_nm_w} nm/px')
    print(f'Render resolution y: {r_res_dbu_h} dbu/px {r_res_nm_h} nm/px')
    print('---')
    print(f'Paper size:          {paper_cm_w} cm x {paper_cm_h} cm')
    print(f'                     {paper_in_w} inch x {paper_in_h} inch')
    print(f'Resolution:          {info["dpi"]} dpi')
    print('---')
    print(f'Overall image size:  {tot_paper_w} cm x {tot_paper_h} cm')
    print('------------------')
