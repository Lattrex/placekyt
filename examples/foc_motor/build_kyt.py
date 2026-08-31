# SPDX-License-Identifier: GPL-3.0-or-later
"""Regenerate ``foc_motor.kyt`` from the authored anchors — and REVERIFY on the
real simulator before saving.

The .kyt is only written after the built chip has produced a BIT-EXACT duty
packet against the host golden. A failing run raises and leaves the shipped
file untouched.

Run::

    QT_QPA_PLATFORM=offscreen .venv/bin/python examples/foc_motor/build_kyt.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from foc_motor_demo import (  # noqa: E402
    KYT_PATH, FocChain, arm_landings, chip_for, golden, place_route_build)


def main():
    print("placing + routing + building the FOC current loop...")
    project, bres, cat, ct = place_route_build()
    lands = arm_landings(bres)
    used = sum(c.cell_count for c in bres.chips.values())
    print(f"  {used} cells of 120; arm landings {lands}")

    print("reverifying on the real simulator...")
    e_d, e_q, theta = 1000, 2000, 0x1234
    want = golden([e_d], [e_q], [theta])
    chain = FocChain(bres, chip_for(bres, lands), lands)
    chain.iteration(e_d, e_q, theta)
    got = chain.words
    print(f"  chip   : {[hex(w) for w in got]}")
    print(f"  golden : {[hex(w) for w in want]}")
    if got != want:
        raise SystemExit("verification FAILED — .kyt NOT saved")
    if set(chain.stops) != {"QueueEmpty"}:
        raise SystemExit(f"stop_reasons not all QueueEmpty: {chain.stops} "
                         f"— .kyt NOT saved")
    print("  EXACT, and every run settled QueueEmpty.")

    from engine.io.project_io import save_project
    save_project(project, KYT_PATH)
    print(f"wrote {KYT_PATH}")


if __name__ == "__main__":
    main()
