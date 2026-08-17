# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify AddCCBlock / SubCCBlock against GNU Radio blocks.add_cc / blocks.sub_cc.

The two-EXTERNAL-complex-stream combiners: ``out = a + b`` / ``out = a - b``
elementwise over two complex streams from two independent sources (GR semantics
pinned LIVE 2026-08-16: memoryless, strict per-sample pairing, delay 0). The math
is separable per rail — two independent saturating Q15 adds (the AddBlock rail
idiom): ``rail_i`` (the landing cell) computes yi and forwards (yi, aq, bq);
``rail_q`` computes yq and emits the (yi, yq) complex packet.

The DELIVERY is the historically hard part (the "4-operand wall"): each sample is
4 operands as TWO complex packets (multi-WRITE + one JUMP each) from two sources,
single-fired by the landing cell's COUNTING JOIN (toggle, fires on the second
trigger in ANY order). The reusable two-complex-stream driver lives in
``kyttar_verify.run_block_dut_complex2`` (+ the saturated ``_pipelined`` twin) —
the MultiplyCCBlock (same shape) drives through it unchanged.

Reference tiers:
  * DSP equivalence — DUT vs GR add_cc/sub_cc per rail (compare_complex_against_grc,
    AMPLITUDE) on IN-RANGE stimulus (|a±b| < 1 per rail).
  * Bit-exact substrate — DUT vs process_reference_q15 (per-rail SATURATING
    add/sub), EXACT, including overflow corners on both rails.

Saturation is verified directly per rail (out-of-range pins to ±full-scale, no
wrap). Per INV-4 every gate is paired with mutations that must FAIL: inverted,
wrong-second-stream, per-rail corruptions (aq-only / bi-only), +1 delay, empty;
SUB additionally the REQUIRED swapped-streams mutation (a-b ≠ b-a) while ADD's
swap is asserted commutative (documented, not a corruption). Orientation (all 8
D4) and the SATURATED pipeline gate (per-sample == queue_words drive, bit-exact,
with a non-vacuity probe) are covered here bespoke — the shared
test_pipeline_saturation harnesses cannot deliver a 2-jump-per-sample stream
(the blocks are listed in its NEEDS_BESPOKE with this file as the gate).

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_add_sub_cc.py -x -q
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
from gr_kyttar.placement.blocks.add_sub_cc_block import (  # noqa: E402
    AddCCBlock, SubCCBlock)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_AVAILABLE),
    reason="chip yaml or GNU Radio interpreter absent")

# (block_type, GR factory, class) — parameterizes the whole suite over add & sub.
_VARIANTS = {
    "add": ("AddCCBlock", "blocks.add_cc()", AddCCBlock),
    "sub": ("SubCCBlock", "blocks.sub_cc()", SubCCBlock),
}


def _s16(v):
    if v is None:
        return None
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def _q15(v: float) -> int:
    q = int(round(v * 32768.0))
    return max(-32768, min(32767, q)) & 0xFFFF


# In-range edge pairs: |re(a)±re(b)| < 1 AND |im(a)±im(b)| < 1 for both variants.
_EDGE_A = [complex(0.0, 0.49), complex(0.3, 0.3), complex(-0.3, -0.3),
           complex(0.49, -0.49), complex(-0.49, 0.49), complex(0.25, -0.25),
           complex(-0.4, 0.4), complex(0.1, -0.45), complex(-0.1, 0.45),
           complex(0.45, -0.45)]
_EDGE_B = [complex(0.49, 0.0), complex(0.3, -0.3), complex(0.3, 0.3),
           complex(0.49, 0.49), complex(-0.49, -0.49), complex(-0.25, 0.25),
           complex(0.4, -0.4), complex(-0.45, 0.1), complex(0.45, -0.1),
           complex(-0.45, 0.45)]


def _random_streams(seed, n=24, amp=0.45):
    rng = random.Random(seed)
    mk = lambda: [complex(rng.uniform(-amp, amp), rng.uniform(-amp, amp))  # noqa: E731
                  for _ in range(n)]
    return mk(), mk()


def _run_dut(block_type, a, b, **kw):
    dut = run_block_dut_complex2(block_type, a, b, chip_yaml=CHIP_YAML, **kw)
    assert dut.ok, dut.reason
    return dut


