# SPDX-License-Identifier: GPL-3.0-or-later
"""A multi-cell FIR folds compactly and PREFERS an even column count so its I/O
co-locates on the same edge when it cleanly can (INV-14).

Bug: ``default_layout`` snaked the datapath cells column-major at a FIXED column
height (FOLD_HEIGHT=4), letting the COLUMN COUNT fall out of the cell count. A
column-major snake puts the input at the top of column 0; the output lands back
on the SAME (top) edge only when the snake fills an EVEN number of full columns —
column 0 goes down, column 1 up, … an even count ends going up at the top. With
a fixed height the count was often odd (or a single partial column), so the
output ended on the OPPOSITE (bottom) edge — the recurring "input and output on
opposite sides" problem.

Fix (no padding): the fold chooser prefers the most compact fold whose cells fill
an EVEN number of full columns, so the input (cell 0, top of column 0) and the
output (last cell) co-locate on the top edge with NO relay padding. When the cell
count can't fold into full even columns the most compact fold is used as-is and
the router connects the output from wherever the last cell lands — close, not
forced. This pins: (1) the fold is compact (≤ FOLD_HEIGHT rows); (2) when an even
full-column fold exists, I/O co-locates at the top edge; (3) no transit/relay pad
cells are introduced.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from gr_kyttar.placement.blocks.fir_filter_block import FIRFilterBlock


# Tap counts whose cell counts DO fold into full even columns (co-locate).
@pytest.mark.parametrize("taps", [8, 20, 40])
def test_even_fold_colocates_io(taps):
    b = FIRFilterBlock("f", [0.4 / taps] * taps)
    n = b.cell_count
    assert n > 1
    layout = b.default_layout()
    cols, rows = b._fold_geometry()
    assert n == cols * rows, "these counts fold into a full rectangle"
    assert cols % 2 == 0, "an even column count is what co-locates I/O"
    # Input cell 0 at the top edge; output (last cell) back on the top edge.
    assert layout[0][1] == 0
    assert layout[n - 1][1] == 0, "even full-column fold lands the output on the top edge"


@pytest.mark.parametrize("taps", [8, 13, 20, 40, 64])
def test_fold_is_compact_and_padfree(taps):
    b = FIRFilterBlock("f", [0.4 / taps] * taps)
    n = b.cell_count
    if n <= 1:
        return
    layout = b.default_layout()
    cols, rows = b._fold_geometry()
    assert rows <= b.FOLD_HEIGHT, "fold must stay within FOLD_HEIGHT rows"
    # No relay/transit padding cells — every layout key is a real datapath cell.
    assert all(isinstance(k, int) for k in layout), "fold must not introduce pad cells"
    assert len(layout) == n


def test_partial_fold_is_left_for_the_router():
    """A cell count with no even full-column fold takes the most compact fold and
    leaves the output wherever the snake ends — the router connects it (no pad,
    no forced co-location)."""
    b = FIRFilterBlock("f", [0.1] * 13)  # 3 cells → single column 1×3
    layout = b.default_layout()
    cols, rows = b._fold_geometry()
    assert all(isinstance(k, int) for k in layout)
    # Compact: the 3 cells stack in one column (the tallest fold ≤ FOLD_HEIGHT).
    assert cols * rows >= b.cell_count
    assert rows <= b.FOLD_HEIGHT
