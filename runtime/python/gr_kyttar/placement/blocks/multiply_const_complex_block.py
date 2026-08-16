# SPDX-License-Identifier: GPL-3.0-or-later
"""MultiplyConstComplex — see :class:`MultiplyConstComplex`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict, List, Tuple, Any
from ._base import KyttarBlock, BlockInterface, float_to_q15


class MultiplyConstComplex(KyttarBlock):
    """TRUE complex-constant multiply — mirrors GNU Radio ``blocks.multiply_const_cc(k)``
    fed a **complex** constant ``k = re + im·j``.

    Unlike :class:`ComplexGainBlock` (which scales BOTH rails by the SAME *real*
    constant — a pure magnitude change, NO rotation), this block multiplies the
    complex stream by a full complex constant, so it SCALES **and** ROTATES the
    constellation::

        out = in · k = (xi + xq·j)·(re + im·j)
        yi = xi·re − xq·im
        yq = xi·im + xq·re

    This is the genuine ``multiply_const_cc`` (GR's ``constant`` is a
    ``gr_complex``): each output rail is a DIFFERENCE / SUM of two products, and the
    two CROSS-terms (``−xq·im`` on I, ``+xi·im`` on Q) are what produce the rotation.

    Q15 headroom — the EXACT ComplexGainBlock bug class (INV-13 / INV-25). The
    datapath is Q15 ``[-1, 1)`` but ``re``/``im`` may exceed 1 (``|re|, |im| < 2``),
    AND each rail sums TWO half-scale products whose difference/sum could reach
    magnitude ~2 → a naïve accumulator would WRAP mid-rail, and a naïve restore would
    WRAP on overload where GR SATURATES. Both are avoided by storing the coefficients
    pre-scaled by ``2^-2`` and restoring with ONE saturating ``<<2``::

        re4 = Q15(re / 4)      im4 = Q15(im / 4)          # |·| < 1/2  ⇒  fit Q15
        p1  = MULQ(xi, re4) = xi·re/4   (|p1| < 1/2, in range — no wrap)
        p2  = MULQ(xq, im4) = xq·im/4   (|p2| < 1/2, in range — no wrap)
        acc = p1 − p2                    (|acc| < 1  ⇒  the 16-bit SUB CANNOT overflow)
        yi  = SAT(acc << 2)             = clamp(xi·re − xq·im), pinned to ±full-scale

    The ``|re|,|im| < 2`` range is the KEY over a single-product scaler: two products
    at the ``/4`` scale are each ``< 1/2`` in magnitude, so their sum/difference
    stays STRICTLY in ``[-1, 1)`` and the 16-bit ADD/SUB never wraps (a wider
    coefficient — ``/4`` at ``|re|<4`` like ComplexGain — would let the two
    half-scale products sum PAST full-scale and wrap the accumulator, the trap this
    range closes). The ONLY place a true overdrive overflows is the final saturating
    ``<<2`` (two ``ADD R0,R0`` doublings; the FIRST signed overflow pins the rail to
    ``acc``'s sign via ``0x7FFF + signbit``). So any ``|re|, |im| < 2`` is
    exact-to-Q15 in range AND SATURATES on overload exactly like
    ``multiply_const_cc`` — it CLIPS, never wraps.

    ``acc``'s sign == the true rail's sign (``acc = yi/4`` exactly in fixed point),
    so pinning to ``acc``'s sign gives the correct rail; sign is captured BEFORE the
    doublings (a doubling that overflows destroys ``acc``).

    TWO CELLS (the full complex product does not fit ONE cell's 32-word budget: the
    four MULQs + two accumulates + TWO saturating restores + two anchored writes
    exceed it). The split is a clean feed-forward wavefront, NO feedback / no
    reconvergent fan-in — so NO serialize-lock is needed (INV-19/20 do not apply):

      * ``mul``  — landing cell. Saves ``xi`` (R0 is clobbered by the first MULQ),
        forms the four ``/4`` products and the two IN-RANGE accumulators
        ``acc_i = xi·re/4 − xq·im/4`` and ``acc_q = xi·im/4 + xq·re/4`` (each in
        ``[-1, 1)`` — the SUB/ADD cannot wrap), and forwards BOTH to ``sat``.
      * ``sat`` — output cell. Restores each rail with the SATURATING ``<<2`` and
        emits the complex packet (``WRITE yi; WRITE yq; JUMP`` — INV-17). Being the
        block's LAST + output cell, its trig self-terminates.

    HARDWARE RANGE (documented, INV-0): ``|re|, |im| < 2``. A complex constant with a
    part ``≥ 2`` in magnitude is not representable in this ``/4``-headroom Q15 datapath
    (two half-scale products would sum past full-scale and wrap the accumulator), so
    the block RAISES rather than silently mis-scale. ``|k|`` up to ``2√2 ≈ 2.83`` is
    supported — ample overload for a drop-in ``multiply_const_cc``.

    Interface: complex (xi @R0, xq @R1) in, complex (yi, yq) out. Memoryless (group
    delay 0).
    """
    CATEGORY = "signal_conditioning"
    TAGS = ["multiply", "complex", "rotate", "scaler", "signal_conditioning"]

    _interface = BlockInterface(entry_address=1, input_registers=[0, 1],
                                output_registers=[0, 1])

    HEAD_SHIFT = 2                 # store k/2^2, restore with a saturating <<2
    SAT_POS_Q15 = 0x7FFF           # 0x7FFF + signbit ⇒ +0x7FFF / -0x8000
    RANGE = 2.0                    # |re|, |im| < 2 (two /4 products stay in range)

    _CELL_IDS = ["mul", "sat"]

    def __init__(self, name: str, re: float = 0.7, im: float = 0.5):
        """Args: name; re, im — the real/imag parts of the complex constant
        ``k = re + im·j`` GR ``multiply_const_cc`` multiplies by. ``|re|, |im| < 2``."""
        r = float(re)
        m = float(im)
        if not (abs(r) < self.RANGE and abs(m) < self.RANGE):
            raise ValueError(
                f"MultiplyConstComplex re/im must satisfy |re|,|im| < {self.RANGE}; "
                f"got re={re}, im={im}")
        super().__init__(name, re=r, im=m)
        self._re = r
        self._im = m
        # Store re/4, im/4 in Q15 — |·| < 1/2 for all |re|,|im| < 2; restored <<2.
        self._re_q = float_to_q15(r / 4.0) & 0xFFFF
        self._im_q = float_to_q15(m / 4.0) & 0xFFFF

    @property
    def cell_count(self) -> int:
        return 2

    @property
    def re(self) -> float:
        return self._re

    @property
    def im(self) -> float:
        return self._im

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    # ------------------------------------------------------------------ cells
    def build_cell_programs(self) -> Dict[Any, CellProgram]:
        # --- mul: form the two IN-RANGE accumulators and forward them ---------
        # xi lands at R0 but the first MULQ writes R0, so xi is saved to state
        # first; xq lands at R1 and MULQ never writes R1, so xq survives both rails.
        # MULQ writes R0 and does NOT clobber its operands, so xis/xq/coeffs persist.
        #   acc_i = xi·re/4 − xq·im/4   (both products < 1/2 ⇒ acc_i in [-1,1), no wrap)
        #   acc_q = xi·im/4 + xq·re/4   (same)
        mul_cell = CellProgram(
            inputs=[Port("xi", register=0), Port("xq", register=1)],
            outputs=[Port("acc_i"), Port("acc_q"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("re_q", self._re_q, address=2),
                  DataWord("im_q", self._im_q, address=3)],
            state=[StateVar("xis"), StateVar("sgn")],
            assembly_template="""\
start:
    MOVE R{state:xis}, R{in:xi}
    MULQ R{in:xq}, R{data:im_q}
    MOVE R{state:sgn}, R0
    MULQ R{state:xis}, R{data:re_q}
    SUB R0, R{state:sgn}
    {write:acc_i}
    MULQ R{in:xq}, R{data:re_q}
    MOVE R{state:sgn}, R0
    MULQ R{state:xis}, R{data:im_q}
    ADD R0, R{state:sgn}
    {write:acc_q}
    {jump:trig}
""",
        )

        # --- sat: saturating <<2 restore per rail, emit the complex packet ----
        # acc_i lands at R0, acc_q at R1. Each rail doubles its acc twice; the FIRST
        # signed overflow (V) pins the rail to acc's sign via 0x7FFF + signbit. The
        # sign is captured (si/sq) BEFORE the doublings (an overflowing double
        # destroys acc). LOCAL conditional branches only — no GOTO near {write}
        # (INV-13 / the ComplexGain lesson). Both paths converge at a REAL anchor
        # (MOVE R0,R0) before the emit (a branch target cannot be the placeholder).
        # yi/yq + JUMP form the INV-17 complex packet the build de-interleaves.
        sat_cell = CellProgram(
            inputs=[Port("acc_i", register=0), Port("acc_q", register=1)],
            outputs=[Port("yi"), Port("yq"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("satpos", self.SAT_POS_Q15, address=2)],
            state=[StateVar("si"), StateVar("sq")],
            assembly_template="""\
start:
    MOVE R{state:si}, R{in:acc_i}
    ADD R0, R0
    BR.V _si
    ADD R0, R0
    BR.NV _wi
  _si:
    SHR R{state:si}, #15
    ADD R0, R{data:satpos}
  _wi:
    MOVE R0, R0
    {write:yi}
    MOVE R{state:sq}, R{in:acc_q}
    MOVE R0, R{in:acc_q}
    ADD R0, R0
    BR.V _sq
    ADD R0, R0
    BR.NV _wq
  _sq:
    SHR R{state:sq}, #15
    ADD R0, R{data:satpos}
  _wq:
    MOVE R0, R0
    {write:yq}
    {jump:trig}
""",
        )

        return {"mul": mul_cell, "sat": sat_cell}

    def internal_connections(self) -> List[Tuple[Any, str, Any, str]]:
        """Feed-forward: mul forwards both accumulators to sat (no feedback)."""
        return [
            ("mul", "acc_i", "sat", "acc_i"),
            ("mul", "acc_q", "sat", "acc_q"),
        ]

    def internal_jumps(self) -> List[Tuple[Any, str, Any, str]]:
        """Linear chain: mul triggers sat; sat is the LAST + output cell, so its
        trig self-terminates (``__terminate__``) — the build must not default it."""
        return [
            ("mul", "trig", "sat", "default"),
            ("sat", "trig", "__terminate__", "default"),
        ]

    def output_cell_id(self) -> Any:
        """The complex packet (yi, yq) egresses from the ``sat`` cell."""
        return "sat"

    def default_layout(self) -> Dict[Any, Tuple[int, int, str]]:
        """Compact 2×1 fold, I/O co-located on the SAME (west/bus) edge: ``mul`` (the
        input landing) at (0,0) emitting EAST to ``sat`` at (1,0); ``sat`` emits its
        complex packet on its routed output face. Both cells sit on row 0, so the
        bus tapping the west edge reaches the input (mul) and the output (sat) is one
        cell over — a tight, routable fold (INV-8; even single row, ≤8 across INV-9)."""
        return {
            "mul": (0, 0, "east"),
            "sat": (1, 0, "east"),
        }

    # -------------------------------------------------------------- reference
    def process_reference(self, input_samples: np.ndarray):
        """Q15-exact: per rail out = SAT( (MULQ(xi,A/4) ± MULQ(xq,B/4)) << 2 ), the
        exact 2-cell arithmetic (truncating MULQ, in-range ADD/SUB, saturating <<2)."""
        def s16(v):
            v = int(v) & 0xFFFF
            return v - 0x10000 if v & 0x8000 else v

        def mulq(a, b):
            # Hardware MULQ = arithmetic (A*B) >> 15 — TRUNCATE toward -inf (Python >>
            # on a signed int floors, matching the arithmetic shift). No round bias.
            return s16((s16(a) * s16(b)) >> 15)

        def sat_shift(acc):
            # clamp(acc << 2) via two ``ADD R0,R0`` doublings; on the first signed-16
            # overflow pin to acc's sign rail (0x7FFF + signbit). Exactly the cell.
            a = s16(acc)
            for _ in range(self.HEAD_SHIFT):
                a2 = a + a
                if a2 > 32767 or a2 < -32768:            # signed overflow (V)
                    signbit = (s16(acc) & 0xFFFF) >> 15
                    return s16((self.SAT_POS_Q15 + signbit) & 0xFFFF)
                a = a2
            return a

        arr = np.asarray(input_samples)
        if np.iscomplexobj(arr):
            iq = [(float_to_q15(c.real), float_to_q15(c.imag)) for c in arr]
        elif arr.ndim == 2 and arr.shape[1] == 2:
            iq = [(int(a) & 0xFFFF, int(b) & 0xFFFF) for a, b in arr]
        else:
            iq = [(float_to_q15(float(v)), 0) for v in arr]

        out = []
        for (xi, xq) in iq:
            # mul cell: the two in-range accumulators (SUB/ADD cannot wrap).
            acc_i = s16((mulq(xi, self._re_q) - mulq(xq, self._im_q)) & 0xFFFF)
            acc_q = s16((mulq(xi, self._im_q) + mulq(xq, self._re_q)) & 0xFFFF)
            # sat cell: the saturating <<2 restore per rail.
            yi = sat_shift(acc_i)
            yq = sat_shift(acc_q)
            out.append((yi & 0xFFFF, yq & 0xFFFF))
        return np.array(out, dtype=np.int32)

    def reset(self):
        pass
