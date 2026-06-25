# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify SoftDemodulatorBlock against GNU Radio's BPSK soft decoder.

SoftDemodulatorBlock is the BPSK soft-decision demapper: it turns a (carrier-
recovered, real-axis) I sample into a Log-Likelihood Ratio (LLR) for a soft-input
FEC decoder. On chip it is a single ``MULQ`` — ``LLR = coeff · I`` — where the
coefficient ``coeff = min(0.5, 2/σ²·llr_scale)`` maps the theoretical BPSK LLR
``2I/σ²`` into the Q15-representable range (saturating at the production scale 0.5
for any realistic noise variance σ² ≤ 4).

The golden is GNU Radio's ``digital.constellation_soft_decoder_cf`` on a
``constellation_bpsk()``, which emits ``LLR = 4·I``. The decision-relevant property
is the SIGN (the hard bit the decoder acts on) — which must agree EXACTLY — plus
the soft MAGNITUDE within a derived Q15 tolerance once the two LLR scales are
aligned: ``llr_scale = coeff / 4`` (e.g. 0.5/4 = 0.125 at the production scale).
This uses the LLR-aware comparator (``compare_llr_against_grc``); the same metric
the complex/LLR harness was proven on (see test_complex_harness.py).

Per INV-4 every gate is paired with a mutation (flipped sign — the wrong bit;
halved magnitude; +1 delay; empty) that must FAIL. A memoryless demod has delay 0.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_soft_demodulator.py -x -q
"""
from __future__ import annotations

import os
import random
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PLACEKYT = Path(__file__).resolve().parents[2] / "placekyt"
_VERIFY = Path(__file__).resolve().parents[1]
_RUNTIME = Path(__file__).resolve().parents[2] / "runtime" / "python"
for p in (str(_PLACEKYT), str(_VERIFY), str(_RUNTIME)):
    if p not in sys.path:
        sys.path.insert(0, p)

from kyttar_verify import (  # noqa: E402
    run_block_dut, run_gnuradio_ref, compare_llr_against_grc, compare_against_grc,
    write_report, Metric)
from gr_kyttar.placement.blocks.soft_demodulator_block import (  # noqa: E402
    SoftDemodulatorBlock)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_AVAILABLE),
    reason="chip yaml or GNU Radio interpreter absent")

_GR_BPSK_SCALE = 4.0  # GNU Radio BPSK constellation_soft_decoder emits LLR = 4*I


def _s16(v):
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def _gr_bpsk_llr(inputs_q15):
    """GNU Radio BPSK soft-decision LLR for an I stimulus (real, on the I axis)."""
    return run_gnuradio_ref(
        input_q15=inputs_q15,
        gnuradio_script="""
