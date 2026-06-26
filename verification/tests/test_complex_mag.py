# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify ComplexToMagSquaredBlock against GNU Radio blocks.complex_to_mag_squared.

Instantaneous power |z|² = re² + im² — the envelope/power primitive behind energy
detectors, AGC error, and squelch. On chip: ``MULQ re,re`` + ``MACQ im,im`` in one
cell (two Q15 ops), with a SATURATING clamp (the power range [0,2) overflows Q15's
[0,1) for |z| ≥ 1). No params (full GRC parity).

Two reference tiers:
  * DSP equivalence — DUT vs GR complex_to_mag_squared, AMPLITUDE, on IN-RANGE
    stimulus (|z| < 1, result Q15-representable), two-op floor.
  * Bit-exact substrate — DUT vs process_reference_q15 (``(re²>>15)+(im²>>15)``
    saturating), EXACT, including overflow corners.

Saturation is verified directly (|z| ≥ 1 pins to +full-scale, no wrap). Per INV-4
every gate is paired with a mutation that must FAIL. NOTE |z|² = re²+im² is
SYMMETRIC in re/im, so a swapped-channel mutation is not a corruption (documented);
teeth come from a wrong-second-stream / inverted / halved / +1-delay mutation.
Memoryless → delay=0.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_complex_mag.py -x -q
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
    run_block_dut_complex, run_gnuradio_ref_complex, compare_against_grc,
    write_report, Metric)
from gr_kyttar.placement.blocks.complex_mag_block import (  # noqa: E402
    ComplexToMagSquaredBlock)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_AVAILABLE),
    reason="chip yaml or GNU Radio interpreter absent")

# Two Q15 ops (MULQ + MACQ) → derived floor.
OP_COUNT = 2


def _s16(v):
    if v is None:
        return None
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def _q15(v: float) -> int:
    q = int(round(v * 32768.0))
    return max(-32768, min(32767, q)) & 0xFFFF


# In-range (|z| < 1): |re|,|im| ≤ 0.65 → power ≤ 0.85.
EDGE = [complex(0.0, 0.0), complex(0.5, 0.0), complex(0.0, -0.6),
        complex(0.4, 0.4), complex(-0.65, 0.3), complex(0.6, -0.5),
        complex(-0.3, -0.7), complex(0.65, 0.65)]


def _random(seed, n=24, amp=0.65):
    rng = random.Random(seed)
    return [complex(rng.uniform(-amp, amp), rng.uniform(-amp, amp))
            for _ in range(n)]


def _run_dut(stim):
    dut = run_block_dut_complex(
        "ComplexToMagSquaredBlock", stim, chip_yaml=CHIP_YAML,
        in_ports=("re", "im"), words_per_sample=1)
    assert dut.ok, dut.reason
    return dut


def _gr(stim):
    return run_gnuradio_ref_complex(
        stim,
        gnuradio_script="""
from gnuradio import gr, blocks
tb = gr.top_block()
src = blocks.vector_source_c(input_complex, False)
m = blocks.complex_to_mag_squared()
snk = blocks.vector_sink_f()
tb.connect(src, m); tb.connect(m, snk)
tb.run()
output_float = list(snk.data())
""")


def _compare(dut, gr):
    return compare_against_grc(dut.i_q15, gr.i, metric=Metric.AMPLITUDE,
                               delay=0, op_count=OP_COUNT)


# --- structure ----------------------------------------------------------------

def test_drives_and_captures():
    dut = _run_dut(_random(1, 12))
    assert dut.words_per_sample == 1
    assert dut.in_regs == (0, 1)
    assert all(v is not None for v in dut.i_q15)


# --- DSP equivalence vs GNU Radio ---------------------------------------------

def test_edge_vectors():
    dut = _run_dut(EDGE)
    gr = _gr(EDGE)
    res = _compare(dut, gr)
    print("\nedge:", res.summary())
    assert res.passed, res.summary()


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_random_vectors(seed):
    stim = _random(seed)
    dut = _run_dut(stim)
    gr = _gr(stim)
    res = _compare(dut, gr)
    print(f"\nrandom seed={seed}:", res.summary())
    assert res.passed, res.summary()


@pytest.mark.parametrize("amp", [0.2, 0.4, 0.55, 0.65])
def test_amplitude_sweep(amp):
    stim = _random(99, n=24, amp=amp)
    dut = _run_dut(stim)
    gr = _gr(stim)
    res = _compare(dut, gr)
    print(f"\namp={amp}:", res.summary())
    assert res.passed, res.summary()


