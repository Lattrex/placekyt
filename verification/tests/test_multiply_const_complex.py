# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify MultiplyConstComplex against GNU Radio blocks.multiply_const_cc(k) fed a
COMPLEX constant ``k = re + im·j``.

MultiplyConstComplex is the TRUE complex-constant multiply — it SCALES **and**
ROTATES the I/Q stream (out = in·k), NOT a duplicate of ComplexGainBlock (which
scales both rails by the same REAL constant, no rotation)::

    yi = xi·re − xq·im        yq = xi·im + xq·re

Each rail is a DIFFERENCE / SUM of two products; the cross-terms (−xq·im on I,
+xi·im on Q) are the rotation. The datapath stores re/4, im/4 in Q15 (each < 1/2 for
|re|,|im| < 2), MULQs each rail's two products (always in range), sums them in-range
(the two /4 products can never sum past full-scale — the |re|,|im|<2 range guarantees
|acc| < 1, so the 16-bit ADD/SUB never wraps), then restores with a SATURATING <<2.
So it SATURATES on overload exactly like multiply_const_cc (GR clips to the Q15
rails) — it CLIPS, never wraps (the ComplexGainBlock/INV-25 bug class, per product).

Two reference tiers:
  * DSP equivalence — DUT vs GNU Radio multiply_const_cc(complex), AMPLITUDE, BOTH
    channels, within the DERIVED Q15 floor.
  * Bit-exact substrate — DUT vs process_reference (the exact per-rail truncating
    MULQ, in-range SUB/ADD, saturating <<2), EXACT, on both channels.

DERIVED TOLERANCE (not tuned). Each rail forms TWO products at the /4 (S=2) scale and
sums them, then restores with <<2 (×2^S=×4), which amplifies BOTH the coefficient
quantization AND the MULQ truncation by 4, and the two products' errors ADD:
    |out − (xi·re±xq·im)| <= 2^S · (2·coeff_err + 2·trunc) + gr_quant
                           = 4   · (2·0.5       + 2·1.0  ) + 1        = 13 LSB
(coeff_err <= 0.5 LSB from rounding re/4, im/4 to Q15; trunc <= 1.0 LSB because
hardware MULQ TRUNCATES toward -inf rather than rounds; TWO products per rail so both
error terms are doubled; +1 for GR's own float->Q15 output quantization). Measured
worst across the |k|/arg(k) sweep = 9 LSB, so the derived 13 bounds it with margin.
This is the honest fixed-point floor of a Q15 two-product complex multiply, NOT a
loosened tolerance.

Per INV-4 every gate is paired with a mutation (dropped cross-term = no rotation,
sign-swapped −xq·im term, wrapped-not-saturated, swap I/Q, +1 delay, wrong k, empty)
that must FAIL. The block is memoryless -> group delay 0.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_multiply_const_complex.py -x -q
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
    run_block_dut_complex, run_gnuradio_ref_complex, compare_complex_against_grc,
    write_report, Metric)
from gr_kyttar.placement.blocks.multiply_const_complex_block import (  # noqa: E402
    MultiplyConstComplex)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_AVAILABLE),
    reason="chip yaml or GNU Radio interpreter absent")

# Derived Q15 floor for the two-product /4-then-<<2 (S=2) complex-multiply datapath —
# see the module docstring.  TOL = 2^S·(2·coeff_err + 2·trunc) + gr_quant
#                                  = 4·(2·0.5 + 2·1.0) + 1 = 13 LSB.
TOL = 13


def _s16(v):
    if v is None:
        return None
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def _gr(stim, re, im):
    """GNU Radio multiply_const_cc(complex(re, im)) over the complex stimulus."""
    return run_gnuradio_ref_complex(
        stim,
        gnuradio_script="""
from gnuradio import gr, blocks
tb = gr.top_block()
src = blocks.vector_source_c(input_complex, False)
mult = blocks.multiply_const_cc(complex(re, im))
snk = blocks.vector_sink_c()
tb.connect(src, mult); tb.connect(mult, snk)
tb.run()
output_complex = list(snk.data())
""",
        extra_args={"re": float(re), "im": float(im)})


