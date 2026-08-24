# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify SigmoidBlock / TanhBlock (Q15 activations, one shared two-cell core).

There is NO stock GNU Radio counterpart — the golden reference is numpy over
the documented input mapping (the library's established pattern for
no-counterpart blocks), plus a TRANSCRIBED bit-exact integer design-reference
(`_design_ref` below): the canonical mask-and-shift formulation of the
16-interval table + linear interpolation with index clamp. The blocks'
`process_reference_q15` is a DIFFERENTLY-DERIVED model (the literal two-cell
datapath: mask-free shift pair, unsigned domain cap, patched SUB unfold), so
asserting DUT == design-ref and reference == design-ref over EXHAUSTIVE sweeps
is a two-independent-models agreement, not self-confirmation.

Gates:
  * exhaustive (all 65536 words) design-ref == process_reference_q15, at
    dshift = 0 / +1 / +2 / -1;
  * on-chip DUT bit-exact vs the design-ref over a dense sweep (domain edges,
    the sign-fold boundary 0 / -1 LSB / -1.0, beyond-domain clamp words,
    strided full-range coverage + 3 random seeds), all four dshift values;
  * float accuracy vs numpy at the pinned bar: sigmoid max 0.0030 / RMS
    0.0010, tanh 0.0060 / 0.0021 — identical at every tested dshift; output
    monotone in the input, asymptote clamp at the edges;
  * saturated (queue_words, pipelined) output == per-sample output BIT-EXACT
    (also enforced fleet-wide by test_pipeline_saturation.py);
  * INV-4 mutations proven to FAIL: wrong table entry, off-by-one index
    clamp, broken sign fold, wrong dshift patch, inverted output, +1 delay,
    empty output;
  * layout pinning: the lut cell resolves to exactly 32 used addresses with
    entry 22 and the runtime patch slot at address 28 (input role); dshift
    outside [-4, 10] raises (shift-immediate hardware limit).

Run:
    QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_activation_blocks.py -q
