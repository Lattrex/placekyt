# SPDX-License-Identifier: GPL-3.0-or-later
"""AbsBlock — see the class docstring."""
import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock


class AbsBlock(KyttarBlock):
    """
    Absolute value — drop-in for GNU Radio ``blocks.abs_ff``: ``out[n] = |in[n]|``.

    On chip: one conditional negate — ``CMP`` the input against zero and, if
    negative, replace it with ``0 − in`` (the abs idiom the AGC / QAM16 slicer use).
    No parameters (full GRC parity). Memoryless → delay=0. Single real input,
    single real output.

    Q15 NOTE: the only input whose abs overflows is exactly −1.0 (0x8000):
    ``|−1.0| = +1.0`` is unrepresentable, so the negate WRAPS back to 0x8000 (the
    bit-exact reference models the wrap). The DSP-equivalence stimulus keeps the
    input > −1.0 so ``|x|`` tracks GR float exactly.
    """
    CATEGORY = "signal_conditioning"
    TAGS = ["abs", "abs_ff", "rectify", "signal_conditioning"]

    _interface = BlockInterface(
        entry_address=1, input_registers=[0], output_registers=[0])

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
            inputs=[Port("x", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[DataWord("zero", 0, address=1)],
            state=[StateVar("xs")],
            assembly_template="""\
start:
    MOVE R{state:xs}, R{in:x}
    CMP R{state:xs}, R{data:zero}
    BR.NN _emit
    SUB R{data:zero}, R{state:xs}
    MOVE R{state:xs}, R0
_emit:
    MOVE R0, R{state:xs}
    {write:out}
    {jump:out}
""",
        )}

    # -------------------------------------------------------------- reference
    @staticmethod
    def _s16(v):
        v = int(v) & 0xFFFF
        return v - 0x10000 if v >= 0x8000 else v

    def process_reference_q15(self, x_q15) -> list:
        """Bit-exact predictor: |x| with the Q15 wrapping negate (x = −1.0 → −1.0)."""
        out = []
        for x in x_q15:
            X = self._s16(x)
            a = (-X) & 0xFFFF if X < 0 else X & 0xFFFF
            out.append(a)
        return out

    def process_reference(self, input_samples) -> np.ndarray:
        """Float reference: |x|."""
        return np.abs(np.asarray(input_samples).astype(np.float64)).astype(np.float32)
