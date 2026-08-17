# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify BlockInterleaverBlock — the classic rows x cols row-column (matrix)
block interleaver / deinterleaver.

NO stock GNU Radio streaming counterpart exists (gr-fec / gr-dtv interleavers
are PDU/tagged), so the GOLDEN is the standard row-column interleaver of the
coding literature (B. Sklar, *Digital Communications*, 2nd ed., ch. 8
"Interleaving"; S. Lin & D. J. Costello, *Error Control Coding*), with the
write/read order stated LOUDLY:

    WRITE each block of N = rows*cols symbols into the matrix ROW BY ROW
    (arrival order); READ it out COLUMN BY COLUMN. The i-th symbol read comes
    from arrival index sigma(i) = (i mod rows)*cols + (i div rows).
    ``deinterleave=True`` applies the exact inverse permutation
    sigma'(i) = (i mod cols)*rows + (i div cols) (one machinery, both
    directions), so interleave -> deinterleave is the identity.

The golden is INDEPENDENTLY validated in-test against a literal numpy
reshape/transpose formulation before the DUT is held to it.

RATE / LATENCY CONTRACT (INV-2 — asserted as an EXACT shift, never a lag
search): strict 1:1 with a group delay of EXACTLY N samples (double-buffered
ping-pong: block b streams in while block b-1 streams out permuted; the first N
outputs are the initial-buffer zeros). Pure data movement (no arithmetic on the
samples) => the gate is BIT-EXACT (Metric.EXACT, 0 LSB).

Coverage: exhaustive small-config sweeps (both directions), 3 random seeds,
edge words (0x7FFF/0x8000/0/±1 LSB), the exact-latency impulse assertion, an
ON-CHIP interleave -> deinterleave round-trip identity, the burst-dispersion
property (a channel burst of length <= rows corrupts at most ONE symbol per
row-codeword after deinterleaving — the property an FEC row code needs),
SATURATED (pipelined) drive equality, and the INV-4 mutations (no-transpose
passthrough, wrong stride, off-by-one latency, +1 shift, empty), each proven
to FAIL the gate.

HW limit (raises, never clamps): rows*cols <= MAX_DEPTH (12) — the 2N-word
ping-pong buffer must fit the store cell's 32-word register file. Larger
matrices are the documented SRAM-panel growth path (INV-29/31), NOT shipped.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_block_interleaver.py -x -q
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
for p in (str(_PLACEKYT), str(_VERIFY), str(_RUNTIME)):
    if p not in sys.path:
        sys.path.insert(0, p)

from kyttar_verify import (  # noqa: E402
    run_block_dut, compare_against_grc, write_report, Metric)
from kyttar_verify.dut_runner import run_block_dut_pipelined  # noqa: E402
from gr_kyttar.placement.blocks.block_interleaver_block import (  # noqa: E402
    BlockInterleaverBlock, _read_permutation)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
pytestmark = pytest.mark.skipif(
    not os.path.exists(CHIP_YAML), reason="chip yaml absent")

MAXN = BlockInterleaverBlock.MAX_DEPTH

# Every legal (rows, cols) config (rows*cols <= MAX_DEPTH).
_ALL_CONFIGS = [(r, c) for r in range(1, MAXN + 1)
                for c in range(1, MAXN + 1) if r * c <= MAXN]
# The on-chip sweep subset: exhaustive over the small matrices plus the
# boundary/degenerate shapes (full-depth, single-row, single-column).
_CHIP_CONFIGS = [(1, 1), (1, 2), (2, 1), (2, 2), (2, 3), (3, 2), (3, 3),
                 (2, 4), (4, 2), (2, 5), (5, 2), (3, 4), (4, 3), (2, 6),
                 (6, 2), (1, 12), (12, 1)]


# --------------------------------------------------------------------------- util
def _s16(v):
    if v is None:
        return None
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def _random(seed, n=48):
    rng = random.Random(seed)
    # Full 16-bit range: pure data movement, so any word is a legal payload and
    # any byte-level corruption anywhere is caught.
    return [rng.randint(0, 0xFFFF) for _ in range(n)]


