# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify AGCCCBlock is a Q15 drop-in for GNU Radio ``analog.agc_cc``.

GR SEMANTICS (pinned against LIVE GR before authoring — see the semantics-pin
test): per sample ``out = in * gain`` THEN ``gain += rate * (reference - |out|)``
(TRUE complex magnitude; the FIRST sample is scaled by the INITIAL gain), and
``if max_gain > 0: gain = min(gain, max_gain)``. No lower clamp in GR (the chip
clamps at 0 — unreachable in-regime, see the block docstring).

THE GATES:

  * LIVE semantics pin: the float model law matches ``analog.agc_cc`` to float
    precision (a golden that doesn't match GR proves nothing — INV-26).
  * BIT-EXACT vs ``process_reference_q15`` (the strongest gate: the V-pinned
    increment add + clamps, the 2-rail MULQ, the exact CORDIC magnitude chain
    on the emitted words, and the error-feedback accumulator, word-for-word)
    over edge + random (3 seeds) + the rate sweep {1e-4 (GR default), ~1e-2,
    ~5e-2} x reference sweep x gain/max_gain combos (clamp engaged + unlimited).
  * LIVE-GR settled-tail equivalence per rail, DERIVED warm-up + tolerance.
    REGIME MIRRORING (the agc_ff / audio_meter lesson): the GR golden runs at
    the CHIP-QUANTIZED constants (rate_q/ref_q/gain_q/gmax_q as floats, and
    max_gain=0 -> the Q15 ceiling 32767/32768) so both loops run the same law
    at the same operating point; the chip still takes the GR-verbatim floats.
  * Default-rate (1e-4) long-run: the bit-exact model (chip-linked above) vs
    LIVE GR at the quantized default rate 3/32768 over the full 137k-sample
    warm-up — python-side, the chip<->model link being the bit-exact gate.
  * INV-4 mutations, each proven to FAIL: inverted, wrong-reference,
    magnitude-approximation (|I|+|Q| instead of the true magnitude), no-feedback
    (frozen gain), +1 complex-sample delay, empty; plus 1-LSB rate perturbation
    breaks bit-exactness and a bare-MULQ increment (no error feedback) STALLS
    at the default rate.
  * INV-19 serialize-LOCK: saturated == per-sample bit-exact (the generic
    COMPLEX_2IN2OUT gate in test_pipeline_saturation.py) and the hazard is
    REAL — pipeline_lock=False diverges under saturated drive (pinned here).

DERIVED TOLERANCE (settled tail vs live GR — NOT tuned):
  CORDIC |.| error transfers 1:1 to the settled envelope
    (the loop settles where mag_chip == ref, so true |out| = ref - err):
    chain bound 20 LSB (measured max 19.7)                       <= 20 LSB
  error-feedback gain dither (+-1 gain LSB x |in| <= 1)          <=  1 LSB
  output MULQ truncation per rail                                <=  1 LSB
  warm-up residual after n_warm = ceil(10/(rate_eff*amp))
    (e^-10 of the initial gain offset)                           <=  1 LSB
  TOTAL -> 24 Q15 LSB. Measured peaks: 11 LSB (mean ~8).

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_agc_cc.py -q
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
    run_block_dut_complex, run_gnuradio_ref_complex, compare_against_grc,
    write_report, Metric)
from kyttar_verify.dut_runner import run_block_dut_pipelined  # noqa: E402
from gr_kyttar.placement.blocks import all_block_classes  # noqa: E402
from gr_kyttar.placement.blocks.cordic_blocks import cordic_mag_word  # noqa: E402

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")

_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON",
                                              "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_AVAILABLE),
    reason="chip yaml or GNU Radio interpreter absent")

# The DERIVED settled-tail tolerance (module docstring error budget). NOT tuned.
TOL_LSB = 24

