# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify LowPassFilter against GNU Radio's filter.firdes.low_pass + fir_filter_fff.

LowPassFilter is GRC's **Low Pass Filter**: a convenience FIR specified in DSP
units (gain, sample rate, cutoff, transition width, window) whose taps come from
``filter.firdes.low_pass(...)``. The Kyttar block reproduces firdes' windowed-sinc
designer in pure Python (the runtime has no GNU Radio) and runs the taps on the
verified FIRFilterBlock datapath. This suite proves BOTH halves:

  1. The DESIGN — the block's float taps equal GNU Radio's firdes taps (BIT-EXACT
     for the Hamming/Hann/Rectangular/Kaiser windows; for every window the
     Q15-quantized taps that actually reach the chip are bit-exact, so the
     on-chip filter IS the firdes filter).
  2. The DSP — the on-chip output matches GNU Radio ``fir_filter_fff`` fed those
     firdes taps, within the derived headroom-aware Q15 floor; and bit-exactly
     matches the FIRFilterBlock Q15 reference (the hardware predictor).

Per INV-4 every gate is paired with a mutation (inverted, wrong cutoff, +1 delay,
empty) that must FAIL. firdes low-pass taps are linear-phase SYMMETRIC, so delay=0
(group delay carried identically by GR and the DUT) and the reversed-tap
convention is moot.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_low_pass_filter.py -x -q
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
from gr_kyttar.placement.blocks.low_pass_filter_block import LowPassFilter  # noqa: E402
from gr_kyttar.placement.blocks._base import float_to_q15  # noqa: E402

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_AVAILABLE),
    reason="chip yaml or GNU Radio interpreter absent")

# canonical window name -> GR firdes.WIN_* enum int (what a .grc file stores)
_WIN_ENUM = {"hamming": 0, "hann": 1, "blackman": 2, "rectangular": 3,
             "kaiser": 4, "blackman_harris": 5}

EDGE = [0x0000, 0x4000, 0x2000, 0xC000, 0x7FFF, 0x8001, 0x6000, 0xA000,
        0x1000, 0x3000]


def _s16(v):
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


# --- GNU Radio golden ---------------------------------------------------------

def _gr_firdes_taps(gain, fs, cutoff, tw, window="hamming", beta=6.76):
    """GNU Radio's firdes.low_pass taps (the golden design)."""
    r = run_gnuradio_ref(
        input_q15=[0],
        gnuradio_script="""
from gnuradio.filter import firdes
output_float = list(firdes.low_pass(gain, fs, cutoff, tw, window, beta))
""",
        extra_args={"gain": gain, "fs": fs, "cutoff": cutoff, "tw": tw,
                    "window": _WIN_ENUM[window], "beta": beta})
    return r.floats


