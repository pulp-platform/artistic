# ArtistIC

ArtistIC turns a chip GDS into artwork, layer images, and a zoomable map. A
project is a TOML file and the Python `Project` class is the implementation of
the flow. The `bin/artistic` command and the Makefile only select a project and
call that interface.

Each product has explicit stages. KLayout stages generate physical data;
host stages apply colors or assemble image files.

```text
logo prepare -> logo merge -> render generate -> render compose -> render annotate (optional)
                                  \-> map generate -> map build
```

## Quick start

Use the example project from the ArtistIC directory:

```sh
bin/artistic inspect examples/mlem/project.toml
make PROJECT=examples/mlem/project.toml logo-prepare
# In the environment that provides KLayout:
make PROJECT=examples/mlem/project.toml logo-merge render-generate map-generate
make PROJECT=examples/mlem/project.toml render-compose map-build
```

`make all` runs the same stages in order when one environment provides both
KLayout and the host image tools.

The same commands work from another working directory. Paths in the TOML file
are resolved relative to that file, not relative to the shell's current
directory.

The stages are also available as Make targets:

| Target | Result |
| --- | --- |
| `inspect` | Detect the layout bounding box and routing stack |
| `logo-prepare` | Convert artwork to a feature-grid mask |
| `logo-merge` | Place complete mask features on the selected metal layer |
| `render-generate` | Generate one raw mask per selected layer and segment |
| `render-compose` | Apply colors and write PNG, JPEG, or PDF images |
| `render-annotate` | Add module outlines and labels from a placed DEF |
| `map-generate` | Generate raw masks for map tiles |
| `map-build` | Assemble memory-bounded tile pyramids and an HTML viewer |
| `all` | Run logo, render, and map stages in dependency order |

Run `bin/artistic --help` or `make -f Makefile -n all` to inspect the thin
adapters without executing a stage.

## Project file

See [`examples/mlem/project.toml`](examples/mlem/project.toml). Users edit only
the TOML file; JSON files in `work/` are generated records containing the
resolved inputs and hashes used to reject stale later stages.

- `[design]` defines the project name, source GDS, and work directory.
- `[technology]` may provide a technology file or explicit layer-stack
  overrides.
- `[logo]` defines artwork, physical width and height, feature size, selected
  layer, and optional center offsets.
- `[render]` and `[map]` define the input (`design`, `logo`, or another GDS),
  viewport margin, resolution, segments, layers, palette, and outputs.
- `[palettes.<name>]` contains the background and per-layer colors. A
  `[render.colors.<layer>]` or `[map.colors.<layer>]` table overrides one color.

`layers = "routing"` follows the routing stack in the technology file. A list
selects exact layers. `top-metal` is an alias for the terminal routing layer.
For maps, `views = "metals"` selects the metal layers from the generated set;
`composite` is the colorized combination of all selected layers.

Render and map viewports start at the actual GDS bounding box and expand by
`margin_um`. Logo placement is centered in that box by default. Width, height,
and `offset_x_um`/`offset_y_um` control its feature-grid canvas.

For an intentional crop, `viewport_um = [left, bottom, right, top]` in the
`[render]` or `[map]` section replaces the automatically derived viewport.
`[render].page_width_cm` optionally sets the physical width and DPI of PDF
output without changing the rendered pixels.

Raw masks need to be regenerated after changing the input, resolution, segment
grid, overrender factor, viewport margin, or selected layers. Colors, palettes,
render formats, map views, tile size, and output directory are applied by the
host stages and can be changed without rerunning KLayout.

### Logo tones

Logo masks use complete `feature_um` squares. `[logo].dither` can be
`"threshold"` (the default for line artwork), `"floyd-steinberg"` (photographs
and smooth shading), or `"ordered"` (a repeating dot pattern). `threshold = 0.5`
sets the cutoff in threshold mode only; `contrast = 1.0` leaves contrast
unchanged in all modes.
Transparent artwork is flattened onto white before processing. Changing these
settings requires `logo-prepare` and `logo-merge` again.

### Colors and backgrounds

White is the default background. Set a palette's `background` to `"#000000"`
for black or `"transparent"` for transparency. PNG and PDF preserve alpha;
JPEG flattens onto `[render].jpeg_background`, which defaults to white and must
be opaque.

A palette can generate colors and rotate their hue without editing each layer:

```toml
[palettes.chip]
background = "transparent"
hue_rotation_deg = 30

[palettes.chip.generate]
hue_start_deg = 0
saturation = 0.75
lightness = [0.35, 0.55, 0.7]
alpha = [0.8, 0.2]
```

Generated hues follow the full routing stack, so selecting fewer layers does
not change their colors. Lightness values repeat; alpha interpolates from the
first to the last routing layer. Either option also accepts a single number.
Explicit palette layer colors override generated colors, then hue rotation
applies. Stage-specific `render.colors` or `map.colors` overrides apply last.

Map layer views default to `layer_style = "mask"`: opaque black geometry on
white, independent of the palette. Set `[map].layer_style = "color"` for
palette-colored individual layers. The composite always uses the palette.

### Poster sheets

`segments` divides KLayout rendering into memory-bounded work units; it does
not divide the printed image. Add a poster table to produce a separate
`<name>_poster.pdf` during composition:

```toml
[render.poster]
grid = [3, 2]              # columns, rows
page_size_mm = [210, 297]
margin_mm = 10
overlap_mm = 5
```

The image is fitted and centered across the printable area of all sheets.
Pages run left to right, then top to bottom. Adjacent sheets repeat the
configured overlap. This does not change the regular image/PDF outputs or
require new raw masks.

### Module outlines

Add `[render.outlines]` to describe annotations from a matching placed DEF,
then run `make PROJECT=... render-annotate` after `render-compose`:

```toml
[render.outlines]
def = "chip.def"
lef_files = ["macros/*.lef"]
resolution = 200
min_area_pixels = 50
background_opacity = 0.65
font_size = 14
stroke_width = 1
label_lightness = 0.85
formats = ["svg", "png", "pdf"]

[render.outlines.modules]
i_core = { label = "Core", color = "#fb120d" }
```

Module keys select instance groups. `resolution` limits the raster used to
trace outlines: a coarse grid groups nearby cells into module regions.
`min_area_pixels` removes smaller isolated regions from that tracing grid.
Font and stroke sizes are output-image pixels. LEFs supply macro sizes;
`**` in a LEF glob searches nested directories. Annotation follows the render
viewport and rejects a stale or modified composed image. After changing
palette colors or the background, rerun `render-compose` before annotation;
changing outline settings alone does not require recomposition.
Annotated JPEGs use the same `render.jpeg_background` as regular JPEGs.
Composition retains a PNG for the SVG background
even when PNG is not in `render.formats`; keep it alongside the SVG. Annotation
is optional and is not included in `make all`.

## Technology and tools

Technology files are resolved in this order:

1. `[technology].file` in the project;
2. `KLAYOUT_TECH_FILE`;
3. `tech/$KLAYOUT_TECH.lyt` below each location in `KLAYOUT_PATH`.

Projects without a suitable KLayout technology file can define the stack
directly. String and inline-table layer definitions can be mixed:

```toml
[technology]
routing = ["M0", "V0", "M1"]
top_metal = "M1" # optional; defaults to the last routing entry

[technology.layers]
M0 = "180/250"
V0 = { layer = 159, datatype = 250 }
M1 = "31/250"
```

When a technology file is available, explicit layers overlay its symbols and
explicit `routing` or `top_metal` values replace the discovered values. This
supports local PDK variations without duplicating the full technology file.

ArtistIC discovers the routing stack from the technology connectivity graph or
uses the stack defined by the project. The KLayout worker inspects the actual
GDS and writes raw layer masks. Image composition and map assembly run in
ordinary Python.

KLayout stages require KLayout with Python support. Host stages require Python
3.11 or newer and Pillow. SVG artwork also requires Inkscape, and PDF output
requires img2pdf. Poster output also uses pikepdf (installed with img2pdf).
Module outlines require potrace; annotation PNG/PDF exports use Inkscape.
Map viewers load Leaflet from its public CDN.

`bin/artistic` uses `PYTHON` when set, otherwise the active `python3` (including
an activated virtual environment). If the default `python3` is older, run
`PYTHON=python3.11 bin/artistic ...`. Install host dependencies for that same
interpreter; its normal site packages remain available.

Generated stage records use project- or work-relative paths, so
the whole project tree can move between host and KLayout environments. Older
stage records need regeneration. A nonempty map output directory is rebuilt
only when it contains ArtistIC's ownership marker; choose a fresh directory
for an existing unrelated output.

## License

ArtistIC is licensed under Apache-2.0. See [`LICENSE`](LICENSE).