def _run_dut(stim, re, im):
    dut = run_block_dut_complex(
        "MultiplyConstComplex", stim, params={"re": re, "im": im}, chip_yaml=CHIP_YAML,
        in_ports=("xi", "xq"), words_per_sample=2)
    assert dut.ok, dut.reason
    return dut


def _signal(seed, n, amp=0.9):
    rng = random.Random(seed)
    return [complex(rng.uniform(-amp, amp), rng.uniform(-amp, amp)) for _ in range(n)]


# EDGE: full-scale rails on each axis (drive the saturating restore), zero, quadrant
# corners. At |k|>1 the outer points overload and MUST pin to the Q15 rails.
EDGE = [complex(0.9, 0.1), complex(0.1, 0.9), complex(-0.9, -0.1),
        complex(-0.1, -0.9), complex(0.5, -0.5), complex(-0.5, 0.5),
        complex(0.0, 0.0), complex(0.999, -0.999), complex(-0.999, 0.999)]

# The load-bearing default constant + a spread of |k| and arg(k): pure-real (no
# rotation), pure-imag (90° swap), diagonals, sub-unity, and >1 magnitudes (overload).
KS = [(0.7, 0.5), (1.5, 0.0), (0.0, 1.0), (0.0, -1.0), (1.0, 0.0),
      (0.9, 0.9), (1.4, 1.4), (-0.9, 0.6), (1.9, -1.9), (0.3, -1.2),
      (-1.1, -0.4), (0.6, 1.7)]


def _cmp_gr(dut, gr):
    return compare_complex_against_grc(
        dut.i_q15, dut.q_q15, gr.i, gr.q, metric=Metric.AMPLITUDE,
        delay=0, tolerance=TOL)


def _cmp_ref(dut, stim, re, im):
    ref = MultiplyConstComplex("ref", re=re, im=im).process_reference(stim)
    ri = [_s16(a) / 32768.0 for a, b in ref]
    rq = [_s16(b) / 32768.0 for a, b in ref]
    return compare_complex_against_grc(
        dut.i_q15, dut.q_q15, ri, rq, metric=Metric.EXACT, delay=0)


# --- structure / smoke --------------------------------------------------------

def test_drives_and_captures():
    dut = _run_dut(_signal(1, 24), 0.7, 0.5)
    assert dut.words_per_sample == 2, f"expected 2 words/sample, got {dut.words_per_sample}"
    assert dut.in_regs == (0, 1), "complex signal should land xi@R0, xq@R1"
    assert all(v is not None for v in dut.i_q15) and all(v is not None for v in dut.q_q15)


# --- DSP equivalence vs GNU Radio multiply_const_cc(complex) ------------------

def test_edge_vectors():
    """MultiplyConstComplex matches GR multiply_const_cc(complex) on edge/full-scale
    vectors (both rails, incl. the saturating outer points) at the default k."""
    dut = _run_dut(EDGE, 0.7, 0.5)
    res = _cmp_gr(dut, _gr(EDGE, 0.7, 0.5))
    print("\nedge:", res.summary(), "| hop", dut.hop_count)
    assert res.passed, res.summary()


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_random_vectors(seed):
    stim = _signal(seed, 48)
    dut = _run_dut(stim, 0.7, 0.5)
    res = _cmp_gr(dut, _gr(stim, 0.7, 0.5))
    print(f"\nrandom seed={seed}:", res.summary())
    assert res.passed, res.summary()


@pytest.mark.parametrize("re,im", KS)
def test_k_sweep(re, im):
    """Sweep |k| and arg(k): pure-real (no rotation), pure-imag (90° swap), diagonals,
    sub-unity and >1 magnitudes (saturating overload). Both rails match GR within the
    derived floor at every point — the full complex-constant space."""
    stim = _signal(11, 48)
    dut = _run_dut(stim, re, im)
    res = _cmp_gr(dut, _gr(stim, re, im))
    print(f"\nk={re}+{im}j:", res.summary())
    assert res.passed, res.summary()


