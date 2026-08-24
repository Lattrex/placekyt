# SPDX-License-Identifier: GPL-3.0-or-later
"""FFT64 / FFT128 — the single-block PLACEMENT wall (known-limit guard, INV-style).

VERDICT (2026-08-23, the post-FFT16 fit check): a 64-point streaming R2SDF FFT
in the pinned FFT16 architecture CANNOT fit this 10x12 chip as ONE block, and
the reason is GEOMETRY, not numerics. This file is the executable form of that
finding, in two halves:

1. **The octant-fold twiddle numerics are PROVEN SOUND** (so nobody mistakes
   the wall for a table-size problem): every non-trivial twiddle of the N=64
   and N=128 stage-0 tables is reconstructed BIT-EXACTLY from two octant
   tables (COS and SIN over (0, pi/4]: 8+8 words for N=64, 16+16 for N=128,
   each an easy fit for one cell) by index fold + sign/swap steering —
   asserted exhaustively against the shipped ``quantize_twiddle`` direct
   values, with an INV-4 negative (a wrong-quadrant fold must break the
   equality). Big-N twiddle STORAGE was never the blocker.

2. **The cell-count floor exceeds the max routable single-block footprint.**
   The floor is computed from SHIPPED, MEASURED builder constants only (the
   FFT16 per-stage spine, the ComplexDelayLine density, the TwiddleMultiply
   chain) and the SAME accounting reproduces the shipped FFT16Block's 44
   cells (floor 43 + its one documented layout-padding delay cell). For N=64
   the floor is 77+ cells; the largest single-block footprint that routes AND
   survives the mandatory 8-orientation D4 gate on this 10-wide chip is
   8 x 8 = 64 cells (INV-9 / layout_rules: a block needs a bus channel on
   each side of the 10-wide axis, and D4 rotation swaps the dims, so BOTH
   must be <= 8). 77 > 64 with zero slack for ports. N=128 floors at 102+
   cells — 85% of the whole 120-cell array before a single routing channel.

These are GUARD tests in the FIRFilterBlock-tap-ceiling tradition: they pin
the limit so it is loud, and they FLIP (start failing) if the substrate grows
(a wider chip raises ``MAX_CELLS_ACROSS``; a denser delay cell raises
``SAMPLES_PER_CELL``) — the signal to un-quarantine FFT64Block. The manifest
rows FFT64Block / FFT128Block are ``needs_human`` citing this file; the
un-blocking design option (a stage-split cascade of 2-3 CHAINED blocks wired
in GRC, each under the 64-cell cap) changes the product shape (one block ->
several) and is a human call, not a builder call.

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

from gr_kyttar.placement.blocks.complex_delay_line_block import (  # noqa: E402
    ComplexDelayLineBlock)
from gr_kyttar.placement.blocks.fft16_block import FFT16Block  # noqa: E402
from gr_kyttar.placement.blocks.fft_primitives import (  # noqa: E402
    KIND_ID, KIND_MJ, KIND_MUL, quantize_twiddle, s16, u16)
from gr_kyttar.placement.blocks.fir_filter_block import (  # noqa: E402
    FIRFilterBlock)

Q15_ONE = 32768

# The max routable single-block footprint on this chip: both dims <= 8
# (INV-9 + the D4 orientation gate — rotation swaps the dims, so the 10-wide
# axis's bus-channel cap binds BOTH).
_ACROSS = FIRFilterBlock.MAX_CELLS_ACROSS          # 8, chip-size convention
SINGLE_BLOCK_CELL_CAP = _ACROSS * _ACROSS          # 64 cells


# ---------------------------------------------------------------------------
# Half 1 — the octant-fold twiddle reconstruction (bit-exact, exhaustive).
# ---------------------------------------------------------------------------

def _direct_words(N: int, k: int):
    """The shipped build-time quantization for stage-0 slot k (kind, c, d)."""
    th = 2.0 * np.pi * k / N
    if k == 0:
        return quantize_twiddle(1)
    if 4 * k == N:
        return quantize_twiddle(-1j)
    return quantize_twiddle(complex(np.cos(th), -np.sin(th)))


def _octant_tables(N: int):
    """COS and SIN over (0, pi/4]: m = 1..N/8, quantized round(32768*x).

    N=64 -> 8+8 words; N=128 -> 16+16 words. Each table + a fetch program is
    comfortably one 32-word cell (the FFT16 fetch cell held 8 table words +
    3 consts + an 8-instruction program at 20/32) — STORAGE is not the wall.
    """
    M = N // 8
    C = {m: int(np.round(np.cos(2.0 * np.pi * m / N) * Q15_ONE))
         for m in range(1, M + 1)}
    S = {m: int(np.round(np.sin(2.0 * np.pi * m / N) * Q15_ONE))
         for m in range(1, M + 1)}
    return C, S


def _folded_words(N: int, k: int, C, S, *, corrupt: str = ""):
    """Reconstruct stage-0 slot k's (c, d) from the octant tables.

    octant o = k // (N/8); table index m = |k - (nearest multiple of N/4)|;
    steering per octant:  o0: (+C, -S)   o1: (+S, -C)   o2: (-S, -C)
                          o3: (-C, -S)
    (d already carries the DIF conjugation: W = cos - j sin, d = -32768 sin.)
    ``corrupt`` selects an INV-4 single-fault model.
    """
    M = N // 8
    o = k // M
    t = ((k + M) // (2 * M)) * (2 * M)
    m = abs(k - t)
    if corrupt == "wrong_octant_sign" and o == 2:
        # fold quadrant 2 with quadrant 1's c sign (the missed negate)
        return u16(S[m]), u16(-C[m])
    if corrupt == "wrong_fold_quadrant" and o == 3:
        # fold quadrant 3 as if it were quadrant 0 (no reflection)
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


@pytest.mark.parametrize("N", [64, 128])
def test_octant_fold_bit_exact_exhaustive(N):
    """EVERY non-trivial stage-0 twiddle word pair reconstructs BIT-EXACTLY
    from the two octant tables — the direct round(32768*x) values, no
    off-by-one-LSB anywhere (including the k = N/8 and 3N/8 boundary slots
    where cos(pi/4) and sin(pi/4) quantize through different float paths)."""
    C, S = _octant_tables(N)
    trivial = []
    for k in range(N // 2):
        kind, c, d = _direct_words(N, k)
        if kind in (KIND_ID, KIND_MJ):
            trivial.append(k)
            continue
        assert kind == KIND_MUL
        fc, fd = _folded_words(N, k, C, S)
        assert (fc, fd) == (c, d), (
            f"N={N} k={k}: fold gave ({s16(fc)}, {s16(fd)}), "
            f"direct is ({s16(c)}, {s16(d)})")
    # exactly two trivial slots: W^0 = 1 and W^(N/4) = -j
    assert trivial == [0, N // 4]


@pytest.mark.parametrize("corrupt", ["wrong_octant_sign", "wrong_fold_quadrant"])
def test_octant_fold_equality_gate_has_teeth(corrupt):
    """INV-4: a corrupted fold (wrong quadrant sign / un-reflected quadrant)
    must BREAK the exhaustive equality — the gate can fail."""
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


# ---------------------------------------------------------------------------
# Half 2 — the cell-count floor vs the single-block footprint cap.
# ---------------------------------------------------------------------------

# Per-stage spine, measured on the shipped FFT16Block: ctl + the four RHE leg
# cells (sum legs 24/32, diff legs 25/32 words — two legs cannot share a cell)
# + gather + out. Not compressible within the pinned numerics.
_SPINE_CELLS = 7
# Direct-table twiddle chain (P <= 16 fits the 32-word fetch cells):
# fetch_c + fetch_d + steer + prods + rail. Measured shape, FFT16 stages 0/1.
_DIRECT_TW_CELLS = 5
# Octant-fold twiddle chain lower bound: the two octant TABLE cells alone
# (sequencer/steering charged at ZERO cells — deliberately unattainable, so
# the wall cannot hinge on the fold-cell estimate) + steer + prods + rail.
_OCTANT_TW_CELLS_LOWER_BOUND = 2 + 3


def _delay_cells(samples: int) -> int:
    """Stage line of ``samples`` physical complex samples (D-1; the emerging
    sample lives in ctl's a-pair). ComplexDelayLine density, floor'd
    generously: no output-cell cap, min 1 cell (the D=1 stage's relay)."""
    if samples <= 0:
        return 1
    return math.ceil(samples / ComplexDelayLineBlock.SAMPLES_PER_CELL)


def _stage_floor(D: int, twiddle: str) -> int:
    tw = {"direct": _DIRECT_TW_CELLS,
          "octant": _OCTANT_TW_CELLS_LOWER_BOUND,
          "none": 0}[twiddle]
    return _SPINE_CELLS + _delay_cells(D - 1) + tw


def _fft_floor(N: int) -> int:
    """Cell-count floor for an N-point single-block streaming R2SDF FFT.

    Stage s has delay D = (N/2) >> s and twiddle period P = D over W_N^(2^s):
    P >= 32 needs the octant fold (a direct table busts the 32-word fetch
    cell); 4 <= P <= 16 is the FFT16 direct-table chain; P = 2 is the
    kind-word stage (no extra cells); P = 1 is the identity stage.
    """
    total = 0
    D = N // 2
    while D >= 1:
        if D >= 32:
            total += _stage_floor(D, "octant")
        elif D >= 4:
            total += _stage_floor(D, "direct")
        else:
            total += _stage_floor(D, "none")
        D //= 2
    return total


def test_fit_accounting_reproduces_fft16():
    """The floor formula is calibrated: it reproduces the SHIPPED 44-cell
    FFT16Block as floor 43 + its one documented layout-padding cell (the
    stage-1 [2,1] delay split that fills the 7-wide band)."""
    floor16 = _fft_floor(16)
    assert floor16 == 43
    assert floor16 <= FFT16Block("fft16").cell_count <= floor16 + 1


def test_fft64_exceeds_single_block_footprint_cap():
    """THE WALL: the FFT64 floor (77+, with the octant fold charged at an
    unattainable 2 table cells) exceeds the 8x8 = 64-cell max routable
    D4-safe single-block footprint by 13+ cells. There is no placement of
    77+ cells on the 10x12 WITH ports, bus channels, and the mandatory
    orientation gate. If this test ever FAILS, the substrate grew — revisit
    the FFT64Block quarantine."""
    floor64 = _fft_floor(64)
    assert floor64 >= 77
    assert floor64 > SINGLE_BLOCK_CELL_CAP, (
        f"FFT64 floor {floor64} <= cap {SINGLE_BLOCK_CELL_CAP}: "
        f"the wall has moved — un-quarantine FFT64Block")
    # Even the absurd free-lunch bound — ZERO cells for ALL N=64-specific
    # twiddle machinery (stages 0 and 1 rotate for free), keeping only the
    # two ALREADY-SHIPPED FFT16-shape chains — still busts the cap:
    free_lunch = (2 * _SPINE_CELLS + _delay_cells(31) + _delay_cells(15)
                  + _stage_floor(8, "direct") + _stage_floor(4, "direct")
                  + _stage_floor(2, "none") + _stage_floor(1, "none"))
    assert free_lunch > SINGLE_BLOCK_CELL_CAP


def test_fft128_exceeds_single_block_footprint_cap():
    """N=128 floors at 102+ cells — 85% of the ENTIRE 120-cell array before
    a single routing channel or port; blocked a fortiori."""
    floor128 = _fft_floor(128)
    assert floor128 >= 102
    assert floor128 > SINGLE_BLOCK_CELL_CAP