def _gr(gr_factory, a, b):
    """GR golden: two complex vector sources into add_cc/sub_cc."""
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
    # one saturating ADD/SUB per rail, memoryless: op_count=1, delay=0.
    return compare_complex_against_grc(dut.i_q15, dut.q_q15, gr.i, gr.q,
                                       metric=Metric.AMPLITUDE, delay=0,
                                       op_count=1)


# --- structure / smoke --------------------------------------------------------

@pytest.mark.parametrize("variant", ["add", "sub"])
def test_cell_budget_and_join_entry(variant):
    """Both cells fit their 32-word budget; the landing cell's FIRST entry is the
    counting join (external packet JUMPs + resolved_io land there); the output
    cell keeps >=1 free word for the INV-17 fan-out JUMP (the full built-memory
    gate is placekyt/tests/test_complex_output_fanout.py)."""
    from gr_kyttar.placement.resolver import CellProgramResolver
    _, _, cls = _VARIANTS[variant]
    blk = cls("probe")
    cps = blk.build_cell_programs()
    assert list(cps) == ["rail_i", "rail_q"]
    r = CellProgramResolver()
    for cid, cp in cps.items():
        n_instr = r.count_instructions(cp)
        n_regs = (len(cp.inputs) + len(cp.data or ()) + len(cp.state or ()))
        used = n_instr + n_regs
        free = 32 - used
        assert free >= 0, f"{cid}: {used}/32 words — over budget"
        if cid == "rail_q":
            assert free >= 1, (
                f"rail_q: {used}/32 words — no room for the INV-17 fan-out JUMP")
    assert cps["rail_i"].entries[0].name == "join", \
        "the landing cell's default entry must BE the counting join"


@pytest.mark.parametrize("variant", ["add", "sub"])
def test_drives_and_captures(variant):
    block_type, _, _ = _VARIANTS[variant]
    a, b = _random_streams(1, 12)
    dut = _run_dut(block_type, a, b)
    assert dut.words_per_sample == 2
    assert dut.in_regs == (0, 1, 2, 3), \
        "the four operands should land ai@R0, aq@R1, bi@R2, bq@R3"
    assert all(v is not None for v in dut.i_q15)
    assert all(v is not None for v in dut.q_q15)


def test_num_inputs_pinned_raises():
    """HW-DEVIATION: num_inputs is pinned to 2 (32-word cell budget) — any other
    value must raise loudly, never silently clamp."""
    for cls in (AddCCBlock, SubCCBlock):
        with pytest.raises(ValueError, match="HARDWARE LIMIT"):
            cls("x", num_inputs=3)
        with pytest.raises(ValueError, match="HARDWARE LIMIT"):
            cls("x", num_inputs=1)


# --- DSP equivalence vs GNU Radio (per rail) ----------------------------------

@pytest.mark.parametrize("variant", ["add", "sub"])
def test_edge_vectors(variant):
    block_type, gr_factory, _ = _VARIANTS[variant]
    dut = _run_dut(block_type, _EDGE_A, _EDGE_B)
    gr = _gr(gr_factory, _EDGE_A, _EDGE_B)
    res = _compare(dut, gr)
    print(f"\n{variant}_cc edge: I {res.i.summary()} | Q {res.q.summary()}")
    assert res.passed, f"I {res.i.summary()} | Q {res.q.summary()}"


@pytest.mark.parametrize("variant", ["add", "sub"])
@pytest.mark.parametrize("seed", [1, 7, 42])
def test_random_vectors(variant, seed):
    block_type, gr_factory, _ = _VARIANTS[variant]
    a, b = _random_streams(seed)
    dut = _run_dut(block_type, a, b)
    gr = _gr(gr_factory, a, b)
    res = _compare(dut, gr)
    print(f"\n{variant}_cc seed={seed}: I {res.i.summary()} | Q {res.q.summary()}")
    assert res.passed, f"I {res.i.summary()} | Q {res.q.summary()}"


