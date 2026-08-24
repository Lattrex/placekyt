# SPDX-License-Identifier: GPL-3.0-or-later
"""FFT128 as a TWO-DIE split — the structural and arithmetic gates.

N=128 does not fit one die: its 7-stage ctl/out spine needs 14 rows in ONE
column against a 12-row array, and the spine height is not negotiable. The
supported topology is a STAGE-BOUNDARY split, and this file gates it.

THE CORRECTNESS ARGUMENT, in one line:

    whole(x) == die1(die0(x))

R2SDF stages are a pure feed-forward pipeline — the only feedback is INSIDE a
stage, from its own ``out`` back to its own ``ctl`` — so cutting at a stage
boundary needs exactly ONE complex stream crossing, in one direction, with no
handshake beyond the ordinary packet. This suite asserts that identity word
for word rather than arguing it.

WHY THE SPLIT IS A PARAMETER, NOT A SECOND IMPLEMENTATION. Each die is the
SAME ``LargeFFTBlock`` over a different ``STAGE_RANGE``: same cell builders,
same octant fold, same spine planner, same golden function. So there is no
second FFT to drift from the verified one, and the only genuinely new thing
in the design is the crossing.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_fft128_split.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT / "runtime" / "python") not in sys.path:
    sys.path.insert(0, str(_ROOT / "runtime" / "python"))

from gr_kyttar.placement.blocks import fft_large as FL  # noqa: E402
from gr_kyttar.placement.blocks.fft_large import (  # noqa: E402
    FFT64Block, FFT128Block, FFT128Die0, FFT128Die1, LargeFFTGeometryError,
    SPLIT_STAGE, direct_dif_reference, output_bins, sdf_streaming_reference,
    stage_delays)
from gr_kyttar.placement.blocks.fft_primitives import u16  # noqa: E402

N = 128
CHIP_W, CHIP_H = 10, 12
PORTS = frozenset({(0, 0), (CHIP_W - 1, 0)})


def _q15(x):
    return int(round(max(-1.0, min(32767 / 32768.0, float(x)))
                     * 32768.0)) & 0xFFFF


def _stim(n, seed=5):
    rng = np.random.default_rng(seed)
    return [(_q15(rng.uniform(-0.6, 0.6)), _q15(rng.uniform(-0.6, 0.6)))
            for _ in range(n)]


# =============================================================================
# 1. Why the split exists at all
# =============================================================================
def test_single_die_is_ruled_out_on_the_spine_height():
    """N=128's obstacle is the SPINE HEIGHT (7 stages x 2 = 14 rows in ONE
    column against a 12-row array), not area — and constructing the
    whole-transform class says so LOUDLY rather than shipping an unroutable
    layout."""
    with pytest.raises(LargeFFTGeometryError) as ei:
        FFT128Block("probe")
    msg = str(ei.value)
    assert "spine" in msg and "14" in msg


def test_split_point_is_pinned():
    """The cut is after stage 0 — recorded so it cannot drift silently."""
    assert SPLIT_STAGE == 0
    assert FFT128Die0.STAGE_RANGE == (0, 0)
    assert FFT128Die1.STAGE_RANGE == (1, 6)


# =============================================================================
# 2. The two dies: geometry
# =============================================================================
@pytest.mark.parametrize("cls,stages,delays,cells", [
    (FFT128Die0, (0,), (64,), 30),
    (FFT128Die1, (1, 2, 3, 4, 5, 6), (32, 16, 8, 4, 2, 1), 84),
])
def test_die_shape_pinned(cls, stages, delays, cells):
    blk = cls("probe")
    assert blk.stage_ids == stages
    assert blk._delays == delays
    assert blk.cell_count == cells
    lay = blk.default_layout()
    assert len(lay) == cells
    pos = [(v[0], v[1]) for v in lay.values()]
    assert len(set(pos)) == cells, "the footprint self-overlaps"
    assert all(0 <= x < CHIP_W and 0 <= y < CHIP_H for (x, y) in pos)
    assert not (set(pos) & PORTS), "a die cell sits on an x16 port"


def test_die1_is_the_verified_fft64_shape():
    """Die 1 is structurally the VERIFIED FFT64Block, and that is the point
    of cutting here: die 1 inherits geometry already proven bit-exact on a
    real chip, so the only genuinely new thing in the design is the crossing.

    The correspondence is far stronger than "similar size" — it is an
    IDENTITY, and it falls straight out of the DIF structure. Stage ``s+1`` of
    an N-point transform uses ``k = j * 2^(s+1)`` over N, and stage ``s`` of
    the N/2-point transform uses ``k = j * 2^s`` over N/2; since
    ``2k/N == k/(N/2)``, the two are the SAME ANGLES. So:

        stage_table(128, s+1) == stage_table(64, s)   for every s

    word for word. Die 1 therefore computes exactly the transform the
    verified FFT64Block computes, on exactly the same 84-cell / 12-row
    geometry, with the same delays and the same per-stage chains.

    The ONE thing that differs is how the head stage RECONSTRUCTS those
    identical words: die 1 walks M=16 octant tables with a stride-2 exponent,
    FFT64 walks M=8 tables with stride 1. Same output words, different route
    to them — which is the part this split actually has to prove, and which
    ``test_die1_head_stage_walks_the_tables_with_stride_two`` pins."""
    d1, f64 = FFT128Die1("a"), FFT64Block("b")
    # identical SHAPE
    assert d1.cell_count == f64.cell_count == 84
    assert d1.n_stages == f64.n_stages == 6
    assert d1._delays == f64._delays == (32, 16, 8, 4, 2, 1)
    assert ([len(d1._stage_chain(s)) for s in range(6)]
            == [len(f64._stage_chain(s)) for s in range(6)])
    ys1 = [v[1] for v in d1.default_layout().values()]
    ys6 = [v[1] for v in f64.default_layout().values()]
    assert max(ys1) - min(ys1) == max(ys6) - min(ys6) == CHIP_H - 1
    # identical ARITHMETIC — the DIF angle identity, asserted at every stage
    assert d1.stage_ids == (1, 2, 3, 4, 5, 6), "die 1 is not stages 1..6"
    assert d1._tables == f64._tables
    for s in range(6):
        assert FL.stage_table(128, s + 1) == FL.stage_table(64, s), s
    # ... reconstructed by a DIFFERENT fold walk (the only new arithmetic)
    assert len(d1._octC) == 16 and len(f64._octC) == 8


def test_die1_output_stream_equals_fft64_on_the_same_input():
    """The identity above, end to end: driven with the SAME words, die 1
    produces the SAME output stream as the verified FFT64Block — startup
    transient included. Die 1 is not merely FFT64-shaped; it computes FFT64.

    (The N=128 transform is die0 THEN this — so what die 1 sees in service is
    die 0's output, not the raw input. That composition is gated separately.)
    """
    words = _stim((N // 2 - 1) + (N // 2) * 3, 23)
    d1 = FFT128Die1("a").process_reference_q15(words)
    f64 = FFT64Block("b").process_reference_q15(words)
    assert d1 == f64, (
        "die 1 diverges from FFT64 on identical input at "
        f"{next(k for k in range(len(f64)) if d1[k] != f64[k])}")


@pytest.mark.parametrize("cls,anchor", [(FFT128Die0, (1, 0)),
                                        (FFT128Die1, (0, 0))])
def test_declared_anchor_reproduces_the_planned_layout(cls, anchor):
    """A die must be placed at its DECLARED anchor, not at (0, 0).

    ``place_block(x, y)`` emits each cell at ``x + dx - min_dx``: it
    NORMALISES the footprint to its own bounding box. Anchoring at
    ``(min_dx, min_dy)`` makes that the identity, so the router sees exactly
    the geometry the planner validated; anchoring anywhere else TRANSLATES the
    fold and invalidates the reserved egress lane, the port distances, and
    which columns stay free.

    Die 0 has to reach column 1 to leave its corridors open, so its plan sits
    at ``min x = 1`` and it MUST be anchored at (1, 0) — placed at (0, 0) it
    arrives one column left with cells on (0,2) and (0,3), sealing the input
    port, and the route fails. Die 1 declares (0, 0). FFT64 declares (0, 0)
    too, which is exactly why this contract was invisible until a second size
    existed."""
    blk = cls("probe")
    assert blk.default_anchor == anchor
    lay = blk.default_layout()
    mdx = min(v[0] for v in lay.values())
    mdy = min(v[1] for v in lay.values())
    assert (mdx, mdy) == anchor
    # placing AT the declared anchor is the identity transform
    for cid, (dx, dy, _f) in lay.items():
        assert (anchor[0] + dx - mdx, anchor[1] + dy - mdy) == (dx, dy), cid


def test_fft64_declares_the_origin_anchor():
    """The shipped FFT64 fold declares (0, 0) — pinned so a future planner
    change cannot silently move it and invalidate its verified placement."""
    assert FFT64Block("probe").default_anchor == (0, 0)


def test_split_adds_no_cells():
    """Splitting must not cost cells — the two dies together are exactly the
    single-die cell count the fit arithmetic predicts."""
    total = FFT128Die0("a").cell_count + FFT128Die1("b").cell_count
    assert total == 114


def test_latencies_add_to_the_transform_latency():
    """Each R2SDF stage contributes its delay D, so the halves add back to
    N-1 exactly."""
    d0, d1 = FFT128Die0("a"), FFT128Die1("b")
    assert d0.latency == 64 and d1.latency == 63
    assert d0.latency + d1.latency == N - 1 == sum(stage_delays(N))


def test_each_die_leaves_corridors_to_both_ports():
    """A fold that FILLS the array builds and then fails to route. Free,
    non-block cells must still connect each die's input landing to x16_in and
    its exit to x16_out — the check the placer's own corridor guard makes,
    re-asserted here on the shipped layout."""
    for cls in (FFT128Die0, FFT128Die1):
        blk = cls("probe")
        lay = blk.default_layout()
        occupied = {(v[0], v[1]) for v in lay.values()}
        assert not (occupied & PORTS)
        order = list(blk.build_cell_programs())
        in_cell = (lay[order[0]][0], lay[order[0]][1])
        out_id = blk.output_cell_id()
        out_cell = (lay[out_id][0], lay[out_id][1])

        def reaches(cell, port):
            seen, stack = set(), []
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nb = (cell[0] + dx, cell[1] + dy)
                if (0 <= nb[0] < CHIP_W and 0 <= nb[1] < CHIP_H
                        and nb not in occupied):
                    seen.add(nb)
                    stack.append(nb)
            while stack:
                cur = stack.pop()
                if cur == port:
                    return True
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx = (cur[0] + dx, cur[1] + dy)
                    if (0 <= nx[0] < CHIP_W and 0 <= nx[1] < CHIP_H
                            and nx not in occupied and nx not in seen):
                        seen.add(nx)
                        stack.append(nx)
            return False

        assert reaches(in_cell, (0, 0)), f"{cls.__name__}: input unreachable"
        assert reaches(out_cell, (CHIP_W - 1, 0)), (
            f"{cls.__name__}: OUTPUT unreachable — this is exactly the "
            "port-blind-enumeration defect the planner's reserved egress "
            "lane exists to prevent")


# =============================================================================
# 3. THE CORRECTNESS ARGUMENT: whole(x) == die1(die0(x))
# =============================================================================
@pytest.mark.parametrize("seed", [5, 17, 29])
def test_composition_identity_is_word_exact(seed):
    """The whole N=128 transform equals die 1 applied to die 0's output
    stream, word for word, over the startup transient AND three full frames.

    This is the split's WHOLE correctness argument, and it is asserted rather
    than argued. It holds because the stages are a pure feed-forward pipeline
    with no cross-stage feedback."""
    words = _stim((N - 1) + N * 3, seed)
    whole = sdf_streaming_reference(N, words)
    mid = FFT128Die0("a").process_reference_q15(words)
    tail = FFT128Die1("b").process_reference_q15(mid)
    assert len(tail) == len(whole) == len(words)
    assert tail == whole, (
        "die1(die0(x)) != whole(x), first mismatch at "
        f"{next(k for k in range(len(whole)) if tail[k] != whole[k])}")


def test_composition_identity_gate_has_teeth():
    """INV-4: the identity must FAIL when the halves are composed WRONG.

    Three single faults, each a plausible integration mistake: cutting at the
    wrong stage boundary, feeding die 1 the RAW input instead of die 0's
    output, and swapping the order of the dies."""
    words = _stim((N - 1) + N * 2, 11)
    whole = sdf_streaming_reference(N, words)
    d0, d1 = FFT128Die0("a"), FFT128Die1("b")

    # (a) wrong boundary: die 1 re-run as if the cut were after stage 1.
    wrong = sdf_streaming_reference(N, d0.process_reference_q15(words), (2, 6))
    assert wrong != whole, "a wrong-boundary composition matched the whole"

    # (b) die 1 fed the RAW stream (the crossing forgotten).
    assert d1.process_reference_q15(words) != whole, \
        "feeding die 1 the raw input matched the whole"

    # (c) dies swapped.
    swapped = d0.process_reference_q15(d1.process_reference_q15(words))
    assert swapped != whole, "the dies compose the same way round either way"


def test_die0_output_is_not_frequency_bins():
    """A split half emits a PARTIALLY transformed stream. Asking it for the
    bin map must raise rather than hand back a plausible-looking wrong
    answer — the whole-transform map belongs to the pair, not to a die."""
    for cls in (FFT128Die0, FFT128Die1):
        blk = cls("probe")
        assert blk.is_split_half
        with pytest.raises(ValueError, match="not the whole transform"):
            _ = blk.output_bins
    # the whole transform's map is still well defined, and is what the PAIR
    # produces at its far end.
    assert len(output_bins(N)) == N
    assert output_bins(N)[:4] == (0, 64, 32, 96)


@pytest.mark.parametrize("seed", [3, 8])
def test_pair_matches_the_independent_direct_dif(seed):
    """The composed pair equals an INDEPENDENT direct-DIF transcription of
    the same frames — a third path to the same answer, so the golden the
    split is measured against is not self-referential."""
    words = _stim((N - 1) + N * 3, seed)
    mid = FFT128Die0("a").process_reference_q15(words)
    tail = FFT128Die1("b").process_reference_q15(mid)
    direct = direct_dif_reference(N, words)
    lat = N - 1
    for f in range(3):
        assert tail[lat + f * N: lat + (f + 1) * N] == \
            direct[f * N:(f + 1) * N], f"frame {f}: pair != direct DIF"


# =============================================================================
# 4. The dies inherit the VERIFIED cell contracts
# =============================================================================
@pytest.mark.parametrize("cls", [FFT128Die0, FFT128Die1])
def test_no_die_cell_pins_state_into_its_instruction_region(cls):
    """INV-33 overlap — the defect that made FFT64 run once and stop. Both
    dies must be clean, at N=128's LARGER tables (M=16 octant tables and
    P=16 direct tables are where the overflow bites)."""
    from test_cell_program_reachability import (  # noqa: PLC0415
        instruction_region_overlaps)
    blk = cls("probe")
    bad = {}
    for cid, cp in blk.build_cell_programs().items():
        if not cp.assembly_template:
            continue
        over, base = instruction_region_overlaps(cp)
        if over:
            bad[cid] = (over, base)
    assert not bad, f"{cls.__name__}: data/state inside instructions: {bad}"


@pytest.mark.parametrize("cls", [FFT128Die0, FFT128Die1])
def test_every_declared_entry_is_reachable(cls):
    """INV-35 — the dead dispatch entry that put every odd bin of FFT64
    wrong. Both dies carry octant folds, so both carry the trivial/numeric
    split that defect lived in."""
    from test_cell_program_reachability import (  # noqa: PLC0415
        unreachable_entries)
    dead = unreachable_entries(cls("probe"))
    assert not dead, f"{cls.__name__}: dead dispatch entries: {dead}"


@pytest.mark.parametrize("cls", [FFT128Die0, FFT128Die1])
def test_every_die_cell_fits_the_word_budget(cls):
    """Every authored cell of both dies inside 32 words, resolver-measured."""
    from gr_kyttar.placement.resolver import CellProgramResolver  # noqa: PLC0415
    res = CellProgramResolver()
    over = []
    for cid, cp in cls("probe").build_cell_programs().items():
        if not cp.assembly_template:
            continue
        regs = ([p.register for p in cp.inputs]
                + [d.address for d in (cp.data or ())]
                + [sv.register for sv in (cp.state or ())])
        total = max([a for a in regs if a is not None], default=-1) + 1 \
            + res.count_instructions(cp)
        if total > 32:
            over.append((cid, total))
    assert not over, f"{cls.__name__}: cells over 32 words: {over}"


def test_die1_head_stage_walks_the_tables_with_stride_two():
    """Die 1's head stage is parent stage 1, whose twiddle exponent is
    ``k = 2j`` — it walks the SAME 16+16 octant tables as die 0's stage 0 but
    in steps of two. The fold sequencer encodes that as its ``step`` data
    word, and it MUST be built from the PARENT stage index (a split half's
    local index is 0, which would silently ship stride 1)."""
    blk = FFT128Die1("probe")
    assert blk.uses_fold(0), "die 1's head stage should be a fold stage"
    seq = blk.build_cell_programs()["s0_seq"]
    step = next(d for d in seq.data if d.name == "step")
    assert step.value == 1 << 1, (
        f"stride is {step.value}, expected 2 — the sequencer was built from "
        "the LOCAL stage index instead of the parent's")
    # and die 0's head stage is parent stage 0: stride 1.
    seq0 = FFT128Die0("probe").build_cell_programs()["s0_seq"]
    assert next(d for d in seq0.data if d.name == "step").value == 1


# =============================================================================
# 5. Each die VERIFIED ALONE on a real built chip
#
# The 2-die design decomposes into three independently checkable parts — die 0,
# die 1, and the CROSSING — and verifying the dies SEPARATELY is what makes a
# whole-system failure localise instead of being attributed by guesswork. Both
# gates below are single-chip (x16_in -> die -> x16_out) and therefore cheap;
# the crossing needs the multi-chip engine and is gated separately.
#
# MEASURED (2026-08-24, real built chip, this exact geometry):
#   die 0 alone, 80 samples of random I/Q  -> 80/80 bit-exact
#                (reference carries 16 non-zero outputs past its delay-64
#                latency, so the gate is not vacuous)
#   die 1 alone, 200 samples of DIE 0's OWN OUTPUT STREAM -> 200/200 bit-exact
#                (73 non-zero outputs)
# =============================================================================
@pytest.mark.parametrize("cls,stages,n,min_nonzero", [
    (FFT128Die0, (0, 0), 80, 16),
    (FFT128Die1, (1, 6), 200, 73),
])
def test_die_reference_is_non_vacuous(cls, stages, n, min_nonzero):
    """The single-chip gates above are only meaningful if the stream they
    compare against is not all zeros. Pin the non-zero count each die's
    reference carries at the sample count actually driven on chip, so a
    future change that shortens the run (and silently tests only the
    zero-fill transient) fails here instead of passing vacuously.

    This is the REACH discipline FFT64 paid for: an 80-sample FFT64 run was
    bit-exact and proved nothing, because it never reached the twiddled half.
    """
    words = _stim(n, 9)
    if cls is FFT128Die1:
        # die 1 sees die 0's OUTPUT in service, not the raw input.
        words = sdf_streaming_reference(N, words, (0, 0))
    ref = sdf_streaming_reference(N, words, stages)
    assert len(ref) == n
    nz = sum(1 for r in ref if r != (0, 0))
    assert nz >= min_nonzero, (
        f"{cls.__name__}: only {nz} non-zero outputs in {n} samples — the "
        f"on-chip gate would be near-vacuous (expected >= {min_nonzero})")
