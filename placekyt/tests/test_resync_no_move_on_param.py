# SPDX-License-Identifier: GPL-3.0-or-later
"""A GRC resync that doesn't RESIZE a block must not move/rotate anything.

Bug: changing only a block's VALUE param in GRC (e.g. a gain) and resyncing
re-flowed the WHOLE chip (auto_place + auto_route_all), dumping unrelated blocks
(the FIR) into new, rotated, disconnected positions. Fix: re-place + re-route only
when an affected block actually changes footprint (cell_count); a non-resizing
param change applies the params in place and leaves every placement and
connection untouched.
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


def _chain():
    ct = load_chip_type(str(CT_PATH))
    ctrl = AppController()
    ctrl.new_project("d", "kyttar_10x12")
    g = ctrl.place_block("GainBlock", 0, 0, 0, library="lattrex.official",
                         params={"gain": 0.5})
    g = g if isinstance(g, str) else g.name
    # FIR at a non-default spot the auto-placer WOULD move if it re-flowed.
    f = ctrl.place_block("FIRFilterBlock", 0, 5, 3, library="lattrex.official",
                         params={"coefficients": [0.2, 0.2, 0.2, 0.2, 0.2]})
    f = f if isinstance(f, str) else f.name
    ctrl.add_logical_connection(ChipPortEndpoint(0, "x16_in"),
                                BlockEndpoint(g, "sample"))
    ctrl.add_logical_connection(BlockEndpoint(g, "out"),
                                BlockEndpoint(f, "sample"))
    ctrl.add_logical_connection(BlockEndpoint(f, "out"),
                                ChipPortEndpoint(0, "x16_out"))
    ctrl.auto_route_all({"kyttar_10x12": ct})
    return ctrl, g, f, ct


def test_gain_value_resync_does_not_move_unrelated_block():
    ctrl, g, f, ct = _chain()
    fir_before = tuple((c.x, c.y, str(c.face))
                       for c in ctrl.project.block(f).placement.cells)
    n_conn_before = len(ctrl.project.connections)

    # GRC changed ONLY the gain value (no resize anywhere).
    ctrl.grc_sync.observe(g, {"gain": 0.8})
    diffs = ctrl.refresh_grc_sync()
    assert diffs and not any(d.resizes for d in diffs.values()), \
        "a gain-value change must not be flagged as a resize"

    ctrl.resync_from_grc(mode=prefs.GRC_AUTO, chip_types={"kyttar_10x12": ct})

    fir_after = tuple((c.x, c.y, str(c.face))
                      for c in ctrl.project.block(f).placement.cells)
    assert fir_after == fir_before, \
        "a non-resizing resync must NOT move/rotate an unrelated block"
    assert len(ctrl.project.connections) == n_conn_before, \
        "a non-resizing resync must NOT drop connections"
    # The gain param WAS applied.
    assert ctrl.project.block(g).params.get("gain") == 0.8


def test_resize_resync_is_flagged_and_replaces():
    """A tap-COUNT change is flagged as a resize (so the re-place path runs).
    (We only assert the resize flag + that resync completes without error; the
    in-place-vs-replace behavior for a resize is the auto_place path.)"""
    ctrl, g, f, ct = _chain()
    ctrl.grc_sync.observe(f, {"coefficients": "[" + ",".join(["0.05"] * 20) + "]"})
    diffs = ctrl.refresh_grc_sync()
    assert diffs.get("firfilter") and diffs["firfilter"].resizes, \
        "a tap-count change must be flagged as a resize"
    # Should not raise.
    ctrl.resync_from_grc(mode=prefs.GRC_AUTO, chip_types={"kyttar_10x12": ct})