# Exactly-Q15-representable fast/mid rates for the live settled-tail sweep
# (round(r*32768) reproduces them, so DUT and GR run the IDENTICAL loop gain).
RATE_FAST = 1638 / 32768.0      # ~5e-2
RATE_MID = 328 / 32768.0        # ~1e-2
RATE_DEFAULT = 1e-4             # GR default; quantizes to 3/32768


def _cls():
    return all_block_classes()["AGCCCBlock"]


def _q15w(x: float) -> int:
    q = int(round(x * 32768.0))
    return max(-32768, min(32767, q)) & 0xFFFF


def _s16(v) -> int:
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def _warm(rate: float, amp: float) -> int:
    """DERIVED warm-up: the linearized loop pole is (1 - rate_eff*amp); n =
    10/(rate_eff*amp) leaves an e^-10 residual (< 1 LSB of any in-range gain
    offset)."""
    rq = int(round(rate * 32768.0))
    return int(math.ceil(10.0 * 32768.0 / (max(1, rq) * amp)))


def _tone(n: int, amp: float, f: float = 0.037):
    """Constant-envelope rotating phasor — the canonical AGC stimulus (the
    envelope is what the loop regulates)."""
    return [complex(amp * math.cos(2 * math.pi * f * i),
                    amp * math.sin(2 * math.pi * f * i)) for i in range(n)]


def _words(stim) -> list[tuple[int, int]]:
    return [(_q15w(z.real), _q15w(z.imag)) for z in stim]


def _quantized_stim(stim):
    """The Q15-quantized stimulus as complex floats — feed GR EXACTLY what the
    chip ingests (no harness/bridge skew)."""
    return [complex(_s16(a) / 32768.0, _s16(b) / 32768.0)
            for (a, b) in _words(stim)]


def _gr_agc_cc(stim, rate, reference, gain, max_gain):
    """LIVE GNU Radio analog.agc_cc over the quantized stimulus."""
    return run_gnuradio_ref_complex(
        _quantized_stim(stim),
        gnuradio_script="""
from gnuradio import gr, blocks, analog
tb = gr.top_block()
src = blocks.vector_source_c(input_complex, False)
op = analog.agc_cc(rate, reference, gain, max_gain)
snk = blocks.vector_sink_c()
tb.connect(src, op, snk)
tb.run()
output_complex = list(snk.data())
""",
        extra_args={"rate": rate, "reference": reference,
                    "gain": gain, "max_gain": max_gain})


def _chip_quantized_params(params: dict) -> dict:
    """The CHIP's quantized constants as floats — the regime-mirrored GR golden
    params (max_gain=0 -> the Q15 ceiling; see the module docstring)."""
    b = _cls()("probe", **params)
    return dict(rate=b._rate_q15 / 32768.0,
                reference=b._reference_q15 / 32768.0,
                gain=b._gain_q15 / 32768.0,
                max_gain=b._max_gain_q15 / 32768.0)


def _run_dut(stim, params, **kw):
    dut = run_block_dut_complex("AGCCCBlock", stim, params=params,
                                chip_yaml=CHIP_YAML, in_ports=("xi", "xq"),
                                out_port="yi_tap", words_per_sample=2, **kw)
    assert dut.ok, dut.reason
    return dut


def _assert_bit_exact(stim, params):
    dut = _run_dut(stim, params)
    ref = _cls()("ref", **params).process_reference_q15(_words(stim))
    flat_dut = [w for g in dut.outputs_q15 for w in g]
    flat_ref = [w for p in ref for w in p]
    assert len(flat_dut) == len(flat_ref), \
        f"count: chip {len(flat_dut)} vs model {len(flat_ref)}"
    mism = [k for k in range(len(flat_ref)) if flat_dut[k] != flat_ref[k]]
    assert not mism, (
        f"chip diverges from the bit-exact model at word {mism[0]}: "
        f"chip=0x{flat_dut[mism[0]]:04X} model=0x{flat_ref[mism[0]]:04X} "
        f"({len(mism)}/{len(flat_ref)} words)")
    return dut


