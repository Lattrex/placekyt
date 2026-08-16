# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify MultiplyCCBlock against GNU Radio blocks.multiply_cc.

The TRUE two-EXTERNAL-complex-stream product: ``out = a * b`` elementwise over
two complex streams from two independent sources — ``yi = ai*bi - aq*bq``,
``yq = ai*bq + aq*bi``. Unlike add_cc/sub_cc the math is NOT separable per
rail: each output rail needs ALL FOUR operands, so the historically-predicted
"4-operand co-residency wall" applies in full. The architecture that fells it:
the landing cell's counting join (two packets per sample, any order) snapshots
all four operands into ITS OWN state and forms the four full-scale products;
only products travel to the combine cell — the operands are co-resident
exactly once, in the landing cell.

Headroom strategy (derived, not tuned): both factors are Q15 signals so every
MULQ product is already in range; only the per-rail combine can overflow, and
a single 16-bit ADD/SUB overflow is exactly recoverable from the V flag (the
AddCC minuend-sign restore). S=0 — no headroom shift, no error amplification.
Derived GR-equivalence floor: q15_quant_floor(op_count=2, head_shift=0) =
3 LSB (two truncating MULQs per rail + comparison quantization); the stimulus
is snapped to the Q15 grid so GR (float) sees the exact values the chip sees
and input-quantization error cannot stack on top of the floor.

Reference tiers:
  * DSP equivalence — DUT vs GR multiply_cc (compare_complex_against_grc,
    AMPLITUDE, op_count=2) on IN-RANGE stimulus (|a|,|b| <= 0.7 per rail =>
    each product <= 0.49, each rail <= 0.98 — no saturation, no wrap).
  * Bit-exact substrate — DUT vs process_reference_q15 (truncating MULQ incl.
    the (-1)*(-1) wrap + saturating per-rail combine), EXACT, including the
    saturation corners on both rails and the wrap corner.

Per INV-4 every gate is paired with mutations that must FAIL: inverted,
wrong-second-stream, DROPPED-CROSS-TERM golden (yi=ai*bi, yq=aq*bq — a
non-rotating fake), SIGN-SWAPPED-CROSS-TERM golden (GR fed conj(b) — the
conjugate product), swapped I/Q rails, +1 delay, empty. Stream swap is NOT a
corruption (multiplication is commutative — asserted EQUAL, documented), so
the saturated-drive non-vacuity probe conjugates b instead. Orientation (all
8 D4) and the SATURATED pipeline gate (per-sample == queue_words drive,
bit-exact) are covered here bespoke — the shared test_pipeline_saturation
harnesses cannot deliver a 2-jump-per-sample stream (NEEDS_BESPOKE points
here).

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_multiply_cc.py -x -q
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
    run_block_dut_complex2, run_block_dut_complex2_pipelined,
    run_gnuradio_ref_complex, compare_against_grc, compare_complex_against_grc,
    compare_dut_results, write_report, Metric, D4_ORIENTATIONS)
from gr_kyttar.placement.blocks.multiply_cc_block import MultiplyCCBlock  # noqa: E402

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_AVAILABLE),
    reason="chip yaml or GNU Radio interpreter absent")

BLOCK = "MultiplyCCBlock"
GR_FACTORY = "blocks.multiply_cc()"
# Two truncating MULQs per rail, S=0 — the derived q15_quant_floor(2, 0) = 3.
OP_COUNT = 2


def _s16(v):
    if v is None:
        return None
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def _q15(v: float) -> int:
    q = int(round(v * 32768.0))
    return max(-32768, min(32767, q)) & 0xFFFF


def _snap(c: complex) -> complex:
    """Snap a stimulus value onto the Q15 grid so GR's float golden computes
    over EXACTLY the words the chip receives — input-quantization error is
    then zero and the derived op_count floor is the whole error budget."""
    return complex(round(c.real * 32768.0) / 32768.0,
                   round(c.imag * 32768.0) / 32768.0)


# In-range edge pairs, |re|,|im| <= 0.7 per rail on BOTH streams (products
# <= 0.49, rails <= 0.98 — strictly inside Q15, no saturation, no MULQ wrap).
_EDGE_A = [complex(0.7, 0.7), complex(-0.7, -0.7), complex(0.7, -0.7),
           complex(0.5, 0.0), complex(0.0, 0.5), complex(0.0, 0.0),
           complex(-0.7, 0.0), complex(0.25, -0.25), complex(0.1, 0.65),
           complex(-0.5, 0.5)]