def test_pure_real_equals_complex_gain_behavior():
    """A pure-real k (im=0) must NOT rotate — out = k·in on both rails, exactly the
    ComplexGainBlock behavior (the scope guard: this block is a SUPERSET, and it must
    reduce to plain scaling when the cross-terms vanish)."""
    stim = _signal(3, 40)
    for k in (0.5, 1.0, 1.5):
        dut = _run_dut(stim, k, 0.0)
        res = _cmp_gr(dut, _gr(stim, k, 0.0))
        print(f"\npure-real k={k}:", res.summary())
        assert res.passed, res.summary()


def test_pure_imag_is_90_degree_swap():
    """A pure-imaginary k = j rotates 90°: (a + b·j)·j = −b + a·j, i.e. yi = −xq,
    yq = xi. Verify the DUT performs the swap-and-negate (proves the cross-terms carry
    the rotation, not a scalar)."""
    stim = [complex(0.5, 0.3), complex(-0.4, 0.6), complex(0.2, -0.7)]
    dut = _run_dut(stim, 0.0, 1.0)
    for i, c in enumerate(stim):
        yi = _s16(dut.i_q15[i]) / 32768.0
        yq = _s16(dut.q_q15[i]) / 32768.0
        assert abs(yi - (-c.imag)) < TOL / 32768.0, f"yi {yi} != -xq {-c.imag}"
        assert abs(yq - c.real) < TOL / 32768.0, f"yq {yq} != xi {c.real}"


# --- bit-exact substrate ------------------------------------------------------

@pytest.mark.parametrize("re,im", [(0.7, 0.5), (1.5, 0.0), (0.0, 1.0), (1.4, 1.4),
                                   (1.9, -1.9), (-0.9, 0.6)])
def test_bitexact_reference(re, im):
    """DUT matches the on-chip Q15 reference EXACTLY on BOTH channels (per-rail
    truncating MULQ /4 products, in-range SUB/ADD, saturating <<2) over a long random
    burst exercising the in-range AND the saturating paths."""
    stim = _signal(42, 64)
    dut = _run_dut(stim, re, im)
    res = _cmp_ref(dut, stim, re, im)
    print(f"\nbit-exact k={re}+{im}j:", res.summary())
    assert res.passed, res.summary()


def test_out_of_range_raises():
    """HARDWARE RANGE (documented, INV-0): a re/im with |·| >= 2 is not representable
    in the /4-headroom Q15 datapath and RAISES (never silently mis-scales)."""
    for re, im in ((2.0, 0.0), (0.0, 2.0), (-2.5, 0.5), (0.5, 3.0), (2.1, 2.1)):
        with pytest.raises(ValueError, match="must satisfy"):
            MultiplyConstComplex("c", re=re, im=im)


# --- MANDATORY mutation tests (the gate must DETECT these) ---------------------

def _setup(re=0.7, im=0.5):
    stim = _signal(7, 48)
    dut = _run_dut(stim, re, im)
    gr = _gr(stim, re, im)
    return stim, dut, gr


def test_mutation_dropped_cross_term_fails():
    """DROP the rotation: compare the DUT (full complex multiply) against a NON-rotating
    reference (pure real gain, out = k·in with NO cross-terms). The gate MUST fail —
    proving the DUT actually rotates (the whole point vs ComplexGainBlock). k has a real
    im so the cross-terms are non-trivial."""
    stim, dut, _ = _setup(0.7, 0.5)
    # reference without cross-terms: yi = xi·re, yq = xq·re  (im dropped)
    no_rot = _gr(stim, 0.7, 0.0)
    res = compare_complex_against_grc(dut.i_q15, dut.q_q15, no_rot.i, no_rot.q,
                                      metric=Metric.AMPLITUDE, delay=0, tolerance=TOL)
    assert not res.passed, "gate failed to detect a DROPPED cross-term (no rotation)!"


def test_mutation_sign_swapped_cross_term_fails():
    """SWAP the sign of the −xq·im term (the classic complex-multiply bug: yi = xi·re
    + xq·im instead of − xq·im → rotation the WRONG way). The GR reference for the
    CONJUGATE constant k* = re − im·j gives exactly that wrong-signed I rail; the DUT
    (correct k) must DIFFER from it."""
    stim, dut, _ = _setup(0.7, 0.5)
    conj = _gr(stim, 0.7, -0.5)   # k* = re − im·j : yi = xi·re + xq·im, yq = −xi·im + xq·re
    res = compare_complex_against_grc(dut.i_q15, dut.q_q15, conj.i, conj.q,
                                      metric=Metric.AMPLITUDE, delay=0, tolerance=TOL)
    assert not res.passed, "gate failed to detect a sign-swapped cross-term!"


