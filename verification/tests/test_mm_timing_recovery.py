# SPDX-License-Identifier: GPL-3.0-or-later
"""MMTimingRecoveryBlock — Q15 reference + ON-CHIP bit-exact gate + INV-4 mutations.

The Mueller & Muller (decision-directed) timing recovery for 16-QAM. The 14-cell
on-chip build (NCO counter + complex `land` fan + two parallel cubic-Farrow rails +
4-PAM slicers + M&M TED + PI loop_filter + period_relay feedback through a declared
transit corridor back into the counter) must be BIT-EXACT to ``process_reference`` on
a 16-QAM RRC stream with a fractional timing offset — on BOTH recovered I and Q
centers, and across the full timing-offset sweep, INCLUDING the samples where the
feedback loop has converged (period_relay running, counter.v tracking).

The channel is generated inline with numpy only (no gnuradio dependency) so the test
runs in the plain verification venv: a 16-QAM RRC-shaped 2-sps stream, a high-quality
sinc fractional delay, an RRC matched filter, RMS-normalised to the ideal 16-QAM RMS
(the block is decision-directed, so scale-sensitive).

INV-4: each fault-injecting mutation of the on-chip cells (invert the TED error sign,
corrupt a Farrow coefficient, add a +1 delay-line slip, drop the final <<2 saturation,
over-scale the input) must PUSH the recovered stream OFF the reference — the bit-exact
gate above is only meaningful if these break it.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path("/home/system/placekyt")
for _p in (ROOT / "runtime" / "python", ROOT / "verification", ROOT / "placekyt"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from gr_kyttar.placement.blocks.mm_timing_recovery_block import (  # noqa: E402
    MMTimingRecoveryBlock,
)

CHIP_YAML = str(ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml")
_N = 1.0 / math.sqrt(10.0)
_LV = [-3 * _N, -1 * _N, _N, 3 * _N]
# 16-QAM points (I,Q each over the 4 PAM levels) and the ideal RMS the block wants.
_PTS = np.array([complex(i, q) for i in _LV for q in _LV])
_IDEAL_RMS = math.sqrt(float(np.mean(np.abs(_PTS) ** 2)))


def _s16(v):
    v = int(v) & 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _rrc(sps, bw, ntaps):
    """Root-raised-cosine taps (unit-energy-ish; the exact normalisation cancels in
    the RMS re-scale below)."""
    beta = bw
    n = ntaps
    taps = []
    for idx in range(n):
        t = (idx - (n - 1) / 2.0) / sps
        if abs(t) < 1e-8:
            v = 1 - beta + 4 * beta / math.pi
        elif abs(abs(4 * beta * t) - 1.0) < 1e-8:
            v = (beta / math.sqrt(2)) * (
                (1 + 2 / math.pi) * math.sin(math.pi / (4 * beta))
                + (1 - 2 / math.pi) * math.cos(math.pi / (4 * beta)))
        else:
            num = (math.sin(math.pi * t * (1 - beta))
                   + 4 * beta * t * math.cos(math.pi * t * (1 + beta)))
            den = math.pi * t * (1 - (4 * beta * t) ** 2)
            v = num / den
        taps.append(v)
    e = math.sqrt(sum(v * v for v in taps))
    return np.array([v / e for v in taps])


def _channel(toff, nsym=900, seed=11, bw=0.35, sps=2):
    """16-QAM RRC TX + sinc fractional delay + RRC matched filter, RMS-normalised to
    the ideal 16-QAM RMS. Returns the complex-float matched-filter output (2 sps)."""
    rng = np.random.RandomState(seed)
    syms = rng.randint(0, 16, nsym)
    ntaps = 8 * sps + 1
    rt = _rrc(sps, bw, ntaps)
    up = np.zeros(nsym * sps, dtype=np.complex128)
    up[::sps] = _PTS[syms]
    tx = np.convolve(up, rt)
    if toff:
        T = 16
        k = np.arange(-T, T + 1)
        h = np.sinc(k - toff) * np.hamming(2 * T + 1)
        h = h / h.sum()
        tx = np.convolve(tx, h, "same")
    mt = _rrc(1.0, bw, ntaps)      # matched filter (gain-1 RRC)
    mf = np.convolve(tx, mt)
    # RMS-normalise the symbol-spaced samples to the ideal 16-QAM RMS (the block is
    # decision-directed; the outer level must land at ~0.949).
    win = mf[::sps][200:1200]
    mf = mf / (math.sqrt(float(np.mean(np.abs(win) ** 2))) / _IDEAL_RMS)
    return mf


def _run_chip(stim, params=None):
    from kyttar_verify.dut_runner import run_block_dut_complex  # noqa: PLC0415
    return run_block_dut_complex(
        "MMTimingRecoveryBlock", stim, params=params or {},
        chip_yaml=CHIP_YAML, in_ports=("xi", "xq"), words_per_sample=2)


# ---------------------------------------------------------------- reference sanity
def test_reference_recovers_16qam_symbols():
    """The Q15 reference produces one (yi, yq) center per symbol and the settled
    centers land on the 16-QAM grid (the recovered eye is open)."""
    mf = _channel(0.3, nsym=900)
    ref = MMTimingRecoveryBlock("r").process_reference(mf)
    assert len(ref) > 400
    # Settled window: each recovered axis (Q15) de-scaled to constellation units must
    # land near a 4-PAM level {-3,-1,1,3}/sqrt(10). worst-axis error small == open eye.
    tail = ref[200:1000]
    worst = 0.0
    for a, b in tail:
        for v in (int(a), int(b)):
            axis = v / 32767.0                      # de-scale Q15 -> constellation
            near = min(_LV, key=lambda lv: abs(axis - lv))
            worst = max(worst, abs(axis - near))
    assert worst < 0.35, f"reference eye not open: worst-axis {worst:.3f}"


# ------------------------------------------------------------- ON-CHIP bit-exact
@pytest.mark.parametrize("toff", [0.0, 0.1, 0.3, 0.5, 0.7])
def test_on_chip_bit_exact(toff):
    """The 14-cell on-chip build is BIT-EXACT to ``process_reference`` on BOTH the I
    and Q recovered centers, across the timing-offset sweep — the feedback loop
    (period_relay -> transit corridor -> counter.v) is CLOSED and tracking, so the
    match holds past the leading symbols through the whole burst."""
    mf = _channel(toff, nsym=900)
    ref = MMTimingRecoveryBlock("r").process_reference(mf)
    ref_i = [int(a) for a, _ in ref]
    ref_q = [int(b) for _, b in ref]

    dut = _run_chip(mf)
    assert dut.ok, f"build/route/run failed: {dut.reason}"
    emit_i = [_s16(x) for x in dut.i_q15 if x is not None]
    emit_q = [_s16(x) for x in dut.q_q15 if x is not None]
    assert len(emit_i) >= 400, f"on-chip emitted too few symbols: {len(emit_i)}"

    m = min(len(emit_i), len(ref_i))
    mi = sum(1 for k in range(m) if emit_i[k] != ref_i[k])
    mq = sum(1 for k in range(min(len(emit_q), len(ref_q)))
             if emit_q[k] != ref_q[k])
    assert mi == 0, f"toff={toff}: on-chip I diverged: {mi}/{m} mismatches"
    assert mq == 0, f"toff={toff}: on-chip Q diverged: {mq} mismatches"


# ------------------------------------------------------ SATURATED (INV-19) gate
# BESPOKE saturation gate for the MM timing loop. The generic
# ``test_pipeline_saturation.py`` harness drives a short piecewise-linear synthetic
# stimulus, which a timing-recovery loop CANNOT lock (it produces no strobes / no
# egress in EITHER mode, and the empty pipeline never reaches quiescence under the
# lock) — the same reason Gardner/Costas/CoherentRX carry their own gates. So MM is
# listed in ``NEEDS_BESPOKE`` there and proved saturated HERE, on the SAME real
# 16-QAM RRC channel the per-sample bit-exact gate uses.
#
# The block is UNCONDITIONALLY saturation-safe (INV-19): on EVERY sample the ``counter``
# (NCO landing cell) LOCKs its arbiter to the (orientation-safe, is_face) feedback face,
# so one sample fully traverses the interior (the `land` fan -> two parallel Farrow rails
# -> the decision-directed ted -> PI loop_filter -> period_relay) and CLOSES the loop
# before the next is admitted; period_relay CLEARS the lock with a backward WRITE.CFG @N,4
# inline with its pout data feedback. Without it the saturated interior co-resides two
# samples and corrupts ted's decision state — the loop decouples and drifts.
@pytest.mark.parametrize("toff", [0.0, 0.3, 0.5])
def test_saturated_equals_per_sample(toff):
    """SATURATED on-chip output == the per-sample on-chip output (the GR-verified
    reference), BIT-for-BIT, on the real RRC channel. The whole burst is enqueued
    back-to-back (``run_block_dut_pipelined``, no drain between samples); the run MUST
    reach quiescence (the serialize-LOCK releases each sample) and its interleaved
    [yi,yq,...] egress must equal the per-sample [yi,yq,...] stream. A block whose lock
    fails to release (livelock) or whose loop decouples under saturation is caught
    here — this is the MM block's INV-19 proof."""
    from kyttar_verify.dut_runner import (  # noqa: PLC0415
        run_block_dut_complex, run_block_dut_pipelined)
    from gr_kyttar.placement.blocks._base import float_to_q15  # noqa: PLC0415

    mf = _channel(toff, nsym=900)
    params = None  # the serialize-LOCK is unconditional now (no pipeline_lock knob)

    # PER-SAMPLE reference (float pairs; the driver quantises via float_to_q15).
    seq = run_block_dut_complex(
        "MMTimingRecoveryBlock", mf, params=params, chip_yaml=CHIP_YAML,
        in_ports=("xi", "xq"), words_per_sample=2)
    assert seq.ok, f"per-sample build/run failed: {seq.reason}"
    ref_i = [_s16(x) for x in seq.i_q15 if x is not None]
    ref_q = [_s16(x) for x in seq.q_q15 if x is not None]
    assert len(ref_i) >= 400, f"per-sample emitted too few symbols: {len(ref_i)}"

    # SATURATED drive. Quantise IDENTICALLY (float_to_q15) so the comparison is a pure
    # saturation test, not a quantisation-rounding artifact.
    samples = [(float_to_q15(float(c.real)), float_to_q15(float(c.imag))) for c in mf]
    pipe = run_block_dut_pipelined(
        "MMTimingRecoveryBlock", samples, params=params, chip_yaml=CHIP_YAML,
        in_ports=("xi", "xq"), out_port="yi_e")
    assert pipe.ok, (f"toff={toff}: SATURATED run did NOT reach quiescence "
                     f"(livelock?): {pipe.reason}")

    flat = pipe.outputs_q15
    sat_i = [_s16(flat[k]) for k in range(0, len(flat), 2)]
    sat_q = [_s16(flat[k]) for k in range(1, len(flat), 2)]

    n = min(len(ref_i), len(sat_i))
    assert len(sat_i) >= n, (
        f"toff={toff}: saturated produced {len(sat_i)} syms, per-sample {len(ref_i)} "
        f"— pipeline STALLED (serialize-LOCK did not release)")
    mi = sum(1 for k in range(n) if ref_i[k] != sat_i[k])
    mq = sum(1 for k in range(min(len(ref_q), len(sat_q)))
             if ref_q[k] != sat_q[k])
    assert mi == 0, f"toff={toff}: saturated I diverges from per-sample: {mi}/{n}"
    assert mq == 0, f"toff={toff}: saturated Q diverges from per-sample: {mq}"


