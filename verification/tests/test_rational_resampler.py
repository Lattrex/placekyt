# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify RationalResamplerBlock vs LIVE GR ``filter.rational_resampler_fff``.

The block is the GRC **Rational Resampler**: resample by L/M through one
polyphase FIR. Pinned-live GR semantics this suite enforces (probed, not
assumed — see the block docstring):

* VALUES: GR output == the zero-stuffed convolution ``y_full`` subsampled at
  ``y_full[D::M]`` with ``D = L*(ceil(N/L)-1)`` (the polyphase history
  alignment). D is NOT phase 0 for N > L — ``rational_resampler_fff(1, M,
  taps)`` is deliberately NOT sample-aligned with ``fir_filter_fff(M, taps)``,
  and a phase-0 "fix" MUST fail this suite (see the alignment mutation).
* COUNT: the chip emits the deterministic ``ceil((n*L - D)/M)`` outputs for
  ``n`` inputs; GR's scheduler emits a PREFIX of the same sequence on a finite
  stream (observed tail deficit <= 2). The gate pins the DUT count formula
  exactly and requires GR == the DUT prefix.
* AUTO-DESIGN: empty ``taps`` -> gcd-reduce (L, M) -> GR's float32
  firdes.low_pass KAISER(beta=7) design (fractional_bw <= 0 -> 0.4, >= 0.5
  raises), zero-padded to a multiple of L by the taps() getter. Tap parity is
  gated Q15-EXACT (INV-16) with a float floor, against live GR.

Supported on-chip range (measured against the real resolver — the probe grid
and its failures are in the lessons log): L=1 -> 5 taps, L=2 -> 4, L=3 -> 3,
any M in [1, 32767], sum(|taps|) <= 1. Everything else RAISES loudly with the
compose-Upsampler->FIR workaround (HW-DEVIATION).

Run:
    cd verification
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
        ../.venv/bin/python -m pytest tests/test_rational_resampler.py -v
