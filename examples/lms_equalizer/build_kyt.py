# SPDX-License-Identifier: GPL-3.0-or-later
"""Regenerate ``lms_equalizer.kyt`` from ``lms_equalizer.grc`` via the real
import → auto place-and-route → build flow. Verified END TO END by
``lms_eq_demo.py`` / ``verification/tests/test_lms_equalizer_example.py``."""
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

    from lms_eq_demo import KYT_PATH, import_and_pnr

    project, bres, _cat, _ct = import_and_pnr()
    used = sum(c.cell_count for c in bres.chips.values())
    save_project(project, KYT_PATH)
    print(f"wrote {KYT_PATH} ({used}/120 cells, {len(project.blocks)} blocks)")


if __name__ == "__main__":
    main()
