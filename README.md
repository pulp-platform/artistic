# ArtistIC: An Open-Source Toolchain for Top-Metal IC Art and Ultra-High-Fidelity GDSII Renders

ArtistIC is a framework can be used to

* Translate and insert ASIC art on top-metal layers.
* Render GDSII files at ultra-high fidelity.

ArtistIC is part of the [PULP (Parallel Ultra-Low-Power) platform](https://pulp-platform.org/),
where it is used render high-definition posters of our [chips](http://asic.ethz.ch/).

ArtistIC is in a ***experimental*** stage and might produce bad results. We are happy to
receive your contributions through issues and PRs improving this framework. In a first step, the
entire process has to be streamlined.


## Top-Metal ASIC Artwork Generation (Meerkat)

***Logo Generation Only Tested for the [IHP 130nm Open PDK](https://github.com/IHP-GmbH/IHP-Open-PDK)***

Prepare a temporary work directory:

```
mkdir -p meerkat_work
```


Export the top-metal layer:

```
python3 scripts/meerkat_interface.py \
    -i ../examples/mlem/mlem_vanilla.gds.gz \
    -m mlem_tm.gds.gz \
    -g mlem_logo.gds \
    -o mlem_chip.gds.gz \
    -w meerkat_work \
    -l 134
cd meerkat_work; klayout -zz -rm ../scripts/export_top_metal.py; cd ..
cd meerkat_work; gzip -d mlem_tm.gds.gz; cd ..
```


Transform the logo to a 1-bit b/w image:

```
convert examples/mlem/mlem_logo.png -remap pattern:gray50 meerkat_work/mlem_logo_mono.png
```


Transform the logo to GDS:

```
python3 scripts/meerkat.py \
    -m 210,210 \
    -i meerkat_work/mlem_logo_mono.png \
    -g meerkat_work/mlem_tm.gds \
    -l 134 \
    -n mlem \
    -s meerkat_work/mlem_logo.svg \
    -o meerkat_work/mlem_logo.gds
```


Merge the logo into the chip:

```
cd meerkat_work; klayout -zz -rm ../scripts/merge_logo.py; cd ..
```


This generates the file `meerkat_work/mlem_chip.gds.gz` containing the generated top-metal logo.



## Ultra-High-Fidelity Rendering (RenderICs, formally Tapete)

For this example to work, ensure the previously generated GDSII is present in the work directory:

```
mkdir -p /dev/shm/renderics
cp meerkat_work/mlem_chip.gds.gz /dev/shm/renderics
```


Configuration is given through a `json` file. An example is provided in `examples/mlem/mlem.json`

The configuration can be checked using the Makefile.
Make sure to point this to a large enough temporary work directory (*abs path expected*)!

```
make CFG_FILE=examples/mlem/mlem.json analyze
```


The black/white database can be rendered using:

```
make CFG_FILE=examples/mlem/mlem.json gen_raw
```


The resulting PNGs and PDFs can be created using:

```
make CFG_FILE=examples/mlem/mlem.json gen_pdfs
```

In this example, the generated PDF is called `/dev/shm/renderics/PDF__mlem_0-0.pdf`
The last step can be parallelized using the '-j' option.

Profit!


## Automatic Module Outline Generation

***Module Outline Generation Only Tested for the [IHP 130nm Open PDK](https://github.com/IHP-GmbH/IHP-Open-PDK) and OpenROAD***

With the information gathered from the chip's DEF file, ArtistIC can automatically annotate module
outlines on top of renders.

For the provided example, fetch `v0.1.0` of IHP's open PDK in the `pdk` directory:

```
git clone https://github.com/IHP-GmbH/IHP-Open-PDK.git --recursive --branch v0.1.0 pdk
```

Unzip the DEF file:

```
gzip -dc examples/mlem/mlem.def.gz > /dev/shm/renderics/mlem.def
```

A vector image containing the outlines can then be generated using:

```
python3 scripts/gen_outline.py \
    -i /dev/shm/renderics/mlem.def \
    -o /dev/shm/renderics/mlem_modules.svg \
    -b /dev/shm/renderics/DPI__mlem_0-0.png \
    --lef_files pdk/ihp-sg13g2/libs.ref/sg13g2_sram/lef/*.lef \
    --px_scale 15000 \
    --offset_x 100 \
    --offset_y 83 \
    --module_json examples/mlem/mlem_modules.json \
    --opacity 0.65 \
    --font_size 35 \
    --luminosity 0.85
```

The resulting file is called `/dev/shm/renderics/mlem_modules.svg` and does not contain the bond pads.


## License
ArtistIC is released under Version 2.0 (Apache-2.0) see [`LICENSE`](LICENSE):


## Contributing
We are happy to accept pull requests and issues from any contributors. See [`CONTRIBUTING.md`](CONTRIBUTING.md)
for additional information.


## Prerequisites

- [`ImageMagick  >= v6.9.12-93`](https://imagemagick.org/script/download.php)
- [`Inkscape  >= v1.0.0`](inkscape.org)
- [`Potrace  >= v1.15`](https://potrace.sourceforge.net/)
- [`KLayout  >= v0.29.0`](https://www.klayout.de/build.html)
- [`img2pdf  >= v0.4.4`](https://pypi.org/project/img2pdf)
- [`gdspy  >= v1.6.13`](https://pypi.org/project/gdspy)
- [`Pillow  >= v10.0.0`](https://pypi.org/project/pillow)
- [`svgpathtools  >= v1.7.2`](https://pypi.org/project/svgpathtools)