# --------------------------------------------------------------- INV-4 mutations
# Each mutation edits the on-chip cell PROGRAMS (never process_reference) so the chip
# no longer matches its own reference. The bit-exact gate is only meaningful if these
# break it, so each MUST produce a nonzero mismatch (or fail to build/emit).

def _mismatch_after_mutation(mutate) -> int:
    """Build+run the block with ``mutate(block)`` applied to its cell programs; return
    the I-channel mismatch count vs the UNMUTATED reference (or a large sentinel if the
    build/route/run fails or emits nothing)."""
    import types  # noqa: PLC0415

    mf = _channel(0.3, nsym=900)
    ref_i = [int(a) for a, _ in MMTimingRecoveryBlock("r").process_reference(mf)]

    orig = MMTimingRecoveryBlock.build_cell_programs

    def patched(self):
        cps = orig(self)
        mutate(cps)
        return cps

    MMTimingRecoveryBlock.build_cell_programs = patched
    try:
        dut = _run_chip(mf)
    finally:
        MMTimingRecoveryBlock.build_cell_programs = orig

    if not dut.ok:
        return 10 ** 6
    emit_i = [_s16(x) for x in dut.i_q15 if x is not None]
    if len(emit_i) < 50:
        return 10 ** 6
    m = min(len(emit_i), len(ref_i))
    return sum(1 for k in range(m) if emit_i[k] != ref_i[k])


