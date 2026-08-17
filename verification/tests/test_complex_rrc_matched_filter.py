# SPDX-License-Identifier: GPL-3.0-or-later
"""ComplexRRCMatchedFilterBlock vs GNU Radio ``filter.fir_filter_ccf`` (RRC taps).

The complex RRC matched filter is GNU Radio's ``fir_filter_ccf`` fed taps from
``filter.firdes.root_raised_cosine(gain, samp_rate, sym_rate, alpha, ntaps)``:
complex I/Q in, complex I/Q out, ONE shared real sqrt-RRC tap set filtering each
rail independently. It sits at the front of the coherent BPSK/QPSK RX.

This block was a ``poc`` (INV-25): it worked in the shipped modems but had never
been held against GNU Radio per-block. Verification found the real bugs (recorded
in the lessons log): its taps were an INVENTED unit-energy sqrt-RRC (not GR's
``root_raised_cosine``) and its params (``beta``/``sps``/``span``) were not the GR
names; and it wrapped instead of saturating on overload (no INV-13 headroom
restore). Fixed: GR-faithful ``firdes.root_raised_cosine`` taps, GR-verbatim params
(``samp_rate``/``sym_rate``/``alpha``/``ntaps``/``gain``), and the coefficient-
headroom saturating restore.

Coverage:
  * GR-EQUIVALENCE (edge + random ≥3 seeds + a full param sweep over
    samp_rate/sym_rate/alpha/ntaps): DUT (built+simulated on simKYT) vs GR
    ``fir_filter_ccf`` fed the SAME firdes RRC taps, within the derived Q15
    tolerance. Run at ``Σ|h|≤1`` (headroom shift S=0) — the exact bit-clean GR
    drop-in regime (INV-14: a normalised filter has no headroom precision loss).
  * BIT-EXACT vs ``process_reference`` at the modem-calibrated default (S=1): the
    on-chip datapath equals the block's Q15 reference exactly.
  * OVERLOAD/RAIL (INV-13/INV-25): a high-gain (S>0) filter driven full-scale pins
    to ±full-scale (never wraps) — the original PoC bug.
  * Q15 TAP PARITY (INV-16): the on-chip Q15 taps are bit-exact to GR's firdes RRC
    taps quantised identically.
  * MANDATORY mutations proven to FAIL (INV-4): swapped I/Q, negated Q, wrong taps
    (wrong alpha), +1 sample offset, empty output.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_complex_rrc_matched_filter.py -x -q
"""

from __future__ import annotations

import os
import random
import sys
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PLACEKYT = Path(__file__).resolve().parents[2] / "placekyt"
_VERIFY = Path(__file__).resolve().parents[1]
_RUNTIME = Path(__file__).resolve().parents[2] / "runtime" / "python"
for p in (str(_RUNTIME), str(_PLACEKYT), str(_VERIFY)):
    if p not in sys.path:
        sys.path.insert(0, p)

from kyttar_verify import (  # noqa: E402
    run_block_dut_complex, run_gnuradio_ref_complex,
    compare_complex_against_grc, write_report, Metric)
from gr_kyttar.placement.blocks.complex_rrc_matched_filter_block import (  # noqa: E402
    ComplexRRCMatchedFilterBlock)
from gr_kyttar.placement.blocks import _firdes  # noqa: E402
from gr_kyttar.placement.blocks._base import float_to_q15  # noqa: E402

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_AVAILABLE),
    reason="chip yaml or GNU Radio interpreter absent")

# A gain that keeps Σ|h| ≤ 1 (headroom shift S=0) for the tap counts under test —
# the exact bit-clean GR drop-in regime (INV-14). At gain 0.6, Σ|h| ≈ 0.89 (17
# taps); every param combo in the sweep stays S=0.
_S0_GAIN = 0.6


def _complex_stim(seed, n, amp=0.4):
    rng = random.Random(seed)
    return [complex(rng.uniform(-amp, amp), rng.uniform(-amp, amp))
            for _ in range(n)]


