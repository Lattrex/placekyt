# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify NCOBlock against GNU Radio's analog.sig_source_c (complex cosine).

NCOBlock is GRC's **Signal Source** (the complex ``sig_source_c``): a numerically-
controlled oscillator emitting ``amplitude·exp(jθ_n)`` — ``I = amplitude·cos θ_n``,
``Q = amplitude·sin θ_n`` with ``θ_n = 2π·frequency/sample_rate·n``. Each input
sample is a TRIGGER (ignored); one complex output per trigger. Params mirror GRC's
Signal Source in the user's units (sample_rate, frequency Hz, amplitude, waveform);
the phase word is derived (``freq_word = round(frequency/sample_rate·65536)``).

PRECISION. GNU Radio's ``sig_source_c`` is effectively exact. The Kyttar NCO
reconstructs the sine from a 33-entry quarter-wave Q15 table with LINEAR
INTERPOLATION (idx_bits=7). Two distinct, derived effects:
  * the TABLE-INTERP floor ≈ 11 LSB — the linear-interpolation error of a 33-point
    quarter table (≤ 1 LSB on phase that lands on a table grid point); and
  * the FREQ_WORD quantization — the 16-bit phase word represents frequency to
    ``fs/65536`` Hz, so an off-grid requested frequency runs at the nearest
    representable tone and slowly drifts vs GR's exact frequency.
This suite isolates both: grid-aligned frequencies (freq_word a multiple of 512)
are BOTH exactly representable AND on the table grid → DUT matches GR to ~1 LSB;
off-grid, the DUT is compared to GR at the DUT's ACTUAL (freq_word) frequency, so
only the ~11-LSB table floor remains.

Reference tiers:
  * DSP equivalence — DUT vs GNU Radio ``sig_source_c`` (AMPLITUDE, derived floor).
  * Bit-exact substrate — DUT vs ``process_reference_q15`` (the on-chip datapath:
    angle-fold + parity-split interpolated table + amplitude-then-sign), EXACT, at
    grid AND off-grid frequencies (so interpolation + the odd-idx path are gated).

Per INV-4 every gate is paired with a mutation (swap I/Q, negate Q, +1 delay, wrong
frequency, empty) that must FAIL. n=0 = (amp, 0) (GR phase-0 start), delay=0.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_nco.py -x -q
"""
from __future__ import annotations

import math
import os
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
from gr_kyttar.placement.blocks.nco_block import NCOBlock  # noqa: E402

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_AVAILABLE),
    reason="chip yaml or GNU Radio interpreter absent")

TABLE_FLOOR_LSB = 12   # 33-entry quarter table, idx_bits=7 interpolation floor


def _s16(v):
    if v is None:
        return None
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def _run_dut(fs, f, amp, n, offset=0.0, phase=0.0):
    dut = run_block_dut_complex(
        "NCOBlock", [complex(0, 0)] * n,
        params={"sample_rate": fs, "frequency": f, "amplitude": amp,
                "offset": offset, "phase": phase},
        chip_yaml=CHIP_YAML, words_per_sample=2)
    assert dut.ok, dut.reason
    return dut


def _gr_sig_source(fs, f, amp, n, offset=0.0, phase=0.0):
    return run_gnuradio_ref_complex(
        [complex(0, 0)] * n,
        gnuradio_script="""
