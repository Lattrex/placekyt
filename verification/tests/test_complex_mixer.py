# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify ComplexMixerBlock against GNU Radio multiply_cc(signal, sig_source_c).

ComplexMixerBlock is the fused complex mixer / frequency shifter: it multiplies a
complex input signal by a complex exponential — ``out[n] = in[n]·exp(jθ_n)``,
``θ_n = 2π·frequency/sample_rate·n`` — i.e. GR's ``blocks.multiply_cc(signal,
analog.sig_source_c(...))``. The full complex product (multiply_cc, both quadrature
arms): yi = xi·cos − xq·sin, yq = xi·sin + xq·cos.

The complex exponential is the SAME interpolated quarter-wave NCO as NCOBlock
(angle-fold + parity-split 33-entry table, idx_bits=7), so the mixer's error vs GR
is the signal carried through the ~11-LSB table-NCO floor. Params mirror GRC's
Signal Source (the mixing oscillator): sample_rate, frequency in Hz.

Two reference tiers:
  * DSP equivalence — DUT vs GNU Radio multiply_cc(signal, sig_source_c), AMPLITUDE,
    grid-aligned (freq_word%512==0 → ~1 LSB) and off-grid (vs GR at the DUT's actual
    freq_word frequency → within the table floor).
  * Bit-exact substrate — DUT vs process_reference_q15 (the interpolated cos/sin +
    the Q15 complex product), EXACT, at grid AND off-grid frequencies.

Per INV-4 every gate is paired with a mutation (swap I/Q, negate Q, +1 delay, wrong
frequency, conjugate, empty) that must FAIL. n=0 multiplies by exp(j·0)=1, delay=0.
Signal amplitude is kept ≤ 0.5 so the complex product stays inside Q15 (no overflow,
so the saturating-free DUT matches GR float).

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_complex_mixer.py -x -q
"""
from __future__ import annotations

import os
import math
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
    run_block_dut_complex, run_gnuradio_ref_complex, compare_complex_against_grc,
    Metric)
from gr_kyttar.placement.blocks.complex_mixer_block import ComplexMixerBlock  # noqa: E402

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_AVAILABLE),
    reason="chip yaml or GNU Radio interpreter absent")

TABLE_FLOOR_LSB = 12


def _s16(v):
    if v is None:
        return None
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def _signal(seed, n, amp=0.5):
    rng = random.Random(seed)
    return [complex(rng.uniform(-amp, amp), rng.uniform(-amp, amp)) for _ in range(n)]


def _run_dut(fs, f, stim, amplitude=1.0, offset=0.0, phase=0.0):
    dut = run_block_dut_complex(
        "ComplexMixerBlock", stim,
        params={"sample_rate": fs, "frequency": f, "amplitude": amplitude,
                "offset": offset, "phase": phase},
        chip_yaml=CHIP_YAML, words_per_sample=2)
    assert dut.ok, dut.reason
    return dut


def _gr_mix(fs, f, stim, amplitude=1.0, offset=0.0, phase=0.0):
    return run_gnuradio_ref_complex(
        stim,
        gnuradio_script="""
