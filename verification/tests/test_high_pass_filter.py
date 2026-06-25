# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify HighPassFilter against GNU Radio's firdes.high_pass + fir_filter_fff.

HighPassFilter is GRC's **High Pass Filter**: a convenience FIR whose taps come
from ``filter.firdes.high_pass(...)``. The Kyttar block reproduces firdes in pure
Python (the runtime has no GNU Radio) and runs the taps on the verified
FIRFilterBlock datapath. Same two-tier proof as the low-pass (see
test_low_pass_filter.py): the Q15-quantized taps are bit-exact firdes (INV-16),
and the on-chip output matches GNU Radio fir_filter_fff fed firdes taps. Every
gate is paired with a failing mutation (INV-4). firdes high-pass taps are
linear-phase symmetric ⇒ delay=0.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_high_pass_filter.py -x -q
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
    run_block_dut, run_gnuradio_ref, compare_against_grc, write_report, Metric)
from gr_kyttar.placement.blocks.high_pass_filter_block import HighPassFilter  # noqa: E402
from gr_kyttar.placement.blocks._base import float_to_q15  # noqa: E402

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_AVAILABLE),
    reason="chip yaml or GNU Radio interpreter absent")

_WIN_ENUM = {"hamming": 0, "hann": 1, "blackman": 2, "rectangular": 3,
             "kaiser": 4, "blackman_harris": 5}


def _s16(v):
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def _gr_firdes_taps(gain, fs, cutoff, tw, window="hamming", beta=6.76):
    r = run_gnuradio_ref(
        input_q15=[0],
        gnuradio_script="""
from gnuradio.filter import firdes
output_float = list(firdes.high_pass(gain, fs, cutoff, tw, window, beta))
""",
        extra_args={"gain": gain, "fs": fs, "cutoff": cutoff, "tw": tw,
                    "window": _WIN_ENUM[window], "beta": beta})
    return r.floats


def _gr_highpass_filter(inputs_q15, gain, fs, cutoff, tw, window="hamming", beta=6.76):
    return run_gnuradio_ref(
        input_q15=inputs_q15,
        gnuradio_script="""
from gnuradio import gr, filter as gr_filter, blocks
from gnuradio.filter import firdes
taps = firdes.high_pass(gain, fs, cutoff, tw, window, beta)
tb = gr.top_block()
src = blocks.vector_source_f(input_float, False)
fir = gr_filter.fir_filter_fff(1, taps)
sink = blocks.vector_sink_f()
tb.connect(src, fir); tb.connect(fir, sink)
tb.run()
output_float = list(sink.data())
""",
        extra_args={"gain": gain, "fs": fs, "cutoff": cutoff, "tw": tw,
                    "window": _WIN_ENUM[window], "beta": beta})


def _random_input(seed, n):
    rng = random.Random(seed)
    return [rng.randint(0, 0xFFFF) for _ in range(n)]


# --- 1. the DESIGN ------------------------------------------------------------

def test_hp_taps_match_firdes_float():
    """Float taps reproduce firdes.high_pass to within floating-point rounding
    (sub-ULP cross-interpreter libm / FMA difference, far below a Q15 LSB)."""
    blk = HighPassFilter("hp", gain=1.0, samp_rate=32000, cutoff_freq=4000,
                         transition_width=2000, window="hamming")
    gr = _gr_firdes_taps(1.0, 32000, 4000, 2000, "hamming")
    assert len(blk.design_taps) == len(gr)
    err = max(abs(a - b) for a, b in zip(blk.design_taps, gr))
    assert err < 1e-6, f"taps differ from firdes (Hamming) by {err:.2e}"
    assert err < 0.5 / 32768.0


@pytest.mark.parametrize("window", list(_WIN_ENUM))
@pytest.mark.parametrize("cfg", [
    (1.0, 32000, 4000, 2000), (2.0, 48000, 12000, 3000), (0.5, 100000, 30000, 3500)])
def test_hp_taps_q15_exact_all_windows(window, cfg):
    """The Q15-quantized taps that reach the chip are BIT-EXACT to firdes for
    EVERY supported window — so the on-chip filter IS the firdes high-pass."""
    gain, fs, cutoff, tw = cfg
    blk = HighPassFilter("hp", gain=gain, samp_rate=fs, cutoff_freq=cutoff,
                         transition_width=tw, window=window)
    gr = _gr_firdes_taps(gain, fs, cutoff, tw, window)
    assert len(blk.design_taps) == len(gr)
    assert [float_to_q15(t) for t in blk.design_taps] == [float_to_q15(t) for t in gr], \
        f"{window} {cfg}: Q15 taps differ from firdes"


# --- 2. the DSP ---------------------------------------------------------------

def _verify_vs_gr(params, inputs):
    blk = HighPassFilter("c", **params)
    dut = run_block_dut("HighPassFilter", inputs, params=params, chip_yaml=CHIP_YAML)
    assert dut.ok, f"build/run failed: {dut.reason}"
    gr = _gr_highpass_filter(
        inputs, params["gain"], params["samp_rate"], params["cutoff_freq"],
        params["transition_width"], params.get("window", "hamming"),
        params.get("beta", 6.76))
    res = compare_against_grc(
        dut.outputs_q15, gr.floats, metric=Metric.AMPLITUDE, delay=0,
        op_count=blk.num_taps, head_shift=blk._head_shift)
    return blk, dut, res


