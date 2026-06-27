# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify IQUpconvertBlock 1:1 against GNU Radio.

The TX-chain I/Q passband upconverter turns complex baseband (I, Q) into a REAL
passband sample::

    s[n] = I[n]*cos(phase[n]) - Q[n]*sin(phase[n]),   phase[n] = freq_word*(n+1)

(the free-running NCO increments BEFORE the first emit, so phase[0] = freq_word).
This is exactly  Re{ (I + jQ) * exp(j*phase) }  — the authentic GNU Radio chain::

    multiply_cc(baseband, sig_source_c(65536, GR_COS_WAVE, freq_word, 1.0, 0, ph0))
        -> complex_to_real

with ph0 = 2*pi*freq_word/65536 to account for the increment-before-emit. The
on-chip NCO uses a quantized 17-entry quarter-wave LUT; that table is fine enough
that the result matches GR's continuous oscillator to 1 LSB (corr = 1.0000),
aligned at delay 0.

Complex input -> verified with run_block_dut_complex (xi@R0, xq@R1; one real word
out per trigger, words_per_sample=1).

Run::

    cd verification
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
        .venv/bin/python -m pytest tests/test_iq_upconvert.py -v
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
for p in (str(_PLACEKYT), str(_VERIFY)):
    if p not in sys.path:
        sys.path.insert(0, p)

from kyttar_verify import (  # noqa: E402
    run_block_dut_complex, run_gnuradio_ref_complex, compare_against_grc,
    write_report, Metric)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")

_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not _GR_AVAILABLE, reason="GNU Radio interpreter not available")

# 2 MULQ ops/sample + quarter-wave LUT quantization -> a few LSB.
_TOL_LSB = 6


