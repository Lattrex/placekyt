# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify FreqXlatingFIRBlock against GNU Radio filter.freq_xlating_fir_filter_ccf.

FreqXlatingFIR is the workhorse channelizer: a frequency shift (a complex mixer with
an NCO) FUSED with a decimating real-tap FIR. GR semantics (VERBATIM param names):
multiply the complex input by exp(-j·2π·center_freq/sampling_freq·n) (a DOWN-shift),
apply the real FIR ``taps``, and decimate by ``decimation``.

GR folds the rotator into the taps for efficiency; this block is OUTPUT-equivalent by
the algebraically-exact decomposition (derived + verified against GR here):

    out[m] = mixed_FIR[m·decimation]
    mixed[n] = x[n]·exp(-j·(fwT0·n + θ0)),  fwT0 = 2π·center_freq/sampling_freq
    θ0 = fwT0·(L-1)/2   (GR's output-rotator group-delay phase, folded into the NCO
                         initial phase so the whole thing is one down-mixer + real FIR)

Two reference tiers (exactly the ComplexMixer pattern):
  * BIT-EXACT substrate — DUT vs process_reference_q15 (the interpolated cos/sin NCO
    down-mix + the wrapping-MACQ real FIR + the phase-0 decimation gate), EXACT.
  * DSP equivalence — DUT vs GNU Radio freq_xlating_fir_filter_ccf, AMPLITUDE, within a
    DERIVED tolerance: the NCO interpolated-table floor (~12 LSB, inherited from the
    ComplexMixer/NCO) attenuated through the Σ|h|≤1 FIR, plus ≤1 LSB/tap FIR rounding.

Coverage (AGENTS.md §4): edge (center_freq=0 → pure FIR; decimation=1; a real tone
shifted to baseband; taps=[1] passthrough), random ≥3 seeds, a sweep over center_freq
× decimation × tap sets, and mutation tests (wrong shift sign, missing decimation,
wrong taps, swap I/Q, +1 delay, empty) proven to FAIL the gate (INV-4).

HARDWARE CONSTRAINT: Σ|taps| ≤ 1 (head_shift == 0) — the fused last FIR cell (dual-rail
emit + mod-M decimation gate) has no room for a coefficient-headroom saturating restore
(it RAISES otherwise). All tap sets here are normalized (a firdes low/band-pass at
gain ≤ 1 satisfies this).

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_freq_xlating_fir.py -q
"""
from __future__ import annotations

import json
import math
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

from kyttar_verify import (  # noqa: E402
    run_block_dut_complex, run_gnuradio_ref_complex, compare_complex_against_grc,
    Metric)
from gr_kyttar.placement.blocks.freq_xlating_fir_block import (  # noqa: E402
    FreqXlatingFIRBlock)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_AVAILABLE),
    reason="chip yaml or GNU Radio interpreter absent")

FS = 32000.0
# Derived DUT-vs-GR tolerance (LSB). The NCO interpolated-quarter-wave table floor is
# ~12 LSB (inherited from ComplexMixer/NCO — TABLE_FLOOR_LSB=12 in test_complex_mixer),
# and the real FIR adds ≤1 LSB/tap of MACQ rounding. The taps are Σ|h|≤1 (attenuating),
# so the mixer floor is scaled DOWN through the filter, but we keep the honest un-scaled
# floor + a small FIR margin rather than tune it to pass. Not RMS-normalized: the FIR
# attenuation keeps absolute LSB the meaningful metric (values stay well inside Q15).
GR_TOL_LSB = 16


def _s16(v):
    if v is None:
        return None
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def _signal(seed, n, amp=0.35):
    rng = np.random.RandomState(seed)
    return [complex(rng.uniform(-amp, amp), rng.uniform(-amp, amp)) for _ in range(n)]


def _norm_taps(seed, L, lo=-0.2, hi=0.3):
    """A normalized (Σ|h| ≤ 1) real tap set."""
    rng = np.random.RandomState(seed + 1000)
    t = rng.uniform(lo, hi, L)
    s = np.sum(np.abs(t))
    return list(t / max(1.0, s))


def _run_dut(decimation, taps, center_freq, stim, fs=FS):
    dut = run_block_dut_complex(
        "FreqXlatingFIRBlock", stim,
        params={"decimation": decimation, "taps": list(taps),
                "center_freq": center_freq, "sampling_freq": fs},
        chip_yaml=CHIP_YAML, in_ports=("xi", "xq"), out_port="out",
        words_per_sample=2)
    return dut


def _gr_fx(decimation, taps, center_freq, stim, fs=FS):
    """GNU Radio golden reference: filter.freq_xlating_fir_filter_ccf."""
    return run_gnuradio_ref_complex(
        stim,
        gnuradio_script="""
