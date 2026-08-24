# SPDX-License-Identifier: GPL-3.0-or-later
"""FFT16Block — 16-point streaming radix-2 single-path delay-feedback FFT.

A COMPOSITE block built from the shipped radix-2 FFT primitives: it imports the
bit-exact integer reference helpers and the RHE butterfly leg programs from
``fft_primitives`` (R2Butterfly / TwiddleMultiply) and the distributed
delay-segment idiom from ``complex_delay_line_block`` — one block, four R2SDF
stages, everything on-fabric.

ARCHITECTURE (single-path delay feedback, R2SDF, decimation-in-frequency):

    in ──► stage0(D=8) ──► stage1(D=4) ──► stage2(D=2) ──► stage3(D=1) ──► out

Each stage ``s`` owns a delay line of ``D = 8 >> s`` complex samples and a
period-``2D`` schedule (the standard R2SDF schedule, one complex sample in and
one out per trigger):

  * ``t mod 2D <  D`` (FILL/EMIT): the incoming sample is pushed into the
    delay line; the emerging value (a stored difference from the previous
    half-period) is multiplied by the stage twiddle ``W_16^(j·2^s)`` for its
    slot ``j = t mod 2D`` and emitted.
  * ``t mod 2D >= D`` (BUTTERFLY): with ``a`` = the emerging delay-line value
    and ``b`` = the incoming sample, the stage emits the SCALED sum
    ``RHE((a+b)/2)`` and pushes the scaled difference ``RHE((a-b)/2)`` back
    into the delay line (twiddled later, on its way out).

NUMERICS (pinned — identical to the shipped primitives):

  * Unconditional ``>>1`` per stage with ROUND-HALF-TO-EVEN (RHE), computed
    16-bit-safe (the 17-bit sum is never materialized) with the mandatory
    saturating combine — the exact ``R2ButterflyBlock`` leg programs.
    **Output = FFT/16** (fixed known scale; four scaled stages).
  * Twiddles stored ``round(32768·x)`` with the trivial values special-cased
    STRUCTURALLY (``W=1`` pass-through, ``W=-j`` rail swap + saturating
    negate) — the exact ``TwiddleMultiplyBlock`` machinery, including the
    0x8000 sentinel tables and the 4-MULQ/2-saturating-combine multiply.
    Non-trivial twiddle words per stage: 12 / 4 / 0 / 0 (stages 2 and 3 need
    NO multiply cells at all; stage 2's single ``-j`` slot is one structural
    swap/negate entry).

⚠️ OUTPUT ORDER — BIT-REVERSED (read this before consuming the block):

    The block streams FRAMES of 16 output samples. Output slot ``k`` of a
    frame carries frequency bin ``FFT16_OUTPUT_BINS[k]``:

        slot:  0  1  2   3  4   5  6   7  8  9  10  11 12  13 14  15
        bin :  0  8  4  12  2  10  6  14  1  9   5  13  3  11  7  15

    (the standard DIF bit-reversed order). There is deliberately NO reorder
    buffer in this block — a consumer that needs natural bin order applies the
    index map (``bins[FFT16_OUTPUT_BINS[k]] = frame[k]``) or an explicit
    reorder stage.

LATENCY / FRAMING: the pipeline latency is ``N-1 = 15`` samples. The block
emits ONE complex output pair per input trigger from the very first trigger;
the first 15 outputs are the deterministic startup values of the zero-
initialized pipeline (mostly zeros) and are part of the bit-exact contract.
Frame ``f`` occupies output samples ``15 + 16·f .. 15 + 16·f + 15``; frame
alignment is tied to the ABSOLUTE trigger count since configuration (the
stream is continuous — state carries across batches like any streaming block).

SATURATION SAFETY (INV-19/20): each stage closes a data-feedback ring — the
delay tail returns the emerging sample to the stage controller's ``a``
registers — so every stage carries the proven serialize-LOCK: the controller
LOCKs its arbiter after dispatching a sample; the stage's ``out`` cell clears
the lock (backward ``WRITE.CFG``) inline with the ``a``-write-back, AFTER the
stage's output packet has been accepted downstream. Stages pipeline against
each other; each stage is internally single-sample. This is always on (it is
correctness, not an option).

The fill and butterfly paths have different chain lengths through a stage
(fill transits the twiddle cells); the per-stage serialize-LOCK is what makes
that safe — no two samples are ever co-resident in one stage, so no
overtaking is possible.

⚠️ UNWIRED-OUTPUT HAZARD (the R2Butterfly lesson): the complex output pair
must be WIRED (to a downstream block or a chip port). A dangling complex
output's default-resolved WRITE/JUMPs fire into whatever neighbours the
placement leaves there.

GOLDEN: there is no GNU Radio counterpart block; the golden is the
bit-exact streaming integer model in this module
(:func:`fft16_streaming_reference` — the cycle-accurate R2SDF schedule over
the shared ``fft_primitives`` arithmetic), which the verification suite
re-asserts against an independently transcribed direct DIF integer FFT and
against float ``numpy.fft.fft`` (SNR floor).

Params: NONE (N is pinned at 16; the scale, order, and latency are fixed
contracts documented above).
"""
from typing import Dict, List, Tuple

import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock
from .fft_primitives import (
    HALF_Q15, KIND_ID, KIND_MJ, SAT_POS_Q15, R2ButterflyBlock,
    TRIVIAL_SENTINEL, quantize_twiddle, rhe_half_diff, rhe_half_sum, s16,
    twiddle_cmul_ref, u16)

N_FFT = 16
N_STAGES = 4
LATENCY = N_FFT - 1                  # 15 samples (sum of stage delays 8+4+2+1)