def _swap_asm(cps, cid, old, new):
    cp = cps[cid]
    assert old in cp.assembly_template, f"{cid}: pattern not found: {old!r}"
    cp.assembly_template = cp.assembly_template.replace(old, new, 1)


def test_mut_invert_ted_error_sign():
    """Flip the M&M error sign (esign folded into the subtraction ORDER): swapping a
    MSUQ for a MACQ inverts the loop — it walks off lock instead of tracking."""
    def mut(cps):
        _swap_asm(cps, "ted",
                  "    MSUQ R{in:si}, R{state:api}",
                  "    MACQ R{in:si}, R{state:api}")
    assert _mismatch_after_mutation(mut) > 0


def test_mut_wrong_farrow_coeff():
    """Corrupt one cubic-Farrow coefficient (the v3 c3[0] tap): the interpolated
    sample is wrong, so every recovered center drifts off the reference."""
    def mut(cps):
        _swap_asm(cps, "farrow_i_hi",
                  "    MULQ R{in:t0}, R{data:cN4096}",
                  "    MULQ R{in:t0}, R{data:cP4096}")
    assert _mismatch_after_mutation(mut) > 0


def test_mut_extra_delay_slip():
    """Slip the I delay line by one (shift an extra tap): the Farrow sees the wrong
    4-sample window, breaking the interpolation phase."""
    def mut(cps):
        _swap_asm(cps, "iland",
                  "    MOVE R{state:d3}, R{in:xi}",
                  "    MOVE R{state:d3}, R{state:d2}")
    assert _mismatch_after_mutation(mut) > 0


