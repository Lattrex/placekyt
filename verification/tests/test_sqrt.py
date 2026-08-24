# SPDX-License-Identifier: GPL-3.0-or-later
"""SqrtBlock — standalone Q15 square root, verified against LIVE GNU Radio.

GOLDEN: ``blocks.transcendental("sqrt", "float")`` — GR's elementwise libm
dispatcher run with ``name="sqrt"``. It is a real, live GR block (not a numpy
stand-in), so this is a genuine drop-in equivalence gate.

DERIVED TOLERANCE (NOT tuned to pass). The datapath is the RMS family's sqrt
tail (shift-count normalize -> quartic LSQ poly -> denormalize). This suite
MEASURES its error EXHAUSTIVELY over ALL 32768 Q15 input words against the
ideal rounded square root and asserts the bound, then uses ``ceil(|bound|)`` as
the GR-equivalence tolerance:

    exhaustive sweep err in [-4, +1] LSB  ->  TOL_LSB = 5

The bound is re-measured by ``test_sqrt_exhaustive_error_bound`` on every run,
so a datapath change that widens it FAILS the gate rather than silently
relaxing it.

Run::

    cd <repo root>
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      .venv/bin/python -m pytest verification/tests/test_sqrt.py -q
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
    run_block_dut, run_gnuradio_ref, compare_against_grc, write_report, Metric)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
LIB = "lattrex.official"

_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_AVAILABLE),
    reason="chip yaml or GNU Radio interpreter absent")

# DERIVED from the exhaustive sweep below — |min| of the measured [-4, +1] LSB
# interval, rounded up to a whole LSB. NEVER widen this to make a test pass; a
# larger error means the datapath regressed.
TOL_LSB = 5

# EDGE vectors: the two Q15 rails, the exact powers of two that hit every
# shift-count parity, the sub-LSB corner, and the negative/zero domain guards.
EDGE = [0x0000, 0x0001, 0x0002, 0x0003, 0x0004, 0x0010, 0x0100, 0x0400,
        0x1000, 0x2000, 0x3FFF, 0x4000, 0x4001, 0x6000, 0x7FFE, 0x7FFF]


def _sqrt_block():
    from gr_kyttar.placement.blocks import SqrtBlock
    return SqrtBlock("ref")


def _gr_sqrt(inputs_q15):
    """LIVE GNU Radio golden: blocks.transcendental('sqrt', 'float')."""
    return run_gnuradio_ref(
        input_q15=inputs_q15,
        gnuradio_script="""
