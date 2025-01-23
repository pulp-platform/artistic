# Copyright 2025 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
#
# Thomas Benz <tbenz@iis.ee.ethz.ch>
# Paul Scheffler <paulsc@iis.ee.ethz.ch>
# Nils Wistoff <nwistoff@iis.ee.ethz.ch>
# Philippe Sauter <phsauter@iis.ee.ethz.ch>

"""Helper functions handling files"""

import sys
import json
from analyze import analyze as analyze


def gen_raw_list(data: dict) -> list:
    res = []

    # get information required
    info = analyze(data)
    work = data['work']['dir']
    chip = data['general']['chip']

    # generate list
    for w in range(info['tiles_w']):
        for h in range(info['tiles_h']):
            for c in data['tech']['layer_order']:
                layer_num, layer_id = data["colors"][c]['layer'].split('/')
                res.append(f'{work}/RAW__{chip}_{layer_num}.{layer_id}.{c}_{w}-{h}.png')

    return res


def list_color_files(data: dict, target_tile_file: str) -> list:
    res = []

    work = data['work']['dir']
    chip = data['general']['chip']

    # for given tile, name all layer files
    coord = target_tile_file.split('/')[-1].split('_')[-1].split('.')[0]
    for c in data['tech']['layer_order']:
        layer_num, layer_id = data["colors"][c]['layer'].split('/')
        res.append(f'{work}/COL__{chip}_{layer_num}.{layer_id}.{c}_{coord}.png')

    return res


def emit_compose_command(data: dict, target_tile_file: str) -> list:
    res = []

    work = data['work']['dir']
    chip = data['general']['chip']

    coord = base_file.split('/')[-1].split('_')[-1].split('.')[0]
    first = True
    for c in data['tech']['layer_order']:
        line = ''
        layer_num, layer_id = data["colors"][c]['layer'].split('/')
        line += f'{work}/COL__{chip}_{layer_num}.{layer_id}.{c}_{coord}.png'
        if first:
            first = False
        else:
            line += ' -composite'
        res.append(line)

    return res


def gen_tile_list(data: dict) -> list:
    res = []

    # get information required
    info = analyze(data)
    work = data['work']['dir']
    chip = data['general']['chip']

    # generate list
    for w in range(info['tiles_w']):
        for h in range(info['tiles_h']):
            res.append(f'{work}/MRG__{chip}_{w}-{h}.png')

    return res


def gen_seg_list(data: dict) -> list:
    res = []

    # get information required
    work = data['work']['dir']
    chip = data['general']['chip']

    # generate list
    for w in range(data['image']['num_segs_width']):
        for h in range(data['image']['num_segs_height']):
            res.append(f'{work}/SEG__{chip}_{h}-{w}.png')

    return res


def gen_seg_src_list(data: dict, target_tile_file: str) -> list:
    res = []

    # get information required
    info = analyze(data)
    work = data['work']['dir']
    chip = data['general']['chip']

    # current segment
    h_coord, w_coord = target_tile_file.split('/')[-1].split('_')[-1].split('.')[0].split('-')

    num_tiles_per_seg_w = info['tiles_w'] // data['image']['num_segs_width']
    num_tiles_per_seg_h = info['tiles_h'] // data['image']['num_segs_height']

    for w in range(num_tiles_per_seg_w):
        for h in range(num_tiles_per_seg_h):
            w_ = w + int(w_coord) * num_tiles_per_seg_w
            h_ = h + int(h_coord) * num_tiles_per_seg_h
            res.append(f'{work}/RSZ__{chip}_{h_}-{w_}.png')

    return res


def gen_seg_command(data: dict, target_tile_file: str) -> list:
    res = ''

    # get information required
    info = analyze(data)
    work = data['work']['dir']
    chip = data['general']['chip']

    # current segment
    h_coord, w_coord = target_tile_file.split('/')[-1].split('_')[-1].split('.')[0].split('-')

    num_tiles_per_seg_w = info['tiles_w'] // data['image']['num_segs_width']
    num_tiles_per_seg_h = info['tiles_h'] // data['image']['num_segs_height']

    clm_cmd = 'convert '
    for w in range(num_tiles_per_seg_w):
        w_ = w + int(w_coord) * num_tiles_per_seg_w

        # generate columns
        if num_tiles_per_seg_h == 1:
            res += f'cp {work}/RSZ__{chip}_{h_coord}-{w_}.png '
            res += f'{work}/CLM__{chip}_{h_coord}-{w_coord}-{w}.png; '
        else:
            res += 'convert '
            for h in range(num_tiles_per_seg_h - 1, -1, -1):
                h_ = h + int(h_coord) * num_tiles_per_seg_h
                res += f'{work}/RSZ__{chip}_{h_}-{w_}.png '
            res += '-append '
            res += f'{work}/CLM__{chip}_{h_coord}-{w_coord}-{w}.png; '

        # handle columns
        if not num_tiles_per_seg_w == 1:
            clm_cmd += f'{work}/CLM__{chip}_{h_coord}-{w_coord}-{w}.png '

    if num_tiles_per_seg_w == 1:
        res += f'cp {work}/CLM__{chip}_{h_coord}-{w_coord}-0.png '
        res += f'{work}/SEG__{chip}_{h_coord}-{w_coord}.png; '
    else:
        res += clm_cmd + '+append ' + f'{work}/SEG__{chip}_{h_coord}-{w_coord}.png; '

    return res


if __name__ == '__main__':

    # parse command line args
    _, chip_json, option, base_file = sys.argv

    # read data
    with open(chip_json, 'r') as f:
        data = json.load(f)

    # emit lists
    if option == 'RAW':
        print(' '.join(gen_raw_list(data)))

    elif option == 'COL':
        print(' '.join(list_color_files(data, base_file)))

    elif option == 'CMP':
        print(' '.join(emit_compose_command(data, base_file)))

    elif option == 'MRG':
        print(' '.join(gen_tile_list(data)))

    elif option == 'RSZ':
        print(' '.join(gen_tile_list(data)).replace('MRG__', 'RSZ__'))

    elif option == 'SCALE':
        print(analyze(data)['scale'])

    elif option == 'SEG':
        print(' '.join(gen_seg_list(data)))

    elif option == 'SEGSRC':
        print(' '.join(gen_seg_src_list(data, base_file)))

    elif option == 'SEGGEN':
        print(gen_seg_command(data, base_file))

    elif option == 'DPI':
        print(' '.join(gen_seg_list(data)).replace('SEG__', 'DPI__'))

    elif option == 'DPI_SCALE':
        print(analyze(data)['dpi'])

    elif option == 'PAGE_PX':
        print(analyze(data)['page_px'])

    elif option == 'PDF':
        print(' '.join(gen_seg_list(data)).replace('SEG__', 'PDF__').replace('.png', '.pdf'))

    else:
        analyze(data)
        sys.exit(-1)
