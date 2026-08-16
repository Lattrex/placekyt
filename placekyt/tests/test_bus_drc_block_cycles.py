# SPDX-License-Identifier: GPL-3.0-or-later
"""Bus DRC — waits-for cycles THROUGH placed blocks (INV-32).

A streaming block consumes its input before it can emit its output, so the
deadlock waits-for graph must see straight THROUGH a block: collapse block
cells to one supernode and add the broker DELIVERY edge. Without this, a
route topology whose cycle closes through a block's internals passes the DRC
and ships as a hard runtime Deadlock that ONLY saturated drive exposes — the
audited data_link f2c lockup (sim stop_reason='Deadlock', zero egress) passed
every per-sample gate. These tests pin the strengthened check for the
own-block shape, the cross-block shape, and the single-cell in==out 2-cycle,
plus the clean-chain non-flagging case.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.bus_drc import check_bus  # noqa: E402
from engine.catalog import BlockCatalog  # noqa: E402
from model.connection import BlockEndpoint, ChipPortEndpoint  # noqa: E402
from ui.controller import AppController  # noqa: E402

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402

pytestmark = pytest.mark.skipif(not Path(str(CT_PATH)).exists(),
                                reason="chip yaml absent")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def catalog(qapp):
    return BlockCatalog.from_gr_kyttar()


def _proj(catalog, blocks):
    """A minimal project with single-cell GainBlocks at the given (name, x, y)."""
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("drc_cycles", "kyttar_10x12")
    for name_hint, x, y in blocks:
        ctrl.place_block("GainBlock", 0, x, y, params={"gain": 0.5},
                         library="lattrex.official")
    # place_block auto-names; map by position
    by_pos = {}
    for b in ctrl.project.blocks:
        c = b.placement.cells[0]
        by_pos[(c.x, c.y)] = b.name
    names = {hint: by_pos[(x, y)] for hint, x, y in blocks}
    return ctrl, names


def test_own_block_cycle_is_named(catalog):
    """The data_link f2c shape: G delivers into F via broker (3,6); F's output
    corridor threads BACK THROUGH (3,6). Cycle F -> (4,5) -> (4,6) -> (3,6) -> F."""
    ctrl, n = _proj(catalog, [("G", 7, 5), ("F", 3, 5), ("H", 8, 4)])
    ctrl.add_logical_connection(BlockEndpoint(block=n["G"], port="out"),
                                BlockEndpoint(block=n["F"], port="sample"),
                                name="in_f")
    ctrl.add_logical_connection(BlockEndpoint(block=n["F"], port="out"),
                                BlockEndpoint(block=n["H"], port="sample"),
                                name="out_f")
    routes = {
        "in_f": [(7, 5), (6, 5), (5, 5), (4, 5), (4, 6), (3, 6)],
        "out_f": [(3, 5), (4, 5), (4, 6), (3, 6), (3, 7), (4, 7), (5, 7),
                  (6, 7), (7, 7), (8, 7), (8, 6), (8, 5)],
    }
    viols = check_bus(ctrl.project, routes, {})
    kinds = {v.kind for v in viols}
    assert "deadlock" in kinds, [str(v) for v in viols]
    assert any(n["F"] in str(v) for v in viols if v.kind == "deadlock"), \
        [str(v) for v in viols]
    # WITHOUT the project the cycle is invisible — documents why the callers
    # must thread the project through (the pre-INV-32 blind spot).
    assert not any(v.kind == "deadlock"
                   for v in check_bus(None, routes, {}))


def test_logical_block_loop_is_out_of_scope_documented(catalog):
    """SCOPE PIN: a LOGICAL dataflow loop between two blocks (A -> B and
    B -> A on disjoint corridors) is deliberately NOT flagged by the own-block
    check — a general through-block cycle test cannot distinguish a working
    pipelined ring of independent corridor segments from a true circular wait
    (it false-positived the proven-saturated coherent RX). Circular DATAFLOW
    is a design-level property (GRC graphs are DAGs); the physical
    routing-induced hazard the DRC owns is the own-block delivery cycle."""
    ctrl, n = _proj(catalog, [("A", 1, 1), ("B", 5, 1)])
    ctrl.add_logical_connection(BlockEndpoint(block=n["A"], port="out"),
                                BlockEndpoint(block=n["B"], port="sample"),
                                name="a_to_b")
    ctrl.add_logical_connection(BlockEndpoint(block=n["B"], port="out"),
                                BlockEndpoint(block=n["A"], port="sample"),
                                name="b_to_a")
    routes = {
        "a_to_b": [(1, 1), (2, 1), (3, 1), (4, 1)],
        "b_to_a": [(5, 1), (5, 2), (4, 2), (3, 2), (2, 2), (1, 2)],
    }
    viols = check_bus(ctrl.project, routes, {})
    assert not any(v.kind == "deadlock" for v in viols), [str(v) for v in viols]


def test_single_cell_inout_two_cycle_is_named(catalog):
    """A single-cell block whose input broker cell is ALSO its output's first
    hop: the §5.3 in==out shape as a 2-cycle (S -> broker -> S)."""
    ctrl, n = _proj(catalog, [("G", 4, 5), ("S", 0, 5), ("H", 4, 3)])
    ctrl.add_logical_connection(BlockEndpoint(block=n["G"], port="out"),
                                BlockEndpoint(block=n["S"], port="sample"),
                                name="in_s")
    ctrl.add_logical_connection(BlockEndpoint(block=n["S"], port="out"),
                                BlockEndpoint(block=n["H"], port="sample"),
                                name="out_s")
    routes = {
        "in_s": [(4, 5), (3, 5), (2, 5), (1, 5)],
        "out_s": [(0, 5), (1, 5), (1, 4), (2, 4), (3, 4)],
    }
    viols = check_bus(ctrl.project, routes, {})
    assert any(v.kind == "deadlock" for v in viols), [str(v) for v in viols]


def test_clean_chain_not_flagged(catalog):
    """A plain forward chain (port -> A -> broker -> B -> egress) must stay
    violation-free under the strengthened graph — no false positives."""
    ctrl, n = _proj(catalog, [("A", 1, 1), ("B", 4, 1)])
    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=n["A"], port="sample"),
                                name="in_a")
    ctrl.add_logical_connection(BlockEndpoint(block=n["A"], port="out"),
                                BlockEndpoint(block=n["B"], port="sample"),
                                name="a_to_b")
    ctrl.add_logical_connection(BlockEndpoint(block=n["B"], port="out"),
                                ChipPortEndpoint(chip=0, port="x16_out"),
                                name="out_b")
    routes = {
        "in_a": [(0, 0), (0, 1), (1, 1)],
        "a_to_b": [(1, 1), (2, 1), (3, 1)],
        "out_b": [(4, 1), (5, 1), (5, 0), (6, 0), (7, 0), (8, 0), (9, 0)],
    }
    assert check_bus(ctrl.project, routes, {}) == []