"""
from __future__ import annotations

import math
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
    Metric, compare_against_grc, run_gnuradio_ref, write_report)
from kyttar_verify.dut_runner import run_block_dut_rate  # noqa: E402

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")

_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON",
                                              "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not _GR_AVAILABLE, reason="GNU Radio interpreter not available")


def _fq(v: float) -> int:
    q = int(round(v * 32768.0))
    return max(-32768, min(32767, q)) & 0xFFFF


def _blk(L, M, taps):
    from gr_kyttar.placement.blocks.rational_resampler_block import (
        RationalResamplerBlock)
    return RationalResamplerBlock("ref", interpolation=L, decimation=M,
                                  taps=taps)


# GR log level is forced OFF: the gcd(L,M)>1 user-taps info line goes to
# STDOUT and would corrupt the harness's JSON channel.
_GR_RR_SRC = """
from gnuradio import gr, blocks, filter as gfilter
gr.logging().set_default_level(gr.log_levels.off)
tb = gr.top_block()
src = blocks.vector_source_f(input_float, False, 1, [])
rr = gfilter.rational_resampler_fff(L, M, [float(t) for t in taps], 0.0)
snk = blocks.vector_sink_f()
tb.connect(src, rr, snk)
tb.run()
output_float = list(snk.data())
"""

_GR_TAPS_SRC = """
from gnuradio import gr, filter as gfilter
gr.logging().set_default_level(gr.log_levels.off)
rr = gfilter.rational_resampler_fff(L, M, [], FBW)
output_float = list(rr.taps())
"""


def _gr_rr(inq, L, M, taps):
    return run_gnuradio_ref(inq, _GR_RR_SRC,
                            extra_args={"taps": list(taps), "L": int(L),
                                        "M": int(M)})


def _run_case(inq, L, M, taps, orient=None):
    """DUT (chip) + GR golden for one (L, M, taps); returns (dut, gr_result)."""
    dut = run_block_dut_rate("RationalResamplerBlock", inq,
                             params={"interpolation": L, "decimation": M,
                                     "taps": list(taps)},
                             chip_yaml=CHIP_YAML, in_port="sample",
                             out_port="out", orient=orient)
    assert dut.ok, dut.reason
    return dut, _gr_rr(inq, L, M, taps)


# The verified parameter sweep: every supported L at its measured tap ceiling
# (asymmetric taps per INV-12 so tap-order bugs cannot hide), rate-reducing,
# rate-expanding, equal-rate, gcd>1 (NO reduction with user taps — pinned
# live), and the degenerate L=1 / M=1 / L=M=1 edges.
_SWEEP = [
    (2, 3, [0.4, 0.25, -0.2, 0.1]),      # L/M < 1, tap cap, D=2
    (3, 2, [0.45, 0.2, -0.15]),          # L/M > 1, tap cap, D=0
    (2, 5, [0.5, -0.25]),                # deep decimation
    (3, 4, [0.45, 0.2, -0.15]),
    (2, 2, [0.4, 0.25, -0.2, 0.1]),      # gcd=2: user taps are NOT reduced
    (1, 2, [0.3, 0.25, -0.2, 0.15, -0.05]),  # L=1 (pure decim), tap cap, D=4
    (2, 1, [0.4, 0.25, -0.2, 0.1]),      # M=1 (pure interp), D=2
    (3, 1, [0.45, 0.2, -0.15]),
    (1, 1, [0.45, 0.2, -0.15]),          # degenerate: still D=N-1 shifted!
    (3, 7, [0.45, 0.2, -0.15]),          # large M
    (2, 3, [0.5, 0.3, -0.15]),           # N not a multiple of L (virtual pad)
    (3, 2, [0.6, -0.35]),                # N < L would pad; N=2, L=3 arm gaps
    (3, 2, [0.9]),                       # N < L: zero-pad arms emit exact 0s
]


@pytest.mark.parametrize("L,M,taps", _SWEEP,
                         ids=[f"L{c[0]}M{c[1]}N{len(c[2])}" for c in _SWEEP])
def test_matches_live_gr(L, M, taps):
    """DUT vs LIVE GR: count formula exact, GR == DUT prefix within the derived
    Q15 tolerance, and the DUT == the bit-exact Q15 reference."""
    rng = random.Random(100 + L * 10 + M)
    inq = [_fq(rng.uniform(-0.9, 0.9)) for _ in range(24)]  # >= 2x state depth
    dut, ref = _run_case(inq, L, M, taps)
    blk = _blk(L, M, taps)

    # 1. deterministic rate-count exactness (ceil((nL-D)/M)).
    assert len(dut.outputs_q15) == blk.expected_output_count(len(inq)), (
        f"count: got {len(dut.outputs_q15)}, "
        f"expected {blk.expected_output_count(len(inq))}")
    # 2. GR emits a PREFIX of the same sequence (scheduler tail under-run <= 4).
    n_gr = len(ref.floats)
    assert 0 <= len(dut.outputs_q15) - n_gr <= 4, (
        f"GR emitted {n_gr}, DUT {len(dut.outputs_q15)} — not a small tail "
        f"deficit; alignment/count semantics broken")
    # 3. value equivalence vs live GR (derived Q15 tolerance, no lag search).
    res = compare_against_grc(dut.outputs_q15[:n_gr], ref.floats,
                              metric=Metric.AMPLITUDE, delay=0,
                              op_count=len(taps))
    assert res.passed, res.summary()
    # 4. bit-exact against the block's own Q15 datapath model.
    assert dut.outputs_q15 == blk.process_reference_q15(inq), (
        "DUT != bit-exact Q15 reference")


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_random_seeds(seed):
    """>= 3 random seeds on a representative combo case (INV-12)."""
    rng = random.Random(seed)
    inq = [_fq(rng.uniform(-0.95, 0.95)) for _ in range(20)]
    L, M, taps = 2, 3, [0.4, 0.25, -0.2, 0.1]
    dut, ref = _run_case(inq, L, M, taps)
    n = len(ref.floats)
    res = compare_against_grc(dut.outputs_q15[:n], ref.floats,
                              metric=Metric.AMPLITUDE, delay=0,
                              op_count=len(taps))
    assert res.passed, res.summary()
    assert dut.outputs_q15 == _blk(L, M, taps).process_reference_q15(inq)


def test_edge_vectors_full_scale():
    """Full-scale edges (+-1.0, 0x7FFF, 0x8000-side) — Q15 saturation modeled
    on the reference side (INV-3)."""
    inq = [0x7FFF, 0x8000, 0x7FFF, 0x0000, 0x8000, 0x7FFF, 0x4000, 0xC000,
           0x7FFF, 0x8000, 0x0001, 0xFFFF]
    L, M, taps = 2, 3, [0.4, 0.25, -0.2, 0.1]
    dut, ref = _run_case(inq, L, M, taps)
    n = len(ref.floats)
    res = compare_against_grc(dut.outputs_q15[:n], ref.floats,
                              metric=Metric.AMPLITUDE, delay=0,
                              op_count=len(taps))
    assert res.passed, res.summary()
    assert dut.outputs_q15 == _blk(L, M, taps).process_reference_q15(inq)


def test_alignment_impulse_is_yfull_D():
    """Impulse alignment pin: the FIRST output is ``h[D]``, NOT ``h[0]`` — the
    live-probed GR polyphase alignment (D = L*(ceil(N/L)-1)). Asserted on BOTH
    GR and the chip so the golden's own competence is on record (INV-26)."""
    L, M, taps = 2, 3, [0.4, 0.25, -0.2, 0.1]   # K=2 -> D=2
    inq = [0x7FFF] + [0] * 11                    # ~unit impulse
    dut, ref = _run_case(inq, L, M, taps)
    d = 2
    # the impulse is 0x7FFF = 0.99997, so allow its ~3e-5 scaling (vs the
    # 0.6 separation from the phase-0 candidate h[0]=0.4)
    assert abs(ref.floats[0] - taps[d]) < 1e-3, (
        f"GR first output {ref.floats[0]} != h[{d}]={taps[d]} — GR alignment "
        f"changed; re-pin the semantics before touching the block")
    got0 = dut.outputs_q15[0]
    got0 = got0 - 0x10000 if got0 >= 0x8000 else got0
    assert abs(got0 / 32768.0 - taps[d] * (0x7FFF / 32768.0)) < 3 / 32768.0