# --------------------------------------------------------------------------
# LIVE semantics pin (INV-26: prove the golden law IS GR before gating on it)
# --------------------------------------------------------------------------

def test_gr_semantics_pin_live():
    """The model law (out = in*gain; gain += rate*(ref - |out|); upper clamp
    only when max_gain > 0) matches LIVE analog.agc_cc to float precision —
    including first-sample-uses-initial-gain and max_gain=0 = unclamped."""
    stim = _tone(60, 0.8) + _tone(30, 0.3)
    for (rate, ref, g0, mg) in [(0.1, 0.3, 1.0, 1.0), (0.5, 0.5, 0.8, 0.0),
                                (0.1, 0.9, 0.2, 0.25)]:
        gr = _gr_agc_cc(stim, rate, ref, g0, mg)
        g = g0
        qs = _quantized_stim(stim)
        for k, z in enumerate(qs):
            o = z * g
            err = max(abs(o.real - gr.i[k]), abs(o.imag - gr.q[k]))
            assert err < 5e-6, (
                f"model law diverges from live agc_cc at {k} "
                f"(rate={rate}, ref={ref}, g0={g0}, mg={mg}): {err}")
            g += rate * (ref - abs(o))
            if mg > 0:
                g = min(g, mg)


# --------------------------------------------------------------------------
# BIT-EXACT chip vs process_reference_q15
# --------------------------------------------------------------------------

def test_bit_exact_edges():
    """Corner stimulus: full-scale rails (incl. the -1.0 corner), zeros, and a
    full-scale burst that drives the gain hard against both clamps."""
    stim = ([complex(1.0, 1.0), complex(-1.0, -1.0), complex(-1.0, 1.0),
             complex(0.0, 0.0), complex(1.0, 0.0), complex(0.0, -1.0)] * 4
            + [complex(0.0, 0.0)] * 8 + [complex(-1.0, 0.5)] * 8)
    _assert_bit_exact(stim, dict(rate=0.5, reference=0.3, gain=1.0,
                                 max_gain=0.0))
    _assert_bit_exact(stim, dict(rate=1.0, reference=1.0, gain=0.0,
                                 max_gain=0.1))


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_bit_exact_random(seed):
    rng = np.random.default_rng(seed)
    stim = [complex(a, b) for a, b in rng.uniform(-0.9, 0.9, (80, 2))]
    _assert_bit_exact(stim, dict(rate=0.05, reference=0.5, gain=1.0,
                                 max_gain=0.0))


@pytest.mark.parametrize("rate", [RATE_DEFAULT, RATE_MID, RATE_FAST])
@pytest.mark.parametrize("reference", [0.2, 0.7])
def test_bit_exact_rate_reference_sweep(rate, reference):
    """The dispatch sweep: rate {1e-4, ~1e-2, ~5e-2} x reference — bit-exact
    at every point (the GR default rate=1e-4 exercises the error-feedback
    accumulator's sub-LSB regime on-chip)."""
    stim = _tone(60, 0.8)
    _assert_bit_exact(stim, dict(rate=rate, reference=reference, gain=1.0,
                                 max_gain=0.0))


@pytest.mark.parametrize("gain,max_gain", [(1.0, 0.0), (0.6, 0.5), (0.2, 1.0)])
def test_bit_exact_gain_maxgain(gain, max_gain):
    """Initial-gain / clamp combos: unlimited, clamp-engaged from below, and a
    small initial gain rising toward the ceiling."""
    stim = _tone(60, 0.6)
    _assert_bit_exact(stim, dict(rate=0.05, reference=0.8, gain=gain,
                                 max_gain=max_gain))


# --------------------------------------------------------------------------
# LIVE-GR settled-tail equivalence (derived warm-up + tolerance)
# --------------------------------------------------------------------------