# Per-stage delay depth D = 8 >> s; physical line = D-1 samples + the ctl
# ``a`` register pair (the re-timed R2SDF ring — total storage D, exact).
_STAGE_D = (8, 4, 2, 1)
# Physical delay-line segmentation per stage (complex samples per delay cell,
# ComplexDelayLineBlock density; the LAST segment is the tail that feeds the
# stage's out/write-back cell). Stage 3's depth-0 line is a plain relay.
_DELAY_SEGS = {0: [5, 2], 1: [2, 1], 2: [1], 3: []}


def bit_reverse_4(k: int) -> int:
    """4-bit bit reversal (N=16)."""
    k &= 0xF
    return ((k & 1) << 3) | ((k & 2) << 1) | ((k & 4) >> 1) | ((k & 8) >> 3)


# Output slot k of a frame carries frequency bin FFT16_OUTPUT_BINS[k].
FFT16_OUTPUT_BINS = tuple(bit_reverse_4(k) for k in range(N_FFT))


def fft16_stage_tables() -> List[List[Tuple[str, int, int]]]:
    """The four stage twiddle tables, ``(kind, c_word, d_word)`` per slot.

    Stage ``s`` slot ``j`` (j = 0..D-1) holds ``W_16^(j·2^s)`` with the
    trivial angles detected by INDEX (k=0 → identity, k=N/4 → -j) so the
    special-casing is exact, never float-equality on cos/sin."""
    tables: List[List[Tuple[str, int, int]]] = []
    for s in range(N_STAGES):
        D = _STAGE_D[s]
        rows: List[Tuple[str, int, int]] = []
        for j in range(D):
            k = j << s
            if k == 0:
                rows.append((KIND_ID, TRIVIAL_SENTINEL, 0x0000))
            elif 4 * k == N_FFT:
                rows.append((KIND_MJ, TRIVIAL_SENTINEL, TRIVIAL_SENTINEL))
            else:
                th = 2.0 * np.pi * k / N_FFT
                rows.append(quantize_twiddle(complex(np.cos(th), -np.sin(th))))
        tables.append(rows)
    return tables


# ---------------------------------------------------------------------------
# Bit-exact streaming golden (the transcribed cycle-accurate R2SDF schedule).
# ---------------------------------------------------------------------------

class _SDFStageModel:
    """One R2SDF stage of the golden: delay D, table ``tw`` (D entries)."""

    def __init__(self, D: int, tw: List[Tuple[str, int, int]]):
        self.D = D
        self.tw = tw
        self.line: List[Tuple[int, int]] = [(0, 0)] * D
        self.t = 0

    def step(self, xi: int, xq: int) -> Tuple[int, int]:
        D = self.D
        out_i, out_q = self.line.pop(0)
        ph = self.t % (2 * D)
        if ph < D:
            # FILL/EMIT: push input; twiddle the emerging stored difference.
            self.line.append((u16(xi), u16(xq)))
            kind, c, d = self.tw[ph]
            o_i, o_q = twiddle_cmul_ref(out_i, out_q, kind, c, d)
        else:
            # BUTTERFLY: emit the scaled sum, push the scaled difference.
            s_i = rhe_half_sum(out_i, xi)
            s_q = rhe_half_sum(out_q, xq)
            d_i = rhe_half_diff(out_i, xi)
            d_q = rhe_half_diff(out_q, xq)
            self.line.append((d_i, d_q))
            o_i, o_q = s_i, s_q
        self.t += 1
        return o_i, o_q


def fft16_streaming_reference(iq_words) -> List[Tuple[int, int]]:
    """The bit-exact per-trigger output stream of the streaming FFT.

    ``iq_words`` is a list of ``(i, q)`` uint16 Q15 word pairs; the return is
    one ``(i, q)`` output pair PER INPUT TRIGGER — including the first
    ``LATENCY`` startup outputs of the zero-initialized pipeline. From output
    index ``LATENCY`` on, every 16 consecutive outputs are one frame in
    bit-reversed bin order (see :data:`FFT16_OUTPUT_BINS`), scaled FFT/16."""
    tables = fft16_stage_tables()
    stages = [_SDFStageModel(_STAGE_D[s], tables[s]) for s in range(N_STAGES)]
    out: List[Tuple[int, int]] = []
    for (xi, xq) in iq_words:
        vi, vq = u16(xi), u16(xq)
        for st in stages:
            vi, vq = st.step(vi, vq)
        out.append((vi, vq))
    return out


# ---------------------------------------------------------------------------
# FFT16Block
# ---------------------------------------------------------------------------

