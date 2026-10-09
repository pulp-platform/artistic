# MLEM

[MLEM](http://asic.ethz.ch/2024/MLEM.html) is the first tapeout based on the
[Croc SoC platform](https://github.com/pulp-platform/croc). Students built it
with an open-source flow using [yosys-slang](https://github.com/povik/yosys-slang)
and the [IHP 130 nm PDK](https://github.com/IHP-GmbH/IHP-Open-PDK), as a pilot for
the [Open VLSI Lecture](https://vlsi.ethz.ch/).

## Run the Example

From the ArtistIC directory, with Python 3.11+, Pillow, img2pdf, KLayout,
Inkscape and potrace available:

```sh
make PROJECT=examples/mlem/project.toml all
```

This runs inspection, logo placement, rendering, module annotation, and the
full 12288-pixel map. `work/` also contains the palette preview, physical logo
SVG, an 84.1 cm-wide PDF, and a four-sheet A4 poster. The macro LEF is a
bounding-box fixture for annotation only, not a physical-design model.

The example uses the original PNG logo at 1576 um, 4 um metal squares on a 6 um
grid, and the original -100..2100 um viewport. The images below use a white
background, chip shadow, and contrasting module outlines and labels.
Set `[render.shadow].enabled = false` to remove the shadow.

## Compare the Legacy Renderer

`legacy-render.toml` isolates rendering from changes to logo geometry. Place
the chip GDS from the old logo flow at `legacy/mlem_chip.gds.gz`, then run:

```sh
make PROJECT=examples/mlem/legacy-render.toml render-generate render-compose
```

This restores the legacy render settings, including the black background and
84.1 cm page width. Raw masks can be compared directly; final images use
Lanczos rather than box downsampling. Use the same input GDS for both renderers:
the new spaced-square logo has different geometry.

## Example Output

![MLEM Result](golden/mlem_render.jpg)

![MLEM Module Result](golden/mlem_render_modules.jpg)
