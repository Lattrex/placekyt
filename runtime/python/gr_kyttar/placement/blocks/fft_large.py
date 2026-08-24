# SPDX-License-Identifier: GPL-3.0-or-later
"""Large-N streaming R2SDF FFT — the CHIP-SCALE block class (N = 64, 128).

This module generalises the shipped :mod:`fft16_block` architecture to the
transform sizes that only fit a die as its SOLE OCCUPANT. Everything that made
FFT16 bit-exact is reused VERBATIM — the ``R2ButterflyBlock`` RHE leg programs,
the ``TwiddleMultiply`` steer/prods/rail/gather cells, the ``ComplexDelayLine``
segment cells, the re-timed R2SDF ring, and the per-stage serialize-LOCK. Two
things are NEW, and only two:

1. **The octant-fold twiddle chain** for the stages whose twiddle period
   ``P >= 32`` busts a 32-word direct fetch cell (stage 0 at N=64; stages 0
   and 1 at N=128). See :ref:`the fold <octant-fold>` below.
2. **The chip-scale placement class** (``CHIP_SCALE = True``): the block may
   be the full 10 columns wide and use the full panel height, the perimeter
   routing-channel reservation is waived, and the D4 rotation requirement is
   waived (a 10-wide block cannot rotate on a 10x12). The ONLY placement
   contract is that the block's input and output are REACHABLE from the
   chip's x16 input/output ports — gated end to end on a real built chip.

.. _octant-fold:

THE OCTANT FOLD (the pinned, exhaustively proven twiddle reconstruction)
------------------------------------------------------------------------

A stage with twiddle period ``P`` needs ``W_N^(k)`` for ``k = j * 2^s``,
``j = 0..P-1``. Storing ``P`` word PAIRS directly costs ``P`` words per table
cell; a fetch cell holds at most 16 table words (measured: ``P=16`` with the
``c`` forward is exactly 32/32 words). So ``P >= 32`` needs the fold.

Only TWO tables over the first octant are stored, ``M = N/8`` words each:

    C[m] = round(32768 * cos(2*pi*m/N))     m = 1..M
    S[m] = round(32768 * sin(2*pi*m/N))     m = 1..M

(N=64 -> 8+8 words; N=128 -> 16+16 — one cell each, with room to spare.)

For slot index ``p`` (the stage's free-running fill-slot counter, and for a
``P >= 32`` stage ``k == p`` because those stages are always ``s <= 1`` with
``k = p << s``; the ``s=1`` case is handled by walking the counter in steps of
2 — see ``_fold_tab_cell``), the fold is

    r = p & (2M - 1)        (position in the up-down cycle; 2M = N/4)
    m = M - |r - M|         (the triangle walk 0,1..M,M-1..1,0,1..)
    o = (p >> log2 M) & 3   (the octant, 0..3)

and the steering per octant reconstructs the DIF-conjugated pair
``(c, d) = (Re W, Im W)`` with ``W = cos(theta) - j*sin(theta)``:

    o = 0:  ( +C[m], -S[m] )        o = 1:  ( +S[m], -C[m] )
    o = 2:  ( -S[m], -C[m] )        o = 3:  ( -C[m], -S[m] )

This is the SAME fold the landed proof pinned (``o = k // (N/8)``,
``m = |k - round_{N/4}(k)|``); the branchless ``r``/``|r-M|`` form is an exact
algebraic restatement, asserted equal slot-for-slot in the test suite. It
reconstructs EVERY non-trivial ``round(32768*x)`` twiddle BIT-EXACTLY at both
N=64 and N=128.

``m == 0`` (where ``C[0] = 32768`` would be unrepresentable in Q15) occurs at
EXACTLY the two trivial slots ``k = 0`` (identity) and ``k = N/4`` (``-j``),
both of which are dispatched STRUCTURALLY by the sentinel path and never read
the tables — so the fold never needs an unrepresentable word. This is asserted
exhaustively, not argued.

NUMERICS, ORDER, SCALE, LATENCY (unchanged contracts from FFT16)
-----------------------------------------------------------------

  * Unconditional ``>>1`` per stage with round-half-to-even, computed
    16-bit-safe with saturating combines — the R2Butterfly leg programs.
    **Output = FFT/N** (``log2 N`` scaled stages).
  * Output is in **BIT-REVERSED bin order** (``output_bins(N)``); there is
    deliberately no reorder buffer.
  * Latency is ``N-1`` samples; one complex output pair per input trigger from
    the first trigger, startup transient included in the bit-exact contract.
  * Each stage carries the always-on serialize-LOCK (INV-19).

GOLDEN: the bit-exact streaming integer model :func:`sdf_streaming_reference`
(the cycle-accurate R2SDF schedule over the shared ``fft_primitives``
arithmetic), which the suites re-assert against an independently transcribed
direct DIF integer FFT and against float ``numpy.fft.fft`` (SNR floors).

.. _geometry-limit:

STATUS — N=64 (read this before trusting the block)
------------------------------------------------------------

Stated precisely, because "it places" is NOT "it works":

**Verified** (gated by ``verification/tests/test_fft64_fit_limit.py``):

  * the octant fold, exhaustively bit-exact at N=64 and N=128 — including the
    STRIDED stage (N=128 stage 1 walks the same tables with ``k = 2j``) and
    both trivial encodings — with INV-4 negatives;
  * the whole fold CHAIN simulated cell by cell, reproducing every shipped
    ``stage_table`` word;
  * EVERY authored cell of both sizes inside the 32-word budget (resolver-
    measured), with every state var explicitly pinned (INV-33);
  * the whole-block cell counts, WITH the ring-fold parity pads:
    **N=64 = 84 cells** (81 + 3 pad cells), N=128 = 114.
  * the SPINE LAYOUT's structural properties, on the real placed block: every
    consecutive chain pair edge-adjacent; ``ctl`` above ``out`` above the next
    ``ctl`` for every stage; ZERO ROUTE-TIME FACE RULE violations; and all
    **349 forward internal edges trace to exactly their chain distance** (the
    same audit gives FFT16 0/188).

**THE INV-33 OVERLAP DEFECT, and how it was fixed.** Two cells were EXACTLY
one word over budget, and the overflow landed on their pinned STATE register:
``s0_mcalc`` (state ``t`` at address 8, instructions based at 8) and
``s1_fetch_d`` (state ``ptr`` at 21, instructions based at 21). Because the
word COUNT gate only checks ``max_addr + 1 + instr_count <= 32``, a cell at
exactly 32/32 passes it while its state sits ON TOP of its own first
instruction. The block therefore placed, routed, built and ran all 84 cells —
and then the first ``MOVE R{state}, R0`` ZEROED the instruction word the next
trigger enters at, so only the first sample's word egressed.

Each cell bought its word back WITHOUT changing any arithmetic (both proven
by exhaustive on-chip equality against the unreduced program):

  * ``mcalc`` — a dead ``BR.Z +0`` pad (the conditional negate only ever has
    to skip the negate itself) and a ``CMP`` that re-derived a Z flag the
    preceding ``SUB`` had already set (MOVE does not touch flags). The
    conditional negate, the triangle walk and the trivial dispatch are
    untouched.
  * ``fetch_d`` — the ``c`` forward moved to the INV-33 accumulator-delivery
    idiom: it arrives in R0 and is re-emitted by the cell's FIRST
    instruction, which frees both the input register and the staging MOVE.
    The table walk and the pointer wrap are untouched.

Both are now gated: ``test_fft64_fit_limit`` and the repo-wide
``test_cell_program_reachability`` assert NO authored cell pins data or state
into its instruction region, with an INV-4 negative that re-inflates each
pre-fix shape and asserts the gate catches it at exactly the addresses above.

**THE SECOND DEFECT: a dead dispatch entry (INV-35).** With the overlap fixed
the block streamed, and was still wrong — in a way an 80-sample run could not
see. ``swap`` had ONE jump port, wired unconditionally to ``sign``'s ``num``
entry, so ``sign``'s ``triv`` entry was UNREACHABLE and the fold emitted
numeric words on its two structurally trivial slots (``k = 0`` and
``k = N/4``) instead of the sentinel encoding ``steer`` dispatches on. Thirty
of thirty-two slots were right; the two wrong ones put the ENTIRE odd-bin
half of every frame out. It was found by reading the ``steer`` cell's latched
``(csav, dsav)`` off a running chip trigger by trigger — wrong at exactly
triggers 0 and 16 and nowhere else. ``swap`` now has ``t_num`` and ``t_triv``
and both are wired.

Note the reach arithmetic, because it is why this hid: the first valid output
is at ``N-1 = 63``, and frame slots ``0..31`` are the EVEN bins (stage 0's
SUM branch). The TWIDDLED half is not reached until output 95, so any run
shorter than that says nothing about the fold.

**THE LAYOUT: a vertical CTL/OUT SPINE** (this REPLACES the stacked-band
scheme, which did not fit and whose two "walls" were both artefacts of the
banding rather than fabric rules — see :meth:`LargeFFTBlock._plan`).

Every R2SDF stage's ``out`` cell needs three @1 neighbours, and the shipped
FFT16 gets them all from one arrangement that generalises directly:

  * its own ``ctl`` directly ABOVE (the data write-back + serialize-LOCK
    clear, emitted on the in-program ``face_fb`` = NORTH);
  * the NEXT stage's ``ctl`` directly BELOW (the forward packet, on the
    in-program ``face_tap`` = SOUTH, which is also the cell's RESTING face and
    therefore what the router traces);
  * for the LAST stage, a free neighbour instead of a next ``ctl``, which the
    egress corridor starts from.

So the whole ``ctl``/``out`` sequence is ONE COLUMN of ``2 * n_stages`` cells.
That is forced, not stylistic: an internal handoff's hop is resolved by
TRACING resting faces (``router._get_routing_distance``) and silently falls
back to Manhattan distance when the trace fails — so a non-traceable
inter-stage handoff does not error, it ships a wrong hop and the stage spins
on its own ring forever.

What is NOT forced — and is exactly what the band scheme got wrong — is the
rest of the stage. Only ``ctl`` and ``out`` are pinned to the spine; a
stage's other cells merely have to form a connected, face-abutted chain from
``ctl`` round to ``out``, and may bulge sideways past their own rows. Freed
of "one full-width 2-row band per stage", N=64 fits.

Two constraints do survive, and both are real:

  1. **PARITY** (:data:`RING_FOLD_NEEDS_EVEN_LENGTH`). A chain of ``L`` cells
     can land its last cell edge-adjacent to its first only when ``L`` is
     EVEN — chessboard-colour the array and every step flips the colour. An
     odd stage is repaired by spreading its delay line over ONE extra cell
     (:func:`_delay_segments`, ``extra_cells``): same total delay, even chain.
  2. **SPINE HEIGHT.** ``2 * n_stages`` rows in one column: 12 for N=64, which
     is exactly the array height (the spine column avoids the port columns, so
     row 0 IS usable — the old 11-row budget assumed a 10-wide fold); and 14
     for N=128, which does not exist. **N=128 single-die is ruled out on the
     spine height**, and the stage-boundary 2-die split is its topology.

Constructing a size that does not fit raises :class:`LargeFFTGeometryError`
with the exact shortfall — a LOUD failure, not a silently-unroutable layout.
"""
from collections import deque
from itertools import product as _product
from typing import Dict, List, Sequence, Tuple

import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock
from .complex_delay_line_block import ComplexDelayLineBlock
from .fft_primitives import (
    HALF_Q15, KIND_ID, KIND_MJ, KIND_MUL, SAT_POS_Q15, R2ButterflyBlock,
    TRIVIAL_SENTINEL, quantize_twiddle, rhe_half_diff, rhe_half_sum, s16,
    twiddle_cmul_ref, u16)

Q15_ONE = 32768

#: Twiddle period at or above which a direct fetch table busts a 32-word cell
#: (measured: P=16 with the ``c`` forward is exactly 32/32 words).
DIRECT_TABLE_MAX = 16


