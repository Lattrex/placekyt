# SPDX-License-Identifier: GPL-3.0-or-later
"""2P2S plumbing proof: two PARALLEL daisy-chains of two chips each.

The first test of placeKYT driving more than one daisy-chain at once — the
foundation for mapping designs onto the 4-chip 2P2S dev board (two parallel
chains, each chip0->chip1, the FPGA selecting chains + merging outputs).

Topology (matches resources/boards/dev2p2s.kdb):

    chain A:  inject chip0.x16_in -> [gain] -> x16_out --wire--> chip1.x16_in -> [gain] -> capture chip1.x16_out
    chain B:  inject chip2.x16_in -> [gain] -> x16_out --wire--> chip3.x16_in -> [gain] -> capture chip3.x16_out

Two 0.5x gains in series per chain => 0.25x at each tail. Both chains run at once
with DIFFERENT stimulus and must NOT interfere — that proves parallel + cross-chip
multiplexing together.

KNOWN CONSTRAINT (documented, not a bug in this test): multichip injection uses
``write_port_i16``, which drives a head block only when it sits AT the input
landing cell. So each chain's HEAD gain is placed at (0,0). Driving a ROUTED head
input (a gain tapped off a through-bus) needs the inject_data/jump primitive added
to the MultiChipSimulation binding — a separate, scoped task. This test proves the
PLUMBING (parallel chains + cross-chip relay + independent per-chain I/O) with an
at-landing head, isolating that primitive.

Run:
    QT_QPA_PLATFORM=offscreen placekyt/.venv/bin/python -m pytest \
        placekyt/tests/test_2p2s_plumbing.py -q
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.catalog import BlockCatalog  # noqa: E402
from ui.controller import AppController  # noqa: E402
from model.connection import BlockEndpoint, ChipPortEndpoint  # noqa: E402

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402

_BOARD = (Path(__file__).resolve().parents[1] / "resources" / "boards"
          / "dev2p2s.kdb")

pytestmark = pytest.mark.skipif(not CT_PATH.exists(), reason="chip yaml absent")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def catalog():
    return BlockCatalog.from_gr_kyttar()


def _build_2p2s(catalog):
    """4 chips, 2 chains. Head gain AT (0,0) per chip; output routed to x16_out."""
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("2p2s", "kyttar_10x12")
    for _ in range(3):
        ctrl.add_chip()                      # chips 1, 2, 3 (chip 0 exists)
    for chip in range(4):
        ctrl.place_block("GainBlock", chip, 0, 0, library="lattrex.official")
        gn = [b.name for b in ctrl.project.blocks
              if b.placement and b.placement.chip == chip][-1]
        ctrl.add_route(BlockEndpoint(gn, "out"),
                       ChipPortEndpoint(chip, "x16_out"),
                       [(x, 0) for x in range(10)])
    ctrl.add_inter_chip(0, "x16_out", 1, "x16_in")   # chain A series link
    ctrl.add_inter_chip(2, "x16_out", 3, "x16_in")   # chain B series link
    return ctrl


def test_board_loads_and_matches_topology():
    from engine.io.board_io import load_board
    b = load_board(str(_BOARD))
    assert len(b.chips) == 4
    # both series links present, cross-chain absent
    assert b.has_chip_connection(0, "x16_out", 1, "x16_in")
    assert b.has_chip_connection(2, "x16_out", 3, "x16_in")
    assert not b.has_chip_connection(0, "x16_out", 3, "x16_in")


def test_2p2s_drc_clean_against_board(qapp, catalog):
    from engine.io.board_io import load_board
    from engine.drc import check_project
    ctrl = _build_2p2s(catalog)
    board = load_board(str(_BOARD))
    drc = check_project(ctrl.project, ctrl.chip_types(), board,
                        catalog=ctrl.catalog)
    assert drc.ok, [getattr(e, "category", None) for e in drc.errors]


def test_two_parallel_chains_relay_independently(qapp, catalog):
    """Both chains run at once with DIFFERENT stimulus; each tail = 0.25x of its
    OWN chain's input, with no cross-chain interference."""
    from engine.simulator import MultiChipSimEngine

    ctrl = _build_2p2s(catalog)
    r = ctrl.build()
    assert r.ok, [getattr(e, "category", None) for e in r.errors]

    ct = str(ctrl.registry.require("kyttar_10x12").path)
    eng = MultiChipSimEngine({0: ct, 1: ct, 2: ct, 3: ct})
    eng.connect(0, "x16_out", 1, "x16_in")   # chain A
    eng.connect(2, "x16_out", 3, "x16_in")   # chain B
    e, ir = catalog.resolved_io("GainBlock")
    for cid in range(4):
        eng.load(cid, r.words(cid), trace=True)
        eng.configure_input_port(cid, "x16_in", entry_addr=e, hop_count=30,
                                 data_addr=ir[0])

    eng.inject(0, "x16_in", [0x4000, 0x2000])   # chain A stimulus
    eng.inject(2, "x16_in", [0x6000, 0x1000])   # chain B stimulus (distinct)
    eng.run_until_output(1, "x16_out", 2, None, 4000)
    eng.run_until_output(3, "x16_out", 2, None, 4000)

    out_a = eng.capture(1, "x16_out")
    out_b = eng.capture(3, "x16_out")
    # 0.25x of each chain's OWN input — proves parallel + cross-chip, no crosstalk.
    assert out_a[:2] == [0x1000, 0x0800], out_a
    assert out_b[:2] == [0x1800, 0x0400], out_b
    # cell-state overlay spans all four chips
    states = eng.cell_states()
    assert {k[0] for k in states} == {0, 1, 2, 3}