def test_mutation_wrapped_not_saturated_fails():
    """The overload MUST saturate, not wrap (the ComplexGainBlock/INV-25 bug). A
    wrapping model (<<2 with NO rail clamp, mod 2^16) must FAIL the bit-exact gate
    against the DUT — proving the DUT actually saturates on overload."""
    stim = EDGE
    re, im = 1.9, -1.9   # heavy overload on the outer points
    dut = _run_dut(stim, re, im)
    blk = MultiplyConstComplex("ref", re=re, im=im)

    def mulq(a, b):
        return _s16(((_s16(a) * _s16(b)) >> 15) & 0xFFFF)

    def fq(v):
        return max(-32768, min(32767, int(round(v * 32768.0)))) & 0xFFFF

    wr_i, wr_q = [], []
    for c in stim:
        xi, xq = fq(c.real), fq(c.imag)
        acc_i = _s16((mulq(xi, blk._re_q) - mulq(xq, blk._im_q)) & 0xFFFF)
        acc_q = _s16((mulq(xi, blk._im_q) + mulq(xq, blk._re_q)) & 0xFFFF)
        wr_i.append(_s16((acc_i << 2) & 0xFFFF) / 32768.0)   # <<2 WITHOUT saturation
        wr_q.append(_s16((acc_q << 2) & 0xFFFF) / 32768.0)
    res = compare_complex_against_grc(dut.i_q15, dut.q_q15, wr_i, wr_q,
                                      metric=Metric.EXACT, delay=0)
    assert not res.passed, (
        "gate failed to detect a wrapping (non-saturating) datapath — the DUT must "
        "SATURATE on overload, so it must DIFFER from the wrapping model!")


def test_mutation_swapped_iq_fails():
    _, dut, gr = _setup()
    res = compare_complex_against_grc(dut.q_q15, dut.i_q15, gr.i, gr.q,
                                      metric=Metric.AMPLITUDE, delay=0, tolerance=TOL)
    assert not res.passed, "gate failed to detect swapped I/Q!"


def test_mutation_one_sample_offset_fails():
    _, dut, gr = _setup()
    sh_i = [0x0000] + list(dut.i_q15[:-1])
    sh_q = [0x0000] + list(dut.q_q15[:-1])
    res = compare_complex_against_grc(sh_i, sh_q, gr.i, gr.q,
                                      metric=Metric.AMPLITUDE, delay=0, tolerance=TOL)
    assert not res.passed, "gate failed to detect a 1-sample latency error!"


def test_mutation_wrong_k_fails():
    """A DUT built at the wrong constant must FAIL against the right reference — the
    core equivalence claim (and proof the tolerance is not so loose it accepts any k)."""
    stim = _signal(7, 48)
    dut = _run_dut(stim, 0.7, 0.5)
    gr_wrong = _gr(stim, 1.2, -0.3)   # reference for a DIFFERENT constant
    res = compare_complex_against_grc(dut.i_q15, dut.q_q15, gr_wrong.i, gr_wrong.q,
                                      metric=Metric.AMPLITUDE, delay=0, tolerance=TOL)
    assert not res.passed, "gate failed to detect a wrong-constant mismatch!"


def test_empty_output_fails():
    _, _, gr = _setup()
    res = compare_complex_against_grc([], [], gr.i, gr.q,
                                      metric=Metric.AMPLITUDE, delay=0, tolerance=TOL)
    assert not res.passed


# --- dashboard report ---------------------------------------------------------

def test_emit_report():
    """Emit the dashboard report at the default k = 0.7 + 0.5j. Runs a passing
    equivalence check first."""
    re, im = 0.7, 0.5
    stim = _signal(7, 64)
    dut = _run_dut(stim, re, im)
    res = _cmp_gr(dut, _gr(stim, re, im))
    assert res.passed, res.summary()
    write_report("MultiplyConstComplex", res.i, coverage={
        "edge": True, "random": 3, "param_sweep": len(KS), "bit_exact": True,
        "mutation": True, "orientation": True})