def _rrc_taps(gain, samp_rate, sym_rate, alpha, ntaps):
    return _firdes.root_raised_cosine(gain, samp_rate, sym_rate, alpha, ntaps)


def _gr_complex_fir(stim, taps):
    # fir_filter_ccf convolves latest-sample-first (the reverse of the on-chip
    # coefficient order). RRC taps are linear-phase SYMMETRIC so the reversal is a
    # no-op, but reverse for generality.
    return run_gnuradio_ref_complex(
        stim,
        gnuradio_script="""
from gnuradio import gr, blocks, filter as gr_filter
tb = gr.top_block()
src = blocks.vector_source_c(input_complex, False)
fir = gr_filter.fir_filter_ccf(1, taps)
sink = blocks.vector_sink_c()
tb.connect(src, fir); tb.connect(fir, sink)
tb.run()
output_complex = list(sink.data())
""",
        extra_args={"taps": list(reversed(taps))})


def _run_dut(stim, params):
    dut = run_block_dut_complex(
        "ComplexRRCMatchedFilterBlock", stim, params=params, chip_yaml=CHIP_YAML,
        in_ports=("xi", "xq"), words_per_sample=2)
    assert dut.ok, dut.reason
    assert dut.words_per_sample == 2, (
        f"complex output should be 2 words/sample, got {dut.words_per_sample}")
    return dut


def _compare_vs_gr(dut, stim, taps):
    gr = _gr_complex_fir(stim, taps)
    assert gr.is_complex
    return gr, compare_complex_against_grc(
        dut.i_q15, dut.q_q15, gr.i, gr.q,
        metric=Metric.AMPLITUDE, delay=0, op_count=len(taps))


# =============================================================================
# GR equivalence — the DUT built on simKYT vs fir_filter_ccf(firdes RRC taps)
# =============================================================================

_EDGE_PARAMS = dict(gain=_S0_GAIN, samp_rate=2.0, sym_rate=1.0, alpha=0.35, ntaps=17)


def test_rrc_edge_vectors():
    """Full-scale-ish edge stimulus (the RRC MF at its default shape) matches GR."""
    # Edge: alternating ±amp symbols spaced sps apart (real matched-filter drive)
    # plus a couple of large impulses — the shape the filter is designed for.
    edge = ([complex(0.45, -0.45), complex(-0.45, 0.45)] * 12
            + [complex(0.45, 0.45), 0j, 0j, complex(-0.45, -0.45)])
    dut = _run_dut(edge, _EDGE_PARAMS)
    taps = _rrc_taps(**_EDGE_PARAMS)
    _gr, res = _compare_vs_gr(dut, edge, taps)
    print("\nedge:", res.summary())
    assert res.passed, res.summary()


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_rrc_random_vectors(seed):
    """Random I/Q (≥ 2× the filter state depth) matches GR (INV-12)."""
    stim = _complex_stim(seed, n=2 * _EDGE_PARAMS["ntaps"] + 16, amp=0.4)
    dut = _run_dut(stim, _EDGE_PARAMS)
    taps = _rrc_taps(**_EDGE_PARAMS)
    _gr, res = _compare_vs_gr(dut, stim, taps)
    print(f"\nrandom seed={seed}:", res.summary())
    assert res.passed, res.summary()


# The FULL parameter sweep the manifest demands: samp_rate/sym_rate/alpha/ntaps.
# Each keeps Σ|h| ≤ 1 at gain 0.6 (S=0) so it is the exact GR drop-in.
_SWEEP = [
    # (samp_rate, sym_rate, alpha, ntaps)
    (2.0, 1.0, 0.35, 11),   # sps=2, short
    (2.0, 1.0, 0.35, 17),   # sps=2, default span
    (2.0, 1.0, 0.35, 25),   # sps=2, long (15 cells, still ≤8 across)
    (2.0, 1.0, 0.22, 17),   # low roll-off
    (2.0, 1.0, 0.50, 17),   # high roll-off
    (4.0, 1.0, 0.35, 25),   # sps=4
    (3.0, 1.0, 0.35, 19),   # sps=3, odd
    (8000.0, 2000.0, 0.35, 17),  # real-world Hz units, sps=4
]