def _settled_vs_gr(params, amp, tail=150):
    qp = _chip_quantized_params(params)
    warm = _warm(qp["rate"], amp)
    stim = _tone(warm + tail, amp)
    dut = _run_dut(stim, params)
    gr = _gr_agc_cc(stim, **qp)
    res_i = compare_against_grc(dut.i_q15, gr.i, metric=Metric.AMPLITUDE,
                                delay=0, tolerance=TOL_LSB, head_shift=warm)
    res_q = compare_against_grc(dut.q_q15, gr.q, metric=Metric.AMPLITUDE,
                                delay=0, tolerance=TOL_LSB, head_shift=warm)
    return dut, res_i, res_q, warm


@pytest.mark.parametrize("rate,reference,amp", [
    (RATE_FAST, 0.3, 0.8),
    (RATE_FAST, 0.5, 0.9),
    (RATE_MID, 0.2, 0.6),
])
def test_settled_tail_vs_live_gr(rate, reference, amp):
    """The on-chip settled envelope matches LIVE agc_cc per rail within the
    derived 24-LSB budget after the derived warm-up."""
    params = dict(rate=rate, reference=reference, gain=1.0, max_gain=0.0)
    dut, res_i, res_q, warm = _settled_vs_gr(params, amp)
    print(f"\nrate={rate:.6f} ref={reference}: warm={warm}",
          "I:", res_i.summary(), "Q:", res_q.summary())
    assert res_i.passed, f"I rail: {res_i.summary()}"
    assert res_q.passed, f"Q rail: {res_q.summary()}"


def test_settled_tail_clamped_vs_live_gr():
    """max_gain engaged (the reference is unreachable): both loops pin at the
    clamp and the output is gain-clamp exact (sub-LSB — measured 1)."""
    params = dict(rate=RATE_FAST, reference=0.8, gain=0.5, max_gain=0.5)
    dut, res_i, res_q, warm = _settled_vs_gr(params, 0.5)
    assert res_i.passed and res_q.passed, (res_i.summary(), res_q.summary())


def test_default_rate_settled_model_vs_live_gr():
    """GR's DEFAULT rate=1e-4 (quantized 3/32768): the full 137k-sample
    convergence, model vs LIVE GR (the chip<->model link is the bit-exact gate
    above — a 137k-sample chip run is not needed to close the chain). This is
    exactly the regime where a bare-MULQ increment stalls a third of full scale
    short; the error-feedback accumulator tracks GR to the settled budget."""
    import json
    import tempfile

    params = dict(rate=RATE_DEFAULT, reference=0.3, gain=1.0, max_gain=0.0)
    qp = _chip_quantized_params(params)
    amp = 0.8
    warm = _warm(qp["rate"], amp)          # ~137k samples
    tail = 500
    stim = _tone(warm + tail, amp)
    model = _cls()("m", **params).process_reference_q15(_words(stim))
    # 137k samples exceed the subprocess argv limit — hand GR the quantized
    # stimulus via a temp file (bit-identical to what the model consumed).
    qs = _quantized_stim(stim)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump([[z.real, z.imag] for z in qs], f)
        stim_path = f.name
    try:
        gr = run_gnuradio_ref_complex(
            qs[:4],  # dummy; the script loads the real stimulus from the file
            gnuradio_script="""
import json as _json
with open(stim_path) as _f:
    input_complex = [complex(a, b) for (a, b) in _json.load(_f)]
from gnuradio import gr, blocks, analog
tb = gr.top_block()
src = blocks.vector_source_c(input_complex, False)
op = analog.agc_cc(rate, reference, gain, max_gain)
snk = blocks.vector_sink_c()
tb.connect(src, op, snk)
tb.run()
output_complex = list(snk.data())
""",
            extra_args=dict(stim_path=stim_path, **qp), timeout=600)
    finally:
        os.unlink(stim_path)
    worst = 0
    for k in range(warm, warm + tail):
        yi, yq = _s16(model[k][0]), _s16(model[k][1])
        worst = max(worst,
                    abs(yi - int(round(gr.i[k] * 32768.0))),
                    abs(yq - int(round(gr.q[k] * 32768.0))))
    print(f"\ndefault-rate settled: warm={warm} worst={worst} LSB")
    assert worst <= TOL_LSB, \
        f"default-rate settled tail {worst} LSB > {TOL_LSB}"