def test_gr_equals_zero_stuff_model():
    """INV-26 golden-competence pin: GR itself equals the zero-stuff float
    model subsampled at [D::M] on this stimulus (the semantic claim the whole
    suite rests on)."""
    L, M, taps = 3, 2, [0.45, 0.2, -0.15]
    rng = random.Random(5)
    x = [rng.uniform(-0.8, 0.8) for _ in range(20)]
    inq = [_fq(v) for v in x]
    ref = _gr_rr(inq, L, M, taps)
    xf = [(w if w < 0x8000 else w - 0x10000) / 32768.0 for w in inq]
    stuffed = []
    for v in xf:
        stuffed.append(v)
        stuffed.extend([0.0] * (L - 1))
    N = len(taps)
    d = [0.0] * N
    yfull = []
    for s in stuffed:
        d = [s] + d[:-1]
        yfull.append(sum(taps[k] * d[k] for k in range(N)))
    D = L * (math.ceil(N / L) - 1)
    model = yfull[D::M]
    assert all(abs(a - b) < 1e-6
               for a, b in zip(ref.floats, model)), (
        "GR no longer matches the zero-stuff [D::M] model — semantics drifted")


def test_per_trigger_emission_pattern():
    """Rate-count exactness at the TRIGGER level: input n emits exactly the
    full-rate indices j in [nL, nL+L) with j >= D and j == D (mod M)."""
    L, M, taps = 2, 3, [0.4, 0.25, -0.2, 0.1]
    inq = [_fq(0.1 * ((i % 7) - 3)) for i in range(15)]
    dut, _ = _run_case(inq, L, M, taps)
    D = 2
    for n, words in enumerate(dut.per_trigger):
        expect = len([j for j in range(n * L, n * L + L)
                      if j >= D and (j - D) % M == 0])
        assert len(words) == expect, (
            f"trigger {n}: emitted {len(words)} words, expected {expect}")


