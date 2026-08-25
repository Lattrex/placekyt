# SPDX-License-Identifier: GPL-3.0-or-later
"""Regenerate ``fft128_2die.kyt`` from the real flow — place both dies at
their declared anchors, auto-route every net, wire the crossing, build, and
serialize the placed + routed project.

The ``.kyt`` this writes is what you OPEN IN placeKYT: two chips, both dies
placed and routed, the crossing wired. It is produced by the same
``build_two_die()`` the demo and the gate drive, so the file can never drift
from the design that was verified.

    QT_QPA_PLATFORM=offscreen .venv/bin/python \\
        examples/fft128_2die/build_kyt.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fft128_2die as D  # noqa: E402


def main():
    from engine.io.project_io import save_project

    ctrl, bres, d0, d1 = D.build_two_die()
    save_project(ctrl.project, D.KYT_PATH)
    tot = sum(c.cell_count for c in bres.chips.values())
    print(f"wrote {D.KYT_PATH}")
    print(f"  chip 0 (die 0, stage 0)     {bres.chips[0].cell_count:3d} cells")
    print(f"  chip 1 (die 1, stages 1..6) {bres.chips[1].cell_count:3d} cells")
    print(f"  {tot} cells over two chips, {len(ctrl.project.blocks)} blocks, "
          f"crossing wired chip0.x16_out -> chip1.x16_in")


if __name__ == "__main__":
    main()