@pytest.mark.parametrize("variant", ["add", "sub"])
@pytest.mark.parametrize("amp", [0.1, 0.25, 0.49])
def test_amplitude_sweep(variant, amp):
    """Parity across amplitude regimes (the param-sweep analogue — the blocks
    have no DSP params), kept in range per rail."""
    block_type, gr_factory, _ = _VARIANTS[variant]
    a, b = _random_streams(99, n=20, amp=amp)
    dut = _run_dut(block_type, a, b)
    gr = _gr(gr_factory, a, b)
    res = _compare(dut, gr)
    print(f"\n{variant}_cc amp={amp}: I {res.i.summary()} | Q {res.q.summary()}")
    assert res.passed, f"I {res.i.summary()} | Q {res.q.summary()}"


# --- bit-exact substrate ------------------------------------------------------

@pytest.mark.parametrize("variant", ["add", "sub"])
@pytest.mark.parametrize("seed", [3, 17, 256])
def test_bitexact_reference(variant, seed):
    """DUT matches the per-rail SATURATING Q15 reference EXACTLY over a stream
    that INCLUDES out-of-range sums on both rails (amp 0.9)."""
    block_type, _, cls = _VARIANTS[variant]
    a, b = _random_streams(seed, n=60, amp=0.9)
    dut = _run_dut(block_type, a, b)
    blk = cls("ref")
    yi, yq = blk.process_reference_q15(
        [_q15(c.real) for c in a], [_q15(c.imag) for c in a],
        [_q15(c.real) for c in b], [_q15(c.imag) for c in b])
    ri = compare_against_grc(dut.i_q15, [_s16(w) / 32768.0 for w in yi],
                             metric=Metric.EXACT, delay=0)
    rq = compare_against_grc(dut.q_q15, [_s16(w) / 32768.0 for w in yq],
                             metric=Metric.EXACT, delay=0)
    print(f"\n{variant}_cc bit-exact seed={seed}: I {ri.summary()} | Q {rq.summary()}")
    assert ri.passed and rq.passed, f"I {ri.summary()} | Q {rq.summary()}"


@pytest.mark.parametrize("variant,a,b,rail_i,rail_q", [
    # add: 0.9+0.9 -> +full on both rails
    ("add", complex(0.9, 0.9), complex(0.9, 0.9), 32767, 32767),
    # add: -0.9-0.9 -> -full on both rails
    ("add", complex(-0.9, -0.9), complex(-0.9, -0.9), -32768, -32768),
    # add, mixed rails: I overflows +, Q overflows -
    ("add", complex(0.9, -0.9), complex(0.9, -0.9), 32767, -32768),
    # sub: 0.9-(-0.9)=1.8 -> +full both rails
    ("sub", complex(0.9, 0.9), complex(-0.9, -0.9), 32767, 32767),
    # sub: -0.9-0.9=-1.8 -> -full both rails
    ("sub", complex(-0.9, -0.9), complex(0.9, 0.9), -32768, -32768),
    # sub, mixed rails
    ("sub", complex(0.9, -0.9), complex(-0.9, 0.9), 32767, -32768),
])
def test_saturates_not_wraps(variant, a, b, rail_i, rail_q):
    """An out-of-range result must PIN to ±full-scale PER RAIL (no wrap)."""
    block_type, _, _ = _VARIANTS[variant]
    astream = [complex(0.1, 0.1), a, complex(-0.2, 0.2), a]
    bstream = [complex(0.05, -0.1), b, complex(0.1, -0.1), b]
    dut = _run_dut(block_type, astream, bstream)
    for idx in (1, 3):
        assert _s16(dut.i_q15[idx]) == rail_i, (
            f"{variant}_cc I rail must saturate to {rail_i}, "
            f"got {_s16(dut.i_q15[idx])}")
        assert _s16(dut.q_q15[idx]) == rail_q, (
            f"{variant}_cc Q rail must saturate to {rail_q}, "
            f"got {_s16(dut.q_q15[idx])}")


# --- MANDATORY mutation tests (INV-4) -----------------------------------------

def _setup(variant, seed=7, n=24):
    block_type, gr_factory, _ = _VARIANTS[variant]
    a, b = _random_streams(seed, n)
    dut = _run_dut(block_type, a, b)
    gr = _gr(gr_factory, a, b)
    return dut, gr, a, b, gr_factory