from gnuradio import gr, blocks
tb = gr.top_block()
# GR's libm sqrt is NaN for a negative input; the Kyttar block CLAMPS the
# negative domain to 0 (a documented HW-DEVIATION), so the golden clamps too.
src = blocks.vector_source_f([x if x > 0.0 else 0.0 for x in input_float], False)
t = blocks.transcendental('sqrt', 'float')
sink = blocks.vector_sink_f()
tb.connect(src, t); tb.connect(t, sink)
tb.run()
output_float = list(sink.data())
""",
    )


def _run(inputs):
    dut = run_block_dut("SqrtBlock", inputs, chip_yaml=CHIP_YAML,
                        in_port="sample", out_port="out")
    assert dut.ok, dut.reason
    return dut


def _compare(dut, inputs, tol=TOL_LSB):
    ref = _gr_sqrt(inputs)
    # Memoryless elementwise map -> group delay 0.
    return compare_against_grc(dut.outputs_q15, ref.floats,
                               metric=Metric.AMPLITUDE, delay=0,
                               tolerance=tol)


# --------------------------------------------------------------------------- #
#  The DERIVED tolerance: measured, not chosen.                                #
# --------------------------------------------------------------------------- #

def test_sqrt_exhaustive_error_bound():
    """EXHAUSTIVE over ALL 32768 Q15 input words: the block's bit-exact model vs
    the ideal rounded sqrt. This MEASURES the bound the GR-equivalence tolerance
    is derived from — if the datapath regresses, this fails FIRST and the
    tolerance is never quietly widened."""
    from gr_kyttar.placement.blocks import SqrtBlock
    lo, hi = 0, 0
    for w in range(0, 32768):
        got = SqrtBlock.sqrt_q15(w)
        ideal = min(32767, int(round(math.sqrt(w / 32768.0) * 32768.0)))
        e = got - ideal
        lo, hi = min(lo, e), max(hi, e)
    print(f"\nexhaustive sqrt error over 32768 words: [{lo}, {hi}] LSB")
    assert (lo, hi) == (-4, 1), (
        f"the sqrt datapath's exhaustive error bound moved to [{lo}, {hi}] LSB "
        f"(was [-4, +1]). TOL_LSB={TOL_LSB} is DERIVED from that bound — fix "
        f"the block, never the tolerance.")
    assert max(abs(lo), abs(hi)) <= TOL_LSB


def test_sqrt_path_is_identical_to_the_rms_sqrt():
    """The standalone block re-uses the RMS family's PROVEN sqrt tail rather than
    a re-derivation, so the two can never drift. Assert word-equality over all
    32768 inputs (the RMS core has no negative domain — it is fed a power word —
    so compare on the non-negative range it defines)."""
    from gr_kyttar.placement.blocks import SqrtBlock
    from gr_kyttar.placement.blocks.rms_block import _RMSCoreBlock
    bad = [w for w in range(1, 32768)
           if SqrtBlock.sqrt_q15(w) != _RMSCoreBlock._sqrt_q15(w)]
    assert not bad, f"{len(bad)} words diverge from the RMS sqrt path, e.g. {bad[:5]}"
    assert SqrtBlock.sqrt_q15(0) == 0


# --------------------------------------------------------------------------- #
#  DUT vs LIVE GNU Radio                                                       #
# --------------------------------------------------------------------------- #

def test_sqrt_edge_vectors():
    """Edge vectors (both rails, every shift-count parity, the sub-LSB corner)."""
    dut = _run(EDGE)
    res = _compare(dut, EDGE)
    print("\nedge:", res.summary(), "| hop", dut.hop_count, "| words", dut.n_words)
    assert res.passed, res.summary()


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_sqrt_random_vectors(seed):
    """Random full-domain stimulus, 3 seeds."""
    rng = random.Random(seed)
    xs = [rng.randint(0, 0x7FFF) for _ in range(24)]
    dut = _run(xs)
    res = _compare(dut, xs)
    print(f"\nrandom seed={seed}:", res.summary())
    assert res.passed, res.summary()


@pytest.mark.parametrize("decade", [0, 1, 2, 3, 4])
def test_sqrt_dense_sweep_by_decade(decade):
    """DENSE on-chip sweep across the WHOLE Q15 input range, split by magnitude
    decade so every shift-count s (and both parities of s) is exercised on the
    real chip — not just the values a random draw happens to pick. This is the
    parameter-sweep equivalent for a block whose 'parameter' is its input
    magnitude."""
    lo = 1 if decade == 0 else 8 ** decade
    hi = min(0x7FFF, 8 ** (decade + 1))
    step = max(1, (hi - lo) // 24)
    xs = list(range(lo, hi, step))[:24]
    assert xs, f"empty decade {decade}"
    dut = _run(xs)
    res = _compare(dut, xs)
    print(f"\ndecade {decade} [{lo},{hi}):", res.summary())
    assert res.passed, res.summary()


def test_sqrt_bit_exact_against_own_reference():
    """BIT-EXACT (tol 0) vs the block's own ``process_reference_q15`` — the
    predictor of the on-chip datapath. This is the strongest per-word gate: it
    proves the built cells compute EXACTLY the modelled program, with no
    tolerance to hide behind."""
    xs = EDGE + [random.Random(11).randint(0, 0x7FFF) for _ in range(24)]
    dut = _run(xs)
    exp = _sqrt_block().process_reference_q15(xs)
    got = [w for w in dut.outputs_q15]
    assert got == exp, [(i, x, g, e) for i, (x, g, e)
                        in enumerate(zip(xs, got, exp)) if g != e][:6]


# --------------------------------------------------------------------------- #
#  Domain edges (x = 0, x -> 1, the negative HW-DEVIATION)                     #
# --------------------------------------------------------------------------- #

class _FakeDut:
    """Carries mutated words through the same compare path as a real DUT."""

    def __init__(self, words):
        self.outputs_q15 = words


def test_sqrt_zero_and_near_one_edges():
    """x = 0 -> 0 EXACTLY (the s=30 sentinel path: 15 right-shifts crush any
    polynomial output to zero), and x -> 1 saturates at the Q15 rail rather than
    reaching an unrepresentable 1.0."""
    xs = [0x0000, 0x0001, 0x7FFE, 0x7FFF]
    dut = _run(xs)
    got = list(dut.outputs_q15)
    assert got[0] == 0, f"sqrt(0) must be EXACTLY 0, got {got[0]}"
    assert got[1] == 181, (
        f"sqrt(1 LSB) = round(sqrt(1/32768)*32768) = 181, got {got[1]}")
    # sqrt(32767/32768) ~= 0.9999847 -> just under the rail; must stay inside Q15.
    assert 32700 <= got[3] <= 32767, got[3]
    assert got[3] == _sqrt_block().process_reference_q15([0x7FFF])[0]


def test_sqrt_negative_input_clamps_to_zero():
    """HW-DEVIATION guard: sqrt of a negative is not real. GR's libm returns NaN;
    on the Q15 datapath a negative word (bit 15 set) would spin the normalize
    loop forever, so the block CLAMPS to 0. Pin that ON CHIP — a documented
    deviation, not an accident."""
    xs = [0x8000, 0x8001, 0xC000, 0xFFFF]
    dut = _run(xs)
    assert list(dut.outputs_q15) == [0, 0, 0, 0], list(dut.outputs_q15)


# --------------------------------------------------------------------------- #
#  MANDATORY mutation tests (INV-4) — the gate must FAIL on a corrupted DUT    #
# --------------------------------------------------------------------------- #

def _poly_norm(w):
    """Shared normalize + Horner used by the mutants: returns (p, s)."""
    from gr_kyttar.placement.blocks.rms_block import (
        _SQRT_C0, _SQRT_C1, _SQRT_C2, _SQRT_C3, _SQRT_C4, _s16)
    ys, s = w, 0
    while ys < 0x4000:
        ys = (ys << 1) & 0xFFFF
        s += 1
    fw = ((ys - 0x4000) << 1) & 0xFFFF
    p = _SQRT_C4
    for c in (_SQRT_C3, _SQRT_C2, _SQRT_C1, _SQRT_C0):
        p = _s16(((p * _s16(fw)) >> 15) + c)
    return p, s


def test_mutation_dropped_denormalize_fails():
    """Drop the DENORMALIZE stage (emit the polynomial's normalized mantissa with
    no 2^(-s/2) scaling). Every input whose shift count s > 0 is then wildly too
    large — the gate MUST fail."""
    def _no_denorm(w):
        if w == 0 or (w & 0x8000):
            return 0
        p, _s = _poly_norm(w)
        return p & 0xFFFF            # <-- missing the 1/sqrt2 and >> (s//2)

    mutated = [_no_denorm(w) for w in EDGE]
    res = _compare(_FakeDut(mutated), EDGE)
    assert not res.passed, "gate failed to detect a DROPPED denormalize stage!"


def test_mutation_wrong_poly_coefficient_fails():
    """Perturb ONE polynomial coefficient (c1, the dominant linear term) by ~3.5%
    and the gate MUST fail — proof the quartic FIT is under test, not just the
    normalize/denormalize scaffolding around it."""
    from gr_kyttar.placement.blocks.rms_block import (
        _SQRT_C0, _SQRT_C1, _SQRT_C2, _SQRT_C3, _SQRT_C4,
        _INV_SQRT2_Q15, _s16)

    def _bad_c1(w, c1):
        if w == 0 or (w & 0x8000):
            return 0
        ys, s = w, 0
        while ys < 0x4000:
            ys = (ys << 1) & 0xFFFF
            s += 1
        fw = ((ys - 0x4000) << 1) & 0xFFFF
        p = _SQRT_C4
        for c in (_SQRT_C3, _SQRT_C2, c1, _SQRT_C0):
            p = _s16(((p * _s16(fw)) >> 15) + c)
        if s & 1:
            p = (p * _INV_SQRT2_Q15) >> 15
        return (p >> (s >> 1)) & 0xFFFF

    mutated = [_bad_c1(w, _SQRT_C1 + 400) for w in EDGE]
    res = _compare(_FakeDut(mutated), EDGE)
    assert not res.passed, "gate failed to detect a perturbed poly coefficient!"


def test_mutation_odd_even_shift_branch_swapped_fails():
    """SWAP the odd/even shift-count branch (apply 1/sqrt(2) on EVEN s instead of
    ODD). Every word in one parity class is then off by a factor of sqrt(2) — the
    gate MUST fail. This is the denormalize stage's load-bearing decision."""
    from gr_kyttar.placement.blocks.rms_block import _INV_SQRT2_Q15

    def _swapped(w):
        if w == 0 or (w & 0x8000):
            return 0
        p, s = _poly_norm(w)
        if not (s & 1):                      # <-- inverted parity test
            p = (p * _INV_SQRT2_Q15) >> 15
        return (p >> (s >> 1)) & 0xFFFF

    mutated = [_swapped(w) for w in EDGE]
    res = _compare(_FakeDut(mutated), EDGE)
    assert not res.passed, "gate failed to detect a swapped odd/even shift branch!"


