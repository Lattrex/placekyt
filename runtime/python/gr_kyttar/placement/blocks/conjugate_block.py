# SPDX-License-Identifier: GPL-3.0-or-later
"""ConjugateBlock — see the class docstring."""
import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock


class ConjugateBlock(KyttarBlock):
    """
    Complex conjugate — drop-in for GNU Radio ``blocks.conjugate_cc``:
    ``out[n] = conj(in[n]) = re[n] − j·im[n]`` (negate the imaginary part).

    The staple of correlators and conjugate-multiply (matched filtering, delay-and-
    conjugate-multiply frequency estimators). On chip: pass the real part through
    and negate the imaginary part (``0 − im`` via SUB), emitting the two words of
    the conjugated sample. No parameters (full GRC parity). Memoryless → delay=0.

    Q15 NOTE: the only imag value whose negation overflows is exactly −1.0
    (0x8000): ``−(−1.0) = +1.0`` is unrepresentable, so the SUB WRAPS back to
    0x8000 (the bit-exact reference models the wrap). The DSP-equivalence stimulus
    keeps |im| < 1 so the conjugate tracks GR float exactly; the wrap is exercised
    only against the bit-exact reference.

    Interface: COMPLEX input (re@R0, im@R1, the proven complex-burst fan-in),
    COMPLEX output (re, −im).
    """
    CATEGORY = "signal_conditioning"
    TAGS = ["conjugate", "conjugate_cc", "complex", "signal_conditioning"]

    _interface = BlockInterface(
        entry_address=1, input_registers=[0, 1], output_registers=[0, 1])

    def __init__(self, name: str):
        super().__init__(name)

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> dict:
        return {0: CellProgram(
            inputs=[Port("re", register=0), Port("im", register=1)],
            outputs=[Port("out_re"), Port("out_im"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("zero", 0, address=2)],
            state=[StateVar("rs"), StateVar("neg")],
            assembly_template="""\
start:
    MOVE R{state:rs}, R{in:re}
    SUB R{data:zero}, R{in:im}
    MOVE R{state:neg}, R0
    MOVE R0, R{state:rs}
    {write:out_re}
    MOVE R0, R{state:neg}
    {write:out_im}
    {jump:trig}
""",
        )}

    def output_cell_ids(self):
        return [0]

    # -------------------------------------------------------------- reference
    @staticmethod
    def _s16(v):
        v = int(v) & 0xFFFF
        return v - 0x10000 if v >= 0x8000 else v

    def process_reference_q15(self, re_q15, im_q15) -> list:
        """Bit-exact predictor: (re, −im) with the Q15 wrapping negate."""
        out = []
        for r, i in zip(re_q15, im_q15):
            neg = (-self._s16(i)) & 0xFFFF       # wraps for im = -32768
            out.append((int(r) & 0xFFFF, neg))
        return out

    def process_reference(self, input_samples) -> np.ndarray:
        """Float reference: the complex conjugate."""
        arr = np.asarray(input_samples)
        if np.iscomplexobj(arr):
            return np.conjugate(arr).astype(np.complex64)
        return arr.astype(np.complex64)
