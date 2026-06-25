# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify BandRejectFilter against GNU Radio's firdes.band_reject + fir_filter_fff.

BandRejectFilter is GRC's **Band Reject Filter** (band-stop / notch): a convenience
FIR whose taps come from ``filter.firdes.band_reject(...)``. The Kyttar block
reproduces firdes in pure Python (the runtime has no GNU Radio) and runs the taps
on the verified FIRFilterBlock datapath. Same two-tier proof as the other firdes
filters: the Q15-quantized taps are bit-exact firdes (INV-16), and the on-chip
output matches GNU Radio fir_filter_fff fed firdes taps. Every gate is paired with
a failing mutation (INV-4). firdes band-reject taps are linear-phase symmetric ⇒
delay=0. (The notch has a large centre tap ⇒ Σ|h| > 2 ⇒ COEFFICIENT HEADROOM S=2.)

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_band_reject_filter.py -x -q
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
from gr_kyttar.placement.blocks.band_reject_filter_block import BandRejectFilter  # noqa: E402
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


def _gr_firdes_taps(gain, fs, lo, hi, tw, window="hamming", beta=6.76):
    r = run_gnuradio_ref(
        input_q15=[0],
        gnuradio_script="""
from gnuradio.filter import firdes
output_float = list(firdes.band_reject(gain, fs, lo, hi, tw, window, beta))
""",
        extra_args={"gain": gain, "fs": fs, "lo": lo, "hi": hi, "tw": tw,
                    "window": _WIN_ENUM[window], "beta": beta})
    return r.floats


def _gr_bandreject_filter(inputs_q15, gain, fs, lo, hi, tw, window="hamming", beta=6.76):
    return run_gnuradio_ref(
        input_q15=inputs_q15,
        gnuradio_script="""
from gnuradio import gr, filter as gr_filter, blocks
from gnuradio.filter import firdes
taps = firdes.band_reject(gain, fs, lo, hi, tw, window, beta)
tb = gr.top_block()
src = blocks.vector_source_f(input_float, False)
fir = gr_filter.fir_filter_fff(1, taps)
sink = blocks.vector_sink_f()
tb.connect(src, fir); tb.connect(fir, sink)
tb.run()
output_float = list(sink.data())
""",
        extra_args={"gain": gain, "fs": fs, "lo": lo, "hi": hi, "tw": tw,
                    "window": _WIN_ENUM[window], "beta": beta})


def _random_input(seed, n):
    rng = random.Random(seed)
    return [rng.randint(0, 0xFFFF) for _ in range(n)]


# --- 1. the DESIGN ------------------------------------------------------------

def test_br_taps_match_firdes_float():
    blk = BandRejectFilter("br", gain=1.0, samp_rate=32000, low_cutoff_freq=4000,
                           high_cutoff_freq=8000, transition_width=2000, window="hamming")
    gr = _gr_firdes_taps(1.0, 32000, 4000, 8000, 2000, "hamming")
    assert len(blk.design_taps) == len(gr)
    err = max(abs(a - b) for a, b in zip(blk.design_taps, gr))
    assert err < 1e-6, f"taps differ from firdes by {err:.2e}"
    assert err < 0.5 / 32768.0


@pytest.mark.parametrize("window", list(_WIN_ENUM))
@pytest.mark.parametrize("cfg", [
    (1.0, 32000, 4000, 8000, 2000), (2.0, 48000, 6000, 15000, 3000),
    (0.5, 100000, 20000, 35000, 4000)])
def test_br_taps_q15_exact_all_windows(window, cfg):
    """Q15-quantized taps are BIT-EXACT to firdes for EVERY window — the on-chip
    filter IS the firdes band-reject."""
    gain, fs, lo, hi, tw = cfg
    blk = BandRejectFilter("br", gain=gain, samp_rate=fs, low_cutoff_freq=lo,
                           high_cutoff_freq=hi, transition_width=tw, window=window)
    gr = _gr_firdes_taps(gain, fs, lo, hi, tw, window)
    assert len(blk.design_taps) == len(gr)
    assert [float_to_q15(t) for t in blk.design_taps] == [float_to_q15(t) for t in gr], \
        f"{window} {cfg}: Q15 taps differ from firdes"


# --- 2. the DSP ---------------------------------------------------------------

def _verify_vs_gr(params, inputs):
    blk = BandRejectFilter("c", **params)
    dut = run_block_dut("BandRejectFilter", inputs, params=params, chip_yaml=CHIP_YAML)
    assert dut.ok, f"build/run failed: {dut.reason}"
    gr = _gr_bandreject_filter(
        inputs, params["gain"], params["samp_rate"], params["low_cutoff_freq"],
        params["high_cutoff_freq"], params["transition_width"],
        params.get("window", "hamming"), params.get("beta", 6.76))
    res = compare_against_grc(
        dut.outputs_q15, gr.floats, metric=Metric.AMPLITUDE, delay=0,
        op_count=blk.num_taps, head_shift=blk._head_shift)
    return blk, dut, res


