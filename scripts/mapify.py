# Copyright 2025 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
#
# Thomas Benz <tbenz@iis.ee.ethz.ch>
# Paul Scheffler <paulsc@iis.ee.ethz.ch>
# Nils Wistoff <nwistoff@iis.ee.ethz.ch>
# Philippe Sauter <phsauter@iis.ee.ethz.ch>

"""Creates a command to generate an OpenStreetMap database"""

import sys
import math
import json

# parse command line args
_, chip_json = sys.argv

# read data
with open(chip_json, 'r') as f:
    data = json.load(f)

# assign values
root = data['work']['dir']
raw_stems = data['map']['layers']
chip_name = data['general']['chip']
tmp_dir = data['map']['tmp']

# format stems
stems = []
sub_map_names = {}
for stem in raw_stems:
    if stem == '#RENDER#':
        stem_in = f'MRG__{chip_name}'
        stems.append(stem_in)
        sub_map_names[stem_in] = 'render'
    else:
        layer = '.'.join(data['colors'][stem]["layer"].split('/'))
        stem_in = f'RAW__{chip_name}_{layer}.{stem}'
        stems.append(stem_in)
        sub_map_names[stem_in] = stem

num_tiles_x = data['image']['num_segs_width']
num_tiles_y = data['image']['num_segs_height']
map_tile_size = data['map']['openmaps_tile_size_px']
tile_size = data['image']['px_height'] // data['image']['num_segs_height']

num_tiles_max = max(num_tiles_x, num_tiles_y)
max_zoom_lvl = math.ceil(math.log((num_tiles_max * tile_size / map_tile_size), 2))
merge_zoom_lvl = math.ceil(math.log(num_tiles_max, 2))
num_tiles = 2**merge_zoom_lvl

# prepare command
cmd = ''

# prepare tmp dir
cmd += f'mkdir -p {tmp_dir}\n'


# only scale -> create base level
for stem in stems:
    # create zoom directory
    out_dir = data['map']['output'] + f'/{sub_map_names[stem]}'
    cmd += f'mkdir -p {out_dir}\n'
    cmd += f'mkdir -p {out_dir}/{merge_zoom_lvl}\n'

    # go through y input tiles
    for t_y in range(num_tiles_y - 1, -1, -1):
        t_y_map = num_tiles_y - 1 - t_y

        # go through x input tiles
        for t_x in range(num_tiles_x - 1, -1, -1):
            t_x_map = t_x

            # scale and autotile
            scaled_size = int(tile_size / 2**(max_zoom_lvl - merge_zoom_lvl))
            cmd += f'convert {root}/{stem}_{t_y}-{t_x}.png -resize {scaled_size}x{scaled_size} -crop {map_tile_size}x{map_tile_size} {tmp_dir}/{stem}_{t_y}-{t_x}.cropped.%d.png\n'

            # create map dirs and move
            num_map_tiles = scaled_size // map_tile_size
            for m_x in range(0, num_map_tiles):
                cmd += f'mkdir -p {out_dir}/{merge_zoom_lvl}/{m_x + num_map_tiles * t_x_map}\n'

                # move the images
                for m_y in range(0, num_map_tiles):
                    t_num = m_x + num_map_tiles * m_y
                    cmd += f'mv {tmp_dir}/{stem}_{t_y}-{t_x}.cropped.{t_num}.png {out_dir}/{merge_zoom_lvl}/{m_x + num_map_tiles * t_x_map}/{m_y + num_map_tiles * t_y_map}.png\n'


