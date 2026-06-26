# SPDX-License-Identifier: GPL-3.0-or-later
"""ComplexToMagSquaredBlock — see the class docstring."""
import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port
from ._base import BlockInterface, KyttarBlock


class ComplexToMagSquaredBlock(KyttarBlock):
    """
    Instantaneous power |z|² — drop-in for GNU Radio
    ``blocks.complex_to_mag_squared``: ``out[n] = re[n]² + im[n]²``.

    The envelope/power primitive behind every energy detector, AGC error, and
    squelch. On chip it is two Q15 ops in one cell: ``MULQ re,re`` then
    ``MACQ im,im`` accumulate ``(re²+im²)`` in R0. No parameters (GR's
    complex_to_mag_squared has none — full GRC parity). Memoryless → delay=0.

    RANGE / SATURATION: with re, im ∈ [-1, 1) the true power is in [0, 2), but Q15
    only represents [0, 1). So |z| ≥ 1 (notably the unit circle) overflows; this
    block SATURATES to +full-scale (production behavior) rather than wrapping. The
    power is always ≥ 0, so any overflow makes the 16-bit accumulator look negative
    (bit 15 set) — detected with a single ``BR.N`` and pinned to ``0x7FFF``. The
    DSP-equivalence stimulus stays inside the unit circle (|z| < 1) where the
    result is Q15-representable and tracks GR float within the two-op floor.

    Interface: COMPLEX input (re@R0, im@R1, the proven complex-burst fan-in), ONE
    real output (the power).
    """
    CATEGORY = "signal_conditioning"
    TAGS = ["complex_to_mag_squared", "power", "envelope", "signal_conditioning"]

    SAT_POS_Q15 = 0x7FFF

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
            inputs=[Port("re", register=0), Port("im", register=1)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[DataWord("satpos", self.SAT_POS_Q15, address=2)],
            assembly_template="""\
start:
    MULQ R{in:re}, R{in:re}
    MACQ R{in:im}, R{in:im}
    BR.N _sat
    {write:out}
    {jump:out}
    HALT
_sat:
    MOVE R0, R{data:satpos}
    {write:out}
    {jump:out}
""",
        )}

    # -------------------------------------------------------------- reference
    @staticmethod
    def _s16(v):
        v = int(v) & 0xFFFF
        return v - 0x10000 if v >= 0x8000 else v

    def process_reference_q15(self, re_q15, im_q15) -> list:
        """Bit-exact predictor: ``(re²>>15) + (im²>>15)`` with saturation. The
        power is non-negative so saturation is a single upper clamp."""
        out = []
        for r, i in zip(re_q15, im_q15):
            R, I = self._s16(r), self._s16(i)
            s = ((R * R) >> 15) + ((I * I) >> 15)
            s = min(32767, s)                       # saturate (s >= 0 always)
            out.append(s & 0xFFFF)
        return out

    def process_reference(self, input_samples) -> np.ndarray:
        """Float reference: re² + im², clamped to the Q15 range."""
        arr = np.asarray(input_samples)
        if np.iscomplexobj(arr):
            p = (arr.real.astype(np.float64) ** 2
                 + arr.imag.astype(np.float64) ** 2)
        else:
            p = arr.astype(np.float64) ** 2
        return np.clip(p, 0.0, 32767.0 / 32768.0).astype(np.float32)
