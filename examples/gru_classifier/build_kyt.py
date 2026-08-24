# SPDX-License-Identifier: GPL-3.0-or-later
"""Regenerate ``gru_classifier.kyt`` — the placed, routed, built classifier.

Unlike the transceiver examples, this design is HAND-PLACED rather than
imported-and-auto-placed. That is deliberate and measured: the chain fills 102
of the array's 120 cells, and a 400-layout random search over the free band
found exactly ONE arrangement that both routes and builds. The generic
auto-placer does not find it, so the anchors are pinned in
``gru_classifier.BEST_KNOWN_ANCHORS`` and this script writes that design out.

Verified END TO END (the shipped stimulus classified on the real placed +
routed chip) by ``gru_classifier_demo.py`` and
``verification/tests/test_gru_classifier_example.py``.
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


def main() -> int:
    from engine.io.project_io import save_project

    from gru_classifier import KYT, build_chain

    ctrl, bres, _ids, bad = build_chain()
    if bad:
        raise SystemExit(f"the chain did not place/route/build: {bad}")
    used = sum(c.cell_count for c in bres.chips.values())
    save_project(ctrl.project, str(KYT))
    print(f"wrote {KYT} ({used}/120 cells, {len(ctrl.project.blocks)} blocks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