# add non-exiting tiles on merge zoom level
for stem in stems:
    # create zoom directory
    out_dir = data['map']['output'] + f'/{sub_map_names[stem]}'

    # go through y input tiles
    if num_tiles_y < num_tiles:
        for t_x in range(num_tiles_x - 1, -1, -1):
            cmd += f'mkdir -p {out_dir}/{merge_zoom_lvl}/{t_x}\n'
            for t_y in range(num_tiles - 1, num_tiles_y - 1, -1):
                cmd += f'convert -size {map_tile_size}x{map_tile_size} xc:none {out_dir}/{merge_zoom_lvl}/{t_x}/{t_y}.png\n'

    if num_tiles_x < num_tiles:
        for t_x in range(num_tiles - 1, num_tiles_x - 1, -1):
            cmd += f'mkdir -p {out_dir}/{merge_zoom_lvl}/{t_x}\n'
            for t_y in range(num_tiles - 1, -1, -1):
                cmd += f'convert -size {map_tile_size}x{map_tile_size} xc:none {out_dir}/{merge_zoom_lvl}/{t_x}/{t_y}.png\n'


# merge and crop: going up
for zoom in range(merge_zoom_lvl, 0, -1):
    for stem in stems:
        # create zoom directory
        out_dir = data['map']['output'] + f'/{sub_map_names[stem]}'

        # create zoom directory
        cmd += f'mkdir -p {out_dir}/{zoom - 1}\n'

        # go through merge zoom level and combine
        for t_x in range(0, 2**(zoom - 1)):

            # create y directories
            cmd += f'mkdir -p {out_dir}/{zoom-1}/{t_x}\n'
            for t_y in range(0, 2**(zoom - 1)):

                # merge and scale
                cmd += f'convert -append {out_dir}/{zoom}/{2 * t_x}/{2 * t_y}.png {out_dir}/{zoom}/{2 * t_x}/{2 * t_y + 1}.png {tmp_dir}/{t_x}{t_y}_right.png\n'
                cmd += f'convert -append {out_dir}/{zoom}/{2 * t_x + 1}/{2 * t_y}.png {out_dir}/{zoom}/{2 * t_x + 1}/{2 * t_y + 1}.png {tmp_dir}/{t_x}{t_y}_left.png\n'
                cmd += f'convert +append {tmp_dir}/{t_x}{t_y}_right.png {tmp_dir}/{t_x}{t_y}_left.png {tmp_dir}/{t_x}{t_y}_large.png\n'
                cmd += f'convert {tmp_dir}/{t_x}{t_y}_large.png -resize {map_tile_size}x{map_tile_size} {out_dir}/{zoom - 1}/{t_x}/{t_y}.png\n'
                cmd += f'rm {tmp_dir}/{t_x}{t_y}_right.png\n'
                cmd += f'rm {tmp_dir}/{t_x}{t_y}_left.png\n'
                cmd += f'rm {tmp_dir}/{t_x}{t_y}_large.png\n'


# scale and crop phase: going down
for zoom in range(merge_zoom_lvl + 1, max_zoom_lvl + 1):

    for stem in stems:
        # create zoom directory
        out_dir = data['map']['output'] + f'/{sub_map_names[stem]}'

        # create zoom directory
        cmd += f'mkdir -p {out_dir}/{zoom}\n'

        # go through y input tiles
        for t_y in range(num_tiles_y - 1, -1, -1):
            t_y_map = num_tiles_y - 1 - t_y

            # go through x input tiles
            for t_x in range(num_tiles_x - 1, -1, -1):
                t_x_map = t_x

                # scale and autotile
                scaled_size = int(tile_size / 2**(max_zoom_lvl - zoom))
                cmd += f'convert {root}/{stem}_{t_y}-{t_x}.png -resize {scaled_size}x{scaled_size} -crop {map_tile_size}x{map_tile_size} {tmp_dir}/{stem}_{t_y}-{t_x}.cropped.%d.png\n'

                # create map dirs and move
                num_map_tiles = scaled_size // map_tile_size
                for m_x in range(0, num_map_tiles):
                    cmd += f'mkdir -p {out_dir}/{zoom}/{m_x + num_map_tiles * t_x_map}\n'

                    # move the images
                    for m_y in range(0, num_map_tiles):
                        t_num = m_x + num_map_tiles * m_y
                        cmd += f'mv {tmp_dir}/{stem}_{t_y}-{t_x}.cropped.{t_num}.png {out_dir}/{zoom}/{m_x + num_map_tiles * t_x_map}/{m_y + num_map_tiles * t_y_map}.png\n'


# emit command
print(cmd)
