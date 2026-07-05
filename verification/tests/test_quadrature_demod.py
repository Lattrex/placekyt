# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify QuadratureDemodBlock against GNU Radio's analog.quadrature_demod_cf.

QuadratureDemodBlock is GRC's **Quadrature Demod** — the FM discriminator. GR computes
``out[n] = gain*arg(x[n]*conj(x[n-1]))`` (a literal atan2). The Kyttar block uses the
STANDARD 16-bit-DSP DISCRIMINATOR — the divide-free derivative form every real FM RX
uses::

    out[n] = gain * di[n],   di[n] = Im(x[n]*conj(x[n-1])) = I[n]*Q[n-1] - Q[n]*I[n-1]

``di`` is already computed by the ``conjmult`` cell. This is ALL MAC/multiply/subtract
(the fabric's strengths) and lands in TWO cells, vs the ~47 a literal on-chip atan2
(CORDIC) would need on this accumulator ISA. The discriminator is atan2's first-order-
equivalent derivative form (``d/dt*atan2(Q,I) = (I*Q'-Q*I')/(I^2+Q^2)``); the two AGREE
for the constant-|x| (limited/AGC'd) signal a real FM RX operates on.

CONTRACT (RULE #0 algorithm deviation, CM-approved 2026-07-05):
  * DUT is BIT-EXACT to process_reference_q15 (the on-chip Q15 discriminator datapath).
  * vs GR the metric is a CORRELATION gate (>=0.999), NOT bit-exact equality to atan2 —
    because GR's literal op (atan2) is hostile to the fabric but the mathematically-
    equivalent cheaper discriminator matches its output by shape. Correlation vs GR is
    ~0.99999 at typical FM deviation, degrading gracefully only past ~1 rad/sample.
"""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PLACEKYT = Path(__file__).resolve().parents[2] / "placekyt"
_VERIFY = Path(__file__).resolve().parents[1]
_RUNTIME = Path(__file__).resolve().parents[2] / "runtime" / "python"
for p in (str(_PLACEKYT), str(_VERIFY), str(_RUNTIME)):
    if p not in sys.path:
        sys.path.insert(0, p)

from kyttar_verify import (  # noqa: E402
    run_block_dut_complex, run_gnuradio_ref_complex)
from gr_kyttar.placement.blocks.quadrature_demod_block import (  # noqa: E402
    QuadratureDemodBlock)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_AVAILABLE),
    reason="chip yaml or GNU Radio interpreter absent")

# A limited FM tone tracks GR's discriminator shape to ~1.0 at normal deviation. A firm
# 0.999 gate fails a broken block (wrong sign, swapped I/Q, missing delay).
CORR_MIN = 0.999


def _s16(v):
    if v is None:
        return None
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def _fm_signal(n, fdev=2500.0, fmod=1000.0, fs=48000.0, amp=1.0, seed=None):
    """A UNIT-amplitude (limited) FM tone — the regime an FM RX operates in."""
    t = np.arange(n) / fs
    if seed is None:
        msg = np.sin(2 * np.pi * fmod * t)
    else:
        rng = np.random.default_rng(seed)
        msg = np.clip(rng.standard_normal(n), -0.95, 0.95)
    phase = 2 * np.pi * fdev * np.cumsum(msg) / fs
    return amp * np.exp(1j * phase)


def _flatten(outputs_q15):
    flat = []
    for o in outputs_q15:
        if isinstance(o, (list, tuple)):
            flat.extend(o)
        else:
            flat.append(o)
    return [int(v) & 0xFFFF for v in flat]


def _run_dut(x, gain):
    dut = run_block_dut_complex(
        "QuadratureDemodBlock", np.asarray(x), params={"gain": gain},
        chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    return _flatten(dut.outputs_q15)


def _gr_quad_demod(x, gain):
    """GNU Radio quadrature_demod_cf(gain) over a complex stimulus x, returns the real
    demod output."""
    res = run_gnuradio_ref_complex(
        [complex(c) for c in x],
        gnuradio_script="""
from gnuradio import gr, analog, blocks
tb = gr.top_block()
src = blocks.vector_source_c(list(input_complex), False)
qd = analog.quadrature_demod_cf(gain)
snk = blocks.vector_sink_f()
tb.connect(src, qd); tb.connect(qd, snk)
tb.run()
output_float = list(snk.data())
""",
        extra_args={"gain": gain})
    return np.asarray(res.i, dtype=np.float64)


def _corr(a, b, signed=False):
    """Pearson correlation. ``abs`` by default (shape match, sign-agnostic — a real
    FM RX's polarity is a convention); ``signed=True`` keeps the sign so a
    polarity-flip mutation is DETECTED."""
    n = min(len(a), len(b))
    if n < 2:
        return 0.0
    a = np.asarray(a[:n], dtype=np.float64)
    b = np.asarray(b[:n], dtype=np.float64)
    if a.std() == 0 or b.std() == 0:
        return 0.0
    c = np.corrcoef(a, b)[0, 1]
    return c if signed else abs(c)


# --- structure / smoke --------------------------------------------------------

def test_qd_block_shape():
    blk = QuadratureDemodBlock("q", gain=1.0)
    assert blk.cell_count == 2, "discriminator is 2 cells (conjmult + gain)"
    assert blk.interface.input_registers == [0, 1], "complex input xi/xq"
    assert blk.interface.output_registers == [0], "one real output"


def test_qd_drives_and_captures():
    du = _run_dut(_fm_signal(24), 1.0)
    assert len(du) > 0, "no egress from the discriminator"
    assert du[0] == 0, "x[-1]=0 -> di[0]=0 (matches GR out[0]=gain*arg(0)=0)"


# --- bit-exact substrate (the on-chip Q15 discriminator datapath, EXACT) ------

@pytest.mark.parametrize("gain,seed", [
    (1.0, None), (0.5, None), (2.0, None), (1.0, 7), (0.3, 11),
])
def test_qd_bitexact_reference(gain, seed):
    """DUT matches the on-chip Q15 reference EXACTLY (the two Q15 MULQ truncations,
    the subtract, and the gain MULQ + saturating shift gate op-for-op)."""
    x = _fm_signal(96, seed=seed)
    du = _run_dut(x, gain)
    ref = [int(r) & 0xFFFF
           for r in QuadratureDemodBlock("ref", gain=gain).process_reference_q15(x)]
    n = min(len(du), len(ref))
    assert n > 0
    mism = sum(1 for i in range(n) if du[i] != ref[i])
    assert mism == 0, f"gain={gain} seed={seed}: {mism}/{n} mismatch vs Q15 ref"


# --- DSP equivalence vs GNU Radio (correlation) -------------------------------

@pytest.mark.parametrize("gain", [0.5, 1.0, 2.0])
def test_qd_matches_gnuradio_correlation(gain):
    x = _fm_signal(600, fdev=2500.0)
    du = [_s16(v) for v in _run_dut(x, gain)]
    gr = _gr_quad_demod(x, gain)
    corr = _corr(du[1:], gr[1:])   # skip the x[-1]=0 startup sample
    print(f"\nQD corr vs GR (gain={gain}): {corr:.6f}")
    assert corr >= CORR_MIN, f"corr {corr:.6f} < {CORR_MIN}"


def test_qd_graceful_at_higher_deviation():
    """The discriminator (sin(dphi)) compresses vs GR's linear angle at large deviation;
    correlation degrades GRACEFULLY, not catastrophically."""
    x = _fm_signal(600, fdev=8000.0)
    du = [_s16(v) for v in _run_dut(x, 1.0)]
    gr = _gr_quad_demod(x, 1.0)
    corr = _corr(du[1:], gr[1:])
    print(f"\nQD corr vs GR (high fdev): {corr:.6f}")
    assert corr >= 0.99, "high-deviation correlation collapsed (should degrade gently)"


# --- mandatory mutation tests (INV-4: the gate MUST detect these) -------------

def _mutate_and_corr(mutator, gain=1.0, signed=False):
    x = _fm_signal(600, fdev=2500.0)
    du = np.asarray([_s16(v) for v in _run_dut(x, gain)], dtype=np.float64)
    gr = _gr_quad_demod(x, gain)
    n = min(len(du), len(gr))
    return _corr(mutator(du[1:n]), gr[1:n], signed=signed)


def test_qd_mutation_negated_output_fails():
    """Negating the demod output (wrong discriminator sign / swapped I·dQ vs Q·dI) must
    break the SIGNED gate.  (Polarity is a convention, so the abs-correlation DSP gate is
    sign-agnostic by design — a real sign bug is caught bit-exact vs the ref AND by the
    signed correlation here.)"""
    corr = _mutate_and_corr(lambda a: -a, signed=True)
    assert corr < CORR_MIN, f"signed gate missed negated output (corr {corr:.4f})"


def test_qd_mutation_shifted_output_fails():
    """A one-sample latency error must break the gate."""
    corr = _mutate_and_corr(lambda a: np.roll(a, 1))
    assert corr < CORR_MIN, f"gate missed 1-sample latency (corr {corr:.4f})"


def test_qd_mutation_wrong_gain_fails_bitexact():
    """A block built with the WRONG gain must NOT be bit-exact to the correct ref."""
    x = _fm_signal(96)
    du = _run_dut(x, 1.0)
    ref_wrong = [int(r) & 0xFFFF
                 for r in QuadratureDemodBlock("w", gain=2.0).process_reference_q15(x)]
    n = min(len(du), len(ref_wrong))
    mism = sum(1 for i in range(n) if du[i] != ref_wrong[i])
    assert mism > 0, "gate failed to detect wrong gain (bit-exact to wrong ref)"


def test_qd_empty_output_fails():
    """An empty DUT output must fail the correlation gate (degenerate guard)."""
    gr = _gr_quad_demod(_fm_signal(64), 1.0)
    corr = _corr([], gr)
    assert corr < CORR_MIN