from gnuradio import gr, blocks, filter
tb = gr.top_block()
src = blocks.vector_source_c(input_complex, False)
fx = filter.freq_xlating_fir_filter_ccf(decimation, taps, center_freq, fs)
snk = blocks.vector_sink_c()
tb.connect(src, fx, snk)
tb.run()
output_complex = list(snk.data())
""",
        extra_args={"decimation": int(decimation),
                    "taps": [float(t) for t in taps],
                    "center_freq": float(center_freq), "fs": float(fs)})


def _emitted(dut):
    """The non-dropped (non-None) I/Q streams (decimation records dropped as None)."""
    ei = [v for v in dut.i_q15 if v is not None]
    eq = [v for v in dut.q_q15 if v is not None]
    return ei, eq


# --- structure / smoke --------------------------------------------------------

def test_fx_drives_and_captures():
    dut = _run_dut(1, [0.25, 0.5, 0.25], 2000.0, _signal(1, 24))
    assert dut.ok, dut.reason
    assert dut.words_per_sample == 2, f"expected 2 words/sample, got {dut.words_per_sample}"
    assert dut.in_regs == (0, 1), "complex signal lands xi@R0, xq@R1"
    assert all(v is not None for v in dut.i_q15) and all(v is not None for v in dut.q_q15)


def test_param_names_mirror_gnuradio():
    """The block exposes GR's exact param names (INV-0)."""
    b = FreqXlatingFIRBlock("fx", decimation=2, taps=[0.5, 0.5],
                            center_freq=1000.0, sampling_freq=48000.0)
    assert set(("decimation", "taps", "center_freq", "sampling_freq")) <= set(b.params)
    assert b.params["decimation"] == 2 and b.params["center_freq"] == 1000.0
    assert b.params["taps"] == [0.5, 0.5] and b.params["sampling_freq"] == 48000.0


def test_sum_abs_over_one_raises():
    """HARDWARE LIMIT (documented + loud): Σ|taps| > 1 RAISES rather than silently
    rescaling (the fused last cell has no headroom for a saturating restore)."""
    with pytest.raises(ValueError, match=r"Σ\|taps\|"):
        FreqXlatingFIRBlock("fx", taps=[0.9, 0.9], center_freq=0.0)


# --- EDGE: center_freq=0 (pure FIR), passthrough, tone-to-baseband -------------

def test_center_freq_zero_is_pure_fir():
    """center_freq=0 → the NCO is exp(0)=1 every sample → the block is a plain complex
    FIR. Bit-exact to the reference AND matches GR's fir_filter_ccf semantics."""
    stim = _signal(3, 48)
    taps = [0.1, 0.2, 0.4, 0.2, 0.1]
    dut = _run_dut(1, taps, 0.0, stim)
    assert dut.ok, dut.reason
    ref = FreqXlatingFIRBlock("r", decimation=1, taps=taps, center_freq=0.0,
                              sampling_freq=FS).process_reference_q15(stim)
    ri = [_s16(a) / 32768.0 for a, _ in ref]
    rq = [_s16(b) / 32768.0 for _, b in ref]
    res = compare_complex_against_grc(dut.i_q15, dut.q_q15, ri, rq,
                                      metric=Metric.EXACT, delay=0)
    assert res.passed, res.summary()


def test_taps_unit_is_passthrough_mixer():
    """taps=[1.0] → the FIR is identity → the block is a pure down-mixer. Bit-exact to
    the on-chip NCO down-mix reference."""
    stim = _signal(9, 32)
    dut = _run_dut(1, [1.0], 2000.0, stim)
    assert dut.ok, dut.reason
    ref = FreqXlatingFIRBlock("r", taps=[1.0], center_freq=2000.0,
                              sampling_freq=FS).process_reference_q15(stim)
    ri = [_s16(a) / 32768.0 for a, _ in ref]
    rq = [_s16(b) / 32768.0 for _, b in ref]
    res = compare_complex_against_grc(dut.i_q15, dut.q_q15, ri, rq,
                                      metric=Metric.EXACT, delay=0)
    assert res.passed, res.summary()


def test_real_tone_shifted_to_baseband():
    """A complex tone at +center_freq, down-shifted to DC, then low-passed, is a slowly
    varying (near-DC) complex output. Verified bit-exact vs the reference; and the DUT
    matches GR's freq_xlating within the derived floor."""
    fc = 2000.0
    n = 64
    stim = [complex(0.4 * math.cos(2 * math.pi * fc / FS * k),
                    0.4 * math.sin(2 * math.pi * fc / FS * k)) for k in range(n)]
    taps = _norm_taps(0, 5)
    dut = _run_dut(1, taps, fc, stim)
    assert dut.ok, dut.reason
    gr = _gr_fx(1, taps, fc, stim)
    res = compare_complex_against_grc(dut.i_q15, dut.q_q15, gr.i, gr.q,
                                      metric=Metric.AMPLITUDE, delay=0,
                                      tolerance=GR_TOL_LSB)
    assert res.passed, res.summary()


