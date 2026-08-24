# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify ChirpSyncBlock — the CSS preamble K-consecutive-equal-argmax run
detector.

There is NO stock GNU Radio streaming counterpart (like BinArgmaxBlock /
Crc16Block), so the golden is the INDEPENDENT numpy/python state machine below,
written from the block's pinned contract:

  * per input index word, ONE packed output word (1:1): the LOCKED BIN once K
    consecutive EQUAL indices have been seen, else the NO-SYNC sentinel 0xFFFF
    (raw -1; the sign bit is the inverted sync flag — a legal index is
    0..32767, so no collision);
  * the run counter SATURATES at K (arbitrarily long preambles, no overflow);
  * a mismatch fully re-arms the run on the NEW value (run = 1);
  * K = 1 is the degenerate always-locked pass-through.

Coverage: preambles at various offsets, absent preambles, broken runs of
exactly K-1, index changes mid-run, back-to-back distinct runs, full random
index streams across 3 seeds and a K sweep, and the locked-bin value REPORT.
Per INV-4 every gate is paired with mutations proven to FAIL: K off-by-one
(on-chip wrong-K build), equality->inequality, no-reset-on-mismatch, run NOT
saturating (overflow model), +1 delay, empty. The saturated (queue_words)
drive with a LOCK-EXERCISING repeated-index stimulus is gated here bespokely
(the shared REAL_1IN stimulus has no equal-adjacent pair, so it exercises only
the sentinel path); the shared registry entry covers the per-sample==saturated
equality on the generic stimulus.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \\
      <venv>/python -m pytest verification/tests/test_chirp_sync.py -q
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

from kyttar_verify import run_block_dut, compare_against_grc, write_report, Metric  # noqa: E402
from kyttar_verify.dut_runner import run_block_dut_pipelined  # noqa: E402
from gr_kyttar.placement.blocks.chirp_sync_block import ChirpSyncBlock  # noqa: E402

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
pytestmark = pytest.mark.skipif(
    not os.path.exists(CHIP_YAML), reason="chip yaml absent")

SENT = 0xFFFF


# --- the independent golden state machine -------------------------------------

def _golden(idxs, k):
    """One packed word per input index: the run's value once it has repeated k
    times consecutively (run saturating at k), else the 0xFFFF sentinel.
    Written independently of the block (two implementations of the pinned
    contract must agree)."""
    out, prev, run = [], None, 0
    for x in idxs:
        x = int(x) & 0xFFFF
        if prev is not None and x == prev:
            run = min(run + 1, k)
        else:
            prev, run = x, 1
        out.append(prev if run >= k else SENT)
    return out


def _random_idxs(seed, count, hi=15):
    rng = random.Random(seed)
    return [rng.randint(0, hi) for _ in range(count)]


def _run_dut(stim, k, **kw):
    dut = run_block_dut("ChirpSyncBlock", stim, params={"k": k},
                        chip_yaml=CHIP_YAML, in_port="idx", out_port="out",
                        **kw)
    assert dut.ok, dut.reason
    return dut


def _words(dut):
    return [w & 0xFFFF for w in dut.outputs_q15]


# --- golden self-consistency --------------------------------------------------

def test_block_reference_matches_independent_golden():
    """The block's own process_reference_q15 == this file's independent golden
    across seeds and the K sweep (first-sample, saturation, and re-arm
    conventions all agree)."""
    for k in (1, 2, 3, 4, 7):
        for seed in (1, 2, 3):
            stim = _random_idxs(seed, 60, hi=3)   # small alphabet -> real runs
            blk = ChirpSyncBlock("r", k=k)
            assert blk.process_reference_q15(stim) == _golden(stim, k), \
                f"k={k} seed={seed}"


# --- exact on-chip vs the golden ----------------------------------------------

@pytest.mark.parametrize("k", [1, 2, 4, 7])
@pytest.mark.parametrize("seed", [1, 7, 42])
def test_exact_vs_golden_random(k, seed):
    """EXACT (tol 0) on random index streams over a small alphabet (real runs
    occur); 1:1 rate (one word per trigger, no None gaps)."""
    stim = _random_idxs(seed, 48, hi=2)
    dut = _run_dut(stim, k)
    assert None not in dut.outputs_q15, "1:1 contract broken (missing words)"
    assert _words(dut) == _golden(stim, k)


