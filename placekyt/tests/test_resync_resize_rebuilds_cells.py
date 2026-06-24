# SPDX-License-Identifier: GPL-3.0-or-later
"""A resync that RESIZES a block rebuilds its cells to the new size.

Bug: changing a FIR from 40 → 8 taps in GRC and resyncing kept the OLD footprint
(8 cells from the 40-tap layout) instead of rebuilding to the 8-tap size (2
cells), and stranded a fly line over the leftover cells. Fix: the resize branch
of resync regenerates each resized block's placement cells from its new params
(default_cells) before re-placing/re-routing — so the cell count matches the new
size and no stale cells linger.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from engine.io.chip_type_io import load_chip_type
from ui.controller import AppController
from model.connection import ChipPortEndpoint, BlockEndpoint
import engine.preferences as prefs

from tests.conftest import CHIP_YAML as CT_PATH

pytestmark = pytest.mark.skipif(not CT_PATH.exists(), reason="chip yaml absent")


def _gain_fir_chain(taps):
    ct = load_chip_type(str(CT_PATH))
    ctrl = AppController()
    ctrl.new_project("d", "kyttar_10x12")
    g = ctrl.place_block("GainBlock", 0, 1, 1, library="lattrex.official",
                         params={"gain": 0.5})
    g = g if isinstance(g, str) else g.name
    f = ctrl.place_block("FIRFilterBlock", 0, 4, 1, library="lattrex.official",
                         params={"coefficients": [0.4 / taps] * taps})
    f = f if isinstance(f, str) else f.name
    ctrl.add_logical_connection(ChipPortEndpoint(0, "x16_in"),
                                BlockEndpoint(g, "sample"))
    ctrl.add_logical_connection(BlockEndpoint(g, "out"), BlockEndpoint(f, "sample"))
    ctrl.add_logical_connection(BlockEndpoint(f, "out"),
                                ChipPortEndpoint(0, "x16_out"))
    ctrl.auto_place()
    ctrl.auto_route_all(use_bus=True)
    return ctrl, g, f, ct


def test_resync_shrink_rebuilds_fir_cell_count():
    ctrl, g, f, ct = _gain_fir_chain(40)
    big = len(ctrl.project.block(f).placement.cells)
    assert big > 2, "a 40-tap FIR should be multi-cell to start"

    # GRC changed 40 → 8 taps; resync.
    ctrl.grc_sync.observe(
        f, {"coefficients": "[" + ",".join(["0.125"] * 8) + "]"})
    diffs = ctrl.refresh_grc_sync()
    assert diffs["firfilter"].resizes, "a tap-count change is a resize"
    ctrl.resync_from_grc(mode=prefs.GRC_AUTO, chip_types={"kyttar_10x12": ct})

    small = len(ctrl.project.block(f).placement.cells)
    import math
    expected = math.ceil(8 / __import__(
        "gr_kyttar.placement.blocks.fir_filter_block",
        fromlist=["FIRFilterBlock"]).FIRFilterBlock.TAPS_PER_CELL)
    assert small == expected, (
        f"resync to 8 taps must rebuild to {expected} cells, got {small} "
        f"(stale {big}-tap footprint left behind)")

    # The only unrouted net may be the chip-input direct injection (unrouted by
    # design); the block-to-block and block-to-output nets must be routed.
    for c in ctrl.project.connections:
        if isinstance(c.source, ChipPortEndpoint) and c.source.port.endswith("_in"):
            continue
        assert c.is_routed, f"net {c.name} must be routed after a resize resync"
