# Copyright 2026 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from artistic.technology import run_pya


class KLayoutRenderTests(unittest.TestCase):
    def test_headless_render_selects_top_cell_and_writes_nonblank_mask(self):
        klayout = shutil.which("klayout")
        if not klayout:
            self.skipTest("KLayout is not installed")
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is not installed")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gds, creator = root / "chip.gds", root / "create_layout.py"
            creator.write_text("""import os
import pya

layout = pya.Layout()
layout.dbu = 0.001
top = layout.create_cell("TOP")
top.shapes(layout.layer(8, 0)).insert(pya.Box(250, 250, 750, 750))
top.shapes(layout.layer(100, 0)).insert(pya.Box(0, 0, 1000, 1000))
layout.write(os.environ["ARTISTIC_TEST_GDS"])
""")
            env = os.environ.copy()
            env["ARTISTIC_TEST_GDS"] = str(gds)
            subprocess.run([klayout, "-b", "-r", str(creator)], check=True, env=env,
                           capture_output=True, text=True)

            raw = root / "raw"
            run_pya({"operation": "render", "gds": str(gds), "raw_dir": str(raw),
                     "chip": "chip", "viewport_um": [0, 0, 1, 1],
                     "resolution": [64, 64], "segments": [1, 1], "overrender": 1,
                     "layers": [{"name": "Metal1", "layer": 8, "datatype": 0}]})
            mask = raw / "RAW__chip_8.0.Metal1_0-0.png"
            self.assertTrue(mask.is_file())
            with Image.open(mask) as image:
                self.assertLess(image.convert("L").getextrema()[0], 255)


if __name__ == "__main__":
    unittest.main()