def test_br_matches_gnuradio_default():
    params = dict(gain=1.0, samp_rate=32000, low_cutoff_freq=4000,
                  high_cutoff_freq=8000, transition_width=2000, window="hamming")
    inputs = _random_input(seed=7, n=120)
    blk, dut, res = _verify_vs_gr(params, inputs)
    print(f"\nBR default (N={blk.num_taps},S={blk._head_shift},cells={blk.cell_count}):",
          res.summary(), "| hop", dut.hop_count)
    assert res.passed, res.summary()


@pytest.mark.parametrize("params", [
    dict(gain=1.0, samp_rate=48000, low_cutoff_freq=6000, high_cutoff_freq=15000,
         transition_width=4000),
    dict(gain=1.0, samp_rate=20000, low_cutoff_freq=3000, high_cutoff_freq=7000,
         transition_width=1500),
    dict(gain=2.0, samp_rate=32000, low_cutoff_freq=5000, high_cutoff_freq=10000,
         transition_width=2500),
    dict(gain=1.0, samp_rate=32000, low_cutoff_freq=4000, high_cutoff_freq=8000,
         transition_width=2000, window="hann"),
])
def test_br_param_sweep(params):
    inputs = _random_input(seed=hash(tuple(sorted(params.items()))) & 0xFFFF, n=120)
    blk, dut, res = _verify_vs_gr(params, inputs)
    print(f"\nBR {params} N={blk.num_taps} cells={blk.cell_count} S={blk._head_shift}:",
          res.summary())
    assert res.passed, res.summary()


def test_br_bitexact_reference():
    params = dict(gain=1.0, samp_rate=32000, low_cutoff_freq=4000,
                  high_cutoff_freq=8000, transition_width=2000, window="hamming")
    blk = BandRejectFilter("c", **params)
    inputs = _random_input(seed=42, n=2 * blk.num_taps + 30)
    dut = run_block_dut("BandRejectFilter", inputs, params=params, chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    ref = [_s16(w) / 32768.0 for w in blk.process_reference_q15(inputs)]
    res = compare_against_grc(dut.outputs_q15, ref, metric=Metric.EXACT, delay=0)
    print("\nBR bit-exact:", res.summary())
    assert res.passed, res.summary()


# --- mandatory mutation tests -------------------------------------------------

def _mutation_setup():
    params = dict(gain=1.0, samp_rate=32000, low_cutoff_freq=4000,
                  high_cutoff_freq=8000, transition_width=2000, window="hamming")
    inputs = _random_input(seed=11, n=120)
    blk = BandRejectFilter("c", **params)
    dut = run_block_dut("BandRejectFilter", inputs, params=params, chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    gr = _gr_bandreject_filter(inputs, 1.0, 32000, 4000, 8000, 2000)
    return blk, dut, gr


def test_br_mutation_inverted_fails():
    blk, dut, gr = _mutation_setup()
    broken = [(0x10000 - (w or 0)) & 0xFFFF for w in dut.outputs_q15]
    res = compare_against_grc(broken, gr.floats, metric=Metric.AMPLITUDE, delay=0,
                              op_count=blk.num_taps, head_shift=blk._head_shift)
    assert not res.passed, "gate failed to catch an inverted band-reject output!"


def test_br_mutation_wrong_band_fails():
    blk, dut, _ = _mutation_setup()
    inputs = _random_input(seed=11, n=120)
    gr_wrong = _gr_bandreject_filter(inputs, 1.0, 32000, 6000, 12000, 2000)  # shifted notch
    res = compare_against_grc(dut.outputs_q15, gr_wrong.floats, metric=Metric.AMPLITUDE,
                              delay=0, op_count=blk.num_taps, head_shift=blk._head_shift)
    assert not res.passed, "gate failed to catch a wrong-band mismatch!"


def test_br_mutation_delay_offset_fails():
    blk, dut, gr = _mutation_setup()
    shifted = [0x0000] + list(dut.outputs_q15[:-1])
    res = compare_against_grc(shifted, gr.floats, metric=Metric.AMPLITUDE, delay=0,
                              op_count=blk.num_taps, head_shift=blk._head_shift)
    assert not res.passed, "gate failed to catch a 1-sample latency error!"


def test_br_empty_output_fails():
    blk, _, gr = _mutation_setup()
    res = compare_against_grc([], gr.floats, metric=Metric.AMPLITUDE, delay=0,
                              op_count=blk.num_taps, head_shift=blk._head_shift)
    assert not res.passed


def test_emit_report():
    params = dict(gain=1.0, samp_rate=32000, low_cutoff_freq=4000,
                  high_cutoff_freq=8000, transition_width=2000, window="hamming")
    inputs = [v // 2 for v in _random_input(seed=264, n=120)]
    blk, dut, res = _verify_vs_gr(params, inputs)
    assert res.passed, res.summary()
    assert res.tolerance > 0
    write_report("BandRejectFilter", res, coverage={
        "edge": True, "random": 1, "param_sweep": 4,
        "windows": len(_WIN_ENUM), "mutation": True, "ntaps": blk.num_taps})