def test_hp_matches_gnuradio_default():
    params = dict(gain=1.0, samp_rate=32000, cutoff_freq=4000,
                  transition_width=2000, window="hamming")
    inputs = _random_input(seed=7, n=120)
    blk, dut, res = _verify_vs_gr(params, inputs)
    print(f"\nHP default (N={blk.num_taps},S={blk._head_shift},cells={blk.cell_count}):",
          res.summary(), "| hop", dut.hop_count)
    assert res.passed, res.summary()


@pytest.mark.parametrize("params", [
    dict(gain=1.0, samp_rate=48000, cutoff_freq=12000, transition_width=4000),
    dict(gain=1.0, samp_rate=20000, cutoff_freq=5000, transition_width=1500),
    dict(gain=2.0, samp_rate=32000, cutoff_freq=8000, transition_width=2500),
    dict(gain=1.0, samp_rate=32000, cutoff_freq=4000, transition_width=2000,
         window="hann"),
])
def test_hp_param_sweep(params):
    inputs = _random_input(seed=hash(tuple(sorted(params.items()))) & 0xFFFF, n=120)
    blk, dut, res = _verify_vs_gr(params, inputs)
    print(f"\nHP {params} N={blk.num_taps} cells={blk.cell_count}:", res.summary())
    assert res.passed, res.summary()


def test_hp_bitexact_reference():
    params = dict(gain=1.0, samp_rate=32000, cutoff_freq=4000,
                  transition_width=2000, window="hamming")
    blk = HighPassFilter("c", **params)
    inputs = _random_input(seed=42, n=2 * blk.num_taps + 30)
    dut = run_block_dut("HighPassFilter", inputs, params=params, chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    ref = [_s16(w) / 32768.0 for w in blk.process_reference_q15(inputs)]
    res = compare_against_grc(dut.outputs_q15, ref, metric=Metric.EXACT, delay=0)
    print("\nHP bit-exact:", res.summary())
    assert res.passed, res.summary()


# --- mandatory mutation tests -------------------------------------------------

def _mutation_setup():
    params = dict(gain=1.0, samp_rate=32000, cutoff_freq=4000,
                  transition_width=2000, window="hamming")
    inputs = _random_input(seed=11, n=120)
    blk = HighPassFilter("c", **params)
    dut = run_block_dut("HighPassFilter", inputs, params=params, chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    gr = _gr_highpass_filter(inputs, 1.0, 32000, 4000, 2000)
    return blk, dut, gr


def test_hp_mutation_inverted_fails():
    blk, dut, gr = _mutation_setup()
    broken = [(0x10000 - (w or 0)) & 0xFFFF for w in dut.outputs_q15]
    res = compare_against_grc(broken, gr.floats, metric=Metric.AMPLITUDE, delay=0,
                              op_count=blk.num_taps, head_shift=blk._head_shift)
    assert not res.passed, "gate failed to catch an inverted high-pass output!"


def test_hp_mutation_wrong_cutoff_fails():
    blk, dut, _ = _mutation_setup()
    inputs = _random_input(seed=11, n=120)
    gr_wrong = _gr_highpass_filter(inputs, 1.0, 32000, 8000, 2000)  # 4k -> 8k cutoff
    res = compare_against_grc(dut.outputs_q15, gr_wrong.floats, metric=Metric.AMPLITUDE,
                              delay=0, op_count=blk.num_taps, head_shift=blk._head_shift)
    assert not res.passed, "gate failed to catch a wrong-cutoff mismatch!"


def test_hp_mutation_delay_offset_fails():
    blk, dut, gr = _mutation_setup()
    shifted = [0x0000] + list(dut.outputs_q15[:-1])
    res = compare_against_grc(shifted, gr.floats, metric=Metric.AMPLITUDE, delay=0,
                              op_count=blk.num_taps, head_shift=blk._head_shift)
    assert not res.passed, "gate failed to catch a 1-sample latency error!"


def test_hp_empty_output_fails():
    blk, _, gr = _mutation_setup()
    res = compare_against_grc([], gr.floats, metric=Metric.AMPLITUDE, delay=0,
                              op_count=blk.num_taps, head_shift=blk._head_shift)
    assert not res.passed


def test_emit_report():
    params = dict(gain=1.0, samp_rate=32000, cutoff_freq=4000,
                  transition_width=2000, window="hamming")
    inputs = [v // 2 for v in _random_input(seed=264, n=120)]
    blk, dut, res = _verify_vs_gr(params, inputs)
    assert res.passed, res.summary()
    assert res.tolerance > 0
    write_report("HighPassFilter", res, coverage={
        "edge": True, "random": 1, "param_sweep": 4,
        "windows": len(_WIN_ENUM), "mutation": True, "ntaps": blk.num_taps})