def test_mutation_inverted_output_fails():
    """A sign-inverted DUT must FAIL."""
    dut = _run(EDGE)
    mutated = [(0x10000 - (w or 0)) & 0xFFFF for w in dut.outputs_q15]
    res = _compare(_FakeDut(mutated), EDGE)
    assert not res.passed, "gate failed to detect an inverted output!"


def test_mutation_one_sample_offset_fails():
    """A +1-sample delay must FAIL when delay=0 is asserted (INV-2)."""
    dut = _run(EDGE)
    shifted = [0x0000] + list(dut.outputs_q15[:-1])
    res = _compare(_FakeDut(shifted), EDGE)
    assert not res.passed, "gate failed to detect a 1-sample latency error!"


def test_empty_output_fails():
    """An empty DUT output is a hard fail (green must not be reachable empty)."""
    ref = _gr_sqrt(EDGE)
    res = compare_against_grc([], ref.floats, metric=Metric.AMPLITUDE,
                              tolerance=TOL_LSB)
    assert not res.passed


# --------------------------------------------------------------------------- #
#  Structure                                                                   #
# --------------------------------------------------------------------------- #

def test_name_param_is_a_documented_deviation():
    """GR's blocks.transcendental takes ``name`` (the libm function) and ``type``.
    Neither is a Kyttar param: ``name`` IS this block's identity (and would
    collide with the placeKYT instance name, which the catalog passes as
    ``cls(name=...)``), and ``type`` has no meaning on a Q15 fabric. Both must be
    declared in GRC_UNSUPPORTED_PARAMS so INV-22's binding gate accepts their
    absence rather than the omission passing unnoticed."""
    from gr_kyttar.placement.blocks import SqrtBlock
    assert set(SqrtBlock.GRC_UNSUPPORTED_PARAMS) == {"name", "type"}
    # And the block genuinely takes NO other params (nothing hidden).
    import inspect
    sig = inspect.signature(SqrtBlock.__init__)
    assert [p for p in sig.parameters if p not in ("self", "name")] == []


