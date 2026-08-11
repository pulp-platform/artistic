# ArtistIC

ArtistIC turns a chip GDS into artwork, layer images, and a zoomable map. A
project is a TOML file and the Python `Project` class is the implementation of
the flow. The `bin/artistic` command and the Makefile only select a project and
call that interface.

Each product has explicit stages. KLayout stages generate physical data;
host stages apply colors or assemble image files.

```text
logo prepare -> logo merge -> render generate -> render compose
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
- `[technology]` may provide a technology file override.
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

Raw masks need to be regenerated after changing the input, resolution, segment
grid, overrender factor, viewport margin, or selected layers. Colors, palettes,
render formats, map views, tile size, and output directory are applied by the
host stages and can be changed without rerunning KLayout.

## Technology and tools

Technology files are resolved in this order:

1. `[technology].file` in the project;
2. `KLAYOUT_TECH_FILE`;
3. `tech/$KLAYOUT_TECH.lyt` below each location in `KLAYOUT_PATH`.

ArtistIC reads the technology connectivity graph to discover the routing stack
and terminal metal. The KLayout worker inspects the actual GDS and writes raw
layer masks. Image composition and map assembly run in ordinary Python.

KLayout stages require KLayout with Python support. Host stages require Python
3.11 or newer, Pillow, ImageMagick, and Inkscape for SVG artwork. Map viewers
load Leaflet from its public CDN.

## License

ArtistIC is licensed under Apache-2.0. See [`LICENSE`](LICENSE).
