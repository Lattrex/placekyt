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

    # Reverify STREAMING, not a single shot: consecutive iterations with
    # DIFFERENT inputs. A one-iteration check cannot see a rendezvous whose
    # release re-admits the wrong arm, which is correct for iteration 0 and
    # wedges from iteration 1. The golden is one call over the whole sequence
    # because the PI integrators evolve across samples.
    print("reverifying on the real simulator (streaming)...")
    e_d = [1000, 0x0333, -1500 & 0xFFFF, 300, 0x0700, -200 & 0xFFFF]
    e_q = [2000, 0x1500, 900, -2200 & 0xFFFF, 0x0123, 1750]
    theta = [0x1234, 0x4000, 0x8000, 0xC000, 0x2468, 0x9ABC]
    want = golden(e_d, e_q, theta)
    chain = FocChain(bres, chip_for(bres, lands), lands)
    for i in range(len(e_d)):
        chain.iteration(e_d[i], e_q[i], theta[i])
    got = chain.words
    for i in range(len(e_d)):
        g, w = got[3 * i:3 * i + 3], want[3 * i:3 * i + 3]
        print(f"  iter {i}: {[hex(v) for v in g]} "
              f"{'==' if g == w else '!='} {[hex(v) for v in w]}")
    if got != want:
        raise SystemExit("verification FAILED — .kyt NOT saved")
    if set(chain.stops) != {"QueueEmpty"}:
        raise SystemExit(f"stop_reasons not all QueueEmpty: {chain.stops} "
                         f"— .kyt NOT saved")
    print(f"  EXACT over {len(e_d)} iterations, every run QueueEmpty.")

    from engine.io.project_io import save_project
    save_project(project, KYT_PATH)
    print(f"wrote {KYT_PATH}")


if __name__ == "__main__":
    main()