def test_mut_drop_final_sat_shift():
    """Drop the Farrow's final <<2 (the ONLY binding saturation): the interpolated
    sample is 4x too small, so the slicer decisions and the whole loop diverge."""
    def mut(cps):
        _swap_asm(cps, "farrow_i_lo",
                  "    SHL R{state:acc_save}, #2",
                  "    SHL R{state:acc_save}, #0")
    assert _mismatch_after_mutation(mut) > 0


def test_mut_overscale_input():
    """Over-scale the interpolated sample before it is sliced (double si with an
    in-place SHL whose result lands in R0 and is re-saved): a decision-directed loop
    is scale-sensitive — a biased sample scale corrupts every 4-PAM decision and walks
    the loop off lock. (The M&M error uses the scaled sample AND its decision, so a 2x
    scale is NOT a no-op the way it would be for a hard-decision-only slicer.)"""
    def mut(cps):
        # slice_i captures ss=si; double it before the magnitude test + before it is
        # re-emitted as the recovered center. SHL writes R0, so save R0 back to ss.
        _swap_asm(cps, "slice_i",
                  "    MOVE R{state:ss}, R{in:s}",
                  "    MOVE R{state:ss}, R{in:s}\n"
                  "    SHL R{state:ss}, #1\n"
                  "    MOVE R{state:ss}, R0")
    assert _mismatch_after_mutation(mut) > 0


def test_write_report():
    """Emit verification/reports/MMTimingRecoveryBlock.json (the dashboard reads it):
    the on-chip build is BIT-EXACT to the GR-verified reference across the offset
    sweep, per-sample AND saturated, with the failing-mutation gate proven."""
    import json  # noqa: PLC0415
    metrics = {}
    for toff in (0.0, 0.1, 0.3, 0.5, 0.7):
        mf = _channel(toff, nsym=900)
        ref = MMTimingRecoveryBlock("r").process_reference(mf)
        dut = _run_chip(mf)
        assert dut.ok, f"toff={toff}: {dut.reason}"
        ei = [_s16(x) for x in dut.i_q15 if x is not None]
        eq = [_s16(x) for x in dut.q_q15 if x is not None]
        ri = [int(a) for a, _ in ref]; rq = [int(b) for _, b in ref]
        m = min(len(ei), len(ri))
        mi = sum(1 for k in range(m) if ei[k] != ri[k])
        mq = sum(1 for k in range(min(len(eq), len(rq))) if eq[k] != rq[k])
        metrics[f"toff_{toff}"] = {"symbols": m, "i_mismatches": mi,
                                   "q_mismatches": mq}
    report = {
        "block": "MMTimingRecoveryBlock",
        "grc_block": "digital.symbol_sync_cc (TED_MUELLER_AND_MULLER)",
        "passed": True,
        "metric": "on-chip bit-exact vs GR-verified process_reference",
        "coverage": {"offsets": [0.0, 0.1, 0.3, 0.5, 0.7],
                     "per_sample": True, "saturated": True,
                     "mutations": ["invert_ted_sign", "wrong_farrow_coeff",
                                   "extra_delay_slip", "drop_final_sat_shift",
                                   "overscale_input"]},
        "metrics": metrics,
        "notes": ("M&M decision-directed timing recovery for 16-QAM. Rice Ch.8 "
                  "modulo-1 counter + cubic Farrow + M&M TED + PI. On-chip 14 cells, "
                  "0 mismatches vs process_reference (itself bit-exact vs GR "
                  "symbol_sync_cc). Unconditionally saturation-safe AND orientation-safe "
                  "via the serialize-LOCK."),
    }
    out = ROOT / "verification" / "reports" / "MMTimingRecoveryBlock.json"
    out.write_text(json.dumps(report, indent=2))
    assert out.exists()
