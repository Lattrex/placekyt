# SPDX-License-Identifier: GPL-3.0-or-later
"""GardnerTimingRecovery (poc) — GR-equivalence attempt + QUARANTINE guard.

INV-25: the block's code EXISTS and is used in the coherent BPSK RX demo, but it was
NEVER held against its GNU Radio counterpart ``digital.symbol_sync_cc`` (TED_GARDNER)
per-block. This suite finalizes that verification, and its verdict is:

    QUARANTINE — GardnerTimingRecovery is NOT a drop-in equivalent of
    digital.symbol_sync_cc(TED_GARDNER) on the industry-standard channel that block
    is designed for (an RRC-shaped, matched-filtered, Nyquist 2-sps stream). On the
    exact channel where GR locks at BER 0 across the whole fractional-timing-offset
    sweep, the Q15 Gardner loop jitters too hard to hold the sampling instant and
    recovers at BER ~4-12%. The VERIFIED timing-recovery block for this channel is
    MMTimingRecoveryBlock (test_mm_timing_recovery.py — bit-exact vs GR, BER 0).

WHY (the substrate wall, reproduced below, not asserted):
  * Gardner's TED forms ``e = mid * (s - c_prev)``. In Q15 the BPSK sample difference
    ``s - c_prev`` OVERFLOWS int16 at full scale, so the block HALVES both samples
    (``>>1``) before the product to keep it in range (``ewhi = MULHI(mid,(s>>1)-(cprev>>1))``).
    That halving, plus the coarse power-of-two loop-filter gains (``>>8`` integral /
    ``>>2`` proportional), yields a NOISY timing estimate: on a non-Nyquist stimulus
    (the block's own synthetic ``_make_bpsk_2sps``) the spread-out symbol energy
    tolerates the jitter and the loop reaches BER 0 — but that stimulus is one GR
    ITSELF cannot lock (BER ~0.45). On the true matched-filter Nyquist channel the
    sampling instant is SHARP, the same jitter injects ISI, and the eye closes on
    ~10-18% of centers (period tail-jitter std ~340 in Q14 at frac 0.3).
  * Two real attempts to close the gap failed (see the lessons_log entry): an
    amplitude / RMS-normalisation sweep (BER 0 only at isolated (amp,frac) points,
    never across the offset sweep) and a loop-gain / period-trace diagnosis (the loop
    mean sits at nominal but the variance is the wall). Fixing it is a block REDESIGN
    (a wider TED product without the ``>>1`` truncation, e.g. the M&M cubic-Farrow +
    modulo-1-counter datapath MMTimingRecoveryBlock already ships), NOT a tolerance
    tweak — so it is quarantined for a human, per the scope steer.

This file is the EXECUTABLE guard for that verdict (INV-4 / the FIRFilter tap-ceiling
pattern): it PROVES, reproducibly, that GR locks where the DUT does not, and it will
FLIP GREEN the day the block is fixed (``test_gardner_would_be_gr_equivalent_when_fixed``
is an xfail that becomes an xpass). It does NOT fake a pass and does NOT loosen a
tolerance to hide the gap.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_gardner_timing_recovery.py -q
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PLACEKYT = Path(__file__).resolve().parents[2] / "placekyt"
_VERIFY = Path(__file__).resolve().parents[1]
_RUNTIME = Path(__file__).resolve().parents[2] / "runtime" / "python"
for _p in (str(_PLACEKYT), str(_VERIFY), str(_RUNTIME)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from gr_kyttar.placement.blocks.gardner_timing_recovery import (  # noqa: E402
    GardnerTimingRecovery)
from gr_kyttar.placement.blocks._base import float_to_q15  # noqa: E402

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_PY = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")
_GR_AVAILABLE = os.path.exists(_GR_PY)
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_AVAILABLE),
    reason="chip yaml or GNU Radio interpreter absent")

# The offset sweep (fractional timing offset in samples). GR locks BER 0 across ALL of
# these on the matched-filter channel; the DUT does not.
_FRACS = [0.1, 0.3, 0.5, 0.7]
# GR's default control-loop settings for symbol_sync_cc (from its docstring):
# loop_bw ~ 2*pi*0.040, damping 1.0, ted_gain 1.0, max_deviation 1.5, sps 2, osps 1.
_GR_LOOP_BW = 2 * math.pi * 0.040
_GR_DAMPING = 1.0
_GR_TED_GAIN = 1.0
_GR_MAX_DEV = 1.5
_SPS = 2.0


def _s16(v):
    v = int(v) & 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


# --------------------------------------------------------------------------- #
# Stimulus: the INDUSTRY-STANDARD RX channel a symbol synchroniser is built for —
# RRC-shaped TX, a high-quality sinc fractional delay, an RRC MATCHED FILTER at the
# RX. This is the SAME channel shape MMTimingRecoveryBlock is verified on. (It is
# distinct from the block's own ``_make_bpsk_2sps`` synthetic, which applies a single
# RRC and no matched filter — a non-Nyquist stimulus GR cannot lock, see the
# reproduction test below.)
# --------------------------------------------------------------------------- #
def _rrc(sps, beta, span):
    N = span * sps
    t = np.arange(-N, N + 1) / sps
    taps = (np.sinc(t) * np.cos(np.pi * beta * t)
            / (1 - (2 * beta * t) ** 2 + 1e-12))
    return taps / np.sqrt(np.sum(taps ** 2))


def _matched_channel(bits, frac, sps=2, beta=0.35, span=8, amp=0.7):
    syms = np.array([1.0 if b else -1.0 for b in bits])
    up = np.zeros(len(syms) * sps)
    up[::sps] = syms
    tx = np.convolve(up, _rrc(sps, beta, span))
    T = 16
    k = np.arange(-T, T + 1)
    h = np.sinc(k - frac) * np.hamming(2 * T + 1)
    h = h / h.sum()
    txd = np.convolve(tx, h, "same")
    mf = np.convolve(txd, _rrc(1.0, beta, span))     # RRC matched filter
    mf = mf / (np.max(np.abs(mf)) + 1e-12)
    return mf * amp


def _gr_symbol_sync(x):
    """digital.symbol_sync_cc(TED_GARDNER) recovered complex output (GR golden)."""
    payload = {"x": [[float(c.real), float(c.imag)] for c in x],
               "sps": _SPS, "lb": _GR_LOOP_BW, "damp": _GR_DAMPING,
               "tg": _GR_TED_GAIN, "md": _GR_MAX_DEV}
    script = (
        "import json,sys\n"
        "from gnuradio import gr, digital, blocks\n"
        "d=json.load(sys.stdin); x=[complex(a,b) for a,b in d['x']]\n"
        "tb=gr.top_block(); src=blocks.vector_source_c(x,False)\n"
        "ss=digital.symbol_sync_cc(digital.TED_GARDNER,d['sps'],d['lb'],d['damp'],"
        "d['tg'],d['md'],1,digital.constellation_bpsk().base())\n"
        "snk=blocks.vector_sink_c(); tb.connect(src,ss,snk); tb.run(); y=list(snk.data())\n"
        "print(json.dumps([[float(v.real),float(v.imag)] for v in y]))\n")
    p = subprocess.run([_GR_PY, "-c", script], input=json.dumps(payload),
                       capture_output=True, text=True, timeout=180)
    if p.returncode != 0:
        raise RuntimeError(f"GR symbol_sync_cc failed: {p.stderr[-500:]}")
    data = json.loads(p.stdout.strip().splitlines()[-1])
    return np.array([complex(a, b) for a, b in data])


def _ber_vs_tx(recovered_i, txbits, skip=80):
    """Best BER of a recovered symbol-rate stream against the KNOWN TX bits, tolerant
    of the global BPSK sign ambiguity and a small integer symbol lag (the pulse-shape
    group delay). This is the honest drop-in metric for a timing-recovery block: does
    it recover the transmitted bits?"""
    rec = np.asarray(recovered_i, dtype=float).real
    tx = np.asarray(txbits)
    best = 1.0
    for lag in range(-4, 30):
        r = rec[max(0, lag):]
        for sgn in (1, -1):
            rb = (sgn * r > 0).astype(int)
            m = min(len(rb), len(tx))
            if m < skip + 40:
                continue
            best = min(best, float(np.mean(rb[skip:m] != tx[skip:m])))
    return best


def _dut_reference_ber(bits, frac):
    sig = _matched_channel(bits, frac)
    ref = GardnerTimingRecovery("g").process_reference(np.array(sig))
    return _ber_vs_tx([_s16(v) for v in ref], bits)


def _dut_onchip_ber(bits, frac):
    from kyttar_verify.dut_runner import run_block_dut_rate  # noqa: PLC0415
    sig = _matched_channel(bits, frac)
    inq = [float_to_q15(float(x)) for x in sig]
    r = run_block_dut_rate("GardnerTimingRecovery", inq,
                           chip_yaml=CHIP_YAML, in_port="xi")
    assert r.ok, r.reason
    return _ber_vs_tx([_s16(v) for v in r.outputs_q15], bits)


# =========================================================================== #
# 1. GR symbol_sync_cc(Gardner) LOCKS at BER 0 on the matched-filter channel.
#    (Establishes the golden: this IS a channel the GR Gardner block handles.)
# =========================================================================== #
@pytest.mark.parametrize("seed", [1234, 77, 2026])
@pytest.mark.parametrize("frac", _FRACS)
def test_gr_gardner_locks_ber0_on_matched_channel(seed, frac):
    rng = np.random.default_rng(seed)
    bits = rng.integers(0, 2, 900).tolist()
    x = np.array([complex(v, 0.0) for v in _matched_channel(bits, frac)])
    gr = _gr_symbol_sync(x)
    ber = _ber_vs_tx(gr.real, bits)
    assert ber == 0.0, (
        f"GR symbol_sync_cc(Gardner) did NOT lock on the matched channel "
        f"(seed={seed} frac={frac} BER={ber}); the golden is invalid")


# =========================================================================== #
# 2. THE SUBSTRATE WALL — the DUT does NOT reach GR's BER 0 on that channel.
#    This is the QUARANTINE guard: it PINS the gap. Reference AND on-chip.
# =========================================================================== #
# The derived DECISION tolerance for a drop-in timing block is BER == 0 (a bit error
# is a hard miss). GR meets it; the DUT does not, by a wide, unambiguous margin.
_DUT_FLOOR_BER = 0.02   # the DUT stays WELL above this on the sweep (peak ~0.12)


@pytest.mark.parametrize("frac", _FRACS)
def test_dut_reference_fails_gr_equivalence(frac):
    """The Q15 Gardner REFERENCE recovers at BER far above GR's 0 on the exact
    matched-filter channel GR locks — the documented substrate wall. If this ever
    flips (DUT BER drops to ~0), the block was fixed: promote it and delete this
    guard (see ``test_gardner_would_be_gr_equivalent_when_fixed``)."""
    rng = np.random.default_rng(1234)
    bits = rng.integers(0, 2, 900).tolist()
    ber = _dut_reference_ber(bits, frac)
    assert ber > _DUT_FLOOR_BER, (
        f"frac={frac}: DUT reference BER={ber:.4f} is at/near GR's 0 — the wall "
        f"may be gone; RE-VERIFY and promote GardnerTimingRecovery to done")


@pytest.mark.parametrize("frac", [0.3, 0.5])
def test_dut_on_chip_fails_gr_equivalence(frac):
    """Same wall on the BUILT + SIMULATED on-chip DUT (not just the Python reference):
    the 4-cell Gardner loop, driven through x16_in on simKYT, recovers at BER ~5-10%
    where GR is 0. This is the on-silicon proof the quarantine is real."""
    rng = np.random.default_rng(1234)
    bits = rng.integers(0, 2, 400).tolist()
    ber = _dut_onchip_ber(bits, frac)
    assert ber > _DUT_FLOOR_BER, (
        f"frac={frac}: on-chip Gardner BER={ber:.4f} reached GR's 0 — RE-VERIFY")


# =========================================================================== #
# 3. Reproduction of the ROOT CAUSE: the block's OWN synthetic stimulus is one GR
#    itself cannot lock — the loop was tuned to a non-Nyquist test signal.
# =========================================================================== #
def _synthetic_non_matched(bits, frac, sps=2, beta=0.35, span=8):
    """The block's own ``_make_bpsk_2sps``: a SINGLE RRC and NO matched filter — a
    non-Nyquist stimulus with ISI at the symbol instants."""
    syms = np.array([1.0 if b else -1.0 for b in bits])
    up = np.zeros(len(syms) * sps)
    up[::sps] = syms
    taps = _rrc(sps, beta, span)
    shaped = np.convolve(up, taps, mode="same")
    n = np.arange(len(shaped))
    idx = np.clip(n + frac, 0, len(shaped) - 1)
    lo = np.floor(idx).astype(int)
    hi = np.clip(lo + 1, 0, len(shaped) - 1)
    fr = idx - lo
    off = shaped[lo] * (1 - fr) + shaped[hi] * fr
    off /= (np.max(np.abs(off)) + 1e-12)
    return off * 0.7


def test_block_synthetic_is_a_stimulus_gr_cannot_lock():
    """The block converges (BER 0) on its OWN synthetic stimulus, but GR
    symbol_sync_cc does NOT lock on that same stimulus — proof the loop was validated
    against a signal outside GR's operating regime. This is the INV-25 lesson made
    executable: 'it works in the demo' verified nothing against GR."""
    rng = np.random.default_rng(1234)
    bits = rng.integers(0, 2, 600).tolist()
    sig = _synthetic_non_matched(bits, 0.3)
    # DUT locks on its own stimulus...
    ref = GardnerTimingRecovery("g").process_reference(np.array(sig))
    dut_ber = _ber_vs_tx([_s16(v) for v in ref], bits)
    assert dut_ber == 0.0, f"DUT should lock its own synthetic (BER {dut_ber})"
    # ...but GR does NOT lock on that same (non-Nyquist) stimulus.
    x = np.array([complex(v, 0.0) for v in sig])
    gr_ber = _ber_vs_tx(_gr_symbol_sync(x).real, bits)
    assert gr_ber > 0.2, (
        f"GR unexpectedly locked the non-Nyquist synthetic (BER {gr_ber}); "
        f"the regime-mismatch premise of the quarantine needs review")


# =========================================================================== #
# 4. The VERIFIED ALTERNATIVE exists: MMTimingRecoveryBlock passes on this channel.
#    (A quarantine that names a working replacement is a valid, valuable outcome.)
# =========================================================================== #
def test_verified_alternative_mm_exists():
    """MMTimingRecoveryBlock — the project's VERIFIED timing-recovery block — has a
    passing report (bit-exact vs GR symbol_sync_cc, TED_MUELLER_AND_MULLER, BER 0 on
    the matched-filter channel). It is the drop-in a user should reach for."""
    rep = _VERIFY / "reports" / "MMTimingRecoveryBlock.json"
    assert rep.exists(), "MMTimingRecoveryBlock report missing"
    data = json.loads(rep.read_text())
    assert data.get("passed") is True
    assert "symbol_sync_cc" in data.get("grc_block", "")


# =========================================================================== #
# 5. The FLIP-WHEN-FIXED marker (xfail today; xpass promotes the block).
# =========================================================================== #
@pytest.mark.xfail(reason="GardnerTimingRecovery is quarantined (needs_human): the Q15 "
                          "Gardner TED cannot lock the matched-filter Nyquist channel "
                          "to GR's BER 0. Remove this xfail + the guards above and "
                          "promote to done when the block is redesigned.",
                   strict=True)
@pytest.mark.parametrize("frac", _FRACS)
def test_gardner_would_be_gr_equivalent_when_fixed(frac):
    """When Gardner is fixed to be a true GR drop-in, its reference will recover the TX
    bits at BER 0 on the matched-filter channel (the same bar MM meets). This xfail
    turns into an xpass the moment that happens — the signal to promote the block."""
    rng = np.random.default_rng(1234)
    bits = rng.integers(0, 2, 900).tolist()
    ber = _dut_reference_ber(bits, frac)
    assert ber == 0.0, f"frac={frac}: DUT BER {ber} (still quarantined)"


# =========================================================================== #
# Dashboard report — QUARANTINE record with measured numbers.
# =========================================================================== #
def test_write_quarantine_report():
    """Emit verification/reports/GardnerTimingRecovery.json documenting the quarantine
    (GR BER 0 vs DUT BER on the matched channel) for the dashboard / the paper."""
    rng = np.random.default_rng(1234)
    bits900 = rng.integers(0, 2, 900).tolist()
    bits400 = np.random.default_rng(1234).integers(0, 2, 400).tolist()
    per_frac = {}
    worst_dut = 0.0
    for frac in _FRACS:
        x = np.array([complex(v, 0.0) for v in _matched_channel(bits900, frac)])
        gr_ber = _ber_vs_tx(_gr_symbol_sync(x).real, bits900)
        dut_ref_ber = _dut_reference_ber(bits900, frac)
        per_frac[f"frac_{frac}"] = {
            "gr_ber": round(gr_ber, 4),
            "dut_reference_ber": round(dut_ref_ber, 4)}
        worst_dut = max(worst_dut, dut_ref_ber)
    # one on-chip point for the record
    onchip_ber = _dut_onchip_ber(bits400, 0.3)

    report = {
        "kyttar_block": "GardnerTimingRecovery",
        "grc_block": "digital.symbol_sync_cc (TED_GARDNER)",
        "passed": False,
        "status": "needs_human",
        "metric": "decision",
        "verdict": "QUARANTINE",
        "coverage": {
            "channel": "RRC TX + sinc fractional delay + RRC matched filter (Nyquist 2 sps)",
            "offsets": _FRACS, "seeds": [1234, 77, 2026],
            "gr_config": {"ted": "TED_GARDNER", "sps": _SPS, "loop_bw": _GR_LOOP_BW,
                          "damping": _GR_DAMPING, "ted_gain": _GR_TED_GAIN,
                          "max_deviation": _GR_MAX_DEV},
            "attempts": ["amplitude+RMS-normalisation sweep", "loop-gain/period-trace diagnosis"]},
        "metrics": {
            "per_frac": per_frac,
            "dut_on_chip_ber_frac0.3": round(onchip_ber, 4),
            "gr_ber_all_fracs": 0.0,
            "dut_worst_reference_ber": round(worst_dut, 4)},
        "verified_alternative": "MMTimingRecoveryBlock",
        "notes": (
            "QUARANTINE (INV-25 + the scope steer). GardnerTimingRecovery is NOT a "
            "drop-in for digital.symbol_sync_cc(TED_GARDNER) on the industry-standard "
            "matched-filter Nyquist 2-sps channel: GR locks BER 0 across the whole "
            "fractional-offset sweep; the Q15 Gardner loop recovers at BER ~0.04-0.12 "
            "(reference) / ~0.05-0.10 (on-chip) because the TED HALVES the BPSK sample "
            "difference (>>1) to fit int16 and the resulting timing jitter closes the "
            "Nyquist eye. The block converges BER 0 ONLY on a non-Nyquist synthetic "
            "stimulus (its own _make_bpsk_2sps) that GR ITSELF cannot lock (BER ~0.45) "
            "— it was tuned outside GR's regime. Gardner is a BPSK/QPSK-only TED and "
            "this matches the documented 4-PAM limit that blocked the M17 4FSK modem. "
            "The VERIFIED timing-recovery block for this channel is "
            "MMTimingRecoveryBlock (bit-exact vs GR symbol_sync_cc, BER 0). Fixing "
            "Gardner is a datapath REDESIGN (a wider TED product without the >>1 "
            "truncation), not a tolerance tweak — quarantined for a human."),
    }
    out = _VERIFY / "reports" / "GardnerTimingRecovery.json"
    out.write_text(json.dumps(report, indent=2) + "\n")
    assert out.exists()