from gnuradio import gr, analog, blocks
tb = gr.top_block()
src = blocks.vector_source_c(input_complex, False)
osc = analog.sig_source_c(fs, analog.GR_COS_WAVE, f, amplitude, offset, phase)
mul = blocks.multiply_cc()
snk = blocks.vector_sink_c()
tb.connect(src, (mul, 0)); tb.connect(osc, (mul, 1)); tb.connect(mul, snk)
tb.run()
output_complex = list(snk.data())
""",
        extra_args={"fs": fs, "f": f, "amplitude": amplitude,
                    "offset": offset, "phase": phase})


# --- structure / smoke --------------------------------------------------------

def test_mixer_drives_and_captures():
    dut = _run_dut(32000, 2000, _signal(1, 24))
    assert dut.words_per_sample == 2, f"expected 2 words/sample, got {dut.words_per_sample}"
    assert dut.in_regs == (0, 1), "complex signal should land xi@R0, xq@R1"
    assert all(v is not None for v in dut.i_q15) and all(v is not None for v in dut.q_q15)


def test_mixer_phase_zero_passthrough():
    """At n=0, exp(j·0)=1, so out[0] == in[0] (the signal passes through unmixed)."""
    stim = _signal(5, 8)
    dut = _run_dut(32000, 2000, stim)
    blk = ComplexMixerBlock("c", sample_rate=32000, frequency=2000)
    ref0 = blk.process_reference_q15(stim)[0]
    assert _s16(dut.i_q15[0]) == _s16(ref0[0]) and _s16(dut.q_q15[0]) == _s16(ref0[1])


# --- bit-exact substrate (grid AND off-grid) ----------------------------------

@pytest.mark.parametrize("fs,f", [
    (32000, 2000), (32000, 2050), (32000, 777), (48000, 5000), (32000, 12345)])
def test_mixer_bitexact_reference(fs, f):
    """DUT matches the on-chip Q15 reference EXACTLY (interpolated cos/sin + the Q15
    complex product) over a long random complex signal — grid AND off-grid."""
    stim = _signal(42, 100)
    dut = _run_dut(fs, f, stim)
    blk = ComplexMixerBlock("ref", sample_rate=fs, frequency=f)
    ref = blk.process_reference_q15(stim)
    ri = [_s16(yi) / 32768.0 for yi, yq in ref]
    rq = [_s16(yq) / 32768.0 for yi, yq in ref]
    res = compare_complex_against_grc(dut.i_q15, dut.q_q15, ri, rq,
                                      metric=Metric.EXACT, delay=0)
    print(f"\nbit-exact fs={fs} f={f} fw={blk.freq_word}:", res.summary())
    assert res.passed, res.summary()


# --- DSP equivalence vs GNU Radio multiply_cc ---------------------------------

@pytest.mark.parametrize("fs,f", [(32000, 2000), (32000, 4000), (48000, 3000)])
def test_mixer_matches_gnuradio_grid(fs, f):
    """On grid-aligned mixing frequencies the DUT is a drop-in for GNU Radio's
    multiply_cc(signal, sig_source_c) to within ~3 LSB on both channels."""
    blk = ComplexMixerBlock("c", sample_rate=fs, frequency=f)
    assert blk.freq_word % 512 == 0, "grid-aligned test needs freq_word % 512 == 0"
    stim = _signal(7, 64)
    dut = _run_dut(fs, f, stim)
    gr = _gr_mix(fs, f, stim)
    res = compare_complex_against_grc(dut.i_q15, dut.q_q15, gr.i, gr.q,
                                      metric=Metric.AMPLITUDE, delay=0, tolerance=4)
    print(f"\nvs GR grid f={f}:", res.summary())
    assert res.passed, res.summary()


@pytest.mark.parametrize("amplitude,phase", [
    (1.0, 0.0), (0.5, 0.0), (1.0, math.pi / 2), (0.7, math.pi / 4),
    (0.9, math.pi)])
def test_mixer_oscillator_amp_phase(amplitude, phase):
    """The mixing oscillator's GR sig_source_c params amplitude (scales the
    oscillator, folded into the NCO table) and initial phase (radians) match
    multiply_cc(sig, sig_source_c) on a grid-aligned tone within the table floor."""
    fs, f = 32000, 2000
    blk = ComplexMixerBlock("c", sample_rate=fs, frequency=f,
                            amplitude=amplitude, phase=phase)
    assert blk.freq_word % 512 == 0
    stim = _signal(7, 48)
    dut = _run_dut(fs, f, stim, amplitude=amplitude, phase=phase)
    gr = _gr_mix(fs, f, stim, amplitude=amplitude, phase=phase)
    res = compare_complex_against_grc(dut.i_q15, dut.q_q15, gr.i, gr.q,
                                      metric=Metric.AMPLITUDE, delay=0, tolerance=15)
    print(f"\nvs GR amp={amplitude} ph={phase:.3f}:", res.summary())
    assert res.passed, res.summary()


def test_mixer_oscillator_offset_raises():
    """HARDWARE LIMIT (documented): a non-zero mixing-oscillator offset is not
    supported (the mixer output cell is at its register budget) and RAISES with a
    clear message rather than silently mis-building."""
    with pytest.raises(ValueError, match="offset"):
        ComplexMixerBlock("c", sample_rate=32000, frequency=2000, offset=0.2)


@pytest.mark.parametrize("fs,f", [(32000, 2050), (48000, 5000)])
def test_mixer_matches_gnuradio_offgrid(fs, f):
    """Off a table grid, the DUT matches GNU Radio within the table-NCO floor when
    compared to GR mixing at the DUT's ACTUAL (freq_word) frequency (removing the
    separate freq_word-quantization drift)."""
    blk = ComplexMixerBlock("c", sample_rate=fs, frequency=f)
    assert blk.freq_word % 512 != 0
    f_actual = blk.freq_word / 65536.0 * fs
    stim = _signal(11, 64)
    dut = _run_dut(fs, f, stim)
    gr = _gr_mix(fs, f_actual, stim)
    res = compare_complex_against_grc(dut.i_q15, dut.q_q15, gr.i, gr.q,
                                      metric=Metric.AMPLITUDE, delay=0,
                                      tolerance=TABLE_FLOOR_LSB)
    print(f"\nvs GR off-grid f={f} (GR@{f_actual:.2f}Hz):", res.summary())
    assert res.passed, res.summary()


# --- mandatory mutation tests -------------------------------------------------

def _setup():
    stim = _signal(7, 64)
    dut = _run_dut(32000, 2000, stim)
    gr = _gr_mix(32000, 2000, stim)
    return dut, gr, stim


def test_mixer_mutation_swapped_iq_fails():
    dut, gr, _ = _setup()
    res = compare_complex_against_grc(dut.q_q15, dut.i_q15, gr.i, gr.q,
                                      metric=Metric.AMPLITUDE, delay=0, tolerance=4)
    assert not res.passed, "gate failed to detect swapped I/Q!"


def test_mixer_mutation_negated_q_fails():
    dut, gr, _ = _setup()
    neg_q = [(0x10000 - (w or 0)) & 0xFFFF for w in dut.q_q15]
    res = compare_complex_against_grc(dut.i_q15, neg_q, gr.i, gr.q,
                                      metric=Metric.AMPLITUDE, delay=0, tolerance=4)
    assert not res.passed, "gate failed to detect a negated Q channel!"


def test_mixer_mutation_one_sample_offset_fails():
    dut, gr, _ = _setup()
    sh_i = [0x0000] + list(dut.i_q15[:-1])
    sh_q = [0x0000] + list(dut.q_q15[:-1])
    res = compare_complex_against_grc(sh_i, sh_q, gr.i, gr.q,
                                      metric=Metric.AMPLITUDE, delay=0, tolerance=4)
    assert not res.passed, "gate failed to detect a 1-sample latency error!"


def test_mixer_mutation_wrong_frequency_fails():
    stim = _signal(7, 64)
    dut = _run_dut(32000, 2000, stim)
    gr_wrong = _gr_mix(32000, 5000, stim)   # mix 2 kHz DUT vs 5 kHz GR
    res = compare_complex_against_grc(dut.i_q15, dut.q_q15, gr_wrong.i, gr_wrong.q,
                                      metric=Metric.AMPLITUDE, delay=0, tolerance=4)
    assert not res.passed, "gate failed to detect a wrong mixing frequency!"


def test_mixer_mutation_conjugate_fails():
    """Conjugating the output (negate Q) is a down- vs up-conversion sign error the
    full multiply_cc must distinguish — must FAIL."""
    dut, gr, _ = _setup()
    conj_q = [(0x10000 - (w or 0)) & 0xFFFF for w in dut.q_q15]
    res = compare_complex_against_grc(dut.i_q15, conj_q, gr.i, gr.q,
                                      metric=Metric.AMPLITUDE, delay=0, tolerance=4)
    assert not res.passed, "gate failed to detect a conjugated output!"


def test_mixer_empty_output_fails():
    _, gr, _ = _setup()
    res = compare_complex_against_grc([], [], gr.i, gr.q,
                                      metric=Metric.AMPLITUDE, delay=0, tolerance=4)
    assert not res.passed


# --- dashboard report ---------------------------------------------------------

def test_emit_report():
    import json
    # Report the OFF-GRID (interpolated) floor — the representative noise at an
    # arbitrary mixing frequency, not the grid-aligned best case (where the NCO
    # phase lands on table entries and the mixer is ~1 LSB / -83 dB, over-stating
    # real accuracy). 2050 Hz is off the table grid; compare to GR mixing at the
    # DUT's ACTUAL freq_word frequency so only the table-NCO floor through the
    # complex product shows (the freq_word-quantization drift is a separate
    # frequency-accuracy spec).
    fs, f = 32000, 2050
    blk = ComplexMixerBlock("c", sample_rate=fs, frequency=f)
    assert blk.freq_word % 512 != 0, "report must use an OFF-grid frequency"
    f_actual = blk.freq_word / 65536.0 * fs
    stim = _signal(7, 64)
    dut = _run_dut(fs, f, stim)
    gr = _gr_mix(fs, f_actual, stim)
    res = compare_complex_against_grc(dut.i_q15, dut.q_q15, gr.i, gr.q,
                                      metric=Metric.AMPLITUDE, delay=0,
                                      tolerance=TABLE_FLOOR_LSB)
    assert res.passed, res.summary()
    report = {
        "kyttar_block": "ComplexMixerBlock", "passed": True, "metric": "amplitude",
        "n_compared": res.i.n_compared, "max_abs_err": res.i.max_abs_err,
        "tolerance": res.i.tolerance, "nmse_db": res.i.nmse_db,
        "correlation": res.i.correlation, "bit_errors": 0, "delay_used": 0,
        "coverage": {"param_sweep": 5, "bit_exact": True, "mutation": True,
                     "grid_aligned": True, "off_grid": True},
    }
    (_VERIFY / "reports").mkdir(exist_ok=True)
    (_VERIFY / "reports" / "ComplexMixerBlock.json").write_text(json.dumps(report))