def test_preamble_at_offsets_locks_and_reports_bin():
    """A K-run preamble (repeated bin 0 — the aligned dechirped base chirp)
    at several stream offsets: sync asserts EXACTLY at the K-th frame of the
    run and reports the locked bin; the data symbols around it stay
    un-synced (distinct adjacent indices)."""
    k = 4
    for off in (0, 1, 5, 11):
        pre = [0] * 6                               # a 6-frame preamble
        data = [3, 9, 1, 12, 5, 14, 2, 8, 6, 13, 4, 10][:off]
        stim = data + pre + [7, 2, 9]
        dut = _run_dut(stim, k)
        got = _words(dut)
        assert got == _golden(stim, k)
        # sync rises exactly at preamble frame k-1 (0-based) and holds
        lock_start = off + k - 1
        assert all(w == SENT for w in got[:lock_start])
        assert all(w == 0 for w in got[lock_start:off + len(pre)]), \
            f"offset {off}: locked-bin report wrong"
        assert got[off + len(pre)] == SENT, "run break must de-assert sync"


def test_locked_bin_reports_nonzero_bin():
    """The locked-bin REPORT is the run's value, not a constant: a repeated
    bin 9 (a timing-offset preamble — the documented integer-boundary
    limitation shifts the peak bin) locks and reports 9."""
    stim = [1, 9, 9, 9, 9, 9, 2]
    dut = _run_dut(stim, 4)
    assert _words(dut) == [SENT, SENT, SENT, SENT, 9, 9, SENT]


def test_broken_run_of_k_minus_1_never_locks():
    """Runs of exactly K-1 equal indices (broken just before lock) NEVER
    assert sync."""
    k = 4
    stim = [5, 5, 5, 8, 8, 8, 0, 0, 0, 2] * 3      # all runs length 3 = k-1
    dut = _run_dut(stim, k)
    assert all(w == SENT for w in _words(dut))
    assert _golden(stim, k) == [SENT] * len(stim)


def test_index_change_mid_run_rearms():
    """An index change mid-run re-arms on the NEW value: the old run's credit
    never counts toward the new value's run."""
    k = 3
    stim = [4, 4, 7, 7, 7, 4, 4, 4]
    dut = _run_dut(stim, k)
    assert _words(dut) == [SENT, SENT, SENT, SENT, 7, SENT, SENT, 4]


def test_long_preamble_saturates_counter():
    """A preamble much longer than K stays locked throughout (the counter
    saturates at K — no overflow, no de-assert)."""
    k = 4
    stim = [0] * 40 + [1]
    dut = _run_dut(stim, k)
    got = _words(dut)
    assert got[:3] == [SENT] * 3
    assert all(w == 0 for w in got[3:40])
    assert got[40] == SENT


def test_absent_preamble_never_locks():
    """A stream with NO K-run (all adjacent indices distinct) never asserts."""
    stim = [(3 * i + 1) % 13 for i in range(40)]
    assert all(stim[i] != stim[i + 1] for i in range(len(stim) - 1))
    dut = _run_dut(stim, 4)
    assert all(w == SENT for w in _words(dut))


def test_k1_degenerate_passthrough():
    stim = _random_idxs(9, 20, hi=15)
    dut = _run_dut(stim, 1)
    assert _words(dut) == stim


# --- parameter validation ------------------------------------------------------

@pytest.mark.parametrize("bad", [0, -1, 32768, 65536, 2.5])
def test_invalid_k_raises(bad):
    with pytest.raises(ValueError):
        ChirpSyncBlock("bad", k=bad)


def test_k_bounds_construct():
    ChirpSyncBlock("lo", k=1).build_cell_programs()
    blk = ChirpSyncBlock("hi", k=32767)
    blk.build_cell_programs()
    assert blk.process_reference_q15([3] * 32767)[-1] == 3
    assert blk.process_reference_q15([3] * 32766)[-1] == SENT


# --- MANDATORY mutation tests (INV-4) -----------------------------------------

def test_mutation_k_off_by_one_onchip_fails():
    """A REAL on-chip mutant: a DUT built with k=3 against the k=4 golden must
    FAIL (a run of exactly 3 locks the mutant, not the truth)."""
    stim = [5, 5, 5, 1, 0, 0, 0, 0, 2]
    dut = _run_dut(stim, 3)
    assert _words(dut) != _golden(stim, 4), \
        "gate failed to detect a K off-by-one!"


def _mutant_inequality(idxs, k):
    """MUTANT: equality -> inequality (runs count consecutive CHANGES)."""
    out, prev, run = [], None, 0
    for x in idxs:
        x = int(x) & 0xFFFF
        if prev is not None and x != prev:
            run = min(run + 1, k)
        else:
            run = 1
        prev = x
        out.append(prev if run >= k else SENT)
    return out


def test_mutation_equality_flip_fails():
    stim = [0, 0, 0, 0, 0, 3, 7, 1, 9, 2]
    dut = _run_dut(stim, 4)
    got = _words(dut)
    assert got == _golden(stim, 4)
    assert got != _mutant_inequality(stim, 4), \
        "gate failed to detect an inverted (inequality) run test!"


