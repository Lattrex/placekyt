# SPDX-License-Identifier: GPL-3.0-or-later
"""Two-stream complex Add / Subtract — :class:`AddCCBlock`, :class:`SubCCBlock`."""
import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock


class _TwoStreamAddSubCC(KyttarBlock):
    """Shared 2-cell SATURATING two-complex-stream combiner.

        yi[n] = sat(ai[n] (+|-) bi[n]),   yq[n] = sat(aq[n] (+|-) bq[n])

    GNU Radio's ``add_cc`` / ``sub_cc`` sum/difference N complex streams
    elementwise (memoryless, delay 0, strict per-sample pairing — pinned LIVE
    against GR 2026-08-16). The math is SEPARABLE PER RAIL: two independent
    saturating 2-operand adds (the proven AddBlock ``BR.NV`` restore idiom),
    which is what makes the historical "4-operand wall" (4 external operands
    from 2 sources) tractable on 32-word cells.

    TOPOLOGY (2 cells, ONE landing cell — the port map exposes external inputs
    only from the landing cell, so all four operand ports live there):

      * ``rail_i`` (landing + I-rail): external ports ``ai``@R0, ``aq``@R1
        (stream a's complex pair) and ``bi``@R2, ``bq``@R3 (stream b's pair).
        Each stream arrives as ONE complex packet (multi-WRITE + one JUMP), so
        the cell sees TWO triggers per sample in ANY order — its first entry is
        the proven AddBlock COUNTING JOIN (toggle counter, single-fire on the
        second arrival; ``entries[0]`` so both external packet JUMPs and the
        importer's ``_elect_join_triggers`` land on it). On fire it computes
        ``yi = sat(ai (+|-) bi)`` and forwards ``(yi, aq, bq)`` + one trigger to
        ``rail_q``.
      * ``rail_q`` (Q-rail + emit): computes ``yq = sat(aq (+|-) bq)`` and emits
        the ``(yi, yq)`` COMPLEX PACKET (``{write:yi}; {write:yq}; {jump:trig}``
        — the INV-17 form; the cell keeps >1 free word so the build's fan-out
        transform can insert its extra JUMP).

    Each rail saturates to the Q15 range (the bare ALU would WRAP — a sign
    flip); GR's float combiner keeps accumulating, so the GR-equivalence gate
    drives IN-RANGE stimulus (|a+-b| < 1 per rail) and the saturation behaviour
    is gated against the exact ``process_reference_q15``. The overflow restore
    is the AddBlock rail verbatim: save the first operand, op (sets V), on
    overflow rebuild the rail from the saved operand's sign (``SHR #15; ADD
    satpos`` -> +0x7FFF / -0x8000); forward ``BR.NV`` skip only (no GOTO onto a
    placeholder, INV-13).

    Hardware deviations from blocks.add_cc / blocks.sub_cc:
    -----------------------------------------------------------------------
    HW-DEVIATION (32-word cell budget): ``num_inputs`` is pinned to 2. A third
    complex stream needs 2 more operand registers, a 3-arm join and a longer
    saturating chain, which overflow the landing cell (measured: >40 words).
    The block RAISES on ``num_inputs != 2`` (never silently clamps).

    Interface: 4 external input registers (ai, aq, bi, bq = R0..R3) on the
    landing cell, complex output pair (yi, yq). Memoryless -> delay 0.
    """

    _OP = "ADD"            # overridden to "SUB" by SubCCBlock
    _SIGN = +1             # +1 add, -1 sub (references)
    SAT_POS_Q15 = 0x7FFF
    MAX_INPUTS = 2         # HW limit — see class docstring

    def __init__(self, name: str, num_inputs: int = 2):
        n = int(num_inputs)
        if n != self.MAX_INPUTS:
            # HARDWARE DEVIATION: a >2-stream complex combiner does not fit the
            # 32-word landing cell (6 operand regs + 3-arm join + chained
            # saturating adds per rail); raise loudly per INV-0.
            raise ValueError(
                f"HARDWARE LIMIT: num_inputs={n} unsupported — the two-complex-"
                f"stream combiner is pinned to num_inputs=2 (4 operand registers "
                f"+ counting join + per-rail saturating add fill the 32-word "
                f"landing cell; a third stream cannot fit).")
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
        op = self._OP
        cells = {}

        # (1) rail_i — the landing cell. Counting join over the two stream
        # packets (the AddBlock tail VERBATIM: toggle counter, fires on the
        # SECOND arrival in any order; R0 is both the ALU result register and
        # the ai landing register, so the tail save/restores it), then the
        # I-rail saturating op, then the (yi, aq, bq) forward to rail_q.
        # ``join`` is entries[0]: every external delivery (and resolved_io /
        # the importer's counting-join election) enters the tail.
        cells["rail_i"] = CellProgram(
            inputs=[Port("ai", register=0), Port("aq", register=1),
                    Port("bi", register=2), Port("bq", register=3)],
            outputs=[Port("yi_f"), Port("aq_f"), Port("bq_f"), Port("trig")],
            entries=[EntryPoint("join")],
            data=[DataWord("satpos", self.SAT_POS_Q15, address=4),
                  DataWord("one", 1, address=5)],
            state=[StateVar("asav"), StateVar("jcnt"), StateVar("jsav")],
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
                "    MOVE R{state:asav}, R0\n"          # save ai (restore sign)
                f"    {op} R0, R{{in:bi}}\n"            # yi = ai (op) bi, sets V
                "    BR.NV +3\n"                        # no overflow -> skip restore
                "    MOVE R0, R{state:asav}\n"          # overflow: rail from ai sign
                "    SHR R0, #15\n"
                "    ADD R0, R{data:satpos}\n"
                "    {write:yi_f}\n"                    # yi -> rail_q
                "    MOVE R0, R{in:aq}\n"
                "    {write:aq_f}\n"                    # aq -> rail_q
                "    MOVE R0, R{in:bq}\n"
                "    {write:bq_f}\n"                    # bq -> rail_q
                "    {jump:trig}\n"),
        )

        # (2) rail_q — Q-rail + the block's complex output cell. ONE trigger
        # (rail_i's forward burst), so no join. Computes yq from the forwarded
        # (aq, bq) with the same saturating rail, then emits the (yi, yq)
        # complex PACKET (INV-17 form; >1 word left free for the build's
        # fan-out JUMP insertion — asserted by the block's budget test).
        cells["rail_q"] = CellProgram(
            inputs=[Port("yi_in", register=0), Port("aq_in", register=1),
                    Port("bq_in", register=2)],
            outputs=[Port("yi"), Port("yq"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("satpos", self.SAT_POS_Q15, address=3)],
            state=[StateVar("yis"), StateVar("asav"), StateVar("yqs")],
            assembly_template=(
                "default:\n"
                "    MOVE R{state:yis}, R0\n"           # stash yi before any ALU
                "    MOVE R{state:asav}, R{in:aq_in}\n" # save aq (restore sign)
                f"    {op} R{{state:asav}}, R{{in:bq_in}}\n"  # yq = aq (op) bq
                "    BR.NV +3\n"
                "    MOVE R0, R{state:asav}\n"
                "    SHR R0, #15\n"
                "    ADD R0, R{data:satpos}\n"
                "    MOVE R{state:yqs}, R0\n"           # stash yq
                "    MOVE R0, R{state:yis}\n"
                "    {write:yi}\n"                      # complex packet: yi ...
                "    MOVE R0, R{state:yqs}\n"
                "    {write:yq}\n"                      # ... then yq ...
                "    {jump:trig}\n"),                   # ... ONE trigger
        )
        return cells

    # ------------------------------------------------------- multi-cell wiring
    def _chain(self):
        return ["rail_i", "rail_q"]

    def internal_connections(self):
        return [("rail_i", "yi_f", "rail_q", "yi_in"),
                ("rail_i", "aq_f", "rail_q", "aq_in"),
                ("rail_i", "bq_f", "rail_q", "bq_in")]

    def internal_jumps(self):
        return [("rail_i", "trig", "rail_q", "default")]

    def output_cell_ids(self):
        return ["rail_q"]

    def default_layout(self):
        # 2x1, I/O co-located on the top edge (INV-8/14): the landing cell and
        # the emitting cell sit side by side, both bus-reachable.
        return {"rail_i": (0, 0, "east"), "rail_q": (1, 0, "east")}

    # -------------------------------------------------------------- reference
    @staticmethod
    def _s16(v):
        v = int(v) & 0xFFFF
        return v - 0x10000 if v >= 0x8000 else v

    def _sat_op(self, a_w: int, b_w: int) -> int:
        """One saturating rail: sat(a (op) b) exactly as the cell computes it."""
        r = self._s16(a_w) + self._SIGN * self._s16(b_w)
        return max(-32768, min(32767, r)) & 0xFFFF

    def process_reference_q15(self, ai, aq, bi, bq) -> tuple:
        """Bit-exact predictor of the on-chip per-rail SATURATING add/sub.
        Takes four equal-length uint16 Q15 word lists (stream a's and stream
        b's I/Q rails); returns ``(yi_words, yq_words)``."""
        yi = [self._sat_op(a, b) for a, b in zip(ai, bi)]
        yq = [self._sat_op(a, b) for a, b in zip(aq, bq)]
        return yi, yq

    def process_reference(self, a_stream, b_stream) -> np.ndarray:
        """Float reference: elementwise ``a (+|-) b`` of two complex streams,
        each rail clipped to the Q15 range (GR's float combiner is unbounded —
        compare in range, saturation via :meth:`process_reference_q15`)."""
        a = np.asarray(a_stream, dtype=np.complex128)
        b = np.asarray(b_stream, dtype=np.complex128)
        s = a + self._SIGN * b
        lo, hi = -1.0, 32767.0 / 32768.0
        return (np.clip(s.real, lo, hi)
                + 1j * np.clip(s.imag, lo, hi)).astype(np.complex64)


class AddCCBlock(_TwoStreamAddSubCC):
    """Two-stream complex adder — drop-in for GNU Radio ``blocks.add_cc``
    (``out = a + b`` elementwise). Saturating Q15 per rail. See
    :class:`_TwoStreamAddSubCC`."""
    CATEGORY = "math_operators"
    TAGS = ["add", "add_cc", "complex", "sum", "math_operators"]
    _OP = "ADD"
    _SIGN = +1


class SubCCBlock(_TwoStreamAddSubCC):
    """Two-stream complex subtractor — drop-in for GNU Radio ``blocks.sub_cc``
    (``out = a - b`` elementwise). Saturating Q15 per rail; NON-commutative
    (the swapped-streams mutation is a tested corruption). See
    :class:`_TwoStreamAddSubCC`."""
    CATEGORY = "math_operators"
    TAGS = ["subtract", "sub_cc", "complex", "difference", "math_operators"]
    _OP = "SUB"
    _SIGN = -1
