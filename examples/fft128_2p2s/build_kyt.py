# SPDX-License-Identifier: GPL-3.0-or-later
"""Regenerate ``fft128_2p2s.kyt`` from the real flow — place both dies on
chain A of the 2P2S board, auto-route every net, wire both chains' carrier
links, build, and serialize the placed + routed project.

The ``.kyt`` this writes is what you OPEN IN placeKYT: four dies (the whole
board), the transform placed and routed on chain A, both carrier links wired.
It is produced by the same ``build_2p2s()`` the demo and the gate drive, so
the file can never drift from the design that was verified.

    QT_QPA_PLATFORM=offscreen .venv/bin/python \\
        examples/fft128_2p2s/build_kyt.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fft128_2p2s as D  # noqa: E402


def main():
    from engine.io.project_io import save_project

    ctrl, bres, d0, d1 = D.build_2p2s()
    save_project(ctrl.project, D.KYT_PATH)
    tot = sum(c.cell_count for c in bres.chips.values())
    print(f"wrote {D.KYT_PATH}")
    for cid in sorted(D.CHIP_LABELS):
        role = ("die 0, stage 0" if cid == D.CHIP_DIE0 else
                "die 1, stages 1..6" if cid == D.CHIP_DIE1 else
                "idle (chain B)")
        print(f"  chip {cid} {D.CHIP_LABELS[cid]:22s} "
              f"{bres.chips[cid].cell_count:3d} cells   {role}")
    print(f"  {tot} cells over the board's four dies, "
          f"{len(ctrl.project.blocks)} blocks")
    print(f"  carrier links: chip0.x16_out -> chip1.x16_in (chain A), "
          f"chip2.x16_out -> chip3.x16_in (chain B)")


if __name__ == "__main__":
    main()
