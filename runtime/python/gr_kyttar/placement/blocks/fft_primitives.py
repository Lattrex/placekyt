# SPDX-License-Identifier: GPL-3.0-or-later
"""Radix-2 FFT primitives — :class:`R2ButterflyBlock` / :class:`TwiddleMultiplyBlock`.

One shared module (the ``cordic_blocks`` / ``add_sub_cc`` pattern): the two
streaming radix-2 DIF FFT primitive blocks plus the module-level BIT-EXACT
integer reference helpers they share.  A future composite streaming-FFT block
imports the cell-program builders and the reference helpers from here, the way
``AGCCCBlock`` reuses the CORDIC engine — keep the helpers dependency-free.

Numerics (PINNED by the completed FFT numeric design spike; implement, do not
redesign):

* **Butterfly scaling** = unconditional ``>>1`` per stage with
  **round-half-to-even** (RHE).  Rounding bias is the dominant coherent-tone
  error term: on an on-bin full-scale sine at N=128 the measured transform SNR
  is floor 68.6 dB -> half-up 72.5 dB -> mixed 81.1 dB -> **RHE 90.1 dB** —
  half-up's +0.25 LSB bias is a deterministic function of the data LSB pattern
  and concentrates into structured bins on coherent tones.  The 16-bit-safe
  fabric mapping (the 17-bit sum ``v = a ± b`` is NEVER materialized)::

      sum leg :  k    = floor(a/2) + floor(b/2) + ((a AND b) AND 1)
      diff leg:  k    = floor(a/2) − floor(b/2) − ((NOT a AND b) AND 1)
      both    :  corr = ((a XOR b) AND k) AND 1      ; v odd AND k odd
                 out  = k + corr                     ; round-half-to-even

  ``floor(x/2)`` is one ``MULQ/MACQ/MSUQ`` by ``0x4000`` (Q15 one-half — the
  product shift is arithmetic, i.e. floor), so every intermediate is provably
  in 16-bit range (see the per-step bound comments in the cell programs).

* **Saturating combines are MANDATORY** even in-contract: the spike measured
  ~4e-5 half-LSB RHE-fuzz saturation events at exact full scale.  On the
  butterfly itself the one reachable overflow is the DIFFERENCE leg's exact
  tie ``a=+0x7FFF, b=-0x8000`` (v = 65535, k = 32767 odd, corr = 1 ->
  32768): the cell clamps it to +0x7FFF via the V flag.  The SUM leg provably
  cannot overflow (max v = 65534 is even -> corr = 0, |k| <= 32768 with
  k = -32768 only for even/corr-0 v), which is asserted by an exhaustive-corner
  test, not assumed.

* **Twiddles** are stored ``round(32768*x)`` (round-half-even, full scale) and
  **trivial twiddles are special-cased structurally, never multiplied**:
  ``W = 1`` passes through untouched; ``W = -j`` is a rail swap + saturating
  negate.  Representing 1.0 as 0x7FFF and multiplying everything costs a
  measured 2-6 dB on every signal class AND spends multiplier work on trivial
  rotations.  No non-trivial angle rounds to ±32768, so every stored
  coefficient is a legal full-scale Q15 word; a user value that would quantize
  to ±32768 without being exactly trivial RAISES (never clamps).

* **Complex multiply** = 4 MULQ + 2 saturating combines in the pinned
  MultiplyCC ordering (``p1 = xi*c, p2 = xq*d, p3 = xi*d, p4 = xq*c;
  yi = sat(p1-p2), yq = sat(p3+p4)``).  Every product is unconditionally in
  range (|c|,|d| <= 0x7FFF); only the two combines can leave range and each is
  one V-restore saturate.  The 3-multiply (Karatsuba) form is REJECTED: its
  precomputed constants ``c+d`` / ``d-c`` reach ±46341 (±sqrt(2) in Q15) —
  structurally unrepresentable without a precision-losing rescale.
"""
from typing import Any, Dict, List, Tuple

import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock

Q15_ONE = 32768
Q15_MAX = 32767
Q15_MIN = -32768
HALF_Q15 = 0x4000          # 0.5 in Q15: MULQ by it == arithmetic >>1 (floor)
SAT_POS_Q15 = 0x7FFF

# Twiddle-table sentinel: 0x8000 (-1.0) never appears as a legal NON-trivial
# stored coefficient (the block RAISES on any value that would quantize there),
# so the C table uses it to mark a trivial entry; the D table then disambiguates
# identity (0x0000) from -j (0x8000, sign bit set — tested with one SHR #15).
TRIVIAL_SENTINEL = 0x8000
KIND_MUL = "mul"
KIND_ID = "id"             # W = 1: pass through untouched
KIND_MJ = "mj"             # W = -j: (re, im) -> (im, sat(-re))


# ---------------------------------------------------------------------------
# Bit-exact integer reference helpers (shared with the future composite FFT).
# These mirror the fabric ops exactly and are the module's golden primitives.
# ---------------------------------------------------------------------------

def s16(v: int) -> int:
    """Signed 16-bit view of a word."""
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def u16(v: int) -> int:
    return int(v) & 0xFFFF


def mulq(a: int, b: int) -> int:
    """Fabric MULQ: floor((a*b) / 2^15) — per-product floor truncation."""
    return u16((s16(a) * s16(b)) >> 15)


def sat_q15(v: int) -> int:
    """Saturating clamp of a true integer value to the Q15 word range."""
    return u16(max(Q15_MIN, min(Q15_MAX, int(v))))


def rhe_half_sum(a: int, b: int) -> int:
    """Round-half-to-even (a+b)/2 with the mandatory saturating combine.

    Computed on the TRUE 17-bit sum (numpy/int arithmetic) — the independent
    check that the cells' 16-bit-safe decomposition is exact is a test concern,
    but this reference IS that decomposition's defined result:
    ``k = floor(v/2); out = k + (v odd AND k odd)``, clamped to Q15 (the sum
    leg provably never needs the clamp; it is kept for symmetry/safety)."""
    v = s16(a) + s16(b)
    k = v >> 1                      # floor(v/2), exact on Python ints
    return sat_q15(k + ((v & k) & 1))


