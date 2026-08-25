# SPDX-License-Identifier: GPL-3.0-or-later
"""Regenerate ``fft_spectrum.kyt`` — hand-place the chip-scale FFT64 + the
one-cell power stage, AUTO-ROUTE every corridor, build, and save.

The placement is pinned rather than auto-packed because ``FFT64Block`` is a
CHIP-SCALE block whose verified layout is a 12-row ctl/out spine; the generic
packer does not model that class and shifts the spine off the array. See
``fft_spectrum_demo.py`` for the full reasoning. Everything downstream of the
two anchors — the corridors, the brokers, the DRC, the bitstream — is the real
engine, and the whole chain is verified END TO END (bit-exact power stream, the
tone in its true bin after un-reversal) by ``fft_spectrum_demo.py`` /
``verification/tests/test_fft_spectrum_example.py``.
"""
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

    from fft_spectrum_demo import (KYT32_PATH, KYT_PATH, SIZES, build_chain)

    for n, path in ((64, KYT_PATH), (32, KYT32_PATH)):
        ctrl, bres, _cat, _ct = build_chain(n)
        used = sum(c.cell_count for c in bres.chips.values())
        save_project(ctrl.project, str(path))
        print(f"wrote {path} (N={n}, {SIZES[n][0]}, {used}/120 cells, "
              f"{len(ctrl.project.blocks)} blocks)")


if __name__ == "__main__":
    main()
