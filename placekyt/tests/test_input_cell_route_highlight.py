# SPDX-License-Identifier: GPL-3.0-or-later
"""Selecting a block's INPUT cell highlights its INCOMING net (not just output).

Bug: route-highlight-on-select matched a connection only by its route ENDPOINT
waypoints, and skipped unrouted nets. For a multi-cell block (FIR) the incoming
net's route didn't always land on the input cell, and a chip-input direct-
injection net is unrouted — so selecting the OUTPUT cell highlighted the outgoing
net but selecting the INPUT cell highlighted nothing. Fix: also match a
connection whose block I/O ENDPOINT resolves (via the port-cell provider) to the
selected cell, for routed and unrouted nets alike.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from engine.io.chip_type_io import load_chip_type
from engine.route_analysis import connections_terminating_at_cell
from ui.controller import AppController
from model.connection import ChipPortEndpoint, BlockEndpoint

from tests.conftest import CHIP_YAML as CT_PATH

pytestmark = pytest.mark.skipif(not CT_PATH.exists(), reason="chip yaml absent")


def _resolver(ctrl):
    def f(block_type, library, params=None):
        pm = ctrl.catalog.port_map(block_type, params, library=library)
        return {p.name: (p.cell_id, p.direction) for p in pm.ports}
    return f


def test_input_cell_highlights_incoming_net_multicell():
    ct = load_chip_type(str(CT_PATH))
    ctrl = AppController()
    ctrl.new_project("d", "kyttar_10x12")
    g = ctrl.place_block("GainBlock", 0, 0, 0, library="lattrex.official",
                         params={"gain": 0.5})
    g = g if isinstance(g, str) else g.name
    # 20-tap FIR → multi-cell, input and output on DIFFERENT cells.
    f = ctrl.place_block("FIRFilterBlock", 0, 3, 0, library="lattrex.official",
                         params={"coefficients": [0.05] * 20})
    f = f if isinstance(f, str) else f.name
    ctrl.add_logical_connection(ChipPortEndpoint(0, "x16_in"),
                                BlockEndpoint(g, "sample"))
    ctrl.add_logical_connection(BlockEndpoint(g, "out"), BlockEndpoint(f, "sample"))
    ctrl.add_logical_connection(BlockEndpoint(f, "out"),
                                ChipPortEndpoint(0, "x16_out"))
    ctrl.auto_route_all({"kyttar_10x12": ct})

    pm = ctrl.catalog.port_map("FIRFilterBlock", {"coefficients": [0.05] * 20},
                               library="lattrex.official")
    fpl = ctrl.project.block(f).placement
    in_cid = next(p.cell_id for p in pm.ports if p.direction == "in")
    out_cid = next(p.cell_id for p in pm.ports if p.direction == "out")
    in_cell = fpl.cell(in_cid)
    out_cell = fpl.cell(out_cid)
    assert (in_cell.x, in_cell.y) != (out_cell.x, out_cell.y), \
        "test needs a block whose input and output are on different cells"

    res = _resolver(ctrl)
    # Selecting the INPUT cell highlights the INCOMING net (gain → FIR).
    incoming = connections_terminating_at_cell(
        ctrl.project, 0, in_cell.x, in_cell.y, port_cell_resolver=res)
    assert any("gain" in n and "firfilter" in n for n in incoming), \
        f"input cell must highlight the incoming net; got {incoming}"
    # And the OUTPUT cell still highlights the outgoing net.
    outgoing = connections_terminating_at_cell(
        ctrl.project, 0, out_cell.x, out_cell.y, port_cell_resolver=res)
    assert any("x16_out" in n for n in outgoing), \
        f"output cell must highlight the outgoing net; got {outgoing}"


def test_input_cell_highlights_unrouted_chip_input_net():
    """The chip-input → block net is a direct injection (unrouted). Selecting the
    block's input cell must still highlight it."""
    ct = load_chip_type(str(CT_PATH))
    ctrl = AppController()
    ctrl.new_project("d", "kyttar_10x12")
    g = ctrl.place_block("GainBlock", 0, 0, 0, library="lattrex.official",
                         params={"gain": 0.5})
    g = g if isinstance(g, str) else g.name
    ctrl.add_logical_connection(ChipPortEndpoint(0, "x16_in"),
                                BlockEndpoint(g, "sample"))
    ctrl.add_logical_connection(BlockEndpoint(g, "out"),
                                ChipPortEndpoint(0, "x16_out"))
    ctrl.auto_route_all({"kyttar_10x12": ct})

    gpl = ctrl.project.block(g).placement
    in_cell = gpl.cells[0]
    res = _resolver(ctrl)
    names = connections_terminating_at_cell(
        ctrl.project, 0, in_cell.x, in_cell.y, port_cell_resolver=res)
    assert any("x16_in" in n for n in names), \
        f"input cell must highlight the (unrouted) chip-input net; got {names}"