_EDGE_B = [complex(0.7, -0.7), complex(0.7, 0.7), complex(-0.7, -0.7),
           complex(0.0, 0.5), complex(0.5, 0.0), complex(0.7, 0.7),
           complex(0.0, -0.7), complex(0.6, 0.6), complex(-0.65, 0.1),
           complex(0.5, 0.5)]
_EDGE_A = [_snap(c) for c in _EDGE_A]
_EDGE_B = [_snap(c) for c in _EDGE_B]


def _random_streams(seed, n=24, amp=0.7):
    rng = random.Random(seed)
    mk = lambda: [_snap(complex(rng.uniform(-amp, amp), rng.uniform(-amp, amp)))  # noqa: E731
                  for _ in range(n)]
    return mk(), mk()


def _run_dut(a, b, **kw):
    dut = run_block_dut_complex2(BLOCK, a, b, chip_yaml=CHIP_YAML, **kw)
    assert dut.ok, dut.reason
    return dut


def _gr(a, b, gr_factory=GR_FACTORY):
    """GR golden: two complex vector sources into multiply_cc."""
    return run_gnuradio_ref_complex(
        a,
        extra_args={"b_i": [float(c.real) for c in b],
                    "b_q": [float(c.imag) for c in b]},
        gnuradio_script=f"""
from gnuradio import gr, blocks
tb = gr.top_block()
sa = blocks.vector_source_c(input_complex, False)
sb = blocks.vector_source_c([complex(i, q) for i, q in zip(b_i, b_q)], False)
op = {gr_factory}
snk = blocks.vector_sink_c()
tb.connect(sa, (op, 0)); tb.connect(sb, (op, 1)); tb.connect(op, snk)
tb.run()
output_complex = list(snk.data())
""")


def _compare(dut, gr):
    # two truncating MULQs per rail, memoryless: op_count=2, delay=0.
    return compare_complex_against_grc(dut.i_q15, dut.q_q15, gr.i, gr.q,
                                       metric=Metric.AMPLITUDE, delay=0,
                                       op_count=OP_COUNT)


# --- structure / smoke --------------------------------------------------------

def test_cell_budget_and_join_entry():
    """Both cells fit their 32-word budget; the landing cell's FIRST entry is
    the counting join (external packet JUMPs + resolved_io land there); the
    output cell keeps >=1 free word for the INV-17 fan-out JUMP (the full
    built-memory gate is placekyt/tests/test_complex_output_fanout.py)."""
    from gr_kyttar.placement.resolver import CellProgramResolver
    blk = MultiplyCCBlock("probe")
    cps = blk.build_cell_programs()
    assert list(cps) == ["prods", "combine"]
    r = CellProgramResolver()
    for cid, cp in cps.items():
        n_instr = r.count_instructions(cp)
        n_regs = (len(cp.inputs) + len(cp.data or ()) + len(cp.state or ()))
        used = n_instr + n_regs
        free = 32 - used
        assert free >= 0, f"{cid}: {used}/32 words — over budget"
        if cid == "combine":
            assert free >= 1, (
                f"combine: {used}/32 words — no room for the INV-17 fan-out JUMP")
    assert cps["prods"].entries[0].name == "join", \
        "the landing cell's default entry must BE the counting join"


def test_drives_and_captures():
    a, b = _random_streams(1, 12)
    dut = _run_dut(a, b)
    assert dut.words_per_sample == 2
    assert dut.in_regs == (0, 1, 2, 3), \
        "the four operands should land ai@R0, aq@R1, bi@R2, bq@R3"
    assert all(v is not None for v in dut.i_q15)
    assert all(v is not None for v in dut.q_q15)


def test_num_inputs_pinned_raises():
    """HW-DEVIATION: num_inputs is pinned to 2 (a 3rd stream is a chained
    complex multiply — a whole second stage) — any other value must raise
    loudly, never silently clamp."""
    with pytest.raises(ValueError, match="HARDWARE LIMIT"):
        MultiplyCCBlock("x", num_inputs=3)
    with pytest.raises(ValueError, match="HARDWARE LIMIT"):
        MultiplyCCBlock("x", num_inputs=1)