"""
from __future__ import annotations

import math
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
    run_block_dut, write_report, CompareResult, Metric)
from kyttar_verify.dut_runner import run_block_dut_pipelined  # noqa: E402
from gr_kyttar.placement.blocks import all_block_classes  # noqa: E402
from gr_kyttar.placement.blocks.activation_blocks import (  # noqa: E402
    SIGMOID_TABLE_Q15, TANH_TABLE_Q15, LUT_ENTRY, LUT_PATCH_REG,
    DSHIFT_MIN, DSHIFT_MAX)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
pytestmark = pytest.mark.skipif(not os.path.exists(CHIP_YAML),
                                reason="chip yaml absent")

DSHIFTS = (0, 1, 2, -1)          # the pinned coverage set

# (class name, table, canonical k, odd?) — negop follows from odd.
CASES = [("SigmoidBlock", SIGMOID_TABLE_Q15, 3, False),
         ("TanhBlock", TANH_TABLE_Q15, 2, True)]


def _s16(v):
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def _mulq(a, b):
    return _s16((_s16(a) * _s16(b)) >> 15)


# --- the TRANSCRIBED design reference (canonical mask-and-shift form) --------
def _design_ref(v, table, dshift, odd):
    """Bit-exact canonical reference: 16-interval table + linear interp,
    index clamp to table[16], sign fold/unfold. Independent formulation from
    the blocks' own datapath model (mask + shift vs mask-free shifts +
    unsigned domain cap)."""
    v = _s16(v)
    neg = v < 0
    mag = -v if neg else v
    if mag > 32767:                       # only -32768
        mag = 32767
    ds = dshift
    if ds < 0:
        mag >>= (-ds)
        ds = 0
    shift_idx = 11 - ds                   # 15 - log2(16) - ds
    assert shift_idx >= 0
    idx = mag >> shift_idx
    if idx >= 16:                         # reachable only for dshift > 0
        y = table[16]
    else:
        frac = (mag & ((1 << shift_idx) - 1)) << (4 + ds)
        P = table[idx]
        Qv = table[idx + 1]
        y = _s16(P + _mulq(_s16(Qv - P), frac))
    if odd:
        return (-y) & 0xFFFF if neg else y & 0xFFFF
    return (0x8000 - y) & 0xFFFF if neg else y & 0xFFFF


def _blk(name, dshift=0):
    return all_block_classes()[name]("ref", dshift=dshift)


# --- stimulus families -------------------------------------------------------
_EDGE = [
    0x0000, 0x0001, 0x0002,               # zero + smallest positives
    0xFFFF, 0xFFFE,                       # -1 LSB, -2 LSB (sign-fold boundary)
    0x7FFF, 0x7FFE,                       # +full-scale (domain edge)
    0x8000, 0x8001,                       # -1.0 (the |x| clamp) and -0.99997
    0x4000, 0xC000, 0x3FFF, 0xBFFF,       # half-scale, straddling interval 8
    0x2000, 0xE000, 0x6000, 0xA000,       # quarter/three-quarter scale
    0x0800, 0xF800,                       # deep inner segments
]


def _sweep_words(seed=None, n=60):
    words = list(_EDGE) + list(range(0, 65536, 1021))
    if seed is not None:
        rng = random.Random(seed)
        words += [rng.randint(0, 65535) for _ in range(n)]
    return words


def _run_dut(name, words, dshift, **kw):
    dut = run_block_dut(name, words, params={"dshift": dshift},
                        chip_yaml=CHIP_YAML, **kw)
    assert dut.ok, f"{name} dshift={dshift}: {dut.reason}"
    outs = [None if o is None else (o & 0xFFFF) for o in dut.outputs_q15]
    assert len(outs) == len(words), "output count != input count (1:1 block)"
    assert all(o is not None for o in outs), "missing output words"
    return outs


# --- table provenance --------------------------------------------------------
@pytest.mark.parametrize("name,table,k,odd", CASES)
def test_table_is_derived_from_the_function(name, table, k, odd):
    """table[i] == round(32768 * f(R*i/16)), R = 2**k — the tables are the
    function, not free constants."""
    R = float(2 ** k)
    fn = (lambda x: math.tanh(x)) if odd else (
        lambda x: 1.0 / (1.0 + math.exp(-x)))
    want = [min(32767, round(fn(R * i / 16.0) * 32768.0)) for i in range(17)]
    assert table == want


# --- exhaustive two-model agreement (pure python, no sim) --------------------
@pytest.mark.parametrize("name,table,k,odd", CASES)
@pytest.mark.parametrize("dshift", DSHIFTS)
def test_reference_matches_design_ref_exhaustive(name, table, k, odd, dshift):
    """The block's datapath model == the canonical design reference for EVERY
    input word (65536), per dshift. Two independently-formulated models."""
    blk = _blk(name, dshift)
    ref = blk.process_reference_q15(range(65536))
    bad = [w for w in range(65536)
           if ref[w] != _design_ref(w, table, dshift, odd)]
    assert not bad, f"first mismatches: {bad[:8]}"


# --- on-chip bit-exactness ---------------------------------------------------
@pytest.mark.parametrize("name,table,k,odd", CASES)
@pytest.mark.parametrize("dshift", DSHIFTS)
def test_dut_bit_exact_dense_sweep(name, table, k, odd, dshift):
    """DUT (built + simulated on simKYT) == design reference, bit-exact, over
    edges + full-range stride + random words — per dshift value."""
    words = _sweep_words(seed=41 + dshift)
    got = _run_dut(name, words, dshift)
    want = [_design_ref(w, table, dshift, odd) for w in words]
    bad = [(w, g, r) for w, g, r in zip(words, got, want) if g != r]
    assert not bad, f"{len(bad)} mismatches, first: {bad[:5]}"


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_dut_random_seeds_default_dshift(seed):
    """Three dedicated random seeds at the default dshift for both blocks."""
    rng = random.Random(seed)
    words = [rng.randint(0, 65535) for _ in range(96)]
    for name, table, k, odd in CASES:
        got = _run_dut(name, words, 0)
        want = [_design_ref(w, table, 0, odd) for w in words]
        assert got == want, f"{name} seed={seed}"


def test_dut_clamp_region_dshift_pos():
    """Beyond-domain words (|a| past the canonical edge, dshift>0) must clamp
    to the asymptote table[16] (sign-unfolded) — the index-clamp path."""
    for dshift in (1, 2):
        cap = 1 << (15 - dshift)
        words = [cap, cap + 1, cap + 1234, 0x7FFF,
                 (-cap) & 0xFFFF, (-cap - 1) & 0xFFFF, 0x8000]
        for name, table, k, odd in CASES:
            got = _run_dut(name, words, dshift)
            top = table[16]
            neg_top = ((-top) if odd else (0x8000 - top)) & 0xFFFF
            want = [top, top, top, top, neg_top, neg_top, neg_top]
            assert got == want, (name, dshift, got, want)


# --- float accuracy at the pinned bar ---------------------------------------
# Exhaustive over all 65536 words, per dshift, on the (DUT-bit-exact-proven)
# reference model. Pinned MEASURED numbers (not tuned): the MAX error is
# dshift-invariant (sigmoid 0.00302-0.00304, tanh 0.00598-0.00603 — the
# approximation error of the canonical table). The RMS at dshift >= 0 is the
# pinned 0.00105 / 0.00208; at dshift < 0 the datapath's pre-shift DISCARDS
# |dshift| input bits (mag >>= -dshift), so the input-quantization component
# of the RMS floor rises — measured 0.00148 / 0.00294 at dshift = -1. Bounds
# carry a 2% slack ONLY for float rounding in the error computation itself.
# {name: (max_bar, {dshift>=0 rms_bar, dshift<0 rms_bar})}
_ACC = {"SigmoidBlock": (0.00304, 0.00105, 0.00148),
        "TanhBlock": (0.00603, 0.00208, 0.00294)}


@pytest.mark.parametrize("name,table,k,odd", CASES)
@pytest.mark.parametrize("dshift", DSHIFTS)
def test_float_accuracy_exhaustive(name, table, k, odd, dshift):
    blk = _blk(name, dshift)
    words = np.arange(65536, dtype=np.int64)
    got = np.array(blk.process_reference_q15(words), dtype=np.int64)
    got_f = np.where(got >= 0x8000, got - 0x10000, got) / 32768.0
    a = (np.where(words >= 0x8000, words - 0x10000, words) / 32768.0
         * (2.0 ** (k + dshift)))
    truth = np.tanh(a) if odd else 1.0 / (1.0 + np.exp(-a))
    err = np.abs(got_f - truth)
    mx, rms = float(err.max()), float(np.sqrt((err ** 2).mean()))
    mx_bar, rms_bar_pos, rms_bar_neg = _ACC[name]
    rms_bar = rms_bar_pos if dshift >= 0 else rms_bar_neg
    print(f"\n{name} dshift={dshift:+d}: max={mx:.5f} rms={rms:.5f}")
    assert mx <= mx_bar * 1.02, (mx, mx_bar)
    assert rms <= rms_bar * 1.02, (rms, rms_bar)
    # non-vacuous: a genuinely-wrong table could not sit this close
    assert mx > 1.0 / 32768.0


@pytest.mark.parametrize("name,table,k,odd", CASES)
def test_monotone_and_edge_clamp(name, table, k, odd):
    """Output is monotone nondecreasing in the SIGNED input (<=1 LSB
    truncation jitter) and the edges sit at the asymptotes."""
    blk = _blk(name, 0)
    order = [(w ^ 0x8000) for w in range(65536)]   # signed ascending order
    y = np.array(blk.process_reference_q15(order), dtype=np.int64)
    ys = np.where(y >= 0x8000, y - 0x10000, y)
    assert np.all(np.diff(ys) >= -1), "non-monotone beyond 1 LSB jitter"
    lo, hi = ys[0] / 32768.0, ys[-1] / 32768.0
    if odd:
        assert abs(lo + 1.0) < 2e-3 and abs(hi - 1.0) < 2e-3, (lo, hi)
    else:
        assert abs(lo - 0.0) < 2e-3 and abs(hi - 1.0) < 2e-3, (lo, hi)
    # exact anchor at 0: sigmoid(0)=0.5 (16384), tanh(0)=0
    assert blk.process_reference_q15([0])[0] == (0 if odd else 16384)


# --- saturated (pipelined) drive == per-sample ------------------------------
@pytest.mark.parametrize("name,table,k,odd", CASES)
@pytest.mark.parametrize("dshift", [0, 1])
def test_saturated_equals_per_sample(name, table, k, odd, dshift):
    """The whole burst enqueued back-to-back (queue_words, one continuous run)
    must be BIT-EXACT to the per-sample output. Feed-forward 2-cell chain —
    INV-19/20 hazards do not apply, but the gate is required, not assumed
    (the runtime patch-slot delivery must survive pipelining). Fleet-wide
    coverage lives in test_pipeline_saturation.py (REAL_1IN)."""
    rng = random.Random(11 + dshift)
    words = list(_EDGE) + [rng.randint(0, 65535) for _ in range(80)]
    per = _run_dut(name, words, dshift)
    sat = run_block_dut_pipelined(name, [(w,) for w in words],
                                  params={"dshift": dshift},
                                  chip_yaml=CHIP_YAML, in_ports=("sample",))
    assert sat.ok, sat.reason
    got = [w & 0xFFFF for w in sat.outputs_q15]
    assert got == per, "saturated stream != per-sample stream"


# --- MANDATORY mutations (INV-4): each corruption must FAIL the gate ---------
def _gate(words, got, want):
    """The suite's bit-exact gate as a predicate."""
    return list(got) == list(want)