@pytest.mark.parametrize("variant", ["add", "sub"])
def test_mutation_inverted_output_fails(variant):
    dut, gr, *_ = _setup(variant)
    mut_i = [(0x10000 - (w or 0)) & 0xFFFF for w in dut.i_q15]
    mut_q = [(0x10000 - (w or 0)) & 0xFFFF for w in dut.q_q15]
    res = compare_complex_against_grc(mut_i, mut_q, gr.i, gr.q,
                                      metric=Metric.AMPLITUDE, delay=0, op_count=1)
    assert not res.passed, "gate failed to detect an inverted output!"


@pytest.mark.parametrize("variant", ["add", "sub"])
def test_mutation_wrong_second_stream_fails(variant):
    """A DUT fed the WRONG b stream must fail the (a, b) gate."""
    block_type, gr_factory, _ = _VARIANTS[variant]
    a, b = _random_streams(7, 24)
    _, wrong_b = _random_streams(8, 24)
    dut = _run_dut(block_type, a, wrong_b)
    gr = _gr(gr_factory, a, b)
    res = _compare(dut, gr)
    assert not res.passed, "gate failed to detect a wrong second stream!"


@pytest.mark.parametrize("variant", ["add", "sub"])
def test_mutation_corrupt_q_rail_only_fails(variant):
    """PER-RAIL mutation: corrupting ONLY stream a's imag rail (aq) must fail —
    proof the Q rail is genuinely under test, not shadowed by I."""
    block_type, gr_factory, _ = _VARIANTS[variant]
    a, b = _random_streams(7, 24)
    a_bad = [complex(c.real, -c.imag if abs(c.imag) > 0.05 else 0.3) for c in a]
    dut = _run_dut(block_type, a_bad, b)
    gr = _gr(gr_factory, a, b)
    res = _compare(dut, gr)
    assert not res.passed and res.i.passed, (
        "corrupting aq must fail the gate on the Q rail while I stays clean "
        f"(I passed={res.i.passed}, Q passed={res.q.passed})")


@pytest.mark.parametrize("variant", ["add", "sub"])
def test_mutation_corrupt_i_rail_only_fails(variant):
    """PER-RAIL mutation: corrupting ONLY stream b's real rail (bi) must fail on
    the I rail — proof the I rail's b operand is genuinely under test."""
    block_type, gr_factory, _ = _VARIANTS[variant]
    a, b = _random_streams(7, 24)
    b_bad = [complex(-c.real if abs(c.real) > 0.05 else 0.3, c.imag) for c in b]
    dut = _run_dut(block_type, a, b_bad)
    gr = _gr(gr_factory, a, b)
    res = _compare(dut, gr)
    assert not res.passed and res.q.passed, (
        "corrupting bi must fail the gate on the I rail while Q stays clean "
        f"(I passed={res.i.passed}, Q passed={res.q.passed})")


def test_sub_mutation_swapped_streams_fails():
    """REQUIRED (non-commutative): a SubCC DUT driven (b, a) must FAIL the
    GR (a, b) gate — a - b != b - a."""
    block_type, gr_factory, _ = _VARIANTS["sub"]
    a, b = _random_streams(11, 24)
    dut = _run_dut(block_type, b, a)          # swapped drive
    gr = _gr(gr_factory, a, b)
    res = _compare(dut, gr)
    assert not res.passed, "gate failed to detect swapped sub_cc operands!"


def test_add_swapped_streams_is_commutative():
    """DOCUMENTED: add is commutative — the swapped drive PASSES, so a swapped-
    stream mutation carries no teeth for AddCC (the teeth are the wrong-second-
    stream + per-rail mutations above)."""
    block_type, gr_factory, _ = _VARIANTS["add"]
    a, b = _random_streams(11, 24)
    dut = _run_dut(block_type, b, a)          # swapped drive
    gr = _gr(gr_factory, a, b)
    res = _compare(dut, gr)
    assert res.passed, "add_cc must be commutative (a+b == b+a)"


def test_add_vs_sub_reference_mutation_fails():
    """WRONG-OP mutation: the AddCC DUT must FAIL the sub_cc golden (and thereby
    the shared-module _OP/_SIGN pair is proven load-bearing)."""
    a, b = _random_streams(13, 24)
    dut = _run_dut("AddCCBlock", a, b)
    gr = _gr("blocks.sub_cc()", a, b)
    res = _compare(dut, gr)
    assert not res.passed, "AddCC passed a sub_cc reference — op not under test!"


