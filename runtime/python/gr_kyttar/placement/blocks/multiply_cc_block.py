# SPDX-License-Identifier: GPL-3.0-or-later
"""Two-stream complex multiply — :class:`MultiplyCCBlock`."""
import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock


class MultiplyCCBlock(KyttarBlock):
    """TRUE two-EXTERNAL-complex-stream product — drop-in for GNU Radio
    ``blocks.multiply_cc`` (2 complex streams, elementwise)::

        y[n] = a[n] * b[n]
        yi   = sat(ai*bi - aq*bq)
        yq   = sat(ai*bq + aq*bi)

    Unlike :class:`AddCCBlock` the math is NOT separable per rail: each output
    rail needs operands from BOTH rails of BOTH streams (the two CROSS-terms
    ``-aq*bq`` on I and ``+aq*bi`` on Q are the rotation), so ALL FOUR operands
    must be co-resident where the products are formed. And unlike
    :class:`MultiplyConstComplex` BOTH factors are signals — there is no
    constant to pre-scale, so the coefficient-headroom trick does not apply.

    HEADROOM STRATEGY (derived, not tuned): both factors are Q15 signals, so
    every product ``MULQ(x, y)`` is itself in the Q15 range ``[-1, 1)`` — no
    product prescale is needed at all. Only the per-rail COMBINE of two
    full-scale products can leave range (``|p1 -+ p2|`` can reach 2), and a
    single 16-bit ADD/SUB overflow is exactly recoverable from the V flag: the
    AddCCBlock saturating-rail idiom restores the rail from the MINUEND's sign
    (SUB overflow => sign(a) = -sign(b) => result sign = sign(a); ADD overflow
    => signs equal => result sign = either operand's). So the rails are
    computed at FULL scale and saturate exactly like GR's float product would
    clip — zero headroom shift (S=0), zero headroom error amplification. The
    derived Q15 floor is q15_quant_floor(op_count=2, head_shift=0) = 3 LSB
    (two truncating MULQs per rail + comparison quantization); measured worst
    error is 1-2 LSB.

    TOPOLOGY (2 cells, ONE landing cell — the port map / ``resolved_io`` /
    ``_iq_sibling`` machinery exposes external inputs only from the landing
    cell, and ``_elect_join_triggers`` resolves ONE join address, so all four
    operand ports MUST live there — the AddCCBlock lesson):

      * ``prods`` (landing + products): external ports ``ai``@R0, ``aq``@R1
        (stream a's complex pair) and ``bi``@R2, ``bq``@R3 (stream b's pair).
        Each stream arrives as ONE complex packet (multi-WRITE + one JUMP), so
        the cell sees TWO triggers per sample in ANY order — its first entry
        is the proven AddBlock COUNTING JOIN (toggle counter, single-fire on
        the second arrival; ``entries[0]`` so external packet JUMPs and the
        importer's counting-join election land on it). On fire it snapshots
        the operands into state (each input register is read EXACTLY ONCE —
        the ComplexMixer stale-latch trap; ``ai`` rides the join's own
        ``jsav`` save, for free) and forms the four full-scale products
        ``P1=ai*bi, P2=aq*bq, P3=ai*bq, P4=aq*bi``, forwarding all four + one
        trigger to ``combine``. THIS is the 4-operand co-residency plan: the
        operands meet ONCE, in the landing cell's state, and only products
        travel.
      * ``combine`` (rails + emit): ``yi = sat(P1 - P2)``, ``yq = sat(P3 +
        P4)`` — the AddCC saturating rail verbatim (save the minuend, op sets
        V, on overflow rebuild from the saved minuend's sign: ``SHR #15; ADD
        satpos`` -> +0x7FFF / -0x8000; ``BR.NV`` skip only, conditional
        branches only in the exit cell). Emits the ``(yi, yq)`` COMPLEX
        PACKET (``{write:yi}; {write:yq}; {jump:trig}`` — the INV-17 form;
        the cell keeps >1 free word for the build's fan-out JUMP).

    Q15 WRAP CORNER (documented, the MultiplyBlock (-1)*(-1) corner): the ONLY
    input pair whose product overflows MULQ is exactly (-1.0)*(-1.0) = +1.0,
    unrepresentable in Q15, which the MULQ datapath WRAPS to -1.0. A rail fed
    such a product deviates from GR at that measure-zero corner (e.g. a = b =
    -1-1j: true yq = +2 -> GR-clip +full, chip P3 = P4 = wrap(-1), yq pins to
    -full). The bit-exact gate pins this corner against the block's OWN
    ``process_reference_q15`` (which models the wrap); the GR-equivalence
    stimulus is bounded |a|, |b| <= 0.7 per rail (products <= 0.49, rails
    <= 0.98 — strictly in range, no saturation, no wrap).

    Hardware deviations from blocks.multiply_cc:
    -----------------------------------------------------------------------
    HW-DEVIATION (32-word cell budget): ``num_inputs`` is pinned to 2. A
    third complex stream turns the product into a chained complex multiply —
    a second full 4-MULQ/2-rail stage plus 2 more operand registers and a
    3-arm join, far beyond the 32-word landing cell. The block RAISES on
    ``num_inputs != 2`` (never silently clamps).

    Interface: 4 external input registers (ai, aq, bi, bq = R0..R3) on the
    landing cell, complex output pair (yi, yq). Memoryless -> delay 0.
    """

    CATEGORY = "math_operators"
    TAGS = ["multiply", "multiply_cc", "complex", "product", "math_operators"]

    SAT_POS_Q15 = 0x7FFF
    MAX_INPUTS = 2         # HW limit — see class docstring

    def __init__(self, name: str, num_inputs: int = 2):
        n = int(num_inputs)
        if n != self.MAX_INPUTS:
            # HARDWARE DEVIATION: a >2-stream complex product is a CHAINED
            # complex multiply (a second 4-MULQ stage + 2 more operand regs +
            # a 3-arm join) — nowhere near one 32-word landing cell. Raise
            # loudly per INV-0.
            raise ValueError(
                f"HARDWARE LIMIT: num_inputs={n} unsupported — the two-complex-"
                f"stream product is pinned to num_inputs=2 (4 operand registers "
                f"+ counting join + 4 products fill the landing cell; a third "
                f"stream needs a whole second complex-multiply stage).")
        super().__init__(name, num_inputs=n)
        self._num_inputs = n
        self._interface = BlockInterface(
            entry_address=1, input_registers=[0, 1, 2, 3], output_registers=[0])

    @property
    def num_inputs(self) -> int:
        return self._num_inputs

    @property
    def cell_count(self) -> int:
        return 2

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    # ------------------------------------------------------------------ build
    def build_cell_programs(self) -> dict:
        cells = {}

        # (1) prods — the landing cell. Counting join over the two stream
        # packets (the AddBlock tail VERBATIM: toggle counter, fires on the
        # SECOND arrival in any order; R0 is the ai landing register, so the
        # tail's jsav save/restore doubles as the ai snapshot). On fire it
        # snapshots aq/bi/bq (one read each — the stale-latch trap; the next
        # sample's packets may land in the input registers while we compute)
        # and forms the four full-scale Q15 products, forwarded as one burst
        # (4 WRITEs + 1 JUMP) to combine.
        cells["prods"] = CellProgram(
            inputs=[Port("ai", register=0), Port("aq", register=1),
                    Port("bi", register=2), Port("bq", register=3)],
            outputs=[Port("p1"), Port("p2"), Port("p3"), Port("p4"),
                     Port("trig")],
            entries=[EntryPoint("join")],
            data=[DataWord("one", 1, address=4)],
            state=[StateVar("jcnt"), StateVar("jsav"),
                   StateVar("aqs"), StateVar("bis"), StateVar("bqs")],
            assembly_template=(
                "join:\n"
                "    MOVE R{state:jsav}, R0\n"
                "    MOVE R0, R{data:one}\n"
                "    SUB R0, R{state:jcnt}\n"
                "    BR.Z +3\n"
                "    MOVE R{state:jcnt}, R0\n"
                "    MOVE R0, R{state:jsav}\n"
                "    HALT\n"
                "    MOVE R{state:jcnt}, R0\n"
                "    MOVE R0, R{state:jsav}\n"
                "default:\n"
                "    MOVE R{state:aqs}, R{in:aq}\n"       # snapshot: 1 read each
                "    MOVE R{state:bis}, R{in:bi}\n"
                "    MOVE R{state:bqs}, R{in:bq}\n"
                "    MULQ R{state:jsav}, R{state:bis}\n"  # P1 = ai*bi
                "    {write:p1}\n"
                "    MULQ R{state:aqs}, R{state:bqs}\n"   # P2 = aq*bq
                "    {write:p2}\n"
                "    MULQ R{state:jsav}, R{state:bqs}\n"  # P3 = ai*bq
                "    {write:p3}\n"
                "    MULQ R{state:aqs}, R{state:bis}\n"   # P4 = aq*bi
                "    {write:p4}\n"
                "    {jump:trig}\n"),
        )

        # (2) combine — the two saturating rails + the block's output cell.
        # ONE trigger (prods' burst), so no join. yq FIRST (stashed), then yi
        # so R0 holds yi at the packet head; each rail is the AddCC saturating
        # idiom (minuend saved to state, ALU result lands in R0, V-overflow
        # rebuilds the rail from the minuend's sign). EXIT CELL: conditional
        # branches only (a GOTO here would be rewritten by the output-handoff
        # pass — the RMS lesson); >1 free word for the INV-17 fan-out JUMP.
        cells["combine"] = CellProgram(
            inputs=[Port("p1", register=0), Port("p2", register=1),
                    Port("p3", register=2), Port("p4", register=3)],
            outputs=[Port("yi"), Port("yq"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("satpos", self.SAT_POS_Q15, address=4)],
            state=[StateVar("p1s"), StateVar("p3s"), StateVar("yqs")],
            assembly_template=(
                "default:\n"
                "    MOVE R{state:p1s}, R0\n"             # save p1 (ALU clobbers R0)
                "    MOVE R{state:p3s}, R{in:p3}\n"       # save p3 (restore sign)
                "    ADD R{state:p3s}, R{in:p4}\n"        # yq = p3 + p4, sets V
                "    BR.NV +3\n"                          # no overflow -> skip restore
                "    MOVE R0, R{state:p3s}\n"             # overflow: rail from p3 sign
                "    SHR R0, #15\n"
                "    ADD R0, R{data:satpos}\n"
                "    MOVE R{state:yqs}, R0\n"             # stash yq
                "    SUB R{state:p1s}, R{in:p2}\n"        # yi = p1 - p2, sets V
                "    BR.NV +3\n"
                "    MOVE R0, R{state:p1s}\n"             # overflow: rail from p1 sign
                "    SHR R0, #15\n"
                "    ADD R0, R{data:satpos}\n"
                "    {write:yi}\n"                        # complex packet: yi ...
                "    MOVE R0, R{state:yqs}\n"
                "    {write:yq}\n"                        # ... then yq ...
                "    {jump:trig}\n"),                     # ... ONE trigger
        )
        return cells

    # ------------------------------------------------------- multi-cell wiring
    def _chain(self):
        return ["prods", "combine"]

    def internal_connections(self):
        return [("prods", "p1", "combine", "p1"),
                ("prods", "p2", "combine", "p2"),
                ("prods", "p3", "combine", "p3"),
                ("prods", "p4", "combine", "p4")]

    def internal_jumps(self):
        return [("prods", "trig", "combine", "default")]

    def output_cell_ids(self):
        return ["combine"]

    def default_layout(self):
        # 2x1, I/O co-located on the top edge (INV-8/14): the landing cell and
        # the emitting cell sit side by side, both bus-reachable.
        return {"prods": (0, 0, "east"), "combine": (1, 0, "east")}

    # -------------------------------------------------------------- reference
    @staticmethod
    def _s16(v):
        v = int(v) & 0xFFFF
        return v - 0x10000 if v >= 0x8000 else v

    @classmethod
    def _mulq(cls, a_w: int, b_w: int) -> int:
        """Hardware MULQ = arithmetic (A*B) >> 15, truncating toward -inf.
        WRAPS at exactly (-1.0)*(-1.0) (2^30 >> 15 = 0x8000 = -1.0)."""
        return cls._s16((cls._s16(a_w) * cls._s16(b_w)) >> 15)

    @classmethod
    def _sat_combine(cls, p_min: int, p_other: int, sign: int) -> int:
        """One saturating rail: sat(p_min (+|-) p_other) exactly as the cell
        computes it — on 16-bit V-overflow, pin to the MINUEND's sign rail."""
        r = p_min + sign * p_other
        if r > 32767 or r < -32768:
            signbit = (p_min & 0xFFFF) >> 15
            return (cls.SAT_POS_Q15 + signbit) & 0xFFFF
        return r & 0xFFFF

    def process_reference_q15(self, ai, aq, bi, bq) -> tuple:
        """Bit-exact predictor of the on-chip complex product (truncating
        MULQ incl. the (-1)*(-1) wrap, saturating per-rail combine). Takes
        four equal-length uint16 Q15 word lists (stream a's and stream b's
        I/Q rails); returns ``(yi_words, yq_words)``."""
        yi, yq = [], []
        for a_i, a_q, b_i, b_q in zip(ai, aq, bi, bq):
            p1 = self._mulq(a_i, b_i)
            p2 = self._mulq(a_q, b_q)
            p3 = self._mulq(a_i, b_q)
            p4 = self._mulq(a_q, b_i)
            yi.append(self._sat_combine(p1, p2, -1))
            yq.append(self._sat_combine(p3, p4, +1))
        return yi, yq

    def process_reference(self, a_stream, b_stream) -> np.ndarray:
        """Float reference: elementwise ``a * b`` of two complex streams, each
        rail clipped to the Q15 range (GR's float product is unbounded —
        compare in range; saturation + the MULQ wrap corner are gated via
        :meth:`process_reference_q15`)."""
        a = np.asarray(a_stream, dtype=np.complex128)
        b = np.asarray(b_stream, dtype=np.complex128)
        p = a * b
        lo, hi = -1.0, 32767.0 / 32768.0
        return (np.clip(p.real, lo, hi)
                + 1j * np.clip(p.imag, lo, hi)).astype(np.complex64)