def _ref(stim, r, c, deint=False):
    return BlockInterleaverBlock("ref", rows=r, cols=c,
                                 deinterleave=deint).process_reference_q15(stim)


def _ref_floats(ref_words):
    return [_s16(v) / 32768.0 for v in ref_words]


def _dut(stim, r, c, deint=False, **kw):
    return run_block_dut("BlockInterleaverBlock", stim,
                         params={"rows": r, "cols": c, "deinterleave": deint},
                         chip_yaml=CHIP_YAML, **kw)


# =============================================================================
# 0. The GOLDEN itself is validated first (INV-26 discipline: pin the reference
#    before holding the DUT to it).
# =============================================================================
def test_golden_sigma_equals_numpy_reshape_transpose():
    """sigma is EXACTLY 'write row-major, read column-major' by construction:
    reading the row-major matrix column by column == the transpose flattened
    row-major. Independent numpy formulation, exhaustive over all configs."""
    for (r, c) in _ALL_CONFIGS:
        n = r * c
        arrival = np.arange(n)
        matrix = arrival.reshape(r, c)          # written row by row
        col_read = matrix.T.reshape(-1)         # read column by column
        assert list(col_read) == _read_permutation(r, c, False), (r, c)
        # deinterleave = the inverse permutation
        inv = np.empty(n, dtype=int)
        inv[col_read] = arrival
        assert list(inv) == _read_permutation(r, c, True), (r, c)


def test_golden_permutations_are_inverse_pairs():
    for (r, c) in _ALL_CONFIGS:
        n = r * c
        s = _read_permutation(r, c, False)
        sp = _read_permutation(r, c, True)
        assert sorted(s) == list(range(n))
        assert all(s[sp[i]] == i for i in range(n)), (r, c)
        assert all(sp[s[i]] == i for i in range(n)), (r, c)


def test_golden_reference_is_permuted_previous_block():
    """The streaming reference: y[b*N+i] = x[(b-1)*N + sigma(i)], zeros first."""
    r, c = 3, 4
    n = r * c
    x = list(range(1, 1 + 3 * n + 5))           # 3 full blocks + a partial
    y = _ref(x, r, c)
    s = _read_permutation(r, c, False)
    assert y[:n] == [0] * n
    for g in range(n, len(x)):
        b, i = divmod(g, n)
        assert y[g] == x[(b - 1) * n + s[i]], g


# =============================================================================
# 1. On-chip bit-exact equivalence — exhaustive config sweep, both directions
# =============================================================================
@pytest.mark.parametrize("r,c", _CHIP_CONFIGS)
def test_onchip_interleave_bit_exact(r, c):
    stim = _random(1000 * r + 10 * c, n=max(4 * r * c + 3, 24))
    dut = _dut(stim, r, c, False)
    assert dut.ok, dut.reason
    res = compare_against_grc(dut.outputs_q15, _ref_floats(_ref(stim, r, c)),
                              metric=Metric.EXACT, delay=0)
    assert res.passed, f"{r}x{c}: {res.summary()}"


@pytest.mark.parametrize("r,c", _CHIP_CONFIGS)
def test_onchip_deinterleave_bit_exact(r, c):
    stim = _random(2000 * r + 10 * c, n=max(4 * r * c + 3, 24))
    dut = _dut(stim, r, c, True)
    assert dut.ok, dut.reason
    res = compare_against_grc(
        dut.outputs_q15, _ref_floats(_ref(stim, r, c, True)),
        metric=Metric.EXACT, delay=0)
    assert res.passed, f"{r}x{c} deint: {res.summary()}"


@pytest.mark.parametrize("seed", [11, 22, 33])
def test_onchip_random_seeds(seed):
    """Three random seeds on the 2x3 workhorse config — bit-exact each."""
    stim = _random(seed, n=40)
    dut = _dut(stim, 2, 3)
    assert dut.ok, dut.reason
    res = compare_against_grc(dut.outputs_q15, _ref_floats(_ref(stim, 2, 3)),
                              metric=Metric.EXACT, delay=0)
    assert res.passed, f"seed={seed}: {res.summary()}"
    if seed == 11:
        write_report("BlockInterleaverBlock", res,
                     coverage={"edge": True, "random": 3,
                               "param_sweep": len(_CHIP_CONFIGS) * 2,
                               "mutation": True})


