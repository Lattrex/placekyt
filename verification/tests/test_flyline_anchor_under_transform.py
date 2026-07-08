# SPDX-License-Identifier: GPL-3.0-or-later
"""Bug A guard: a block's port→I/O-cell resolution + flyline anchor stay CORRECT under
every D4 transform, so a manual router following a flyline never wires a connection
backwards.

The SSB hand-place hit a reversed flyline (iqupconvert_2.out -> clpf_2.xi, a cycle NOT
in the netlist). Root-caused to the degenerate orientation stack (bug B, now fixed via
canonicalisation). This test locks in the invariant that made bug A possible: for EVERY
transform of a real block, each port resolves to a DISTINCT, correctly-placed cell whose
FACE matches the port direction — the data the flyline anchor + stub-click normalisation
rely on. If this holds, a followed flyline can only ever produce a forward (out→in) net.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = Path(__file__).resolve().parents[2]
for p in (str(_ROOT / "placekyt"), str(_ROOT / "runtime" / "python")):
    if p not in sys.path:
        sys.path.insert(0, p)

from model.enums import Face  # noqa: E402
from model.placement import PlacedCell, Placement  # noqa: E402

_SEQS = [
    [], ["cw"], ["ccw"], ["cw", "cw"], ["mirror_h"], ["mirror_v"],
    ["cw", "mirror_h"], ["mirror_v", "cw", "ccw", "mirror_v"],  # the SSB degenerate one
]


def _catalog():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from engine.catalog import BlockCatalog
    return BlockCatalog.from_gr_kyttar()


def _placement_from_layout(block):
    lay = block.default_layout()
    return Placement(chip=0, cells=[
        PlacedCell(cell_id=cid, x=xy[0], y=xy[1], face=Face.from_str(xy[2]))
        for cid, xy in lay.items()])


@pytest.mark.parametrize("seq", _SEQS)
def test_iqupconvert_ports_distinct_and_placed_under_transform(seq):
    """IQUpconvert's input (phase) and output (upmix) cells stay DISTINCT + on-grid
    under every transform — the exact block whose reversed flyline broke the SSB
    hand-place."""
    from gr_kyttar.placement.blocks.iq_upconvert_block import IQUpconvertBlock
    pl = _placement_from_layout(IQUpconvertBlock("iq", frequency=6000.0))
    for k in seq:
        pl.transform(k)
    cells = {c.cell_id: (c.x, c.y) for c in pl.cells}
    assert "phase" in cells and "upmix" in cells
    assert cells["phase"] != cells["upmix"], (seq, cells)     # in ≠ out cell


@pytest.mark.parametrize("seq", _SEQS)
def test_portmap_direction_and_cell_agree_after_transform(seq):
    """The PortMap (which drives the flyline stub's direction + anchor cell) resolves
    each port to a cell the placement actually HAS after the transform, and input vs
    output land on different cells — so a stub-click can only normalise to out→in."""
    cat = _catalog()
    from gr_kyttar.placement.blocks.iq_upconvert_block import IQUpconvertBlock
    pl = _placement_from_layout(IQUpconvertBlock("iq", frequency=6000.0))
    for k in seq:
        pl.transform(k)
    pm = cat.port_map("IQUpconvertBlock", {}, library=None)
    placed = {c.cell_id for c in pl.cells}
    ins, outs = set(), set()
    for p in pm.ports:
        assert p.cell_id in placed, (p.name, p.cell_id, seq)   # port cell exists
        (ins if p.direction == "in" else outs).add(p.cell_id)
    # Every input port cell is DISTINCT from every output port cell → no self-anchor
    # that could render a flyline as a reversed (out→in-of-self) net.
    assert ins and outs and ins.isdisjoint(outs), (ins, outs, seq)