from gnuradio import gr, analog, blocks
tb = gr.top_block()
src = analog.sig_source_c(fs, analog.GR_COS_WAVE, f, amp, offset, phase)
hd = blocks.head(gr.sizeof_gr_complex, N)
snk = blocks.vector_sink_c()
tb.connect(src, hd); tb.connect(hd, snk)
tb.run()
output_complex = list(snk.data())
""",
        extra_args={"fs": fs, "f": f, "amp": amp, "N": n,
                    "offset": offset, "phase": phase})


# --- structure / smoke --------------------------------------------------------

def test_nco_drives_and_captures():
    dut = _run_dut(32000, 2000, 0.9, 24)
    assert dut.words_per_sample == 2, f"expected 2 words/sample, got {dut.words_per_sample}"
    assert dut.in_regs == (0, 1), "complex trigger should land xi@R0, xq@R1"
    assert all(v is not None for v in dut.i_q15), "I channel has a missing egress"
    assert all(v is not None for v in dut.q_q15), "Q channel has a missing egress"


def test_nco_phase_zero_start():
    """n=0 output is (amplitude, 0) — GR's phase-0 convention (cos0=1, sin0=0)."""
    dut = _run_dut(32000, 2000, 0.9, 8)
    blk = NCOBlock("c", sample_rate=32000, frequency=2000, amplitude=0.9)
    ref0 = blk.process_reference_q15(range(1))[0]
    assert _s16(dut.i_q15[0]) == _s16(ref0[0]), "n=0 I mismatch"
    assert _s16(dut.q_q15[0]) == 0 and _s16(ref0[1]) == 0, "n=0 Q must be 0"
    assert _s16(dut.i_q15[0]) > 0.85 * 32768, "n=0 I must be ~+amplitude"


# --- bit-exact substrate (grid AND off-grid: gates interpolation + odd path) ---

@pytest.mark.parametrize("fs,f,amp", [
    (32000, 2000, 0.9),    # fw 4096 grid-aligned (frac=0, even idx)
    (32000, 2050, 0.9),    # fw 4198 OFF-grid (non-zero frac, odd idx exercised)
    (32000, 777, 0.8),     # fw 1591 off-grid
    (48000, 5000, 0.9),    # fw 6827 off-grid
    (32000, 12345, 0.95),  # fw 25283 off-grid (multi-quadrant)
])
def test_nco_bitexact_reference(fs, f, amp):
    """The DUT matches the on-chip Q15 reference EXACTLY on BOTH channels over a
    long run — including OFF-GRID frequencies, which exercise interpolation
    (non-zero frac) and the odd-idx parity-swap path that grid-aligned tests miss."""
    n = 120
    dut = _run_dut(fs, f, amp, n)
    blk = NCOBlock("ref", sample_rate=fs, frequency=f, amplitude=amp)
    ref = blk.process_reference_q15(range(n))
    ri = [_s16(yi) / 32768.0 for yi, yq in ref]
    rq = [_s16(yq) / 32768.0 for yi, yq in ref]
    res = compare_complex_against_grc(dut.i_q15, dut.q_q15, ri, rq,
                                      metric=Metric.EXACT, delay=0)
    print(f"\nbit-exact fs={fs} f={f} fw={blk.freq_word}:", res.summary())
    assert res.passed, res.summary()


# --- DSP equivalence vs GNU Radio ---------------------------------------------

@pytest.mark.parametrize("fs,f,amp", [
    (32000, 2000, 0.9), (32000, 4000, 0.8), (32000, 1000, 0.9), (48000, 3000, 0.7)])
def test_nco_matches_gnuradio_grid(fs, f, amp):
    """On grid-aligned frequencies (freq_word a table-grid multiple, no
    interpolation error AND no freq_word drift) the DUT is a drop-in for GNU
    Radio's sig_source_c to within ~1 LSB on BOTH channels."""
    n = 64
    blk = NCOBlock("c", sample_rate=fs, frequency=f, amplitude=amp)
    assert blk.freq_word % 512 == 0, "grid-aligned test needs freq_word % 512 == 0"
    dut = _run_dut(fs, f, amp, n)
    gr = _gr_sig_source(fs, f, amp, n)
    res = compare_complex_against_grc(dut.i_q15, dut.q_q15, gr.i, gr.q,
                                      metric=Metric.AMPLITUDE, delay=0, tolerance=2)
    print(f"\nvs GR grid f={f} fw={blk.freq_word}:", res.summary())
    assert res.passed, res.summary()


