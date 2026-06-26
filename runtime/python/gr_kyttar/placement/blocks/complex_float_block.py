# SPDX-License-Identifier: GPL-3.0-or-later
"""Complex<->Float type conversions — :class:`ComplexToFloatBlock`,
:class:`FloatToComplexBlock`."""
import numpy as np

from ..block import CellProgram, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock


class _IQPassthrough(KyttarBlock):
    """Shared single-cell I/Q de/recompose.

    On the Kyttar substrate a complex sample is carried as a TWO-operand pair
    (the real part in R0, the imaginary part in R1) — the exact representation the
    complex-burst fan-in delivers and a complex output cell emits (re then im,
    interleaved on one bus corridor, as the NCO/mixer prove). So BOTH GR
    conversions are the SAME datapath — read the two operands, emit them as two
    words — they differ only in GRC port typing:

      * ``blocks.float_to_complex``: two real inputs (re, im) -> one complex out.
      * ``blocks.complex_to_float``: one complex input -> two real outs (re, im).

    Pure data movement (MOVE/WRITE, no arithmetic), so the conversion is EXACT —
    zero Q15 error. Memoryless -> delay=0.
    """
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
            data=[],
            state=[StateVar("rs"), StateVar("is_")],
            assembly_template="""\
start:
    MOVE R{state:rs}, R{in:re}
    MOVE R{state:is_}, R{in:im}
    MOVE R0, R{state:rs}
    {write:out_re}
    MOVE R0, R{state:is_}
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
        """Bit-exact predictor: identity pass-through of the I/Q pair."""
        return [(int(r) & 0xFFFF, int(i) & 0xFFFF)
                for r, i in zip(re_q15, im_q15)]

    def process_reference(self, input_samples) -> np.ndarray:
        """Float reference: the complex input is returned unchanged (the I/Q pair
        is merely relabeled by the conversion)."""
        arr = np.asarray(input_samples)
        if np.iscomplexobj(arr):
            return arr.astype(np.complex64)
        return arr.astype(np.complex64)


class FloatToComplexBlock(_IQPassthrough):
    """Combine two real streams into a complex stream — drop-in for GNU Radio
    ``blocks.float_to_complex`` (``out = re + j·im``). Exact. See
    :class:`_IQPassthrough`."""
    CATEGORY = "signal_conditioning"
    TAGS = ["float_to_complex", "type_convert", "iq", "signal_conditioning"]


class ComplexToFloatBlock(_IQPassthrough):
    """Split a complex stream into its real and imaginary streams — drop-in for
    GNU Radio ``blocks.complex_to_float`` (``re = real(in)``, ``im = imag(in)``).
    Exact. See :class:`_IQPassthrough`."""
    CATEGORY = "signal_conditioning"
    TAGS = ["complex_to_float", "type_convert", "iq", "signal_conditioning"]
