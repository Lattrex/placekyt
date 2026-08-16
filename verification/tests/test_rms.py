# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify RMSBlock / RMSCFBlock are Q15 drop-ins for GNU Radio ``blocks.rms_ff``
and ``blocks.rms_cf``.

GR SEMANTICS (pinned against LIVE GR before authoring — see the semantics-pin
test): per sample ``avg = (1-alpha)*avg + alpha*|x|^2`` (avg starts 0) then
``out = sqrt(avg)`` — sqrt AFTER the update, so ``out[0] = sqrt(alpha)*|x[0]|``.

THE GATES (both blocks):

  * BIT-EXACT vs ``process_reference_q15`` (the strongest gate: the on-chip
    front + error-feedback IIR + normalize/quartic/denorm sqrt, word-for-word)
    over edge + random (3 seeds) + an alpha sweep including GR's default.
  * LIVE-GR settled-tail equivalence: the settled RMS is the mean power —
    alpha-independent — so the tail after a DERIVED warm-up must match GR within
    the DERIVED tolerance. The sweep uses EXACTLY-Q15-REPRESENTABLE alphas
    (k/32768) so DUT and GR run the IDENTICAL filter and the transient matches
    too (the step-response test relies on that).
  * The alpha-QUANTIZATION HW-DEVIATION is pinned at GR's default alpha=1e-4
    (alpha_eff = 3/32768, 8.4% slower): on a CONSTANT-amplitude input both
    filters settle to the same RMS, so the tail matches after the (longer)
    quantized-alpha warm-up — and a mid-TRANSIENT comparison FAILS, proving the
    warm-up guard is load-bearing.
  * INV-4 mutations per block, each proven to FAIL the gate: wrong alpha,
    no-sqrt passthrough, x-not-squared, +1 sample delay, inverted, empty; for
    the complex twin also wrong-second-rail and imag-rail-dropped. (Swapped
    rails is NOT a corruption — |z|^2 is symmetric in re/im.)

DERIVED TOLERANCE (settled-tail vs live GR; NOT tuned — see the class
docstring's error budget): sqrt path <= 4.5 LSB (exhaustive bound over all
32768 power words) + settled power error <= 2.5 LSB amplified by
d(sqrt)/dY = 90.5/sqrt(Y) <= 2.78 at stimulus RMS >= 0.18 (-> <= 7 LSB) +
warm-up residual <= 4 LSB (n_warm = ceil(10/alpha_eff), e^-10 residual)
=> 16 Q15 LSB. Measured peaks: 4.4 LSB at RMS 0.4, 7.7 LSB at RMS 0.18.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_rms.py -q
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
for p in (str(_PLACEKYT), str(_VERIFY)):
    if p not in sys.path:
        sys.path.insert(0, p)

from kyttar_verify import (  # noqa: E402
    run_block_dut, run_block_dut_complex, run_gnuradio_ref,
    run_gnuradio_ref_complex, compare_against_grc, write_report, Metric)
from gr_kyttar.placement.blocks import all_block_classes  # noqa: E402

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")

_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON",
                                              "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_AVAILABLE),
    reason="chip yaml or GNU Radio interpreter absent")

# The DERIVED settled-tail tolerance (see module docstring / class docstring
# error budget). NOT tuned to pass.
TOL_LSB = 16

# Exactly-Q15-representable alphas for the DUT-vs-GR sweeps (round(a*32768)
# reproduces k exactly, so DUT and GR run the IDENTICAL single-pole filter).
ALPHA_FAST = 16384 / 32768.0     # 0.5
ALPHA_MID = 3277 / 32768.0       # ~0.1
ALPHA_SLOW = 328 / 32768.0       # ~0.01
ALPHA_STEP = 8192 / 32768.0      # 0.25 (step-response test)


def _cls(name):
    return all_block_classes()[name]


def _q15w(x: float) -> int:
    q = int(round(x * 32768.0))
    return max(-32768, min(32767, q)) & 0xFFFF


