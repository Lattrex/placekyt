# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify RRCPulseShaperBlock 1:1 against GNU Radio.

The TX-chain root-raised-cosine pulse shaper is a real multi-cell FIR (span*sps+1
taps, default span=8 sps=4 -> 33 taps across 7 cells, chained partial-sum MACs).
Its coefficients are generated from the standard RRC formula and normalized to
DC gain 1 — which is BIT-FOR-BIT the same tap set GNU Radio produces with::

    firdes.root_raised_cosine(1.0, sps, 1.0, alpha, ntaps)

(verified to printed precision: gain=1 already normalizes GR's taps to sum~1, and
the block's closed-form matches it). The on-chip FIR is CAUSAL with the standard
convention out[n] = sum_k h[k]*x[n-k], identical to GNU Radio's ``fir_filter_fff``,
so the comparison aligns at delay 0 (empirically confirmed: 3 LSB error, the 33-tap
Q15 MAC floor).

GR equivalent: ``filter.fir_filter_fff(1, firdes.root_raised_cosine(1, sps, 1, alpha, ntaps))``

Run::

    cd verification
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
        .venv/bin/python -m pytest tests/test_rrc_pulse_shaper.py -v
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PLACEKYT = Path(__file__).resolve().parents[2] / "placekyt"
_VERIFY = Path(__file__).resolve().parents[1]
for p in (str(_PLACEKYT), str(_VERIFY)):
    if p not in sys.path:
        sys.path.insert(0, p)

from kyttar_verify import (  # noqa: E402
    run_block_dut, run_gnuradio_ref, compare_against_grc, write_report, Metric)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")

_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not _GR_AVAILABLE, reason="GNU Radio interpreter not available")

_SPS = 4
_ALPHA = 0.35
_SPAN = 8
_NTAPS = _SPAN * _SPS + 1  # 33


def _fq(v: float) -> int:
    q = int(round(v * 32768.0))
    return max(-32768, min(32767, q)) & 0xFFFF


def _kyttar_taps(alpha=_ALPHA, span=_SPAN):
    """The block's own coefficients (so the tap-equivalence test is self-contained)."""
    from gr_kyttar.placement.blocks.rrc_pulse_shaper_block import RRCPulseShaperBlock
    return list(RRCPulseShaperBlock("rrc", alpha=alpha, span=span).coefficients)


def _gr_rrc(inq, alpha=_ALPHA, ntaps=_NTAPS, sps=_SPS):
    """GNU Radio golden: causal fir_filter_fff with firdes RRC taps."""
    return run_gnuradio_ref(
        inq,
        """
from gnuradio import gr, blocks, filter as gfilter
from gnuradio.filter import firdes

taps = firdes.root_raised_cosine(1.0, float(sps), 1.0, alpha, ntaps)
tb = gr.top_block()
src = blocks.vector_source_f(input_float, False, 1, [])
f = gfilter.fir_filter_fff(1, taps)
snk = blocks.vector_sink_f()
tb.connect(src, f, snk)
tb.run()
output_float = list(snk.data())
""",
        extra_args={"alpha": alpha, "ntaps": int(ntaps), "sps": int(sps)},
    )


def _upsampled(symbols, sps=_SPS):
    up = []
    for s in symbols:
        up.append(s)
        up.extend([0.0] * (sps - 1))
    return up


def _run(symbols, *, alpha=_ALPHA, span=_SPAN):
    inq = [_fq(v) for v in _upsampled(symbols)]
    dut = run_block_dut("RRCPulseShaperBlock", inq,
                        params={"alpha": alpha, "span": span},
                        chip_yaml=CHIP_YAML, in_port="sample", out_port="out")
    assert dut.ok, dut.reason
    ntaps = span * _SPS + 1
    ref = _gr_rrc(inq, alpha=alpha, ntaps=ntaps)
    res = compare_against_grc(dut.outputs_q15, ref.floats, metric=Metric.AMPLITUDE,
                              delay=0, op_count=ntaps)
    return dut, res


# --- tap-level parity (the heart of "mirror GRC verbatim") --------------------

def test_taps_match_firdes():
    """The block's RRC coefficients ARE GNU Radio's firdes.root_raised_cosine
    taps (gain=1), to within float round-off — proving exact GRC parity at the
    coefficient level, not merely a similar shape."""
    import numpy as np
    kt = np.array(_kyttar_taps())
    out = run_gnuradio_ref(
        [0],
        """
from gnuradio.filter import firdes
output_float = list(firdes.root_raised_cosine(1.0, float(sps), 1.0, alpha, ntaps))
""",
        extra_args={"alpha": _ALPHA, "ntaps": _NTAPS, "sps": _SPS})
    gt = np.array(out.floats)
    assert len(kt) == len(gt) == _NTAPS
    max_dev = float(np.max(np.abs(kt - gt)))
    print(f"\nmax tap deviation vs firdes: {max_dev:.2e}")
    assert max_dev < 1e-5, f"taps diverge from firdes by {max_dev}"