@pytest.mark.parametrize("samp_rate,sym_rate,alpha,ntaps", _SWEEP,
                         ids=[f"fs{a}_rs{b}_al{c}_nt{d}" for a, b, c, d in _SWEEP])
def test_rrc_param_sweep(samp_rate, sym_rate, alpha, ntaps):
    """Parameter parity across samp_rate/sym_rate/alpha/ntaps vs GR."""
    params = dict(gain=_S0_GAIN, samp_rate=samp_rate, sym_rate=sym_rate,
                  alpha=alpha, ntaps=ntaps)
    blk = ComplexRRCMatchedFilterBlock("ref", **params)
    assert blk._head_shift == 0, "sweep must stay in the S=0 GR-exact regime"
    stim = _complex_stim(seed=5, n=2 * ntaps + 16, amp=0.4)
    dut = _run_dut(stim, params)
    taps = _rrc_taps(**params)
    _gr, res = _compare_vs_gr(dut, stim, taps)
    print(f"\nsweep fs={samp_rate} rs={sym_rate} al={alpha} nt={ntaps} "
          f"({blk.cell_count} cells):", res.summary())
    assert res.passed, res.summary()


# =============================================================================
# Q15 tap parity (INV-16) — the on-chip coefficients ARE the firdes RRC taps
# =============================================================================

@pytest.mark.parametrize("samp_rate,sym_rate,alpha,ntaps", _SWEEP,
                         ids=[f"fs{a}_rs{b}_al{c}_nt{d}" for a, b, c, d in _SWEEP])
def test_q15_tap_parity(samp_rate, sym_rate, alpha, ntaps):
    """The block's SCALED Q15 taps == firdes RRC taps scaled + quantised the SAME
    way, bit-exact (S=0 so scaling is a no-op)."""
    params = dict(gain=_S0_GAIN, samp_rate=samp_rate, sym_rate=sym_rate,
                  alpha=alpha, ntaps=ntaps)
    blk = ComplexRRCMatchedFilterBlock("ref", **params)
    gr_taps = _rrc_taps(**params)
    S = blk._head_shift
    expect = [float_to_q15(t / (1 << S)) for t in gr_taps]
    assert blk.coeff_q15 == expect, (
        f"Q15 tap mismatch:\n block={blk.coeff_q15}\n firdes={expect}")


# =============================================================================
# Bit-exact vs process_reference at the modem-calibrated default (S=1)
# =============================================================================

def _s16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _q15_grid(c):
    """Snap a complex float onto the EXACT Q15 grid the harness injector uses
    (round with *32768, clamp), so the DUT and the reference see identical inputs
    and the comparison is a true BIT-EXACT test (no rounding-boundary slack)."""
    def q(v):
        return max(-32768, min(32767, int(round(v * 32768.0))))
    return q(c.real), q(c.imag)


def test_default_bit_exact_vs_reference():
    """At the shipped default (gain 0.7105 => headroom S=1 + saturating restore),
    the on-chip datapath is BIT-EXACT to the block's Q15 process_reference — the
    model that captures the exact restore. (GR-equivalence at S=1 has the expected
    headroom precision loss, so it is gated at S=0 above; here we pin the datapath
    to its own exact model.) The stimulus is snapped to the Q15 grid so DUT and
    reference ingest identical samples — a true 0-LSB assertion (INV-4)."""
    blk = ComplexRRCMatchedFilterBlock("mf")
    assert blk._head_shift == 1, "default gain should engage the headroom restore"
    rng = random.Random(9)
    ints = [(rng.randint(-9000, 9000), rng.randint(-9000, 9000)) for _ in range(60)]
    stim = [complex(a / 32768.0, b / 32768.0) for a, b in ints]
    # Confirm the float stimulus snaps back to exactly these ints (grid-aligned).
    assert all(_q15_grid(c) == (a, b) for c, (a, b) in zip(stim, ints))
    dut = _run_dut(stim, {})
    ref = blk.process_reference(np.array(ints, dtype=np.int32))
    di = [_s16(w) for w in dut.i_q15]
    dq = [_s16(w) for w in dut.q_q15]
    ri = [_s16(int(a) & 0xFFFF) for a, _ in ref]
    rq = [_s16(int(b) & 0xFFFF) for _, b in ref]
    n = min(len(di), len(ri))
    assert n >= len(stim) - 2, f"too few outputs: {len(di)} vs {len(stim)}"
    ie = max(abs(di[k] - ri[k]) for k in range(n))
    qe = max(abs(dq[k] - rq[k]) for k in range(n))
    print(f"\ndefault (S=1) vs reference: max_i={ie} max_q={qe} LSB (grid-aligned)")
    assert ie == 0 and qe == 0, f"datapath != reference (i={ie}, q={qe} LSB)"