def test_onchip_edge_words():
    """Full-scale / sign-boundary words pass through bit-exact (pure data
    movement, no Q15 arithmetic to saturate)."""
    edge = [0x7FFF, 0x8000, 0x0000, 0x0001, 0xFFFF, 0x8001,
            0x7FFE, 0x4000, 0xC000, 0x0002, 0xAAAA, 0x5555]
    stim = edge + edge[::-1] + edge
    dut = _dut(stim, 2, 3)
    assert dut.ok, dut.reason
    ref = _ref(stim, 2, 3)
    assert dut.outputs_q15 == ref


# =============================================================================
# 2. Exact latency (INV-2 — a known integer delay, asserted directly)
# =============================================================================
def test_impulse_lands_at_exactly_n_plus_sigma_inverse():
    """An impulse at arrival index j of block 0 must appear at output index
    N + sigma^-1(j) — the exact group delay + permutation — and NOWHERE else.
    A block with any other latency or permutation cannot satisfy this."""
    r, c = 2, 3
    n = r * c
    s = _read_permutation(r, c, False)
    sinv = {s[i]: i for i in range(n)}
    for j in (0, 2, n - 1):
        stim = [0] * (3 * n)
        stim[j] = 0x4000
        dut = _dut(stim, r, c)
        assert dut.ok, dut.reason
        out = [_s16(v) for v in dut.outputs_q15]
        want = n + sinv[j]
        assert out[want] == 0x4000, f"j={j}: impulse not at {want}: {out}"
        assert all(out[g] == 0 for g in range(len(out)) if g != want), \
            f"j={j}: spurious energy: {out}"


def test_first_block_is_exactly_n_zeros():
    for (r, c) in [(2, 2), (3, 3), (12, 1)]:
        n = r * c
        stim = _random(50 + n, n=2 * n)
        dut = _dut(stim, r, c)
        assert dut.ok, dut.reason
        out = [_s16(v) for v in dut.outputs_q15]
        assert all(v == 0 for v in out[:n]), f"{r}x{c}: startup not zeros"
        # ...and output n is the first real (permuted) sample, not zero-padded.
        s = _read_permutation(r, c, False)
        assert dut.outputs_q15[n] == stim[s[0]]


# =============================================================================
# 3. ON-CHIP round-trip identity: interleave -> deinterleave == pure 2N delay
# =============================================================================
@pytest.mark.parametrize("r,c", [(2, 3), (3, 4), (2, 2)])
def test_onchip_roundtrip_identity(r, c):
    """Chain two REAL chip runs: the interleaver DUT's on-chip output is fed to
    the deinterleaver DUT. The result must be the original stream delayed by
    exactly 2N samples — the transpose-pair identity, all DSP on-chip."""
    n = r * c
    stim = _random(7000 + n, n=4 * n)
    d1 = _dut(stim, r, c, False)
    assert d1.ok, d1.reason
    mid = [int(v) & 0xFFFF for v in d1.outputs_q15]
    d2 = _dut(mid, r, c, True)
    assert d2.ok, d2.reason
    out = [_s16(v) for v in d2.outputs_q15]
    assert all(v == 0 for v in out[:2 * n]), "round-trip startup not zeros"
    xs = [_s16(v) for v in stim]
    assert out[2 * n:] == xs[:len(xs) - 2 * n], \
        "interleave->deinterleave is not the identity (2N delay)"


