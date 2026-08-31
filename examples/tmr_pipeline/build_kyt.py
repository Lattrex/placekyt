# SPDX-License-Identifier: GPL-3.0-or-later
"""Regenerate tmr_pipeline.kyt from tmr_pipeline.grc.

import -> HAND-place (``tmr_pipeline_demo.PLACEMENT``) -> route -> build ->
verify the whole 256-byte ramp through BOTH streams on a throwaway chip ->
save. The example is hand-placed like the modem examples: the voter's
rendezvous cell needs its three redundant arms delivered on three DISTINCT
faces (west/north/south) with the block's own fold running east, a geometry
the generic auto-P&R pack does not produce. The layout is only saved after
the full-ramp run has matched the pinned goldens word for word — a layout
that routes but mis-delivers an arm is never shipped.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tmr_pipeline_demo import (  # noqa: E402
    KYT_PATH, RAMP, place_route_build, run_streams, solo_golden, tmr_golden)


def main():
    print("import + hand-place + route + build ...")
    project, bres, cat, ct = place_route_build()
    inj_b = next(b for b in project.blocks
                 if b.type == "AddConstBlock" and b.params.get("const", 0))
    f_lsbs = int(round(float(inj_b.params["const"]) * 32768.0))
    print(f"verify the full ramp (path-B fault = {f_lsbs} LSB) ...")
    tmr, solo, reasons = run_streams(project, bres, RAMP)
    ok = (tmr == tmr_golden(RAMP, f_lsbs)
          and solo == solo_golden(RAMP)
          and set(reasons) == {"QueueEmpty"})
    print(f"   tmr={len(tmr) // 2}/{len(RAMP)} packets, "
          f"solo={len(solo)}/{len(RAMP)}, "
          f"settle reasons={sorted(set(map(str, reasons)))} "
          f"-> {'OK' if ok else 'MISMATCH'}")
    if not ok:
        raise SystemExit("verification failed — .kyt NOT saved")
    from engine.io.project_io import save_project
    save_project(project, KYT_PATH)
    used = sum(c.cell_count for c in bres.chips.values())
    print(f"saved {KYT_PATH} — {used}/120 cells, {len(project.blocks)} blocks")


if __name__ == "__main__":
    main()