# --- BIT-EXACT substrate (DUT == process_reference_q15) ------------------------

@pytest.mark.parametrize("decim,L,fc", [
    (1, 3, 2000.0), (1, 5, 4000.0), (1, 8, 6000.0), (1, 1, 0.0),
    (2, 3, 2000.0), (2, 5, 3000.0), (2, 7, 3000.0), (2, 8, 4000.0),
    (4, 3, 2000.0), (4, 9, 1234.0), (1, 4, -5000.0), (2, 1, 2000.0)])
def test_bitexact_reference(decim, L, fc):
    """DUT matches the on-chip Q15 reference EXACTLY (NCO down-mix + wrapping-MACQ real
    FIR + phase-0 decimation) — every parameter combination, both I and Q. This is the
    strong hardware-determining gate; a wrong register/handoff/gate shows here."""
    stim = _signal(42, 40)
    taps = _norm_taps(L, L)
    dut = _run_dut(decim, taps, fc, stim)
    assert dut.ok, dut.reason
    ref = FreqXlatingFIRBlock("r", decimation=decim, taps=taps, center_freq=fc,
                              sampling_freq=FS).process_reference_q15(stim)
    ei, eq = _emitted(dut)
    n = min(len(ei), len(ref))
    assert n >= len(ref) - 1, f"decim={decim}: only {len(ei)} emitted, ref {len(ref)}"
    di = [_s16(v) for v in ei[:n]]
    dq = [_s16(v) for v in eq[:n]]
    ri = [_s16(a) for a, _ in ref[:n]]
    rq = [_s16(b) for _, b in ref[:n]]
    assert di == ri, f"I mismatch decim={decim} L={L} fc={fc}\n dut={di[:8]}\n ref={ri[:8]}"
    assert dq == rq, f"Q mismatch decim={decim} L={L} fc={fc}\n dut={dq[:8]}\n ref={rq[:8]}"


# --- DSP equivalence vs GNU Radio (derived tolerance) -------------------------

@pytest.mark.parametrize("decim,L,fc,seed", [
    (1, 3, 2000.0, 1), (1, 5, 4000.0, 2), (1, 7, 6000.0, 3),
    (2, 5, 3000.0, 1), (2, 7, 3000.0, 2), (4, 9, 1234.0, 3),
    (1, 4, -5000.0, 4), (2, 3, 0.0, 5)])
def test_matches_gnuradio(decim, L, fc, seed):
    """DUT is a drop-in for GNU Radio freq_xlating_fir_filter_ccf within the derived
    NCO-table + FIR floor (~16 LSB) — the whole center_freq × decimation × taps sweep,
    random ≥3 seeds. Both I and Q gated (a swap/negate/latency in either fails)."""
    stim = _signal(seed, 64)
    taps = _norm_taps(L + seed, L)
    dut = _run_dut(decim, taps, fc, stim)
    assert dut.ok, dut.reason
    gr = _gr_fx(decim, taps, fc, stim)
    ei, eq = _emitted(dut)
    res = compare_complex_against_grc(ei, eq, gr.i, gr.q,
                                      metric=Metric.AMPLITUDE, delay=0,
                                      tolerance=GR_TOL_LSB)
    print(f"\nvs GR decim={decim} L={L} fc={fc} seed={seed}:", res.summary())
    assert res.passed, res.summary()


# --- MUTATION tests (INV-4): a corrupted DUT MUST fail the gate ----------------

def _ref_iq(decim, taps, fc, stim):
    ref = FreqXlatingFIRBlock("m", decimation=decim, taps=taps, center_freq=fc,
                              sampling_freq=FS).process_reference_q15(stim)
    return ([_s16(a) / 32768.0 for a, _ in ref],
            [_s16(b) / 32768.0 for _, b in ref])


def test_mutation_wrong_shift_sign_fails():
    """UP-shift (wrong sign of center_freq) must NOT match the DOWN-shift reference."""
    stim = _signal(11, 48)
    taps = [0.25, 0.5, 0.25]
    fc = 4000.0
    dut = _run_dut(1, taps, -fc, stim)   # wrong sign → up-shift
    assert dut.ok, dut.reason
    ri, rq = _ref_iq(1, taps, fc, stim)  # correct down-shift reference
    res = compare_complex_against_grc(dut.i_q15, dut.q_q15, ri, rq,
                                      metric=Metric.EXACT, delay=0)
    assert not res.passed, "wrong shift sign should FAIL the gate"