# --- DSP equivalence vs GNU Radio ---------------------------------------------

def test_edge_vectors():
    dut = _run_dut(_EDGE_A, _EDGE_B)
    gr = _gr(_EDGE_A, _EDGE_B)
    res = _compare(dut, gr)
    print(f"\nmultiply_cc edge: I {res.i.summary()} | Q {res.q.summary()}")
    assert res.passed, f"I {res.i.summary()} | Q {res.q.summary()}"


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_random_vectors(seed):
    a, b = _random_streams(seed)
    dut = _run_dut(a, b)
    gr = _gr(a, b)
    res = _compare(dut, gr)
    print(f"\nmultiply_cc seed={seed}: I {res.i.summary()} | Q {res.q.summary()}")
    assert res.passed, f"I {res.i.summary()} | Q {res.q.summary()}"


@pytest.mark.parametrize("amp", [0.1, 0.3, 0.7])
def test_amplitude_sweep(amp):
    """Parity across amplitude regimes (the param-sweep analogue — the block
    has no DSP params), kept in range (|a|,|b| <= 0.7)."""
    a, b = _random_streams(99, n=20, amp=amp)
    dut = _run_dut(a, b)
    gr = _gr(a, b)
    res = _compare(dut, gr)
    print(f"\nmultiply_cc amp={amp}: I {res.i.summary()} | Q {res.q.summary()}")
    assert res.passed, f"I {res.i.summary()} | Q {res.q.summary()}"


@pytest.mark.parametrize("factor,angle", [
    (complex(0.0, 0.7), "+90deg (pure-j: yi=-0.7*aq, yq=+0.7*ai)"),
    (complex(-0.7, 0.0), "180deg (pure-real-negative)"),
    (complex(0.495, 0.495), "+45deg diagonal"),
])
def test_rotation_cases(factor, angle):
    """A CONSTANT complex factor on stream b must ROTATE stream a's
    constellation (the cross-terms at work — a separable per-rail fake cannot
    produce this). Verified vs GR; the pure-j case additionally pinned to the
    analytic 90-degree I/Q swap."""
    factor = _snap(factor)
    a, _ = _random_streams(5, 16, amp=0.7)
    b = [factor] * len(a)
    dut = _run_dut(a, b)
    gr = _gr(a, b)
    res = _compare(dut, gr)
    print(f"\nmultiply_cc rotation {angle}: I {res.i.summary()} | Q {res.q.summary()}")
    assert res.passed, f"I {res.i.summary()} | Q {res.q.summary()}"
    if factor.real == 0.0:  # pure-j: yi = -k*aq, yq = +k*ai (90-degree swap)
        k = factor.imag
        for c, wi, wq in zip(a, dut.i_q15, dut.q_q15):
            assert abs(_s16(wi) / 32768.0 - (-k * c.imag)) < 4 / 32768.0
            assert abs(_s16(wq) / 32768.0 - (+k * c.real)) < 4 / 32768.0


# --- bit-exact substrate ------------------------------------------------------

@pytest.mark.parametrize("seed", [3, 17, 256])
def test_bitexact_reference(seed):
    """DUT matches the truncating-MULQ + saturating-combine Q15 reference
    EXACTLY over a stream that INCLUDES out-of-range rails (amp 0.99)."""
    a, b = _random_streams(seed, n=60, amp=0.99)
    dut = _run_dut(a, b)
    blk = MultiplyCCBlock("ref")
    yi, yq = blk.process_reference_q15(
        [_q15(c.real) for c in a], [_q15(c.imag) for c in a],
        [_q15(c.real) for c in b], [_q15(c.imag) for c in b])
    ri = compare_against_grc(dut.i_q15, [_s16(w) / 32768.0 for w in yi],
                             metric=Metric.EXACT, delay=0)
    rq = compare_against_grc(dut.q_q15, [_s16(w) / 32768.0 for w in yq],
                             metric=Metric.EXACT, delay=0)
    print(f"\nmultiply_cc bit-exact seed={seed}: I {ri.summary()} | Q {rq.summary()}")
    assert ri.passed and rq.passed, f"I {ri.summary()} | Q {rq.summary()}"