# =============================================================================
# 4. Burst-error dispersion — the property the FEC chain needs
# =============================================================================
def test_burst_dispersion_property_golden_exhaustive():
    """A channel burst spanning at most `rows` CONSECUTIVE interleaved symbols
    corrupts AT MOST ONE symbol in each row (= each cols-long codeword) after
    deinterleaving. This is the classic guarantee: `rows` consecutive reads walk
    down at most one full column (or the tail of one + the head of the next,
    whose row ranges are disjoint), so no row is hit twice. A row code that
    corrects 1 symbol then heals any such burst. Exhaustive over configs and
    every burst start position, aligned within one block."""
    for (r, c) in _ALL_CONFIGS:
        n = r * c
        s = _read_permutation(r, c, False)
        for L in range(1, r + 1):               # burst length <= rows
            for start in range(0, n - L + 1):   # any position within the block
                hit_rows = [s[start + k] // c for k in range(L)]
                assert len(set(hit_rows)) == L, \
                    (f"{r}x{c}: burst len {L} at {start} hit row(s) twice: "
                     f"{hit_rows}")


def test_burst_dispersion_onchip_demo():
    """The same property demonstrated END-TO-END on the chip: interleave a
    known block on-chip, corrupt `rows` consecutive symbols of the on-chip
    channel stream, deinterleave on-chip, and confirm the errors landed in
    distinct rows (one per cols-long codeword window)."""
    r, c = 3, 4
    n = r * c
    # Two blocks of data + a flush block so the round-trip emits everything.
    data = _random(555, n=n) + [0] * (2 * n)
    d1 = _dut(data, r, c, False)
    assert d1.ok, d1.reason
    chan = [int(v) & 0xFFFF for v in d1.outputs_q15]
    # Corrupt a burst of `rows` consecutive CHANNEL symbols of block 0's
    # interleaved image (which occupies chan[n:2n]).
    burst_at = 5
    for k in range(r):
        chan[n + burst_at + k] ^= 0x5A5A
    d2 = _dut(chan, r, c, True)
    assert d2.ok, d2.reason
    restored = d2.outputs_q15[2 * n:3 * n]      # block 0, restored order
    errors = [i for i in range(n) if restored[i] != data[i]]
    assert len(errors) == r, f"expected {r} corrupted symbols, got {errors}"
    err_rows = {i // c for i in errors}
    assert len(err_rows) == r, \
        f"burst not dispersed one-per-row: error rows {sorted(err_rows)}"


# =============================================================================
# 5. Saturated (pipelined) drive — full-speed streaming equals per-sample
# =============================================================================
@pytest.mark.parametrize("r,c,deint", [(2, 3, False), (3, 4, True),
                                       (12, 1, False)])
def test_saturated_pipelined_bit_exact(r, c, deint):
    """The whole burst enqueued back-to-back (queue_words_physical, one
    continuous run) must yield the SAME bit-exact stream as per-sample drive:
    the 3-cell pipeline (rgen -> wctl -> store, incl. the runtime patch-slot
    store) carries no INV-19/20 hazard. Also the formal REAL_1IN gate in
    test_pipeline_saturation.py covers this block."""
    n = r * c
    stim = _random(90 + n + deint, n=5 * n)
    ref = _ref(stim, r, c, deint)
    res = run_block_dut_pipelined(
        "BlockInterleaverBlock", [(w,) for w in stim],
        params={"rows": r, "cols": c, "deinterleave": deint},
        chip_yaml=CHIP_YAML)
    assert res.ok, res.reason
    assert len(res.outputs_q15) == len(stim), \
        f"1:1 rate violated saturated: {len(res.outputs_q15)}/{len(stim)}"
    assert res.outputs_q15 == ref, "saturated stream != golden"


# =============================================================================
# 6. Mutations — the gate MUST FAIL on a corrupted DUT (INV-4)
# =============================================================================
def test_mutation_no_transpose_passthrough_fails():
    """A DUT that skips the transpose (pure N-delay, identity permutation)
    must FAIL against the interleaver golden."""
    r, c = 2, 3
    n = r * c
    stim = _random(41, n=5 * n)
    ref = _ref_floats(_ref(stim, r, c))
    passthrough = [0] * n + stim[:len(stim) - n]   # delay-only, no permutation
    res = compare_against_grc(passthrough, ref, metric=Metric.EXACT, delay=0)
    assert not res.passed, "gate did NOT catch a no-transpose passthrough"


def test_mutation_wrong_stride_fails():
    """A DUT built with the TRANSPOSED geometry (3x2 instead of 2x3 — the
    wrong column-walk stride, same N) must FAIL against the 2x3 golden."""
    stim = _random(42, n=36)
    wrong = _dut(stim, 3, 2, False)      # wrong stride, correct depth/latency
    assert wrong.ok, wrong.reason
    ref = _ref_floats(_ref(stim, 2, 3))
    res = compare_against_grc(wrong.outputs_q15, ref,
                              metric=Metric.EXACT, delay=0)
    assert not res.passed, "gate did NOT catch a wrong-stride (transposed) DUT"


def test_mutation_off_by_one_block_latency_fails():
    """A DUT with an off-by-one block length (N-1 startup zeros instead of N —
    e.g. a wrap bound off by one) must FAIL."""
    r, c = 2, 3
    n = r * c
    stim = _random(43, n=5 * n)
    good = _dut(stim, r, c)
    assert good.ok, good.reason
    mutated = good.outputs_q15[1:] + [0]         # everything one sample early
    ref = _ref_floats(_ref(stim, r, c))
    res = compare_against_grc(mutated, ref, metric=Metric.EXACT, delay=0)
    assert not res.passed, "gate did NOT catch an off-by-one latency"


def test_mutation_plus_one_shift_fails():
    """A +1 extra sample of delay must FAIL (the INV-2 no-free-lag teeth)."""
    r, c = 2, 3
    stim = _random(44, n=30)
    good = _dut(stim, r, c)
    assert good.ok, good.reason
    shifted = [0] + good.outputs_q15[:-1]
    ref = _ref_floats(_ref(stim, r, c))
    res = compare_against_grc(shifted, ref, metric=Metric.EXACT, delay=0)
    assert not res.passed, "gate did NOT catch a +1 shift"


def test_mutation_empty_fails():
    ref = _ref_floats(_ref(_random(45, n=24), 2, 3))
    res = compare_against_grc([], ref, metric=Metric.EXACT, delay=0)
    assert not res.passed, "gate did NOT catch an empty output"


# =============================================================================
# 7. Hardware limit — RAISES, never clamps (INV-0 deviation protocol)
# =============================================================================
def test_depth_over_budget_raises():
    for (r, c) in [(13, 1), (1, 13), (4, 4), (2, 7), (12, 12)]:
        with pytest.raises(ValueError, match="MAX_DEPTH"):
            BlockInterleaverBlock("x", rows=r, cols=c)


def test_bad_dims_raise():
    for (r, c) in [(0, 3), (3, 0), (-1, 2)]:
        with pytest.raises(ValueError):
            BlockInterleaverBlock("x", rows=r, cols=c)


def test_max_depth_builds_and_runs():
    """The full-depth 12-word matrix (both degenerate and 3x4/4x3 shapes)
    builds, routes, and is bit-exact — the shipped ceiling is real."""
    for (r, c) in [(3, 4), (4, 3)]:
        stim = _random(60 + r, n=5 * r * c)
        dut = _dut(stim, r, c)
        assert dut.ok, dut.reason
        assert None not in dut.outputs_q15, f"{r}x{c}: dropped an output"
        assert dut.outputs_q15 == _ref(stim, r, c)


# =============================================================================
# 8. The runtime patch-slot idiom's resolver contract (regression for the
#    classify_addresses fix this block introduced)
# =============================================================================
def test_pinned_input_in_instruction_range_keeps_input_role():
    """An input Port explicitly pinned INSIDE the instruction range (the store
    cell's patch slot) must classify as INPUT, so the router resolves internal
    patch WRITEs to it — reclassifying it 'instruction' silently misroutes the
    patch to the cell's first input register (the zero-output bug this block
    found)."""
    from gr_kyttar.placement.resolver import CellProgramResolver
    blk = BlockInterleaverBlock("t", rows=2, cols=3)
    store = blk.build_cell_programs()["store"]
    cls = CellProgramResolver().classify_addresses(store)
    slot_reg = next(p.register for p in store.inputs if p.name == "slot")
    assert cls[slot_reg]["role"] == "input"
    assert cls[slot_reg]["name"] == "slot"
    # ...and the write-local patch base matches the assembler's encoding.
    import simkyt
    words = simkyt.Program.from_source(
        "t", "start:\n    WRITE @0, 5\n    HALT\n", 20).get_words()
    assert words[0] == (BlockInterleaverBlock.WRITE_LOCAL_BASE | 5)