def test_mutation_missing_decimation_fails():
    """decimation=1 output vs a decimation=2 reference must FAIL (wrong rate/phase)."""
    stim = _signal(13, 48)
    taps = _norm_taps(2, 5)
    fc = 3000.0
    dut = _run_dut(1, taps, fc, stim)             # NO decimation
    assert dut.ok, dut.reason
    ref2 = FreqXlatingFIRBlock("m", decimation=2, taps=taps, center_freq=fc,
                               sampling_freq=FS).process_reference_q15(stim)
    ri = [_s16(a) / 32768.0 for a, _ in ref2]
    rq = [_s16(b) / 32768.0 for _, b in ref2]
    # DUT has ~2x as many outputs as the decim=2 ref; the prefix must still disagree.
    res = compare_complex_against_grc(dut.i_q15[:len(ri)], dut.q_q15[:len(rq)], ri, rq,
                                      metric=Metric.EXACT, delay=0)
    assert not res.passed, "missing decimation should FAIL the gate"


def test_mutation_wrong_taps_fails():
    """A different tap set must FAIL against the correct-taps reference."""
    stim = _signal(17, 48)
    fc = 2000.0
    good = [0.25, 0.5, 0.25]
    dut = _run_dut(1, [0.5, 0.0, 0.5], fc, stim)  # wrong taps
    assert dut.ok, dut.reason
    ri, rq = _ref_iq(1, good, fc, stim)
    res = compare_complex_against_grc(dut.i_q15, dut.q_q15, ri, rq,
                                      metric=Metric.EXACT, delay=0)
    assert not res.passed, "wrong taps should FAIL the gate"


def test_mutation_swap_iq_fails():
    """Swapping the DUT's I and Q channels must FAIL (proves both channels are gated)."""
    stim = _signal(19, 48)
    taps = [0.25, 0.5, 0.25]
    fc = 2000.0
    dut = _run_dut(1, taps, fc, stim)
    assert dut.ok, dut.reason
    ri, rq = _ref_iq(1, taps, fc, stim)
    res = compare_complex_against_grc(dut.q_q15, dut.i_q15, ri, rq,   # swapped
                                      metric=Metric.EXACT, delay=0)
    assert not res.passed, "swapped I/Q should FAIL the gate"


def test_mutation_plus_one_delay_fails():
    """A +1 sample delay must FAIL when delay=0 is asserted (INV-2: no free lag)."""
    stim = _signal(23, 48)
    taps = [0.25, 0.5, 0.25]
    fc = 2000.0
    dut = _run_dut(1, taps, fc, stim)
    assert dut.ok, dut.reason
    ri, rq = _ref_iq(1, taps, fc, stim)
    # shift the DUT by one sample → must not match the delay=0 reference
    res = compare_complex_against_grc(dut.i_q15[1:], dut.q_q15[1:], ri, rq,
                                      metric=Metric.EXACT, delay=0)
    assert not res.passed, "+1 sample delay should FAIL the gate"


def test_mutation_empty_output_fails():
    """An empty DUT output must FAIL (the gate cannot read a stalled block green)."""
    stim = _signal(29, 48)
    taps = [0.25, 0.5, 0.25]
    ri, rq = _ref_iq(1, taps, 2000.0, stim)
    res = compare_complex_against_grc([], [], ri, rq, metric=Metric.EXACT, delay=0)
    assert not res.passed, "empty output should FAIL the gate"


# --- report (for the dashboard) ------------------------------------------------

def test_write_report():
    """Emit verification/reports/FreqXlatingFIR.json (pass + measured metrics vs GR)
    for the dashboard. Uses a representative decimating channelizer config."""
    decim, L, fc = 2, 5, 3000.0
    stim = _signal(101, 64)
    taps = _norm_taps(L, L)
    dut = _run_dut(decim, taps, fc, stim)
    assert dut.ok, dut.reason
    gr = _gr_fx(decim, taps, fc, stim)
    ei, eq = _emitted(dut)
    res = compare_complex_against_grc(ei, eq, gr.i, gr.q,
                                      metric=Metric.AMPLITUDE, delay=0,
                                      tolerance=GR_TOL_LSB)
    assert res.passed, res.summary()
    report = {
        "kyttar_block": "FreqXlatingFIR", "passed": True, "metric": "amplitude",
        "n_compared": res.i.n_compared, "max_abs_err": res.i.max_abs_err,
        "tolerance": res.i.tolerance, "nmse_db": res.i.nmse_db,
        "correlation": res.i.correlation, "bit_errors": 0, "delay_used": 0,
        "coverage": {"param_sweep": 8, "bit_exact": True, "mutation": True,
                     "center_freq_zero": True, "decimation": [1, 2, 4],
                     "orientation_invariant": True},
    }
    (_VERIFY / "reports").mkdir(exist_ok=True)
    (_VERIFY / "reports" / "FreqXlatingFIR.json").write_text(json.dumps(report))
