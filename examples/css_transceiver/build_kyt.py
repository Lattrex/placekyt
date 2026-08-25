# SPDX-License-Identifier: GPL-3.0-or-later
"""Regenerate ``css_transceiver.kyt`` from ``css_transceiver.grc``.

The TOPOLOGY comes from the ``.grc`` (import gives the nets, the stream_id /
out_tag duplex plumbing, and the synthesized complex sibling rails — yq/xq and
out_q/im — that the flowgraph draws as single complex wires). The GEOMETRY is
then PINNED to the proven hand layout before routing.

WHY THE GEOMETRY IS PINNED (measured, not assumed). A generic auto-place of
this design rotates the 44-cell FFT16 (and the mixer) CCW and packs the chain
into the top nine rows. That layout routes and builds "ok" and then does not
work: driven with the shipped burst it emits 6 words instead of 50, and their
values are outside BinArgmax(16)'s legal 0..15 range. Isolated further: the
proven geometry with FFT16 alone rotated CCW does not even complete a run
(0 words out) — a composition-level deadlock, NOT a block defect (FFT16 passes
``test_orientation_invariance.py`` in all 8 D4 orientations standalone). The
pinned layout below is the one the CSS receive-spine system gate measured
(``verification/tests/test_css_rx_system.py``) and is bit-exact end to end.

This is the same "hand-placed .kyt" convention the FSK4 and 16-QAM modem
examples ship for designs the generic placer cannot lay out. The shipped
``.kyt`` is the artifact a user opens; see the README's Run section.
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

    from css_transceiver_demo import KYT_PATH, import_and_pnr

    project, bres, _cat, _ctrl = import_and_pnr()
    used = sum(c.cell_count for c in bres.chips.values())
    save_project(project, KYT_PATH)
    print(f"wrote {KYT_PATH} ({used}/120 cells, {len(project.blocks)} blocks)")


if __name__ == "__main__":
    main()
