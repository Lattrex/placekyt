# SPDX-License-Identifier: GPL-3.0-or-later
"""Regenerate lz4_stream.kyt from lz4_stream.grc.

import -> HAND-place (``lz4_stream_demo.apply_hand_pnr``) -> build -> verify
the FULL 1 KB round trip on a throwaway chip -> save. The example is
hand-placed: two SRAM-panel-backed blocks (the design limit) share the chip's
single x1 port pair, a geometry no auto-P&R panel template produces — the
shared x1_in / x16_in corridors fork at CrossoverBlocks and the two
controllers' to-panel corridors merge same-direction into the port. The
layout is only saved after the round trip has matched the pinned goldens byte
for byte.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lz4_stream_demo import (  # noqa: E402
    KYT_PATH, PAYLOAD, goldens, import_and_pnr, run_roundtrip)


def main():
    print("import + hand-place + build ...")
    project, bres, cat, ct, tags = import_and_pnr()
    print(f"verify the full 1 KB round trip (tags {tags}) ...")
    cmp_bytes, dec_bytes, info = run_roundtrip(project, bres, PAYLOAD)
    exp_cmp, _rep, _rnd = goldens()
    ok = (cmp_bytes == exp_cmp and dec_bytes == PAYLOAD
          and "Deadlock" not in info["stops"])
    print(f"   {len(cmp_bytes)} compressed (model-exact: "
          f"{cmp_bytes == exp_cmp}), {len(dec_bytes)} recovered "
          f"(exact: {dec_bytes == PAYLOAD}), stops {sorted(info['stops'])} "
          f"-> {'OK' if ok else 'MISMATCH'}")
    if not ok:
        raise SystemExit("verification failed — .kyt NOT saved")
    from engine.io.project_io import save_project
    save_project(project, KYT_PATH)
    used = sum(c.cell_count for c in bres.chips.values())
    print(f"saved {KYT_PATH} — {used}/120 cells, {len(project.blocks)} blocks, "
          f"panel {project.panels[0].size_words} words")


if __name__ == "__main__":
    main()
