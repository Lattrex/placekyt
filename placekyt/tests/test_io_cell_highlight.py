# SPDX-License-Identifier: GPL-3.0-or-later
"""A multi-cell block highlights BOTH its input and output I/O cells.

Regression: the canvas resolved a block's I/O cells from a PortMap built WITHOUT
the block's params. For a scaling block (FIR) the param-less default is the 1-tap
case where input and output collapse to cell 0, so the "output takes precedence"
rule overwrote the input role and only the output cell got the pink highlight.
The fix resolves the PortMap WITH the placed block's params, so a multi-cell FIR
exposes its true distinct input (cell 0) and output (last cell).
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from engine.catalog import BlockCatalog

LIB = "lattrex.official"


@pytest.fixture(scope="module")
def cat():
    return BlockCatalog.from_gr_kyttar()


def test_multicell_fir_portmap_has_distinct_io_cells(cat):
    """With params, a 20-tap FIR's input and output sit on DIFFERENT cells —
    the precondition for highlighting both (the param-less default collapses
    them to cell 0, which is the bug)."""
    taps = [0.05] * 20
    pm = cat.port_map("FIRFilterBlock", {"coefficients": taps}, library=LIB)
    ins = {p.cell_id for p in pm.ports if p.direction == "in"}
    outs = {p.cell_id for p in pm.ports if p.direction == "out"}
    assert ins and outs, "FIR must expose both an input and an output port"
    assert ins.isdisjoint(outs), \
        "multi-cell FIR input and output must be DISTINCT cells (so both highlight)"
    assert 0 in ins, "input is the first cell"


def test_paramless_default_collapses_io(cat):
    """Documents WHY params matter: without them the FIR PortMap is the 1-tap
    default where input and output share cell 0 (only output would highlight)."""
    pm = cat.port_map("FIRFilterBlock", library=LIB)
    ins = {p.cell_id for p in pm.ports if p.direction == "in"}
    outs = {p.cell_id for p in pm.ports if p.direction == "out"}
    # The param-less default collapses I/O — exactly the case the fix avoids by
    # passing the instance params.
    assert ins == outs == {0}


def test_io_roles_marks_both_cells_for_multicell_block(cat):
    """End-to-end: the canvas I/O-role resolver, given a params-aware provider,
    marks BOTH an input and an output cell for a placed multi-cell FIR."""
    taps = [0.05] * 20

    def provider(block_type, library, params=None):
        pm = cat.port_map(block_type, params, library=library)
        return {p.name: (p.cell_id, p.direction) for p in pm.ports}

    # Mirror the canvas's io_roles resolution (chip_canvas._render_chip).
    pmap = provider("FIRFilterBlock", LIB, {"coefficients": taps})
    io_roles = {}
    for pname, (cid, direction) in pmap.items():
        role = "input" if direction in ("in", "input") else "output"
        if io_roles.get(cid) != "output":
            io_roles[cid] = role
    roles = set(io_roles.values())
    assert "input" in roles, "input cell must be highlighted (was missing)"
    assert "output" in roles, "output cell must be highlighted"