# --------------------------------------------------------------------------
# INV-19: the serialize-LOCK is load-bearing (hazard pinned)
# --------------------------------------------------------------------------

def test_unlocked_saturated_drive_diverges():
    """pipeline_lock=False under SATURATED drive diverges from per-sample (the
    gain feedback races open-loop — the Costas dphase failure shape). This pins
    the hazard the default lock exists to fix; the locked block's saturated ==
    per-sample bit-exactness is the generic COMPLEX_2IN2OUT gate in
    test_pipeline_saturation.py."""
    stim = _tone(40, 0.8, f=0.05)
    params = dict(rate=0.05, reference=0.3, gain=1.0, max_gain=0.0,
                  pipeline_lock=False)
    seq = _run_dut(stim, params)
    seq_out = [w for g in seq.outputs_q15 for w in g]
    samples = _words(stim)
    pipe = run_block_dut_pipelined("AGCCCBlock", samples, params=params,
                                   chip_yaml=CHIP_YAML, in_ports=("xi", "xq"),
                                   out_port="yi_tap", max_events=8_000_000)
    assert pipe.ok, pipe.reason
    n = len(seq_out)
    diffs = sum(1 for k in range(min(n, len(pipe.outputs_q15)))
                if pipe.outputs_q15[k] != seq_out[k])
    assert diffs > n // 4, (
        "unlocked saturated drive unexpectedly matched per-sample — the "
        f"serialize-LOCK hazard did not reproduce ({diffs}/{n} diffs)")


# --------------------------------------------------------------------------
# Cell memory budget (32-word cells; INV-17 fan-out headroom on the tap)
# --------------------------------------------------------------------------

def test_cell_budget_and_fanout_headroom():
    from gr_kyttar.placement.resolver import CellProgramResolver
    blk = _cls()("probe")
    cps = blk.build_cell_programs()
    r = CellProgramResolver()
    for cid, cp in cps.items():
        n_instr = r.count_instructions(cp)
        n_regs = len(cp.inputs) + len(cp.data or ()) + len(cp.state or ())
        used = n_instr + n_regs
        assert used <= 32, f"{cid}: {used}/32 words — over budget"
        if cid == "tap":
            assert 32 - used >= 1, (
                f"tap: {used}/32 words — no room for the INV-17 fan-out JUMP")


# --------------------------------------------------------------------------
# Parameter-domain guards (HW-DEVIATION raises, never silent clamps)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    dict(gain=1.5), dict(reference=1.2), dict(max_gain=2.0),
    dict(rate=0.0), dict(rate=1e-6), dict(gain=-0.1), dict(reference=0.0),
])
def test_out_of_regime_params_raise(bad):
    params = dict(rate=0.05, reference=0.3, gain=1.0, max_gain=0.0)
    params.update(bad)
    with pytest.raises(ValueError):
        _cls()("probe", **params)


# --------------------------------------------------------------------------
# MANDATORY mutations (INV-4): the gate must DETECT real corruptions
# --------------------------------------------------------------------------

_MUT_PARAMS = dict(rate=RATE_FAST, reference=0.3, gain=1.0, max_gain=0.0)
_MUT_AMP = 0.8


def _mut_setup():
    qp = _chip_quantized_params(_MUT_PARAMS)
    warm = _warm(qp["rate"], _MUT_AMP)
    stim = _tone(warm + 150, _MUT_AMP)
    gr = _gr_agc_cc(stim, **qp)
    return stim, gr, warm


def _rails(pairs):
    return ([p[0] for p in pairs], [p[1] for p in pairs])


