# SPDX-License-Identifier: GPL-3.0-or-later
"""No shipped .kyt may route a corridor THROUGH a DSP block's cells.

User-reported (2026-08-10, twice): "cells are routing through each other …
I still see routing through the blocks". The duplex panel templates used to
weave corridors through client cells (the RX tap/tailxo relays sat ON the TX
feed corridor; the RX emit sat ON the x1_in return corridor). The redesign
replaced every pure tap/delivery relay with a standard build BROKER (a plain
routing cell corridor words transit at HOP<31) and moved the RX emit off the
return corridor behind a fork broker — this gate keeps it that way.

The ONE permitted exception is a CrossoverBlock cell: a genuine corridor
CROSSING must share a cell (each cell has ONE fwd_face — two corridors in two
directions can only meet in a cell programmed to steer both), and the
crossover IS that primitive. Every remaining transit must be one.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT / "placekyt"), str(_ROOT / "runtime" / "python")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

EXAMPLE_KYTS = sorted((_ROOT / "examples").glob("*/*.kyt"))
assert EXAMPLE_KYTS, "no example .kyt files found"

# Tracked open debt (a VISIBLE xfail, not a silent pass). Remove as fixed.
_KNOWN_OPEN = {
    "gain_2p2s/gain_2p2s.kyt":
        "the hand-placed 2P2S multichip layout runs its tagged egress bus "
        "THROUGH gain_3 at (1,0) by original design — the multichip hop "
        "arithmetic (inter-chip-hop, output-cross-chip) was proven ON this "
        "exact geometry; reworking it to broker form needs the multichip "
        "harness re-proof (MultiChipSimEngine), not a drive-by edit.",
}


def _rel(p: Path) -> str:
    return p.parent.name + "/" + p.name


@pytest.mark.parametrize("kyt", EXAMPLE_KYTS, ids=_rel)
def test_no_route_transits_dsp_block_cells(kyt):
    from engine.io.project_io import load_project
    from model.connection import BlockEndpoint

    if _rel(kyt) in _KNOWN_OPEN:
        pytest.xfail(_KNOWN_OPEN[_rel(kyt)])

    project = load_project(str(kyt))
    cell_owner = {}
    for b in project.blocks:
        if b.placement:
            for c in b.placement.cells:
                cell_owner[(c.x, c.y)] = b
    findings = []
    for conn in project.connections:
        if not isinstance(conn.route, list):
            continue
        ends = {ep.block for ep in (conn.source, conn.target)
                if isinstance(ep, BlockEndpoint)}
        for p in conn.route:
            owner = cell_owner.get((p.x, p.y))
            if owner is None or owner.name in ends:
                continue
            if owner.type == "CrossoverBlock":
                continue          # the crossing primitive — the one exception
            findings.append(
                f"{conn.name} transits {owner.name} ({owner.type}) "
                f"at ({p.x},{p.y})")
    assert not findings, "\n".join(findings)