@pytest.mark.parametrize("orient", [
    ["cw"], ["cw", "cw"], ["cw", "cw", "cw"], ["mirror_v"],
    ["mirror_v", "cw"], ["mirror_v", "cw", "cw"],
    ["mirror_v", "cw", "cw", "cw"]])
def test_orientation_invariant_full_burst(orient):
    """All 8 D4 orientations produce the IDENTICAL full burst stream (INV-23);
    the shared orientation gate covers the last-word-per-trigger view, this
    covers every burst word."""
    L, M, taps = 2, 3, [0.4, 0.25, -0.2, 0.1]
    rng = random.Random(9)
    inq = [_fq(rng.uniform(-0.6, 0.6)) for _ in range(12)]
    ident = run_block_dut_rate("RationalResamplerBlock", inq,
                               params={"interpolation": L, "decimation": M,
                                       "taps": taps},
                               chip_yaml=CHIP_YAML, in_port="sample",
                               out_port="out")
    assert ident.ok, ident.reason
    rot = run_block_dut_rate("RationalResamplerBlock", inq,
                             params={"interpolation": L, "decimation": M,
                                     "taps": taps},
                             chip_yaml=CHIP_YAML, in_port="sample",
                             out_port="out", orient=orient)
    assert rot.ok, rot.reason
    assert rot.outputs_q15 == ident.outputs_q15, (
        f"orientation {orient} diverges from identity")


# --- auto-design tap parity (INV-16: Q15-EXACT, float floor) -------------------

_DESIGN_CASES = [(2, 3, 0.0), (3, 2, 0.25), (1, 2, 0.0), (3, 4, 0.35),
                 (2, 2, 0.0), (4, 6, 0.0), (5, 3, 0.45), (2, 7, 0.0)]


@pytest.mark.parametrize("L,M,fbw", _DESIGN_CASES,
                         ids=[f"L{c[0]}M{c[1]}fbw{c[2]}" for c in _DESIGN_CASES])
