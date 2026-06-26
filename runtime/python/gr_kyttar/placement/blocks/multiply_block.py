# SPDX-License-Identifier: GPL-3.0-or-later
"""MultiplyBlock — see :class:`MultiplyBlock`."""
import numpy as np

from ..block import CellProgram, EntryPoint, Port
from ._base import BlockInterface, KyttarBlock


class MultiplyBlock(KyttarBlock):
    """
    Two-stream multiply — drop-in for GNU Radio ``blocks.multiply_ff``: the
    element-wise product of two real input streams.

        out[n] = a[n] · b[n]

    GNU Radio's ``multiply_ff`` takes no parameters (it is a pure pointwise
    product), so this block has none either — full GRC parity. On chip it is a
    SINGLE ``MULQ``: ``out = (a · b) >> 15`` (Q15 fixed-point product).

    Interface: TWO real inputs ``a`` @R0 and ``b`` @R1 (the same complex-burst
    fan-in the Costas xi/xq tap proves — each sample delivers ``WRITE a -> R0`` +
    ``WRITE b -> R1`` + one ``JUMP``), and ONE real output. Memoryless, no group
    delay (delay=0).

    Q15 note: the only product that overflows the Q15 range is the exact
    ``(-1.0) · (-1.0) = +1.0`` corner — ``(0x8000·0x8000) >> 15`` wraps to
    ``0x8000`` (the MULQ datapath WRAPS, it does not saturate). The bit-exact
    reference models that wrap; verification keeps the GR-equivalence stimulus off
    the simultaneous full-scale-negative corner so the Q15 product matches GR float
    within the single-MULQ quantization floor.
    """
    CATEGORY = "signal_conditioning"
    TAGS = ["multiply", "product", "two_stream", "multiply_ff", "signal_conditioning"]

    # Two real inputs land in R0 (a) and R1 (b); the product egresses from R0.
    _interface = BlockInterface(
        entry_address=1, input_registers=[0, 1], output_registers=[0])

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
            inputs=[Port("a", register=0), Port("b", register=1)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[],
            assembly_template="""\
start:
    MULQ R{in:a}, R{in:b}     ; R0 = (a * b) >> 15   (Q15 product)
    {write:out}
    {jump:out}
""",
        )}

    # -------------------------------------------------------------- reference
    @staticmethod
    def _s16(v):
        v = int(v) & 0xFFFF
        return v - 0x10000 if v >= 0x8000 else v

    def process_reference_q15(self, a_q15, b_q15) -> list:
        """Bit-exact predictor of the on-chip MULQ: ``(a · b) >> 15`` with the Q15
        datapath's wrapping overflow. ``a_q15``/``b_q15`` are uint16 Q15 words."""
        out = []
        for a, b in zip(a_q15, b_q15):
            p = (self._s16(a) * self._s16(b)) >> 15   # arithmetic shift (floor)
            out.append(p & 0xFFFF)
        return out

    def process_reference(self, input_samples) -> np.ndarray:
        """Float reference. The two streams are carried as one complex array
        (real = a, imag = b); returns their real product."""
        arr = np.asarray(input_samples)
        if np.iscomplexobj(arr):
            a = arr.real.astype(np.float64)
            b = arr.imag.astype(np.float64)
        else:
            a = arr.astype(np.float64)
            b = arr.astype(np.float64)
        return (a * b).astype(np.float32)