def rhe_half_diff(a: int, b: int) -> int:
    """Round-half-to-even (a-b)/2 with the mandatory saturating combine.

    The one reachable butterfly saturation: a=+0x7FFF, b=-0x8000 -> v=65535,
    k=32767 (odd), corr=1 -> 32768 -> clamps to +0x7FFF."""
    v = s16(a) - s16(b)
    k = v >> 1
    return sat_q15(k + ((v & k) & 1))


def sat_combine(p_min: int, p_other: int, sign: int) -> int:
    """One V-restore saturating rail: ``sat(p_min (+|-) p_other)`` exactly as
    the cell computes it — on 16-bit signed overflow the rail is rebuilt from
    the MINUEND's sign (the proven AddCC/MultiplyCC idiom)."""
    r = s16(p_min) + sign * s16(p_other)
    if r > Q15_MAX or r < Q15_MIN:
        signbit = (u16(p_min)) >> 15
        return u16(SAT_POS_Q15 + signbit)
    return u16(r)


def butterfly_ref(a_word: int, b_word: int) -> Tuple[int, int]:
    """One REAL rail of the scaled DIF butterfly: (RHE((a+b)/2), RHE((a-b)/2))."""
    return rhe_half_sum(a_word, b_word), rhe_half_diff(a_word, b_word)


def quantize_twiddle(w: complex) -> Tuple[str, int, int]:
    """Build-time twiddle classification + quantization (the pinned contract).

    Returns ``(kind, c, d)``: trivial entries are detected from the EXACT
    parameter values (``1`` -> identity, ``-1j`` -> swap + saturating negate)
    and carry sentinel table words; anything else stores
    ``c = round(32768*Re), d = round(32768*Im)`` (round-half-even).  A
    non-trivial value that would quantize to ±32768 on either rail RAISES —
    it is unrepresentable at full scale in Q15 (this includes W = -1; no DIF
    stage table contains it: stage twiddle angles live in [0, pi))."""
    w = complex(w)
    if w == 1:
        return KIND_ID, TRIVIAL_SENTINEL, 0x0000
    if w == -1j:
        return KIND_MJ, TRIVIAL_SENTINEL, TRIVIAL_SENTINEL
    c = int(np.round(w.real * Q15_ONE))
    d = int(np.round(w.imag * Q15_ONE))
    if not (-Q15_MAX <= c <= Q15_MAX) or not (-Q15_MAX <= d <= Q15_MAX):
        raise ValueError(
            f"HARDWARE LIMIT: twiddle {w!r} quantizes to (c={c}, d={d}) — a "
            f"non-trivial twiddle coefficient must fit [-32767, +32767] in Q15 "
            f"(±1.0 rails are unrepresentable at full scale; exactly 1 and "
            f"exactly -1j are the structurally special-cased trivial values).")
    return KIND_MUL, u16(c), u16(d)


def twiddle_cmul_ref(xi: int, xq: int, kind: str, c: int, d: int
                     ) -> Tuple[int, int]:
    """Bit-exact one-sample twiddle multiply (the spike's ``cmul_tw``):

    * identity: pass through untouched.
    * -j: ``(re, im) -> (im, sat(-re))`` (negate saturates -32768 -> +32767).
    * non-trivial: 4 floor-MULQs + 2 V-restore saturating combines, pinned
      ordering ``yi = sat(p1 - p2)``, ``yq = sat(p3 + p4)``.
    """
    if kind == KIND_ID:
        return u16(xi), u16(xq)
    if kind == KIND_MJ:
        return u16(xq), sat_q15(-s16(xi))
    p1 = mulq(xi, c)
    p2 = mulq(xq, d)
    p3 = mulq(xi, d)
    p4 = mulq(xq, c)
    return sat_combine(p1, p2, -1), sat_combine(p3, p4, +1)


# ---------------------------------------------------------------------------
# R2ButterflyBlock
# ---------------------------------------------------------------------------

