# SPDX-License-Identifier: GPL-3.0-or-later
"""Block-verification for ComplexFIRFilterBlock: memory budget + the S>0 guard.

A multi-cell complex FIR runs an I and a Q delay segment per cell (shared taps)
and, on the LAST cell, two saturating-restore emit sequences. We must guarantee
at BLOCK-VERIFY time (not chip build time) that:

  1. every cell fits the 32-word budget for the Σ|h|≤1 (head_shift==0) case that
     the low-pass / band wrappers produce, across a range of tap counts;
  2. the block REJECTS a Σ|h|>1 (head_shift>0) multi-cell filter with a clear
     error instead of emitting an oversized last cell.

Bit-exactness vs GNU Radio's fir_filter_ccf is proven in the verification harness
(complex I/Q sweep); this test guards the memory contract only.
"""

from __future__ import annotations

import pytest

from gr_kyttar.placement.blocks.complex_fir_filter_block import ComplexFIRFilterBlock
from gr_kyttar.placement.blocks import _firdes

CELL_WORDS = 32


def _worst_cell_words(block: ComplexFIRFilterBlock) -> int:
    """Largest total word count (instructions + auto-halt + data + state +
    auto-allocated inputs + the two fixed xi/xq landing regs) over all cells."""
    worst = 0
    for prog in block.build_cell_programs().values():
        ninstr = sum(
            1 for ln in prog.assembly_template.splitlines()
            if ln.strip() and not ln.strip().endswith(":"))
        auto_inputs = sum(1 for p in prog.inputs if p.register is None)
        # +1 auto-halt word, +2 for the fixed xi(R0)/xq(R1) landing registers.
        total = (ninstr + 1) + len(prog.data) + len(prog.state) + auto_inputs + 2
        worst = max(worst, total)
    return worst


@pytest.mark.parametrize("transition_width,expect_min_cells", [
    (8000.0, 2),   # ~9 taps
    (6000.0, 4),   # ~13 taps
    (4000.0, 6),   # ~19 taps
    (2500.0, 12),  # ~31 taps (the SSB Weaver LPF)
])
def test_multicell_budget_fits(transition_width, expect_min_cells):
    # gain=0.9 keeps Σ|h|<=1 -> head_shift==0 -> no last-cell restore overflow.
    taps = _firdes.low_pass(0.9, 32000.0, 1200.0, transition_width, "hamming", 6.76)
    block = ComplexFIRFilterBlock("cfir", taps)
    assert block.cell_count >= expect_min_cells
    worst = _worst_cell_words(block)
    assert worst <= CELL_WORDS, (
        f"{block.cell_count}-cell complex FIR worst cell needs {worst} words "
        f"(> {CELL_WORDS})")


def test_single_cell_budget_fits():
    # Short filter (3 taps) -> single cell path.
    taps = _firdes.low_pass(0.9, 32000.0, 6000.0, 20000.0, "hamming", 6.76)
    block = ComplexFIRFilterBlock("cfir1", taps)
    assert block.cell_count == 1
    assert _worst_cell_words(block) <= CELL_WORDS


def test_multicell_sum_gt_one_is_rejected():
    # gain=1.0 low-pass has Σ|h| slightly >1 -> head_shift==1; the multi-cell
    # last cell would overflow, so the block must refuse to build its programs.
    taps = _firdes.low_pass(1.0, 32000.0, 1200.0, 2500.0, "hamming", 6.76)
    block = ComplexFIRFilterBlock("cfir_hot", taps)
    assert block.cell_count > 1
    assert block._head_shift > 0
    with pytest.raises(ValueError, match=r"Σ|head_shift|overflow"):
        block.build_cell_programs()