@pytest.mark.parametrize("a,b,rail_i,rail_q", [
    # (0.99+0.99j)^2: yi = .98-.98 = 0, yq = 1.96 -> Q pins +full
    (complex(0.99, 0.99), complex(0.99, 0.99), 0, 32767),
    # conj pair negated: yq = -1.96 -> Q pins -full
    (complex(0.99, 0.99), complex(-0.99, -0.99), 0, -32768),
    # a*conj(a)-shape: yi = .98+.98 = 1.96 -> I pins +full
    (complex(0.99, 0.99), complex(0.99, -0.99), 32767, None),
    # negated: yi = -1.96 -> I pins -full
    (complex(-0.99, -0.99), complex(0.99, -0.99), -32768, None),
])
def test_saturates_not_wraps(a, b, rail_i, rail_q):
    """An out-of-range rail must PIN to ±full-scale PER RAIL (no wrap) — the
    V-flag minuend-sign restore path, exercised in both signs on both rails.
    The non-saturating sibling rail must simultaneously be BIT-EXACT (the
    products cancel to ~0), proving the restore never fires spuriously."""
    astream = [complex(0.1, 0.1), a, complex(-0.2, 0.2), a]
    bstream = [complex(0.05, -0.1), b, complex(0.1, -0.1), b]
    dut = _run_dut(astream, bstream)
    blk = MultiplyCCBlock("ref")
    yi, yq = blk.process_reference_q15(
        [_q15(c.real) for c in astream], [_q15(c.imag) for c in astream],
        [_q15(c.real) for c in bstream], [_q15(c.imag) for c in bstream])
    for idx in (1, 3):
        if rail_i is not None:
            assert _s16(dut.i_q15[idx]) == rail_i, (
                f"I rail must saturate to {rail_i}, got {_s16(dut.i_q15[idx])}")
        if rail_q is not None:
            assert _s16(dut.q_q15[idx]) == rail_q, (
                f"Q rail must saturate to {rail_q}, got {_s16(dut.q_q15[idx])}")
        assert dut.i_q15[idx] == yi[idx] and dut.q_q15[idx] == yq[idx], (
            "saturation corner must be bit-exact vs process_reference_q15 on "
            "BOTH rails")


def test_wrap_corner_pinned():
    """The documented MULQ wrap corner (the MultiplyBlock (-1)*(-1) class):
    a = b = -1-1j makes ALL FOUR products (-1)*(-1) = +1.0, which MULQ WRAPS
    to -1.0. yi = p1-p2 = 0 (the wraps cancel — still correct!) but
    yq = p3+p4 = -2 pins to -full where GR's float gives +2 (would clip
    +full): a per-rail deviation at exactly this measure-zero corner, pinned
    BIT-EXACT against the block's OWN wrap-modelling reference. In-range
    GR-equivalence stimulus (|a|,|b| <= 0.7) can never reach it."""
    corner = complex(-1.0, -1.0)
    a = [complex(0.2, 0.1), corner, complex(-0.3, 0.4)]
    b = [complex(0.1, -0.2), corner, complex(0.25, 0.5)]
    dut = _run_dut(a, b)
    blk = MultiplyCCBlock("ref")
    yi, yq = blk.process_reference_q15(
        [_q15(c.real) for c in a], [_q15(c.imag) for c in a],
        [_q15(c.real) for c in b], [_q15(c.imag) for c in b])
    assert list(dut.i_q15) == yi and list(dut.q_q15) == yq, \
        "wrap corner must match the wrap-modelling reference bit-exact"
    assert _s16(dut.i_q15[1]) == 0, "yi: the four wraps cancel exactly"
    assert _s16(dut.q_q15[1]) == -32768, \
        "yq: wrapped products drive the rail to -full (the documented corner)"


# --- MANDATORY mutation tests (INV-4) -----------------------------------------

def _setup(seed=7, n=24):
    a, b = _random_streams(seed, n)
    dut = _run_dut(a, b)
    gr = _gr(a, b)
    return dut, gr, a, b


def test_mutation_inverted_output_fails():
    dut, gr, *_ = _setup()
    mut_i = [(0x10000 - (w or 0)) & 0xFFFF for w in dut.i_q15]
    mut_q = [(0x10000 - (w or 0)) & 0xFFFF for w in dut.q_q15]
    res = compare_complex_against_grc(mut_i, mut_q, gr.i, gr.q,
                                      metric=Metric.AMPLITUDE, delay=0,
                                      op_count=OP_COUNT)
    assert not res.passed, "gate failed to detect an inverted output!"