@pytest.mark.parametrize("variant", ["add", "sub"])
def test_mutation_one_sample_offset_fails(variant):
    dut, gr, *_ = _setup(variant)
    sh_i = [0x0000] + list(dut.i_q15[:-1])
    sh_q = [0x0000] + list(dut.q_q15[:-1])
    res = compare_complex_against_grc(sh_i, sh_q, gr.i, gr.q,
                                      metric=Metric.AMPLITUDE, delay=0, op_count=1)
    assert not res.passed, "gate failed to detect a 1-sample latency error!"


@pytest.mark.parametrize("variant", ["add", "sub"])
def test_empty_output_fails(variant):
    _, gr, *_ = _setup(variant)
    res = compare_complex_against_grc([], [], gr.i, gr.q,
                                      metric=Metric.AMPLITUDE, delay=0, op_count=1)
    assert not res.passed


# --- orientation invariance (INV-23, all 8 D4) --------------------------------

_ORIENT_PARAMS = [
    pytest.param(v, o, id=f"{v}-{'+'.join(o) if o else 'identity'}")
    for v in ("add", "sub") for o in D4_ORIENTATIONS[1:]
]


@pytest.mark.parametrize("variant,orient", _ORIENT_PARAMS)
def test_orientation_invariant(variant, orient):
    """The on-chip output under every D4 orientation must EQUAL the identity
    output (driven through the two-packet complex2 driver)."""
    block_type, _, _ = _VARIANTS[variant]
    a, b = _random_streams(3, 16)
    base = _run_dut(block_type, a, b)
    res = run_block_dut_complex2(block_type, a, b, chip_yaml=CHIP_YAML,
                                 orient=list(orient))
    assert res.ok, f"{block_type} {orient}: {res.reason}"
    ok, detail = compare_dut_results(base, res)
    assert ok, f"{block_type} {'+'.join(orient)}: {detail}"


# --- SATURATED (pipelined) gate — bespoke (INV-19/20) --------------------------
# The shared test_pipeline_saturation harnesses deliver all operands with ONE
# JUMP per sample; this block's samples are TWO packets (two JUMPs) for its
# counting join, so the saturated gate lives here (NEEDS_BESPOKE points here).

@pytest.mark.parametrize("variant", ["add", "sub"])
def test_pipelined_equals_per_sample(variant):
    """Saturated (whole burst queued, ONE continuous run, no inter-sample
    quiescence) output must equal the per-sample output BIT-EXACT — the
    counting join must pace correctly with packets slammed back-to-back."""
    block_type, _, cls = _VARIANTS[variant]
    a, b = _random_streams(21, 32, amp=0.9)   # includes saturating samples
    seq = _run_dut(block_type, a, b)
    seq_flat = [w for g in seq.outputs_q15 for w in g]

    aw = [(_q15(c.real), _q15(c.imag)) for c in a]
    bw = [(_q15(c.real), _q15(c.imag)) for c in b]
    pipe = run_block_dut_complex2_pipelined(block_type, aw, bw,
                                            chip_yaml=CHIP_YAML)
    assert pipe.ok, f"pipelined build/run failed (deadlock/livelock?): {pipe.reason}"

    # PROBE the drive is REAL (the vacuous-drive trap): the saturated run must
    # produce one full (yi, yq) pair per sample and a non-trivial stream that
    # matches the independently-computed Q15 reference — not just [] == [].
    n = len(seq_flat)
    assert n == 2 * len(a), "per-sample run must emit a (yi, yq) pair per sample"
    assert len(pipe.outputs_q15) >= n, (
        f"{block_type}: saturated produced {len(pipe.outputs_q15)} words, "
        f"per-sample produced {n} — pipeline STALLED (join/handshake hazard)")
    yi, yq = cls("ref").process_reference_q15(
        [w for w, _ in aw], [w for _, w in aw],
        [w for w, _ in bw], [w for _, w in bw])
    exp = [w for pair in zip(yi, yq) for w in pair]
    assert len(set(exp)) > 4, "stimulus produced a degenerate reference stream"
    assert seq_flat == exp, "per-sample output must equal the Q15 reference"
    assert pipe.outputs_q15[:n] == seq_flat, (
        f"{block_type}: saturated output diverges from per-sample at index "
        f"{next(i for i in range(n) if pipe.outputs_q15[i] != seq_flat[i])}")


