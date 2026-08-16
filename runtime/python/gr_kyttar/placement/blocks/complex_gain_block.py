# SPDX-License-Identifier: GPL-3.0-or-later
"""ComplexGainBlock — see :class:`ComplexGainBlock`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict, Any
from ._base import KyttarBlock, BlockInterface, float_to_q15


class ComplexGainBlock(KyttarBlock):
    """Complex fixed-gain scaler — mirrors GNU Radio ``blocks.multiply_const_cc(gain)``.

    Multiplies a complex (I, Q) stream by the SAME real constant ``gain`` on BOTH
    rails (out = gain * in), so the constellation is scaled WITHOUT rotation or
    distortion — the receiver gain-staging stage a matched filter needs before a
    fixed-threshold decision-directed loop (the 16-QAM RX: the MF output is
    attenuated ~2.8x by the MF's Q15 headroom pre-scale, and the DD Costas + slicer
    have FIXED decision thresholds that assume the constellation at its nominal scale,
    so the MF output MUST be scaled back up).

    Q15 gain > 1 (COEFFICIENT-HEADROOM datapath, INV-13). The datapath is Q15
    ``[-1, 1)`` but ``gain`` may exceed 1 (a receiver amplifies). Rather than store
    ``gain`` (which does not fit Q15 for gain>1), the block stores ``gain/4`` — always
    representable for ``0 < gain < 4`` — multiplies each rail by it (the product
    ``x·gain/4`` is ALWAYS in Q15 range, so the accumulator NEVER wraps), then restores
    the gain with a **saturating left shift by 2**::

        gq = Q15(gain / 4)                 # coefficient, |gq| < 1  ⇒  fits Q15
        p  = MULQ(x, gq)                   # = x·gain/4, in range (no wrap)
        out = SAT(p << 2)                  # = clamp(x·gain), pinned to ±full-scale

    So any ``gain`` in ``(0, 4)`` is exact-to-Q15 AND SATURATES on overload exactly
    like ``multiply_const_cc`` (GR clips to the Q15 rails; the block pins to
    ``0x7FFF``/``0x8000``). The ``<<2`` restore is two ``ADD R0,R0`` doublings, each
    setting V on signed overflow (INV-13's doubling variant — S=2 is small so this is
    leaner than a bias-and-shift). All terms share ``x``'s sign (gain>0) so the sum is
    monotonic — the FIRST V means true overload, and the rail is pinned to ``x``'s
    sign via ``0x7FFF + signbit`` (``x`` survives in its state reg, MULQ writing R0).
    Each rail (I, Q) runs the identical MULQ + saturating restore; one cell, complex
    packet out (``WRITE yi; WRITE yq; JUMP`` — INV-17).

    Interface: complex (xi @R0, xq @R1) in, complex (yi, yq) out. One cell.
    """
    CATEGORY = "signal_conditioning"
    TAGS = ["gain", "complex", "scaler", "signal_conditioning"]

    _interface = BlockInterface(entry_address=1, input_registers=[0, 1],
                                output_registers=[0, 1])

    HEAD_SHIFT = 2                 # gain < 4  ⇒  store gain/2^2, restore with <<2
    SAT_POS_Q15 = 0x7FFF           # 0x7FFF + signbit ⇒ +0x7FFF / -0x8000

    def __init__(self, name: str, gain: float = 1.0):
        """Args: name; gain (real multiplier applied to both I and Q, 0 < gain < 4)."""
        g = float(gain)
        if not (0.0 < g < 4.0):
            raise ValueError(f"ComplexGainBlock gain must be in (0, 4); got {gain}")
        super().__init__(name, gain=g)
        self._gain = g
        # Store gain/4 in Q15 — representable for all 0 < gain < 4; restored <<2.
        self._gain_q = float_to_q15(g / 4.0) & 0xFFFF

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def gain(self) -> float:
        return self._gain

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def _scale_rail(self, inreg: str, tag: str, emit: str) -> str:
        """Compute ``clamp(gain * R{inreg})`` in R0 (saturating), then WRITE it
        (``emit`` = the rail's ``{write:...}`` placeholder). One rail, self-contained.

        INV-13 saturating restore, DOUBLING variant (S=2 ⇒ only 2 doublings). MULQ by
        ``gain/4`` gives ``p = x·gain/4`` — always in Q15 range (no wrap). The rail's
        SIGN is captured from ``p`` (into ``sgn``) BEFORE the doublings, because a
        doubling that overflows destroys ``p``. Then double twice to restore ``·4``;
        each ``ADD R0,R0`` sets V on signed overflow. All terms share ``p``'s sign, so
        the sum is MONOTONIC — the FIRST V means true overload, pinned to ``p``'s sign
        via ``0x7FFF + signbit``.

        CONTROL FLOW — conditional (LOCAL) branches ONLY, never an unconditional
        ``GOTO`` (the assembler compiles a GOTO near a ``{write}``/``{jump}`` as an
        EXTERNAL output jump, not a local branch — verified via the sim trace: a
        ``GOTO`` over the sat block fired as a trigger and fell through, DOUBLE-emitting
        the rail. INV-13's placeholder-miscompile, sharpened). The overflow paths and
        the in-range path CONVERGE at ``_wr_{tag}`` (a REAL ``MOVE R0,R0`` anchor — a
        branch target must be a real instruction, not the ``emit`` placeholder) with
        the result in R0, then fall through to the single ``emit``. ``sgn`` is one
        scratch reg reused by both (sequential) rails."""
        return "\n".join([
            f"    MULQ R{inreg}, R{{data:gain}}",          # R0 = p = x*gain/4 (in range)
            f"    MOVE R{{state:sgn}}, R0",                # keep p for its sign
            f"    ADD R0, R0",                             # ·2 (V on overflow)
            f"    BR.V _sat_{tag}",                        # overflow → saturate (local)
            f"    ADD R0, R0",                             # ·4 (V on overflow)
            f"    BR.NV _wr_{tag}",                        # in range → result in R0 (local)
            f"  _sat_{tag}:",
            f"    SHR R{{state:sgn}}, #15",                # R0 = sign bit of p
            f"    ADD R0, R{{data:satpos}}",               # R0 = 0x7FFF + bit
            f"  _wr_{tag}:",
            f"    MOVE R0, R0",                            # anchor (real instr); R0 = result
            f"    {emit}",                                 # write R0 = clamp(x·gain)
        ])

    def build_cell_programs(self) -> Dict[Any, CellProgram]:
        # Each rail multiplies its INPUT reg directly (xi@R0, xq@R1 — a single read),
        # clamps in R0, and WRITEs its rail. The two WRITEs + the JUMP form the complex
        # packet the build de-interleaves (yi, yq). Rails run sequentially, no GOTO —
        # rail Q falls through from rail I, the JUMP closes the packet.
        i_rail = self._scale_rail("{in:xi}", "i", "{write:yi}")
        q_rail = self._scale_rail("{in:xq}", "q", "{write:yq}")
        return {0: CellProgram(
            inputs=[Port("xi", register=0), Port("xq", register=1)],
            outputs=[Port("yi"), Port("yq"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("gain", self._gain_q, address=2),
                  DataWord("satpos", self.SAT_POS_Q15, address=3)],
            state=[StateVar("sgn")],
            assembly_template="""\
start:
""" + i_rail + "\n" + q_rail + """
    {jump:trig}
""",
        )}

    def process_reference(self, input_samples: np.ndarray):
        """Q15-exact: out = SAT( MULQ(in, gain/4) doubled x2 ), per rail (the cell)."""
        def s16(v):
            v = int(v) & 0xFFFF
            return v - 0x10000 if v & 0x8000 else v

        def mulq(a, b):
            # Hardware MULQ = arithmetic (A*B) >> 15 — TRUNCATE toward -inf, NO
            # round-to-nearest bias (verified via sim trace: (a*4096)>>15 == a>>3).
            # Python >> on a signed int floors, matching the arithmetic shift.
            return s16((s16(a) * s16(b)) >> 15)

        def sat_double(p, x):
            # clamp(p << 2) via two ``ADD R0,R0`` doublings; on the first signed-16
            # overflow pin to x's sign rail (0x7FFF + signbit). Exactly the cell.
            acc = s16(p)
            for _ in range(self.HEAD_SHIFT):
                acc2 = acc + acc
                if acc2 > 32767 or acc2 < -32768:       # signed overflow (V)
                    signbit = (s16(x) & 0xFFFF) >> 15
                    return s16((self.SAT_POS_Q15 + signbit) & 0xFFFF)
                acc = acc2
            return acc

        arr = np.asarray(input_samples)
        if np.iscomplexobj(arr):
            iq = [(float_to_q15(c.real), float_to_q15(c.imag)) for c in arr]
        elif arr.ndim == 2 and arr.shape[1] == 2:
            iq = [(int(a) & 0xFFFF, int(b) & 0xFFFF) for a, b in arr]
        else:
            iq = [(float_to_q15(float(v)), 0) for v in arr]
        out = []
        for (xi, xq) in iq:
            row = []
            for x in (xi, xq):
                p = mulq(x, self._gain_q)
                row.append(sat_double(p, x) & 0xFFFF)
            out.append((row[0], row[1]))
        return np.array(out, dtype=np.int32)

    def reset(self):
        pass