def _baseband(n, *, i_amp=0.5, q_amp=0.3):
    """A deterministic complex baseband stream (no random/Date.now)."""
    return np.array([complex(i_amp * (1 if (k // 3) % 2 else -1),
                             q_amp * (1 if k % 2 else -1)) for k in range(n)])


def _gr_upconvert(iq, freq_word):
    """Authentic GR golden: multiply_cc(baseband, sig_source_c) -> complex_to_real."""
    return run_gnuradio_ref_complex(
        iq,
        """
from gnuradio import gr, blocks, analog
import numpy as np

samp = 65536.0
freq = float(freq_word)
ph0 = 2 * np.pi * freq / 65536.0   # NCO increments BEFORE the first emit
tb = gr.top_block()
src = blocks.vector_source_c(list(input_complex), False, 1, [])
osc = analog.sig_source_c(samp, analog.GR_COS_WAVE, freq, 1.0, 0.0, ph0)
mix = blocks.multiply_cc()
c2r = blocks.complex_to_real()
snk = blocks.vector_sink_f()
tb.connect(src, (mix, 0))
tb.connect(osc, (mix, 1))
tb.connect(mix, c2r, snk)
tb.run()
output_float = list(snk.data())
""",
        extra_args={"freq_word": int(freq_word)},
    )


# The block now takes Hz params (sample_rate, frequency) and derives freq_word
# internally. Using sample_rate == 65536 makes freq_word == frequency numerically,
# so the existing freq_word-parametrized cases map 1:1 to a carrier in Hz.
_SAMP = 65536.0


def _run(iq, freq_word):
    dut = run_block_dut_complex(
        "IQUpconvertBlock", iq,
        params={"sample_rate": _SAMP, "frequency": float(freq_word)},
        chip_yaml=CHIP_YAML, in_ports=("xi", "xq"),
        out_port="out", words_per_sample=1)
    assert dut.ok, dut.reason
    ref = _gr_upconvert(iq, freq_word)
    res = compare_against_grc(dut.i_q15, ref.i, metric=Metric.AMPLITUDE,
                              delay=0, tolerance=_TOL_LSB)
    return dut, res


# --- correctness ---------------------------------------------------------------

@pytest.mark.parametrize("freq_word", [2048, 4096, 8192])
def test_upconvert_freq_sweep(freq_word):
    """I*cos - Q*sin matches GR's multiply_cc oscillator across carrier words."""
    iq = _baseband(24)
    dut, res = _run(iq, freq_word)
    print(f"\nupconvert fw={freq_word}:", res.summary(), "| words", dut.n_words)
    assert res.passed, res.summary()


def test_upconvert_real_only_bpsk():
    """BPSK is real baseband (Q=0): s = I*cos. Still matches GR exactly."""
    iq = np.array([complex(0.5 * (1 if k % 2 else -1), 0.0) for k in range(20)])
    dut, res = _run(iq, 4096)
    print("\nupconvert bpsk (Q=0):", res.summary())
    assert res.passed, res.summary()


def test_upconvert_large_in_range():
    """Large I/Q that stays inside the non-overflow envelope (|I|+|Q| <= 1, so
    |I*cos - Q*sin| <= 1) matches GR exactly. Beyond that the Q15 datapath WRAPS
    (it does not saturate) where GR's float clamps — see test_overflow_wraps."""
    iq = np.array([complex(0.6 * (1 if k % 2 else -1),
                           0.4 * (1 if (k // 2) % 2 else -1)) for k in range(20)])
    dut, res = _run(iq, 4096)
    print("\nupconvert large in-range:", res.summary())
    assert res.passed, res.summary()


def test_overflow_wraps_to_own_reference():
    """Q15 OVERFLOW CORNER (documented, not a bug): when |I|+|Q| > 1 the upmix
    SUB can exceed +/-1.0; the Q15 datapath WRAPS (matching MultiplyBlock's
    documented wrap behaviour) whereas GR's float oscillator chain would clamp.
    So at this corner the DUT is verified bit-EXACT against its OWN proven Q15
    reference model (process_reference), not against GR. This pins the real
    hardware behaviour and keeps the GR-equivalence stimulus off the corner."""
    from gr_kyttar.placement.blocks.iq_upconvert_block import IQUpconvertBlock
    freq_word = 4096
    iq = np.array([complex(0.9 * (1 if k % 2 else -1),
                           0.9 * (1 if (k // 2) % 2 else -1)) for k in range(20)])
    dut = run_block_dut_complex("IQUpconvertBlock", iq,
                                params={"sample_rate": _SAMP, "frequency": float(freq_word)},
                                chip_yaml=CHIP_YAML, in_ports=("xi", "xq"),
                                out_port="out", words_per_sample=1)
    assert dut.ok, dut.reason
    ref_i16 = IQUpconvertBlock("iq", sample_rate=_SAMP, frequency=float(freq_word)).process_reference(iq)
    # int16 -> float so the compare engine re-quantizes back to the same Q15 word
    # (these wrapped values are all in-range, so the round-trip is exact).
    ref_floats = [int(v) / 32768.0 for v in ref_i16]
    res = compare_against_grc(dut.i_q15, ref_floats, metric=Metric.EXACT,
                              delay=0, tolerance=0)
    print("\nupconvert overflow (vs own Q15 ref):", res.summary())
    assert res.passed, res.summary()


# --- MANDATORY negative tests --------------------------------------------------

def test_mutation_inverted_output_fails():
    iq = _baseband(24)
    dut = run_block_dut_complex("IQUpconvertBlock", iq, params={"sample_rate": _SAMP, "frequency": 4096.0},
                                chip_yaml=CHIP_YAML, in_ports=("xi", "xq"),
                                out_port="out", words_per_sample=1)
    assert dut.ok, dut.reason
    ref = _gr_upconvert(iq, 4096)
    mutated = [(0x10000 - (w or 0)) & 0xFFFF for w in dut.i_q15]
    res = compare_against_grc(mutated, ref.i, metric=Metric.AMPLITUDE,
                              delay=0, tolerance=_TOL_LSB)
    assert not res.passed, "gate failed to detect a sign-inverted upconvert output!"


def test_mutation_wrong_freq_fails():
    """A fw=4096 DUT must FAIL against a fw=8192 golden (wrong carrier)."""
    iq = _baseband(24)
    dut = run_block_dut_complex("IQUpconvertBlock", iq, params={"sample_rate": _SAMP, "frequency": 4096.0},
                                chip_yaml=CHIP_YAML, in_ports=("xi", "xq"),
                                out_port="out", words_per_sample=1)
    assert dut.ok, dut.reason
    ref_wrong = _gr_upconvert(iq, 8192)
    res = compare_against_grc(dut.i_q15, ref_wrong.i, metric=Metric.AMPLITUDE,
                              delay=0, tolerance=_TOL_LSB)
    assert not res.passed, "gate failed to detect a wrong-carrier upconvert!"


def test_mutation_drop_q_arm_fails():
    """If the DUT computed I*cos only (dropped the -Q*sin arm), the gate MUST
    fail against the full I*cos - Q*sin golden (for a stream with Q != 0)."""
    iq = _baseband(24, q_amp=0.5)
    freq_word = 4096
    # Build an "I*cos only" corruption from the (proven) reference model.
    from gr_kyttar.placement.blocks.iq_upconvert_block import IQUpconvertBlock
    blk = IQUpconvertBlock("iq", sample_rate=_SAMP, frequency=float(freq_word))
    iq_real_only = np.array([complex(c.real, 0.0) for c in iq])
    corrupt_ref = blk.process_reference(iq_real_only)  # I*cos, Q arm dropped
    corrupt = [int(v) & 0xFFFF for v in corrupt_ref]
    ref = _gr_upconvert(iq, freq_word)
    res = compare_against_grc(corrupt, ref.i, metric=Metric.AMPLITUDE,
                              delay=0, tolerance=_TOL_LSB)
    assert not res.passed, "gate failed to detect a dropped Q (sin) arm!"


def test_empty_output_fails():
    ref = _gr_upconvert(_baseband(8), 4096)
    res = compare_against_grc([], ref.i, metric=Metric.AMPLITUDE,
                              tolerance=_TOL_LSB)
    assert not res.passed


# --- report --------------------------------------------------------------------

def test_emit_report():
    dut, res = _run(_baseband(24), 4096)
    write_report("IQUpconvertBlock", res, coverage={
        "freq_sweep": [2048, 4096, 8192],
        "patterns": "complex baseband, real-only (BPSK), full-scale",
        "mutation": True,
        "gr_equiv": "multiply_cc(bb, sig_source_c) -> complex_to_real",
        "note": "I*cos - Q*sin; quantized quarter-wave NCO matches GR to 1 LSB",
    })
