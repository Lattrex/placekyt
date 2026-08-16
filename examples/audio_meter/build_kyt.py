# SPDX-License-Identifier: GPL-3.0-or-later
"""Regenerate ``audio_meter.kyt`` from ``audio_meter.grc`` via the real auto
flow (import → duplex auto place-and-route → build check). Verified END TO END
(both placed streams within the DERIVED per-block error bounds vs stock GNU
Radio) by ``audio_meter_demo.py`` /
``verification/tests/test_audio_meter_example.py``."""
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

    from audio_meter_demo import KYT_PATH, import_and_pnr

    project, bres, _cat, _ct = import_and_pnr()
    used = sum(c.cell_count for c in bres.chips.values())
    save_project(project, KYT_PATH)
    print(f"wrote {KYT_PATH} ({used}/120 cells, {len(project.blocks)} blocks)")


if __name__ == "__main__":
    main()
