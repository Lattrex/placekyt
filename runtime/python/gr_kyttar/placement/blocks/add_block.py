# SPDX-License-Identifier: GPL-3.0-or-later
"""Two-stream Add / Subtract — :class:`AddBlock`, :class:`SubtractBlock`."""
import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock


class _TwoStreamAddSub(KyttarBlock):
    """Shared single-cell SATURATING two-stream combiner.

        out[n] = a[n] (+|-) b[n]   clamped to the Q15 range [-1.0, +0.99997]

    GNU Radio's ``add_ff`` / ``sub_ff`` are pure float combiners (no params, no
    saturation), so these blocks have no params (full GRC parity) and the verified
    DSP equivalence is asserted on IN-RANGE stimulus (|a±b| < 1) where the float
    sum is representable. OUT of range the Q15 ALU would WRAP (a + b mod 2^16 — so
    0.6+0.6 = -0.8, a sign flip), which is a production footgun; this block instead
    SATURATES — the production fixed-point behavior shared by the FIR / decimator /
    dc-blocker datapaths.

    Saturation uses the ADD/SUB ``V`` (signed-overflow) flag: on overflow the true
    result's sign equals ``sign(a)`` for BOTH add (same-sign operands) and subtract
    (opposite-sign operands), so the rail is picked from ``a``'s sign bit with the
    shared ``0x7FFF + signbit`` trick (→ +0x7FFF for positive, -0x8000 for
    negative). The two-path structure (duplicated emit + a terminal HALT, branch
    target on a REAL instruction) is the same one the FIR's saturating restore
    uses — a GOTO/branch onto a ``{write}``/``{jump}`` placeholder label is
    miscompiled into a stray output JUMP.

    Interface: TWO real inputs ``a`` @R0, ``b`` @R1 (the proven complex-burst
    fan-in), ONE real output. Memoryless → delay=0.
    """
    _OP = "ADD"            # overridden to "SUB" by SubtractBlock
    _SIGN = +1             # +1 for add, -1 for subtract (reference)
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
        # Data placed PAST the highest input register (R1) so it never collides
        # with an explicit input reg (NCO state-allocation-gap gotcha).
        return {0: CellProgram(
            inputs=[Port("a", register=0), Port("b", register=1)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[DataWord("satpos", self.SAT_POS_Q15, address=2)],
            state=[StateVar("asav")],
            assembly_template=f"""\
start:
    MOVE R{{state:asav}}, R{{in:a}}
    {self._OP} R{{in:a}}, R{{in:b}}
    BR.V _sat
    {{write:out}}
    {{jump:out}}
    HALT
_sat:
    MOVE R0, R{{state:asav}}
    SHR R0, #15
    ADD R0, R{{data:satpos}}
    {{write:out}}
    {{jump:out}}
""",
        )}

    # -------------------------------------------------------------- reference
    @staticmethod
    def _s16(v):
        v = int(v) & 0xFFFF
        return v - 0x10000 if v >= 0x8000 else v

    def process_reference_q15(self, a_q15, b_q15) -> list:
        """Bit-exact predictor of the on-chip SATURATING add/sub."""
        out = []
        for a, b in zip(a_q15, b_q15):
            s = self._s16(a) + self._SIGN * self._s16(b)
            s = max(-32768, min(32767, s))          # saturate (not wrap)
            out.append(s & 0xFFFF)
        return out

    def process_reference(self, input_samples) -> np.ndarray:
        """Float reference. The two streams are carried as one complex array
        (real = a, imag = b); returns a (+|-) b, clamped to the Q15 range."""
        arr = np.asarray(input_samples)
        if np.iscomplexobj(arr):
            a = arr.real.astype(np.float64)
            b = arr.imag.astype(np.float64)
        else:
            a = arr.astype(np.float64)
            b = np.zeros_like(a)
        s = a + self._SIGN * b
        return np.clip(s, -1.0, 32767.0 / 32768.0).astype(np.float32)


class AddBlock(_TwoStreamAddSub):
    """Two-stream adder — drop-in for GNU Radio ``blocks.add_ff``
    (``out = a + b``). Saturating Q15. See :class:`_TwoStreamAddSub`."""
    CATEGORY = "signal_conditioning"
    TAGS = ["add", "sum", "two_stream", "add_ff", "signal_conditioning"]
    _OP = "ADD"
    _SIGN = +1


class SubtractBlock(_TwoStreamAddSub):
    """Two-stream subtractor — drop-in for GNU Radio ``blocks.sub_ff``
    (``out = a - b``). Saturating Q15. See :class:`_TwoStreamAddSub`."""
    CATEGORY = "signal_conditioning"
    TAGS = ["subtract", "difference", "two_stream", "sub_ff", "signal_conditioning"]
    _OP = "SUB"
    _SIGN = -1