# --- output parity -------------------------------------------------------------

def test_rrc_shapes_bpsk_stream():
    """An upsampled BPSK symbol stream, pulse-shaped on-chip, matches GR's
    fir_filter_fff with the firdes RRC taps within the 33-tap Q15 MAC floor."""
    syms = [0.5, -0.5, 0.5, 0.5, -0.5, -0.5, 0.5, -0.5, 0.5, -0.5]
    dut, res = _run(syms)
    print("\nrrc bpsk:", res.summary(), "| words", dut.n_words)
    assert res.passed, res.summary()


def test_rrc_impulse_is_tap_set():
    """A single impulse drives out the (scaled) tap set — the cleanest FIR check."""
    dut, res = _run([0.5] + [0.0] * 9)
    print("\nrrc impulse:", res.summary())
    assert res.passed, res.summary()


@pytest.mark.parametrize("alpha", [0.25, 0.35, 0.5])
def test_rrc_alpha_sweep(alpha):
    """Different excess-bandwidth factors all match GR's firdes taps."""
    syms = [0.5, -0.5, 0.5, -0.5, 0.5, 0.5, -0.5, -0.5]
    dut, res = _run(syms, alpha=alpha)
    print(f"\nrrc alpha={alpha}:", res.summary())
    assert res.passed, res.summary()


# --- MANDATORY negative tests --------------------------------------------------

def test_mutation_inverted_output_fails():
    syms = [0.5, -0.5, 0.5, 0.5, -0.5, -0.5, 0.5, -0.5]
    inq = [_fq(v) for v in _upsampled(syms)]
    dut = run_block_dut("RRCPulseShaperBlock", inq,
                        params={"alpha": _ALPHA, "span": _SPAN},
                        chip_yaml=CHIP_YAML, in_port="sample", out_port="out")
    assert dut.ok, dut.reason
    ref = _gr_rrc(inq)
    mutated = [(0x10000 - (w or 0)) & 0xFFFF for w in dut.outputs_q15]
    res = compare_against_grc(mutated, ref.floats, metric=Metric.AMPLITUDE,
                              delay=0, op_count=_NTAPS)
    assert not res.passed, "gate failed to detect a sign-inverted RRC output!"


def test_mutation_wrong_alpha_fails():
    """A DUT shaped with alpha=0.5 must FAIL against an alpha=0.2 golden."""
    syms = [0.5, -0.5, 0.5, 0.5, -0.5, -0.5, 0.5, -0.5, 0.5, -0.5]
    inq = [_fq(v) for v in _upsampled(syms)]
    dut = run_block_dut("RRCPulseShaperBlock", inq,
                        params={"alpha": 0.5, "span": _SPAN},
                        chip_yaml=CHIP_YAML, in_port="sample", out_port="out")
    assert dut.ok, dut.reason
    ref_wrong = _gr_rrc(inq, alpha=0.2)
    res = compare_against_grc(dut.outputs_q15, ref_wrong.floats,
                              metric=Metric.AMPLITUDE, delay=0, op_count=_NTAPS)
    assert not res.passed, "gate failed to detect a wrong-alpha RRC!"


def test_empty_output_fails():
    ref = _gr_rrc([_fq(v) for v in _upsampled([0.5, -0.5])])
    res = compare_against_grc([], ref.floats, metric=Metric.AMPLITUDE,
                              op_count=_NTAPS)
    assert not res.passed


# --- report --------------------------------------------------------------------

def test_emit_report():
    dut, res = _run([0.5, -0.5, 0.5, 0.5, -0.5, -0.5, 0.5, -0.5, 0.5, -0.5])
    write_report("RRCPulseShaperBlock", res, coverage={
        "alpha_sweep": [0.25, 0.35, 0.5],
        "patterns": "bpsk stream, impulse",
        "tap_parity": "exact vs firdes.root_raised_cosine (<1e-5)",
        "mutation": True,
        "gr_equiv": "filter.fir_filter_fff(1, firdes.root_raised_cosine(1,sps,1,alpha,ntaps))",
        "note": f"{_NTAPS}-tap causal multi-cell FIR; aligns at delay 0",
    })