from gnuradio import gr, blocks, digital
con = digital.constellation_bpsk().base()
tb = gr.top_block()
src = blocks.vector_source_c([complex(v, 0.0) for v in input_float], False)
dec = digital.constellation_soft_decoder_cf(con)
sink = blocks.vector_sink_f()
tb.connect(src, dec); tb.connect(dec, sink)
tb.run()
output_float = list(sink.data())
""")


def _llr_scale(block):
    """Map GR's 4*I LLR to the block's coeff*I scale: llr_scale = coeff / 4."""
    return (block.llr_coeff_q15 / 32768.0) / _GR_BPSK_SCALE


def _llr_stim(seed, n=32):
    """BPSK-ish I samples spread across both signs, away from the |I|~0 boundary
    so the hard decision is unambiguous (the comparator's dead zone)."""
    rng = random.Random(seed)
    return [int(round(rng.choice([1, -1]) * rng.uniform(0.2, 0.9) * 32768)) & 0xFFFF
            for _ in range(n)]


def _run_dut(inputs_q15, noise_variance=0.1):
    dut = run_block_dut("SoftDemodulatorBlock", inputs_q15,
                        params={"noise_variance": noise_variance}, chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    return dut


# --- the match: sign (exact) + magnitude (derived) ----------------------------

def test_softdemod_matches_gnuradio():
    """The DUT agrees with GNU Radio's BPSK soft decoder on the HARD DECISION
    (sign) for every confident sample, and its soft magnitude is within the
    derived Q15 floor after the LLR scale is applied."""
    stim = _llr_stim(seed=3)
    blk = SoftDemodulatorBlock("c", noise_variance=0.1)
    dut = _run_dut(stim)
    gr = _gr_bpsk_llr(stim)
    res = compare_llr_against_grc(
        dut.outputs_q15, gr.floats, delay=0, llr_scale=_llr_scale(blk), op_count=1)
    print("\nsoftdemod vs GR:", res.summary())
    assert res.passed, res.summary()
    assert res.sign_mismatches == 0, "hard decisions must match GR exactly"


@pytest.mark.parametrize("seed", [11, 23, 31, 47])
def test_softdemod_random_sign_agreement(seed):
    stim = _llr_stim(seed=seed)
    blk = SoftDemodulatorBlock("c", noise_variance=0.1)
    dut = _run_dut(stim)
    gr = _gr_bpsk_llr(stim)
    res = compare_llr_against_grc(
        dut.outputs_q15, gr.floats, delay=0, llr_scale=_llr_scale(blk), op_count=1)
    print(f"\nsoftdemod seed={seed}:", res.summary())
    assert res.passed, res.summary()


def test_softdemod_noise_variance_tracks_then_saturates():
    """The noise_variance parameter genuinely sets the LLR coefficient
    (coeff = min(0.5, 2/σ²)): it SATURATES at the production scale 0.5 for any
    realistic σ² ≤ 4, and SCALES DOWN for very high noise. Either way the DUT
    still matches GR's BPSK soft decoder on sign + (rescaled) magnitude — proving
    the param is real, not cosmetic."""
    stim = _llr_stim(seed=5)
    gr = _gr_bpsk_llr(stim)
    # production regime: coeff pinned at 0.5
    for nv in (0.05, 0.1, 1.0, 4.0):
        blk = SoftDemodulatorBlock("c", noise_variance=nv)
        assert blk.llr_coeff_q15 == 16384, f"σ²={nv} should saturate coeff at 0.5"
        dut = _run_dut(stim, noise_variance=nv)
        res = compare_llr_against_grc(dut.outputs_q15, gr.floats, delay=0,
                                      llr_scale=_llr_scale(blk), op_count=1)
        assert res.passed and res.sign_mismatches == 0, f"σ²={nv}: {res.summary()}"
    # high-noise regime: coeff = 2/σ² < 0.5 (σ²=10 → 0.2), a genuinely different scale
    blk = SoftDemodulatorBlock("c", noise_variance=10.0)
    assert blk.llr_coeff_q15 < 16384, "high σ² should reduce the LLR coefficient"
    dut = _run_dut(stim, noise_variance=10.0)
    res = compare_llr_against_grc(dut.outputs_q15, gr.floats, delay=0,
                                  llr_scale=_llr_scale(blk), op_count=1)
    assert res.passed and res.sign_mismatches == 0, f"σ²=10: {res.summary()}"


def test_softdemod_bitexact_reference():
    """The on-chip DUT matches the block's bit-exact Q15 reference EXACTLY (the
    single-MULQ datapath, LLR = (coeff·I)>>15) across a full-range stimulus."""
    rng = random.Random(99)
    stim = [rng.randint(0, 0xFFFF) for _ in range(40)]
    blk = SoftDemodulatorBlock("c", noise_variance=0.1)
    dut = _run_dut(stim)
    ref = [_s16(w) / 32768.0 for w in blk.process_reference_q15(stim)]
    res = compare_against_grc(dut.outputs_q15, ref, metric=Metric.EXACT, delay=0)
    print("\nsoftdemod bit-exact:", res.summary())
    assert res.passed, res.summary()


# --- mandatory mutation tests (the gate must DETECT these) ---------------------

def test_softdemod_mutation_flipped_sign_fails():
    """Flipping every LLR's sign is a wrong-bit corruption — the SIGN gate MUST
    fail (a magnitude-only gate would pass it, |LLR| unchanged)."""
    stim = _llr_stim(seed=3)
    blk = SoftDemodulatorBlock("c", noise_variance=0.1)
    dut = _run_dut(stim)
    gr = _gr_bpsk_llr(stim)
    flipped = [(0x10000 - (w or 0)) & 0xFFFF for w in dut.outputs_q15]
    res = compare_llr_against_grc(flipped, gr.floats, delay=0,
                                  llr_scale=_llr_scale(blk), op_count=1)
    assert not res.passed, "gate failed to detect flipped LLR signs (wrong bits)!"
    assert res.sign_mismatches > 0


def test_softdemod_mutation_halved_magnitude_fails():
    """Halving the soft magnitude (signs intact) must FAIL the MAGNITUDE half of
    the gate — proving the soft value is checked, not just the sign."""
    stim = _llr_stim(seed=3)
    blk = SoftDemodulatorBlock("c", noise_variance=0.1)
    dut = _run_dut(stim)
    gr = _gr_bpsk_llr(stim)
    halved = [(_s16(w) >> 1) & 0xFFFF for w in dut.outputs_q15]
    res = compare_llr_against_grc(halved, gr.floats, delay=0,
                                  llr_scale=_llr_scale(blk), op_count=1)
    assert not res.passed, "gate failed to detect a halved LLR magnitude!"


def test_softdemod_mutation_one_sample_offset_fails():
    stim = _llr_stim(seed=3)
    blk = SoftDemodulatorBlock("c", noise_variance=0.1)
    dut = _run_dut(stim)
    gr = _gr_bpsk_llr(stim)
    shifted = [0x0000] + list(dut.outputs_q15[:-1])
    res = compare_llr_against_grc(shifted, gr.floats, delay=0,
                                  llr_scale=_llr_scale(blk), op_count=1)
    assert not res.passed, "gate failed to detect a 1-sample LLR latency error!"


def test_softdemod_empty_output_fails():
    stim = _llr_stim(seed=3)
    blk = SoftDemodulatorBlock("c", noise_variance=0.1)
    gr = _gr_bpsk_llr(stim)
    res = compare_llr_against_grc([], gr.floats, delay=0, llr_scale=_llr_scale(blk))
    assert not res.passed


def test_emit_report():
    """Publish the dashboard quality number. The LLR comparator returns an
    ``LLRCompareResult`` (no ``metric`` field, so ``write_report`` doesn't apply);
    the report is written directly in the schema the dashboard reads, with the
    soft-magnitude error / tolerance as the quality and the sign-mismatch count as
    ``bit_errors`` (the decision-relevant figure for a soft demapper)."""
    import json
    stim = _llr_stim(seed=7)
    blk = SoftDemodulatorBlock("c", noise_variance=0.1)
    dut = _run_dut(stim)
    gr = _gr_bpsk_llr(stim)
    res = compare_llr_against_grc(
        dut.outputs_q15, gr.floats, delay=0, llr_scale=_llr_scale(blk), op_count=1)
    assert res.passed, res.summary()
    report = {
        "kyttar_block": "SoftDemodulatorBlock",
        "passed": bool(res.passed),
        "metric": "llr",
        "n_compared": res.n_compared,
        "max_abs_err": res.max_abs_err,
        "tolerance": res.tolerance,
        "nmse_db": None,
        "correlation": None,
        "bit_errors": res.sign_mismatches,
        "delay_used": 0,
        "coverage": {"random": 4, "param_sweep": 5, "bit_exact": True,
                     "mutation": True},
    }
    (_VERIFY / "reports").mkdir(exist_ok=True)
    (_VERIFY / "reports" / "SoftDemodulatorBlock.json").write_text(json.dumps(report))