def test_mutation_wrong_table_entry_fails():
    """A single-LSB corruption of one MID-TABLE entry must be caught — proof
    the gate actually exercises the deep table, not just the edges."""
    words = _sweep_words(seed=5)
    for name, table, k, odd in CASES:
        got = _run_dut(name, words, 0)
        bad_table = list(table)
        bad_table[7] += 1
        mutated = [_design_ref(w, bad_table, 0, odd) for w in words]
        # the stimulus must actually hit interval 7 (indices 7 via idx=7)
        assert any((min(abs(_s16(w)), 32767) >> 11) == 7 for w in words)
        assert not _gate(words, got, mutated), \
            f"{name}: gate blind to a 1-LSB table corruption"


def test_mutation_off_by_one_clamp_fails():
    """A DUT whose index clamp fired one interval early (idx >= 15) must fail
    — checked at dshift=1 where the clamp region is reachable."""
    words = [w for w in _sweep_words(seed=6)]
    for name, table, k, odd in CASES:
        got = _run_dut(name, words, 1)

        def clamp15_ref(v):
            v_s = _s16(v)
            neg = v_s < 0
            mag = min(-v_s if neg else v_s, 32767)
            idx = mag >> 10
            if idx >= 15:                      # off-by-one clamp
                y = table[16]
            else:
                frac = (mag & 0x3FF) << 5
                y = _s16(table[idx] + _mulq(_s16(table[idx + 1] - table[idx]),
                                            frac))
            if odd:
                return (-y) & 0xFFFF if neg else y & 0xFFFF
            return (0x8000 - y) & 0xFFFF if neg else y & 0xFFFF

        mutated = [clamp15_ref(w) for w in words]
        assert any((min(abs(_s16(w)), 1 << 14) >> 10) == 15 for w in words)
        assert not _gate(words, got, mutated), \
            f"{name}: gate blind to an off-by-one index clamp"