def _mutant_no_reset(idxs, k):
    """MUTANT: the run counter is NOT re-armed on a mismatch (only prev
    updates) — stale credit carries into the new value's run."""
    out, prev, run = [], None, 0
    for x in idxs:
        x = int(x) & 0xFFFF
        if prev is not None and x == prev:
            run = min(run + 1, k)
        else:
            prev = x                      # the DEFECT: run keeps its value
        out.append(prev if run >= k else SENT)
    return out


def test_mutation_no_reset_on_mismatch_fails():
    """The no-reset mutant locks early after an interrupted run — must FAIL.
    Stimulus: 3 equal, a breaker, then 2 equal (truth needs 4 consecutive;
    the mutant's carried run=3 locks one frame after the breaker)."""
    stim = [6, 6, 6, 1, 4, 4, 4, 4, 2]
    dut = _run_dut(stim, 4)
    got = _words(dut)
    assert got == _golden(stim, 4)
    mut = _mutant_no_reset(stim, 4)
    assert mut != _golden(stim, 4)        # the mutant IS blind here
    assert got != mut, "gate failed to detect a missing run re-arm!"


def _mutant_no_saturation(idxs, k):
    """MUTANT: the run counter does NOT saturate — it wraps 16-bit signed on a
    very long run, de-asserting sync (modelled: run > 32767 flips negative)."""
    out, prev, run = [], None, 0
    for x in idxs:
        x = int(x) & 0xFFFF
        if prev is not None and x == prev:
            run += 1
            if run > 32767:
                run = -32768
        else:
            prev, run = x, 1
        out.append(prev if run >= k else SENT)
    return out


def test_mutation_counter_overflow_model_fails_on_long_run():
    """The saturating counter is LOAD-BEARING for arbitrarily long preambles:
    the non-saturating model de-asserts after 32767 repeats (reference-level
    gate; a 32k-word on-chip stream is the golden's own contract)."""
    stim = [0] * 32780
    truth = _golden(stim, 4)
    mut = _mutant_no_saturation(stim, 4)
    assert truth[-1] == 0 and mut[-1] == SENT and truth != mut


def test_mutation_one_sample_delay_fails():
    stim = [0, 0, 0, 0, 5, 5, 5, 5, 1]
    dut = _run_dut(stim, 4)
    delayed = [SENT] + _words(dut)[:-1]
    assert delayed != _golden(stim, 4), \
        "gate failed to detect a +1 sample delay!"


def test_mutation_empty_fails():
    res = compare_against_grc(
        [], [w / 32768.0 if w < 0x8000 else (w - 0x10000) / 32768.0
             for w in _golden([0, 0, 0, 0], 4)],
        metric=Metric.EXACT, delay=0)
    assert not res.passed


# --- saturated drive with a LOCK-EXERCISING stimulus (bespoke) ----------------

def test_saturated_equals_per_sample_with_real_runs():
    """SATURATED (queue_words back-to-back) == per-sample on a stimulus with
    REAL runs (locks asserted and broken) — the shared REAL_1IN registry
    stimulus has no equal-adjacent pair, so the lock path is exercised HERE."""
    k = 4
    stim = [0, 0, 0, 0, 0, 0, 3, 9, 9, 9, 9, 1, 0, 0, 0, 0, 2, 2, 2, 2]
    per_sample = _words(_run_dut(stim, k))
    assert per_sample == _golden(stim, k)
    assert sum(1 for w in per_sample if w != SENT) >= 6, (
        "test premise: the stimulus must actually lock (6 locked frames: 3 in "
        "the 6-long 0-run + 1 each in the three 4-long runs)")
    pip = run_block_dut_pipelined("ChirpSyncBlock", [(w,) for w in stim],
                                  params={"k": k}, chip_yaml=CHIP_YAML,
                                  in_ports=("idx",), out_port="out")
    assert pip.ok, pip.reason
    assert [w & 0xFFFF for w in pip.outputs_q15] == per_sample, \
        "saturated stream diverges from per-sample"


# --- dashboard report ---------------------------------------------------------

def test_emit_report():
    k = 4
    stim = _random_idxs(1, 40, hi=1) + [0] * 8 + _random_idxs(2, 16, hi=3)
    dut = _run_dut(stim, k)
    golden_floats = [(w - 0x10000 if w >= 0x8000 else w) / 32768.0
                     for w in _golden(stim, k)]
    res = compare_against_grc(dut.outputs_q15, golden_floats,
                              metric=Metric.EXACT, delay=0)
    assert res.passed, res.summary()
    assert _words(dut) == _golden(stim, k)
    write_report("ChirpSyncBlock", res, coverage={
        "edge": True, "random": 3, "param_sweep": 4, "bit_exact": True,
        "preamble_offsets": 4, "broken_runs": True, "locked_bin_report": True,
        "saturated_with_locks": True, "mutation": True})