def _s16(v) -> int:
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def _warm(alpha: float) -> int:
    """DERIVED warm-up: the IIR transient decays (1-alpha_eff)^n from the full
    initial power offset; n = 10/alpha_eff leaves an e^-10 (~4.5e-5) residual,
    <= ~1.5 LSB of power on any in-range stimulus."""
    aq = int(round(alpha * 32768.0))
    return int(math.ceil(10.0 * 32768.0 / max(1, aq)))


def _random_words(seed: int, n: int, lo=-0.7, hi=0.7) -> list[int]:
    rng = np.random.default_rng(seed)
    return [_q15w(float(v)) for v in rng.uniform(lo, hi, n)]


def _gr_rms_ff(words, alpha):
    """LIVE GNU Radio blocks.rms_ff over the Q15 words."""
    return run_gnuradio_ref(
        input_q15=words,
        gnuradio_script="""
from gnuradio import gr, blocks
tb = gr.top_block()
src = blocks.vector_source_f(input_float)
op = blocks.rms_ff(alpha)
snk = blocks.vector_sink_f()
tb.connect(src, op); tb.connect(op, snk)
tb.run()
output_float = list(snk.data())
""",
        extra_args={"alpha": float(alpha)},
    )


def _gr_rms_cf(pairs, alpha):
    """LIVE GNU Radio blocks.rms_cf over quantized (i, q) float pairs."""
    return run_gnuradio_ref_complex(
        np.asarray(pairs, dtype=np.float64),
        gnuradio_script="""
from gnuradio import gr, blocks
tb = gr.top_block()
src = blocks.vector_source_c(input_complex)
op = blocks.rms_cf(alpha)
snk = blocks.vector_sink_f()
tb.connect(src, op); tb.connect(op, snk)
tb.run()
output_float = list(snk.data())
""",
        extra_args={"alpha": float(alpha)},
    )


def _clip01(vals):
    """GR float RMS -> the Q15-representable [0, 1) span the DUT emits."""
    return [max(0.0, min(32767.0 / 32768.0, v)) for v in vals]


