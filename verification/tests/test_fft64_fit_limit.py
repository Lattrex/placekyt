# SPDX-License-Identifier: GPL-3.0-or-later
"""FFT64 / FFT128 — the CHIP-SCALE placement class and its fit arithmetic.

HISTORY. The first version of this file was the executable form of the
2026-08-23 quarantine: a 64-point single-block streaming R2SDF FFT could not
fit the 10x12 as ONE block under the ordinary layout conventions (both fold
dimensions <= 8, i.e. a 64-cell cap), and N=128 a fortiori. Its own docstring
named the un-quarantine signal: the wall moves if the substrate — or the
POLICY that sets the cap — changes.

THE POLICY CHANGED (2026-08-24), and the signal fired as designed. A
transform-scale block is typically the SOLE OCCUPANT of a die, so for a
declared **chip-scale block class** two conventions that exist only to keep
MULTIPLE blocks co-resident are waived:

  * the perimeter routing-channel reservation (a chip-scale block may be the
    FULL 10 columns wide and use the full panel height), and
  * the D4 rotation requirement (a 10-wide fold cannot rotate on a 10x12).

THE ONLY PLACEMENT CONTRACT for the class is that the block's input and
output are REACHABLE from the chip's x16 input/output ports — gated end to
end on a real built chip, never by inspection.

This file now encodes the NEW class rules, in four parts:

1. **The chip-scale flag is honored, and ONLY for blocks that declare it** —
   the flag relaxes the layout caps for its own class and changes nothing for
   any other block (the ordinary 8x8 cap still binds them).
2. **The octant-fold twiddle numerics are PROVEN SOUND** (unchanged from the
   original file, and still the reason nobody may mistake a geometry wall for
   a table-size problem): every non-trivial twiddle of the N=64 and N=128
   fold stages is reconstructed BIT-EXACTLY from two octant tables by index
   fold + sign/swap steering, asserted exhaustively with INV-4 negatives.
3. **The fit arithmetic under the NEW caps**, computed from SHIPPED, MEASURED
   builder constants, with the same accounting reproducing the shipped
   FFT16Block. The measured constants CORRECT the original file's
   deliberately-unattainable lower bound for the fold chain (it charged the
   fold at 5 cells "so the wall cannot hinge on the fold-cell estimate"); the
   ATTAINABLE cost, every cell resolver-verified <= 32 words, is 9.
4. **The band cap that the @1 stage-ring discipline imposes** — the rule that
   actually decides whether a given N fits, and the one the original
   whole-block cell count could not see.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_fft64_fit_limit.py -q
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest

_RUNTIME = Path(__file__).resolve().parents[2] / "runtime" / "python"
if str(_RUNTIME) not in sys.path:
    sys.path.insert(0, str(_RUNTIME))

from gr_kyttar.placement.blocks._base import KyttarBlock  # noqa: E402
from gr_kyttar.placement.blocks.complex_delay_line_block import (  # noqa: E402
    ComplexDelayLineBlock)
from gr_kyttar.placement.blocks.fft16_block import FFT16Block  # noqa: E402
from gr_kyttar.placement.blocks.fft_primitives import (  # noqa: E402
    KIND_ID, KIND_MJ, KIND_MUL, TRIVIAL_SENTINEL, quantize_twiddle, s16, u16)
from gr_kyttar.placement.blocks.fir_filter_block import (  # noqa: E402
    FIRFilterBlock)
from gr_kyttar.placement.blocks.gain_block import GainBlock  # noqa: E402

Q15_ONE = 32768

# The ORDINARY single-block footprint cap: both dims <= 8 (INV-9 + the D4
# orientation gate — rotation swaps the dims, so the 10-wide axis's
# bus-channel reservation binds BOTH).
_ACROSS = FIRFilterBlock.MAX_CELLS_ACROSS          # 8, chip-size convention
ORDINARY_CELL_CAP = _ACROSS * _ACROSS              # 64 cells

# This chip.
CHIP_W, CHIP_H = 10, 12
# x16_in is at cell (0, 0) and x16_out at (9, 0) — BOTH on row 0 — and a USED
# chip-port cell is an obstacle to every other net (autoroute reserves it), so
# a chip-scale block cannot occupy row 0. Rows 1..11 are what a sole-occupant
# block actually gets.
CHIP_SCALE_ROWS = CHIP_H - 1                       # 11
CHIP_SCALE_AREA = CHIP_W * CHIP_SCALE_ROWS         # 110 cells


# ---------------------------------------------------------------------------
# Part 1 — the chip-scale flag: declared, honored, and NARROW.
# ---------------------------------------------------------------------------

class _PlainProbe(KyttarBlock):
    """A block that does NOT declare the chip-scale class."""

    @property
    def cell_count(self):
        return 12

    def build_cell_programs(self):
        return {}

    def process_reference(self, x):
        return x


class _ChipScaleProbe(_PlainProbe):
    """A block that DOES declare it."""

    CHIP_SCALE = True


def test_chip_scale_flag_defaults_off():
    """The class is OPT-IN: nothing is chip-scale unless it says so. A block
    that forgets to declare it keeps the ordinary caps — the flag can never
    relax a check by accident."""
    assert KyttarBlock.CHIP_SCALE is False
    assert _PlainProbe("p").CHIP_SCALE is False
    # Shipped ordinary blocks are unaffected.
    assert GainBlock("g").CHIP_SCALE is False
    assert FFT16Block("f").CHIP_SCALE is False


def test_chip_scale_flag_relaxes_only_its_own_class():
    """The flag widens the layout caps for the declaring class ONLY — it is
    not a global loosening. A non-chip-scale block still gets (8, 8)."""
    assert _PlainProbe.layout_caps() == (8, 8)
    assert GainBlock.layout_caps() == (8, 8)
    assert FFT16Block.layout_caps() == (8, 8)
    # The chip-scale class gets the full panel.
    assert _ChipScaleProbe.layout_caps() == (CHIP_W, CHIP_H)


def test_chip_scale_waives_the_rotation_requirement_explicitly():
    """A 10-wide fold cannot rotate on a 10x12, so the class declares the
    orientations it SHIPS rather than silently skipping the D4 gate. Identity
    is mandatory and must be present."""
    assert () in _ChipScaleProbe.CHIP_SCALE_ORIENTATIONS, (
        "identity orientation is mandatory for a chip-scale block")
    # The default declaration is identity-only — anything more is opt-in and
    # must be gated by the block's own suite.
    assert KyttarBlock.CHIP_SCALE_ORIENTATIONS == ((),)


def test_chip_scale_caps_do_not_exceed_the_panel():
    """The waiver is bounded by the physical array, not unbounded."""
    w, h = _ChipScaleProbe.layout_caps()
    assert w <= CHIP_W and h <= CHIP_H


# ---------------------------------------------------------------------------
# Part 2 — the octant-fold twiddle reconstruction (bit-exact, exhaustive).
# ---------------------------------------------------------------------------

def _direct_words(N: int, k: int):
    """The shipped build-time quantization for twiddle exponent k."""
    th = 2.0 * np.pi * k / N
    if k == 0:
        return quantize_twiddle(1)
    if 4 * k == N:
        return quantize_twiddle(-1j)
    return quantize_twiddle(complex(np.cos(th), -np.sin(th)))


def _octant_tables(N: int):
    """COS and SIN over (0, pi/4]: m = 1..N/8, quantized round(32768*x).

    N=64 -> 8+8 words; N=128 -> 16+16. Each table + its fetch program is one
    cell with room to spare — STORAGE is not, and never was, the wall.
    """
    M = N // 8
    C = {m: int(np.round(np.cos(2.0 * np.pi * m / N) * Q15_ONE))
         for m in range(1, M + 1)}
    S = {m: int(np.round(np.sin(2.0 * np.pi * m / N) * Q15_ONE))
         for m in range(1, M + 1)}
    return C, S


def _folded_words(N: int, k: int, C, S, *, corrupt: str = ""):
    """Reconstruct exponent k's (c, d) from the octant tables.

    octant o = k // (N/8); table index m = |k - (nearest multiple of N/4)|;
    steering per octant:  o0: (+C, -S)   o1: (+S, -C)   o2: (-S, -C)
                          o3: (-C, -S)
    (d already carries the DIF conjugation: W = cos - j sin, d = -32768 sin.)
    """
    M = N // 8
    o = k // M
    t = ((k + M) // (2 * M)) * (2 * M)
    m = abs(k - t)
    if corrupt == "wrong_octant_sign" and o == 2:
        return u16(S[m]), u16(-C[m])
    if corrupt == "wrong_fold_quadrant" and o == 3:
        return u16(C[m]), u16(-S[m])
    if o == 0:
        c, d = C[m], -S[m]
    elif o == 1:
        c, d = S[m], -C[m]
    elif o == 2:
        c, d = -S[m], -C[m]
    else:
        c, d = -C[m], -S[m]
    return u16(c), u16(d)


def _folded_words_swapsign(N: int, k: int, C, S, *, corrupt: str = ""):
    """The SAME fold, factorised the way a CELL can compute it:

        swap   = o0 XOR o1        -> c takes S and d takes C
        c sign = o1
        d sign = ALWAYS negative

    This is the form the fold's steering cells implement; it must agree with
    the 4-way form word for word.
    """
    M = N // 8
    r = k & (2 * M - 1)
    m = M - abs(r - M)
    o = (k // M) & 3
    o0, o1 = o & 1, (o >> 1) & 1
    swap = o0 ^ o1
    if corrupt == "no_swap":
        swap = 0
    if corrupt == "d_sign_positive":
        cm = S[m] if swap else C[m]
        dm = C[m] if swap else S[m]
        return u16(-cm if o1 else cm), u16(dm)
    cm = S[m] if swap else C[m]
    dm = C[m] if swap else S[m]
    return u16(-cm if o1 else cm), u16(-dm)


def _fold_stage_exponents(N: int):
    """The twiddle exponents each FOLD stage actually needs.

    Stage s has delay D = (N/2) >> s and exponent k = j * 2^s for slot
    j = 0..D-1. A stage needs the fold when its period D exceeds what a direct
    32-word fetch cell holds (16 table words — measured: P=16 with the ``c``
    forward is exactly 32/32).
    """
    out = []
    bits = int(N).bit_length() - 1
    for s in range(bits):
        D = (N // 2) >> s
        if D > 16:
            out.append((s, [j << s for j in range(D)]))
    return out


@pytest.mark.parametrize("N", [64, 128])
def test_octant_fold_bit_exact_exhaustive(N):
    """EVERY non-trivial twiddle word pair of EVERY fold stage reconstructs
    BIT-EXACTLY from the two octant tables — the direct round(32768*x)
    values, no off-by-one-LSB anywhere (including the k = N/8 and 3N/8
    boundary slots where cos(pi/4) and sin(pi/4) quantize through different
    float paths). Covers the STRIDED stage too (N=128 stage 1 walks the same
    tables with k = 2j)."""
    C, S = _octant_tables(N)
    stages = _fold_stage_exponents(N)
    assert stages, f"N={N} has no fold stage — the fixture is wrong"
    for s, ks in stages:
        trivial = []
        for k in ks:
            kind, c, d = _direct_words(N, k)
            if kind in (KIND_ID, KIND_MJ):
                trivial.append(k)
                continue
            assert kind == KIND_MUL
            assert _folded_words(N, k, C, S) == (c, d), (
                f"N={N} stage {s} k={k}: 4-way fold mismatch")
            assert _folded_words_swapsign(N, k, C, S) == (c, d), (
                f"N={N} stage {s} k={k}: swap/sign fold mismatch")
        # exactly two trivial exponents: W^0 = 1 and W^(N/4) = -j
        assert trivial == [0, N // 4], (N, s, trivial)


@pytest.mark.parametrize("N", [64, 128])
def test_octant_table_index_never_zero_on_a_nontrivial_slot(N):
    """``m == 0`` would need C[0] = 32768, UNREPRESENTABLE in Q15. It occurs
    at EXACTLY the two trivial exponents (k = 0 and k = N/4), which are
    dispatched structurally by the sentinel path and never index the tables.
    This is asserted, not argued — it is what makes the fold safe."""
    M = N // 8
    zero_m = [k for k in range(N // 2)
              if (M - abs((k & (2 * M - 1)) - M)) == 0]
    assert zero_m == [0, N // 4]
    for k in zero_m:
        kind, _c, _d = _direct_words(N, k)
        assert kind in (KIND_ID, KIND_MJ), (
            f"N={N} k={k} indexes m=0 but is NOT a trivial slot")


@pytest.mark.parametrize("N", [64, 128])
def test_octant_magnitudes_are_negatable(N):
    """Every stored magnitude is < 32768, so the fold's negates are exactly
    representable and the steering needs NO saturating combine. (The largest
    is C[1]: 32610 at N=64, 32729 at N=128.)"""
    C, S = _octant_tables(N)
    assert max(abs(v) for v in C.values()) < Q15_ONE
    assert max(abs(v) for v in S.values()) < Q15_ONE


@pytest.mark.parametrize("corrupt", ["wrong_octant_sign", "wrong_fold_quadrant"])
def test_octant_fold_equality_gate_has_teeth(corrupt):
    """INV-4: a corrupted 4-way fold (wrong quadrant sign / un-reflected
    quadrant) must BREAK the exhaustive equality."""
    N = 64
    C, S = _octant_tables(N)
    mismatches = 0
    for k in range(N // 2):
        kind, c, d = _direct_words(N, k)
        if kind != KIND_MUL:
            continue
        if _folded_words(N, k, C, S, corrupt=corrupt) != (c, d):
            mismatches += 1
    assert mismatches > 0, f"corruption {corrupt!r} was invisible to the gate"


@pytest.mark.parametrize("corrupt", ["no_swap", "d_sign_positive"])
def test_swapsign_steering_gate_has_teeth(corrupt):
    """INV-4 for the CELL-SHAPED factorisation: dropping the swap, or
    forgetting that d is ALWAYS negated, must break the equality."""
    N = 64
    C, S = _octant_tables(N)
    mismatches = 0
    for k in range(N // 2):
        kind, c, d = _direct_words(N, k)
        if kind != KIND_MUL:
            continue
        if _folded_words_swapsign(N, k, C, S, corrupt=corrupt) != (c, d):
            mismatches += 1
    assert mismatches > 0, f"corruption {corrupt!r} was invisible to the gate"


def _fold_chain_words(N: int, s: int, slot: int, C, S):
    """Run the AUTHORED fold CHAIN's arithmetic step by step, exactly as the
    cells compute it — seq -> mcalc -> tab_c/tab_d -> swap -> sign — including
    the control-word encoding and the trivial-slot path.

    This is a transcription of the shipped cell programs in
    ``gr_kyttar.placement.blocks.fft_large``; it catches a design error in the
    fold before any chip run, and pins the encodings the downstream (shipped,
    proven) steer/prods/rail/gather chain depends on.
    """
    M = N // 8
    log2M = M.bit_length() - 1
    # seq: the running twiddle exponent, stride 2^s, modulo 4M = N/2.
    p = (slot * (1 << s)) & (4 * M - 1)
    o = (p >> log2M) & 3
    r = p & (2 * M - 1)
    # mcalc: the triangle index + the trivial-slot mark.
    m = M - abs(r - M)
    if m == 0:
        m_out, k = 1, (0x8000 if o == 0 else 0x8001)
    else:
        m_out, k = m, o
    # tab_c / tab_d (never meaningfully read on a trivial slot).
    cmag, smag = C[m_out], S[m_out]
    # swap.
    if k & 0x8000:
        cm, dm = cmag, smag
    else:
        cm, dm = ((smag, cmag) if (((k >> 1) ^ k) & 1) else (cmag, smag))
    # sign.
    if k & 0x8000:
        return TRIVIAL_SENTINEL, u16(k << 15)
    return (u16(-cm) if ((k >> 1) & 1) else u16(cm)), u16(-dm)


@pytest.mark.parametrize("N", [64, 128])
def test_fold_chain_reproduces_the_shipped_stage_tables(N):
    """END TO END over the fold cells' own arithmetic: every slot of every
    fold stage — trivial slots included — comes out BIT-IDENTICAL to the
    shipped ``stage_table`` words the direct path would have produced. This
    is what lets the SHIPPED steer/prods/rail/gather chain consume the fold's
    output unchanged."""
    from gr_kyttar.placement.blocks.fft_large import (  # noqa: PLC0415
        DIRECT_TABLE_MAX, octant_tables, stage_delays, stage_table)
    Cl, Sl = octant_tables(N)
    C = {m: Cl[m - 1] for m in range(1, len(Cl) + 1)}
    S = {m: Sl[m - 1] for m in range(1, len(Sl) + 1)}
    checked = 0
    for s, D in enumerate(stage_delays(N)):
        if D <= DIRECT_TABLE_MAX:
            continue
        tab = stage_table(N, s)
        for slot, (_kind, ec, ed) in enumerate(tab):
            assert _fold_chain_words(N, s, slot, C, S) == (ec, ed), (
                f"N={N} stage {s} slot {slot}")
            checked += 1
    assert checked > 0


def test_fold_chain_gate_has_teeth():
    """INV-4: perturbing the fold chain's stride must break the equality
    (the strided N=128 stage 1 is the case a wrong stride would silently
    corrupt)."""
    from gr_kyttar.placement.blocks.fft_large import (  # noqa: PLC0415
        octant_tables, stage_table)
    N = 128
    Cl, Sl = octant_tables(N)
    C = {m: Cl[m - 1] for m in range(1, len(Cl) + 1)}
    S = {m: Sl[m - 1] for m in range(1, len(Sl) + 1)}
    tab = stage_table(N, 1)
    # Walk stage 1 with stage 0's stride (1 instead of 2) — must diverge.
    mismatches = sum(1 for slot, (_k, ec, ed) in enumerate(tab)
                     if _fold_chain_words(N, 0, slot, C, S) != (ec, ed))
    assert mismatches > 0


# ---------------------------------------------------------------------------
# Part 3 — the fit arithmetic under the NEW caps (measured constants only).
# ---------------------------------------------------------------------------

# Per-stage spine, measured on the shipped FFT16Block: ctl + the four RHE leg
# cells (sum legs 31/32, diff legs 30/32 words — two legs cannot share a cell)
# + gather + out. Not compressible within the pinned numerics.
SPINE_CELLS = 7
# Direct-table twiddle chain (period P <= 16 fits the 32-word fetch cells):
# fetch_c + fetch_d + steer + prods + rail. Measured shape, FFT16 stages 0/1.
DIRECT_TW_CELLS = 5
# Largest direct table a fetch cell holds: P=16 with the ``c`` forward is
# EXACTLY 32/32 words. Above this a stage needs the octant fold.
DIRECT_TABLE_MAX = 16
# OCTANT-FOLD twiddle chain, MEASURED (every cell resolver-verified <= 32
# words): seq + mcalc + tab_c + tab_d + swap + sign + steer + prods + rail.
#
# NOTE — this CORRECTS the original file's figure. That version deliberately
# charged the fold at an UNATTAINABLE 5 ("the two octant TABLE cells alone,
# sequencer/steering charged at ZERO cells ... so the wall cannot hinge on the
# fold-cell estimate"). Building it showed the honest cost is 9: the slot
# sequencer, the |r-M| triangle-index computation, the two table LOADs, the
# swap select and the sign application each need their own cell to stay inside
# 32 words. A split-bank DIRECT table (2x16-word banks with a range check) was
# also measured as an alternative and is WORSE — the range check busts every
# cell at every bank size tried — so 9 is the efficient construction, not a
# lazy one.
OCTANT_TW_CELLS = 9


def _delay_cells(samples: int) -> int:
    """Stage line of ``samples`` physical complex samples (D-1; the emerging
    sample lives in ctl's a-pair). ComplexDelayLine density; min 1 cell (the
    D=1 stage's relay)."""
    if samples <= 0:
        return 1
    return math.ceil(samples / ComplexDelayLineBlock.SAMPLES_PER_CELL)


def _twiddle_cells(D: int) -> int:
    if D > DIRECT_TABLE_MAX:
        return OCTANT_TW_CELLS
    if D >= 4:
        return DIRECT_TW_CELLS
    return 0                     # the kind-word (D=2) and identity (D=1) stages


def stage_cells(D: int) -> int:
    """Total cells for one R2SDF stage of delay ``D``."""
    return SPINE_CELLS + _twiddle_cells(D) + _delay_cells(D - 1)


def fft_cells(N: int) -> int:
    """Cell count for an N-point single-block streaming R2SDF FFT."""
    return sum(stage_cells((N // 2) >> s)
               for s in range(int(N).bit_length() - 1))


def test_fit_accounting_reproduces_fft16():
    """The accounting is CALIBRATED: it reproduces the SHIPPED 44-cell
    FFT16Block as 43 + its one documented layout-padding cell (the stage-1
    [2,1] delay split that fills the 7-wide band). A change that breaks this
    invalidates every number below."""
    c16 = fft_cells(16)
    assert c16 == 43
    assert c16 <= FFT16Block("fft16").cell_count <= c16 + 1


def test_fft16_needs_no_fold():
    """N=16's largest period is 8 — comfortably a direct table. The fold
    exists only for the chip-scale sizes."""
    assert all(_twiddle_cells((16 // 2) >> s) != OCTANT_TW_CELLS
               for s in range(4))


@pytest.mark.parametrize("N,expect", [(64, 81), (128, 110)])
def test_chip_scale_cell_counts(N, expect):
    """The measured single-block cell counts at the chip-scale sizes."""
    assert fft_cells(N) == expect


def test_fft64_fits_the_chip_scale_area():
    """N=64 (81 cells) FITS the sole-occupant area (110 cells over rows
    1..11) with real slack — the un-quarantine the policy change enables.
    Under the OLD ordinary cap (64 cells) it did not, which is exactly why
    the old wall test fired."""
    n64 = fft_cells(64)
    assert n64 <= CHIP_SCALE_AREA, (n64, CHIP_SCALE_AREA)
    assert n64 > ORDINARY_CELL_CAP, (
        "N=64 would fit the ORDINARY cap — the policy change was unnecessary")


def test_fft128_area_is_exactly_the_whole_die():
    """N=128 (110 cells) consumes the ENTIRE sole-occupant area — 110 of 110,
    ZERO cells of slack for any layout padding, and 100% of every row the
    ports leave free. This is the honest headline number for the N=128
    single-die question."""
    n128 = fft_cells(128)
    assert n128 == CHIP_SCALE_AREA
    assert CHIP_SCALE_AREA - n128 == 0


# ---------------------------------------------------------------------------
# Part 4 — the BAND CAP: what actually decides whether a size fits.
# ---------------------------------------------------------------------------
#
# The whole-block cell count is necessary but NOT sufficient. Each R2SDF stage
# closes a data-feedback ring (the delay tail returns the emerging sample to
# the stage controller), so every stage carries the serialize-LOCK whose
# write-back + lock-clear WRITE.CFG is an @1 backward edge. FFT16 gets that
# for free from its layout: each stage is ONE 2-row serpentine band with
# ``ctl`` at the band's top-left and ``out`` directly BELOW it, which also
# puts the next stage's ``ctl`` @1 below ``out``. That geometry is what let
# FFT16 ship with ZERO transit cells and no _apply_internal_feedback tracing.
#
# A 2-row band on a 10-wide chip holds at most 2 x 10 = 20 cells. So under the
# @1 stage-ring discipline, 20 CELLS PER STAGE is a hard cap — independent of
# how much whole-die area is left.

BAND_ROWS = 2
STAGE_BAND_CAP = BAND_ROWS * CHIP_W                # 20 cells


def test_band_cap_is_the_two_row_serpentine():
    assert STAGE_BAND_CAP == 20


def test_fft16_every_stage_fits_a_band():
    """The shipped FFT16 is comfortably inside the band cap at every stage —
    which is why the question never arose there."""
    for s in range(4):
        assert stage_cells((16 // 2) >> s) <= STAGE_BAND_CAP


@pytest.mark.parametrize("N,over", [(64, [0]), (128, [0, 1])])
def test_chip_scale_stage0_exceeds_the_band_cap(N, over):
    """THE REMAINING WALL, and it is a BAND wall, not an area wall: the
    fold stages exceed the 20-cell 2-row band even though the whole block
    fits the die.

      N=64  stage 0 = 23 cells (spine 7 + fold 9 + line 7)  -> 3 over
      N=128 stage 0 = 29, stage 1 = 23                      -> 9 and 3 over

    An oversized stage needs either a 4-row fold — which breaks the @1
    write-back and requires a feedback transit column plus
    _apply_internal_feedback tracing — or 3+ fewer cells. This test pins the
    exact shortfall so the next attempt starts from the real number, and it
    FLIPS the moment a stage is brought under the cap."""
    bad = [s for s in range(int(N).bit_length() - 1)
           if stage_cells((N // 2) >> s) > STAGE_BAND_CAP]
    assert bad == over, (N, bad, over)


def test_fft64_stage0_shortfall_is_exactly_three_cells():
    """The precise N=64 gap, so a future saving can be measured against it."""
    s0 = stage_cells(32)
    assert s0 == 23
    assert s0 - STAGE_BAND_CAP == 3


def test_fft128_needs_more_rows_than_the_ports_leave():
    """Independently of the band cap, N=128 as one stacked column of 2-row
    bands needs MORE ROWS than a sole occupant has. Its 7 stages merge to 6
    bands (the two trivial stages, 8 cells each, share one 8-wide band) = 12
    rows, but the x16 ports sit on row 0 and a used port cell is an obstacle,
    leaving 11. This is why the N=128 single-die answer is NO and the 2-die
    stage-boundary split is the authorized path."""
    delays = [(128 // 2) >> s for s in range(7)]
    cells = [stage_cells(D) for D in delays]
    bands, i = [], 0
    while i < len(cells):
        if (i + 1 < len(cells) and delays[i] == 2 and delays[i + 1] == 1
                and cells[i] + cells[i + 1] <= STAGE_BAND_CAP):
            bands.append(cells[i] + cells[i + 1])
            i += 2
        else:
            bands.append(cells[i])
            i += 1
    assert len(bands) == 6
    assert BAND_ROWS * len(bands) == 12
    assert BAND_ROWS * len(bands) > CHIP_SCALE_ROWS


def test_fft64_row_budget_fits():
    """N=64 by contrast merges to 5 bands = 10 rows, inside the 11 a sole
    occupant gets — so N=64's only obstacle is the stage-0 band, not rows."""
    delays = [(64 // 2) >> s for s in range(6)]
    cells = [stage_cells(D) for D in delays]
    bands, i = [], 0
    while i < len(cells):
        if (i + 1 < len(cells) and delays[i] == 2 and delays[i + 1] == 1
                and cells[i] + cells[i + 1] <= STAGE_BAND_CAP):
            bands.append(cells[i] + cells[i + 1])
            i += 2
        else:
            bands.append(cells[i])
            i += 1
    assert len(bands) == 5
    assert BAND_ROWS * len(bands) == 10 <= CHIP_SCALE_ROWS


def test_port_row_is_not_available_to_the_block():
    """The reachability contract's geometric consequence, pinned: x16_in
    (0,0) and x16_out (9,0) are both on row 0 and a USED port cell is an
    obstacle to every other net, so a sole-occupant block gets rows 1..11 —
    NOT the full 12. Getting this wrong inflates the area budget by a whole
    row (10 cells)."""
    assert CHIP_SCALE_ROWS == CHIP_H - 1
    assert CHIP_SCALE_AREA == CHIP_W * (CHIP_H - 1)


def test_old_ordinary_cap_still_binds_non_chip_scale_blocks():
    """The policy change is NARROW: an ordinary block is still capped at
    8x8 = 64 cells, and both N=64 and N=128 still bust that cap. Nothing was
    loosened globally."""
    assert ORDINARY_CELL_CAP == 64
    assert fft_cells(64) > ORDINARY_CELL_CAP
    assert fft_cells(128) > ORDINARY_CELL_CAP
    assert FFT16Block("f").cell_count <= ORDINARY_CELL_CAP


@pytest.mark.parametrize("N", [64, 128])
def test_every_authored_cell_fits_the_word_budget(N):
    """The constraint that FORCED the 9-cell fold: every cell of the authored
    N-point block — spine, fold chain, twiddle chain, delay segments — must
    fit 32 words (data + state + program). This is measured with the real
    resolver, and it is why the fold cannot be fewer cells: collapsing any two
    of seq/mcalc/tab_c/tab_d/swap/sign busts this gate (each was tried).

    It also pins the whole-block cell count, so the fit arithmetic above and
    the authored block can never drift apart."""
    from gr_kyttar.placement.resolver import CellProgramResolver  # noqa: PLC0415
    from gr_kyttar.placement.blocks import fft_large as FL  # noqa: PLC0415

    cls = FL.FFT64Block if N == 64 else FL.FFT128Block
    blk = object.__new__(cls)
    blk._n = N
    blk._delays = FL.stage_delays(N)
    blk._tables = [FL.stage_table(N, s) for s in range(len(blk._delays))]
    blk._octC, blk._octS = FL.octant_tables(N)
    blk._segs = {s: FL._delay_segments(D - 1)
                 for s, D in enumerate(blk._delays)}
    order = []
    for s in range(len(blk._delays)):
        order += cls._stage_chain(blk, s)
    blk._order = order
    cps = cls.build_cell_programs(blk)

    res = CellProgramResolver()
    over = []
    for cid, cp in cps.items():
        n_instr = res.count_instructions(cp)
        regs = ([p.register for p in cp.inputs]
                + [d.address for d in (cp.data or ())]
                + [sv.register for sv in (cp.state or ())])
        max_addr = max([a for a in regs if a is not None], default=-1)
        total = max_addr + 1 + n_instr
        if total > 32:
            over.append((cid, total))
        # Every StateVar explicitly pinned (INV-33).
        for sv in (cp.state or ()):
            assert sv.register is not None, f"{cid}: unpinned state {sv.name}"
    assert not over, f"cells over the 32-word budget: {over}"
    assert len(cps) == fft_cells(N), (len(cps), fft_cells(N))


@pytest.mark.parametrize("N,s0", [(64, 23), (128, 29)])
def test_authored_stage0_matches_the_fit_arithmetic(N, s0):
    """The authored stage-0 chain is exactly the length the fit arithmetic
    predicts — the two cannot drift."""
    from gr_kyttar.placement.blocks import fft_large as FL  # noqa: PLC0415
    cls = FL.FFT64Block if N == 64 else FL.FFT128Block
    blk = object.__new__(cls)
    blk._n = N
    blk._delays = FL.stage_delays(N)
    blk._segs = {s: FL._delay_segments(D - 1)
                 for s, D in enumerate(blk._delays)}
    assert len(cls._stage_chain(blk, 0)) == s0 == stage_cells(N // 2)


def test_measured_fold_cost_exceeds_the_original_lower_bound():
    """The original file's fold figure was an explicit, documented
    lower bound (2 table cells + steer/prods/rail = 5, sequencer and steering
    charged at ZERO). Building it measured 9. Pin the correction so the
    optimistic number cannot creep back into a future estimate."""
    ORIGINAL_UNATTAINABLE_BOUND = 2 + 3
    assert OCTANT_TW_CELLS > ORIGINAL_UNATTAINABLE_BOUND
    assert OCTANT_TW_CELLS == 9
    # And the correction is what moved N=64 from the old "77+" to 81.
    assert fft_cells(64) == 81


# =============================================================================
# 9. THE SPINE FOLD — the structural gates that make the layout trustworthy
#
# The stage-BAND arithmetic above pins the OLD analysis (kept: it is what the
# fold arithmetic must not drift from). These gates pin the fold that actually
# ships. Each one is a property the shipped FFT16 has and that the layouts
# which FAILED on chip did not — so each has been observed to fail.
# =============================================================================
def test_parity_theorem_last_cell_abuts_first_only_when_even():
    """A chain of L cells can land its LAST cell edge-adjacent to its FIRST
    only when L is EVEN.

    Chessboard-colour the array: every step to an edge-adjacent cell flips the
    colour, so cell L-1 carries the start's colour XOR ((L-1) % 2) while
    adjacency demands the OPPOSITE colour. Asserted here by EXHAUSTIVE SEARCH
    over self-avoiding walks, not by restating the argument. This is why the
    shipped FFT16 (stages 14/14/8/8, all even) folds with no effort, and why an
    odd stage needs the delay-line parity pad.
    """
    def reachable(L, dim=4):
        cells = [(0, 0)]
        seen = {(0, 0)}
        found = [False]

        def walk():
            if found[0]:
                return
            if len(cells) == L:
                a, b = cells[0], cells[-1]
                if abs(a[0] - b[0]) + abs(a[1] - b[1]) == 1:
                    found[0] = True
                return
            x, y = cells[-1]
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                n = (x + dx, y + dy)
                if n in seen or not (0 <= n[0] < dim and 0 <= n[1] < dim):
                    continue
                seen.add(n)
                cells.append(n)
                walk()
                cells.pop()
                seen.discard(n)
        walk()
        return found[0]

    for L in range(2, 13):
        assert reachable(L) == (L % 2 == 0), (
            f"L={L}: parity theorem violated")


def test_parity_pad_preserves_the_delay_exactly():
    """The odd-stage repair spreads the SAME total delay over one MORE cell,
    so the transform is bit-identical either way. A pad that changed the delay
    would silently change the FFT."""
    from gr_kyttar.placement.blocks import fft_large as FL  # noqa: PLC0415
    for samples in (3, 7, 15, 31, 63):
        base = FL._delay_segments(samples)
        padded = FL._delay_segments(samples, extra_cells=1)
        assert sum(base) == sum(padded) == samples
        assert len(padded) == len(base) + 1
        assert min(padded) >= 1


def _spine_block():
    from gr_kyttar.placement.blocks import fft_large as FL  # noqa: PLC0415
    return FL.FFT64Block("probe")


def test_fft64_places_on_one_die():
    """N=64 constructs — the fold exists. (N=128 does not; see below.)"""
    blk = _spine_block()
    lay = blk.default_layout()
    assert len(lay) == blk.cell_count == 84
    xs = [v[0] for v in lay.values()]
    ys = [v[1] for v in lay.values()]
    assert max(xs) < CHIP_W and max(ys) < CHIP_H


def test_fft128_single_die_is_ruled_out_on_the_SPINE_HEIGHT():
    """N=128's obstacle is the spine height (7 stages x 2 = 14 rows in ONE
    column, against a 12-row array), NOT area. Pinning the REASON matters: the
    old analysis blamed area/rows-per-band and was wrong about both."""
    from gr_kyttar.placement.blocks import fft_large as FL  # noqa: PLC0415
    with pytest.raises(FL.LargeFFTGeometryError) as ei:
        FL.FFT128Block("probe")
    msg = str(ei.value)
    assert "spine" in msg and "14" in msg


def test_spine_geometry_ctl_above_out_above_next_ctl():
    """THE load-bearing geometry: every stage's out has its own ctl directly
    ABOVE (the write-back, in-program face_fb = NORTH) and the next stage's
    ctl directly BELOW (the forward packet, face_tap = SOUTH). The shipped
    FFT16 satisfies exactly this."""
    blk = _spine_block()
    lay = blk.default_layout()
    col = None
    for s in range(blk.n_stages):
        cx, cy, _ = lay[f"s{s}_ctl"]
        ox, oy, _ = lay[f"s{s}_out"]
        assert (ox, oy) == (cx, cy + 1), f"s{s}: out is not below ctl"
        col = cx if col is None else col
        assert cx == col, f"s{s}: ctl left the spine column"
        if s + 1 < blk.n_stages:
            nx, ny, _ = lay[f"s{s+1}_ctl"]
            assert (nx, ny) == (ox, oy + 1), (
                f"s{s}: next ctl is not below out")
    assert col not in {0, CHIP_W - 1}, (
        "the spine column must avoid the x16 port columns, else the block "
        "loses row 0 and no longer fits")


def test_spine_chain_is_face_abutted_end_to_end():
    """Every consecutive cell of every stage chain is edge-adjacent, so each
    cell's resting face points at its chain successor and the internal hops
    trace exactly. Break this and writes silently fall back to Manhattan."""
    blk = _spine_block()
    lay = blk.default_layout()
    for s in range(blk.n_stages):
        ch = blk._stage_chain(s)
        for a, b in zip(ch, ch[1:]):
            (ax, ay, _), (bx, by, _) = lay[a], lay[b]
            assert abs(ax - bx) + abs(ay - by) == 1, (
                f"{a} -> {b} not adjacent")


def test_spine_route_time_face_audit():
    """The FFT16 route-time-face rule, on the spine fold: every cell's
    last-listed internal dst must be its chain successor or NON-adjacent, the
    one exception being each stage's own out->ctl write-back."""
    blk = _spine_block()
    lay = blk.default_layout()
    order = list(blk.build_cell_programs())
    nxt = {order[i]: order[i + 1] for i in range(len(order) - 1)}
    last = {}
    for (src, _sp, dst, _dp) in blk.internal_connections():
        last[src] = dst
    allowed = {(f"s{s}_out", f"s{s}_ctl") for s in range(blk.n_stages)}
    bad = []
    for src, dst in last.items():
        if dst == nxt.get(src) or (src, dst) in allowed:
            continue
        (sx, sy, _), (dx, dy, _) = lay[src], lay[dst]
        if abs(sx - dx) + abs(sy - dy) == 1:
            bad.append((src, dst))
    assert not bad, f"adjacent non-successor last edges (mis-face): {bad}"


def test_spine_every_forward_edge_traces_to_its_chain_distance():
    """THE gate that the failing folds could not pass, and the one that makes
    a silent failure loud.

    An internal hop is resolved by TRACING resting faces
    (``router._get_routing_distance``) and SILENTLY falls back to MANHATTAN
    distance when the trace fails — so a bad fold does not error, it ships a
    wrong hop and the stage spins on its own ring. Here every FORWARD internal
    edge (connection and jump) is traced along the authored faces and must
    equal its chain distance. The shipped FFT16 scores 0 mismatches / 188.
    """
    delta = {"east": (1, 0), "west": (-1, 0),
             "south": (0, 1), "north": (0, -1)}
    blk = _spine_block()
    lay = blk.default_layout()
    order = list(blk.build_cell_programs())
    idx = {c: i for i, c in enumerate(order)}
    at = {(v[0], v[1]): c for c, v in lay.items()}
    edges = [(s, d) for (s, _p, d, _q) in blk.internal_connections()]
    edges += [(s, d) for (s, _p, d, _e) in blk.internal_jumps()]
    bad = []
    for (src, dst) in edges:
        if src not in lay or dst not in lay or idx[dst] <= idx[src]:
            continue                       # backward: the feedback patcher's
        pos, hops, seen = (lay[src][0], lay[src][1]), None, set()
        for n in range(1, 80):
            here = at.get(pos)
            if here is None or pos in seen:
                break
            seen.add(pos)
            dx, dy = delta[lay[here][2]]
            pos = (pos[0] + dx, pos[1] + dy)
            if pos == (lay[dst][0], lay[dst][1]):
                hops = n
                break
        if hops != idx[dst] - idx[src]:
            bad.append((src, dst, hops, idx[dst] - idx[src]))
    assert not bad, f"forward edges whose traced hop != chain distance: {bad}"


def test_spine_leaves_routing_corridors_to_both_ports():
    """A fold that FILLS the array builds and then fails to route ("no free
    corridor between the ports" — observed). Free, non-block cells must still
    connect the input landing to x16_in and the exit to x16_out."""
    blk = _spine_block()
    lay = blk.default_layout()
    occupied = {(v[0], v[1]) for v in lay.values()}
    in_port, out_port = (0, 0), (CHIP_W - 1, 0)
    assert in_port not in occupied and out_port not in occupied

    def connects(cell, port):
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            start = (cell[0] + dx, cell[1] + dy)
            if not (0 <= start[0] < CHIP_W and 0 <= start[1] < CHIP_H):
                continue
            if start in occupied:
                continue
            seen, stack = {start}, [start]
            while stack:
                cur = stack.pop()
                if cur == port:
                    return True
                for ex, ey in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    n = (cur[0] + ex, cur[1] + ey)
                    if (0 <= n[0] < CHIP_W and 0 <= n[1] < CHIP_H
                            and n not in occupied and n not in seen):
                        seen.add(n)
                        stack.append(n)
        return False

    ctl = lay["s0_ctl"][:2]
    out = lay[f"s{blk.n_stages - 1}_out"][:2]
    assert connects(ctl, in_port), "no free corridor from x16_in to s0_ctl"
    assert connects(out, out_port), "no free corridor from the exit to x16_out"


def test_spine_exit_does_not_rest_toward_its_own_ctl():
    """The block EXIT's resting face is what the build rewrites to the routed
    egress and what the router traces; resting back into its own ctl re-enters
    the stage instead of leaving the block (observed). FFT16's last out rests
    away from its ctl, into the cell the egress corridor starts from."""
    blk = _spine_block()
    lay = blk.default_layout()
    last = f"s{blk.n_stages - 1}_out"
    ox, oy, face = lay[last]
    cx, cy, _ = lay[f"s{blk.n_stages - 1}_ctl"]
    delta = {"east": (1, 0), "west": (-1, 0),
             "south": (0, 1), "north": (0, -1)}
    dx, dy = delta[face]
    assert (ox + dx, oy + dy) != (cx, cy), (
        "the block exit rests toward its own ctl")


def test_spine_cells_are_pairwise_distinct():
    """No stage may overlap another (INV-25): the fold is built by a search,
    so this is a real risk, not a formality."""
    blk = _spine_block()
    lay = blk.default_layout()
    seen = {}
    for cid, (x, y, _f) in lay.items():
        assert (x, y) not in seen, f"{cid} overlaps {seen[(x, y)]} at {(x, y)}"
        seen[(x, y)] = cid


# ---------------------------------------------------------------- INV-4
# The spine gates above are worthless until they are shown to FAIL. Each
# mutation corrupts the layout in the way one real failure mode did and
# asserts the corresponding predicate rejects it.
def _spine_predicates(blk, lay):
    """(stacking_ok, adjacency_ok, hops_ok) evaluated on an ARBITRARY layout
    dict — the same three checks the gates above make, factored so a mutated
    layout can be pushed through them."""
    ns = blk.n_stages

    def stacking():
        for s in range(ns):
            cx, cy, _ = lay[f"s{s}_ctl"]
            ox, oy, _ = lay[f"s{s}_out"]
            if (ox, oy) != (cx, cy + 1):
                return False
            if s + 1 < ns:
                nx, ny, _ = lay[f"s{s+1}_ctl"]
                if (nx, ny) != (ox, oy + 1):
                    return False
        return True

    def adjacency():
        for s in range(ns):
            ch = blk._stage_chain(s)
            for a, b in zip(ch, ch[1:]):
                (ax, ay, _), (bx, by, _) = lay[a], lay[b]
                if abs(ax - bx) + abs(ay - by) != 1:
                    return False
        return True

    def hops():
        delta = {"east": (1, 0), "west": (-1, 0),
                 "south": (0, 1), "north": (0, -1)}
        order = list(blk.build_cell_programs())
        idx = {c: i for i, c in enumerate(order)}
        at = {(v[0], v[1]): c for c, v in lay.items()}
        edges = [(s, d) for (s, _p, d, _q) in blk.internal_connections()]
        edges += [(s, d) for (s, _p, d, _e) in blk.internal_jumps()]
        for (src, dst) in edges:
            if src not in lay or dst not in lay or idx[dst] <= idx[src]:
                continue
            pos, h, seen = (lay[src][0], lay[src][1]), None, set()
            for n in range(1, 80):
                here = at.get(pos)
                if here is None or pos in seen:
                    break
                seen.add(pos)
                dx, dy = delta[lay[here][2]]
                pos = (pos[0] + dx, pos[1] + dy)
                if pos == (lay[dst][0], lay[dst][1]):
                    h = n
                    break
            if h != idx[dst] - idx[src]:
                return False
        return True

    return stacking, adjacency, hops


def test_mutation_broken_ctl_out_stacking_fails():
    """Shift one stage's out off the spine — the stacking gate must reject."""
    blk = _spine_block()
    lay = dict(blk.default_layout())
    assert _spine_predicates(blk, lay)[0]()
    x, y, f = lay["s2_out"]
    lay["s2_out"] = (x + 1, y, f)
    assert not _spine_predicates(blk, lay)[0]()


def test_mutation_broken_chain_adjacency_fails():
    """Teleport a mid-chain cell — the adjacency gate must reject."""
    blk = _spine_block()
    lay = dict(blk.default_layout())
    assert _spine_predicates(blk, lay)[1]()
    x, y, f = lay["s1_sumq"]
    lay["s1_sumq"] = (x, min(y + 4, CHIP_H - 1), f)
    assert not _spine_predicates(blk, lay)[1]()


def test_mutation_wrong_face_breaks_the_hop_trace():
    """Point ONE cell the wrong way. Nothing about the POSITIONS changes — but
    the traced hops no longer match the chain distances, which is exactly the
    silent corruption the fabric would ship (a failed trace falls back to
    Manhattan). The hop gate must reject."""
    blk = _spine_block()
    lay = dict(blk.default_layout())
    assert _spine_predicates(blk, lay)[2]()
    x, y, f = lay["s0_sumi"]
    lay["s0_sumi"] = (x, y, "north" if f != "north" else "south")
    assert not _spine_predicates(blk, lay)[2]()