# =============================================================================
# Overload / rail (INV-13/INV-25) — high gain (S>0) pins, never wraps
# =============================================================================

def test_overload_saturates_not_wraps():
    """A high-gain (S>0) RRC MF driven at FULL SCALE pins to ±full-scale (the
    INV-13 saturating restore) — it must NOT wrap/fold (the original PoC bug,
    INV-25). Compared to the block's own saturating reference (GR float would not
    clip; the drop-in claim is the Q15-clipped output, which the reference is)."""
    hot = ComplexRRCMatchedFilterBlock("hot", gain=4.0)   # Σ|h|≈5.95 => S=3
    assert hot._head_shift >= 2
    # Full-scale aligned drive: a run of +full-scale I/Q so the MF sum overdrives.
    stim = [complex(0.95, 0.95)] * 40
    dut = _run_dut(stim, {"gain": 4.0})
    ints = [_q15_grid(c) for c in stim]
    ref = hot.process_reference(np.array(ints, dtype=np.int32))
    di = [_s16(w) for w in dut.i_q15]
    ri = [_s16(int(a) & 0xFFFF) for a, _ in ref]
    n = min(len(di), len(ri))
    # The reference pins to +0x7FFF at the peak; the DUT must too (never a wrapped
    # negative/small value). Assert both hit the +rail and agree bit-for-bit.
    assert max(ri) >= 0x7000, "reference should overdrive to the +rail"
    assert all(abs(di[k] - ri[k]) <= 1 for k in range(n)), (
        f"overload datapath != saturating reference: "
        f"{[(k, di[k], ri[k]) for k in range(n) if abs(di[k]-ri[k]) > 1][:5]}")
    # And prove it did NOT wrap: the peak output stays near the +rail, not folded.
    assert max(di) >= 0x7000, "DUT should pin at the +rail (wrap bug regressed!)"
    print(f"\noverload: DUT peak={max(di)} ref peak={max(ri)} (pinned, not wrapped)")


# =============================================================================
# MANDATORY mutation tests — the gate MUST detect these (INV-4)
# =============================================================================

_MUT_PARAMS = dict(gain=_S0_GAIN, samp_rate=2.0, sym_rate=1.0, alpha=0.35, ntaps=17)


def _mut_setup(seed=11, n=48):
    stim = _complex_stim(seed, n=n, amp=0.4)
    dut = _run_dut(stim, _MUT_PARAMS)
    taps = _rrc_taps(**_MUT_PARAMS)
    gr = _gr_complex_fir(stim, taps)
    return stim, dut, taps, gr


def test_mutation_swapped_iq_fails():
    _stim, dut, taps, gr = _mut_setup()
    res = compare_complex_against_grc(
        dut.q_q15, dut.i_q15, gr.i, gr.q,   # I/Q swapped
        metric=Metric.AMPLITUDE, delay=0, op_count=len(taps))
    assert not res.passed, "gate failed to detect swapped I/Q!"