# --- bit-exact substrate ------------------------------------------------------

@pytest.mark.parametrize("seed", [3, 17, 256])
def test_bitexact_reference(seed):
    """DUT matches the saturating Q15 reference EXACTLY over a stream that includes
    out-of-unit-circle samples (amp 0.95 → frequent saturation)."""
    stim = _random(seed, n=80, amp=0.95)
    dut = _run_dut(stim)
    blk = ComplexToMagSquaredBlock("ref")
    a = [_q15(c.real) for c in stim]
    b = [_q15(c.imag) for c in stim]
    ref = blk.process_reference_q15(a, b)
    res = compare_against_grc(dut.i_q15, [_s16(r) / 32768.0 for r in ref],
                              metric=Metric.EXACT, delay=0)
    print(f"\nbit-exact seed={seed}:", res.summary())
    assert res.passed, res.summary()


@pytest.mark.parametrize("z", [complex(0.9, 0.9), complex(-0.99, 0.2),
                               complex(0.8, -0.7)])
def test_saturates_not_wraps(z):
    """|z| ≥ 1 must PIN to +full-scale (power is non-negative, never wraps low)."""
    stim = [complex(0.1, 0.1), z, complex(0.2, -0.2), z]
    dut = _run_dut(stim)
    assert _s16(dut.i_q15[1]) == 32767 and _s16(dut.i_q15[3]) == 32767, \
        f"must saturate to 32767, got {_s16(dut.i_q15[1])}"


# --- MANDATORY mutation tests -------------------------------------------------
# NOTE |z|² = re²+im² is SYMMETRIC in re/im, so a swapped-channel mutation is not a
# corruption and is intentionally not tested.

def _setup():
    stim = _random(7, 32)
    dut = _run_dut(stim)
    gr = _gr(stim)
    return dut, gr, stim


def test_mutation_inverted_output_fails():
    """Power is ≥ 0; a sign-inverted DUT must FAIL."""
    dut, gr, _ = _setup()
    mutated = [(0x10000 - (w or 0)) & 0xFFFF for w in dut.i_q15]
    res = compare_against_grc(mutated, gr.i, metric=Metric.AMPLITUDE,
                              delay=0, op_count=OP_COUNT)
    assert not res.passed, "gate failed to detect an inverted output!"


def test_mutation_halved_magnitude_fails():
    dut, gr, _ = _setup()
    halved = [(_s16(w) // 2) & 0xFFFF for w in dut.i_q15]
    res = compare_against_grc(halved, gr.i, metric=Metric.AMPLITUDE,
                              delay=0, op_count=OP_COUNT)
    assert not res.passed, "gate failed to detect a halved-magnitude output!"


def test_mutation_wrong_second_stream_fails():
    stim = _random(7, 32)
    dut = _run_dut(stim)
    other = _random(8, 32)
    wrong = [complex(s.real, o.imag) for s, o in zip(stim, other)]
    gr_wrong = _gr(wrong)
    res = compare_against_grc(dut.i_q15, gr_wrong.i, metric=Metric.AMPLITUDE,
                              delay=0, op_count=OP_COUNT)
    assert not res.passed, "gate failed to detect a wrong imag channel!"


def test_mutation_one_sample_offset_fails():
    dut, gr, _ = _setup()
    shifted = [0x0000] + list(dut.i_q15[:-1])
    res = compare_against_grc(shifted, gr.i, metric=Metric.AMPLITUDE,
                              delay=0, op_count=OP_COUNT)
    assert not res.passed, "gate failed to detect a 1-sample latency error!"


def test_empty_output_fails():
    _, gr, _ = _setup()
    res = compare_against_grc([], gr.i, metric=Metric.AMPLITUDE,
                              delay=0, op_count=OP_COUNT)
    assert not res.passed


# --- dashboard report ---------------------------------------------------------

def test_emit_report():
    dut = _run_dut(EDGE)
    gr = _gr(EDGE)
    res = _compare(dut, gr)
    assert res.passed, res.summary()
    write_report("ComplexToMagSquaredBlock", res, coverage={
        "edge": True, "random": 3, "amplitude_sweep": 4, "bit_exact": True,
        "saturation": True, "mutation": True})