def test_pipelined_drive_is_not_vacuous():
    """MUTATION of the saturated DRIVE itself: swapping the streams in the
    pipelined SubCC drive must CHANGE the output — proof the queued two-packet
    stream actually reaches the datapath (not an empty-vs-empty pass)."""
    a, b = _random_streams(23, 16)
    aw = [(_q15(c.real), _q15(c.imag)) for c in a]
    bw = [(_q15(c.real), _q15(c.imag)) for c in b]
    good = run_block_dut_complex2_pipelined("SubCCBlock", aw, bw,
                                            chip_yaml=CHIP_YAML)
    swapped = run_block_dut_complex2_pipelined("SubCCBlock", bw, aw,
                                               chip_yaml=CHIP_YAML)
    assert good.ok and swapped.ok
    assert good.outputs_q15 and swapped.outputs_q15
    assert good.outputs_q15 != swapped.outputs_q15, (
        "swapping the pipelined sub_cc streams changed nothing — the saturated "
        "drive is vacuous")


# --- GRC import: numeric index counts COMPLEX ports (the 2-stream fix) --------

_TWO_MIXER_GRC = """options:
  parameters: {id: min_addcc, generate_options: qt_gui}
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
- name: comb
  id: kyttar_{VARIANT}_cc
  parameters: {device_id: '"kyttar_0"', num_inputs: '2'}
  states: {coordinate: [300, 160], rotation: 0, state: enabled}
- name: snk
  id: kyttar_sink
  parameters: {device_id: '"kyttar_0"'}
  states: {coordinate: [520, 160], rotation: 0, state: enabled}
connections:
- [src, '0', mixa, '0']
- [src, '0', mixb, '0']
- [mixa, '0', comb, '0']
- [mixb, '0', comb, '1']
- [comb, '0', snk, '0']
"""


@pytest.mark.parametrize("variant", ["add", "sub"])
def test_grc_import_wires_two_streams_and_join(variant):
    """IMPORTER contract for the first >=2-input-I/Q-pair block: GRC's numeric
    port index counts COMPLEX ports, so index 1 must land on the SECOND stream's
    I-half (bi), NOT the first stream's Q-half (aq); the I/Q split synthesises
    aq/bq; ALL four arms are counting-join-elected to the landing cell's join
    entry; and the imported design auto-P&Rs and BUILDS. (Before the
    _resolve_port pair-collapse fix, index 1 wired mixb.yi -> aq and stream b's
    imag rail was silently lost.)"""
    import tempfile
    from engine.catalog import BlockCatalog
    from engine.grc_import import import_grc
    from engine.io.chip_type_io import load_chip_type
    from ui.controller import AppController
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])

    block_type, _, _ = _VARIANTS[variant]
    cat = BlockCatalog.from_gr_kyttar()
    text = _TWO_MIXER_GRC.replace("{VARIANT}", variant)
    with tempfile.NamedTemporaryFile("w", suffix=".grc", delete=False) as tf:
        tf.write(text)
        path = tf.name
    try:
        res = import_grc(path, cat, chip_type="kyttar_10x12")
    finally:
        os.unlink(path)
    assert res.ok and not res.unknown, res.unknown
    comb = next(b.name for b in res.project.blocks if b.type == block_type)
    join_entry, _ = cat.resolved_io(block_type, {})
    arms = {}
    for c in res.project.connections:
        if getattr(c.target, "block", None) == comb:
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


# --- dashboard reports --------------------------------------------------------

@pytest.mark.parametrize("variant", ["add", "sub"])
def test_emit_report(variant):
    block_type, gr_factory, _ = _VARIANTS[variant]
    dut = _run_dut(block_type, _EDGE_A, _EDGE_B)
    gr = _gr(gr_factory, _EDGE_A, _EDGE_B)
    res = _compare(dut, gr)
    assert res.passed, f"I {res.i.summary()} | Q {res.q.summary()}"
    write_report(block_type, res.i, coverage={
        "edge": True, "random": 3, "amplitude_sweep": 3, "bit_exact": True,
        "saturation": True, "orientation_d4": True, "pipelined": True,
        "mutation": True})
