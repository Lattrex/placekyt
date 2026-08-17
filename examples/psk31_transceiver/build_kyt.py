# SPDX-License-Identifier: GPL-3.0-or-later
"""Regenerate ``psk31_transceiver.kyt`` from ``psk31_transceiver.grc`` via the
real auto flow (import → DUPLEX shared-panel auto place-and-route → build
check). Verified END TO END by ``psk31_transceiver_demo.py`` /
``verification/tests/test_psk31_transceiver_example.py``."""
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

    from psk31_transceiver_demo import KYT_PATH, import_and_pnr

    project, bres, _cat, _ct = import_and_pnr()
    used = sum(c.cell_count for c in bres.chips.values())
    save_project(project, KYT_PATH)
    print(f"wrote {KYT_PATH} ({used}/120 cells, {len(project.blocks)} blocks, "
          f"panel {len(project.panels[0].image)} words)")


if __name__ == "__main__":
    main()