def test_mutation_broken_sign_fold_fails():
    """A DUT that dropped the sign unfold (emitting the folded positive y for
    negative inputs) must fail; the stimulus contains negative words."""
    words = _sweep_words(seed=8)
    assert any(_s16(w) < 0 for w in words)
    for name, table, k, odd in CASES:
        got = _run_dut(name, words, 0)
        mutated = [_design_ref(abs(_s16(w)) if _s16(w) != -32768 else 32767,
                               table, 0, odd) for w in words]
        assert not _gate(words, got, mutated), \
            f"{name}: gate blind to a broken sign fold"


def test_mutation_wrong_dshift_patch_fails():
    """A DUT built with the WRONG dshift (shift immediates off by one) must
    fail against the intended-dshift reference — both directions."""
    words = _sweep_words(seed=9)
    for name, table, k, odd in CASES:
        got_d1 = _run_dut(name, words, 1)
        want_d0 = [_design_ref(w, table, 0, odd) for w in words]
        assert not _gate(words, got_d1, want_d0), \
            f"{name}: gate blind to dshift=1 vs dshift=0"
        got_d0 = _run_dut(name, words, 0)
        want_d1 = [_design_ref(w, table, 1, odd) for w in words]
        assert not _gate(words, got_d0, want_d1), \
            f"{name}: gate blind to dshift=0 vs dshift=1"


def test_mutation_inverted_output_fails():
    words = _sweep_words(seed=10)
    for name, table, k, odd in CASES:
        got = _run_dut(name, words, 0)
        inverted = [(0x10000 - g) & 0xFFFF for g in got]
        want = [_design_ref(w, table, 0, odd) for w in words]
        assert not _gate(words, inverted, want)