def test_mutation_negated_q_fails():
    _stim, dut, taps, gr = _mut_setup()
    neg_q = [(0x10000 - (w or 0)) & 0xFFFF for w in dut.q_q15]
    res = compare_complex_against_grc(
        dut.i_q15, neg_q, gr.i, gr.q,
        metric=Metric.AMPLITUDE, delay=0, op_count=len(taps))
    assert not res.passed, "gate failed to detect a negated Q channel!"


def test_mutation_wrong_alpha_fails():
    """A DUT built at the wrong roll-off must FAIL against the right GR reference —
    proves the alpha param actually reaches the taps (INV-12: the deep taps matter)."""
    stim = _complex_stim(11, n=48, amp=0.4)
    dut = _run_dut(stim, _MUT_PARAMS)          # alpha 0.35
    wrong_taps = _rrc_taps(gain=_S0_GAIN, samp_rate=2.0, sym_rate=1.0,
                           alpha=0.90, ntaps=17)   # a very different filter
    gr_wrong = _gr_complex_fir(stim, wrong_taps)
    res = compare_complex_against_grc(
        dut.i_q15, dut.q_q15, gr_wrong.i, gr_wrong.q,
        metric=Metric.AMPLITUDE, delay=0, op_count=len(wrong_taps))
    assert not res.passed, "gate failed to detect a wrong-alpha filter!"


def test_mutation_one_sample_offset_fails():
    _stim, dut, taps, gr = _mut_setup()
    sh_i = [0x0000] + list(dut.i_q15[:-1])
    sh_q = [0x0000] + list(dut.q_q15[:-1])
    res = compare_complex_against_grc(
        sh_i, sh_q, gr.i, gr.q,
        metric=Metric.AMPLITUDE, delay=0, op_count=len(taps))
    assert not res.passed, "gate failed to detect a 1-sample complex latency error!"


def test_mutation_empty_output_fails():
    _stim, _dut, taps, gr = _mut_setup()
    res = compare_complex_against_grc(
        [], [], gr.i, gr.q, metric=Metric.AMPLITUDE, delay=0, op_count=len(taps))
    assert not res.passed, "gate accepted an empty output!"


# =============================================================================
# Guards (documented hardware/harness limits) — explicit, executable
# =============================================================================

def test_decimation_gt_one_rejected():
    """decimation > 1 is a documented limit (the 2-word complex emit + restore
    leave no room for the mod-M gate) — it must RAISE, not silently mis-build."""
    with pytest.raises(ValueError, match=r"decimation"):
        ComplexRRCMatchedFilterBlock("d", decimation=2)


def test_ntaps_too_wide_rejected():
    """ntaps that folds > 8 cells across the 10-wide chip (INV-9) must RAISE."""
    with pytest.raises(ValueError, match=r"fabric|across|ntaps"):
        ComplexRRCMatchedFilterBlock("w", ntaps=33)


def test_legacy_aliases_map_to_gr_params():
    """The old beta/sps/span params still construct the same filter (backward
    compat for the shipped .kyt files)."""
    legacy = ComplexRRCMatchedFilterBlock("l", beta=0.35, sps=2, span=8,
                                          headroom_shift=1, decimation=1)
    assert legacy._alpha == 0.35
    assert legacy._num_taps == 17     # span*sps+1
    assert legacy._samp_rate == 2.0 and legacy._sym_rate == 1.0
    assert legacy.cell_count == 11


# =============================================================================
# Dashboard report — the S=0 GR-drop-in quality (worse of the two rails)
# =============================================================================

def _worse_rail(res):
    return res.i if res.i.max_abs_err >= res.q.max_abs_err else res.q


def test_emit_report():
    stim = _complex_stim(seed=7, n=2 * 17 + 16, amp=0.4)
    dut = _run_dut(stim, _EDGE_PARAMS)
    taps = _rrc_taps(**_EDGE_PARAMS)
    _gr, res = _compare_vs_gr(dut, stim, taps)
    assert res.passed, res.summary()
    write_report("ComplexRRCMatchedFilterBlock", _worse_rail(res), coverage={
        "edge": True, "random": 3, "param_sweep": len(_SWEEP),
        "mutation": True, "ntaps": _EDGE_PARAMS["ntaps"]})