class LargeFFTGeometryError(NotImplementedError):
    """Raised when an authored size does not fit the chip-scale FOLD geometry.

    The block's ARITHMETIC and CELL PROGRAMS are complete and verified at both
    N = 64 and N = 128 (every cell inside the 32-word budget; the octant fold
    bit-exact against the shipped twiddle tables). What does not yet fit is
    the stacked-2-row-band LAYOUT — see the module docstring's GEOMETRY LIMIT
    section for the exact shortfall and the two candidate mechanisms.

    This is deliberately a LOUD failure rather than a silently-wrong layout:
    a block that builds but does not route looks identical to a working one
    until you run it.
    """


def bit_reverse(k: int, bits: int) -> int:
    """Reverse the low ``bits`` bits of ``k``."""
    out = 0
    for _ in range(bits):
        out = (out << 1) | (k & 1)
        k >>= 1
    return out


def output_bins(n: int) -> Tuple[int, ...]:
    """Output slot ``k`` of a frame carries frequency bin ``output_bins(n)[k]``
    (the standard DIF bit-reversed order)."""
    bits = int(n).bit_length() - 1
    return tuple(bit_reverse(k, bits) for k in range(n))


def stage_delays(n: int) -> Tuple[int, ...]:
    """Per-stage delay depth ``D = (n/2) >> s``."""
    bits = int(n).bit_length() - 1
    return tuple((n // 2) >> s for s in range(bits))


def octant_tables(n: int) -> Tuple[List[int], List[int]]:
    """The two octant tables ``C[1..M]`` and ``S[1..M]``, ``M = n/8``.

    Returned as 0-based lists (``C_list[m-1] == C[m]``) since the fold's
    ``m`` is always >= 1 for a non-trivial slot (proven: ``m == 0`` happens
    only at the two structurally-trivial slots).
    """
    M = n // 8
    C = [int(np.round(np.cos(2.0 * np.pi * m / n) * Q15_ONE))
         for m in range(1, M + 1)]
    S = [int(np.round(np.sin(2.0 * np.pi * m / n) * Q15_ONE))
         for m in range(1, M + 1)]
    return C, S


def fold_index(p: int, n: int) -> Tuple[int, int]:
    """The branchless fold: return ``(m, o)`` for twiddle exponent ``p``.

    ``m = M - |(p & (2M-1)) - M|`` and ``o = (p >> log2 M) & 3`` with
    ``M = n/8``. Exactly equal to the landed ``m = |k - round_{N/4}(k)|`` /
    ``o = k // (N/8)`` formulation (asserted slot-for-slot in the suite).
    """
    M = n // 8
    r = p & (2 * M - 1)
    m = M - abs(r - M)
    o = (p // M) & 3
    return m, o


def fold_words(p: int, n: int, C: Sequence[int], S: Sequence[int]
               ) -> Tuple[int, int]:
    """Reconstruct the ``(c, d)`` word pair for exponent ``p`` from the octant
    tables — the per-octant sign/swap steering, bit-exact."""
    m, o = fold_index(p, n)
    if m == 0:
        raise ValueError(
            f"fold_words: m == 0 at p={p} (n={n}) — that is a TRIVIAL slot "
            "(k = 0 or k = N/4) and must be dispatched structurally, never "
            "through the octant tables (C[0] = 32768 is unrepresentable)")
    c_mag, s_mag = C[m - 1], S[m - 1]
    if o == 0:
        c, d = c_mag, -s_mag
    elif o == 1:
        c, d = s_mag, -c_mag
    elif o == 2:
        c, d = -s_mag, -c_mag
    else:
        c, d = -c_mag, -s_mag
    return u16(c), u16(d)


def stage_table(n: int, s: int) -> List[Tuple[str, int, int]]:
    """Stage ``s``'s twiddle table, ``(kind, c_word, d_word)`` per slot.

    Slot ``j`` (``j = 0..D-1``) holds ``W_n^(j * 2^s)``, with the trivial
    angles detected by INDEX (``k == 0`` -> identity, ``4k == n`` -> ``-j``)
    so the special-casing is exact, never a float comparison on cos/sin.
    """
    D = (n // 2) >> s
    rows: List[Tuple[str, int, int]] = []
    for j in range(D):
        k = j << s
        if k == 0:
            rows.append((KIND_ID, TRIVIAL_SENTINEL, 0x0000))
        elif 4 * k == n:
            rows.append((KIND_MJ, TRIVIAL_SENTINEL, TRIVIAL_SENTINEL))
        else:
            th = 2.0 * np.pi * k / n
            rows.append(quantize_twiddle(complex(np.cos(th), -np.sin(th))))
    return rows


# ---------------------------------------------------------------------------
# Bit-exact streaming golden (the transcribed cycle-accurate R2SDF schedule).
# ---------------------------------------------------------------------------

class _SDFStageModel:
    """One R2SDF stage of the golden: delay ``D``, table ``tw`` (D entries)."""

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


def sdf_streaming_reference(n: int, iq_words, stages_range=None
                            ) -> List[Tuple[int, int]]:
    """The bit-exact per-trigger output stream of the N-point streaming FFT.

    ``iq_words`` is a list of ``(i, q)`` uint16 Q15 word pairs; the return is
    one ``(i, q)`` output pair PER INPUT TRIGGER — including the first ``n-1``
    startup outputs of the zero-initialized pipeline. From output index
    ``n-1`` on, every ``n`` consecutive outputs are one frame in bit-reversed
    bin order (:func:`output_bins`), scaled FFT/n.

    ``stages_range`` — ``(lo, hi)`` INCLUSIVE — runs only that contiguous
    span of stages, which is what a 2-DIE SPLIT half computes. Because the
    stages are a pure feed-forward pipeline, running ``(0, k)`` and feeding
    its output stream into ``(k+1, last)`` is EXACTLY the whole reference:
    that composition identity is the split's correctness argument, and the
    suite asserts it word for word rather than taking it on faith.
    """
    delays = stage_delays(n)
    lo, hi = stages_range if stages_range else (0, len(delays) - 1)
    stages = [_SDFStageModel(delays[s], stage_table(n, s))
              for s in range(lo, hi + 1)]
    out: List[Tuple[int, int]] = []
    for (xi, xq) in iq_words:
        vi, vq = u16(xi), u16(xq)
        for st in stages:
            vi, vq = st.step(vi, vq)
        out.append((vi, vq))
    return out


def direct_dif_reference(n: int, iq_words) -> List[Tuple[int, int]]:
    """An INDEPENDENT transcription: the same FFT computed frame-at-a-time by
    a direct recursive DIF, not the streaming schedule.

    Used by the suite to re-assert ``streaming == direct`` at each N (the
    third transcription of the same arithmetic). Consumes whole frames of
    ``n`` samples and returns ``n`` outputs per frame, in bit-reversed order,
    with the identical per-stage RHE ``>>1`` and twiddle numerics.
    """
    bits = int(n).bit_length() - 1
    out: List[Tuple[int, int]] = []
    for f in range(len(iq_words) // n):
        buf = [(u16(i), u16(q)) for (i, q) in iq_words[f * n:(f + 1) * n]]
        size = n
        for s in range(bits):
            half = size // 2
            nxt: List[Tuple[int, int]] = [(0, 0)] * n
            for blk in range(n // size):
                base = blk * size
                for j in range(half):
                    ai, aq = buf[base + j]
                    bi, bq = buf[base + half + j]
                    si = rhe_half_sum(ai, bi)
                    sq = rhe_half_sum(aq, bq)
                    di = rhe_half_diff(ai, bi)
                    dq = rhe_half_diff(aq, bq)
                    kind, c, d = _direct_tw(n, j * (n // size))
                    ti, tq = twiddle_cmul_ref(di, dq, kind, c, d)
                    nxt[base + j] = (si, sq)
                    nxt[base + half + j] = (ti, tq)
            buf = nxt
            size = half
        out.extend(buf)
    return out


def _direct_tw(n: int, k: int) -> Tuple[str, int, int]:
    if k == 0:
        return quantize_twiddle(1)
    if 4 * k == n:
        return quantize_twiddle(-1j)
    th = 2.0 * np.pi * k / n
    return quantize_twiddle(complex(np.cos(th), -np.sin(th)))


# ---------------------------------------------------------------------------
# The octant-fold steering, in the exact form the CELLS implement.
# ---------------------------------------------------------------------------
# The 4-way per-octant steering factorises into three INDEPENDENT decisions,
# which is what makes it fit a cell (asserted equal to the 4-way form, and to
# the direct quantization, exhaustively at both N):
#
#     swap   = o0 XOR o1          (o = 2*o1 + o0)   -> c takes S, d takes C
#     c sign = o1                 (negative when the high octant bit is set)
#     d sign = ALWAYS negative
#
# and no negate can overflow: the largest stored magnitude is C[1] (32610 at
# N=64, 32729 at N=128), always < 32768, so -mag is representable and the fold
# needs NO saturating combine. (m == 0, where C[0] = 32768 WOULD be
# unrepresentable, is unreachable — it occurs only at the two trivial slots,
# which never read the tables.)


def fold_steer(c_mag: int, s_mag: int, o: int) -> Tuple[int, int]:
    """The cells' swap/sign steering: ``(c_mag, s_mag, o) -> (c, d)`` words."""
    o0, o1 = o & 1, (o >> 1) & 1
    swap = o0 ^ o1
    cm = s_mag if swap else c_mag
    dm = c_mag if swap else s_mag
    return u16(-cm if o1 else cm), u16(-dm)


def fold_slot_words(p: int, n: int, C: Sequence[int], S: Sequence[int]
                    ) -> Tuple[int, int]:
    """``fold_index`` + table lookup + :func:`fold_steer` — the whole chain as
    the cells compute it, for exponent ``p``."""
    m, o = fold_index(p, n)
    if m == 0:
        raise ValueError(f"fold_slot_words: trivial slot p={p} (n={n})")
    return fold_steer(C[m - 1], S[m - 1], o)


def _delay_segments(samples: int, extra_cells: int = 0) -> List[int]:
    """Split a stage line of ``samples`` physical complex samples into
    ComplexDelayLine-density segments (``SAMPLES_PER_CELL`` = 5), balanced so
    no cell is fuller than it must be.

    ``samples == 0`` (the last stage, D = 1) returns ``[]`` — that stage uses
    a plain store-and-forward relay, exactly as FFT16 does.

    ``extra_cells`` spreads the SAME total over that many ADDITIONAL cells —
    the RING-FOLD PARITY fix (see :meth:`LargeFFTBlock._plan`). The segment
    count changes; ``sum(result)`` does NOT, so the stage's delay — and
    therefore the whole transform — is bit-identical either way. Raises
    ``ValueError`` if the split would leave a cell holding no samples.
    """
    per = ComplexDelayLineBlock.SAMPLES_PER_CELL
    if samples <= 0:
        if extra_cells:
            raise ValueError(
                "cannot pad a zero-length delay line (the D=1 stage uses a "
                "relay, and it is already an EVEN-length stage)")
        return []
    n = -(-samples // per) + int(extra_cells)     # ceil, plus the parity pad
    if n > samples:
        raise ValueError(
            f"cannot spread {samples} samples over {n} cells (a delay cell "
            "must hold at least one sample)")
    base, rem = divmod(samples, n)
    return [base + (1 if i < rem else 0) for i in range(n)]


def ring_fold(rows: int, width: int) -> List[Tuple[int, int]]:
    """The RING FOLD: a serpentine over a ``rows`` x ``width`` box that starts
    at ``(0, 0)`` and ENDS at ``(0, 1)`` — i.e. the chain's LAST cell lands
    directly BELOW its FIRST, with every consecutive pair edge-adjacent.

    This generalises the shipped FFT16 2-row band to ANY number of rows, which
    is what lets a stage longer than ``2 * width`` keep the ``out``-below-
    ``ctl`` geometry its R2SDF ring depends on (the @1 write-back + lock-clear)
    instead of being forced into an ever-wider 2-row band.

    The walk is: EAST along row 0; then a column-major boustrophedon over
    columns ``width-1 .. 1`` covering rows ``1 .. rows-1``; then UP the
    reserved RETURN LANE (column 0, rows ``rows-1 .. 1``), landing on
    ``(0, 1)``.

    Legality (asserted, and exhaustively gated by the suite): ``rows == 2`` is
    valid at any width; ``rows >= 3`` needs an EVEN ``width`` so the
    boustrophedon over the body ends in the row that steps into the lane.
    """
    if rows < 2 or width < 2:
        raise ValueError(f"ring_fold needs rows>=2 and width>=2 "
                         f"(got {rows}x{width})")
    if rows >= 3 and width % 2:
        raise ValueError(
            f"ring_fold({rows}, {width}): a box of 3+ rows needs an EVEN "
            "width — with an odd width the body boustrophedon ends away "
            "from the return lane and the walk breaks adjacency")
    path: List[Tuple[int, int]] = [(x, 0) for x in range(width)]
    down = True
    for x in range(width - 1, 0, -1):
        span = range(1, rows) if down else range(rows - 1, 0, -1)
        path.extend((x, y) for y in span)
        down = not down
    path.extend((0, y) for y in range(rows - 1, 0, -1))
    return path


def ring_walks(rows: int, width: int, limit: int = 400):
    """Enumerate RING WALKS of a ``rows`` x ``width`` box: Hamiltonian paths
    that start at ``(0, 0)`` and END edge-adjacent to it, so the chain's last
    cell (the stage's ``out``) abuts its first (the stage's ``ctl``).

    :func:`ring_fold` is ONE such walk — the tidy boustrophedon-plus-return-
    lane. It is not always the right one: which walk you pick decides which
    cells end up ADJACENT, and the ROUTE-TIME FACE RULE makes some adjacencies
    fatal (see :meth:`LargeFFTBlock._face_rule_ok`). Enumerating the family
    lets the planner choose a walk that is both the right SHAPE and
    face-clean, instead of being stuck with one canonical shape per box.

    Yields at most ``limit`` walks, each a list of ``rows * width`` cells.
    """
    total = rows * width
    out: List[List[Tuple[int, int]]] = []
    path = [(0, 0)]
    seen = {(0, 0)}

    def rec():
        if len(out) >= limit:
            return
        if len(path) == total:
            ex, ey = path[-1]
            if abs(ex) + abs(ey) == 1:          # ends adjacent to (0, 0)
                out.append(list(path))
            return
        x, y = path[-1]
        for dx, dy in ((1, 0), (0, 1), (-1, 0), (0, -1)):
            nb = (x + dx, y + dy)
            if 0 <= nb[0] < width and 0 <= nb[1] < rows and nb not in seen:
                seen.add(nb)
                path.append(nb)
                rec()
                path.pop()
                seen.discard(nb)

    rec()
    return out


def ring_fold_boxes(length: int, max_rows: int, max_width: int
                    ) -> List[Tuple[int, int]]:
    """Every ``(rows, width)`` box of EXACTLY ``length`` cells that fits within
    ``max_rows`` x ``max_width`` and admits a ring walk, fewest rows first.

    ``length`` must be EVEN (:data:`RING_FOLD_NEEDS_EVEN_LENGTH`) — on an
    odd-area box no Hamiltonian path can end adjacent to its start. Both
    dimensions must be at least 2: a single row or column is a LINE, whose
    ends are the full length apart.
    """
    out: List[Tuple[int, int]] = []
    if length % 2:
        return out
    for rows in range(2, max_rows + 1):
        if length % rows:
            continue
        width = length // rows
        if not (2 <= width <= max_width):
            continue
        out.append((rows, width))
    return out


#: Step delta -> the face name a cell rests at to forward along that step.
_FACE_OF = {(1, 0): "east", (-1, 0): "west",
            (0, 1): "south", (0, -1): "north"}


def _self_avoiding_paths(start, goal, length, blocked, width, height,
                         limit=400):
    """Self-avoiding paths of EXACTLY ``length`` cells from ``start`` to
    ``goal`` on a ``width`` x ``height`` grid, avoiding ``blocked``.

    This is how a stage's chain is laid around the ctl/out spine: consecutive
    cells are edge-adjacent (so each cell's resting face points at its chain
    successor and the internal hops trace exactly), the walk never revisits a
    cell, and it ends on the stage's ``out``.

    The parity prune is the same fact the ring fold rests on: each step flips
    the chessboard colour, so a path of ``rem`` more cells can only close a
    Manhattan gap ``d`` when ``d <= rem`` and ``rem - d`` is even.
    """
    out: List[List[Tuple[int, int]]] = []
    path = [start]
    seen = {start}

    def rec():
        if len(out) >= limit:
            return
        cur = path[-1]
        rem = length - len(path)
        if rem == 0:
            if cur == goal:
                out.append(list(path))
            return
        gap = abs(cur[0] - goal[0]) + abs(cur[1] - goal[1])
        if gap > rem or (rem - gap) % 2:
            return
        for dx, dy in ((0, 1), (1, 0), (0, -1), (-1, 0)):
            nb = (cur[0] + dx, cur[1] + dy)
            if not (0 <= nb[0] < width and 0 <= nb[1] < height):
                continue
            if nb in seen or nb in blocked:
                continue
            if nb == goal and rem != 1:
                continue                      # only ARRIVE on the last step
            seen.add(nb)
            path.append(nb)
            rec()
            path.pop()
            seen.discard(nb)
            if len(out) >= limit:
                return

    rec()
    return out


def _shelf_pack(boxes, max_w: int, max_h: int):
    """Shelf-pack ``(rows, width)`` boxes IN ORDER into ``max_w`` x ``max_h``.

    Shelves run left to right; a box that does not fit the current shelf's
    remaining width starts a new shelf below the tallest box so far. Returns
    ``[(rows, width, bx, by), ...]`` or ``None`` if it does not fit.
    """
    out = []
    x = y = shelf_h = 0
    for (rows, width) in boxes:
        if width > max_w:
            return None
        if x + width > max_w:                    # new shelf
            y += shelf_h
            x = shelf_h = 0
        if y + rows > max_h:
            return None
        out.append((rows, width, x, y))
        x += width
        shelf_h = max(shelf_h, rows)
    return out


#: A chain of ``L`` cells can place its LAST cell edge-adjacent to its FIRST
#: only when ``L`` is EVEN. Colour the array like a chessboard: every step to
#: an edge-adjacent cell FLIPS the colour, so cell ``L-1`` has the start's
#: colour XOR ``(L-1) % 2``; adjacency demands the OPPOSITE colour, hence
#: ``L`` even. This is a property of the grid, not of this chip or this fold —
#: it is WHY the shipped FFT16 (stages 14/14/8/8, all even) folded tidily and
#: why an odd-length stage needs the parity pad below.
RING_FOLD_NEEDS_EVEN_LENGTH = True


# ---------------------------------------------------------------------------
# LargeFFTBlock — the chip-scale streaming R2SDF composite.
# ---------------------------------------------------------------------------

class LargeFFTBlock(KyttarBlock):
    """N-point streaming R2SDF FFT for the CHIP-SCALE sizes (N = 64, 128).

    See the module docstring for the architecture, the octant fold, the pinned
    numerics, the BIT-REVERSED output order, the ``FFT/N`` scale, the ``N-1``
    latency, and the per-stage serialize-LOCK. Concrete sizes are the
    :class:`FFT64Block` / :class:`FFT128Block` subclasses.

    CHIP-SCALE PLACEMENT CLASS (``CHIP_SCALE = True``): the fold may be the
    full 10 columns wide and use the full panel height; the perimeter
    routing-channel reservation and the D4 rotation requirement are waived for
    this class ONLY. The ONE placement contract — block input and output
    reachable from the chip's ``x16_in`` / ``x16_out`` ports — is gated end to
    end on a real built chip by the verification suite, not by inspection.

    Interface: one complex input (``xi`` @R1, ``xq`` @R2 on ``s0_ctl``), one
    complex output pair (``out_i``, ``out_q`` on the last stage's ``out``
    cell). 1:1 rate, one output pair per trigger.
    """

    CATEGORY = "math_operators"
    TAGS = ["fft", "spectrum", "radix2", "r2sdf", "dif", "complex",
            "streaming", "chip_scale", "math_operators"]

    #: The declared chip-scale class (see ``KyttarBlock.CHIP_SCALE``).
    CHIP_SCALE = True
    #: A 10-wide fold cannot rotate on a 10x12 array. Identity is what ships;
    #: the suite gates exactly this set.
    CHIP_SCALE_ORIENTATIONS = ((),)

    #: Rows a SOLE-OCCUPANT block actually gets. The x16 ports sit at (0,0)
    #: and (9,0) — both on row 0 — and a full-width block cannot use that row
    #: without its column-0 ``ctl`` landing on the input port cell, so a
    #: 10-wide fold has rows 1..11 = 11, not the full 12.
    CHIP_SCALE_USABLE_ROWS = 11

    #: The chip's x16 port cells. The spine may not occupy them, and no stage
    #: cell may either — a block cell on a port cell costs the block that port.
    _PORT_CELLS = frozenset({(0, 0), (9, 0)})

    #: How many candidate chains per stage the spine solver enumerates before
    #: backtracking. The compact ones sort first, so a solution is normally
    #: found in the first few; the cap bounds a pathological search.
    _SPINE_PATH_CANDIDATES = 400

    #: Concrete subclasses set this.
    N = 0

    _interface = BlockInterface(
        entry_address=1, input_registers=[1, 2], output_registers=[0, 1])

    #: The CONTIGUOUS range of parent-transform stages this instance carries,
    #: as ``(lo, hi)`` inclusive, or ``None`` for the whole transform. A
    #: 2-DIE SPLIT is expressed ENTIRELY through this: each die is the same
    #: class over a different range, so both halves run the SAME builders,
    #: the SAME fold, and the SAME spine planner as the whole transform —
    #: there is no second implementation to drift. See :class:`FFT128Die0`.
    STAGE_RANGE: Tuple[int, int] = None

    def __init__(self, name: str, **kwargs):
        super().__init__(name, **kwargs)
        n = int(self.N)
        if n not in (64, 128):
            raise ValueError(
                f"LargeFFTBlock: N must be 64 or 128 (got {n}). Smaller "
                "transforms are the shipped FFT16Block (44 cells, not "
                "chip-scale); larger ones do not fit this array.")
        self._n = n
        full = stage_delays(n)
        lo, hi = self.STAGE_RANGE if self.STAGE_RANGE else (0, len(full) - 1)
        if not (0 <= lo <= hi < len(full)):
            raise ValueError(
                f"STAGE_RANGE {(lo, hi)} is not a valid stage range of the "
                f"{len(full)}-stage N={n} transform")
        #: PARENT-transform stage index of each local stage. Everything that
        #: depends on WHICH stage of the transform this is — the twiddle
        #: table and the fold's exponent stride ``2^s`` — is resolved through
        #: this, never through the local index.
        self._stage_ids = tuple(range(lo, hi + 1))
        self._delays = tuple(full[s] for s in self._stage_ids)
        self._tables = [stage_table(n, s) for s in self._stage_ids]
        self._octC, self._octS = octant_tables(n)
        # RING-FOLD PARITY: a stage whose chain is ODD cannot land its `out`
        # edge-adjacent to its `ctl` in ANY fold (see
        # RING_FOLD_NEEDS_EVEN_LENGTH). Spread that stage's delay line over
        # ONE extra cell — same total delay, even chain, @1 write-back kept.
        self._segs = {}
        self._parity_padded = []
        for s, D in enumerate(self._delays):
            segs = _delay_segments(D - 1)
            if (self._chain_length(s, len(segs)) % 2) == 1:
                segs = _delay_segments(D - 1, extra_cells=1)
                self._parity_padded.append(s)
            self._segs[s] = segs
        # The plan is a deterministic search over a fixed board, so it is a
        # pure function of (class, N, stage range) — memoize it (see
        # _PLAN_CACHE). A cache MISS still runs the real planner, so a
        # geometry error is raised exactly as before.
        key = (type(self).__name__, n, self._stage_ids)
        if key not in LargeFFTBlock._PLAN_CACHE:
            LargeFFTBlock._PLAN_CACHE[key] = self._plan()
        layout, order, col = LargeFFTBlock._PLAN_CACHE[key]
        self._layout, self._order, self._spine_col = dict(layout), list(order), col

    # ---------------------------------------------------------------- basics
    @property
    def stage_ids(self) -> Tuple[int, ...]:
        """Parent-transform stage index of each local stage."""
        return self._stage_ids

    @property
    def is_split_half(self) -> bool:
        return self.STAGE_RANGE is not None
    def _chain_length(self, s: int, n_segs: int) -> int:
        """Cells in stage ``s``'s chain when its delay line uses ``n_segs``
        segment cells — the arithmetic form of :meth:`_stage_chain`, usable
        BEFORE ``self._segs`` exists (the parity decision needs it)."""
        # ctl + 4 RHE legs + gather + out, plus the twiddle chain, plus the
        # delay line (or the single relay when there is no line).
        n = 5 + 1 + 1
        if self.uses_fold(s):
            n += 9
        elif self.uses_direct(s):
            n += 5
        return n + (n_segs if n_segs else 1)

    @property
    def n_stages(self) -> int:
        return len(self._delays)

    @property
    def latency(self) -> int:
        """Samples of pipeline latency THIS instance contributes.

        Each R2SDF stage contributes exactly its delay ``D``, so the whole
        transform is ``sum(D) = N-1`` and a split half is the sum over the
        stages it carries. The two halves of a split therefore add back to
        ``N-1`` — asserted in the suite, not assumed."""
        return sum(self._delays)

    @property
    def output_bins(self) -> Tuple[int, ...]:
        """Bin carried by each output slot.

        Only meaningful for a WHOLE transform: a split half emits a partially
        transformed stream, not frequency bins."""
        if self.is_split_half:
            raise ValueError(
                f"{type(self).__name__} carries stages "
                f"{self._stage_ids[0]}..{self._stage_ids[-1]} of an N={self._n} "
                "transform, not the whole transform — its output is a "
                "partially transformed stream, not frequency bins. Ask the "
                "PARENT transform for output_bins.")
        return output_bins(self._n)

    def uses_fold(self, s: int) -> bool:
        """Stage ``s`` needs the octant fold (its twiddle period busts a
        32-word direct fetch cell)."""
        return self._delays[s] > DIRECT_TABLE_MAX

    def uses_direct(self, s: int) -> bool:
        """Stage ``s`` uses the shipped FFT16 direct-table twiddle chain."""
        return 4 <= self._delays[s] <= DIRECT_TABLE_MAX

    @property
    def cell_count(self) -> int:
        return len(self._order)

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    # ------------------------------------------------ NEW: the fold chain
    # SIX cells replace the two direct fetch cells for a P >= 32 stage. Every
    # one is resolver-verified inside the 32-word budget; the split is forced
    # BY that budget, not chosen for elegance:
    #
    #   seq   — the slot SEQUENCER: a free-running slot counter, emitting the
    #           in-cycle position ``r = p & (2M-1)`` and the octant ``o``.
    #   mcalc — the triangle index ``m = M - |r - M|``, plus the trivial-slot
    #           detect (m == 0) which emits a SAFE index and a marked control
    #           word so the table cells stay branch-free.
    #   tab_c — holds C[1..M]; LOADs C[m].
    #   tab_d — holds S[1..M]; LOADs S[m], forwarding C[m].
    #   swap  — selects which magnitude feeds c and which feeds d
    #           (``swap = o0 XOR o1``).
    #   sign  — applies the signs (c negated on ``o1``, d ALWAYS negated) and
    #           emits the trivial sentinel pair on the trivial entry.
    #
    # Downstream of ``sign`` the words are byte-identical to what a direct
    # table would have produced, so the SHIPPED steer/prods/rail/gather chain
    # consumes them unchanged.
    #
    # CONTROL WORD ``k``: 0..3 = the octant; bit 15 set = a trivial slot, with
    # bit 0 selecting identity (0) vs -j (1). The tables are never indexed on
    # a trivial slot (proven: m == 0 only at k = 0 and k = N/4).

    def _fold_seq_cell(self, s: int) -> CellProgram:
        """The fold slot sequencer for stage ``s``.

        Emits, per FILL trigger, ``r = p & (2M-1)`` (the position within the
        up-down octant cycle) and ``o = (p >> log2 M) & 3`` (the octant), then
        advances the slot counter by ``2^s`` — the stage's twiddle exponent
        stride, which is what lets one octant table serve a strided stage
        (N=128 stage 1 walks k = 2j over the same 16+16 words).

        The counter is kept modulo ``4M = N/2`` with one AND, so the windows
        are exact forever with no reset logic. All shift counts are build-time
        immediates (INV-34).
        """
        n = self._n
        M = n // 8
        log2M = M.bit_length() - 1
        return CellProgram(
            inputs=[],
            outputs=[Port("r_f"), Port("o_f"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("mask", 2 * M - 1, address=2),
                  DataWord("three", 3, address=3),
                  DataWord("step", 1 << s, address=4),
                  DataWord("pmask", 4 * M - 1, address=5)],
            state=[StateVar("p", register=6, initial_value=0),
                   StateVar("t", register=7, initial_value=0)],
            assembly_template=(
                "default:\n"
                "    SHR R{state:p}, #%d\n" % log2M +
                "    MOVE R{state:t}, R0\n"
                "    AND R{state:t}, R{data:three}\n"
                "    {write:o_f}\n"
                "    AND R{state:p}, R{data:mask}\n"
                "    {write:r_f}\n"
                "    ADD R{state:p}, R{data:step}\n"
                "    MOVE R{state:t}, R0\n"
                "    AND R{state:t}, R{data:pmask}\n"
                "    MOVE R{state:p}, R0\n"
                "    {jump:trig}\n"),
        )

    def _fold_mcalc_cell(self) -> CellProgram:
        """The triangle index ``m = M - |r - M|`` and the trivial-slot mark.

        ``|r - M|`` is one SUB plus a single conditional negate. When the
        result is ``m == 0`` the slot is trivial (k = 0 or k = N/4, proven
        exhaustively); the cell then emits a SAFE table index (1) and sets the
        control word's bit 15, with bit 0 distinguishing identity from ``-j``
        — so the two table cells downstream stay straight-line (no branch, no
        sentinel compare) and simply never USE the value they loaded.

        WORD BUDGET (INV-33). This cell is EXACTLY full, and its state ``t``
        is PINNED at address 8 — one word below the first instruction. Two
        redundancies in the first draft were removed to buy that word back,
        both semantics-preserving and both gated slot-for-slot against the
        unreduced sequence by ``test_fft64_fit_limit`` :

        1. The conditional negate skipped TWO instructions (``BR.NN +2``) over
           ``SUB zero, R0`` plus a ``BR.Z +0`` no-op pad. The pad is dead: on
           the not-taken (``r >= M``) path R0 already holds ``r - M >= 0``,
           which IS ``|r - M|``, so the branch only ever needs to skip the
           negate itself — ``BR.NN +1``, one word shorter, same landing.
        2. ``CMP t, zero`` re-derived a Z flag that ``SUB mconst, t`` had
           ALREADY set for the very same value: the only instruction between
           them is a ``MOVE``, and MOVE does not touch the flags. The
           ``BR.Z triv`` therefore reads an identical Z either way.

        Neither touches the negation or the zero-compare SEMANTICS — the
        conditional negate, the triangle walk, and the trivial dispatch are
        bit-identical; only two never-observable words are gone.
        """
        M = self._n // 8
        return CellProgram(
            inputs=[Port("r", register=1), Port("o", register=2)],
            outputs=[Port("m_f"), Port("k_f"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("mconst", M, address=3),
                  DataWord("zero", 0, address=4),
                  DataWord("one", 1, address=5),
                  DataWord("tid", 0x8000, address=6),
                  DataWord("tmj", 0x8001, address=7)],
            state=[StateVar("t", register=8, initial_value=0)],
            assembly_template=(
                "default:\n"
                "    SUB R{in:r}, R{data:mconst}\n"
                "    BR.NN +1\n"
                "    SUB R{data:zero}, R0\n"
                "    MOVE R{state:t}, R0\n"
                "    SUB R{data:mconst}, R{state:t}\n"
                "    MOVE R{state:t}, R0\n"
                "    BR.Z triv\n"
                "    MOVE R0, R{state:t}\n"
                "    {write:m_f}\n"
                "    MOVE R0, R{in:o}\n"
                "    {write:k_f}\n"
                "    {jump:trig}\n"
                "    HALT\n"
                "triv:\n"
                "    MOVE R0, R{data:one}\n"
                "    {write:m_f}\n"
                "    MOVE R0, R{data:tid}\n"
                "    CMP R{in:o}, R{data:zero}\n"
                "    BR.Z +1\n"
                "    MOVE R0, R{data:tmj}\n"
                "    {write:k_f}\n"
                "    {jump:trig}\n"),
        )

    @staticmethod
    def _fold_tab_cell(table: Sequence[int], forward: bool) -> CellProgram:
        """One octant TABLE cell: ``LOAD`` the ``m``-th word and forward it.

        ``table`` is ``C[1..M]`` or ``S[1..M]`` (M = 8 at N=64, 16 at N=128 —
        one cell each). ``m`` arrives 1-based and the cell adds its OWN table
        base, so the two tables can sit at different addresses in their
        respective cells. Straight-line: no branch, because ``mcalc`` already
        guaranteed a safe index (the loaded value is simply unused on a
        trivial slot).

        WORD BUDGET at M = 16 (INV-33). ``tab_d`` — the FORWARDING cell —
        carries a THIRD input (``prev``, the C[m] that ``tab_c`` already
        loaded) plus a staging MOVE, and at N = 128 that makes it exactly one
        word over: the resolver then pins its ``ad`` state on top of its own
        first instruction (address 21 with instructions based at 21), which
        builds cleanly and zeroes itself on the first trigger. It is bought
        back the same way as ``fetch_d``: ``prev`` arrives in **R0**
        (INV-33 accumulator delivery) and the cell's FIRST instruction
        re-emits it, before ``ADD``/``LOAD`` can disturb R0 — freeing the
        input register and the staging MOVE with the table LOOKUP untouched.
        N = 64 (M = 8) has slack either way; the two sizes stay one program.
        """
        M = len(table)
        base = 3
        data = [DataWord(f"w{i}", u16(w), address=base + i)
                for i, w in enumerate(table)]
        # LOAD [Rn] is mem[mem[Rn] & 0x1F]; m is 1-based, so the address of
        # entry m is base + m - 1.
        data.append(DataWord("obase", base - 1, address=base + M))
        state = [StateVar("ad", register=base + M + 1, initial_value=0)]
        inputs = [Port("m", register=1), Port("k", register=2)]
        outs = [Port("v_f"), Port("k_f"), Port("trig")]
        if forward:
            # R0 delivery: no input register, no staging MOVE, and the
            # forward is the FIRST instruction (before ADD/LOAD touch R0).
            inputs.append(Port("prev", register=0))
            outs.append(Port("prev_f"))
            pre = "    {write:prev_f}\n"
            extra = ""
        else:
            outs.append(Port("m_f"))
            pre = ""
            extra = ("    MOVE R0, R{in:m}\n"
                     "    {write:m_f}\n")
        return CellProgram(
            inputs=inputs, outputs=outs,
            entries=[EntryPoint("default")],
            data=data, state=state,
            assembly_template=(
                "default:\n"
                + pre +
                "    MOVE R{state:ad}, R{in:m}\n"
                "    ADD R{state:ad}, R{data:obase}\n"
                "    MOVE R{state:ad}, R0\n"
                "    LOAD R{state:ad}\n"
                "    {write:v_f}\n"
                + extra +
                "    MOVE R0, R{in:k}\n"
                "    {write:k_f}\n"
                "    {jump:trig}\n"),
        )

    @staticmethod
    def _fold_swap_cell() -> CellProgram:
        """Select which magnitude feeds ``c`` and which feeds ``d``.

        ``swap = o0 XOR o1`` (octants 1 and 2 take ``c`` from S and ``d`` from
        C).

        THE TRIVIAL SLOT IS A SEPARATE EXIT, and it must be. Path identity in
        this chain travels as WHICH ENTRY the next cell is jumped at (the
        shipped TwiddleMultiply idiom) — that is the only reason ``sign``
        fits 32 words. So this cell has TWO jump ports:

          ``t_num``  -> ``sign``'s ``num`` entry  (a numeric slot)
          ``t_triv`` -> ``sign``'s ``triv`` entry (k = 0 or k = N/4)

        A single ``trig`` port wired to ``num`` — which is what the first
        version shipped — leaves ``sign``'s ``triv`` entry UNREACHABLE, and
        the fold then emits numeric words on the two trivial slots instead of
        the sentinel encoding the downstream ``steer`` dispatches on. That is
        invisible on the 30 non-trivial slots and wrong on exactly two, which
        is why it survived the standalone fold-chain check (which jumped the
        entries by hand) and only showed up on a real chip, as the ENTIRE
        odd-bin half of every frame being wrong.

        Note the branch structure: the trivial test (control bit 15) must NOT
        share the no-swap ``pass`` label, because those two cases now leave
        through different jumps. ``k`` also has to reach ``sign`` UNMODIFIED
        on the trivial path — ``sign``'s ``triv`` derives ``d`` as
        ``k << 15``, so the ``SHR R{in:k}, #1`` of the swap test must stay on
        the numeric side of the branch (it does: the branch is taken first).
        """
        return CellProgram(
            inputs=[Port("cmag", register=1), Port("smag", register=2),
                    Port("k", register=3)],
            outputs=[Port("cm_f"), Port("dm_f"), Port("k_f"),
                     Port("t_num"), Port("t_triv")],
            entries=[EntryPoint("default")],
            data=[DataWord("one", 1, address=4)],
            state=[StateVar("t", register=5, initial_value=0)],
            assembly_template=(
                # TRIVIAL first, and it falls through to NOTHING: it writes
                # the control word and leaves on its own jump. The two
                # NUMERIC paths differ only in WHICH magnitude goes where, so
                # they share the k_f write and the t_num jump (`emit`) — that
                # sharing is what keeps this cell inside its word budget with
                # the second exit added.
                "default:\n"
                "    MOVE R{state:t}, R{in:k}\n"
                "    SHR R{state:t}, #15\n"
                "    BR.NZ triv\n"
                "    SHR R{in:k}, #1\n"
                "    MOVE R{state:t}, R0\n"
                "    XOR R{state:t}, R{in:k}\n"
                "    MOVE R{state:t}, R0\n"
                "    AND R{state:t}, R{data:one}\n"
                "    BR.Z noswap\n"
                "    MOVE R0, R{in:smag}\n"
                "    {write:cm_f}\n"
                "    MOVE R0, R{in:cmag}\n"
                "    {write:dm_f}\n"
                "    BR.NN emit\n"
                "noswap:\n"
                "    MOVE R0, R{in:cmag}\n"
                "    {write:cm_f}\n"
                "    MOVE R0, R{in:smag}\n"
                "    {write:dm_f}\n"
                "emit:\n"
                "    MOVE R0, R{in:k}\n"
                "    {write:k_f}\n"
                "    {jump:t_num}\n"
                "    HALT\n"
                "triv:\n"
                "    MOVE R0, R{in:k}\n"
                "    {write:k_f}\n"
                "    {jump:t_triv}\n"),
        )

    @staticmethod
    def _fold_sign_cell() -> CellProgram:
        """Apply the fold's signs and emit the ``(c, d)`` word pair.

        ``d`` is ALWAYS negated; ``c`` is negated exactly when the octant's
        high bit is set. Negation is ``MUL`` by ``0xFFFF`` — the low-16 MUL is
        an EXACT two's-complement negate, and no clamp is needed because every
        stored magnitude is < 32768 (asserted).

        The trivial path is a SEPARATE ENTRY (the TwiddleMultiply idiom: path
        identity travels as WHICH entry the cell is jumped at), which is what
        keeps this cell inside 32 words. It emits the sentinel ``c`` and
        derives ``d`` branchlessly as ``k << 15`` — 0x8000 for ``-j`` and
        0x0000 for identity, exactly the shipped trivial encodings.
        """
        return CellProgram(
            inputs=[Port("cm", register=1), Port("dm", register=2),
                    Port("k", register=3)],
            outputs=[Port("c_f"), Port("d_f"), Port("t_n"), Port("t_t")],
            entries=[EntryPoint("num"), EntryPoint("triv")],
            data=[DataWord("zero", 0, address=4),
                  DataWord("one", 1, address=5),
                  DataWord("neg1", 0xFFFF, address=6),
                  DataWord("sent", TRIVIAL_SENTINEL, address=7)],
            state=[StateVar("t", register=8, initial_value=0)],
            assembly_template=(
                "num:\n"
                "    MUL R{in:dm}, R{data:neg1}\n"
                "    {write:d_f}\n"
                "    SHR R{in:k}, #1\n"
                "    MOVE R{state:t}, R0\n"
                "    AND R{state:t}, R{data:one}\n"
                "    BR.Z cpos\n"
                "    MUL R{in:cm}, R{data:neg1}\n"
                "    {write:c_f}\n"
                "    {jump:t_n}\n"
                "    HALT\n"
                "cpos:\n"
                "    MOVE R0, R{in:cm}\n"
                "    {write:c_f}\n"
                "    {jump:t_n}\n"
                "    HALT\n"
                "triv:\n"
                "    MOVE R0, R{data:sent}\n"
                "    {write:c_f}\n"
                "    SHL R{in:k}, #15\n"
                "    {write:d_f}\n"
                "    {jump:t_t}\n"),
        )

    # ------------------------ REUSED VERBATIM from the shipped FFT16 builders
    # These are the proven cell programs; only the per-stage D and the table
    # words differ. Importing the class and calling its staticmethods keeps
    # ONE definition of each program (no transcription drift).

    def _ctl_cell(self, s: int) -> CellProgram:
        """The stage controller / landing cell (FFT16 ``_ctl_cell`` shape).

        Holds the feedback pair (ai, aq — written back by the stage's ``out``
        cell), the free-running phase counter, and the FILL/BUTTERFLY entry
        dispatch (``cnt AND D``: D is a power of two, so bit log2(D) of the
        counter IS the half-period selector, and the free-running 16-bit wrap
        is exact because 2^16 is a multiple of 2D at every N here). Engages
        the serialize-LOCK after dispatch.

        The SECOND-TO-LAST stage (D = 2) additionally forwards the fill-slot
        parity as a kind word to its gather cell (its two fill twiddles are
        1 and -j) — the FFT16 stage-2 shape, generalised by delay not index.
        """
        D = self._delays[s]
        external = (s == 0)
        in_names = ("xi", "xq") if external else ("bi", "bq")
        kw = (D == 2)
        outputs = [Port("ai_f"), Port("bi_f"), Port("aq_f"), Port("bq_f")]
        if kw:
            outputs.append(Port("kw_f"))
        outputs += [Port("t_fill"), Port("t_bfly")]
        kw_lines = ("    AND R{state:cnt}, R{data:one}\n"
                    "    {write:kw_f}\n") if kw else ""
        return CellProgram(
            inputs=[Port(in_names[0], register=1),
                    Port(in_names[1], register=2)],
            outputs=outputs,
            entries=[EntryPoint("default")],
            data=[DataWord("one", 1, address=3),
                  DataWord("dmask", D, address=4),
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
        """RHE sum leg (one rail) — the R2Butterfly program, FFT16 shape."""
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
        """RHE difference leg (one rail) — R2Butterfly, FFT16 shape."""
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
    def _fetch_cell(table_words: Sequence[int], has_c_input: bool
                    ) -> CellProgram:
        """A DIRECT twiddle table cell (the FFT16 ``_fetch_cell`` arithmetic).

        WORD BUDGET at P = 16 (INV-33). FFT16's largest direct table is P = 8,
        so its ``fetch_d`` had slack; the FIRST P = 16 ``fetch_d`` — FFT64's
        stage 1 — is EXACTLY one word over, and the overflow lands on the
        state pointer, which the resolver happily pins ON TOP of the cell's
        own first instruction (the word count gate passes, the OVERLAP is
        fatal: the first ``MOVE R{ptr}, R0`` zeroes the instruction the next
        trigger enters at).

        The word is bought back with the ACCUMULATOR-DELIVERY idiom that
        INV-33 documents: the ``c`` forward arrives in **R0** rather than in a
        dedicated input register, and the cell's FIRST instruction re-emits it
        before any ALU op can disturb R0. That frees BOTH the input register
        (address 1) and the ``MOVE R0, R{in:c}`` staging instruction — two
        words — with the table arithmetic, the pointer walk, and the wrap
        completely untouched.

        The idiom's own precondition is met exactly: ``c_f`` is the first
        write, so the value is out before ``LOAD`` overwrites R0, and the
        upstream ``fetch_c`` delivers into R0 as its last write before the
        trigger.
        """
        P = len(table_words)
        base = 2
        data = [DataWord(f"t{i}", u16(w), address=base + i)
                for i, w in enumerate(table_words)]
        data += [DataWord("one", 1, address=base + P),
                 DataWord("pend", base + P, address=base + P + 1),
                 DataWord("pbase", base, address=base + P + 2)]
        ptr_reg = base + P + 3
        if has_c_input:
            # R0 delivery (INV-33 accumulator-delivery): no input register, no
            # staging MOVE — the forward is the FIRST instruction.
            inputs = [Port("c", register=0)]
            fwd = "    {write:c_f}\n"
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
                + fwd +
                "    LOAD R{state:ptr}\n"
                "    {write:t_f}\n"
                "    ADD R{state:ptr}, R{data:one}\n"
                "    MOVE R{state:ptr}, R0\n"
                "    CMP R0, R{data:pend}\n"
                "    BR.NZ +1\n"
                "    MOVE R{state:ptr}, R{data:pbase}\n"
                "    {jump:trig}\n"),
        )

    @staticmethod
    def _steer_cell() -> CellProgram:
        """TwiddleMultiply kind dispatch (FFT16 ``_steer_cell``, verbatim)."""
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
        """The four pinned floor-MULQs (FFT16 ``_prods_cell``, verbatim)."""
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
        """The yi rail / trivial sub-dispatch (FFT16 ``_rail_cell``)."""
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
        """Per-kind combine for a twiddle stage (FFT16 ``_gather_tw_cell``)."""
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
    def _gather_kw_cell() -> CellProgram:
        """D = 2 combine (FFT16 ``_gather_s2_cell``): BUTTERFLY passes
        (si, sq); FILL dispatches on the forwarded slot-parity kind word —
        slot 0 identity, slot 1 the structural ``-j``. No multiply."""
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
    def _gather_id_cell() -> CellProgram:
        """D = 1 combine (FFT16 ``_gather_s3_cell``): its only twiddle is
        W^0 = 1, so FILL is a pure pass-through."""
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
        cell program verbatim (FFT16 ``_delay_cell``)."""
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
        """The D = 1 stage's depth-0 'delay line': a store-and-forward relay
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
        """The stage exit (FFT16 ``_out_cell``, verbatim): snapshot the
        combine result, write the emerging pair back into ``ctl``'s (ai, aq)
        and clear ``ctl``'s serialize-LOCK, then emit the complex packet on
        the tap face. The three ordering rules FFT16 pinned all carry over
        (yi/yq snapshot first; wb+WRITE.CFG before the packet writes so the
        packet writes are the LAST data writes for the egress patchers; the
        FACE restored before the trailing jump)."""
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

    # ---------------------------------------------------------- chain + fold
    def _stage_chain(self, s: int) -> List[str]:
        """The ordered cell chain for stage ``s``.

        CHAIN ORDER IS LOAD-BEARING (the ROUTE-TIME FACE RULE): the router
        derives each cell's route-time face from its LAST-listed internal
        connection **when that dst is physically adjacent**, else from the
        dict-NEXT cell, and internal write/jump DISTANCES are resolved by
        TRACING those faces. So every cell's last-listed dst must be its chain
        SUCCESSOR or NON-adjacent. Sum legs precede diff legs (so a diff leg
        is never adjacent to the delay-push cell it targets), exactly as
        FFT16 pinned after the bug that motivated the rule.
        """
        D = self._delays[s]
        p = f"s{s}_"
        chain = [p + c for c in ("ctl", "sumi", "sumq", "diffi", "diffq")]
        if self.uses_fold(s):
            chain += [p + c for c in ("seq", "mcalc", "tab_c", "tab_d",
                                      "swap", "sign", "steer", "prods",
                                      "rail")]
        elif self.uses_direct(s):
            chain += [p + c for c in ("fetch_c", "fetch_d", "steer", "prods",
                                      "rail")]
        chain.append(p + "gather")
        segs = self._segs[s]
        if segs:
            chain += [p + f"d{i}" for i in range(len(segs))]
        else:
            chain.append(p + "relay")
        chain.append(p + "out")
        _ = D
        return chain

    #: Resolved layouts, keyed by ``(class, N, STAGE_RANGE)``. The plan is a
    #: pure function of those three — a deterministic search over a fixed
    #: board — so memoizing it is safe, and it matters: the 84-cell folds cost
    #: ~26s each under the (correct, router-faithful) corridor check, and the
    #: suites construct them repeatedly. Cached, a second construction is
    #: free. Correctness does not depend on this; only wall-clock does.
    _PLAN_CACHE: Dict[Tuple, Tuple] = {}

    def _plan(self):
        """Lay the stages out on a VERTICAL CTL/OUT SPINE and return
        ``(layout, order, spine_column)``.

        THE GEOMETRY, and why it is forced (this is the correction to the old
        stage-BAND scheme, which did not fit):

        Every R2SDF stage's ``out`` cell needs THREE @1 neighbours, and the
        shipped FFT16 gets all three from one arrangement:

          * its own ``ctl`` directly ABOVE it — the data write-back and the
            serialize-LOCK clear, emitted on the in-program ``face_fb``
            (NORTH);
          * the NEXT stage's ``ctl`` directly BELOW it — the forward packet,
            emitted on the in-program ``face_tap`` (SOUTH), which is also the
            cell's RESTING face and therefore what the router traces;
          * (for the last stage) the routed egress instead of a next ``ctl``.

        So the whole ``ctl``/``out`` sequence is ONE COLUMN of ``2 *
        n_stages`` cells: ``ctl_s`` at ``(C, 2s)``, ``out_s`` at ``(C, 2s+1)``,
        ``ctl_{s+1}`` at ``(C, 2s+2)``. That is not a convention — an internal
        handoff's hop is resolved by TRACING resting faces
        (``router._get_routing_distance``) and SILENTLY falls back to Manhattan
        distance when the trace fails, so a non-traceable inter-stage handoff
        does not error: it ships a wrong hop, the packet lands in the wrong
        cell, and the stage spins on its own ring forever (measured — stage 0
        alone consumed every simulator event and stage 1 never ran).

        What is NOT forced, and is where the old planner went wrong, is the
        rest of the stage. Only ``ctl`` and ``out`` are pinned to the spine;
        the other cells merely have to form a connected, face-abutted chain
        from ``ctl`` round to ``out``. They do NOT have to sit in the stage's
        own 2-row band, and once they may bulge sideways past their own rows
        the block fits: the old scheme's "one full-width 2-row band per stage"
        cost 12 rows for a 10-wide array it also could not fill, whereas the
        spine costs ``2 * n_stages`` rows in ONE column and lets the stages
        interleave around it.

        ROW BUDGET: the spine needs ``2 * n_stages`` rows — 12 for N=64, which
        is the whole array, and 14 for N=128, which is more than exists. The
        spine column must avoid columns 0 and 9 so that ROW 0 is free of the
        x16 ports (a block that is not full-width may use row 0; the old
        planner's 11-row budget assumed a 10-wide fold, which is what made
        N=64 look one row short).

        Raises :class:`LargeFFTGeometryError` if no spine placement exists.
        """
        chains = [self._stage_chain(s) for s in range(self.n_stages)]
        for s, ch in enumerate(chains):
            if len(ch) % 2:
                raise LargeFFTGeometryError(
                    f"stage {s} has an ODD chain of {len(ch)} cells, so its "
                    "`out` cannot land edge-adjacent to its `ctl` in ANY "
                    "fold (the grid parity theorem). __init__'s delay-line "
                    "parity pad should have prevented this.")
        total = sum(len(c) for c in chains)
        W, H = self.CHIP_SCALE_MAX_WIDTH, self.CHIP_SCALE_MAX_HEIGHT
        rows_needed = 2 * self.n_stages
        if rows_needed > H:
            raise LargeFFTGeometryError(
                f"N={self._n} has {self.n_stages} stages, whose ctl/out spine "
                f"needs {rows_needed} rows in ONE column, but the array is "
                f"only {H} tall. Shortfall: {rows_needed - H} rows. Every "
                "stage's out cell must have its own ctl directly above it and "
                "the next stage's ctl directly below it, so the spine height "
                "is not negotiable — the stage-boundary 2-die split is the "
                "supported topology at this size.")
        if total > W * H - len(self._PORT_CELLS):
            raise LargeFFTGeometryError(
                f"N={self._n} needs {total} cells but the array holds "
                f"{W * H - len(self._PORT_CELLS)} outside the x16 port cells. "
                f"Shortfall: {total - (W * H - len(self._PORT_CELLS))}.")
        sol = None
        for col in self._spine_columns(W):
            sol = self._solve_spine(chains, col, W, H)
            if sol is not None:
                break
        if sol is None:
            raise LargeFFTGeometryError(
                f"N={self._n}: {total} cells fit the {W}x{H} array and the "
                f"{rows_needed}-row spine fits its height, but no spine "
                "column admits a set of disjoint, face-abutted stage chains "
                "around it.")
        col, paths = sol
        used_cells = {c for p in paths.values() for c in p}
        layout: Dict[str, Tuple[int, int, str]] = {}
        order: List[str] = []
        for s, ch in enumerate(chains):
            path = paths[s]
            for i, cid in enumerate(ch):
                if i < len(ch) - 1:
                    nxt = path[i + 1]
                elif s < self.n_stages - 1:
                    nxt = paths[s + 1][0]      # out -> next ctl (SOUTH)
                else:
                    # THE BLOCK EXIT. It must NOT rest toward its own ctl: the
                    # resting face is what the router traces and what the build
                    # rewrites to the routed egress direction (output_face_addr),
                    # and a cell resting back into its ctl re-enters the stage
                    # instead of leaving the block. The shipped FFT16's last out
                    # rests SOUTH, away from its ctl, into the free cell the
                    # egress corridor starts from. Pick the free neighbour that
                    # is not the ctl (the corridor check guarantees one exists).
                    nxt = self._exit_rest_cell(path[i], path[i - 1] if i else
                                               path[0], used_cells, W, H)
                px, py = path[i]
                layout[cid] = (px, py, _FACE_OF[(nxt[0] - px, nxt[1] - py)])
                order.append(cid)
        return layout, order, col

    @staticmethod
    def _exit_rest_cell(out_pos, prev_pos, occupied, W: int, H: int):
        """The cell the block's EXIT rests toward: a neighbour that is free of
        block cells (so the egress corridor can start there), never the exit's
        own ``ctl`` and never its chain predecessor."""
        cx, cy = out_pos[0], out_pos[1] - 1          # this stage's ctl
        for dx, dy in ((0, 1), (1, 0), (-1, 0), (0, -1)):
            nb = (out_pos[0] + dx, out_pos[1] + dy)
            if not (0 <= nb[0] < W and 0 <= nb[1] < H):
                continue
            if nb == (cx, cy) or nb == prev_pos or nb in occupied:
                continue
            return nb
        raise LargeFFTGeometryError(
            f"the block exit at {out_pos} has no free neighbour to rest "
            "toward — the egress corridor has nowhere to start")

    def _spine_columns(self, width: int) -> List[int]:
        """Candidate spine columns, best first.

        The spine may not sit in a column holding an x16 port (that would put
        a ``ctl`` or ``out`` on the port cell and cost the block row 0), and a
        middle column leaves usable space on BOTH sides for the stages to
        bulge into, which is what makes the packing solvable at all.
        """
        blocked = {x for (x, _y) in self._PORT_CELLS}
        mid = width / 2.0
        return sorted((c for c in range(width) if c not in blocked),
                      key=lambda c: (abs(c + 0.5 - mid), c))

    def _solve_spine(self, chains, col: int, W: int, H: int):
        """Place every stage as a self-avoiding, face-abutted chain from its
        spine ``ctl`` to its spine ``out``, with all stages disjoint.

        Returns ``(col, {stage: [(x, y), ...]})`` or ``None``. Stages are
        assigned in order with backtracking; among the candidate chains for a
        stage the ones that stay CLOSEST to its own spine rows are tried
        first, which keeps each stage compact and leaves the rest of the array
        contiguous for the stages that follow.

        RESERVED EGRESS COLUMN — why the enumerator, not just the final check,
        has to know about the ports. :meth:`_corridors_ok` runs only after ALL
        stages are placed, so it can reject but not steer. The enumerator is a
        fixed-order DFS that returns at most ``_SPINE_PATH_CANDIDATES`` walks,
        and for a LONG chain in a SHORT spine those walks are all the same
        shape: a tall wall on one side of the spine spanning every row. On the
        N=128 die-0 half (a 30-cell stage-0 chain over a 2-row spine) all 400
        candidates ran west of the spine across all 12 rows, and every single
        one sealed the exit off from ``x16_out`` — the input corridor was
        always fine, the OUTPUT corridor never was. Sorting a biased sample
        cannot fix that.

        So a column between the spine and the output port is RESERVED: no
        stage cell may occupy it, which guarantees a free north-south lane the
        egress corridor can always use, and forces the enumerator to produce
        walks that leave it alone. The reservation is tried progressively —
        first with no reservation at all (the shipped N=64 fold needs none and
        must keep its exact placement), then reserving each candidate column
        in turn — so this can only ADD solutions, never remove one.
        """
        for reserved in self._egress_reservations(col, W):
            sol = self._solve_spine_with(chains, col, W, H, reserved)
            if sol is not None:
                return sol
        return None

    @staticmethod
    def _egress_reservations(col: int, W: int):
        """Columns to try reserving as a free egress lane, easiest first.

        ``None`` first — no reservation, which is what the shipped N=64 fold
        resolves with, so its placement is bit-identical to before. Then the
        columns strictly EAST of the spine (the output port is at the east
        end of row 0), nearest the spine first: a lane close to the spine
        costs the stages least room.
        """
        return [None] + [c for c in range(col + 1, W)]

    def _solve_spine_with(self, chains, col: int, W: int, H: int, reserved):
        n = len(chains)
        spine = [((col, 2 * s), (col, 2 * s + 1)) for s in range(n)]
        all_spine = {p for pair in spine for p in pair}
        if all_spine & self._PORT_CELLS:
            return None
        lane = (set() if reserved is None
                else {(reserved, y) for y in range(H)} - self._PORT_CELLS)
        used = set(self._PORT_CELLS)
        chosen: Dict[int, List[Tuple[int, int]]] = {}

        def walk(s: int) -> bool:
            if s == n:
                return self._corridors_ok(used, chosen, W, H)
            start, goal = spine[s]
            blocked = set(used) | lane | (all_spine - {start, goal})
            for cands in self._stage_candidates(
                    start, goal, len(chains[s]), blocked, W, H, s):
                for path in cands:
                    chosen[s] = path
                    used.update(path)
                    if walk(s + 1):
                        return True
                    used.difference_update(path)
                    chosen.pop(s, None)
            return False

        return (col, chosen) if walk(0) else None

    def _stage_candidates(self, start, goal, length, blocked, W, H, s):
        """Yield BATCHES of candidate chains for one stage, shape-diverse.

        WHY BATCHES, AND WHY BY HEIGHT. The enumerator is a fixed-order DFS
        capped at ``_SPINE_PATH_CANDIDATES``; over a full-height board its cap
        is spent entirely on ONE shape family — for a long chain over a short
        spine, a tall wall spanning every row. Those all wall the ports off
        from each other, so the corridor check rejects every one of them and
        the stage looks unplaceable when it is only unlucky (measured: 0 of
        400 candidates routable for the N=128 die-0 chain).

        THE FULL-HEIGHT BATCH IS FIRST, ALWAYS. That keeps the shipped N=64
        fold bit-identical AND keeps its construction fast: a size that
        already places pays nothing for this, because the extra batches are
        never generated. Only when the full-height batch yields no placement
        does the search fall back to SHRUNK boards — a height cap, shortest
        viable fold first — which forces the enumerator to spend its budget
        on WIDE, SHORT folds that leave a free lane along the array instead of
        rediscovering the same wall.

        A chain of ``length`` cells folded into a strip ``h`` rows tall needs
        ``h * W >= length``, and the spine itself needs ``2*(s+1)`` rows;
        together those set the floor the fallback starts from.
        """
        def batch(cap):
            cands = _self_avoiding_paths(
                start, goal, length, blocked, W, cap,
                limit=self._SPINE_PATH_CANDIDATES)
            if not cands:
                return None
            cands.sort(key=lambda p: (max(abs(c[1] - 2 * s) for c in p),
                                      sum(abs(c[1] - 2 * s) for c in p)))
            return cands

        full = batch(H)
        if full:
            yield full
        floor = max(2 * (s + 1), -(-length // W))
        for cap in range(floor, H):
            got = batch(cap)
            if got:
                yield got

    #: The router's hop ceiling: a corridor longer than this cannot be
    #: expressed in the 5-bit HOP_CNT field, so a placement that needs one is
    #: unroutable however free the cells are.
    _MAX_CORRIDOR_HOPS = 31

    def _corridors_ok(self, used, chosen, W: int, H: int) -> bool:
        """Will the ROUTER actually route this fold's two nets?

        A fold that fills the array builds and then fails to route — the
        shipped FFT16 is 7 wide on a 10-wide chip precisely so that free
        columns remain (INV-8/9).

        THIS MIRRORS THE ROUTER, which is not the same as "both ends are
        connected somewhere". Three things the naive check got wrong, each
        measured on a real failing placement:

          1. **The two nets SHARE occupancy.** The router lays them one at a
             time and RESERVES each finished corridor against the next, so
             two nets that are each individually routable can still collide.
             Worse, the ORDER is not the block's to choose — nets are routed
             in project connection order, which is whatever the caller
             happened to add first, and the two orders are not equivalent
             (measured: the N=128 die-0 placement routed ingress-then-egress
             and failed on the SECOND net, having passed a check that assumed
             egress first). So BOTH orders must succeed. A placement that
             only routes under one connection order is a latent failure
             waiting for a caller to wire its nets the other way round.
          2. **A corridor ends ON the landing cell, not beside it.** The
             router's BFS walks free cells and finishes AT the destination,
             so asking only whether some NEIGHBOUR of the landing is
             reachable passes placements whose landing is walled in.
          3. **Hop count is bounded.** A 5-bit HOP_CNT caps a corridor at
             :data:`_MAX_CORRIDOR_HOPS`; a 26-hop detour around a tall fold
             can be perfectly connected and still unroutable, and an egress
             corridor spends one extra hop leaving the array.

        FOURTH, AND IT INVALIDATES THE OTHER THREE IF IGNORED: **the placer
        NORMALISES a layout to its own bounding box.** ``place_block(x, y)``
        emits each cell at ``x + dx - min_dx``, so a plan whose cells start at
        ``min x = 1`` is SHIFTED ONE COLUMN LEFT when anchored at 0 — and
        every absolute fact this method establishes (port distances, the
        reserved lane, which columns stay free) is then about a layout that no
        longer exists. Measured: the N=128 die-0 plan sat at ``min x = 1``,
        passed this check, and reached the router with block cells on (0,2)
        and (0,3), sealing column 0.

        The fix is NOT to reject un-normalised plans — for a fold that has to
        touch a specific column to leave its corridors, ``min x = 0`` may be
        unreachable, and rejecting it throws away valid geometry. The fix is
        that the block DECLARES the anchor at which its plan is reproduced
        verbatim (:attr:`default_anchor` = ``(min_dx, min_dy)``), and callers
        place it there. Anchoring at the declared value makes
        ``x + dx - min_dx == dx``, i.e. the identity, so what the router sees
        is exactly what was validated here. The shipped N=64 fold declares
        (0, 0) and is unaffected.

        Returns True only when both nets route, in both connection orders.
        """
        block_cells = set(used) - set(self._PORT_CELLS)
        in_cell = chosen[0][0]                       # s0_ctl
        out_cell = chosen[len(chosen) - 1][-1]       # last stage's out
        ports = sorted(self._PORT_CELLS)
        in_port, out_port = ports[0], ports[-1]

        def bfs(start, goal, blocked):
            """Shortest free-cell path start -> goal INCLUSIVE, or None. The
            goal itself is enterable (it is the corridor's last cell); the
            start is not re-entered."""
            if start == goal:
                return [start]
            prev = {start: None}
            q = deque([start])
            while q:
                cur = q.popleft()
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nxt = (cur[0] + dx, cur[1] + dy)
                    if not (0 <= nxt[0] < W and 0 <= nxt[1] < H):
                        continue
                    if nxt in prev:
                        continue
                    if nxt != goal and nxt in blocked:
                        continue
                    prev[nxt] = cur
                    if nxt == goal:
                        path, c = [], nxt
                        while c is not None:
                            path.append(c)
                            c = prev[c]
                        return path[::-1]
                    q.append(nxt)
            return None

        def route_egress(extra):
            """out_cell -> x16_out. The exit emits onto its resting-face
            neighbour so the corridor starts there, and the word spends one
            EXTRA hop leaving the array at the port."""
            p = bfs(out_cell, out_port, (block_cells | extra) - {out_cell})
            if p is None or (len(p) - 1) + 1 > self._MAX_CORRIDOR_HOPS:
                return None
            return p

        def route_ingress(extra):
            """x16_in -> in_cell. The port injects AT its own cell and the
            corridor ends ON the landing."""
            p = bfs(in_port, in_cell, (block_cells | extra) - {in_port})
            if p is None or len(p) - 1 > self._MAX_CORRIDOR_HOPS:
                return None
            return p

        # BOTH connection orders must work — the block does not get to choose
        # which net the caller wires first.
        eg = route_egress(set())
        if eg is None:
            return False
        if route_ingress(set(eg) - {out_cell, out_port}) is None:
            return False

        ing = route_ingress(set())
        if ing is None:
            return False
        if route_egress(set(ing) - {in_cell, in_port}) is None:
            return False
        return True

    # ------------------------------------------------------------------ build
    def build_cell_programs(self) -> Dict[str, CellProgram]:
        """The per-cell programs, emitted in ``self._order`` (the chain order,
        which the layout also follows — INV-33 requires dict order == layout
        order)."""
        made: Dict[str, CellProgram] = {}
        for s in range(self.n_stages):
            p = f"s{s}_"
            D = self._delays[s]
            made[p + "ctl"] = self._ctl_cell(s)
            made[p + "sumi"] = self._sum_leg_cell()
            made[p + "sumq"] = self._sum_leg_cell()
            made[p + "diffi"] = self._diff_leg_cell()
            made[p + "diffq"] = self._diff_leg_cell()
            if self.uses_fold(s):
                # PARENT stage index: the fold's exponent stride is 2^s of the
                # WHOLE transform (a split half's local index is not the
                # transform's), so the sequencer must be built from stage_ids.
                made[p + "seq"] = self._fold_seq_cell(self._stage_ids[s])
                made[p + "mcalc"] = self._fold_mcalc_cell()
                made[p + "tab_c"] = self._fold_tab_cell(self._octC, False)
                made[p + "tab_d"] = self._fold_tab_cell(self._octS, True)
                made[p + "swap"] = self._fold_swap_cell()
                made[p + "sign"] = self._fold_sign_cell()
                made[p + "steer"] = self._steer_cell()
                made[p + "prods"] = self._prods_cell()
                made[p + "rail"] = self._rail_cell()
                made[p + "gather"] = self._gather_tw_cell()
            elif self.uses_direct(s):
                tab = self._tables[s]
                made[p + "fetch_c"] = self._fetch_cell(
                    [c for (_k, c, _d) in tab], False)
                made[p + "fetch_d"] = self._fetch_cell(
                    [d for (_k, _c, d) in tab], True)
                made[p + "steer"] = self._steer_cell()
                made[p + "prods"] = self._prods_cell()
                made[p + "rail"] = self._rail_cell()
                made[p + "gather"] = self._gather_tw_cell()
            elif D == 2:
                made[p + "gather"] = self._gather_kw_cell()
            else:
                made[p + "gather"] = self._gather_id_cell()
            segs = self._segs[s]
            if segs:
                for i, L in enumerate(segs):
                    made[p + f"d{i}"] = self._delay_cell(L)
            else:
                made[p + "relay"] = self._relay_cell()
            made[p + "out"] = self._out_cell(external=(s == self.n_stages - 1))
        # Emit in chain order (== layout order).
        return {cid: made[cid] for cid in self._order}

    def default_layout(self):
        return dict(self._layout)

    @property
    def default_anchor(self) -> Tuple[int, int]:
        """The anchor at which this block's planned layout is reproduced
        VERBATIM — place it here, not at (0, 0).

        ``place_block(x, y)`` emits each cell at ``x + dx - min_dx``, i.e. it
        NORMALISES the footprint to its own bounding box. So anchoring at
        ``(min_dx, min_dy)`` makes that the identity and the router sees
        exactly the geometry :meth:`_corridors_ok` validated; anchoring
        anywhere else TRANSLATES the fold and invalidates every absolute fact
        the plan rests on — the reserved egress lane, the port distances,
        which columns stay free.

        This is not a nicety. The N=128 die-0 fold has to reach column 1 to
        leave its corridors open, so its plan sits at ``min x = 1``; anchored
        at (0, 0) it arrives one column left with cells on (0,2) and (0,3),
        sealing the input port, and the route fails. Anchored at (1, 0) it
        routes. The shipped N=64 fold declares (0, 0) and is unaffected —
        which is precisely why this went unnoticed until a second size
        existed.
        """
        xs = [v[0] for v in self._layout.values()]
        ys = [v[1] for v in self._layout.values()]
        return (min(xs), min(ys))

    # ------------------------------------------------------- multi-cell wiring
    def _push_cell(self, s: int) -> str:
        """The cell the diff legs push the scaled difference into (the head of
        the stage's delay line, or its relay)."""
        p = f"s{s}_"
        return p + ("d0" if self._segs[s] else "relay")

    def _line_tail(self, s: int) -> str:
        """The delay-line cell that feeds the stage's ``out`` write-back."""
        p = f"s{s}_"
        segs = self._segs[s]
        return p + (f"d{len(segs) - 1}" if segs else "relay")

    def internal_connections(self) -> List[Tuple[str, str, str, str]]:
        conns: List[Tuple[str, str, str, str]] = []
        for s in range(self.n_stages):
            p = f"s{s}_"
            D = self._delays[s]
            has_tw = self.uses_fold(s) or self.uses_direct(s)
            gather = p + "gather"
            push = self._push_cell(s)
            a_i_dst, a_q_dst = ((p + "steer", "xi"), (p + "steer", "xq")) \
                if has_tw else ((gather, "ai"), (gather, "aq"))
            # ctl operand fan-out (chain-successor edges LAST per cell — the
            # route-time-face discipline, see _stage_chain).
            if D == 2:
                conns.append((p + "ctl", "kw_f", gather, "kw"))
            conns += [
                (p + "ctl", "aq_f", p + "sumq", "a"),
                (p + "ctl", "bq_f", p + "sumq", "b"),
                (p + "ctl", "ai_f", p + "sumi", "a"),
                (p + "ctl", "bi_f", p + "sumi", "b"),
            ]
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
            conns += [
                (p + "diffi", "v_f", push, "xi"),
                (p + "diffq", "v_f", push, "xq"),
            ]
            if self.uses_fold(s):
                # The fold chain: idx -> tab_c -> tab_d -> fold -> steer.
                conns += [
                    # seq -> mcalc -> tab_c -> tab_d -> swap -> sign -> steer.
                    # Each cell's LAST-listed dst is its chain successor (the
                    # ROUTE-TIME FACE RULE, see _stage_chain).
                    (p + "seq", "o_f", p + "mcalc", "o"),
                    (p + "seq", "r_f", p + "mcalc", "r"),
                    (p + "mcalc", "k_f", p + "tab_c", "k"),
                    (p + "mcalc", "m_f", p + "tab_c", "m"),
                    (p + "tab_c", "k_f", p + "tab_d", "k"),
                    (p + "tab_c", "v_f", p + "tab_d", "prev"),
                    (p + "tab_c", "m_f", p + "tab_d", "m"),
                    # tab_d hands (c_mag, s_mag, k) to the swap select.
                    (p + "tab_d", "k_f", p + "swap", "k"),
                    (p + "tab_d", "v_f", p + "swap", "smag"),
                    (p + "tab_d", "prev_f", p + "swap", "cmag"),
                    (p + "swap", "k_f", p + "sign", "k"),
                    (p + "swap", "dm_f", p + "sign", "dm"),
                    (p + "swap", "cm_f", p + "sign", "cm"),
                    (p + "sign", "d_f", p + "steer", "d"),
                    (p + "sign", "c_f", p + "steer", "c"),
                ]
            elif self.uses_direct(s):
                conns += [
                    (p + "fetch_c", "t_f", p + "fetch_d", "c"),
                    (p + "fetch_d", "t_f", p + "steer", "d"),
                    (p + "fetch_d", "c_f", p + "steer", "c"),
                ]
            if has_tw:
                conns += [
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
            conns += [
                (gather, "yi", p + "out", "yi"),
                (gather, "yq", p + "out", "yq"),
            ]
            # The stage line: gather -> d0 -> ... -> dK -> out (write-back).
            segs = self._segs[s]
            for i in range(len(segs) - 1):
                conns += [
                    (p + f"d{i}", "xi_out", p + f"d{i + 1}", "xi"),
                    (p + f"d{i}", "xq_out", p + f"d{i + 1}", "xq"),
                ]
            tail = self._line_tail(s)
            conns += [
                (tail, "xi_out", p + "out", "awi"),
                (tail, "xq_out", p + "out", "awq"),
            ]
            # Inter-stage packet (forward); the last stage's pair is the egress.
            if s < self.n_stages - 1:
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
        for s in range(self.n_stages):
            p = f"s{s}_"
            D = self._delays[s]
            has_tw = self.uses_fold(s) or self.uses_direct(s)
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
            if self.uses_fold(s):
                jumps += [
                    (p + "diffq", "t_f", p + "seq", "default"),
                    (p + "diffq", "t_b", gather, "id"),
                    (p + "seq", "trig", p + "mcalc", "default"),
                    (p + "mcalc", "trig", p + "tab_c", "default"),
                    (p + "tab_c", "trig", p + "tab_d", "default"),
                    (p + "tab_d", "trig", p + "swap", "default"),
                    # The trivial slot travels as WHICH ENTRY sign is jumped
                    # at (the TwiddleMultiply idiom) — swap dispatches BOTH.
                    # Wiring only `num` here leaves sign's `triv` entry dead
                    # and the fold emits numeric words on k = 0 and k = N/4.
                    (p + "swap", "t_num", p + "sign", "num"),
                    (p + "swap", "t_triv", p + "sign", "triv"),
                    (p + "sign", "t_n", p + "steer", "default"),
                    (p + "sign", "t_t", p + "steer", "default"),
                ]
            elif self.uses_direct(s):
                jumps += [
                    (p + "diffq", "t_f", p + "fetch_c", "default"),
                    (p + "diffq", "t_b", gather, "id"),
                    (p + "fetch_c", "trig", p + "fetch_d", "default"),
                    (p + "fetch_d", "trig", p + "steer", "default"),
                ]
            else:
                jumps += [
                    (p + "diffq", "t_f", gather, "fill"),
                    (p + "diffq", "t_b", gather, "bfly"),
                ]
            if has_tw:
                jumps += [
                    (p + "steer", "t_mul", p + "prods", "mul"),
                    (p + "steer", "t_triv", p + "prods", "triv"),
                    (p + "prods", "t_mul", p + "rail", "mul"),
                    (p + "prods", "t_triv", p + "rail", "triv"),
                    (p + "rail", "t_mul", gather, "mul"),
                    (p + "rail", "t_id", gather, "id"),
                    (p + "rail", "t_mj", gather, "mj"),
                ]
            # gather -> the line -> out
            segs = self._segs[s]
            if segs:
                jumps.append((gather, "trig", p + "d0", "default"))
                for i in range(len(segs) - 1):
                    jumps.append((p + f"d{i}", "fwd", p + f"d{i + 1}",
                                  "default"))
                jumps.append((p + f"d{len(segs) - 1}", "fwd", p + "out",
                              "default"))
            else:
                jumps += [
                    (gather, "trig", p + "relay", "default"),
                    (p + "relay", "fwd", p + "out", "default"),
                ]
            if s < self.n_stages - 1:
                jumps.append((p + "out", "trig", f"s{s + 1}_ctl", "default"))
            _ = D
        return jumps

    def output_cell_id(self):
        """SINGULAR — the block exit is the LAST stage's ``out`` cell: it
        carries the feedback write-back + lock-clear alongside the external
        complex packet, so the build must treat exactly this cell as the exit
        (its packet writes are the LAST data writes; the patchers leave the
        earlier feedback writes and the config write alone)."""
        return f"s{self.n_stages - 1}_out"

    def output_cell_ids(self):
        return [self.output_cell_id()]

    def output_face_addr(self):
        """The exit is a dual-face cell: its packet rides the in-program
        ``face_tap`` word (address 7); declaring it lets the build rewrite it
        to the routed egress direction."""
        return 7

    # -------------------------------------------------------------- reference
    def process_reference_q15(self, iq_words) -> List[Tuple[int, int]]:
        """Bit-exact per-trigger output stream (see
        :func:`sdf_streaming_reference`): one (i, q) uint16 pair per input
        trigger, startup transient included, frames in bit-reversed order.

        For a SPLIT HALF this runs only that half's stages, so the output is
        the partially transformed stream the next die consumes."""
        return sdf_streaming_reference(
            self._n, iq_words,
            (self._stage_ids[0], self._stage_ids[-1]))

    def process_reference(self, input_samples) -> np.ndarray:
        """Float view of the bit-exact stream (complex64, q15/32768 per rail).
        The contract (order/scale/latency) lives in the Q15 model."""
        arr = np.asarray(input_samples)
        if np.iscomplexobj(arr):
            def q15(x):
                return int(round(max(-1.0, min(32767 / 32768.0, float(x)))
                                 * 32768.0)) & 0xFFFF
            words = [(q15(c.real), q15(c.imag)) for c in arr]
        else:
            words = [(int(i) & 0xFFFF, int(q) & 0xFFFF) for (i, q) in arr]
        out = self.process_reference_q15(words)
        return np.array([complex(s16(i) / 32768.0, s16(q) / 32768.0)
                         for (i, q) in out], dtype=np.complex64)


class FFT64Block(LargeFFTBlock):
    """64-point streaming R2SDF FFT — the founding CHIP-SCALE block.

    6 stages (delays 32/16/8/4/2/1), one complex sample in -> one out per
    trigger, latency 63, output in BIT-REVERSED bin order, scale FFT/64.
    Stage 0's twiddle period (32) busts a direct fetch cell, so it uses the
    octant fold (8+8 table words); stages 1..3 use the shipped direct-table
    chain; stages 4 and 5 are the trivial kind-word/identity stages.

    Params: NONE (``n`` is pinned at 64; scale, order, and latency are fixed
    contracts documented on the module).
    """

    N = 64


class FFT128Block(LargeFFTBlock):
    """128-point streaming R2SDF FFT — CHIP-SCALE, TWO DIES.

    7 stages (delays 64/32/16/8/4/2/1), latency 127, BIT-REVERSED bin order,
    scale FFT/128. Stages 0 AND 1 need the octant fold (periods 64 and 32);
    the 16+16-word octant tables serve both (stage 1 walks the same tables
    with a stride-2 exponent, which the fold sequencer handles by advancing
    its slot counter by ``2^s``).

    **Constructing this class RAISES** :class:`LargeFFTGeometryError`: the
    7-stage ctl/out spine needs 14 rows in ONE column against a 12-row array,
    and the spine height is not negotiable (see :meth:`LargeFFTBlock._plan`).
    The supported topology at this size is the STAGE-BOUNDARY 2-DIE SPLIT
    below — :class:`FFT128Die0` and :class:`FFT128Die1`.

    Params: NONE (``n`` is pinned at 128).
    """

    N = 128


#: Where the N=128 pipeline is CUT. Die 0 carries stages 0..SPLIT_STAGE, die 1
#: carries SPLIT_STAGE+1..6.
#:
#: WHY STAGE 0 — measured, and NOT the balanced choice. Cell counts alone
#: suggest cutting in the middle, and that was the first candidate:
#:
#:     after stage 0:  30 / 84 cells,   2 / 12 spine rows   <- shipped
#:     after stage 1:  54 / 60 cells,   4 / 10 spine rows
#:     after stage 2:  70 / 44 cells,   6 /  8 spine rows   <- does NOT place
#:     after stage 3:  84 / 30 cells,   8 /  6 spine rows
#:     after stage 4:  98 / 30 cells,  10 /  4 spine rows
#:     after stage 5: 106 /  8 cells,  12 /  2 spine rows
#:
#: But CELLS ARE NOT THE BINDING CONSTRAINT — SHAPE IS. Cutting after stage 2
#: puts three chains of 30, 24 and 16 cells around a SIX-row spine, and that
#: does not place at ANY spine column with ANY reserved egress lane (measured
#: exhaustively): 70 cells is only 59% of the array, so it fails on the
#: geometry of long chains folded around a short spine, not on area.
#:
#: Cutting after stage 0 places on both dies, and the imbalance is a FEATURE
#: rather than a cost:
#:
#:   * die 0 is the one stage that cannot be anything else — the period-64
#:     octant fold, which exists only at N=128;
#:   * die 1 (stages 1..6, 84 cells, a 12-row spine) is the SAME SHAPE as the
#:     verified FFT64Block, so it inherits geometry that is already proven on
#:     a real chip and the only genuinely new thing in the design is the
#:     CROSSING itself.
#:
#: The goal is a transform that spans two dies with a correctness argument, so
#: concentrating the novelty in one place is worth more than an even split.
SPLIT_STAGE = 0


class _FFT128Half(LargeFFTBlock):
    """Shared base for the two dies of the N=128 split.

    A half is the SAME class over a different ``STAGE_RANGE`` — same cell
    builders, same octant fold, same spine planner, same golden function. That
    is the whole point: there is no second FFT implementation to drift from
    the verified one. Everything that depends on WHICH stage of the transform
    a stage is (its twiddle table and the fold's ``2^s`` exponent stride) is
    resolved through :attr:`stage_ids`, never the local index.

    THE SPLIT IS A SINGLE FEED-FORWARD CROSSING. The R2SDF stages are a pure
    pipeline: stage ``k+1`` consumes stage ``k``'s output stream and nothing
    flows backwards between stages (the only feedback is INSIDE a stage, from
    its own ``out`` to its own ``ctl``). So cutting at a stage boundary needs
    exactly ONE complex stream crossing, in one direction, with no handshake
    beyond the ordinary packet — and the composition identity

        whole(x) == die1(die0(x))

    holds word for word. The suite asserts that identity rather than arguing
    it, and the on-chip gate drives the REAL two-chip system.
    """

    N = 128

    @property
    def latency(self) -> int:
        return sum(self._delays)


class FFT128Die0(_FFT128Half):
    """N=128 die 0 — stage 0 alone (delay 64), 30 cells.

    The period-64 OCTANT FOLD: the one stage of this transform that has no
    counterpart at N=64, and the reason N=128 needs 16+16 octant tables where
    N=64 needs 8+8. Complex in (the transform's input), complex out — the
    partially transformed stream die 1 consumes, NOT frequency bins.
    Contributes 64 of the transform's 127 samples of latency.
    """

    STAGE_RANGE = (0, SPLIT_STAGE)


class FFT128Die1(_FFT128Half):
    """N=128 die 1 — stages 1..6 (delays 32/16/8/4/2/1), 84 cells.

    THE SAME SHAPE AS THE VERIFIED FFT64Block: 84 cells over a 12-row spine,
    six stages, one octant fold at the head followed by direct-table and
    trivial stages. The delays and twiddle tables are of course N=128's
    (stage 1 walks the 16+16 octant tables with a STRIDE-2 exponent, which the
    fold sequencer handles by advancing its slot counter by ``2^s``), but the
    geometry the placer has to solve is one this repo has already proven on a
    real chip.

    Complex in (die 0's output stream), complex out: THIS die's output is the
    transform's, in BIT-REVERSED bin order at scale FFT/128. Contributes 63 of
    the transform's 127 samples of latency.
    """

    STAGE_RANGE = (SPLIT_STAGE + 1, 6)