@pytest.mark.parametrize("offset,phase", [
    (0.0, 0.0), (0.2, 0.0), (0.0, math.pi / 2), (0.0, math.pi),
    (0.1, math.pi / 4)])
def test_nco_offset_and_phase_match_gnuradio(offset, phase):
    """The GR sig_source_c ``offset`` (a real DC bias on the I/real channel only)
    and initial ``phase`` (radians) match GNU Radio on a grid-aligned tone. offset
    shifts I only (Q unchanged), phase rotates the start angle — both within the
    grid ~1-2 LSB floor."""
    fs, f, amp = 32000, 2000, 0.7
    blk = NCOBlock("c", sample_rate=fs, frequency=f, amplitude=amp,
                   offset=offset, phase=phase)
    assert blk.freq_word % 512 == 0
    n = 48
    dut = _run_dut(fs, f, amp, n, offset=offset, phase=phase)
    gr = _gr_sig_source(fs, f, amp, n, offset=offset, phase=phase)
    res = compare_complex_against_grc(dut.i_q15, dut.q_q15, gr.i, gr.q,
                                      metric=Metric.AMPLITUDE, delay=0, tolerance=3)
    print(f"\nvs GR offset={offset} phase={phase:.3f}:", res.summary())
    assert res.passed, res.summary()


@pytest.mark.parametrize("fs,f,amp", [
    (32000, 2050, 0.9), (48000, 5000, 0.9), (32000, 12345, 0.95)])
def test_nco_matches_gnuradio_offgrid_table_floor(fs, f, amp):
    """Off a table grid, the DUT matches GNU Radio within the derived ~11-LSB
    table-interpolation floor — comparing to GR at the DUT's ACTUAL (freq_word)
    frequency so the separate freq_word-quantization drift is removed. This is the
    documented precision limit, not a loosened gate."""
    n = 64
    blk = NCOBlock("c", sample_rate=fs, frequency=f, amplitude=amp)
    assert blk.freq_word % 512 != 0, "off-grid test needs a non-grid freq_word"
    f_actual = blk.freq_word / 65536.0 * fs   # the tone the DUT actually generates
    dut = _run_dut(fs, f, amp, n)
    gr = _gr_sig_source(fs, f_actual, amp, n)
    res = compare_complex_against_grc(dut.i_q15, dut.q_q15, gr.i, gr.q,
                                      metric=Metric.AMPLITUDE, delay=0,
                                      tolerance=TABLE_FLOOR_LSB)
    print(f"\nvs GR off-grid f={f} fw={blk.freq_word} (GR@{f_actual:.2f}Hz):",
          res.summary())
    assert res.passed, res.summary()


def test_nco_table_floor_is_derived():
    """The ~11-LSB floor is the ANALYTIC interpolation error of the 33-entry table,
    not a tuned number: the block's interpolated reference vs the EXACT tone (same
    freq_word, so only the table-interp error remains) peaks near 11 LSB."""
    blk = NCOBlock("c", sample_rate=32000, frequency=2050, amplitude=0.9)
    ref = blk.process_reference_q15(range(400))
    fw, amp = blk.freq_word, 0.9
    maxerr = 0
    for i, (yi, yq) in enumerate(ref):
        th = 2 * math.pi * fw / 65536 * i
        maxerr = max(maxerr, abs(_s16(yi) - int(round(amp * math.cos(th) * 32768))),
                     abs(_s16(yq) - int(round(amp * math.sin(th) * 32768))))
    print(f"\ntable floor: {maxerr} LSB")
    assert 6 <= maxerr <= TABLE_FLOOR_LSB, f"table floor {maxerr} unexpected"


# --- mandatory mutation tests (the gate must DETECT these) --------------------

def _setup():
    dut = _run_dut(32000, 2000, 0.9, 64)
    gr = _gr_sig_source(32000, 2000, 0.9, 64)
    return dut, gr