class R2ButterflyBlock(KyttarBlock):
    """Radix-2 DIF butterfly with the pinned unconditional scale-by-2 —
    two complex streams in (a, b), two complex streams out::

        sum[n]  = RHE((a[n] + b[n]) / 2)     per rail (I and Q)
        diff[n] = RHE((a[n] - b[n]) / 2)     per rail

    RHE = round-half-to-even, computed 16-bit-safe (the 17-bit sum is never
    materialized — see the module docstring for the pinned formulas and why
    RHE is worth up to +18 dB over half-up on coherent tones).  The DIFFERENCE
    rails carry the mandatory saturating combine (the single reachable
    overflow is the exact tie ``a=+0x7FFF, b=-0x8000`` -> clamps +0x7FFF);
    the SUM rails provably cannot overflow (gated by an exhaustive corner
    test, not assumed).

    There is no GNU Radio counterpart block; the golden is the module's
    bit-exact integer reference (:func:`rhe_half_sum` / :func:`rhe_half_diff`),
    itself pinned to the FFT design spike's integer model, cross-checked in the
    test suite against an independent 17-bit-true-value RHE implementation.

    TOPOLOGY (8 cells, 2x4 serpentine fold, fully SERIAL trigger chain — no
    reconvergent fan-in, no feedback, so no serialize-LOCK is needed):

      * ``pair`` (landing): the AddCC counting join over the two source
        packets (a = (ai, aq), b = (bi, bq), any arrival order); forwards
        (ai, bi) one hop to ``sumi`` and (aq, bq) three hops down the chain
        to ``sumq``.
      * ``sumi`` / ``diffi``: the I-rail RHE sum / difference; each rail cell
        computes one output word (~11 / ~16 instructions) and the results ride
        multi-hop writes along the fold to ``sum_out``.
      * ``sumq`` / ``diffq``: the Q rails (sumq re-forwards its operand
        snapshots to diffq; diffq's result rides through ``relay``).
      * ``relay``: a real store-and-forward data cell (a trigger-only relay
        does not reliably re-fire — the NCO lesson).
      * ``sum_out``: dual-face output cell (the FLL ``fanout`` idiom): emits
        the (si, sq) complex packet on the tap face, forwards (di, dq) + the
        trigger on the internal face.  NOTHING transits this cell — every
        crossing value is delivered into its registers, so the face flip can
        never mis-forward another sample's word.
      * ``diff_out``: the exit cell; emits the (di, dq) complex packet.

    Interface: 4 external input registers (ai, aq, bi, bq = R0..R3) on the
    landing cell; two external complex output pairs ``(so_i, so_q)`` (sum) and
    ``(do_i, do_q)`` (difference).  Memoryless -> delay 0.
    """

    CATEGORY = "math_operators"
    TAGS = ["fft", "butterfly", "radix2", "dif", "complex", "math_operators"]

    # output_registers=[0, 1]: each external output is a COMPLEX PAIR — the
    # build's complex-egress patches key on >1 output registers (INV-6/11;
    # the FLL/order-4-Costas precedent).
    _interface = BlockInterface(
        entry_address=1, input_registers=[0, 1, 2, 3], output_registers=[0, 1])

    def __init__(self, name: str):
        super().__init__(name)

    @property
    def cell_count(self) -> int:
        return 8

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    # ------------------------------------------------------------------ build
    @staticmethod
    def _rhe_sum_lines() -> str:
        """The 11-instruction 16-bit-safe RHE sum leg: R0 <- RHE((a+b)/2).

        Bounds (all intermediates 16-bit-safe):
          (a AND b) AND 1            in {0, 1}
          + floor(a/2) (MACQ half)   in [-16384, 16384]
          + floor(b/2) (MACQ half)   = k = floor((a+b)/2) in [-32768, 32767]
          corr = ((a XOR b) AND k) AND 1
          k + corr: k = 32767 requires v in {65534, 65535}; max v = 65534 is
          EVEN -> corr = 0, so the sum leg cannot overflow (corner-gated)."""
        return (
            "    MOVE R{state:as_}, R{in:a}\n"
            "    MOVE R{state:bs}, R{in:b}\n"
            "    AND R{state:as_}, R{state:bs}\n"
            "    AND R0, R{data:one}\n"
            "    MACQ R{state:as_}, R{data:half}\n"
            "    MACQ R{state:bs}, R{data:half}\n"
            "    MOVE R{state:tk}, R0\n"
            "    XOR R{state:as_}, R{state:bs}\n"
            "    AND R0, R{state:tk}\n"
            "    AND R0, R{data:one}\n"
            "    ADD R0, R{state:tk}\n")

    @staticmethod
    def _rhe_diff_lines() -> str:
        """The 16-instruction RHE difference leg with the mandatory saturating
        combine: R0 <- sat(RHE((a-b)/2)).

        k = floor(a/2) - c0 - floor(b/2) with c0 = (NOT a AND b) AND 1; every
        partial stays in [-32768, 32767].  The final ADD sets V exactly at the
        one reachable tie (k = +32767 odd, corr = 1); the restore is R0 <- k
        (= +32767 = the saturated value) — a one-instruction exact clamp."""
        return (
            "    MOVE R{state:as_}, R{in:a}\n"
            "    MOVE R{state:bs}, R{in:b}\n"
            "    NOT R{state:as_}\n"
            "    AND R0, R{state:bs}\n"
            "    AND R0, R{data:one}\n"
            "    MOVE R{state:tk}, R0\n"
            "    MULQ R{state:as_}, R{data:half}\n"
            "    SUB R0, R{state:tk}\n"
            "    MSUQ R{state:bs}, R{data:half}\n"
            "    MOVE R{state:tk}, R0\n"
            "    XOR R{state:as_}, R{state:bs}\n"
            "    AND R0, R{state:tk}\n"
            "    AND R0, R{data:one}\n"
            "    ADD R0, R{state:tk}\n"
            "    BR.NV +1\n"
            "    MOVE R0, R{state:tk}\n")

    def build_cell_programs(self) -> Dict[str, CellProgram]:
        cells: Dict[str, CellProgram] = {}

        # (1) pair — the landing cell.  AddCC counting join VERBATIM (toggle
        # counter, fires on the SECOND packet in any order; R0 is the ai
        # landing register, jsav doubles as the ai snapshot).  On fire it
        # forwards (ai, bi) @1 to sumi and (aq, bq) @3 to sumq (each input
        # register read exactly once).
        cells["pair"] = CellProgram(
            inputs=[Port("ai", register=0), Port("aq", register=1),
                    Port("bi", register=2), Port("bq", register=3)],
            outputs=[Port("ai_f"), Port("bi_f"), Port("aq_f"), Port("bq_f"),
                     Port("trig")],
            entries=[EntryPoint("join")],
            data=[DataWord("one", 1, address=4)],
            state=[StateVar("jcnt", register=5), StateVar("jsav", register=6)],
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
                "    {write:ai_f}\n"            # R0 = ai -> sumi
                "    MOVE R0, R{in:bi}\n"
                "    {write:bi_f}\n"            # -> sumi
                "    MOVE R0, R{in:aq}\n"
                "    {write:aq_f}\n"            # -> sumq (3 hops down the fold)
                "    MOVE R0, R{in:bq}\n"
                "    {write:bq_f}\n"            # -> sumq
                "    {jump:trig}\n"),
        )

        def _rail_cell(sum_leg: bool, out_port: str, fwd_ops: bool) -> CellProgram:
            """One RHE rail cell.  ``fwd_ops`` re-forwards the (a, b) operand
            snapshots one hop (sumi -> diffi, sumq -> diffq)."""
            body = (self._rhe_sum_lines() if sum_leg
                    else self._rhe_diff_lines())
            tail = "    {write:%s}\n" % out_port
            outs = [Port(out_port)]
            if fwd_ops:
                tail += ("    MOVE R0, R{state:as_}\n"
                         "    {write:a_f}\n"
                         "    MOVE R0, R{state:bs}\n"
                         "    {write:b_f}\n")
                outs += [Port("a_f"), Port("b_f")]
            tail += "    {jump:trig}\n"
            outs.append(Port("trig"))
            return CellProgram(
                inputs=[Port("a", register=1), Port("b", register=2)],
                outputs=outs,
                entries=[EntryPoint("default")],
                data=[DataWord("one", 1, address=3),
                      DataWord("half", HALF_Q15, address=4)],
                state=[StateVar("as_", register=5), StateVar("bs", register=6),
                       StateVar("tk", register=7)],
                assembly_template="default:\n" + body + tail,
            )

        # NOTE on the XOR after the operand snapshots were CONSUMED by the leg
        # arithmetic: as_/bs are STATE registers (stable, re-readable) — only
        # INPUT registers are single-read.  In the sum leg as_ holds a and bs
        # holds b throughout; the diff leg's NOT overwrites nothing (NOT
        # writes R0)... both legs re-read as_/bs freely.
        cells["sumi"] = _rail_cell(True, "si_f", True)     # si -> sum_out @5
        cells["diffi"] = _rail_cell(False, "di_f", False)  # di -> sum_out @4
        cells["sumq"] = _rail_cell(True, "sq_f", True)     # sq -> sum_out @3
        cells["diffq"] = _rail_cell(False, "dq_f", False)  # dq -> relay @1

        # (6) relay — REAL store-and-forward data cell (a trigger-only relay
        # does not reliably re-fire; the NCO INV-20 lesson).
        cells["relay"] = CellProgram(
            inputs=[Port("dq", register=1)],
            outputs=[Port("dq_f"), Port("trig")],
            entries=[EntryPoint("default")],
            assembly_template=(
                "default:\n"
                "    MOVE R0, R{in:dq}\n"
                "    {write:dq_f}\n"
                "    {jump:trig}\n"),
        )

        # (7) sum_out — dual-face output cell (the FLL fanout idiom): forward
        # (di, dq) + trigger on the INTERNAL face first, then flip to the tap
        # face and emit the (so_i, so_q) complex packet — the tap writes are
        # the LAST writes (the 2-rail port-egress patch steers both rails).
        # Every crossing value lands in a REGISTER here (nothing transits the
        # cell), so the face flip cannot mis-forward another sample's word.
        cells["sum_out"] = CellProgram(
            inputs=[Port("si", register=1), Port("sq", register=2),
                    Port("di", register=3), Port("dq", register=4)],
            outputs=[Port("di_f"), Port("dq_f"), Port("trig"),
                     Port("so_i"), Port("so_q"), Port("so_trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("face_int", 3, address=5, is_face=True),
                  DataWord("face_tap", 1, address=6, is_face=True)],
            state=[],
            assembly_template=(
                "default:\n"
                "    MOVE [FACE], R{data:face_int}\n"
                "    MOVE R0, R{in:di}\n"
                "    {write:di_f}\n"
                "    MOVE R0, R{in:dq}\n"
                "    {write:dq_f}\n"
                "    {jump:trig}\n"
                "    MOVE [FACE], R{data:face_tap}\n"
                "    MOVE R0, R{in:si}\n"
                "    {write:so_i}\n"
                "    MOVE R0, R{in:sq}\n"
                "    {write:so_q}\n"
                "    {jump:so_trig}\n"),
        )

        # (8) diff_out — the exit cell: the (do_i, do_q) complex packet
        # (INV-17 form; plenty of free words for the fan-out JUMP).
        cells["diff_out"] = CellProgram(
            inputs=[Port("di", register=1), Port("dq", register=2)],
            outputs=[Port("do_i"), Port("do_q"), Port("do_trig")],
            entries=[EntryPoint("default")],
            assembly_template=(
                "default:\n"
                "    MOVE R0, R{in:di}\n"
                "    {write:do_i}\n"
                "    MOVE R0, R{in:dq}\n"
                "    {write:do_q}\n"
                "    {jump:do_trig}\n"),
        )
        return cells

    # ------------------------------------------------------- multi-cell wiring
    def _chain(self):
        return ["pair", "sumi", "diffi", "sumq", "diffq", "relay",
                "sum_out", "diff_out"]

    def internal_connections(self):
        return [
            ("pair", "ai_f", "sumi", "a"),
            ("pair", "bi_f", "sumi", "b"),
            ("pair", "aq_f", "sumq", "a"),
            ("pair", "bq_f", "sumq", "b"),
            ("sumi", "si_f", "sum_out", "si"),
            ("sumi", "a_f", "diffi", "a"),
            ("sumi", "b_f", "diffi", "b"),
            ("diffi", "di_f", "sum_out", "di"),
            ("sumq", "sq_f", "sum_out", "sq"),
            ("sumq", "a_f", "diffq", "a"),
            ("sumq", "b_f", "diffq", "b"),
            ("diffq", "dq_f", "relay", "dq"),
            ("relay", "dq_f", "sum_out", "dq"),
            ("sum_out", "di_f", "diff_out", "di"),
            ("sum_out", "dq_f", "diff_out", "dq"),
        ]

    def internal_jumps(self):
        return [
            ("pair", "trig", "sumi", "default"),
            ("sumi", "trig", "diffi", "default"),
            ("diffi", "trig", "sumq", "default"),
            ("sumq", "trig", "diffq", "default"),
            ("diffq", "trig", "relay", "default"),
            ("relay", "trig", "sum_out", "default"),
            ("sum_out", "trig", "diff_out", "default"),
        ]

    def output_cell_id(self):
        """SINGULAR — the build's carries-handoffs flag: ``sum_out`` is a
        NON-last output cell that also emits internal handoffs (di/dq + the
        chain trigger), so its external writes must be patched ALONE (the
        last-N-writes tap patch — the order-4 Costas ``qpd`` idiom), never
        every WRITE in the cell."""
        return "sum_out"

    def output_cell_ids(self):
        # TWO physically-separate complex output cells (the ComplexFIR i3/q3
        # plural-output precedent): sum first (GRC output index 0), diff second.
        return ["sum_out", "diff_out"]

    def output_face_addr(self):
        """``sum_out`` is a DUAL-FACE output cell: its tap writes fire on the
        in-program ``MOVE [FACE], R{face_tap}`` flip.  Declaring the face
        word's address (6) lets the build rewrite it to the routed egress
        direction (the phantom-route guard); the build applies it only to the
        block-level output cell (sum_out), never to diff_out (a plain
        single-face cell steered by its ``fwd_face``)."""
        return 6

    def default_layout(self):
        # 2x4 serpentine (INV-8/14, even column count): down column 0, up
        # column 1; I/O co-located on the top edge (pair at (0,0), the diff
        # exit at (1,0), the sum tap one cell below at (1,1)).
        return {
            "pair": (0, 0, "south"),
            "sumi": (0, 1, "south"),
            "diffi": (0, 2, "south"),
            "sumq": (0, 3, "east"),
            "diffq": (1, 3, "north"),
            "relay": (1, 2, "north"),
            "sum_out": (1, 1, "north"),
            "diff_out": (1, 0, "east"),
        }

    # -------------------------------------------------------------- reference
    def process_reference_q15(self, ai, aq, bi, bq) -> Tuple[list, list, list, list]:
        """Bit-exact predictor: per sample, per rail,
        ``(si, sq, di, dq) = (RHE((a+b)/2) [IQ], sat(RHE((a-b)/2)) [IQ])``.
        Takes four equal-length uint16 Q15 word lists; returns four word lists."""
        si = [rhe_half_sum(a, b) for a, b in zip(ai, bi)]
        sq = [rhe_half_sum(a, b) for a, b in zip(aq, bq)]
        di = [rhe_half_diff(a, b) for a, b in zip(ai, bi)]
        dq = [rhe_half_diff(a, b) for a, b in zip(aq, bq)]
        return si, sq, di, dq

    def process_reference(self, a_stream, b_stream) -> Tuple[np.ndarray, np.ndarray]:
        """Float reference: ``((a+b)/2, (a-b)/2)`` elementwise, each rail
        clipped to the Q15 range.  The bit-exact rounding contract lives in
        :meth:`process_reference_q15`."""
        a = np.asarray(a_stream, dtype=np.complex128)
        b = np.asarray(b_stream, dtype=np.complex128)
        lo, hi = -1.0, 32767.0 / 32768.0

        def _clip(z):
            return (np.clip(z.real, lo, hi)
                    + 1j * np.clip(z.imag, lo, hi)).astype(np.complex64)

        return _clip((a + b) / 2.0), _clip((a - b) / 2.0)


# ---------------------------------------------------------------------------
# TwiddleMultiplyBlock
# ---------------------------------------------------------------------------

class TwiddleMultiplyBlock(KyttarBlock):
    """Complex multiply by a per-sample TABLE-SELECTED Q15 twiddle constant::

        y[n] = x[n] * twiddles[n mod P],    P = len(twiddles)

    The streaming radix-2 DIF FFT stage's twiddle rotator: each stage owns a
    periodic table ``W_N^{j*2^s}``, applied to the difference-path stream in
    slot order.  Twiddles are stored ``round(32768*x)`` (round-half-even, full
    scale) and TRIVIAL entries are special-cased structurally at build time
    (the pinned contract — see the module docstring):

      * ``W == 1`` exactly: the sample passes through untouched (no multiply).
      * ``W == -1j`` exactly: rail swap + saturating negate
        (``(re, im) -> (im, sat(-re))``; -32768 negates to +32767).
      * anything else: 4 MULQ + 2 V-restore saturating combines in the pinned
        p1-p2 / p3+p4 MultiplyCC ordering.

    There is no GNU Radio counterpart block; the golden is the module's
    bit-exact reference (:func:`twiddle_cmul_ref` over the build-time table
    from :func:`quantize_twiddle`).

    TOPOLOGY (6 cells, 2x3 serpentine, fully SERIAL — every sample transits
    every cell in order whatever its kind, so the trivial and non-trivial
    paths have EQUAL chain length: no reconvergent fan-in, no overtaking, no
    serialize-LOCK):

      * ``fetch_c`` (landing, xi@R1/xq@R2): holds the C table + the slot
        pointer; one LOAD-indirect per sample; forwards c @1 and (xi, xq) @2.
      * ``fetch_d``: holds the D table + its own (lockstep) pointer.
      * ``steer``: the ONE kind dispatch — C sentinel (0x8000) selects the
        trivial path; both paths forward the operands, and the path identity
        travels as WHICH ENTRY each downstream cell is jumped at (no
        downstream re-dispatch except rail_i's 1-instruction d-sign test).
      * ``prods``: entries ``mul`` (4 MULQ) / ``triv`` (pass xi, xq, d).
      * ``rail_i``: entries ``mul`` (yi = sat(p1-p2), forwards p3/p4) /
        ``triv`` (d sign bit picks identity vs -j; forwards the pass values).
      * ``emit`` (exit): entries ``mul`` / ``id`` / ``mj``; emits the (yi, yq)
        complex packet (INV-17 form, >=2 free words for the fan-out JUMP).

    Hardware deviations (no GR counterpart — the deviations are vs the ideal
    "any list" contract):
    -----------------------------------------------------------------------
    HW-DEVIATION (32-word cell budget + LOAD 5-bit table wall): the period P
    is capped at :data:`MAX_PERIOD` entries (each of the two table cells holds
    P words + its program).  P for every N=16 stage is <= 8; bigger stages use
    the documented octant-fold growth path (two <=16-entry quarter tables) —
    an EXTENSION for the composite FFT, deliberately NOT part of this block.
    The block RAISES on P out of range (never truncates).
    HW-DEVIATION (Q15 full-scale wall): a NON-trivial twiddle with a rail that
    quantizes to ±32768 (e.g. W = -1, or |Re/Im| rounding to 1.0) RAISES —
    only exactly 1 and exactly -1j have structural (non-multiply) forms.

    Interface: one complex input (xi@R1, xq@R2 on the landing cell), one
    complex output pair (yi, yq).  Stateful only in the slot counter (period
    phase); per-sample 1:1, delay 0.
    """

    CATEGORY = "math_operators"
    TAGS = ["fft", "twiddle", "rotator", "complex", "multiply",
            "math_operators"]

    MAX_PERIOD = 12

    _interface = BlockInterface(
        entry_address=1, input_registers=[1, 2], output_registers=[0])

    # NOTE: the default is a LIST (the MapBB `map` precedent) so the GRC
    # importer's type-matched literal coercion accepts a .grc list value; it
    # is normalized immediately and never mutated.
    def __init__(self, name: str, twiddles=[1]):  # noqa: B006
        tw = self._normalize(twiddles)
        if not (1 <= len(tw) <= self.MAX_PERIOD):
            # HARDWARE DEVIATION: the two in-cell twiddle tables share their
            # 32-word cells with the fetch programs; beyond MAX_PERIOD the
            # C-table cell overflows.  Raise loudly per INV-0.
            raise ValueError(
                f"HARDWARE LIMIT: twiddles period P={len(tw)} unsupported — "
                f"the in-cell twiddle tables hold 1..{self.MAX_PERIOD} entries "
                f"(32-word cell = P table words + the fetch program; the "
                f"octant-fold growth path for large stages is a composite-FFT "
                f"extension, not part of this block).")
        super().__init__(name, twiddles=[complex(w) for w in tw])
        self._twiddles = [complex(w) for w in tw]
        # Build-time classification (raises on unrepresentable rails).
        self._table = [quantize_twiddle(w) for w in self._twiddles]

    @staticmethod
    def _normalize(twiddles) -> List[complex]:
        """Accept a list of complex numbers or (re, im) pairs."""
        out: List[complex] = []
        for w in list(twiddles):
            if isinstance(w, (tuple, list)) and len(w) == 2:
                out.append(complex(float(w[0]), float(w[1])))
            else:
                out.append(complex(w))
        return out

    @property
    def twiddles(self) -> List[complex]:
        """The user's twiddle list (period P), as normalized complex values."""
        return list(self._twiddles)

    @property
    def table(self) -> List[Tuple[str, int, int]]:
        """The resolved build-time table: ``(kind, c_word, d_word)`` per slot."""
        return list(self._table)

    @property
    def period(self) -> int:
        return len(self._table)

    @property
    def cell_count(self) -> int:
        return 6

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    # ------------------------------------------------------------------ build
    def build_cell_programs(self) -> Dict[str, CellProgram]:
        P = self.period
        cells: Dict[str, CellProgram] = {}

        c_words = [c for (_k, c, _d) in self._table]
        d_words = [d for (_k, _c, d) in self._table]

        def _fetch_cell(table_words, base, in_ports, fwd_lines, fwd_outs):
            """A table cell: LOAD the slot word, forward it + the operands,
            advance the slot pointer with period wrap (base..base+P-1)."""
            data = [DataWord(f"t{i}", u16(w), address=base + i)
                    for i, w in enumerate(table_words)]
            data += [DataWord("one", 1, address=base + P),
                     DataWord("pend", base + P, address=base + P + 1),
                     DataWord("pbase", base, address=base + P + 2)]
            ptr_reg = base + P + 3
            return CellProgram(
                inputs=in_ports,
                outputs=fwd_outs,
                entries=[EntryPoint("default")],
                data=data,
                state=[StateVar("ptr", register=ptr_reg, initial_value=base)],
                assembly_template=(
                    "default:\n"
                    "    LOAD R{state:ptr}\n"
                    "    {write:t_f}\n"
                    + fwd_lines +
                    "    ADD R{state:ptr}, R{data:one}\n"
                    "    MOVE R{state:ptr}, R0\n"
                    "    CMP R0, R{data:pend}\n"
                    "    BR.NZ +1\n"
                    "    MOVE R{state:ptr}, R{data:pbase}\n"
                    "    {jump:trig}\n"),
            )

        # (1) fetch_c — landing (xi@R1, xq@R2; R0 stays the free accumulator,
        # INV-33): c -> fetch_d @1; (xi, xq) skip TWO hops down the fold to
        # steer (the words follow the authored faces, so no colinearity
        # constraint beyond the fold itself).
        cells["fetch_c"] = _fetch_cell(
            c_words, base=3,
            in_ports=[Port("xi", register=1), Port("xq", register=2)],
            fwd_lines=("    MOVE R0, R{in:xi}\n"
                       "    {write:xi_f}\n"
                       "    MOVE R0, R{in:xq}\n"
                       "    {write:xq_f}\n"),
            fwd_outs=[Port("t_f"), Port("xi_f"), Port("xq_f"), Port("trig")])

        # (2) fetch_d — the D table (lockstep pointer); c rides through.
        cells["fetch_d"] = _fetch_cell(
            d_words, base=2,
            in_ports=[Port("c", register=1)],
            fwd_lines=("    MOVE R0, R{in:c}\n"
                       "    {write:c_f}\n"),
            fwd_outs=[Port("t_f"), Port("c_f"), Port("trig")])

        # (3) steer — the ONE kind dispatch (C sentinel).  Path identity
        # travels as the downstream ENTRY (t_mul vs t_triv); the trivial
        # sub-kind travels as the d word itself (0 = identity, sign bit set =
        # -j).  Each input register is read exactly once per execution.
        cells["steer"] = CellProgram(
            inputs=[Port("xi", register=1), Port("xq", register=2),
                    Port("c", register=3), Port("d", register=4)],
            outputs=[Port("c_f"), Port("d_f"), Port("xi_f"), Port("xq_f"),
                     Port("t_mul"), Port("t_triv")],
            entries=[EntryPoint("default")],
            data=[DataWord("sent", TRIVIAL_SENTINEL, address=5)],
            state=[StateVar("csav", register=6), StateVar("dsav", register=7)],
            assembly_template=(
                "default:\n"
                "    MOVE R{state:csav}, R{in:c}\n"
                "    MOVE R{state:dsav}, R{in:d}\n"
                "    CMP R{state:csav}, R{data:sent}\n"
                "    BR.Z +10\n"
                "    MOVE R0, R{state:csav}\n"     # non-trivial: c, d, xi, xq
                "    {write:c_f}\n"
                "    MOVE R0, R{state:dsav}\n"
                "    {write:d_f}\n"
                "    MOVE R0, R{in:xi}\n"
                "    {write:xi_f}\n"
                "    MOVE R0, R{in:xq}\n"
                "    {write:xq_f}\n"
                "    {jump:t_mul}\n"
                "    HALT\n"
                "    MOVE R0, R{in:xi}\n"          # trivial: xi, xq, d(kind)
                "    {write:xi_f}\n"
                "    MOVE R0, R{in:xq}\n"
                "    {write:xq_f}\n"
                "    MOVE R0, R{state:dsav}\n"
                "    {write:d_f}\n"
                "    {jump:t_triv}\n"),
        )

        # (4) prods — the four pinned floor-MULQs (every product in range:
        # |c|,|d| <= 0x7FFF), or the trivial pass-through.  Inputs snapshot to
        # state (each is read twice in the mul path — the stale-latch trap).
        cells["prods"] = CellProgram(
            inputs=[Port("c", register=1), Port("d", register=2),
                    Port("xi", register=3), Port("xq", register=4)],
            outputs=[Port("p1"), Port("p2"), Port("p3"), Port("p4"),
                     Port("t_mul"), Port("t_triv")],
            entries=[EntryPoint("mul"), EntryPoint("triv")],
            state=[StateVar("cs", register=5), StateVar("ds", register=6),
                   StateVar("xis", register=7), StateVar("xqs", register=8)],
            assembly_template=(
                "mul:\n"
                "    MOVE R{state:cs}, R{in:c}\n"
                "    MOVE R{state:ds}, R{in:d}\n"
                "    MOVE R{state:xis}, R{in:xi}\n"
                "    MOVE R{state:xqs}, R{in:xq}\n"
                "    MULQ R{state:xis}, R{state:cs}\n"    # p1 = xi*c
                "    {write:p1}\n"
                "    MULQ R{state:xqs}, R{state:ds}\n"    # p2 = xq*d
                "    {write:p2}\n"
                "    MULQ R{state:xis}, R{state:ds}\n"    # p3 = xi*d
                "    {write:p3}\n"
                "    MULQ R{state:xqs}, R{state:cs}\n"    # p4 = xq*c
                "    {write:p4}\n"
                "    {jump:t_mul}\n"
                "    HALT\n"
                "triv:\n"
                "    MOVE R0, R{in:xi}\n"
                "    {write:p1}\n"                        # xi rides the p1 slot
                "    MOVE R0, R{in:xq}\n"
                "    {write:p2}\n"                        # xq rides the p2 slot
                "    MOVE R0, R{in:d}\n"
                "    {write:p3}\n"                        # kind rides the p3 slot
                "    {jump:t_triv}\n"),
        )

        # (5) rail_i — yi rail (mul) or the 1-instruction trivial sub-dispatch
        # (SHR #15 of the d word: 0 -> identity, 1 -> -j; CMP-free, no extra
        # data word, reads p3 exactly once).  p4 never stops here — prods
        # writes it TWO hops down the fold straight into emit (it transits
        # this plain single-face cell), which is what keeps this cell inside
        # the 32-word budget.
        cells["rail_i"] = CellProgram(
            inputs=[Port("p1", register=1), Port("p2", register=2),
                    Port("p3", register=3)],
            outputs=[Port("yi_f"), Port("p3_f"),
                     Port("t_mul"), Port("t_id"), Port("t_mj")],
            entries=[EntryPoint("mul"), EntryPoint("triv")],
            data=[DataWord("satpos", SAT_POS_Q15, address=5)],
            state=[StateVar("p1s", register=6)],
            assembly_template=(
                "mul:\n"
                "    MOVE R{state:p1s}, R{in:p1}\n"
                "    SUB R{state:p1s}, R{in:p2}\n"        # yi = p1 - p2, sets V
                "    BR.NV +3\n"
                "    MOVE R0, R{state:p1s}\n"             # overflow: minuend sign
                "    SHR R0, #15\n"
                "    ADD R0, R{data:satpos}\n"
                "    {write:yi_f}\n"
                "    MOVE R0, R{in:p3}\n"
                "    {write:p3_f}\n"
                "    {jump:t_mul}\n"
                "    HALT\n"
                "triv:\n"
                "    SHR R{in:p3}, #15\n"                 # d sign: 0=id, 1=-j
                "    BR.NZ +6\n"
                "    MOVE R0, R{in:p1}\n"                 # identity: yi = xi
                "    {write:yi_f}\n"
                "    MOVE R0, R{in:p2}\n"
                "    {write:p3_f}\n"                      # xq onward (yq = xq)
                "    {jump:t_id}\n"
                "    HALT\n"
                "    MOVE R0, R{in:p1}\n"                 # -j: xi onward (negate)
                "    {write:p3_f}\n"
                "    MOVE R0, R{in:p2}\n"
                "    {write:yi_f}\n"                      # yi = xq
                "    {jump:t_mj}\n"),
        )

        # (6) emit — the exit cell; three entries, one shared (yi, yq) complex
        # packet form (INV-17: the pair writes + ONE trigger, free words for
        # the build's fan-out JUMP).
        cells["emit"] = CellProgram(
            inputs=[Port("yi_in", register=1), Port("p3", register=2),
                    Port("p4", register=3)],
            outputs=[Port("yi"), Port("yq"), Port("trig")],
            entries=[EntryPoint("mul"), EntryPoint("id"), EntryPoint("mj")],
            data=[DataWord("zero", 0, address=4),
                  DataWord("satpos", SAT_POS_Q15, address=5)],
            state=[StateVar("p3s", register=6)],
            assembly_template=(
                "mul:\n"
                "    MOVE R0, R{in:yi_in}\n"
                "    {write:yi}\n"
                "    MOVE R{state:p3s}, R{in:p3}\n"
                "    ADD R{state:p3s}, R{in:p4}\n"        # yq = p3 + p4, sets V
                "    BR.NV +3\n"
                "    MOVE R0, R{state:p3s}\n"             # overflow: minuend sign
                "    SHR R0, #15\n"
                "    ADD R0, R{data:satpos}\n"
                "    {write:yq}\n"
                "    {jump:trig}\n"
                "    HALT\n"
                "id:\n"
                "    MOVE R0, R{in:yi_in}\n"
                "    {write:yi}\n"
                "    MOVE R0, R{in:p3}\n"                 # yq = xq
                "    {write:yq}\n"
                "    {jump:trig}\n"
                "    HALT\n"
                "mj:\n"
                "    MOVE R0, R{in:yi_in}\n"              # yi = xq
                "    {write:yi}\n"
                "    SUB R{data:zero}, R{in:p3}\n"        # yq = -xi, sets V
                "    BR.NV +1\n"
                "    MOVE R0, R{data:satpos}\n"           # -(-32768) -> +32767
                "    {write:yq}\n"
                "    {jump:trig}\n"),
        )
        return cells

    # ------------------------------------------------------- multi-cell wiring
    def _chain(self):
        return ["fetch_c", "fetch_d", "steer", "prods", "rail_i", "emit"]

    def internal_connections(self):
        return [
            ("fetch_c", "t_f", "fetch_d", "c"),
            ("fetch_c", "xi_f", "steer", "xi"),
            ("fetch_c", "xq_f", "steer", "xq"),
            ("fetch_d", "t_f", "steer", "d"),
            ("fetch_d", "c_f", "steer", "c"),
            ("steer", "c_f", "prods", "c"),
            ("steer", "d_f", "prods", "d"),
            ("steer", "xi_f", "prods", "xi"),
            ("steer", "xq_f", "prods", "xq"),
            ("prods", "p1", "rail_i", "p1"),
            ("prods", "p2", "rail_i", "p2"),
            ("prods", "p3", "rail_i", "p3"),
            ("prods", "p4", "emit", "p4"),      # 2 hops: transits rail_i
            ("rail_i", "yi_f", "emit", "yi_in"),
            ("rail_i", "p3_f", "emit", "p3"),
        ]

    def internal_jumps(self):
        return [
            ("fetch_c", "trig", "fetch_d", "default"),
            ("fetch_d", "trig", "steer", "default"),
            ("steer", "t_mul", "prods", "mul"),
            ("steer", "t_triv", "prods", "triv"),
            ("prods", "t_mul", "rail_i", "mul"),
            ("prods", "t_triv", "rail_i", "triv"),
            ("rail_i", "t_mul", "emit", "mul"),
            ("rail_i", "t_id", "emit", "id"),
            ("rail_i", "t_mj", "emit", "mj"),
        ]

    def output_cell_ids(self):
        return ["emit"]

    def default_layout(self):
        # 2x3 serpentine (even columns, INV-14): I/O co-located on the top
        # edge (fetch_c at (0,0), emit at (1,0)).
        return {
            "fetch_c": (0, 0, "south"),
            "fetch_d": (0, 1, "south"),
            "steer": (0, 2, "east"),
            "prods": (1, 2, "north"),
            "rail_i": (1, 1, "north"),
            "emit": (1, 0, "east"),
        }

    # -------------------------------------------------------------- reference
    def process_reference_q15(self, xi_words, xq_words) -> Tuple[list, list]:
        """Bit-exact predictor: per sample n, ``y = x * table[n mod P]`` with
        the pinned trivial/non-trivial forms (:func:`twiddle_cmul_ref`)."""
        P = self.period
        yi, yq = [], []
        for n, (i_w, q_w) in enumerate(zip(xi_words, xq_words)):
            kind, c, d = self._table[n % P]
            r_i, r_q = twiddle_cmul_ref(i_w, q_w, kind, c, d)
            yi.append(r_i)
            yq.append(r_q)
        return yi, yq

    def process_reference(self, input_samples) -> np.ndarray:
        """Float reference: ``y[n] = x[n] * twiddles[n mod P]``, each rail
        clipped to the Q15 range (the bit-exact rounding/saturation contract
        lives in :meth:`process_reference_q15`)."""
        x = np.asarray(input_samples, dtype=np.complex128)
        P = self.period
        w = np.array([self._twiddles[n % P] for n in range(len(x))],
                     dtype=np.complex128)
        y = x * w
        lo, hi = -1.0, 32767.0 / 32768.0
        return (np.clip(y.real, lo, hi)
                + 1j * np.clip(y.imag, lo, hi)).astype(np.complex64)