def _passes(i_ch, q_ch, gr, warm):
    res_i = compare_against_grc(i_ch, gr.i, metric=Metric.AMPLITUDE, delay=0,
                                tolerance=TOL_LSB, head_shift=warm)
    res_q = compare_against_grc(q_ch, gr.q, metric=Metric.AMPLITUDE, delay=0,
                                tolerance=TOL_LSB, head_shift=warm)
    return res_i.passed and res_q.passed


def _model_variant(stim, mag_fn=None, freeze_gain=False):
    """The bit-exact model with an injected corruption: ``mag_fn`` replaces the
    CORDIC magnitude; ``freeze_gain`` kills the feedback."""
    b = _cls()("m", **_MUT_PARAMS)
    g = b._gain_q15
    gmax = b._max_gain_q15
    ref_q = b._reference_q15
    rate_q = b._rate_q15
    acclo = 0
    ginc = 0
    out = []
    for (xi, xq) in _words(stim):
        if not freeze_gain:
            s = g + ginc
            if s > 32767:
                s = gmax
            s = min(s, gmax)
            g = max(s, 0)
        yi = (_s16(xi) * g) >> 15
        yq = (_s16(xq) * g) >> 15
        out.append((yi & 0xFFFF, yq & 0xFFFF))
        m = (mag_fn or cordic_mag_word)(yi & 0xFFFF, yq & 0xFFFF)
        prod = rate_q * (ref_q - m)
        hi, lo = prod >> 15, prod & 0x7FFF
        t = acclo + lo
        ginc, acclo = hi + (t >> 15), t & 0x7FFF
    return out


def test_mutation_model_sanity_passes():
    """The TRUE bit-exact model passes the live gate (so the mutation failures
    below are attributable to the corruption, not the gate)."""
    stim, gr, warm = _mut_setup()
    i_ch, q_ch = _rails(_model_variant(stim))
    assert _passes(i_ch, q_ch, gr, warm), "true model failed its own gate"


def test_mutation_magnitude_approximation_fails():
    """|I| + |Q| instead of the TRUE magnitude (the classic cheap-AGC shortcut,
    ~27% high on a rotating phasor -> the loop settles ~27% low) must FAIL."""
    def _l1(yi, yq):
        return min(0x7FFF, abs(_s16(yi)) + abs(_s16(yq)))
    stim, gr, warm = _mut_setup()
    i_ch, q_ch = _rails(_model_variant(stim, mag_fn=_l1))
    assert not _passes(i_ch, q_ch, gr, warm), \
        "gate failed to detect the |I|+|Q| magnitude approximation!"


def test_mutation_no_feedback_fails():
    """A frozen gain (the loop never updates) must FAIL."""
    stim, gr, warm = _mut_setup()
    i_ch, q_ch = _rails(_model_variant(stim, freeze_gain=True))
    assert not _passes(i_ch, q_ch, gr, warm), \
        "gate failed to detect a dead gain loop!"


def test_mutation_inverted_fails():
    stim, gr, warm = _mut_setup()
    pairs = _model_variant(stim)
    i_ch = [(0x10000 - w) & 0xFFFF for (w, _) in pairs]
    q_ch = [(0x10000 - w) & 0xFFFF for (_, w) in pairs]
    assert not _passes(i_ch, q_ch, gr, warm), \
        "gate failed to detect an inverted output!"


def test_mutation_plus_one_delay_fails():
    stim, gr, warm = _mut_setup()
    pairs = _model_variant(stim)
    i_ch, q_ch = _rails([pairs[0]] + pairs[:-1])
    assert not _passes(i_ch, q_ch, gr, warm), \
        "gate failed to detect a +1 complex-sample delay!"


def test_mutation_wrong_reference_fails():
    """A run at reference=0.3 must FAIL a reference=0.6 golden."""
    stim, gr_unused, warm = _mut_setup()
    qp = _chip_quantized_params(dict(_MUT_PARAMS, reference=0.6))
    gr_wrong = _gr_agc_cc(stim, **qp)
    i_ch, q_ch = _rails(_model_variant(stim))
    assert not _passes(i_ch, q_ch, gr_wrong, warm), \
        "gate failed to detect a wrong reference level!"