def test_layout_is_positional_and_egress_is_last(monkeypatch):
    """INV-35 structural audit: ``default_layout`` is a POSITIONAL INDEX — the
    program cells come first in exactly ``build_cell_programs()`` order, and the
    cell that owns the block's EXTERNAL egress is the LAST program cell. A
    mismatch assigns program A to cell B with NO error (the block builds and
    computes garbage)."""
    from gr_kyttar.placement.blocks import SqrtBlock
    b = SqrtBlock("s")
    layout = list(b.default_layout())
    progs = list(b.build_cell_programs())
    n = len(progs)
    assert layout[:n] == progs, (layout, progs)
    assert all(str(c).startswith("transit") for c in layout[n:])
    assert progs[-1] == b.output_cell_ids()[0]


def test_exit_cell_has_no_goto():
    """EXIT-CELL TRAP guard: a GOTO in the block's EXIT cell is rewritten by the
    build's output-handoff pass into the EXTERNAL output JUMP, so the denormalize
    shift loop would run exactly ONCE (outputs one shift short = exactly 2x for
    s >= 4). The loop MUST use conditional branches only. Source-level assertion
    so a future edit can't silently reintroduce it."""
    from gr_kyttar.placement.blocks import SqrtBlock
    b = SqrtBlock("s")
    exit_cell = b.output_cell_ids()[0]
    tmpl = b.build_cell_programs()[exit_cell].assembly_template
    assert "GOTO" not in tmpl, (
        f"the exit cell '{exit_cell}' contains a GOTO — the output-handoff pass "
        f"will turn it into a stray external JUMP:\n{tmpl}")


@pytest.mark.parametrize("place_xy", [(1, 1), (2, 3), (4, 5), (6, 2), (7, 8),
                                      (0, 0), (3, 9)])
def test_routes_and_computes_from_every_anchor(place_xy):
    """PLACEMENT ROBUSTNESS. 3 cells has NO even-full-column fold, so this block
    is deliberately NOT I/O-co-located (``io_colocated=False``) — INV-14 forbids
    PADDING the last column to force it, because a relay in the egress path makes
    the source-exit WRITE land one cell short and the block emits NOTHING. The
    contract is therefore "get close, then let the router hook it up", and THIS
    test is what makes that claim honest: the block must route AND compute
    correctly from anchors all over the die, not just the harness default."""
    xs = [0, 1, 1000, 8192, 20000, 32767]
    dut = run_block_dut("SqrtBlock", xs, chip_yaml=CHIP_YAML,
                        in_port="sample", out_port="out", place_xy=place_xy)
    assert dut.ok, f"anchor {place_xy}: {dut.reason}"
    assert list(dut.outputs_q15) == _sqrt_block().process_reference_q15(xs), (
        f"anchor {place_xy} computed the wrong words: {dut.outputs_q15}")


def test_emit_report():
    """Emit the dashboard report (records the verified metrics + coverage)."""
    dut = _run(EDGE)
    res = _compare(dut, EDGE)
    assert res.passed, res.summary()
    write_report("SqrtBlock", res, coverage={
        "edge": True, "random": 3, "param_sweep": 5, "mutation": True,
        "exhaustive_sweep": 32768})
