# SPDX-License-Identifier: GPL-3.0-or-later
"""QPSKSlicerBlock — see :class:`QPSKSlicerBlock`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class QPSKSlicerBlock(KyttarBlock):
    """
    QPSK Hard-Decision Slicer / Decoder Block (1 cell).

    Mirrors GNU Radio ``digital.constellation_decoder_cb(constellation_qpsk())``:
    turns a received (I, Q) sample into the 2-bit Gray symbol index 0..3. QPSK is
    constant-modulus and separable, so each axis is a pure SIGN decision (no PAM
    magnitude threshold — that is what makes this ONE cell, unlike the 2-cell
    16-QAM slicer). The GR ``constellation_qpsk()`` index map is::

        MSB (bit 0 of the symbol) = imag-sign   (Q >= 0 -> 1)
        LSB (bit 1 of the symbol) = real-sign   (I >= 0 -> 1)
        symbol = (Q >= 0 ? 2 : 0) | (I >= 0 ? 1 : 0)

    Verified against GR itself (constellation_qpsk().decision_maker):
        I+ Q+ -> 3   I- Q+ -> 2   I+ Q- -> 1   I- Q- -> 0

    This is the receiver's final decision stage. Composed with the QPSK mapper it is
    the identity on a clean channel: 2 bits -> (I,Q) -> symbol 0..3. The sign
    convention matches :class:`SoftDemodulatorBlock` ('qpsk'): the two LLR signs it
    emits (MSB from Q, LSB from I) equal this slicer's two symbol bits, so a
    soft-demod chain and a hard-slicer chain agree bit-for-bit on a clean channel.

    Interface:
        - Entry: R1 (cell 0)
        - Inputs: I (R0), Q (R1) — a complex sample landing as a paired packet
        - Output: 2-bit symbol index (0..3)

    Single cell: two branchless sign tests OR-shifted into a running symbol. Uses
    the N (negative) flag from ``CMP R, 0`` — the same hard-decision pattern the
    BPSK slicer and DFE use.
    """
    CATEGORY = "demodulation"
    TAGS = ["slicer", "hard_decision", "qpsk", "demodulation"]

    # The landing cell reads I @R0 and Q @R1 (see build_cell_programs). The interface
    # MUST match those actual cell-input registers so a placeKYT block->block
    # connection (which routes to input_registers) lands I/Q where the slicer reads
    # them. Output: the 2-bit index at R0.
    _interface = BlockInterface(entry_address=1, input_registers=[0, 1],
                                output_registers=[0])

    def __init__(self, name: str):
        super().__init__(name)

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """(I, Q) -> 2-bit Gray symbol. MSB = (Q >= 0), LSB = (I >= 0), matching
        GR constellation_qpsk(). Two sign tests, each OR-shifted into ``sym``.

        ``CMP R{axis}, 0`` sets N when the axis < 0; ``BR.N`` skips the ``OR #1`` so
        a negative axis keeps its bit 0 and a non-negative axis sets it 1. Q is
        sliced first (it is the MSB), then I (the LSB), so ``sym = (q_bit<<1)|i_bit``.
        Single cell, single output face."""
        return {0: CellProgram(
            inputs=[Port("in_i", register=0), Port("in_q", register=1)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("zero", 0x0000, address=2),
                DataWord("one", 0x0001, address=3),
            ],
            state=[StateVar("sym"), StateVar("i_save"), StateVar("q_save")],
            assembly_template="""\
start:
    MOVE R{state:i_save}, R{in:in_i}        ; save I,Q before R0 is clobbered
    MOVE R{state:q_save}, R{in:in_q}
    MOVE R{state:sym}, R{data:zero}
    ; MSB = (Q >= 0)
    SHL R{state:sym}, #1
    CMP R{state:q_save}, R{data:zero}
    BR.N i_bit
    OR R0, R{data:one}
i_bit:
    MOVE R{state:sym}, R0
    ; LSB = (I >= 0)
    SHL R{state:sym}, #1
    CMP R{state:i_save}, R{data:zero}
    BR.N emit
    OR R0, R{data:one}
emit:
    {write:out}
    {jump:out}
""",
        )}

    def process_reference(self, samples) -> np.ndarray:
        """Reference: (I, Q) Q15 pairs -> 2-bit Gray symbol index list (0..3),
        matching GR constellation_qpsk() (MSB = imag-sign, LSB = real-sign)."""
        def s16(v):
            v = int(v) & 0xFFFF
            return v - 0x10000 if v & 0x8000 else v
        out = []
        for (i, q) in samples:
            sym = (2 if s16(q) >= 0 else 0) | (1 if s16(i) >= 0 else 0)
            out.append(sym & 0x3)
        return np.asarray(out, dtype=np.int16)

    def reset(self):
        """No state carried across samples (sym/i_save are per-sample scratch)."""
        pass