def test_mutation_empty_fails():
    stim, gr, warm = _mut_setup()
    assert not _passes([], [], gr, warm)


def test_mutation_rate_one_lsb_breaks_bit_exactness():
    """A 1-LSB rate perturbation must break the bit-exact link (proves the
    bit-exact gate sees the loop constant, not just the pass-through)."""
    stim = _tone(120, 0.8)
    b = _cls()("m", **_MUT_PARAMS)
    good = b.process_reference_q15(_words(stim))
    b2 = _cls()("m", **_MUT_PARAMS)
    b2._rate_q15 += 1
    bad = b2.process_reference_q15(_words(stim))
    assert good != bad, "1-LSB rate perturbation did not change the output"


def test_mutation_bare_mulq_increment_stalls_at_default_rate():
    """WITHOUT the error-feedback accumulator, a bare-MULQ increment truncates
    (floor) to 0 for every POSITIVE err < 2^15/rate_q — at the GR default rate
    (rate_q=3) a gain that must RISE toward the settled point freezes with the
    envelope thousands of LSB short of the reference. (The falling direction
    self-repairs — floor gives -1 for any negative err — which is why the
    stall needs the rising regime: gain starts BELOW ref/amp.) Proves the
    error-feedback accumulator is load-bearing, not decoration."""
    params = dict(rate=RATE_DEFAULT, reference=0.3, gain=0.05, max_gain=0.0)
    b = _cls()("m", **params)
    rate_q, ref_q, gmax = b._rate_q15, b._reference_q15, b._max_gain_q15
    amp = 0.8
    n = _warm(rate_q / 32768.0, amp) + 200
    stim = _tone(n, amp)
    g = b._gain_q15
    stall_env = None
    for (xi, xq) in _words(stim):
        yi = (_s16(xi) * g) >> 15
        yq = (_s16(xq) * g) >> 15
        m = cordic_mag_word(yi & 0xFFFF, yq & 0xFFFF)
        inc = (rate_q * (ref_q - m)) >> 15       # bare MULQ, no accumulator
        g = max(0, min(gmax, g + inc))
        stall_env = m
    true_pairs = b.process_reference_q15(_words(stim))
    true_env = cordic_mag_word(true_pairs[-1][0], true_pairs[-1][1])
    assert abs(true_env - ref_q) <= 64, \
        f"true model did not settle at the reference ({true_env} vs {ref_q})"
    assert abs(stall_env - ref_q) > 1000, (
        f"bare-MULQ increment unexpectedly reached the reference "
        f"({stall_env} vs {ref_q}) — the error-feedback rationale is wrong")


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def test_emit_report():
    params = dict(rate=RATE_FAST, reference=0.3, gain=1.0, max_gain=0.0)
    dut, res_i, res_q, warm = _settled_vs_gr(params, 0.8)
    write_report("AGCCCBlock", res_i, coverage={
        "bit_exact": "edges + 3 seeds + rate{1e-4,1e-2,5e-2} x ref{0.2,0.7} "
                     "+ gain/max_gain combos, chip == model word-for-word",
        "live_gr_settled": "rate x ref x amp sweep + clamped, derived warm-up, "
                           "derived 24-LSB budget, both rails",
        "default_rate": "137k-sample model-vs-GR settled (chip linked bit-exact)",
        "mutations": "inverted, wrong-reference, |I|+|Q| magnitude, "
                     "no-feedback, +1 delay, empty, 1-LSB rate, bare-MULQ stall"
                     " — all FAIL",
        "saturation": "COMPLEX_2IN2OUT locked gate green; unlocked divergence "
                      "pinned",
        "regime": "attenuating (gain/reference/max_gain <= 1, Q15); "
                  "out-of-regime params raise"})
    assert res_i.passed and res_q.passed
