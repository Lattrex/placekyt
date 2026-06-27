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
    run_block_dut, run_block_dut_complex, run_gnuradio_ref,
    compare_llr_against_grc, compare_against_grc, write_report, Metric)
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


# --- constellation parity: generic constellation_soft_decoder_cf --------------
# The block now mirrors the GRC params (constellation, npwr). The float golden is
# validated against GR's exact soft decision; on-chip it computes the separable
# max-log per-axis LLR for BPSK/QPSK (HW limit). QPSK = complex in, 2 LLRs/symbol.

def _gr_soft(con_make, zs):
    """GR golden for an arbitrary constellation, flat LLRs (bits/sym per sample)."""
    import json
    import subprocess
    script = f"""
from gnuradio import gr, blocks, digital
import json, sys
zs = [complex(a, b) for a, b in json.loads(sys.stdin.read())]
con = {con_make}
tb = gr.top_block(); src = blocks.vector_source_c(zs, False)
dec = digital.constellation_soft_decoder_cf(con); snk = blocks.vector_sink_f()
tb.connect(src, dec); tb.connect(dec, snk); tb.run()
print(json.dumps(list(snk.data())))
"""
    gr_py = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")
    r = subprocess.run([gr_py, "-c", script],
                       input=json.dumps([[z.real, z.imag] for z in zs]),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-500:]
    return json.loads(r.stdout.strip().splitlines()[-1])


import json  # noqa: E402


def test_softdemod_float_golden_matches_gr_bpsk_and_qpsk():
    """The block's calc_soft_dec_float reproduces GR's EXACT soft decision (full
    log-sum-exp) for BPSK and QPSK to <1e-3 — the golden predictor itself is
    GR-faithful (sign + magnitude), so the on-chip max-log can be checked against
    it. Proves the constellation param drives the real soft-decision math."""
    import numpy as np
    rng = random.Random(13)
    zs = [complex(rng.uniform(-1, 1), rng.uniform(-1, 1)) for _ in range(20)]
    for name, con in (("bpsk", "digital.constellation_bpsk().base()"),
                      ("qpsk", "digital.constellation_qpsk().base()")):
        blk = SoftDemodulatorBlock("g", constellation=name, npwr=-1.0)
        mine = []
        for z in zs:
            mine += blk.calc_soft_dec_float(z)
        gr = _gr_soft(con, zs)
        err = max(abs(a - b) for a, b in zip(mine, gr))
        print(f"\n{name} float golden vs GR: max err {err:.5f}")
        assert err < 1e-3, f"{name} golden diverges from GR by {err}"


def _qpsk_stim(seed, n=16):
    """QPSK I/Q away from the axes so both per-axis hard decisions are unambiguous."""
    import numpy as np
    rng = random.Random(seed)
    return np.array([complex(rng.choice([-1, 1]) * rng.uniform(0.3, 0.85),
                             rng.choice([-1, 1]) * rng.uniform(0.3, 0.85))
                     for _ in range(n)])


def test_softdemod_qpsk_on_chip_signs_match_gr():
    """The QPSK soft demapper (complex in, 2 LLRs/symbol MSB-first) agrees with
    GR's constellation_soft_decoder_cf on EVERY hard decision and matches the soft
    magnitude after a single fixed LLR-scale alignment."""
    import numpy as np
    iq = _qpsk_stim(7)
    dut = run_block_dut_complex(
        "SoftDemodulatorBlock", iq, params={"constellation": "qpsk"},
        chip_yaml=CHIP_YAML, in_ports=("i_in", "q_in"),
        out_port="llr", words_per_sample=2)
    assert dut.ok, dut.reason
    gr = _gr_soft("digital.constellation_qpsk().base()", iq)
    got = []
    for g in dut.outputs_q15:
        got += [_s16(g[0]) / 32768.0, _s16(g[1]) / 32768.0]
    sign_mismatch = sum(1 for a, b in zip(got, gr) if (a > 0) != (b > 0))
    g = np.array(got)
    G = np.array(gr)
    scale = float(np.dot(g, G) / np.dot(g, g))
    mag_err = float(np.max(np.abs(scale * g - G)))
    print(f"\nqpsk on-chip vs GR: sign mismatches {sign_mismatch}, "
          f"mag err {mag_err:.4f} (scale {scale:.3f})")
    assert sign_mismatch == 0, "QPSK hard decisions must match GR exactly"
    assert mag_err < 0.05, f"QPSK soft magnitude diverges: {mag_err}"


def test_softdemod_qpsk_bitexact_reference():
    """QPSK on-chip matches its separable-max-log Q15 reference EXACTLY."""
    import numpy as np
    rng = random.Random(21)
    iq = np.array([complex(rng.uniform(-0.8, 0.8), rng.uniform(-0.8, 0.8))
                   for _ in range(20)])
    dut = run_block_dut_complex(
        "SoftDemodulatorBlock", iq, params={"constellation": "qpsk"},
        chip_yaml=CHIP_YAML, in_ports=("i_in", "q_in"),
        out_port="llr", words_per_sample=2)
    assert dut.ok, dut.reason
    blk = SoftDemodulatorBlock("r", constellation="qpsk")

    def q15(f):
        return max(-32768, min(32767, int(round(f * 32768)))) & 0xFFFF
    flat = []
    for z in iq:
        flat += [q15(z.real), q15(z.imag)]
    ref = blk.process_reference_q15(flat)
    got = []
    for g in dut.outputs_q15:
        got += [_s16(g[0]), _s16(g[1])]
    assert got == [_s16(r) for r in ref], "QPSK on-chip != Q15 reference"


def test_softdemod_npwr_default_is_one():
    """npwr=-1 (GRC default) resolves to GR's stored d_npwr=1.0 (NOT a derived
    noise estimate). Proven by the BPSK golden: GR emits 4*I at npwr=1."""
    blk = SoftDemodulatorBlock("n", constellation="bpsk", npwr=-1.0)
    assert blk.npwr == 1.0
    # calc_soft_dec for BPSK at npwr=1 is exactly 4*I.
    assert abs(blk.calc_soft_dec_float(complex(0.3, 0.0))[0] - 1.2) < 1e-6


def test_softdemod_nonseparable_constellation_raises():
    """A non-separable / non-Gray constellation (e.g. 8-PSK ring) is the
    documented HARDWARE LIMIT and MUST raise loudly (not silently mis-build)."""
    import math
    pts = [complex(math.cos(2 * math.pi * k / 8),
                   math.sin(2 * math.pi * k / 8)) for k in range(8)]
    with pytest.raises(ValueError, match="HARDWARE LIMIT"):
        SoftDemodulatorBlock("x", constellation=(pts, list(range(8))))


def test_softdemod_unknown_constellation_raises():
    with pytest.raises(ValueError):
        SoftDemodulatorBlock("x", constellation="qam16")


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