def test_mutation_wrong_second_stream_fails():
    """A DUT fed the WRONG b stream must fail the (a, b) gate."""
    a, b = _random_streams(7, 24)
    _, wrong_b = _random_streams(8, 24)
    dut = _run_dut(a, wrong_b)
    gr = _gr(a, b)
    res = _compare(dut, gr)
    assert not res.passed, "gate failed to detect a wrong second stream!"


def test_mutation_dropped_cross_terms_fails():
    """The correct DUT must FAIL a NON-ROTATING golden (yi=ai*bi, yq=aq*bq —
    the separable per-rail fake with the cross-terms dropped): proof the
    on-chip cross-terms are real and load-bearing, not a per-rail echo."""
    a, b = _random_streams(9, 24)
    dut = _run_dut(a, b)
    fake_i = [c1.real * c2.real for c1, c2 in zip(a, b)]
    fake_q = [c1.imag * c2.imag for c1, c2 in zip(a, b)]
    res = compare_complex_against_grc(dut.i_q15, dut.q_q15, fake_i, fake_q,
                                      metric=Metric.AMPLITUDE, delay=0,
                                      op_count=OP_COUNT)
    assert not res.passed, (
        "DUT passed a cross-term-free (non-rotating) golden — the complex "
        "product is not under test!")


def test_mutation_sign_swapped_cross_term_fails():
    """The correct DUT must FAIL the CONJUGATE golden (GR fed conj(b) — the
    sign-swapped cross-terms): proof the cross-term SIGNS are under test
    (a*conj(b) is the correlator, NOT multiply_cc)."""
    a, b = _random_streams(10, 24)
    dut = _run_dut(a, b)
    b_conj = [complex(c.real, -c.imag) for c in b]
    gr = _gr(a, b_conj)
    res = _compare(dut, gr)
    assert not res.passed, (
        "DUT passed the conjugate golden — cross-term signs not under test!")


def test_swapped_streams_is_commutative():
    """DOCUMENTED: complex multiplication is commutative — the swapped drive
    PASSES, so a swapped-stream mutation carries no teeth here (the teeth are
    wrong-second-stream + the cross-term mutations; the saturated-drive
    non-vacuity probe conjugates b instead of swapping)."""
    a, b = _random_streams(11, 24)
    dut = _run_dut(b, a)          # swapped drive
    gr = _gr(a, b)
    res = _compare(dut, gr)
    assert res.passed, "multiply_cc must be commutative (a*b == b*a)"


def test_mutation_swapped_iq_rails_fails():
    """Swapping the DUT's I/Q output rails must fail (yi and yq are distinct
    linear combinations — proof both rails are independently under test)."""
    dut, gr, *_ = _setup(12)
    res = compare_complex_against_grc(dut.q_q15, dut.i_q15, gr.i, gr.q,
                                      metric=Metric.AMPLITUDE, delay=0,
                                      op_count=OP_COUNT)
    assert not res.passed, "gate failed to detect swapped I/Q output rails!"


def test_mutation_one_sample_offset_fails():
    dut, gr, *_ = _setup()
    sh_i = [0x0000] + list(dut.i_q15[:-1])
    sh_q = [0x0000] + list(dut.q_q15[:-1])
    res = compare_complex_against_grc(sh_i, sh_q, gr.i, gr.q,
                                      metric=Metric.AMPLITUDE, delay=0,
                                      op_count=OP_COUNT)
    assert not res.passed, "gate failed to detect a 1-sample latency error!"


def test_empty_output_fails():
    _, gr, *_ = _setup()
    res = compare_complex_against_grc([], [], gr.i, gr.q,
                                      metric=Metric.AMPLITUDE, delay=0,
                                      op_count=OP_COUNT)
    assert not res.passed


# --- orientation invariance (INV-23, all 8 D4) --------------------------------

@pytest.mark.parametrize(
    "orient", D4_ORIENTATIONS[1:],
    ids=lambda o: "+".join(o) if o else "identity")