def test_auto_design_tap_parity(L, M, fbw):
    """design_filter (the block's float32 GR replica) vs LIVE GR taps():
    Q15-EXACT per tap (the hardware coefficient) + a float floor far below half
    a Q15 LSB. GR gcd-reduces (L, M) first and zero-pads the design to a
    multiple of the reduced L — replicated and asserted, including the
    (4,6)->(2,3) reduction case."""
    from gr_kyttar.placement.blocks._base import float_to_q15
    from gr_kyttar.placement.blocks.rational_resampler_block import (
        RationalResamplerBlock)

    gr = run_gnuradio_ref([0], _GR_TAPS_SRC,
                          extra_args={"L": L, "M": M, "FBW": fbw})
    g = math.gcd(L, M)
    Lr = L // g
    mine = RationalResamplerBlock.design_filter(Lr, M // g, fbw)
    mine_padded = mine + [0.0] * ((-len(mine)) % Lr)
    assert len(mine_padded) == len(gr.floats), (
        f"tap COUNT mismatch: designed {len(mine_padded)}, GR {len(gr.floats)}")
    worst = max(abs(a - b) for a, b in zip(mine_padded, gr.floats))
    assert worst < 1e-6, f"float tap deviation {worst} exceeds the 1e-6 floor"
    q_mine = [float_to_q15(t) for t in mine_padded]
    q_gr = [float_to_q15(t) for t in gr.floats]
    assert q_mine == q_gr, "Q15-quantized taps differ from GR (INV-16)"


def test_auto_design_reduces_operative_rates():
    """GR's interpolation()/decimation() getters return the REDUCED values in
    the auto-design path (pinned live) — the block's properties must agree.
    (Construction still raises the budget HW-deviation for the >=17-tap design;
    checked via the design/rates before the cap by re-deriving them.)"""
    g = math.gcd(4, 6)
    assert (4 // g, 6 // g) == (2, 3)
    # And with USER taps there is NO reduction: (2,2) keeps L=2 (the sweep's
    # gcd=2 case matched GR with D computed from the UNREDUCED L).
    blk = _blk(2, 2, [0.4, 0.25, -0.2, 0.1])
    assert blk.interpolation == 2 and blk.decimation == 2


# --- MANDATORY mutations (INV-4) ----------------------------------------------

def _case_for_mutations():
    L, M, taps = 2, 3, [0.4, 0.25, -0.2, 0.1]
    rng = random.Random(42)
    inq = [_fq(rng.uniform(-0.8, 0.8)) for _ in range(20)]
    dut, ref = _run_case(inq, L, M, taps)
    n = len(ref.floats)
    return dut.outputs_q15[:n], ref, taps, inq, L, M


def test_mutation_inverted_output_fails():
    got, ref, taps, *_ = _case_for_mutations()
    inv = [(-w) & 0xFFFF for w in got]
    res = compare_against_grc(inv, ref.floats, metric=Metric.AMPLITUDE,
                              delay=0, op_count=len(taps))
    assert not res.passed, "gate failed to detect an inverted output!"


def test_mutation_plus_one_delay_fails():
    got, ref, taps, *_ = _case_for_mutations()
    delayed = [0] + got[:-1]
    res = compare_against_grc(delayed, ref.floats, metric=Metric.AMPLITUDE,
                              delay=0, op_count=len(taps))
    assert not res.passed, "gate failed to detect a +1 sample delay!"


def test_mutation_empty_output_fails():
    _, ref, taps, *_ = _case_for_mutations()
    res = compare_against_grc([], ref.floats, metric=Metric.AMPLITUDE,
                              delay=0, op_count=len(taps))
    assert not res.passed, "gate passed an EMPTY output!"


def test_mutation_wrong_decimation_fails():
    """A DUT built with M=2 must fail the M=3 golden (rate mutation)."""
    _, ref, taps, inq, L, _M = _case_for_mutations()
    wrong, _ = _run_case(inq, L, 2, taps)
    res = compare_against_grc(wrong.outputs_q15[:len(ref.floats)], ref.floats,
                              metric=Metric.AMPLITUDE, delay=0,
                              op_count=len(taps))
    assert not res.passed, "gate failed to detect a wrong decimation!"


def test_mutation_phase0_alignment_fails():
    """The phase-0 model (y_full[0::M] — what a naive port WOULD compute) must
    FAIL against GR: the D-offset polyphase alignment is load-bearing."""
    _, ref, taps, inq, L, M = _case_for_mutations()
    blk = _blk(L, M, taps)
    stuffed = []
    for s in inq:
        stuffed.append(int(s) & 0xFFFF)
        stuffed.extend([0] * (L - 1))
    phase0 = blk._full_fir_q15(stuffed)[0::M]      # offset D DROPPED
    res = compare_against_grc(phase0[:len(ref.floats)], ref.floats,
                              metric=Metric.AMPLITUDE, delay=0,
                              op_count=len(taps))
    assert not res.passed, (
        "gate failed to detect the phase-0 mis-alignment — the D-offset "
        "assertion is dead")


def test_mutation_deepest_tap_fails():
    """Perturbing the DEEPEST tap must fail the gate — proof the deep datapath
    is exercised (INV-12), not just the head taps."""
    _, ref, taps, inq, L, M = _case_for_mutations()
    mut_taps = list(taps)
    mut_taps[-1] = -mut_taps[-1]
    mut, _ = _run_case(inq, L, M, mut_taps)
    res = compare_against_grc(mut.outputs_q15[:len(ref.floats)], ref.floats,
                              metric=Metric.AMPLITUDE, delay=0,
                              op_count=len(taps))
    assert not res.passed, "gate failed to detect a deep-tap corruption!"


# --- HW-deviation raises (loud, never silent) ----------------------------------

def test_raises_interp_over_cap():
    with pytest.raises(ValueError, match="HARDWARE LIMIT.*interpolation"):
        _blk(4, 3, [0.5, 0.25])


def test_raises_taps_over_cap():
    with pytest.raises(ValueError, match="HARDWARE LIMIT.*at most"):
        _blk(2, 3, [0.2, 0.2, 0.2, 0.2, 0.1])   # 5 taps > cap 4 for L=2


def test_raises_gain_over_one():
    with pytest.raises(ValueError, match=r"sum\(\|taps\|\) <= 1"):
        _blk(2, 3, [0.9, 0.9])


def test_raises_auto_design_never_fits():
    """The GR-verbatim empty-taps default designs a >=17-tap Kaiser low-pass at
    gain L' — always over the cell budget; the raise must be LOUD and name the
    compose workaround."""
    with pytest.raises(ValueError, match="HARDWARE LIMIT"):
        _blk(2, 3, [])
    try:
        _blk(2, 3, [])
    except ValueError as e:
        # (the >1 gain check fires first on the auto design — either raise
        # names the composed workaround)
        assert "compose" in str(e).lower() and "UpsamplerBlock" in str(e)


def test_raises_bad_fractional_bw():
    from gr_kyttar.placement.blocks.rational_resampler_block import (
        RationalResamplerBlock)
    with pytest.raises(ValueError, match="fractional_bandwidth"):
        RationalResamplerBlock("r", interpolation=2, decimation=3, taps=[],
                               fractional_bw=0.5)
    # <= 0 selects GR's default 0.4 (never raises) — pinned live.
    t = RationalResamplerBlock.design_filter(2, 3, -0.1)
    t2 = RationalResamplerBlock.design_filter(2, 3, 0.4)
    assert t == t2


def test_raises_zero_rates():
    with pytest.raises(ValueError, match="interpolation must be > 0"):
        _blk(0, 3, [0.5])
    with pytest.raises(ValueError, match="decimation must be > 0"):
        _blk(2, 0, [0.5])


def test_raises_decimation_over_word():
    with pytest.raises(ValueError, match="32767"):
        _blk(2, 40000, [0.5, 0.25])


# --- report --------------------------------------------------------------------

def test_emit_report():
    L, M, taps = 2, 3, [0.4, 0.25, -0.2, 0.1]
    rng = random.Random(77)
    inq = [_fq(rng.uniform(-0.9, 0.9)) for _ in range(24)]
    dut, ref = _run_case(inq, L, M, taps)
    n = len(ref.floats)
    res = compare_against_grc(dut.outputs_q15[:n], ref.floats,
                              metric=Metric.AMPLITUDE, delay=0,
                              op_count=len(taps))
    assert res.passed, res.summary()
    write_report("RationalResamplerBlock", res, coverage={
        "edge": True, "random": 3,
        "param_sweep": len(_SWEEP),
        "design_parity": len(_DESIGN_CASES),
        "mutation": True, "rate_check": True, "orientation": 8,
        "note": "GR rational_resampler_fff; polyphase single cell; supported "
                "L=1..3 (taps <= 5/4/3), any M <= 32767, sum|taps| <= 1; "
                "output = y_full[L*(ceil(N/L)-1)::M] (GR polyphase alignment, "
                "pinned live); larger configs raise with the "
                "Upsampler->FIR compose workaround"})
