# SPDX-License-Identifier: GPL-3.0-or-later
"""Regenerate the three effect ``.kyt`` files from their ``.grc`` sources via
the real auto flow. Verified END TO END by ``audio_effects_demo.py`` /
``verification/tests/test_audio_effects_example.py``."""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for _p in (str(ROOT / "runtime" / "python"), str(ROOT / "placekyt"), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def main():
    from engine.io.project_io import save_project

    from audio_effects_demo import EFFECTS, import_and_pnr

    for which, (grc_name, _tol, _n) in EFFECTS.items():
        project, bres, _cat, _ct = import_and_pnr(grc_name)
        used = sum(c.cell_count for c in bres.chips.values())
        kyt = HERE / grc_name.replace(".grc", ".kyt")
        save_project(project, kyt)
        print(f"wrote {kyt.name} ({used}/120 cells, "
              f"{len(project.blocks)} blocks)")


if __name__ == "__main__":
    main()