def test_orientation_invariant(orient):
    """The on-chip output under every D4 orientation must EQUAL the identity
    output (driven through the two-packet complex2 driver)."""
    a, b = _random_streams(3, 16)
    base = _run_dut(a, b)
    res = run_block_dut_complex2(BLOCK, a, b, chip_yaml=CHIP_YAML,
                                 orient=list(orient))
    assert res.ok, f"{BLOCK} {orient}: {res.reason}"
    ok, detail = compare_dut_results(base, res)
    assert ok, f"{BLOCK} {'+'.join(orient)}: {detail}"


# --- SATURATED (pipelined) gate — bespoke (INV-19/20) --------------------------
# The shared test_pipeline_saturation harnesses deliver all operands with ONE
# JUMP per sample; this block's samples are TWO packets (two JUMPs) for its
# counting join, so the saturated gate lives here (NEEDS_BESPOKE points here).

def test_pipelined_equals_per_sample():
    """Saturated (whole burst queued, ONE continuous run, no inter-sample
    quiescence) output must equal the per-sample output BIT-EXACT — the
    counting join + the operand SNAPSHOTS must pace correctly with packets
    slammed back-to-back (the stale-latch hazard the snapshot design closes)."""
    a, b = _random_streams(21, 32, amp=0.99)   # includes saturating rails
    seq = _run_dut(a, b)
    seq_flat = [w for g in seq.outputs_q15 for w in g]

    aw = [(_q15(c.real), _q15(c.imag)) for c in a]
    bw = [(_q15(c.real), _q15(c.imag)) for c in b]
    pipe = run_block_dut_complex2_pipelined(BLOCK, aw, bw, chip_yaml=CHIP_YAML)
    assert pipe.ok, f"pipelined build/run failed (deadlock/livelock?): {pipe.reason}"

    # PROBE the drive is REAL (the vacuous-drive trap): the saturated run must
    # produce one full (yi, yq) pair per sample and a non-trivial stream that
    # matches the independently-computed Q15 reference — not just [] == [].
    n = len(seq_flat)
    assert n == 2 * len(a), "per-sample run must emit a (yi, yq) pair per sample"
    assert len(pipe.outputs_q15) >= n, (
        f"{BLOCK}: saturated produced {len(pipe.outputs_q15)} words, "
        f"per-sample produced {n} — pipeline STALLED (join/handshake hazard)")
    yi, yq = MultiplyCCBlock("ref").process_reference_q15(
        [w for w, _ in aw], [w for _, w in aw],
        [w for w, _ in bw], [w for _, w in bw])
    exp = [w for pair in zip(yi, yq) for w in pair]
    assert len(set(exp)) > 4, "stimulus produced a degenerate reference stream"
    assert seq_flat == exp, "per-sample output must equal the Q15 reference"
    assert pipe.outputs_q15[:n] == seq_flat, (
        f"{BLOCK}: saturated output diverges from per-sample at index "
        f"{next(i for i in range(n) if pipe.outputs_q15[i] != seq_flat[i])}")


def test_pipelined_drive_is_not_vacuous():
    """MUTATION of the saturated DRIVE itself: conjugating stream b in the
    pipelined drive must CHANGE the output — proof the queued two-packet
    stream actually reaches the datapath (not an empty-vs-empty pass).
    (Stream SWAP would be vacuous here — multiply is commutative — so the
    probe flips bq's sign instead.)"""
    a, b = _random_streams(23, 16)
    aw = [(_q15(c.real), _q15(c.imag)) for c in a]
    bw = [(_q15(c.real), _q15(c.imag)) for c in b]
    bw_conj = [(re, (0x10000 - im) & 0xFFFF) for re, im in bw]
    good = run_block_dut_complex2_pipelined(BLOCK, aw, bw, chip_yaml=CHIP_YAML)
    conj = run_block_dut_complex2_pipelined(BLOCK, aw, bw_conj,
                                            chip_yaml=CHIP_YAML)
    assert good.ok and conj.ok
    assert good.outputs_q15 and conj.outputs_q15
    assert good.outputs_q15 != conj.outputs_q15, (
        "conjugating the pipelined b stream changed nothing — the saturated "
        "drive is vacuous")


# --- GRC import: two independent complex sources into the product -------------