def test_nco_mutation_swapped_iq_fails():
    dut, gr = _setup()
    res = compare_complex_against_grc(dut.q_q15, dut.i_q15, gr.i, gr.q,
                                      metric=Metric.AMPLITUDE, delay=0, tolerance=2)
    assert not res.passed, "gate failed to detect swapped I/Q!"


def test_nco_mutation_negated_q_fails():
    dut, gr = _setup()
    neg_q = [(0x10000 - (w or 0)) & 0xFFFF for w in dut.q_q15]
    res = compare_complex_against_grc(dut.i_q15, neg_q, gr.i, gr.q,
                                      metric=Metric.AMPLITUDE, delay=0, tolerance=2)
    assert not res.passed, "gate failed to detect a negated Q channel!"


def test_nco_mutation_one_sample_offset_fails():
    dut, gr = _setup()
    sh_i = [0x0000] + list(dut.i_q15[:-1])
    sh_q = [0x0000] + list(dut.q_q15[:-1])
    res = compare_complex_against_grc(sh_i, sh_q, gr.i, gr.q,
                                      metric=Metric.AMPLITUDE, delay=0, tolerance=2)
    assert not res.passed, "gate failed to detect a 1-sample latency error!"


def test_nco_mutation_wrong_frequency_fails():
    dut = _run_dut(32000, 2000, 0.9, 64)
    gr_wrong = _gr_sig_source(32000, 5000, 0.9, 64)   # 2 kHz DUT vs 5 kHz GR
    res = compare_complex_against_grc(dut.i_q15, dut.q_q15, gr_wrong.i, gr_wrong.q,
                                      metric=Metric.AMPLITUDE, delay=0, tolerance=2)
    assert not res.passed, "gate failed to detect a wrong-frequency mismatch!"


def test_nco_empty_output_fails():
    _, gr = _setup()
    res = compare_complex_against_grc([], [], gr.i, gr.q,
                                      metric=Metric.AMPLITUDE, delay=0, tolerance=2)
    assert not res.passed


# --- dashboard report ---------------------------------------------------------

def test_emit_report():
    import json
    # Report the OFF-GRID (interpolated) floor — the REPRESENTATIVE noise the block
    # produces at an arbitrary requested frequency, not the grid-aligned best case
    # (where the phase lands exactly on table entries and the NCO is ~1 LSB / -88 dB,
    # which over-states real-world accuracy). 2050 Hz is off the 33-entry table grid
    # (freq_word % 512 != 0), so this exercises the linear interpolation. Compared to
    # GR at the DUT's ACTUAL freq_word frequency so only the table-interp error shows
    # (the separate freq_word-quantization drift is a frequency-accuracy spec, not a
    # per-sample noise floor).
    fs, f, amp = 32000, 2050, 0.9
    blk = NCOBlock("c", sample_rate=fs, frequency=f, amplitude=amp)
    assert blk.freq_word % 512 != 0, "report must use an OFF-grid frequency"
    f_actual = blk.freq_word / 65536.0 * fs
    dut = _run_dut(fs, f, amp, 64)
    gr = _gr_sig_source(fs, f_actual, amp, 64)
    res = compare_complex_against_grc(dut.i_q15, dut.q_q15, gr.i, gr.q,
                                      metric=Metric.AMPLITUDE, delay=0,
                                      tolerance=TABLE_FLOOR_LSB)
    assert res.passed, res.summary()
    report = {
        "kyttar_block": "NCOBlock", "passed": True, "metric": "amplitude",
        "n_compared": res.i.n_compared, "max_abs_err": res.i.max_abs_err,
        "tolerance": res.i.tolerance, "nmse_db": res.i.nmse_db,
        "correlation": res.i.correlation, "bit_errors": 0, "delay_used": 0,
        "coverage": {"param_sweep": 5, "bit_exact": True, "mutation": True,
                     "grid_aligned": True, "off_grid_floor": TABLE_FLOOR_LSB},
    }
    (_VERIFY / "reports").mkdir(exist_ok=True)
    (_VERIFY / "reports" / "NCOBlock.json").write_text(json.dumps(report))