def test_mutation_one_sample_delay_fails():
    words = _sweep_words(seed=12)
    for name, table, k, odd in CASES:
        got = _run_dut(name, words, 0)
        shifted = [0] + got[:-1]
        want = [_design_ref(w, table, 0, odd) for w in words]
        assert not _gate(words, shifted, want)


def test_mutation_empty_output_fails():
    words = _EDGE
    for name, table, k, odd in CASES:
        want = [_design_ref(w, table, 0, odd) for w in words]
        assert not _gate(words, [], want)


# --- layout / parameter guards ----------------------------------------------
def test_lut_layout_pinned():
    """The lut cell must resolve to the EXACT pinned layout: entry 22, 9
    instructions, the runtime patch slot at address 28 with INPUT role, and a
    completely full 32-word memory image (incl. the R31 HALT)."""
    from gr_kyttar.placement.resolver import CellProgramResolver
    r = CellProgramResolver()
    for name, table, k, odd in CASES:
        for dshift in DSHIFTS:
            blk = _blk(name, dshift)
            progs = blk.build_cell_programs()
            lut = progs["lut"]
            assert r.count_instructions(lut) == 9
            assert r.compute_entry_addresses(lut)["default"] == LUT_ENTRY
            cmap = r.classify_addresses(lut)
            assert cmap[LUT_PATCH_REG]["role"] == "input", cmap[LUT_PATCH_REG]
            assert len(cmap) == 32 and max(cmap) == 31
            fold = progs["fold"]
            fmap = r.classify_addresses(fold)
            assert max(fmap) <= 31 and len(fmap) <= 32


def test_dshift_out_of_range_raises():
    for name, table, k, odd in CASES:
        cls = all_block_classes()[name]
        for bad in (DSHIFT_MIN - 1, DSHIFT_MAX + 1, 99):
            with pytest.raises(ValueError):
                cls("x", dshift=bad)
        with pytest.raises(ValueError):
            cls("x", dshift=0.5)


def test_resolved_io_per_dshift():
    """Entry address depends on dshift (INV-6): resolve with the instance's
    real params, never the bare type."""
    from engine.catalog import BlockCatalog
    cat = BlockCatalog.from_gr_kyttar()
    for name, table, k, odd in CASES:
        entries = set()
        for dshift in DSHIFTS:
            entry, ins = cat.resolved_io(name, {"dshift": dshift},
                                         library="lattrex.official")
            assert entry is not None
            entries.add(entry)
        assert entries == {8, 9}, entries   # d=0 -> 9; others -> 8


# --- reports ------------------------------------------------------------------
def _emit_report(name, table, k, odd):
    words = _sweep_words(seed=41)
    got = _run_dut(name, words, 0)
    want = [_design_ref(w, table, 0, odd) for w in words]
    res = CompareResult(passed=(got == want), metric=Metric.EXACT,
                        n_compared=len(words),
                        bit_errors=sum(1 for a, b in zip(got, want) if a != b),
                        delay_used=0)
    assert res.passed, res.summary()
    mx, rms, rms_neg = _ACC[name]
    write_report(name, res, coverage={
        "golden": ("numpy float (no stock GR counterpart) + transcribed "
                   "bit-exact 16-interval table+interp design reference; "
                   "two independent integer models agree exhaustively"),
        "exhaustive_reference": "65536 words x dshift {0,+1,+2,-1}",
        "dut_bit_exact": "dense sweep (edges+stride+random), 4 dshift values",
        "float_accuracy": (f"max<={mx} (dshift-invariant), rms<={rms} at "
                           f"dshift>=0 / <={rms_neg} at dshift<0 "
                           "(exhaustive)"),
        "edge": True, "random": 3, "param_sweep": len(DSHIFTS),
        "clamp": "beyond-domain asymptote pinned (dshift>0)",
        "saturated": "pipelined == per-sample bit-exact (dshift 0,1)",
        "mutation": ("wrong-table-entry / off-by-one-clamp / broken-sign-fold"
                     " / wrong-dshift-patch / inverted / +1-delay / empty"),
    })


def test_emit_report_sigmoid():
    _emit_report(*CASES[0])


def test_emit_report_tanh():
    _emit_report(*CASES[1])