_TWO_MIXER_GRC = """options:
  parameters: {id: min_mulcc, generate_options: qt_gui}
  states: {coordinate: [8, 8], rotation: 0, state: enabled}
blocks:
- name: mixa
  id: kyttar_complex_mixer
  parameters: {frequency: '1000', sample_rate: '48000'}
  states: {coordinate: [100, 100], rotation: 0, state: enabled}
- name: mixb
  id: kyttar_complex_mixer
  parameters: {frequency: '2000', sample_rate: '48000'}
  states: {coordinate: [100, 240], rotation: 0, state: enabled}
- name: src
  id: kyttar_source
  parameters: {device_id: '"kyttar_0"', complex_in: 'True'}
  states: {coordinate: [20, 160], rotation: 0, state: enabled}
- name: prod
  id: kyttar_multiply_cc
  parameters: {device_id: '"kyttar_0"', num_inputs: '2'}
  states: {coordinate: [300, 160], rotation: 0, state: enabled}
- name: snk
  id: kyttar_sink
  parameters: {device_id: '"kyttar_0"'}
  states: {coordinate: [520, 160], rotation: 0, state: enabled}
connections:
- [src, '0', mixa, '0']
- [src, '0', mixb, '0']
- [mixa, '0', prod, '0']
- [mixb, '0', prod, '1']
- [prod, '0', snk, '0']
"""


def test_grc_import_wires_two_streams_and_join():
    """IMPORTER contract (the AddCC pair-collapse fix, exercised for the
    product): GRC's numeric port index counts COMPLEX ports, so index 1 must
    land on the SECOND stream's I-half (bi), NOT the first stream's Q-half
    (aq); the I/Q split synthesises aq/bq; ALL four arms are counting-join-
    elected to the landing cell's join entry; and the imported design
    auto-P&Rs and BUILDS."""
    import tempfile
    from engine.catalog import BlockCatalog
    from engine.grc_import import import_grc
    from engine.io.chip_type_io import load_chip_type
    from ui.controller import AppController
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])

    cat = BlockCatalog.from_gr_kyttar()
    with tempfile.NamedTemporaryFile("w", suffix=".grc", delete=False) as tf:
        tf.write(_TWO_MIXER_GRC)
        path = tf.name
    try:
        res = import_grc(path, cat, chip_type="kyttar_10x12")
    finally:
        os.unlink(path)
    assert res.ok and not res.unknown, res.unknown
    prod = next(b.name for b in res.project.blocks if b.type == BLOCK)
    join_entry, _ = cat.resolved_io(BLOCK, {})
    arms = {}
    for c in res.project.connections:
        if getattr(c.target, "block", None) == prod:
            arms[c.target.port] = (c.source.block, c.source.port,
                                   getattr(c, "entry_override", None))
    assert set(arms) == {"ai", "aq", "bi", "bq"}, (
        f"all four operand rails must be wired (index 1 -> bi + synthesised "
        f"aq/bq); got {arms}")
    assert arms["ai"][0] != arms["bi"][0], "ai and bi must come from DIFFERENT sources"
    assert arms["ai"][0] == arms["aq"][0], "aq must be stream a's synthesised Q rail"
    assert arms["bi"][0] == arms["bq"][0], "bq must be stream b's synthesised Q rail"
    for port, (_s, _p, ov) in arms.items():
        assert ov == join_entry, (
            f"arm {port}: entry_override={ov}, expected the counting-join entry "
            f"{join_entry} (the 2-source election must fire)")
    ct = load_chip_type(CHIP_YAML)
    ctk = getattr(ct, "name", None) or "kyttar_10x12"
    ctrl = AppController(catalog=cat)
    ctrl.project = res.project
    assert ctrl.auto_pnr({ctk: ct}).ok, "imported two-stream design did not route"
    bres = ctrl.build()
    assert bres.ok, "build failed: " + "; ".join(str(e) for e in bres.errors)


# --- dashboard report ---------------------------------------------------------

def test_emit_report():
    dut = _run_dut(_EDGE_A, _EDGE_B)
    gr = _gr(_EDGE_A, _EDGE_B)
    res = _compare(dut, gr)
    assert res.passed, f"I {res.i.summary()} | Q {res.q.summary()}"
    write_report(BLOCK, res.i, coverage={
        "edge": True, "random": 3, "amplitude_sweep": 3, "rotation": 3,
        "bit_exact": True, "saturation": True, "wrap_corner": True,
        "orientation_d4": True, "pipelined": True, "mutation": True})
