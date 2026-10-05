# Copyright 2026 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

import copy
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from artistic.image_outputs import pdf_geometry
from artistic.inspection import analyze, format_summary, write_palette_preview
from artistic.palettes import palette
from artistic.project import Project, ProjectError
from artistic.render import _settings, generation_hash, resolve_dimensions


class InspectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = {
            "design": {"name": "chip", "work_dir": self.temporary.name,
                       "gds": str(Path(self.temporary.name) / "chip.gds")},
            "render": {"resolution": [200, 100], "overrender": 3, "max_px_tile": 128,
                       "layers": ["M2"], "palette": "generated", "page_height_cm": 10},
            "map": {"resolution": [100, 200], "layers": ["M1", "M2"],
                    "palette": "generated", "colors": {"M1": {"color": "#123456", "alpha": .4}}},
            "palettes": {"generated": {"background": "#12345680", "generate": {
                "hue_start_deg": 40, "saturation": .8, "lightness": [.4, .7],
                "alpha": [.9, .3]}}},
        }
        self.manifest = {
            "gds": "chip.gds", "render": {}, "map": {},
            "technology": {"routing": ["M1", "V1", "M2"], "top_metal": "M2",
                           "symbols": {"M1": {"layer": 1, "datatype": 0},
                                       "V1": {"layer": 2, "datatype": 0},
                                       "M2": {"layer": 3, "datatype": 0}}},
            "layout": {"top_cell": "chip", "bbox_um": [0, 0, 10, 10],
                       "present_layers": [{"layer": i, "datatype": 0} for i in (1, 2, 3)]},
        }

    def test_actual_viewport_nm_per_px_and_page(self):
        original = copy.deepcopy(self.manifest)
        report = analyze(self.config, self.manifest)
        render = report["render"]
        self.assertEqual(render["viewport_um"], [-5, 0, 15, 10])
        self.assertEqual(render["requested_viewport_um"], [0, 0, 10, 10])
        self.assertEqual(render["resolution"], [200, 100])
        self.assertEqual(render["raw_resolution"], [600, 300])
        self.assertEqual(render["segments"], [5, 3])
        self.assertEqual(render["nm_per_px"], 100)
        self.assertAlmostEqual(render["raw_nm_per_px"], 100 / 3)
        self.assertEqual(render["page_cm"], [20, 10])
        self.assertAlmostEqual(render["dpi"], 25.4)
        self.assertEqual(report["map"]["viewport_um"], [0, -5, 10, 15])
        self.assertEqual(report["map"]["nm_per_px"], 100)
        self.assertEqual(self.manifest, original)
        summary = format_summary(report)
        for value in ("Top cell: chip", "600 x 300 px", "100 nm/px", "20 x 10 cm", "25.4 dpi"):
            self.assertIn(value, summary)

    def test_summary_reports_palette_preview_path(self):
        report = analyze(self.config, self.manifest)
        report["palette_preview"] = "chip_palette.svg"
        self.assertIn("Palette preview: chip_palette.svg", format_summary(report))

    def test_explicit_viewport_and_margin(self):
        self.config["render"]["viewport_um"] = [10, 20, 50, 30]
        self.assertEqual(analyze(self.config, self.manifest)["render"]["viewport_um"],
                         [10, 15, 50, 35])
        del self.config["render"]["viewport_um"]
        self.config["render"]["margin_um"] = 5
        self.assertEqual(analyze(self.config, self.manifest)["render"]["viewport_um"],
                         [-15, -5, 25, 15])

    def test_raw_tile_bound_and_equivalent_generation_hash(self):
        settings = self.config["render"]
        automatic = generation_hash(self.config, "render")
        settings["segments"] = [5, 3]
        self.assertEqual(generation_hash(self.config, "render"), automatic)
        del settings["max_px_tile"]
        self.assertEqual(generation_hash(self.config, "render"), automatic)
        settings["segments"] = [4, 3]
        self.assertNotEqual(generation_hash(self.config, "render"), automatic)
        settings["max_px_tile"] = 128
        with self.assertRaisesRegex(ProjectError, "max_px_tile"):
            resolve_dimensions(settings)
        settings.pop("segments")
        settings["overrender"] = 1
        self.assertEqual(resolve_dimensions(settings)["segments"], [2, 1])

    def test_generation_record_uses_resolved_dimensions_and_viewport(self):
        source = Path(self.config["design"]["gds"])
        source.write_bytes(b"gds")
        record = _settings(self.config, self.manifest, "render", source, [("M2", 3, 0)])
        self.assertEqual(record["resolution"], [200, 100])
        self.assertEqual(record["segments"], [5, 3])
        self.assertEqual(record["overrender"], 3)
        self.assertEqual(record["gds"]["viewport_um"], [-5, 0, 15, 10])
        self.assertEqual(record["generation_sha256"], generation_hash(self.config, "render"))
        self.config["map"].update(overrender=2, max_px_tile=64)
        self.assertEqual(analyze(self.config, self.manifest)["map"]["segments"], [4, 7])

    def test_default_dpi_and_width_only_page_geometry(self):
        default = pdf_geometry([600, 300], {})
        self.assertEqual(default["dpi"], 300)
        self.assertEqual(default["page_cm"], [5.08, 2.54])
        width_only = pdf_geometry([600, 300], {"page_width_cm": 10})
        self.assertEqual(width_only["page_cm"], [10, 5])
        self.assertEqual(width_only["image_cm"], [10, 5])
        self.assertEqual(width_only["dpi"], 152.4)

    def test_invalid_raw_tile_and_dimension_controls(self):
        for name, values in {"max_px_tile": [0, -1, True, 1.5, "128", None],
                             "overrender": [0, True, 1.5],
                             "resolution": [100, [0, 1], [1.5, 1], [True, 1]],
                             "segments": [100, [0, 1], [1.5, 1], [601, 1]]}.items():
            for value in values:
                with self.subTest(name=name, value=value):
                    settings = {**self.config["render"], name: value}
                    with self.assertRaisesRegex(ProjectError, name):
                        resolve_dimensions(settings)

    def test_palette_preview_resolves_selected_colors_and_alpha(self):
        preview = write_palette_preview(self.config, self.manifest)
        self.assertEqual(preview, Path(self.temporary.name) / "chip_palette.svg")
        ns = {"s": "http://www.w3.org/2000/svg"}
        root = ET.parse(preview).getroot()
        labels = [item.text for item in root.findall("s:text", ns)]
        self.assertTrue(any("mask layers (black on white)" in label for label in labels))
        groups = {(group.get("data-section"), group.get("data-layer")): group
                  for group in root.findall("s:g", ns)}
        self.assertEqual(set(groups), {("render", "background"), ("render", "M2"),
                                       ("map", "background"), ("map", "M1"), ("map", "M2")})
        resolved = palette(self.config, self.config["render"], [("M2", 3, 0)],
                           routing=self.manifest["technology"]["routing"])
        swatch = groups[("render", "M2")].findall("s:rect", ns)[1]
        self.assertEqual(swatch.get("fill"), resolved["M2"]["color"])
        self.assertAlmostEqual(float(swatch.get("fill-opacity")), .3)
        swatch = groups[("map", "M1")].findall("s:rect", ns)[1]
        self.assertEqual(swatch.get("fill"), "#000000")
        self.assertEqual(float(swatch.get("fill-opacity")), 1)
        swatch = groups[("render", "background")].findall("s:rect", ns)[1]
        self.assertAlmostEqual(float(swatch.get("fill-opacity")), 128 / 255)

    def test_section_gds_inspection_and_logo_metadata_preservation(self):
        design = Path(self.config["design"]["gds"])
        alternate = Path(self.temporary.name) / "alternate.gds"
        design.write_bytes(b"design")
        alternate.write_bytes(b"alternate")
        self.config["render"]["input"] = str(alternate)
        self.config["render"]["layers"] = ["M1"]
        self.config["map"]["input"] = "logo"
        self.config["_root"] = self.temporary.name
        previous = copy.deepcopy(self.manifest)
        previous["logo"] = {"resolved_layer": {"name": "M2", "layer": 3,
                                                 "datatype": 0}}
        Path(self.temporary.name, "manifest.json").write_text(json.dumps(previous))
        Path(self.temporary.name, "logo_merge.json").write_text(json.dumps({
            "resolved_layer": previous["logo"]["resolved_layer"]}))

        def fake_inspect(config, technology=None, input_gds=None):
            report = copy.deepcopy(self.manifest)
            if input_gds == alternate.resolve():
                report["layout"]["bbox_um"] = [10, 20, 50, 60]
                report["layout"]["present_layers"] = [{"layer": 1, "datatype": 0}]
            return report

        from artistic import technology as technology_module
        from artistic import project as project_module
        project = Project(Path(self.temporary.name) / "project.toml", self.config)
        with patch.object(technology_module, "inspect_layout", side_effect=fake_inspect), \
                patch.object(project_module, "input_gds", side_effect=lambda config, section:
                             alternate.resolve() if section == "render" or
                             config.get(section, {}).get("input") == "logo" else
                             design.resolve()):
            result = project.inspect()
        self.assertEqual(result["layout"]["bbox_um"], [0, 0, 10, 10])
        self.assertEqual(result["render"]["layout_bbox_um"], [10, 20, 50, 60])
        self.assertEqual(result["render"]["layers_resolved"], ["M1"])
        self.assertEqual(result["render"]["input_gds"], str(alternate.resolve()))
        self.assertEqual(result["map"]["layout_bbox_um"], [10, 20, 50, 60])
        self.assertEqual(result["logo"]["resolved_layer"],
                         previous["logo"]["resolved_layer"])

    def test_inspection_manifest_config_is_deep_copied(self):
        from artistic import technology as technology_module
        source = Path(self.config["design"]["gds"])
        source.write_bytes(b"gds")
        self.config["_root"] = self.temporary.name
        tech = self.manifest["technology"]
        config = copy.deepcopy(self.config)
        with patch.object(technology_module, "resolve_project", return_value=tech), \
                patch.object(technology_module, "run_pya",
                             side_effect=lambda request: Path(request["result"]).write_text(
                                 json.dumps(self.manifest["layout"]))):
            report = technology_module.inspect_layout(config)
        report["render"]["layers"].append("mutated")
        report["map"]["colors"]["M1"]["alpha"] = 0
        self.assertEqual(self.config, config)

    def test_missing_logo_input_uses_labeled_design_estimate(self):
        source = Path(self.config["design"]["gds"])
        source.write_bytes(b"gds")
        self.config["render"]["input"] = "logo"
        self.config["_root"] = self.temporary.name
        from artistic import technology as technology_module
        project = Project(Path(self.temporary.name) / "project.toml", self.config)
        with patch.object(technology_module, "inspect_layout",
                          return_value=copy.deepcopy(self.manifest)):
            result = project.inspect()
        self.assertEqual(result["render"]["layout_bbox_um"], [0, 0, 10, 10])
        self.assertEqual(result["render"]["geometry_source"],
                         "estimate (logo GDS not prepared)")

    def test_stale_logo_input_falls_back_without_preserving_resolution(self):
        source = Path(self.config["design"]["gds"])
        source.write_bytes(b"gds")
        stale = Path(self.temporary.name) / "chip_chip.gds.gz"
        stale.write_bytes(b"stale")
        self.config["render"]["input"] = "logo"
        self.config["logo"] = {"layer": "M1"}
        self.config["_root"] = self.temporary.name
        old = {"name": "M2", "layer": 3, "datatype": 0}
        previous = copy.deepcopy(self.manifest)
        previous["logo"] = {"resolved_layer": old}
        Path(self.temporary.name, "manifest.json").write_text(json.dumps(previous))
        from artistic import technology as technology_module
        from artistic import project as project_module
        project = Project(Path(self.temporary.name) / "project.toml", self.config)

        def invalid_logo(config, section):
            if config.get(section, {}).get("input") == "logo":
                raise ProjectError("merged logo is stale")
            return source.resolve()

        with patch.object(technology_module, "inspect_layout",
                          return_value=copy.deepcopy(self.manifest)), \
                patch.object(project_module, "input_gds",
                             side_effect=invalid_logo):
            result = project.inspect()
        self.assertEqual(result["render"]["geometry_source"],
                         "estimate (logo GDS is stale; prepare and merge required)")
        self.assertNotIn("resolved_layer", result.get("logo", {}))

    def test_valid_merge_record_retains_resolution_without_logo_section_input(self):
        source = Path(self.config["design"]["gds"])
        source.write_bytes(b"gds")
        merged = Path(self.temporary.name) / "chip_chip.gds.gz"
        merged.write_bytes(b"merged")
        resolution = {"name": "M2", "layer": 3, "datatype": 0}
        Path(self.temporary.name, "logo_merge.json").write_text(
            json.dumps({"resolved_layer": resolution}))
        self.config["_root"] = self.temporary.name
        from artistic import technology as technology_module
        from artistic import project as project_module
        project = Project(Path(self.temporary.name) / "project.toml", self.config)
        with patch.object(technology_module, "inspect_layout",
                          return_value=copy.deepcopy(self.manifest)), \
                patch.object(project_module, "input_gds", side_effect=lambda config, section:
                             merged if config.get("render", {}).get("input") == "logo" else
                             source.resolve()):
            result = project.inspect()
        self.assertEqual(result["logo"]["resolved_layer"], resolution)

    def test_palette_preview_escapes_labels(self):
        special = 'M<&"2'
        self.manifest["technology"]["symbols"][special] = {"layer": 3, "datatype": 0}
        self.config["render"]["layers"] = [special]
        preview = write_palette_preview(self.config, self.manifest)
        ns = {"s": "http://www.w3.org/2000/svg"}
        labels = [item.text for item in ET.parse(preview).getroot().findall(".//s:text", ns)]
        self.assertTrue(any(label.startswith(special) for label in labels))


if __name__ == "__main__":
    unittest.main()
