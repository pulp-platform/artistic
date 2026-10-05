# Copyright 2026 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from artistic.logo import _mask_rows, _merge_config_sha256, merge, prepare
from artistic.project import ProjectError, input_gds, project_relative, sha256, write_json

try:
    from PIL import Image
except ImportError:
    Image = None


@unittest.skipIf(Image is None, "Pillow is not installed")
class LogoStageValidityTests(unittest.TestCase):
    def _setup(self, root):
        source = root / "logo.png"
        with Image.new("RGB", (2, 2), "black") as image:
            image.save(source)
        gds = root / "chip.gds"
        gds.write_bytes(b"base layout")
        config = {"_root": str(root), "design": {"name": "chip", "gds": str(gds),
                  "work_dir": str(root / "work")},
                  "logo": {"source": str(source), "width_um": 2, "height_um": 2,
                           "feature_um": 1, "spacing_um": 1, "pitch_um": 2},
                  "render": {"input": "logo"}, "map": {"input": "logo"},
                  "technology": {"layers": {"M1": "8/0"}, "routing": ["M1"]}}
        prepare(config)
        return config

    def _manifest(self, config, technology=None):
        tech = {"technology": None, "symbols": {"M1": {"layer": 8, "datatype": 0}},
                "routing": ["M1"], "top_metal": "M1"}
        return {"technology": tech, "layout": {"bbox_dbu": [0, 0, 10, 10]},
                "logo": {}}

    def _worker(self, request):
        for key in ("logo_gds", "chip_gds", "logo_svg"):
            Path(request[key]).write_bytes(b"generated " + key.encode())
        Path(request["result"]).write_text(json.dumps({"logo_shapes": 1,
                                                        "accepted_pixels": 1}))

    def _worker_without_result(self, request):
        for key in ("logo_gds", "chip_gds", "logo_svg"):
            Path(request[key]).write_bytes(b"generated " + key.encode())

    def test_snapshot_is_consumed_without_reexpanding_source(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._setup(Path(directory))
            with patch("artistic.logo._expanded_source", side_effect=AssertionError("re-expanded")), \
                 patch("artistic.logo.inspect_layout", side_effect=self._manifest), \
                 patch("artistic.logo.run_pya", side_effect=self._worker):
                merged = merge(config)
            self.assertTrue(merged.is_file())
            self.assertEqual(input_gds(config, "render"), merged.resolve())

    def test_svg_revision_snapshot_survives_changed_expansion_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._setup(root)
            svg = root / "logo.svg"
            svg.write_text("<svg>{{date}} {{git_revision}}</svg>")
            config["logo"]["source"] = str(svg)
            work = Path(config["design"]["work_dir"])
            templated = work / "chip_logo.svg"
            templated.write_text("<svg>prepared-date prepared-revision</svg>")
            preparation = json.loads((work / "logo_prepare.json").read_text())
            preparation.update(source=project_relative(config, svg),
                               source_sha256=sha256(svg),
                               expanded_source_sha256=sha256(templated))
            write_json(work / "logo_prepare.json", preparation)
            with patch("artistic.logo._expanded_source", side_effect=AssertionError("re-expanded")), \
                 patch("artistic.logo.inspect_layout", side_effect=self._manifest), \
                 patch("artistic.logo.run_pya", side_effect=self._worker):
                merge(config)
            self.assertTrue(input_gds(config, "render").is_file())
            templated.write_text("edited after merge")
            with self.assertRaisesRegex(ProjectError, "prepared logo SVG changed"):
                input_gds(config, "render")

    @unittest.skipUnless(shutil.which("inkscape"), "Inkscape is not installed")
    def test_real_prepared_svg_snapshot_survives_later_date_and_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._setup(root)
            svg = root / "logo.svg"
            svg.write_text('<!-- café -->\n'
                           '<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8">'
                           '<rect width="8" height="8" fill="black"/></svg>', encoding="utf-8")
            config["logo"]["source"] = str(svg)
            prepare(config)
            with patch("artistic.logo.inspect_layout", side_effect=self._manifest), \
                 patch("artistic.logo.run_pya", side_effect=self._worker), \
                 patch("artistic.logo._expanded_source", side_effect=AssertionError("re-expanded")):
                merge(config)
                self.assertTrue(input_gds(config, "render").is_file())

    def test_changed_source_and_failed_retry_invalidate_success(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._setup(Path(directory))
            with patch("artistic.logo.inspect_layout", side_effect=self._manifest), \
                 patch("artistic.logo.run_pya", side_effect=self._worker):
                merge(config)
            receipt = Path(config["design"]["work_dir"]) / "logo_merge.json"
            self.assertTrue(receipt.is_file())
            source = Path(config["logo"]["source"])
            original = source.read_bytes()
            source.write_bytes(b"different")
            with self.assertRaisesRegex(ProjectError, "rerun logo prepare"):
                input_gds(config, "map")
            source.write_bytes(original)
            with patch("artistic.logo.inspect_layout", side_effect=self._manifest), \
                 patch("artistic.logo.run_pya", side_effect=RuntimeError("worker failed")):
                with self.assertRaises(RuntimeError):
                    merge(config)
            self.assertFalse(receipt.exists())
            with self.assertRaisesRegex(ProjectError, "rerun logo merge"):
                input_gds(config, "render")

    def test_nonfinite_offsets_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._setup(Path(directory))
            config["logo"]["offset_x_um"] = float("nan")
            with self.assertRaisesRegex(ProjectError, "offset_x_um must be a finite number"):
                merge(config)

    def test_malformed_preparation_record_is_project_error(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._setup(Path(directory))
            (Path(config["design"]["work_dir"]) / "logo_prepare.json").write_text("[]")
            with self.assertRaisesRegex(ProjectError, "record is malformed"):
                merge(config)

    def test_malformed_stage_records_are_project_errors_at_consumption(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._setup(Path(directory))
            with patch("artistic.logo.inspect_layout", side_effect=self._manifest), \
                 patch("artistic.logo.run_pya", side_effect=self._worker):
                merge(config)
            work = Path(config["design"]["work_dir"])
            for filename in ("logo_prepare.json", "logo_merge.json"):
                path = work / filename
                original = path.read_text()
                for invalid in ("[]", "null"):
                    with self.subTest(filename=filename, invalid=invalid):
                        path.write_text(invalid)
                        with self.assertRaisesRegex(ProjectError, "record is malformed"):
                            input_gds(config, "render")
                path.write_text(original)

    def test_stale_merge_inputs_are_rejected_without_host_technology(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._setup(Path(directory))
            with patch("artistic.logo.inspect_layout", side_effect=self._manifest), \
                 patch("artistic.logo.run_pya", side_effect=self._worker):
                merge(config)
            config["logo"]["offset_x_um"] = 1
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ProjectError, "rerun logo merge"):
                    input_gds(config, "render")

    def test_base_gds_and_merged_output_changes_reject_merge_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._setup(Path(directory))
            with patch("artistic.logo.inspect_layout", side_effect=self._manifest), \
                 patch("artistic.logo.run_pya", side_effect=self._worker):
                merged = merge(config)
            base = Path(config["design"]["gds"])
            base_data = base.read_bytes()
            base.write_bytes(base_data + b"changed")
            with self.assertRaisesRegex(ProjectError, "rerun logo merge"):
                input_gds(config, "render")
            base.write_bytes(base_data)
            merged_data = merged.read_bytes()
            merged.write_bytes(merged_data + b"changed")
            with self.assertRaisesRegex(ProjectError, "rerun logo merge"):
                input_gds(config, "render")

    def test_mask_and_merge_record_version_tampering_rejects_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._setup(Path(directory))
            with patch("artistic.logo.inspect_layout", side_effect=self._manifest), \
                 patch("artistic.logo.run_pya", side_effect=self._worker):
                merge(config)
            work = Path(config["design"]["work_dir"])
            mask = work / "chip_logo_mono.png"
            mask.write_bytes(mask.read_bytes() + b"changed")
            with self.assertRaisesRegex(ProjectError, "rerun logo merge"):
                input_gds(config, "render")
            record = work / "logo_merge.json"
            contents = json.loads(record.read_text())
            contents["record_version"] = 2
            record.write_text(json.dumps(contents))
            with self.assertRaisesRegex(ProjectError, "rerun logo merge"):
                input_gds(config, "render")

    def test_preprocessing_change_and_missing_worker_result_reject(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._setup(Path(directory))
            with patch("artistic.logo.inspect_layout", side_effect=self._manifest), \
                 patch("artistic.logo.run_pya", side_effect=self._worker):
                merge(config)
            config["logo"]["threshold"] = 0.7
            with self.assertRaisesRegex(ProjectError, "rerun logo prepare"):
                input_gds(config, "render")
            config["logo"].pop("threshold")
            with patch("artistic.logo.inspect_layout", side_effect=self._manifest), \
                 patch("artistic.logo.run_pya", side_effect=self._worker_without_result):
                with self.assertRaisesRegex(ProjectError, "valid result record"):
                    merge(config)
            self.assertFalse((Path(config["design"]["work_dir"]) /
                              "logo_merge.json").exists())

    def test_worker_input_mutations_never_create_success_receipt(self):
        for target in ("base", "mask", "preparation"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as directory:
                config = self._setup(Path(directory))

                def mutate_during_worker(request):
                    self._worker(request)
                    path = {
                        "base": Path(config["design"]["gds"]),
                        "mask": Path(config["design"]["work_dir"]) / "chip_logo_mono.png",
                        "preparation": Path(config["design"]["work_dir"]) / "logo_prepare.json",
                    }[target]
                    path.write_bytes(path.read_bytes() + b"changed during merge")

                with patch("artistic.logo.inspect_layout", side_effect=self._manifest), \
                     patch("artistic.logo.run_pya", side_effect=mutate_during_worker):
                    with self.assertRaisesRegex(ProjectError, "changed during generation"):
                        merge(config)
                self.assertFalse((Path(config["design"]["work_dir"]) /
                                  "logo_merge.json").exists())
                with self.assertRaisesRegex(ProjectError, "rerun logo merge"):
                    input_gds(config, "render")

    def test_mask_mutation_during_decoding_never_creates_success_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._setup(Path(directory))
            mask = Path(config["design"]["work_dir"]) / "chip_logo_mono.png"
            def read_then_mutate(path):
                decoded = _mask_rows(path)
                path.write_bytes(path.read_bytes() + b"changed during decode")
                return decoded

            with patch("artistic.logo._mask_rows", side_effect=read_then_mutate), \
                 patch("artistic.logo.inspect_layout", side_effect=self._manifest), \
                 patch("artistic.logo.run_pya", side_effect=self._worker):
                with self.assertRaisesRegex(ProjectError, "changed during generation"):
                    merge(config)
            self.assertFalse((Path(config["design"]["work_dir"]) /
                              "logo_merge.json").exists())
            with self.assertRaisesRegex(ProjectError, "rerun logo merge"):
                input_gds(config, "render")

    def test_cross_host_relocated_work_dir_stays_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "original"
            original.mkdir()
            config = self._setup(original)
            with patch("artistic.logo.inspect_layout", side_effect=self._manifest), \
                 patch("artistic.logo.run_pya", side_effect=self._worker):
                merge(config)
            relocated = Path(directory) / "relocated"
            shutil.copytree(original, relocated)
            moved = dict(config)
            moved["_root"] = str(relocated)
            moved["design"] = {**config["design"], "gds": str(relocated / "chip.gds"),
                                "work_dir": str(relocated / "work")}
            moved["logo"] = {**config["logo"], "source": str(relocated / "logo.png")}
            with patch("artistic.technology.resolve_project",
                       side_effect=AssertionError("host technology must not be resolved")):
                self.assertTrue(input_gds(moved, "render").is_file())

    def test_same_content_source_can_move_outside_project(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            config = self._setup(root)
            moved_source = Path(directory) / "elsewhere" / "deeper" / "renamed.png"
            moved_source.parent.mkdir(parents=True)
            moved_source.write_bytes(Path(config["logo"]["source"]).read_bytes())
            config["logo"]["source"] = str(moved_source)
            with patch("artistic.logo.inspect_layout", side_effect=self._manifest), \
                 patch("artistic.logo.run_pya", side_effect=self._worker):
                merge(config)
            self.assertTrue(input_gds(config, "render").is_file())

    def test_source_symlink_target_can_change_when_content_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._setup(root)
            original = Path(config["logo"]["source"])
            first, second = root / "first.png", root / "second.png"
            first.write_bytes(original.read_bytes())
            second.write_bytes(original.read_bytes())
            link = root / "logo-link.png"
            link.symlink_to(first)
            config["logo"]["source"] = str(link)
            prepare(config)
            link.unlink()
            link.symlink_to(second)
            with patch("artistic.logo.inspect_layout", side_effect=self._manifest), \
                 patch("artistic.logo.run_pya", side_effect=self._worker):
                merge(config)
            self.assertTrue(input_gds(config, "render").is_file())

    def test_technology_path_hash_uses_literal_configuration(self):
        for configured_file in ("/outside/shared/tech.lyt", "tech-link.lyt", "~/tech/tech.lyt"):
            with self.subTest(configured_file=configured_file):
                hashes = []
                for root in (Path("/tmp/one"), Path("/tmp/many/deeper/roots/two")):
                    config = {"_root": str(root), "logo": {"layer": "M1"},
                              "technology": {"file": configured_file,
                                             "layers": {"M1": "8/0"}}}
                    hashes.append(_merge_config_sha256(config, {"offset_x_um": 0.0,
                                                                 "offset_y_um": 0.0},
                                                       "M1", 8, 0))
                self.assertEqual(hashes[0], hashes[1])
                changed = {"_root": "/different/root", "logo": {"layer": "Other"},
                           "technology": {"file": configured_file + ".new",
                                          "layers": {"M1": "9/1"}}}
                self.assertNotEqual(hashes[0], _merge_config_sha256(
                    changed, {"offset_x_um": 0.0, "offset_y_um": 0.0}, "M1", 8, 0))


if __name__ == "__main__":
    unittest.main()