def _run_ff(words, alpha):
    dut = run_block_dut("RMSBlock", words, params={"alpha": alpha},
                        chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    return dut


def _run_cf(pairs, alpha):
    dut = run_block_dut_complex("RMSCFBlock", np.asarray(pairs, dtype=np.float64),
                                params={"alpha": alpha}, chip_yaml=CHIP_YAML,
                                in_ports=("re", "im"), out_port="out",
                                words_per_sample=1)
    assert dut.ok, dut.reason
    return dut


def _cf_pairs(seed: int, n: int, lo=-0.65, hi=0.65):
    """Random I/Q pairs PRE-QUANTIZED to the Q15 grid so DUT and GR see the
    IDENTICAL values."""
    rng = np.random.default_rng(seed)
    i = [_s16(_q15w(float(v))) / 32768.0 for v in rng.uniform(lo, hi, n)]
    q = [_s16(_q15w(float(v))) / 32768.0 for v in rng.uniform(lo, hi, n)]
    return list(zip(i, q))


def _pairs_words(pairs):
    return ([_q15w(i) for i, _ in pairs], [_q15w(q) for _, q in pairs])


def _flat_cf(dut) -> list:
    return [ws[0] & 0xFFFF if ws else None for ws in dut.outputs_q15]


# --- GR competence + semantics pin FIRST (INV-26) -----------------------------

def test_gr_semantics_pin():
    """The golden must itself behave as modeled: (a) out[0] = sqrt(alpha)*|x0|
    (sqrt AFTER the avg update, avg starts 0); (b) a constant input settles to
    its own amplitude; (c) rms_cf runs the same filter on re^2+im^2."""
    a = 0.25
    gr = _gr_rms_ff([_q15w(0.5)] * 60, a)
    assert abs(gr.floats[0] - math.sqrt(a) * 0.5) < 1e-6, gr.floats[:3]
    assert abs(gr.floats[-1] - 0.5) < 1e-4, gr.floats[-1]
    grc = _gr_rms_cf([(0.3, 0.4)] * 60, a)
    assert abs(grc.i[0] - math.sqrt(a) * 0.5) < 1e-6, grc.i[:3]
    assert abs(grc.i[-1] - 0.5) < 1e-4, grc.i[-1]


# --- BIT-EXACT gates (DUT == the Q15 reference, word for word) ----------------

_FF_EDGE = [0x0000, 0x8000, 0x7FFF, 0x0001, 0xFFFF, 0x4000, 0x2000,
            0x0000, 0x8000, 0x0003, 0xFFFD, 0x7FFF]

_CF_EDGE = [(0.5, 0.1), (-0.25, 0.7), (-1.0, -1.0), (0.9, 0.9), (0.0, 0.0),
            (32767 / 32768, 32767 / 32768), (-1.0, 0.0), (0.0, -1.0),
            (1 / 32768, -1 / 32768), (0.3, -0.3)]


@pytest.mark.parametrize("alpha", [ALPHA_FAST, ALPHA_MID, 0.0001])
def test_ff_bitexact_edge(alpha):
    """Edge vectors (zero, both full-scale corners, LSB-small) — bit-exact,
    including the x=-1.0 power-saturation corner."""
    b = _cls("RMSBlock")("ref", alpha=alpha)
    dut = _run_ff(_FF_EDGE, alpha)
    got = [w & 0xFFFF if w is not None else None for w in dut.outputs_q15]
    assert got == b.process_reference_q15(_FF_EDGE)


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_ff_bitexact_random(seed):
    b = _cls("RMSBlock")("ref", alpha=ALPHA_MID)
    words = _random_words(seed, 96, -0.95, 0.95)
    dut = _run_ff(words, ALPHA_MID)
    got = [w & 0xFFFF if w is not None else None for w in dut.outputs_q15]
    assert got == b.process_reference_q15(words)


def test_ff_bitexact_default_alpha():
    """GR's default alpha=0.0001 (alpha_eff = 3/32768) — the error-feedback IIR
    must advance (a bare-MULQ increment would truncate to 0 forever)."""
    b = _cls("RMSBlock")("ref")
    words = _random_words(11, 96, -0.9, 0.9)
    dut = run_block_dut("RMSBlock", words, params={}, chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    got = [w & 0xFFFF if w is not None else None for w in dut.outputs_q15]
    ref = b.process_reference_q15(words)
    assert got == ref
    assert ref[-1] > ref[0] > 0, "the default-alpha averager must ADVANCE"


@pytest.mark.parametrize("alpha", [ALPHA_STEP, 0.0001])
def test_cf_bitexact_edge(alpha):
    """Complex edge vectors — bit-exact, including the re=im=-1.0 corner where
    0x8000 + 0x8000 would WRAP TO ZERO without the per-step N-flag guard."""
    b = _cls("RMSCFBlock")("ref", alpha=alpha)
    dut = _run_cf(_CF_EDGE, alpha)
    rw, iw = _pairs_words(_CF_EDGE)
    assert _flat_cf(dut) == b.process_reference_q15(rw, iw)


@pytest.mark.parametrize("seed", [2, 9, 77])
def test_cf_bitexact_random(seed):
    b = _cls("RMSCFBlock")("ref", alpha=ALPHA_MID)
    pairs = _cf_pairs(seed, 96)
    dut = _run_cf(pairs, ALPHA_MID)
    rw, iw = _pairs_words(pairs)
    assert _flat_cf(dut) == b.process_reference_q15(rw, iw)


# --- LIVE-GR equivalence: settled tail + alpha sweep --------------------------

@pytest.mark.parametrize("alpha", [ALPHA_FAST, ALPHA_MID, ALPHA_SLOW])
def test_ff_settled_tail_vs_gr(alpha):
    """Settled-tail amplitude vs LIVE GR rms_ff (alpha sweep, derived warm-up,
    derived tolerance)."""
    warm = _warm(alpha)
    words = _random_words(5, warm + 256, -0.7, 0.7)
    dut = _run_ff(words, alpha)
    gr = _gr_rms_ff(words, alpha)
    res = compare_against_grc(dut.outputs_q15[warm:], _clip01(gr.floats)[warm:],
                              metric=Metric.AMPLITUDE, delay=0,
                              tolerance=TOL_LSB)
    print(f"\nff alpha={alpha:.6f} warm={warm}:", res.summary())
    assert res.passed, res.summary()


@pytest.mark.parametrize("alpha", [ALPHA_FAST, ALPHA_MID])
def test_cf_settled_tail_vs_gr(alpha):
    """Settled-tail amplitude vs LIVE GR rms_cf (complex stimulus)."""
    warm = _warm(alpha)
    pairs = _cf_pairs(6, warm + 256)
    dut = _run_cf(pairs, alpha)
    gr = _gr_rms_cf(pairs, alpha)
    res = compare_against_grc(_flat_cf(dut)[warm:], _clip01(gr.i)[warm:],
                              metric=Metric.AMPLITUDE, delay=0,
                              tolerance=TOL_LSB)
    print(f"\ncf alpha={alpha:.6f} warm={warm}:", res.summary())
    assert res.passed, res.summary()


def _step_words():
    """Amplitude step 0.35 -> 0.95 uniform: the transient window every
    dynamics-sensitive gate (and mutation) uses."""
    rng = np.random.default_rng(13)
    x = np.concatenate([rng.uniform(-0.35, 0.35, 80),
                        rng.uniform(-0.95, 0.95, 80)])
    return [_q15w(float(v)) for v in x]


def test_ff_step_response_vs_gr():
    """FULL-TRAJECTORY equivalence through an amplitude step (alpha exactly
    representable, so DUT and GR run the identical filter): the transient — not
    just the settled value — must track GR. This window is what gives the
    wrong-alpha and +1-delay mutations teeth."""
    words = _step_words()
    warm = _warm(ALPHA_STEP)
    dut = _run_ff(words, ALPHA_STEP)
    gr = _gr_rms_ff(words, ALPHA_STEP)
    res = compare_against_grc(dut.outputs_q15[warm:], _clip01(gr.floats)[warm:],
                              metric=Metric.AMPLITUDE, delay=0,
                              tolerance=TOL_LSB)
    print("\nff step:", res.summary())
    assert res.passed, res.summary()


def test_ff_default_alpha_quantization_deviation():
    """The documented HW-DEVIATION at GR's DEFAULT alpha=1e-4: the chip runs
    alpha_eff = 3/32768 (~9.155e-5, 8.4% slower). On a CONSTANT-amplitude input
    both filters settle to the SAME RMS (the settled value is alpha-independent),
    so the tail after the QUANTIZED-alpha warm-up must match GR within the
    derived tolerance — while a mid-TRANSIENT window must NOT match (the
    warm-up guard is load-bearing, see the mutation below)."""
    aq = _cls("RMSBlock")("probe").alpha_q15
    assert aq == 3
    warm = _warm(3 / 32768.0)               # the SLOWER (quantized) constant
    n = warm + 4000
    words = [_q15w(0.5)] * n
    dut = _run_ff(words, 0.0001)
    # LIVE GR golden — the constant vector is built IN the GR script (113k words
    # inline would overflow the subprocess argv limit; only the length travels).
    gr = run_gnuradio_ref(
        input_q15=words[:1],
        gnuradio_script="""
from gnuradio import gr, blocks
tb = gr.top_block()
src = blocks.vector_source_f([input_float[0]] * n_total)
op = blocks.rms_ff(alpha)
snk = blocks.vector_sink_f()
tb.connect(src, op); tb.connect(op, snk)
tb.run()
output_float = list(snk.data())
""",
        extra_args={"alpha": 0.0001, "n_total": n},
    )
    res = compare_against_grc(dut.outputs_q15[warm:], _clip01(gr.floats)[warm:],
                              metric=Metric.AMPLITUDE, delay=0,
                              tolerance=TOL_LSB)
    print(f"\nff default-alpha deviation (warm={warm}):", res.summary())
    assert res.passed, res.summary()
    # the warm-up guard has teeth: a mid-transient window (where the 8.4% time-
    # constant difference is at full effect) FAILS the same gate.
    mid0, mid1 = warm // 3, warm // 3 + 2000
    res_mid = compare_against_grc(dut.outputs_q15[mid0:mid1],
                                  _clip01(gr.floats)[mid0:mid1],
                                  metric=Metric.AMPLITUDE, delay=0,
                                  tolerance=TOL_LSB)
    assert not res_mid.passed, \
        "mid-transient must FAIL at quantized alpha — warm-up guard is inert!"


def test_ff_full_scale_saturation_corner():
    """x=-1.0 constant drive: |x|^2 saturates to 0x7FFF (HW-DEVIATION 2) and the
    output settles at sqrt(32767/32768) — bit-exact vs the block's own reference
    AND within the derived tolerance of GR's Q15-clipped sqrt(1.0)."""
    b = _cls("RMSBlock")("ref", alpha=ALPHA_FAST)
    words = [0x8000] * 60
    dut = _run_ff(words, ALPHA_FAST)
    got = [w & 0xFFFF if w is not None else None for w in dut.outputs_q15]
    assert got == b.process_reference_q15(words)
    assert abs(got[-1] - 32767) <= TOL_LSB       # vs GR clipped to Q15


def test_cf_full_scale_saturation_corner():
    """re=im=-1.0 constant: the power guard must SATURATE (0x7FFF), never wrap
    0x8000+0x8000 to zero — the tail must sit at the full-scale RMS."""
    b = _cls("RMSCFBlock")("ref", alpha=ALPHA_FAST)
    pairs = [(-1.0, -1.0)] * 60
    dut = _run_cf(pairs, ALPHA_FAST)
    got = _flat_cf(dut)
    rw, iw = _pairs_words(pairs)
    assert got == b.process_reference_q15(rw, iw)
    assert abs(got[-1] - 32767) <= TOL_LSB, \
        f"wrap-to-zero corner: settled {got[-1]}, expected ~full-scale"


def test_delay_is_zero():
    """The RMS output tracks its input sample-for-sample (delay 0) — asserted
    on the step window where a +1 lag is glaring (see the mutation)."""
    words = _step_words()
    warm = _warm(ALPHA_STEP)
    dut = _run_ff(words, ALPHA_STEP)
    gr = _gr_rms_ff(words, ALPHA_STEP)
    res = compare_against_grc(dut.outputs_q15[warm:], _clip01(gr.floats)[warm:],
                              metric=Metric.AMPLITUDE, delay=0,
                              tolerance=TOL_LSB)
    assert res.passed and res.delay_used == 0, res.summary()


# --- MANDATORY mutation tests (INV-4): the gate must DETECT corruption --------

def _iir_power_words(words, alpha):
    """The bit-exact IIR power stream y[n] (the NO-SQRT mutation source)."""
    b = _cls("RMSBlock")("m", alpha=alpha)
    aq = b.alpha_q15
    y = acclo = 0
    out = []
    for w in words:
        x = _s16(w)
        p = min(0x7FFF, (x * x) >> 15)
        inc = aq * (p - y)
        t = acclo + (inc & 0x7FFF)
        y = y + (inc >> 15) + (t >> 15)
        acclo = t & 0x7FFF
        out.append(y & 0xFFFF)
    return out


def test_mutation_wrong_alpha_fails():
    """A DUT at alpha=0.25 must FAIL vs a GR golden at alpha=0.0625 on the
    step-transient window (the settled tail alone is alpha-independent — the
    transient is where alpha lives)."""
    words = _step_words()
    warm = _warm(ALPHA_STEP)
    dut = _run_ff(words, ALPHA_STEP)
    gr = _gr_rms_ff(words, 2048 / 32768.0)          # wrong alpha golden
    res = compare_against_grc(dut.outputs_q15[warm:], _clip01(gr.floats)[warm:],
                              metric=Metric.AMPLITUDE, delay=0,
                              tolerance=TOL_LSB)
    assert not res.passed, "gate failed to detect a wrong alpha!"


def test_mutation_no_sqrt_fails():
    """A no-sqrt passthrough (the raw IIR power word) must FAIL."""
    words = _step_words()
    warm = _warm(ALPHA_STEP)
    mutated = _iir_power_words(words, ALPHA_STEP)
    gr = _gr_rms_ff(words, ALPHA_STEP)
    res = compare_against_grc(mutated[warm:], _clip01(gr.floats)[warm:],
                              metric=Metric.AMPLITUDE, delay=0,
                              tolerance=TOL_LSB)
    assert not res.passed, "gate failed to detect a no-sqrt passthrough!"


def test_mutation_x_not_squared_fails():
    """An averager run on |x| instead of x^2 must FAIL."""
    b = _cls("RMSBlock")("m", alpha=ALPHA_STEP)
    words = _step_words()
    warm = _warm(ALPHA_STEP)
    mutated = b._iir_sqrt_q15([abs(_s16(w)) for w in words])
    gr = _gr_rms_ff(words, ALPHA_STEP)
    res = compare_against_grc(mutated[warm:], _clip01(gr.floats)[warm:],
                              metric=Metric.AMPLITUDE, delay=0,
                              tolerance=TOL_LSB)
    assert not res.passed, "gate failed to detect an un-squared input!"


def test_mutation_one_sample_offset_fails():
    words = _step_words()
    warm = _warm(ALPHA_STEP)
    dut = _run_ff(words, ALPHA_STEP)
    gr = _gr_rms_ff(words, ALPHA_STEP)
    shifted = [0x0000] + list(dut.outputs_q15[:-1])
    res = compare_against_grc(shifted[warm:], _clip01(gr.floats)[warm:],
                              metric=Metric.AMPLITUDE, delay=0,
                              tolerance=TOL_LSB)
    assert not res.passed, "gate failed to detect a +1-sample latency error!"


def test_mutation_inverted_output_fails():
    words = _step_words()
    warm = _warm(ALPHA_STEP)
    dut = _run_ff(words, ALPHA_STEP)
    gr = _gr_rms_ff(words, ALPHA_STEP)
    mutated = [(0x10000 - (w or 0)) & 0xFFFF for w in dut.outputs_q15]
    res = compare_against_grc(mutated[warm:], _clip01(gr.floats)[warm:],
                              metric=Metric.AMPLITUDE, delay=0,
                              tolerance=TOL_LSB)
    assert not res.passed, "gate failed to detect an inverted output!"


def test_mutation_empty_output_fails():
    gr = _gr_rms_ff(_step_words(), ALPHA_STEP)
    res = compare_against_grc([], _clip01(gr.floats),
                              metric=Metric.AMPLITUDE, tolerance=TOL_LSB)
    assert not res.passed


def test_mutation_bitexact_gate_has_teeth():
    """The bit-exact gate must FAIL against a reference whose alpha coefficient
    is off by ONE LSB — proof the alpha word actually reaches the datapath."""
    words = _random_words(1, 96, -0.95, 0.95)
    dut = _run_ff(words, ALPHA_MID)
    got = [w & 0xFFFF if w is not None else None for w in dut.outputs_q15]
    wrong = _cls("RMSBlock")("m", alpha=(3277 + 1) / 32768.0)
    assert wrong.alpha_q15 == 3278
    assert got != wrong.process_reference_q15(words), \
        "a 1-LSB alpha perturbation must break bit-exactness!"


def test_cf_mutation_wrong_second_rail_fails():
    """A DUT fed a DIFFERENT imag stream must FAIL vs the true-stream golden.
    (Swapped rails is NOT a corruption: re^2+im^2 is symmetric.)"""
    pairs = _cf_pairs(21, 160, -0.8, 0.8)
    wrong_pairs = [(i, 0.6 * i - 0.2) for i, _ in pairs]   # bogus imag rail
    warm = _warm(ALPHA_STEP)
    dut = _run_cf(wrong_pairs, ALPHA_STEP)
    gr = _gr_rms_cf(pairs, ALPHA_STEP)
    res = compare_against_grc(_flat_cf(dut)[warm:], _clip01(gr.i)[warm:],
                              metric=Metric.AMPLITUDE, delay=0,
                              tolerance=TOL_LSB)
    assert not res.passed, "gate failed to detect a wrong second rail!"


def test_cf_mutation_imag_dropped_fails():
    """An averager run on re^2 alone (imag rail dropped) must FAIL."""
    b = _cls("RMSCFBlock")("m", alpha=ALPHA_STEP)
    pairs = _cf_pairs(22, 160, -0.8, 0.8)
    rw, _ = _pairs_words(pairs)
    zeros = [0] * len(rw)
    mutated = b.process_reference_q15(rw, zeros)
    warm = _warm(ALPHA_STEP)
    gr = _gr_rms_cf(pairs, ALPHA_STEP)
    res = compare_against_grc(mutated[warm:], _clip01(gr.i)[warm:],
                              metric=Metric.AMPLITUDE, delay=0,
                              tolerance=TOL_LSB)
    assert not res.passed, "gate failed to detect a dropped imag rail!"


def test_cf_mutation_no_sqrt_fails():
    pairs = _cf_pairs(23, 160, -0.8, 0.8)
    rw, iw = _pairs_words(pairs)
    b = _cls("RMSCFBlock")("m", alpha=ALPHA_STEP)
    # power stream (no sqrt): rebuild via the shared IIR on the cf powers
    powers = []
    for r, i in zip(rw, iw):
        R, I = _s16(r), _s16(i)
        p = min(0x7FFF, ((R * R) >> 15) + ((I * I) >> 15))
        powers.append(p)
    aq = b.alpha_q15
    y = acclo = 0
    mutated = []
    for p in powers:
        inc = aq * (p - y)
        t = acclo + (inc & 0x7FFF)
        y = y + (inc >> 15) + (t >> 15)
        acclo = t & 0x7FFF
        mutated.append(y & 0xFFFF)
    warm = _warm(ALPHA_STEP)
    gr = _gr_rms_cf(pairs, ALPHA_STEP)
    res = compare_against_grc(mutated[warm:], _clip01(gr.i)[warm:],
                              metric=Metric.AMPLITUDE, delay=0,
                              tolerance=TOL_LSB)
    assert not res.passed, "gate failed to detect a no-sqrt passthrough (cf)!"


# --- HW-DEVIATION surfacing / parameter validation ----------------------------

def test_alpha_out_of_range_raises():
    """HW-DEVIATION: alpha outside (0, 1] or quantizing to 0 must RAISE (never
    clamp silently) — both blocks."""
    for name in ("RMSBlock", "RMSCFBlock"):
        cls = _cls(name)
        for bad in (0.0, -0.1, 1.5, 1e-6):
            with pytest.raises(ValueError):
                cls("bad", alpha=bad)
        # boundary: the smallest representable alpha builds
        b = cls("ok", alpha=1 / 32768.0)
        assert b.alpha_q15 == 1


def test_sqrt_pipeline_exhaustive_bound():
    """The derived sqrt-path bound (<= 4.5 LSB) holds over ALL 32768 power
    words — the tolerance derivation's first term, verified, not assumed."""
    cls = _cls("RMSBlock")
    errs = []
    for y in range(0, 32768, 1):
        exact = math.sqrt(y / 32768.0) * 32768.0
        errs.append(cls._sqrt_q15(y) - exact)
    lo, hi = min(errs), max(errs)
    assert -4.5 <= lo and hi <= 1.0, (lo, hi)


def test_emit_reports():
    """Emit the dashboard reports for BOTH blocks (runs last, reflecting a
    passing verification)."""
    warm = _warm(ALPHA_MID)
    words = _random_words(5, warm + 256, -0.7, 0.7)
    dut = _run_ff(words, ALPHA_MID)
    gr = _gr_rms_ff(words, ALPHA_MID)
    res = compare_against_grc(dut.outputs_q15[warm:], _clip01(gr.floats)[warm:],
                              metric=Metric.AMPLITUDE, delay=0,
                              tolerance=TOL_LSB)
    assert res.passed, res.summary()
    write_report("RMSBlock", res, coverage={
        "edge": True, "random": 3, "alpha_sweep": 4, "bitexact": True,
        "step_response": True, "default_alpha_deviation": True,
        "saturation_corner": True, "mutation": True,
    })

    pairs = _cf_pairs(6, _warm(ALPHA_MID) + 256)
    dutc = _run_cf(pairs, ALPHA_MID)
    grc = _gr_rms_cf(pairs, ALPHA_MID)
    resc = compare_against_grc(_flat_cf(dutc)[warm:], _clip01(grc.i)[warm:],
                               metric=Metric.AMPLITUDE, delay=0,
                               tolerance=TOL_LSB)
    assert resc.passed, resc.summary()
    write_report("RMSCFBlock", resc, coverage={
        "edge": True, "random": 3, "alpha_sweep": 3, "bitexact": True,
        "saturation_corner": True, "wrap_corner": True, "mutation": True,
    })
