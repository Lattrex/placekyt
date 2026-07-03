# SPDX-License-Identifier: GPL-3.0-or-later
"""Long-burst CONVERGENCE verification for GardnerTimingRecovery.

The block replaced a non-converging kp/ki PI loop (whose interpolator period
collapsed monotonically 16384 -> ~7007 and slipped a whole sample after ~180
symbols, killing decode) with GNU Radio ``digital.symbol_sync``'s control loop
(TED_GARDNER, loop_bw=0.045, damping=1.0). This test PINS that fix:

  1. Reference-level convergence (fast, no chip): over a 600-symbol BPSK stream
     with fractional timing offsets 0.3/0.5/0.7 the interpolator period PLATEAUS
     near nominal (16384) — it does NOT collapse — and recovered bits are BER 0
     past the loop transient.
  2. The on-chip build (simKYT, via ``run_block_dut_rate``, x16_in -> Gardner ->
     x16_out with the internal PI feedback preserved) is BIT-EXACT with
     ``process_reference`` over a long burst. Reference converges + chip ==
     reference  =>  the chip converges. This is the gold-standard closure: it
     would FAIL loudly if a build change ever clobbered the period feedback
     (the exact regression ``test_gardner_build`` guards structurally).

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 \
      <venv>/python -m pytest verification/tests/test_gardner_convergence.py -x -q
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PLACEKYT = Path(__file__).resolve().parents[2] / "placekyt"
_VERIFY = Path(__file__).resolve().parents[1]
_RUNTIME = Path(__file__).resolve().parents[2] / "runtime" / "python"
for p in (str(_PLACEKYT), str(_VERIFY), str(_RUNTIME)):
    if p not in sys.path:
        sys.path.insert(0, p)

from gr_kyttar.placement.blocks.gardner_timing_recovery import (  # noqa: E402
    GardnerTimingRecovery)
from gr_kyttar.placement.blocks._base import float_to_q15  # noqa: E402

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
NOMINAL = 1 << 14  # Q14 nominal half-period (1.0 sample)


# --------------------------------------------------------------------------- #
# Stimulus: BPSK symbols, RRC-shaped, sampled at a FRACTIONAL timing offset.
# --------------------------------------------------------------------------- #
def _make_bpsk_2sps(bits, sps=2, frac=0.5, rrc_span=8, beta=0.35):
    syms = np.array([1.0 if b else -1.0 for b in bits], dtype=np.float64)
    up = np.zeros(len(syms) * sps)
    up[::sps] = syms
    N = rrc_span * sps
    t = (np.arange(-N, N + 1)) / sps
    taps = (np.sinc(t) * np.cos(np.pi * beta * t)
            / (1 - (2 * beta * t) ** 2 + 1e-12))
    taps /= np.sqrt(np.sum(taps ** 2))
    shaped = np.convolve(up, taps, mode="same")
    n = np.arange(len(shaped))
    idx = np.clip(n + frac, 0, len(shaped) - 1)
    lo = np.floor(idx).astype(int)
    hi = np.clip(lo + 1, 0, len(shaped) - 1)
    fr = idx - lo
    off = shaped[lo] * (1 - fr) + shaped[hi] * fr
    off /= (np.max(np.abs(off)) + 1e-12)
    return off * 0.7  # Q15 headroom


def _period_trace(blk, input_samples):
    """Re-run the reference loop, capturing the interpolator half-period
    (``inst_active``) at each CENTER strobe alongside the recovered center
    samples. Bit-identical to ``process_reference`` — only instrumented."""
    def s16(v):
        return v - 0x10000 if v & 0x8000 else v

    def u16(v):
        return v & 0xFFFF

    def mqr(a, b):
        return (s16(a) * s16(b)) >> 15

    def mulhi(a, b):
        return (s16(a) * s16(b)) >> 16

    sq = [float_to_q15(float(x)) for x in input_samples]
    ONE = NOMINAL
    out, periods = [], []
    iavg, avg = 0, ONE
    inst_active = inst_next = ONE
    cprev = midv = 0
    phase = ONE >> 1
    xp = xp2 = parity = 0
    for v in sq:
        xi = s16(v)
        xp2 = xp
        xp = xi
        phase += ONE
        if phase >= inst_active:
            phase -= inst_active
            inst_active = inst_next
            frac = u16(phase << 1)
            s = xp2 + mqr(frac, u16((xp - xp2) & 0xFFFF))
            if parity == 0:
                dch = u16((mqr(u16(s & 0xFFFF), blk._MULQ_HALF)
                           - mqr(u16(cprev & 0xFFFF), blk._MULQ_HALF)) & 0xFFFF)
                ewhi = mulhi(u16(midv & 0xFFFF), dch)
                cprev = s
                out.append(s16(u16(s)))
                periods.append(inst_active)
                iavg += mqr(u16((ewhi + blk._INTEG_RBIAS) & 0xFFFF),
                            blk._MULQ_INTEG)
                iavg = max(-blk._MAXDEV, min(blk._MAXDEV, iavg))
                avg = ONE + iavg
                inst_next = avg + mqr(u16(ewhi & 0xFFFF), blk._MULQ_PROP)
            else:
                midv = s
            parity ^= 1
    return np.array(out, dtype=np.int16), np.array(periods)


def _best_ber(rec_bits, ref_bits, skip=60):
    """BER over the settled region, tolerant of a small integer lag and the
    global BPSK sign ambiguity."""
    rec_bits = np.asarray(rec_bits)
    L = min(len(rec_bits), len(ref_bits))
    ref = np.asarray(ref_bits[:L])
    best = 1.0
    for lag in range(-3, 4):
        r = rec_bits[max(0, lag):L + min(0, lag)]
        g = ref[max(0, -lag):L - max(0, lag)]
        m = min(len(r), len(g))
        if m <= skip + 40:
            continue
        r2, g2 = r[skip:m], g[skip:m]
        for cand in (r2, 1 - r2):
            best = min(best, float(np.mean(cand != g2)))
    return best


# --------------------------------------------------------------------------- #
# 1. Reference-level convergence (fast, no chip build).
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("frac", [0.3, 0.5, 0.7])
def test_period_plateaus_not_collapses(frac):
    """The interpolator period stays locked near nominal over 600 symbols — the
    old kp/ki loop collapsed to ~7007 by ~symbol 189."""
    rng = np.random.default_rng(1234)
    bits = rng.integers(0, 2, 600).tolist()
    sig = _make_bpsk_2sps(bits, frac=frac)
    blk = GardnerTimingRecovery("g")
    recovered, periods = _period_trace(blk, sig)

    pmin, pmax = int(periods.min()), int(periods.max())
    # No collapse: the loop must stay within its +-max_dev clamp band, nowhere
    # near the old 7007 collapse (which is < 0.6 * nominal).
    assert pmin > NOMINAL * 0.7, (
        f"period collapsed to {pmin} (< 0.7*{NOMINAL}); loop is not converging")
    assert pmax < NOMINAL * 1.3, f"period ran away high to {pmax}"
    # Settled: the last-quarter spread is small (plateau, not a monotone drift).
    tail = periods[-len(periods) // 4:]
    tail_spread = int(tail.max() - tail.min())
    assert tail_spread < NOMINAL * 0.1, (
        f"period never settled (tail spread {tail_spread})")

    # And bits recover BER 0 past the transient.
    rec_bits = (recovered > 0).astype(int)
    ber = _best_ber(rec_bits, bits)
    assert ber == 0.0, f"settled BER {ber} != 0 at frac={frac}"


# --------------------------------------------------------------------------- #
# 2. On-chip build is bit-exact with the (converging) reference over a burst.
# --------------------------------------------------------------------------- #
_CHIP_AVAILABLE = os.path.exists(CHIP_YAML)


@pytest.mark.skipif(not _CHIP_AVAILABLE, reason="chip yaml absent")
@pytest.mark.parametrize("frac", [0.3, 0.5])
def test_onchip_matches_reference_long_burst(frac):
    """The built Gardner chip (simKYT, internal PI feedback preserved) is
    BIT-EXACT with ``process_reference`` over a long burst — so the chip
    converges exactly as the reference does. This is the closure that proves
    the on-silicon loop is stable, not just the Python model."""
    from kyttar_verify.dut_runner import run_block_dut_rate  # noqa: PLC0415

    rng = np.random.default_rng(77)
    NSYM = 300  # 600 input samples @ 2 sps — well past the old ~189-sym failure
    bits = rng.integers(0, 2, NSYM).tolist()
    sig = _make_bpsk_2sps(bits, frac=frac)
    inq = [float_to_q15(float(x)) for x in sig]

    r = run_block_dut_rate("GardnerTimingRecovery", inq,
                           chip_yaml=CHIP_YAML, in_port="xi")
    assert r.ok, r.reason

    blk = GardnerTimingRecovery("g")
    ref = blk.process_reference(np.array(sig))
    got = np.array([v - 0x10000 if v & 0x8000 else v for v in r.outputs_q15],
                   dtype=np.int64)
    m = min(len(ref), len(got))
    assert m >= NSYM - 5, f"chip produced only {len(got)} symbols (want ~{NSYM})"
    assert np.array_equal(ref[:m].astype(np.int64), got[:m]), (
        "on-chip Gardner diverged from the reference "
        f"(first mismatch at {int(np.argmax(ref[:m].astype(np.int64) != got[:m]))})")

    # The chip itself recovers the bits BER 0 past the transient.
    rec_bits = (got > 0).astype(int)
    ber = _best_ber(rec_bits, bits)
    assert ber == 0.0, f"on-chip settled BER {ber} != 0 at frac={frac}"
