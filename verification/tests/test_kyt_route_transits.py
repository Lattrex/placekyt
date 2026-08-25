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
#
# gain_2p2s WAS listed here, on the belief that its tagged egress bus ran
# THROUGH gain_3 at (1,0) by design. It does not, and never did. The finding
# was an artifact of this file's own chip-BLIND ownership map: it reported
# "gain_to_x16_out transits gain_3 at (1,0)" for a route on chip 0 against a
# block on chip 3 — three such findings, every one across dies. Each chip's
# egress route only ever crosses its OWN gain, which is its endpoint and
# properly excluded. With ownership keyed on (chip, x, y) the example is clean
# and is gated normally. A quarantine entry is only worth what its evidence is
# worth; this one was documenting a bug in the checker.
_KNOWN_OPEN: dict[str, str] = {}


def _rel(p: Path) -> str:
    return p.parent.name + "/" + p.name


@pytest.mark.parametrize("kyt", EXAMPLE_KYTS, ids=_rel)
def test_no_route_transits_dsp_block_cells(kyt):
    from engine.io.project_io import load_project
    from model.connection import BlockEndpoint

    if _rel(kyt) in _KNOWN_OPEN:
        pytest.xfail(_KNOWN_OPEN[_rel(kyt)])

    project = load_project(str(kyt))
    # Ownership is per (CHIP, x, y) — a cell coordinate means nothing without
    # its chip. Keyed on (x, y) alone this collapses every chip onto one grid,
    # so in a MULTI-CHIP design a route on chip 0 "transits" a block that is
    # actually on chip 1 at the same coordinate. That false positive is
    # invisible to every single-chip example and fires on the first multi-chip
    # one with blocks on more than one die (fft128_2die).
    cell_owner = {}
    for b in project.blocks:
        if b.placement:
            for c in b.placement.cells:
                cell_owner[(b.placement.chip, c.x, c.y)] = b
    findings = []
    for conn in project.connections:
        if not isinstance(conn.route, list):
            continue
        ends = {ep.block for ep in (conn.source, conn.target)
                if isinstance(ep, BlockEndpoint)}
        for chip in _chips_of(project, conn):
            for p in conn.route:
                owner = cell_owner.get((chip, p.x, p.y))
                if owner is None or owner.name in ends:
                    continue
                if owner.type == "CrossoverBlock":
                    continue      # the crossing primitive — the one exception
                findings.append(
                    f"{conn.name} transits {owner.name} ({owner.type}) "
                    f"on chip {chip} at ({p.x},{p.y})")
    assert not findings, "\n".join(findings)


def _chips_of(project, conn):
    """EVERY chip this connection's route is laid on, as a set.

    Usually one. But a CROSS-CHIP logical net — a chip-0 input port feeding a
    block placed on chip 1, which is how 2P2S multiplexes a far die — has its
    endpoints on DIFFERENT dies, and its waypoints are meaningful on both: the
    word transits the head chip's bus AND lands on the far chip. So the route
    is checked against each endpoint's chip, not one guessed chip.

    Getting this wrong in EITHER direction loses the gate. Keyed on (x, y)
    with no chip at all, a route on chip 0 falsely 'transits' a block sitting
    at the same coordinate on chip 1 (which is what a two-die design with
    blocks on both dies exposes). Keyed on a single guessed chip, the genuine
    2P2S cross-chip transit stops being reported at all."""
    from model.connection import BlockEndpoint, ChipPortEndpoint
    chips = set()
    for ep in (conn.source, conn.target):
        if isinstance(ep, ChipPortEndpoint):
            chips.add(ep.chip)
        elif isinstance(ep, BlockEndpoint):
            b = project.block(ep.block)
            if b is not None and b.placement is not None:
                chips.add(b.placement.chip)
    return chips or {0}


def test_the_transit_check_is_chip_aware_in_both_directions():
    """The check must be chip-aware WITHOUT losing its teeth.

    Two failure modes, opposite in sign, both silent:
      * chip-BLIND — every die collapses onto one grid, so a route on chip 0
        falsely 'transits' a block at the same coordinate on chip 1. Only a
        design with blocks on MORE THAN ONE die exposes it.
      * single-chip-GUESSED — a cross-chip logical net (a head chip's port
        feeding a block on a far die) is checked against one endpoint's chip
        only, and a genuine transit on the other stops being reported.

    This asserts the repo still contains an example of each shape, so neither
    mistake can be made again without a test going red."""
    from engine.io.project_io import load_project
    from model.connection import BlockEndpoint, ChipPortEndpoint

    multi_die = cross_chip = None
    for k in EXAMPLE_KYTS:
        proj = load_project(str(k))
        placed = {b.placement.chip for b in proj.blocks if b.placement}
        if len(placed) > 1:
            multi_die = multi_die or _rel(k)
        for conn in proj.connections:
            if len(_chips_of(proj, conn)) > 1:
                cross_chip = cross_chip or _rel(k)
    assert multi_die, (
        "no example places blocks on more than one die — the chip-BLIND "
        "false positive this guards would be undetectable")
    assert cross_chip, (
        "no example has a net whose endpoints are on different dies — the "
        "single-chip-GUESSED false negative this guards would be undetectable")