class FFT16Block(KyttarBlock):
    """16-point streaming R2SDF FFT (see the module docstring for the pinned
    architecture, numerics, BIT-REVERSED output order, /16 scale, latency 15,
    and the per-stage serialize-LOCK).

    TOPOLOGY (44 cells, four 2-row stage bands stacked vertically, 7×8
    footprint — both dimensions ≤ 8, INV-9):

    Per stage: ``ctl`` (landing; holds the ``a`` feedback pair + the phase
    counter; dispatches FILL vs BUTTERFLY as entry chains and engages the
    serialize-LOCK) → the four RHE leg cells (``sumi/sumq/diffi/diffq`` — the
    R2Butterfly leg programs, with cheap pass-through FILL entries; sum legs
    BEFORE diff legs so no cell's last internal-connection dst is an adjacent
    non-successor — the route-time-face rule, see ``_stage_cells``) → the
    twiddle chain (stages 0/1 only: ``fetch_c/fetch_d/steer/prods/rail`` —
    the TwiddleMultiply cell programs; stage 2 inlines its single ``-j`` slot
    in ``gather``; stage 3 has none) → ``gather`` (per-kind combine) → the
    delay cells (``d0``/``tail`` — ComplexDelayLine segments; stage 3: a
    relay) → ``out`` (emits the stage's complex packet to the next stage /
    the block egress, then write-backs the emerging pair into ``ctl`` and
    clears the lock).

    Interface: one complex input (xi@R1, xq@R2 on ``s0_ctl``), one complex
    output pair (out_i, out_q on ``s3_out``). 1:1 rate, one output pair per
    trigger.
    """

    CATEGORY = "math_operators"
    TAGS = ["fft", "spectrum", "radix2", "r2sdf", "dif", "complex",
            "streaming", "math_operators"]

    _interface = BlockInterface(
        entry_address=1, input_registers=[1, 2], output_registers=[0, 1])

    def __init__(self, name: str):
        super().__init__(name)
        self._tables = fft16_stage_tables()

    @property
    def cell_count(self) -> int:
        return 44

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    # ----------------------------------------------------------- cell builders
    @staticmethod
    def _ctl_cell(stage: int, external: bool) -> CellProgram:
        """The stage controller / landing cell.

        Holds the feedback pair (ai, aq — written back by the stage's ``out``
        cell), the free-running phase counter, and the FILL/BUTTERFLY entry
        dispatch (``cnt AND D`` — D is a power of two, so bit ``log2 D`` of
        the counter IS the half-period selector; the 16-bit wrap is exact
        because 2^16 is a multiple of 2D). Engages the serialize-LOCK after
        dispatch (the proven post-jump lock tail; first sample runs unlocked
        = cold start). lock_face is an ``is_face`` word (orientation-safe):
        the unlock/write-back words arrive from the stage's ``out`` cell one
        row SOUTH in the authored layout.

        Stage 2 additionally forwards the fill-slot parity (``cnt AND 1``) as
        a kind word to its gather cell (its two fill twiddles are 1 and -j).
        """
        in_names = ("xi", "xq") if external else ("bi", "bq")
        kw = (stage == 2)
        outputs = [Port("ai_f"), Port("bi_f"), Port("aq_f"), Port("bq_f")]
        if kw:
            outputs.append(Port("kw_f"))
        outputs += [Port("t_fill"), Port("t_bfly")]
        kw_lines = ("    AND R{state:cnt}, R{data:one}\n"
                    "    {write:kw_f}\n") if kw else ""
        return CellProgram(
            inputs=[Port(in_names[0], register=1), Port(in_names[1], register=2)],
            outputs=outputs,
            entries=[EntryPoint("default")],
            data=[DataWord("one", 1, address=3),
                  DataWord("dmask", _STAGE_D[stage], address=4),
                  DataWord("lock_face", 0, address=5, is_face=True)],
            state=[StateVar("ai", register=6, initial_value=0),
                   StateVar("aq", register=7, initial_value=0),
                   StateVar("cnt", register=8, initial_value=0)],
            assembly_template=(
                "default:\n"
                "    MOVE R0, R{state:ai}\n"
                "    {write:ai_f}\n"
                "    MOVE R0, R{in:%s}\n"
                "    {write:bi_f}\n"
                "    MOVE R0, R{state:aq}\n"
                "    {write:aq_f}\n"
                "    MOVE R0, R{in:%s}\n"
                "    {write:bq_f}\n" % in_names
                + kw_lines +
                "    AND R{state:cnt}, R{data:dmask}\n"
                "    BR.NZ +2\n"
                "    {jump:t_fill}\n"
                "    BR.Z +1\n"
                "    {jump:t_bfly}\n"
                "    ADD R{state:cnt}, R{data:one}\n"
                "    MOVE R{state:cnt}, R0\n"
                "    MOVE R0, R{data:lock_face}\n"
                "    MOVE [LOCK_FACE], R0\n"
                "    MOVE R0, R{data:one}\n"
                "    MOVE [LOCK], R0\n"),
        )

    @staticmethod
    def _sum_leg_cell() -> CellProgram:
        """RHE sum leg (one rail): BUTTERFLY entry = the R2Butterfly 11-instr
        16-bit-safe RHE sum + operand re-forward to the diff leg; FILL entry
        passes ``a`` toward the emit path (steer/gather) and ``b`` to the
        diff leg (each input register read exactly once per execution)."""
        return CellProgram(
            inputs=[Port("a", register=1), Port("b", register=2)],
            outputs=[Port("s_f"), Port("a_pass"), Port("a_f"), Port("b_f"),
                     Port("t_b"), Port("t_f")],
            entries=[EntryPoint("bfly"), EntryPoint("fill")],
            data=[DataWord("one", 1, address=3),
                  DataWord("half", HALF_Q15, address=4)],
            state=[StateVar("as_", register=5), StateVar("bs", register=6),
                   StateVar("tk", register=7)],
            assembly_template=(
                "bfly:\n"
                + R2ButterflyBlock._rhe_sum_lines() +
                "    {write:s_f}\n"
                "    MOVE R0, R{state:as_}\n"
                "    {write:a_f}\n"
                "    MOVE R0, R{state:bs}\n"
                "    {write:b_f}\n"
                "    {jump:t_b}\n"
                "    HALT\n"
                "fill:\n"
                "    MOVE R0, R{in:a}\n"
                "    {write:a_pass}\n"
                "    MOVE R0, R{in:b}\n"
                "    {write:b_f}\n"
                "    {jump:t_f}\n"),
        )

    @staticmethod
    def _diff_leg_cell() -> CellProgram:
        """RHE difference leg (one rail): BUTTERFLY entry = the R2Butterfly
        16-instr RHE diff with the mandatory saturating clamp, pushing the
        result into the stage delay line; FILL entry pushes ``b`` (the
        incoming sample rides into the line while the stored value emits)."""
        return CellProgram(
            inputs=[Port("a", register=1), Port("b", register=2)],
            outputs=[Port("v_f"), Port("t_b"), Port("t_f")],
            entries=[EntryPoint("bfly"), EntryPoint("fill")],
            data=[DataWord("one", 1, address=3),
                  DataWord("half", HALF_Q15, address=4)],
            state=[StateVar("as_", register=5), StateVar("bs", register=6),
                   StateVar("tk", register=7)],
            assembly_template=(
                "bfly:\n"
                + R2ButterflyBlock._rhe_diff_lines() +
                "    {write:v_f}\n"
                "    {jump:t_b}\n"
                "    HALT\n"
                "fill:\n"
                "    MOVE R0, R{in:b}\n"
                "    {write:v_f}\n"
                "    {jump:t_f}\n"),
        )

    @staticmethod
    def _fetch_cell(table_words: List[int], has_c_input: bool) -> CellProgram:
        """A twiddle table cell (the TwiddleMultiply fetch idiom): LOAD the
        slot word, forward it, advance the slot pointer with period wrap.
        Triggered ONLY on fill samples, so the pointer walks slot 0..D-1
        exactly in step with the stage's fill phase — no separate sync."""
        P = len(table_words)
        base = 2
        data = [DataWord(f"t{i}", u16(w), address=base + i)
                for i, w in enumerate(table_words)]
        data += [DataWord("one", 1, address=base + P),
                 DataWord("pend", base + P, address=base + P + 1),
                 DataWord("pbase", base, address=base + P + 2)]
        ptr_reg = base + P + 3
        if has_c_input:
            inputs = [Port("c", register=1)]
            fwd = ("    MOVE R0, R{in:c}\n"
                   "    {write:c_f}\n")
            outs = [Port("t_f"), Port("c_f"), Port("trig")]
        else:
            inputs = []
            fwd = ""
            outs = [Port("t_f"), Port("trig")]
        return CellProgram(
            inputs=inputs, outputs=outs,
            entries=[EntryPoint("default")],
            data=data,
            state=[StateVar("ptr", register=ptr_reg, initial_value=base)],
            assembly_template=(
                "default:\n"
                "    LOAD R{state:ptr}\n"
                "    {write:t_f}\n"
                + fwd +
                "    ADD R{state:ptr}, R{data:one}\n"
                "    MOVE R{state:ptr}, R0\n"
                "    CMP R0, R{data:pend}\n"
                "    BR.NZ +1\n"
                "    MOVE R{state:ptr}, R{data:pbase}\n"
                "    {jump:trig}\n"),
        )

    @staticmethod
    def _steer_cell() -> CellProgram:
        """The TwiddleMultiply kind dispatch, VERBATIM: one CMP against the
        C-table sentinel; the path identity travels as WHICH ENTRY each
        downstream cell is jumped at. Reached only on FILL samples (butterfly
        samples bypass straight to gather's pass entry)."""
        return CellProgram(
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
                "    MOVE R0, R{state:csav}\n"
                "    {write:c_f}\n"
                "    MOVE R0, R{state:dsav}\n"
                "    {write:d_f}\n"
                "    MOVE R0, R{in:xi}\n"
                "    {write:xi_f}\n"
                "    MOVE R0, R{in:xq}\n"
                "    {write:xq_f}\n"
                "    {jump:t_mul}\n"
                "    HALT\n"
                "    MOVE R0, R{in:xi}\n"
                "    {write:xi_f}\n"
                "    MOVE R0, R{in:xq}\n"
                "    {write:xq_f}\n"
                "    MOVE R0, R{state:dsav}\n"
                "    {write:d_f}\n"
                "    {jump:t_triv}\n"),
        )

    @staticmethod
    def _prods_cell() -> CellProgram:
        """The four pinned floor-MULQs (TwiddleMultiply VERBATIM), or the
        trivial pass-through."""
        return CellProgram(
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
                "    MULQ R{state:xis}, R{state:cs}\n"
                "    {write:p1}\n"
                "    MULQ R{state:xqs}, R{state:ds}\n"
                "    {write:p2}\n"
                "    MULQ R{state:xis}, R{state:ds}\n"
                "    {write:p3}\n"
                "    MULQ R{state:xqs}, R{state:cs}\n"
                "    {write:p4}\n"
                "    {jump:t_mul}\n"
                "    HALT\n"
                "triv:\n"
                "    MOVE R0, R{in:xi}\n"
                "    {write:p1}\n"
                "    MOVE R0, R{in:xq}\n"
                "    {write:p2}\n"
                "    MOVE R0, R{in:d}\n"
                "    {write:p3}\n"
                "    {jump:t_triv}\n"),
        )

    @staticmethod
    def _rail_cell() -> CellProgram:
        """The yi rail / trivial sub-dispatch (TwiddleMultiply VERBATIM);
        p4 transits this cell straight into gather."""
        return CellProgram(
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
                "    SUB R{state:p1s}, R{in:p2}\n"
                "    BR.NV +3\n"
                "    MOVE R0, R{state:p1s}\n"
                "    SHR R0, #15\n"
                "    ADD R0, R{data:satpos}\n"
                "    {write:yi_f}\n"
                "    MOVE R0, R{in:p3}\n"
                "    {write:p3_f}\n"
                "    {jump:t_mul}\n"
                "    HALT\n"
                "triv:\n"
                "    SHR R{in:p3}, #15\n"
                "    BR.NZ +6\n"
                "    MOVE R0, R{in:p1}\n"
                "    {write:yi_f}\n"
                "    MOVE R0, R{in:p2}\n"
                "    {write:p3_f}\n"
                "    {jump:t_id}\n"
                "    HALT\n"
                "    MOVE R0, R{in:p1}\n"
                "    {write:p3_f}\n"
                "    MOVE R0, R{in:p2}\n"
                "    {write:yi_f}\n"
                "    {jump:t_mj}\n"),
        )

    @staticmethod
    def _gather_tw_cell() -> CellProgram:
        """Per-kind combine for the twiddle stages (the TwiddleMultiply emit
        cell with its writes redirected one step onward). The BUTTERFLY path
        enters at ``id`` directly (the sum legs write (si, sq) into the same
        yi_in/p3 registers the trivial path uses), so no extra entry is
        needed and both modes leave through identical code."""
        return CellProgram(
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
                "    ADD R{state:p3s}, R{in:p4}\n"
                "    BR.NV +3\n"
                "    MOVE R0, R{state:p3s}\n"
                "    SHR R0, #15\n"
                "    ADD R0, R{data:satpos}\n"
                "    {write:yq}\n"
                "    {jump:trig}\n"
                "    HALT\n"
                "id:\n"
                "    MOVE R0, R{in:yi_in}\n"
                "    {write:yi}\n"
                "    MOVE R0, R{in:p3}\n"
                "    {write:yq}\n"
                "    {jump:trig}\n"
                "    HALT\n"
                "mj:\n"
                "    MOVE R0, R{in:yi_in}\n"
                "    {write:yi}\n"
                "    SUB R{data:zero}, R{in:p3}\n"
                "    BR.NV +1\n"
                "    MOVE R0, R{data:satpos}\n"
                "    {write:yq}\n"
                "    {jump:trig}\n"),
        )

    @staticmethod
    def _gather_s2_cell() -> CellProgram:
        """Stage 2 combine: BUTTERFLY passes (si, sq); FILL dispatches on the
        forwarded slot-parity kind word — slot 0 is identity, slot 1 is the
        structural ``-j`` (rail swap + saturating negate). No multiply."""
        return CellProgram(
            inputs=[Port("si", register=1), Port("sq", register=2),
                    Port("ai", register=3), Port("aq", register=4),
                    Port("kw", register=5)],
            outputs=[Port("yi"), Port("yq"), Port("trig")],
            entries=[EntryPoint("bfly"), EntryPoint("fill")],
            data=[DataWord("one", 1, address=6),
                  DataWord("zero", 0, address=7),
                  DataWord("satpos", SAT_POS_Q15, address=8)],
            assembly_template=(
                "bfly:\n"
                "    MOVE R0, R{in:si}\n"
                "    {write:yi}\n"
                "    MOVE R0, R{in:sq}\n"
                "    {write:yq}\n"
                "    {jump:trig}\n"
                "    HALT\n"
                "fill:\n"
                "    AND R{in:kw}, R{data:one}\n"
                "    BR.NZ fmj\n"
                "    MOVE R0, R{in:ai}\n"
                "    {write:yi}\n"
                "    MOVE R0, R{in:aq}\n"
                "    {write:yq}\n"
                "    {jump:trig}\n"
                "    HALT\n"
                "fmj:\n"
                "    MOVE R0, R{in:aq}\n"
                "    {write:yi}\n"
                "    SUB R{data:zero}, R{in:ai}\n"
                "    BR.NV +1\n"
                "    MOVE R0, R{data:satpos}\n"
                "    {write:yq}\n"
                "    {jump:trig}\n"),
        )

    @staticmethod
    def _gather_s3_cell() -> CellProgram:
        """Stage 3 combine: BUTTERFLY passes (si, sq); FILL passes (ai, aq)
        (its only twiddle is W^0 = 1 — pure identity)."""
        return CellProgram(
            inputs=[Port("si", register=1), Port("sq", register=2),
                    Port("ai", register=3), Port("aq", register=4)],
            outputs=[Port("yi"), Port("yq"), Port("trig")],
            entries=[EntryPoint("bfly"), EntryPoint("fill")],
            assembly_template=(
                "bfly:\n"
                "    MOVE R0, R{in:si}\n"
                "    {write:yi}\n"
                "    MOVE R0, R{in:sq}\n"
                "    {write:yq}\n"
                "    {jump:trig}\n"
                "    HALT\n"
                "fill:\n"
                "    MOVE R0, R{in:ai}\n"
                "    {write:yi}\n"
                "    MOVE R0, R{in:aq}\n"
                "    {write:yq}\n"
                "    {jump:trig}\n"),
        )

    @staticmethod
    def _delay_cell(L: int) -> CellProgram:
        """One delay segment of ``L`` complex samples — the ComplexDelayLine
        cell program verbatim (capture-oldest → shift → ingest → forward, per
        rail, one shared osave; inputs at R0/R1, every state pinned — the
        INV-33 no-data-words rule)."""
        state = ([StateVar(f"di{i}", register=i + 2, initial_value=0)
                  for i in range(L)]
                 + [StateVar(f"dq{i}", register=L + i + 2, initial_value=0)
                    for i in range(L)])
        state.append(StateVar("osave", register=2 * L + 2, initial_value=0))
        lines: List[str] = []
        lines.append("    MOVE R{state:osave}, R{state:di0}")
        for i in range(L - 1):
            lines.append(f"    MOVE R{{state:di{i}}}, R{{state:di{i + 1}}}")
        lines.append("    MOVE R{state:di%d}, R{in:xi}" % (L - 1))
        lines.append("    MOVE R0, R{state:osave}")
        lines.append("    {write:xi_out}")
        lines.append("    MOVE R{state:osave}, R{state:dq0}")
        for i in range(L - 1):
            lines.append(f"    MOVE R{{state:dq{i}}}, R{{state:dq{i + 1}}}")
        lines.append("    MOVE R{state:dq%d}, R{in:xq}" % (L - 1))
        lines.append("    MOVE R0, R{state:osave}")
        lines.append("    {write:xq_out}")
        lines.append("    {jump:fwd}")
        return CellProgram(
            inputs=[Port("xi", register=0), Port("xq", register=1)],
            outputs=[Port("xi_out"), Port("xq_out"), Port("fwd")],
            entries=[EntryPoint("default")],
            data=[], state=state,
            assembly_template="default:\n" + "\n".join(lines) + "\n",
        )

    @staticmethod
    def _relay_cell() -> CellProgram:
        """Stage 3's depth-0 'delay line': a real store-and-forward relay
        (the pushed value IS the emerging value when D-1 = 0)."""
        return CellProgram(
            inputs=[Port("xi", register=1), Port("xq", register=2)],
            outputs=[Port("xi_out"), Port("xq_out"), Port("fwd")],
            entries=[EntryPoint("default")],
            assembly_template=(
                "default:\n"
                "    MOVE R0, R{in:xi}\n"
                "    {write:xi_out}\n"
                "    MOVE R0, R{in:xq}\n"
                "    {write:xq_out}\n"
                "    {jump:fwd}\n"),
        )

    @staticmethod
    def _out_cell(external: bool) -> CellProgram:
        """The stage exit: snapshot the combine result, emit the complex
        packet to the next stage (or the block egress), then flip to the
        feedback face, write the emerging pair back into ``ctl``'s (ai, aq)
        state and clear ``ctl``'s serialize-LOCK, and restore the tap face.

        Ordering is load-bearing three ways: (1) the yi/yq SNAPSHOT at the
        top makes the cell immune to the next sample's combine landing while
        this cell is back-pressured mid-emit; (2) the write-back + WRITE.CFG
        run BEFORE the packet writes so the packet writes are the LAST DATA
        WRITES in the cell (the complex-egress patchers patch the last N data
        writes and skip config writes); (3) the packet is emitted and its
        trigger fired on the tap face, with the FACE restored after the
        feedback flip, so the trailing jump never fires into the feedback
        lane. The lock-clear lives INLINE with the data write-back (the
        Costas pd_pi shape).

        Wait — ordering note (2) vs (3): the wb runs BEFORE the packet, so
        the stage's lock releases before the downstream accepts the packet;
        the snapshot in (1) plus the per-stage single-sample lock upstream is
        exactly what makes that safe (at most ONE next sample can land in
        this cell's input registers while it is stalled, and its trigger is
        held until this execution completes)."""
        oi, oq, tg = (("out_i", "out_q", "trig") if external
                      else ("oi", "oq", "trig"))
        return CellProgram(
            inputs=[Port("yi", register=1), Port("yq", register=2),
                    Port("awi", register=3), Port("awq", register=4)],
            outputs=[Port("ai_wb"), Port("aq_wb"),
                     Port(oi), Port(oq), Port(tg)],
            entries=[EntryPoint("default")],
            data=[DataWord("zero", 0, address=5),
                  DataWord("face_fb", 3, address=6, is_face=True),
                  DataWord("face_tap", 0, address=7, is_face=True)],
            state=[StateVar("syi", register=8), StateVar("syq", register=9)],
            assembly_template=(
                "default:\n"
                "    MOVE R{state:syi}, R{in:yi}\n"
                "    MOVE R{state:syq}, R{in:yq}\n"
                "    MOVE [FACE], R{data:face_fb}\n"
                "    MOVE R0, R{in:awi}\n"
                "    {write:ai_wb}\n"
                "    MOVE R0, R{in:awq}\n"
                "    {write:aq_wb}\n"
                "    MOVE R0, R{data:zero}\n"
                "    WRITE.CFG @1, 4\n"
                "    MOVE [FACE], R{data:face_tap}\n"
                "    MOVE R0, R{state:syi}\n"
                "    {write:%s}\n"
                "    MOVE R0, R{state:syq}\n"
                "    {write:%s}\n"
                "    {jump:%s}\n" % (oi, oq, tg)),
        )

    # ------------------------------------------------------------------ build
    @staticmethod
    def _stage_cells(stage: int) -> List[str]:
        # CHAIN ORDER IS LOAD-BEARING (sum legs first, then diff legs):
        # the router derives each cell's ROUTE-TIME face from its LAST
        # internal connection when that dst is adjacent, else from the
        # dict-NEXT cell — and internal write/jump distances are resolved by
        # TRACING those faces. The order below (with the layout) keeps every
        # cell's last-edge dst either its chain successor or NON-adjacent,
        # so the traced serpentine distances are exact (a diff leg sitting
        # directly beside its delay-push target mis-faced the whole ring and
        # silently shipped Manhattan hops — found on first sim contact).
        base = [f"s{stage}_ctl", f"s{stage}_sumi", f"s{stage}_sumq",
                f"s{stage}_diffi", f"s{stage}_diffq"]
        if stage in (0, 1):
            base += [f"s{stage}_fetch_c", f"s{stage}_fetch_d",
                     f"s{stage}_steer", f"s{stage}_prods", f"s{stage}_rail",
                     f"s{stage}_gather", f"s{stage}_d0", f"s{stage}_tail",
                     f"s{stage}_out"]
        elif stage == 2:
            base += [f"s{stage}_gather", f"s{stage}_tail", f"s{stage}_out"]
        else:
            base += [f"s{stage}_gather", f"s{stage}_relay", f"s{stage}_out"]
        return base

    def build_cell_programs(self) -> Dict[str, CellProgram]:
        cells: Dict[str, CellProgram] = {}
        for s in range(N_STAGES):
            p = f"s{s}_"
            cells[p + "ctl"] = self._ctl_cell(s, external=(s == 0))
            cells[p + "sumi"] = self._sum_leg_cell()
            cells[p + "sumq"] = self._sum_leg_cell()
            cells[p + "diffi"] = self._diff_leg_cell()
            cells[p + "diffq"] = self._diff_leg_cell()
            if s in (0, 1):
                c_words = [c for (_k, c, _d) in self._tables[s]]
                d_words = [d for (_k, _c, d) in self._tables[s]]
                cells[p + "fetch_c"] = self._fetch_cell(c_words, False)
                cells[p + "fetch_d"] = self._fetch_cell(d_words, True)
                cells[p + "steer"] = self._steer_cell()
                cells[p + "prods"] = self._prods_cell()
                cells[p + "rail"] = self._rail_cell()
                cells[p + "gather"] = self._gather_tw_cell()
                segs = _DELAY_SEGS[s]
                cells[p + "d0"] = self._delay_cell(segs[0])
                cells[p + "tail"] = self._delay_cell(segs[1])
            elif s == 2:
                cells[p + "gather"] = self._gather_s2_cell()
                cells[p + "tail"] = self._delay_cell(_DELAY_SEGS[2][0])
            else:
                cells[p + "gather"] = self._gather_s3_cell()
                cells[p + "relay"] = self._relay_cell()
            cells[p + "out"] = self._out_cell(external=(s == 3))
        return cells

    # ------------------------------------------------------- multi-cell wiring
    def internal_connections(self) -> List[Tuple[str, str, str, str]]:
        conns: List[Tuple[str, str, str, str]] = []
        for s in range(N_STAGES):
            p = f"s{s}_"
            has_tw = s in (0, 1)
            gather = p + "gather"
            # The delay-push target (the diff legs' v_f destination).
            push = p + ("d0" if has_tw else ("tail" if s == 2 else "relay"))
            # Where the sum legs' FILL ``a`` pass lands.
            a_i_dst, a_q_dst = ((p + "steer", "xi"), (p + "steer", "xq")) \
                if has_tw else ((gather, "ai"), (gather, "aq"))
            # ctl operand fan-out (chain-successor edges LAST per cell — the
            # route-time-face discipline, see _stage_cells).
            if s == 2:
                conns.append((p + "ctl", "kw_f", gather, "kw"))
            conns += [
                (p + "ctl", "aq_f", p + "sumq", "a"),
                (p + "ctl", "bq_f", p + "sumq", "b"),
                (p + "ctl", "ai_f", p + "sumi", "a"),
                (p + "ctl", "bi_f", p + "sumi", "b"),
            ]
            # Sum legs: emit value + fill pass + operand re-forward. The
            # diff-leg dsts sit two hops down the chain (never adjacent), so
            # the legs' route-time faces fall back to the dict-next cell.
            sum_si = ("yi_in" if has_tw else "si")
            sum_sq = ("p3" if has_tw else "sq")
            conns += [
                (p + "sumi", "s_f", gather, sum_si),
                (p + "sumi", "a_pass", a_i_dst[0], a_i_dst[1]),
                (p + "sumi", "a_f", p + "diffi", "a"),
                (p + "sumi", "b_f", p + "diffi", "b"),
                (p + "sumq", "s_f", gather, sum_sq),
                (p + "sumq", "a_pass", a_q_dst[0], a_q_dst[1]),
                (p + "sumq", "a_f", p + "diffq", "a"),
                (p + "sumq", "b_f", p + "diffq", "b"),
            ]
            # Diff legs push into the delay line (both modes, single writer;
            # the push cell is deliberately NON-adjacent to both diff legs).
            conns += [
                (p + "diffi", "v_f", push, "xi"),
                (p + "diffq", "v_f", push, "xq"),
            ]
            if has_tw:
                conns += [
                    (p + "fetch_c", "t_f", p + "fetch_d", "c"),
                    (p + "fetch_d", "t_f", p + "steer", "d"),
                    (p + "fetch_d", "c_f", p + "steer", "c"),
                    (p + "steer", "c_f", p + "prods", "c"),
                    (p + "steer", "d_f", p + "prods", "d"),
                    (p + "steer", "xi_f", p + "prods", "xi"),
                    (p + "steer", "xq_f", p + "prods", "xq"),
                    # p4 first: prods' LAST edge must be its successor (rail).
                    (p + "prods", "p4", gather, "p4"),
                    (p + "prods", "p1", p + "rail", "p1"),
                    (p + "prods", "p2", p + "rail", "p2"),
                    (p + "prods", "p3", p + "rail", "p3"),
                    (p + "rail", "yi_f", gather, "yi_in"),
                    (p + "rail", "p3_f", gather, "p3"),
                ]
            # Combine result rides through the delay cells into the out cell.
            conns += [
                (gather, "yi", p + "out", "yi"),
                (gather, "yq", p + "out", "yq"),
            ]
            if has_tw:
                conns += [
                    (p + "d0", "xi_out", p + "tail", "xi"),
                    (p + "d0", "xq_out", p + "tail", "xq"),
                    (p + "tail", "xi_out", p + "out", "awi"),
                    (p + "tail", "xq_out", p + "out", "awq"),
                ]
            elif s == 2:
                conns += [
                    (p + "tail", "xi_out", p + "out", "awi"),
                    (p + "tail", "xq_out", p + "out", "awq"),
                ]
            else:
                conns += [
                    (p + "relay", "xi_out", p + "out", "awi"),
                    (p + "relay", "xq_out", p + "out", "awq"),
                ]
            # Inter-stage packet (forward) — stage 3's pair is the block egress.
            if s < 3:
                conns += [
                    (p + "out", "oi", f"s{s + 1}_ctl", "bi"),
                    (p + "out", "oq", f"s{s + 1}_ctl", "bq"),
                ]
            # BACKWARD data feedback: the emerging pair returns to ctl's
            # (ai, aq) STATE registers; the WRITE.CFG lock-clear rides the
            # same @1 face-flip corridor (out sits directly below ctl).
            conns += [
                (p + "out", "ai_wb", p + "ctl", "ai"),
                (p + "out", "aq_wb", p + "ctl", "aq"),
            ]
        return conns

    def internal_jumps(self) -> List[Tuple[str, str, str, str]]:
        jumps: List[Tuple[str, str, str, str]] = []
        for s in range(N_STAGES):
            p = f"s{s}_"
            has_tw = s in (0, 1)
            gather = p + "gather"
            jumps += [
                (p + "ctl", "t_fill", p + "sumi", "fill"),
                (p + "ctl", "t_bfly", p + "sumi", "bfly"),
                (p + "sumi", "t_f", p + "sumq", "fill"),
                (p + "sumi", "t_b", p + "sumq", "bfly"),
                (p + "sumq", "t_f", p + "diffi", "fill"),
                (p + "sumq", "t_b", p + "diffi", "bfly"),
                (p + "diffi", "t_f", p + "diffq", "fill"),
                (p + "diffi", "t_b", p + "diffq", "bfly"),
            ]
            if has_tw:
                jumps += [
                    (p + "diffq", "t_f", p + "fetch_c", "default"),
                    (p + "diffq", "t_b", gather, "id"),
                    (p + "fetch_c", "trig", p + "fetch_d", "default"),
                    (p + "fetch_d", "trig", p + "steer", "default"),
                    (p + "steer", "t_mul", p + "prods", "mul"),
                    (p + "steer", "t_triv", p + "prods", "triv"),
                    (p + "prods", "t_mul", p + "rail", "mul"),
                    (p + "prods", "t_triv", p + "rail", "triv"),
                    (p + "rail", "t_mul", gather, "mul"),
                    (p + "rail", "t_id", gather, "id"),
                    (p + "rail", "t_mj", gather, "mj"),
                    (gather, "trig", p + "d0", "default"),
                    (p + "d0", "fwd", p + "tail", "default"),
                    (p + "tail", "fwd", p + "out", "default"),
                ]
            elif s == 2:
                jumps += [
                    (p + "diffq", "t_f", gather, "fill"),
                    (p + "diffq", "t_b", gather, "bfly"),
                    (gather, "trig", p + "tail", "default"),
                    (p + "tail", "fwd", p + "out", "default"),
                ]
            else:
                jumps += [
                    (p + "diffq", "t_f", gather, "fill"),
                    (p + "diffq", "t_b", gather, "bfly"),
                    (gather, "trig", p + "relay", "default"),
                    (p + "relay", "fwd", p + "out", "default"),
                ]
            if s < 3:
                jumps.append((p + "out", "trig", f"s{s + 1}_ctl", "default"))
        return jumps

    def output_cell_id(self):
        """SINGULAR — the block exit is ``s3_out``: it carries the feedback
        write-back + lock-clear alongside the external complex packet, so the
        build must treat exactly this cell as the exit (its packet writes are
        the LAST data writes; the patchers leave the earlier feedback writes
        and the config write alone)."""
        return "s3_out"

    def output_cell_ids(self):
        return ["s3_out"]

    def output_face_addr(self):
        """``s3_out`` is a dual-face cell: its packet rides the in-program
        ``face_tap`` word (address 7); declaring it lets the build rewrite it
        to the routed egress direction (applied to the block-level output
        cell only)."""
        return 7

    def default_layout(self):
        """Four 2-row stage bands stacked vertically (7 wide × 8 tall — both
        ≤ 8, INV-9). Per band: the chain runs EAST along the top row, drops,
        and returns WEST along the bottom row, ending at the ``out`` cell
        directly BELOW ``ctl`` — so the feedback write-back/unlock is a @1
        NORTH flip and the next stage's landing is a @1 SOUTH packet."""
        lay = {}

        def band(p, row, tw):
            if tw:
                top = [p + c for c in ("ctl", "sumi", "sumq", "diffi",
                                       "diffq", "fetch_c", "fetch_d")]
                bot = [p + c for c in ("steer", "prods", "rail", "gather",
                                       "d0", "tail", "out")]
                for i, cid in enumerate(top):
                    lay[cid] = (i, row, "east" if i < 6 else "south")
                for i, cid in enumerate(bot):
                    x = 6 - i
                    lay[cid] = (x, row + 1, "west" if x > 0 else "south")
            else:
                last = "tail" if p == "s2_" else "relay"
                top = [p + c for c in ("ctl", "sumi", "sumq", "diffi")]
                bot = [p + c for c in ("diffq", "gather", last, "out")]
                for i, cid in enumerate(top):
                    lay[cid] = (i, row, "east" if i < 3 else "south")
                for i, cid in enumerate(bot):
                    x = 3 - i
                    lay[cid] = (x, row + 1, "west" if x > 0 else "south")

        band("s0_", 0, True)
        band("s1_", 2, True)
        band("s2_", 4, False)
        band("s3_", 6, False)
        return lay

    # -------------------------------------------------------------- reference
    def process_reference_q15(self, iq_words) -> List[Tuple[int, int]]:
        """Bit-exact per-trigger output stream (see
        :func:`fft16_streaming_reference`): one (i, q) uint16 pair per input
        trigger, startup transient included, frames in bit-reversed order."""
        return fft16_streaming_reference(iq_words)

    def process_reference(self, input_samples) -> np.ndarray:
        """Float view of the bit-exact stream (complex64, q15/32768 per
        rail). The contract (order/scale/latency) lives in the Q15 model."""
        arr = np.asarray(input_samples)
        if np.iscomplexobj(arr):
            def q15(x):
                return int(round(max(-1.0, min(32767 / 32768.0, float(x)))
                                 * 32768.0)) & 0xFFFF
            words = [(q15(c.real), q15(c.imag)) for c in arr]
        else:
            words = [(int(i) & 0xFFFF, int(q) & 0xFFFF) for (i, q) in arr]
        out = fft16_streaming_reference(words)
        return np.array([complex(s16(i) / 32768.0, s16(q) / 32768.0)
                         for (i, q) in out], dtype=np.complex64)
