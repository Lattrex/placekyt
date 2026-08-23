# SPDX-License-Identifier: GPL-3.0-or-later
"""ComplexDelayLineBlock — multi-cell distributed COMPLEX delay line, EXACT gate.

The critical enabler for the streaming-FFT track: a pure delay of ``depth``
complex samples, ``out[n] = in[n-depth]`` with a (0,0) zero prefill for
``n < depth``, distributed across chained delay cells so the depth reaches 64
complex samples ON-FABRIC (13 cells — the N=128 streaming-FFT stage-1 need;
depth 32 = 7 cells covers the N=64 stage).

Golden: the exact numpy complex delay (computed INDEPENDENTLY in this file, not
the block's own reference), bit-exact required — the datapath is pure MOVEs, so
the tolerance is 0 LSB, on BOTH rails, at every index (a missing word is a hard
failure). Alignment is asserted the INV-2 way: the shift is a KNOWN integer, so
``dut[:depth] == (0,0)`` and ``dut[depth+k] == x[k]`` — never a free lag search.

THE I/Q SKEW GATE (explicit): the two rails must come out aligned
sample-for-sample — a complex impulse must land at index ``depth`` on BOTH rails
simultaneously, a quadrature tone must keep its per-sample I/Q pairing, and the
MANDATORY mutation that delays ONE rail by ±1 sample must FAIL the gate. A
1-sample I/Q skew is catastrophic for anything coherent downstream and is the
easiest bug for a dual-rail chain to hide.

Mutations (INV-4, all proven to FAIL): depth off-by-one (both directions),
swapped rails, dropped zero prefill (an advance), single-rail ±1 skew, empty.

Cross-check: GNU Radio ``blocks.delay(gr.sizeof_gr_complex, depth)`` is the
behavioural model (same ``[0]*depth + x`` stream) — one live-GR anchor test.

Saturation + orientation + placement legality are gated in the shared suites
(``test_pipeline_saturation.py`` COMPLEX_2IN2OUT, ``test_orientation_invariance
.py``, ``test_placement_legality.py``).

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_complex_delay_line.py -x -q
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
    run_block_dut_complex, run_gnuradio_ref_complex,
    compare_complex_against_grc, write_report, Metric)
from gr_kyttar.placement.blocks.complex_delay_line_block import (  # noqa: E402
    ComplexDelayLineBlock)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_AVAILABLE),
    reason="chip yaml or GNU Radio interpreter absent")

# The verified depth points. 13 is called out: it crosses the single-cell
# boundary of the real-rail ancestor DelayBlock (MAX_DELAY=12) — proof the
# chained line goes where the single cell cannot — and is also this block's own
# first 3-cell depth. 32 = the N=64 FFT stage-1 need; 64 = the N=128 need.
DEPTHS = [1, 2, 8, 12, 13, 24, 32, 64]


# --------------------------------------------------------------------------- util
def _q15(f: float) -> int:
    return max(-32768, min(32767, int(round(f * 32768.0)))) & 0xFFFF


def _stim(seed: int, n: int):
    """Random I/Q float pairs with I != Q (so a rail swap is detectable)."""
    rng = random.Random(seed)
    return [(rng.uniform(-0.9, 0.9), rng.uniform(-0.9, 0.9)) for _ in range(n)]


def _golden_pairs(pairs, depth: int):
    """The INDEPENDENT numpy-style golden: quantize the stimulus to Q15 words
    exactly as the harness injects them, prepend ``depth`` (0,0) pairs, keep the
    first N — out[n] = in[n-depth], zeros for n < depth."""
    q = [(_q15(i), _q15(qq)) for (i, qq) in pairs]
    return ([(0, 0)] * depth + q)[:len(q)]


def _dut(pairs, depth: int):
    dut = run_block_dut_complex(
        "ComplexDelayLineBlock", pairs, params={"depth": depth},
        chip_yaml=CHIP_YAML, in_ports=("xi", "xq"), words_per_sample=2)
    assert dut.ok, dut.reason
    return dut


def _exact(dut_i, dut_q, ref_pairs):
    """THE gate: both rails present at every index and bit-equal to the golden.
    Returns (ok, first_bad_index). Used by the positive tests AND the mutation
    tests, so a mutation failure is a failure of the real gate (INV-4)."""
    n = len(ref_pairs)
    if len(dut_i) < n or len(dut_q) < n:
        return False, min(len(dut_i), len(dut_q))
    for k in range(n):
        if dut_i[k] is None or dut_q[k] is None:
            return False, k
        if (int(dut_i[k]) & 0xFFFF, int(dut_q[k]) & 0xFFFF) != ref_pairs[k]:
            return False, k
    return True, None


# Cache one DUT run per (depth, seed, n) — the mutation tests reuse the correct
# run and corrupt it, so every mutation exercises the same gate the pass used.
_RUNS: dict = {}


def _cached_run(depth: int, seed: int, n: int):
    key = (depth, seed, n)
    if key not in _RUNS:
        pairs = _stim(seed, n)
        _RUNS[key] = (pairs, _dut(pairs, depth))
    return _RUNS[key]


# =============================================================================
# 1. EXACT (tol 0) across the supported depth range, vs the independent golden
# =============================================================================
@pytest.mark.parametrize("depth", DEPTHS)
def test_depth_sweep_exact(depth):
    """out[n] = in[n-depth], bit-exact on both rails, at every verified depth —
    including 13 (past the single-cell ancestor's ceiling), 32 (7 cells) and 64
    (13 cells, the deepest supported chain)."""
    n = max(24, 2 * depth + 12)          # INV-12: stimulus > 2x state depth
    pairs, dut = _cached_run(depth, 1000 + depth, n)
    ok, bad = _exact(dut.i_q15, dut.q_q15, _golden_pairs(pairs, depth))
    assert ok, f"depth={depth}: first mismatch at index {bad}"
    exp_cells = ComplexDelayLineBlock("c", depth=depth).cell_count
    assert exp_cells == ComplexDelayLineBlock._cells_for(depth)


@pytest.mark.parametrize("seed", [11, 22, 33])
def test_random_seeds_exact(seed):
    """Three random seeds at the boundary-crossing depth (13) — bit-exact."""
    pairs, dut = _cached_run(13, seed, 40)
    ok, bad = _exact(dut.i_q15, dut.q_q15, _golden_pairs(pairs, 13))
    assert ok, f"seed={seed}: first mismatch at index {bad}"


def test_cells_per_depth_cost():
    """The cost function the FFT composite budgets on: 1 cell to depth 4, then
    ceil((D-4)/5)+1 — depth 32 = 7 cells, depth 64 = 13 cells."""
    expect = {0: 1, 1: 1, 4: 1, 5: 2, 8: 2, 12: 3, 13: 3,
              24: 5, 32: 7, 40: 9, 64: 13}
    for d, c in expect.items():
        assert ComplexDelayLineBlock("c", depth=d).cell_count == c, (
            f"depth={d}: expected {c} cells")


# =============================================================================
# 2. The zero-prefill contract + exact-shift alignment (INV-2)
# =============================================================================
@pytest.mark.parametrize("depth", [1, 4, 13, 32])
def test_zero_prefill_and_shift_alignment(depth):
    """dut[:depth] == (0,0) exactly, and dut[depth+k] == x[k] against the RAW
    quantized input — the exact-shift teeth a wrong shift cannot satisfy."""
    n = 2 * depth + 16
    pairs, dut = _cached_run(depth, 2000 + depth, n)
    xs = [(_q15(i), _q15(q)) for (i, q) in pairs]
    for k in range(depth):
        got = (int(dut.i_q15[k]) & 0xFFFF, int(dut.q_q15[k]) & 0xFFFF)
        assert got == (0, 0), f"depth={depth}: prefill[{k}] = {got}, not (0,0)"
    for k in range(n - depth):
        got = (int(dut.i_q15[depth + k]) & 0xFFFF,
               int(dut.q_q15[depth + k]) & 0xFFFF)
        assert got == xs[k], (
            f"depth={depth}: shift misaligned at k={k}: {got} != {xs[k]}")


def test_depth_zero_is_identity():
    """depth=0 — the identity pass-through, bit-exact, no shift."""
    pairs, dut = _cached_run(0, 5, 24)
    ok, bad = _exact(dut.i_q15, dut.q_q15, _golden_pairs(pairs, 0))
    assert ok, f"identity mismatch at index {bad}"


# =============================================================================
# 3. THE I/Q SKEW GATE — both rails aligned sample-for-sample
# =============================================================================
@pytest.mark.parametrize("depth", [1, 13, 32])
def test_complex_impulse_iq_alignment(depth):
    """A complex impulse (0.5 + 0.25j at n=0) must come out at index ``depth``
    on BOTH rails SIMULTANEOUSLY, and nowhere else — per-sample proof the I and
    Q chains are matched (no 1-sample skew)."""
    n = depth + 12
    pairs = [(0.0, 0.0)] * n
    pairs[0] = (0.5, 0.25)
    dut = _dut(pairs, depth)
    for k in range(n):
        gi = int(dut.i_q15[k]) & 0xFFFF
        gq = int(dut.q_q15[k]) & 0xFFFF
        if k == depth:
            assert (gi, gq) == (_q15(0.5), _q15(0.25)), (
                f"depth={depth}: impulse at index {k} is ({gi:#x},{gq:#x}) — "
                f"a rail arrived skewed or attenuated")
        else:
            assert (gi, gq) == (0, 0), (
                f"depth={depth}: spurious energy at index {k}: ({gi:#x},{gq:#x})")


def test_quadrature_tone_iq_alignment():
    """A quadrature tone (cos + j·sin) through depth 13: every output pair must
    be the INPUT pair from exactly 13 samples earlier — the per-sample pairing
    (and thus the quadrature phase relationship) survives the chain intact."""
    import math
    D, n = 13, 48
    pairs = [(0.7 * math.cos(2 * math.pi * 0.08 * k),
              0.7 * math.sin(2 * math.pi * 0.08 * k)) for k in range(n)]
    dut = _dut(pairs, D)
    ok, bad = _exact(dut.i_q15, dut.q_q15, _golden_pairs(pairs, D))
    assert ok, f"quadrature tone misaligned at index {bad}"


# =============================================================================
# 4. Live GNU Radio behavioural anchor — blocks.delay on gr_complex
# =============================================================================
def test_matches_live_gr_complex_delay():
    """GR ``blocks.delay(gr.sizeof_gr_complex, 8)`` emits the same
    ``[0]*8 + x`` stream — the DUT matches it EXACTLY (both streams carry the
    prefix; compared at delay=0 like the real-rail DelayBlock suite)."""
    pairs, dut = _cached_run(8, 1008, 28)
    gr = run_gnuradio_ref_complex(
        [complex(i, q) for (i, q) in pairs],
        gnuradio_script="""
from gnuradio import gr, blocks
tb = gr.top_block()
src = blocks.vector_source_c(input_complex, False)
dly = blocks.delay(gr.sizeof_gr_complex, 8)
snk = blocks.vector_sink_c()
tb.connect(src, dly); tb.connect(dly, snk)
tb.run()
output_complex = list(snk.data())
""")
    assert gr.is_complex
    res = compare_complex_against_grc(
        dut.i_q15, dut.q_q15, gr.i, gr.q, metric=Metric.EXACT, delay=0)
    assert res.passed, res.summary()


# =============================================================================
# 5. Bit-exact vs the block's own Q15 reference
# =============================================================================
@pytest.mark.parametrize("depth", [0, 5, 13, 32])
def test_bit_exact_vs_own_reference(depth):
    n = max(24, 2 * depth + 12)
    pairs, dut = _cached_run(depth, 1000 + depth, n)
    ref = ComplexDelayLineBlock("r", depth=depth).process_reference_q15(
        [(_q15(i), _q15(q)) for (i, q) in pairs])
    ok, bad = _exact(dut.i_q15, dut.q_q15, ref)
    assert ok, f"depth={depth}: own-reference mismatch at index {bad}"


# =============================================================================
# 6. Mutations — the gate MUST FAIL on a corrupted DUT (INV-4)
# =============================================================================
def _good_run():
    """The correct depth-13 run the mutations corrupt (13 = the boundary depth)."""
    pairs, dut = _cached_run(13, 4013, 40)
    return pairs, list(dut.i_q15), list(dut.q_q15)


def test_mutation_depth_plus_one_fails():
    """A DUT stream delayed one EXTRA sample must fail the depth-13 golden."""
    pairs, di, dq = _good_run()
    ok, _ = _exact([0] + di[:-1], [0] + dq[:-1], _golden_pairs(pairs, 13))
    assert not ok, "gate did NOT catch a +1 depth mutation"


def test_mutation_depth_minus_one_fails():
    """A DUT stream delayed one sample TOO FEW must fail (golden depth 14 vs a
    13-deep line — the off-by-one in the other direction)."""
    pairs, di, dq = _good_run()
    ok, _ = _exact(di, dq, _golden_pairs(pairs, 14))
    assert not ok, "gate did NOT catch a -1 depth mutation"


def test_mutation_swapped_rails_fails():
    """I and Q swapped anywhere in the chain must fail (stimulus has I != Q)."""
    pairs, di, dq = _good_run()
    ok, _ = _exact(dq, di, _golden_pairs(pairs, 13))
    assert not ok, "gate did NOT catch swapped I/Q rails"


def test_mutation_dropped_zero_prefill_fails():
    """A chain that skips the zero prefill (emits x[k] from n=0 — uninitialised
    or bypassed delay registers) must fail."""
    pairs, di, dq = _good_run()
    ok, _ = _exact(di[13:] + [0] * 13, dq[13:] + [0] * 13,
                   _golden_pairs(pairs, 13))
    assert not ok, "gate did NOT catch a dropped zero prefill (advance)"


@pytest.mark.parametrize("skew", [+1, -1])
def test_mutation_single_rail_skew_fails(skew):
    """THE skew mutation: ONE rail delayed depth±1 while the other stays at
    depth. This is the catastrophic easy bug for a dual-rail chain — the gate
    must catch a single sample of I/Q misalignment in either direction."""
    pairs, di, dq = _good_run()
    if skew > 0:
        dq_skewed = [0] + dq[:-1]        # Q one sample LATE
    else:
        dq_skewed = dq[1:] + [0]         # Q one sample EARLY
    ok, _ = _exact(di, dq_skewed, _golden_pairs(pairs, 13))
    assert not ok, f"gate did NOT catch a {skew:+d}-sample I/Q skew"


def test_mutation_empty_fails():
    pairs, _di, _dq = _good_run()
    ok, _ = _exact([], [], _golden_pairs(pairs, 13))
    assert not ok, "gate did NOT catch an empty DUT output"


# =============================================================================
# 7. Cell budget + the INV-17 fan-out headroom (block-verify time, never build)
# =============================================================================
CELL_WORDS = 32


def _cell_words(prog) -> int:
    """Total word count for one cell: instructions + auto-HALT (R31) + data +
    state + auto-allocated inputs + the two fixed xi/xq landing regs (the
    test_complex_fir_budget model)."""
    ninstr = sum(1 for ln in prog.assembly_template.splitlines()
                 if ln.strip() and not ln.strip().endswith(":"))
    auto_inputs = sum(1 for p in prog.inputs if p.register is None)
    return (ninstr + 1) + len(prog.data) + len(prog.state) + auto_inputs + 2


@pytest.mark.parametrize("depth", [1, 4, 5, 13, 32, 64])
def test_cell_budget_fits(depth):
    """Every cell fits the 32-word budget, and the OUTPUT cell keeps room for
    the INV-17 fan-out re-sequencing (one extra JUMP) — asserted here at
    block-verify time so a user can never hit it as an opaque build failure."""
    blk = ComplexDelayLineBlock("b", depth=depth)
    progs = blk.build_cell_programs()
    last = max(progs.keys())
    for cid, prog in progs.items():
        w = _cell_words(prog)
        assert w <= CELL_WORDS, f"depth={depth} cell {cid}: {w} words > {CELL_WORDS}"
        if cid == last:
            assert w + 1 <= CELL_WORDS, (
                f"depth={depth} output cell: {w}+1 fan-out words > {CELL_WORDS} "
                f"— the complex pair could not fan out to two consumers")


def test_state_registers_never_overlap_inputs():
    """The INV-33 no-data-words trap: this block has ZERO data words, so every
    state register is explicitly pinned at >= R2 (R0/R1 are the xi/xq inputs).
    An auto-allocated state would silently land on the inputs and echo."""
    for depth in (1, 5, 13, 64):
        for prog in ComplexDelayLineBlock("b", depth=depth) \
                .build_cell_programs().values():
            for sv in prog.state:
                assert sv.register is not None and sv.register >= 2, (
                    f"depth={depth}: state {sv.name} not pinned above the "
                    f"input registers (got {sv.register})")


# =============================================================================
# 8. The depth boundary RAISES (never clamps) — INV-0
# =============================================================================
def test_depth_over_budget_raises():
    with pytest.raises(ValueError, match="exceeds the verified on-fabric"):
        ComplexDelayLineBlock("b", depth=ComplexDelayLineBlock.MAX_DEPTH + 1)


def test_depth_negative_raises():
    with pytest.raises(ValueError, match="depth must be >= 0"):
        ComplexDelayLineBlock("b", depth=-1)


# =============================================================================
# 9. Dashboard report
# =============================================================================
def test_emit_report():
    """Persist the measured quality at the FFT-critical depth (32, 7 cells) —
    EXACT on both rails; report the worse rail (both are 0-error)."""
    pairs, dut = _cached_run(32, 1032, 76)
    golden = _golden_pairs(pairs, 32)
    ref_i = [((w if w < 0x8000 else w - 0x10000) / 32768.0) for (w, _q) in golden]
    ref_q = [((w if w < 0x8000 else w - 0x10000) / 32768.0) for (_i, w) in golden]
    res = compare_complex_against_grc(
        dut.i_q15, dut.q_q15, ref_i, ref_q, metric=Metric.EXACT, delay=0)
    assert res.passed, res.summary()
    worse = res.i if res.i.max_abs_err >= res.q.max_abs_err else res.q
    write_report("ComplexDelayLineBlock", worse, coverage={
        "edge": True, "random": 3, "param_sweep": len(DEPTHS) + 1,
        "mutation": True, "max_depth": ComplexDelayLineBlock.MAX_DEPTH,
        "cells_at_depth_64": 13})