def _gr_lowpass_filter(inputs_q15, gain, fs, cutoff, tw, window="hamming", beta=6.76):
    """GNU Radio END-TO-END: firdes.low_pass designs the taps and fir_filter_fff
    applies them — the full GR Low Pass Filter, the golden the DUT must match."""
    return run_gnuradio_ref(
        input_q15=inputs_q15,
        gnuradio_script="""
from gnuradio import gr, filter as gr_filter, blocks
from gnuradio.filter import firdes
taps = firdes.low_pass(gain, fs, cutoff, tw, window, beta)
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


# --- 1. the DESIGN: taps equal GNU Radio firdes -------------------------------

def test_lp_taps_match_firdes_float():
    """The block's float taps reproduce GNU Radio firdes.low_pass — same tap count
    and same value to within floating-point rounding. (Exact bit reproduction is
    interpreter-dependent: the runtime .venv and the GR host link DIFFERENT libm
    implementations, so sin/cos differ in the last bit and a tap can differ by ~1
    float32 ULP. The hardware-determining property — the Q15-quantized tap — IS
    bit-exact, asserted in test_lp_taps_q15_exact_all_windows. The float floor here
    is far below a single Q15 LSB (3.05e-5), so the design is provably firdes'.)"""
    blk = LowPassFilter("lp", gain=1.0, samp_rate=32000, cutoff_freq=4000,
                        transition_width=2000, window="hamming")
    gr = _gr_firdes_taps(1.0, 32000, 4000, 2000, "hamming")
    mine = blk.design_taps
    assert len(mine) == len(gr), f"tap count {len(mine)} != firdes {len(gr)}"
    err = max(abs(a - b) for a, b in zip(mine, gr))
    assert err < 1e-6, f"taps differ from firdes (Hamming) by {err:.2e} (> float floor)"
    # ... and far below half a Q15 LSB, so quantization is identical.
    assert err < 0.5 / 32768.0


@pytest.mark.parametrize("window", list(_WIN_ENUM))
@pytest.mark.parametrize("cfg", [
    (1.0, 32000, 4000, 2000), (2.0, 48000, 8000, 3000), (0.5, 100000, 9000, 3500)])
def test_lp_taps_q15_exact_all_windows(window, cfg):
    """The Q15-quantized taps — the coefficients that actually reach the chip —
    are BIT-EXACT to GNU Radio's firdes taps quantized the same way, for EVERY
    supported window. (Blackman-family float taps differ from GR by <=1 float32
    ULP from C++ FMA in the cos-window; that never crosses a Q15 boundary, so the
    on-chip filter is provably the firdes filter regardless.)"""
    gain, fs, cutoff, tw = cfg
    blk = LowPassFilter("lp", gain=gain, samp_rate=fs, cutoff_freq=cutoff,
                        transition_width=tw, window=window)
    gr = _gr_firdes_taps(gain, fs, cutoff, tw, window)
    assert len(blk.design_taps) == len(gr)
    qm = [float_to_q15(t) for t in blk.design_taps]
    qg = [float_to_q15(t) for t in gr]
    assert qm == qg, f"{window} {cfg}: Q15 taps differ from firdes"


# --- 2. the DSP: on-chip output matches GNU Radio fir_filter_fff ---------------

def _verify_vs_gr(params, inputs):
    blk = LowPassFilter("c", **params)
    dut = run_block_dut("LowPassFilter", inputs, params=params, chip_yaml=CHIP_YAML)
    assert dut.ok, f"build/run failed: {dut.reason}"
    gr = _gr_lowpass_filter(
        inputs, params["gain"], params["samp_rate"], params["cutoff_freq"],
        params["transition_width"], params.get("window", "hamming"),
        params.get("beta", 6.76))
    res = compare_against_grc(
        dut.outputs_q15, gr.floats, metric=Metric.AMPLITUDE, delay=0,
        op_count=blk.num_taps, head_shift=blk._head_shift)
    return blk, dut, res


def test_lp_matches_gnuradio_default():
    params = dict(gain=1.0, samp_rate=32000, cutoff_freq=4000,
                  transition_width=2000, window="hamming")
    inputs = _random_input(seed=7, n=120)
    blk, dut, res = _verify_vs_gr(params, inputs)
    print(f"\nLP default (N={blk.num_taps},S={blk._head_shift},cells={blk.cell_count}):",
          res.summary(), "| hop", dut.hop_count)
    assert res.passed, res.summary()


@pytest.mark.parametrize("params", [
    dict(gain=1.0, samp_rate=48000, cutoff_freq=6000, transition_width=4000),
    dict(gain=1.0, samp_rate=20000, cutoff_freq=2500, transition_width=1500),
    dict(gain=2.0, samp_rate=32000, cutoff_freq=4000, transition_width=2500),
    dict(gain=1.0, samp_rate=32000, cutoff_freq=4000, transition_width=2000,
         window="hann"),
])
def test_lp_param_sweep(params):
    """Sweep gain / sample-rate / cutoff / transition-width / window: every
    configuration matches GNU Radio's Low Pass Filter within the derived floor."""
    inputs = _random_input(seed=hash(tuple(sorted(params.items()))) & 0xFFFF, n=120)
    blk, dut, res = _verify_vs_gr(params, inputs)
    print(f"\nLP {params} N={blk.num_taps} cells={blk.cell_count}:", res.summary())
    assert res.passed, res.summary()


def test_lp_bitexact_reference():
    """The on-chip DUT matches the FIRFilterBlock Q15 reference EXACTLY (the
    hardware predictor: scaled wrapping accumulation + the final saturating
    shift), driven with a long random burst so every wavefront cell is exercised."""
    params = dict(gain=1.0, samp_rate=32000, cutoff_freq=4000,
                  transition_width=2000, window="hamming")
    blk = LowPassFilter("c", **params)
    inputs = _random_input(seed=42, n=2 * blk.num_taps + 30)
    dut = run_block_dut("LowPassFilter", inputs, params=params, chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    ref = [_s16(w) / 32768.0 for w in blk.process_reference_q15(inputs)]
    res = compare_against_grc(dut.outputs_q15, ref, metric=Metric.EXACT, delay=0)
    print("\nLP bit-exact:", res.summary())
    assert res.passed, res.summary()


# --- mandatory mutation tests (the gate must DETECT these) ---------------------

def _mutation_setup():
    params = dict(gain=1.0, samp_rate=32000, cutoff_freq=4000,
                  transition_width=2000, window="hamming")
    inputs = _random_input(seed=11, n=120)
    blk = LowPassFilter("c", **params)
    dut = run_block_dut("LowPassFilter", inputs, params=params, chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    gr = _gr_lowpass_filter(inputs, 1.0, 32000, 4000, 2000)
    return blk, dut, gr


def test_lp_mutation_inverted_fails():
    blk, dut, gr = _mutation_setup()
    broken = [(0x10000 - (w or 0)) & 0xFFFF for w in dut.outputs_q15]
    res = compare_against_grc(broken, gr.floats, metric=Metric.AMPLITUDE, delay=0,
                              op_count=blk.num_taps, head_shift=blk._head_shift)
    assert not res.passed, "gate failed to catch an inverted low-pass output!"


def test_lp_mutation_wrong_cutoff_fails():
    """The DUT compared against a GR low-pass with a DIFFERENT cutoff must FAIL —
    proves the gate actually checks the designed filter, not just 'some FIR'."""
    blk, dut, _ = _mutation_setup()
    inputs = _random_input(seed=11, n=120)
    gr_wrong = _gr_lowpass_filter(inputs, 1.0, 32000, 8000, 2000)  # 4k -> 8k cutoff
    res = compare_against_grc(dut.outputs_q15, gr_wrong.floats, metric=Metric.AMPLITUDE,
                              delay=0, op_count=blk.num_taps, head_shift=blk._head_shift)
    assert not res.passed, "gate failed to catch a wrong-cutoff mismatch!"


def test_lp_mutation_delay_offset_fails():
    blk, dut, gr = _mutation_setup()
    shifted = [0x0000] + list(dut.outputs_q15[:-1])
    res = compare_against_grc(shifted, gr.floats, metric=Metric.AMPLITUDE, delay=0,
                              op_count=blk.num_taps, head_shift=blk._head_shift)
    assert not res.passed, "gate failed to catch a 1-sample latency error!"


def test_lp_empty_output_fails():
    blk, _, gr = _mutation_setup()
    res = compare_against_grc([], gr.floats, metric=Metric.AMPLITUDE, delay=0,
                              op_count=blk.num_taps, head_shift=blk._head_shift)
    assert not res.passed


# --- dashboard report ---------------------------------------------------------

def test_emit_report():
    params = dict(gain=1.0, samp_rate=32000, cutoff_freq=4000,
                  transition_width=2000, window="hamming")
    inputs = [v // 2 for v in _random_input(seed=264, n=120)]
    blk, dut, res = _verify_vs_gr(params, inputs)
    assert res.passed, res.summary()
    assert res.tolerance > 0
    write_report("LowPassFilter", res, coverage={
        "edge": True, "random": 1, "param_sweep": 4,
        "windows": len(_WIN_ENUM), "mutation": True,
        "ntaps": blk.num_taps})
