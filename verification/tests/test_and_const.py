# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify AndConstBlock against GNU Radio blocks.and_const_bb — BIT-EXACT.

``out[n] = in[n] & constant`` on an unsigned-byte stream (masking): ``&1`` takes
the LSB, ``&0x0F`` the low nibble, ``&0xFF`` is identity, ``&0`` zeros. On chip:
one ``LOGIC.AND`` of the input register against a baked immediate — the same
single-cell-op-with-a-baked-constant shape as GainBlock, but bitwise (no Q15
rounding path), so the DUT matches GR ``and_const_bb`` bit-for-bit.

METRIC CHOICE (deliberate, see lessons_log 2026-08-06): the gate uses
``Metric.EXACT`` (full-word ``array_equal``), NOT ``Metric.DECISION``. DECISION
compares only the LOW BIT (``a & 1``) — for a full byte it CANNOT be bit-exact:
an upper-bit error (e.g. 128 -> 0, differing only in bit 7) passes DECISION but is
a real bug. A ``wrong-constant`` mutation typically corrupts upper bits, so per
INV-4 (the gate MUST catch mutations) only EXACT is a valid bit-exact gate here.
Both are tol 0. The byte reference is delivered to the comparator as ``byte /
32768.0`` floats so the harness's Q15 round-trip reproduces the exact byte word.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_and_const.py -x -q
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
    run_block_dut, compare_against_grc, write_report, Metric)
from kyttar_verify.gnuradio_ref import SYSTEM_PYTHON  # noqa: E402
from kyttar_verify.orientation import (  # noqa: E402
    check_orientation_invariance, D4_ORIENTATIONS)
from gr_kyttar.placement.blocks.and_const_block import AndConstBlock  # noqa: E402

import json  # noqa: E402
import subprocess  # noqa: E402

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_AVAILABLE),
    reason="chip yaml or GNU Radio interpreter absent")


# --- LIVE GNU Radio reference: blocks.and_const_bb (unsigned-byte I/O) ---------
def _gr_and_const(bytes_in, constant):
    """Run the golden GR ``and_const_bb`` in the system-Python subprocess and
    return its output BYTES (0..255). Byte I/O — not the Q15 float path — so the
    reference is the true bitwise AND, bit-for-bit."""
    script = f"""
import json
from gnuradio import gr, blocks
data = {list(int(b) & 0xFF for b in bytes_in)!r}
tb = gr.top_block()
src = blocks.vector_source_b(data, False)
op = blocks.and_const_bb({int(constant)})
snk = blocks.vector_sink_b()
tb.connect(src, op); tb.connect(op, snk); tb.run()
print(json.dumps([int(x) & 0xFF for x in snk.data()]))
"""
    proc = subprocess.run([SYSTEM_PYTHON, "-c", script],
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"GR and_const_bb subprocess failed:\n{proc.stderr}")
    return json.loads(proc.stdout.strip())


def _ref_floats(gr_bytes):
    """Feed GR byte output to the comparator as ``byte/32768.0`` floats so the
    harness's ``_saturate_ref_q15`` round-trips each byte (0..255 < 0x8000) back
    to the exact integer word — an EXACT full-word compare against the DUT bytes."""
    return [b / 32768.0 for b in gr_bytes]


def _random_bytes(seed, n=48):
    rng = random.Random(seed)
    return [rng.randint(0, 255) for _ in range(n)]


# Edge byte vectors: 0, identity byte, alternating masks, powers of two, extremes.
EDGE_BYTES = [0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80,
              0xFF, 0x0F, 0xF0, 0x55, 0xAA, 0x7F, 0x80, 0x81, 0xFE]


def _run_dut(stim, constant, orient=None):
    return run_block_dut(
        "AndConstBlock", stim, chip_yaml=CHIP_YAML,
        params={"constant": constant}, in_port="sample", out_port="out",
        orient=orient)


def _run_and_compare(stim, constant, *, metric=Metric.EXACT, orient=None):
    dut = _run_dut(stim, constant, orient=orient)
    assert dut.ok, dut.reason
    gr = _gr_and_const(stim, constant)
    res = compare_against_grc(dut.outputs_q15, _ref_floats(gr),
                              metric=metric, delay=0)
    return dut, res, gr


# =============================================================================
# BIT-EXACT equivalence vs LIVE GNU Radio and_const_bb (metric EXACT, tol 0)
# =============================================================================

def test_edge_constant_zero_all_zeros():
    """constant=0 -> every output byte is 0."""
    dut, res, gr = _run_and_compare(EDGE_BYTES, 0)
    print("\nconstant=0:", res.summary())
    assert res.passed, res.summary()
    assert all(b == 0 for b in gr), gr
    assert all((d or 0) == 0 for d in dut.outputs_q15), dut.outputs_q15


def test_edge_constant_ff_identity():
    """constant=0xFF -> identity (output == input) over the whole byte range."""
    dut, res, gr = _run_and_compare(EDGE_BYTES, 0xFF)
    print("\nconstant=0xFF:", res.summary())
    assert res.passed, res.summary()
    assert gr == [b & 0xFF for b in EDGE_BYTES], gr


def test_edge_constant_one_lsb_mask():
    """constant=1 -> LSB mask (output is bit0 of each input byte)."""
    stim = _random_bytes(11) + EDGE_BYTES
    dut, res, gr = _run_and_compare(stim, 1)
    print("\nconstant=1 (LSB):", res.summary())
    assert res.passed, res.summary()
    assert gr == [b & 1 for b in stim], gr


def test_edge_constant_0x0f_low_nibble():
    """constant=0x0F -> low-nibble mask."""
    stim = _random_bytes(21) + EDGE_BYTES
    dut, res, gr = _run_and_compare(stim, 0x0F)
    print("\nconstant=0x0F (low nibble):", res.summary())
    assert res.passed, res.summary()
    assert gr == [b & 0x0F for b in stim], gr


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_random_vectors(seed):
    """Random byte stream, a non-trivial mask (0x3C), >=3 seeds, bit-exact."""
    dut, res, gr = _run_and_compare(_random_bytes(seed), 0x3C)
    print(f"\nrandom seed={seed} const=0x3C:", res.summary())
    assert res.passed, res.summary()


@pytest.mark.parametrize("constant", [0x00, 0x01, 0x03, 0x07, 0x0F, 0x1F,
                                      0x3F, 0x55, 0xAA, 0xF0, 0xFF])
def test_constant_sweep(constant):
    """Sweep the mask across representative values; bit-exact vs GR each time."""
    stim = _random_bytes(100 + constant)
    dut, res, gr = _run_and_compare(stim, constant)
    print(f"\nconstant={constant:#04x}:", res.summary())
    assert res.passed, res.summary()
    assert gr == [b & constant for b in stim], (constant, gr)


# =============================================================================
# 'constant' mirrored VERBATIM. NOTE (verified 2026-08-06): this GNU Radio build
# rejects a constant outside 0..255 (both -1 and 256 raise) — the ``and_const_bb``
# constant is an UNSIGNED byte, not a signed char. So "verbatim" means the exact
# 0..255 value the user typed in GRC; the block applies it as ``constant & 0xFF``.
# =============================================================================

def test_constant_ff_mirrored_verbatim_identity():
    """constant=0xFF (255) is delivered verbatim and acts as the identity mask —
    output == input over the whole byte range, bit-for-bit vs GR."""
    stim = _random_bytes(5) + EDGE_BYTES
    dut = _run_dut(stim, 0xFF)
    assert dut.ok, dut.reason
    gr = _gr_and_const(stim, 0xFF)
    res = compare_against_grc(dut.outputs_q15, _ref_floats(gr),
                              metric=Metric.EXACT, delay=0)
    print("\nconstant=0xFF (verbatim identity):", res.summary())
    assert res.passed, res.summary()
    assert gr == [b & 0xFF for b in stim], gr
    assert AndConstBlock("r", constant=0xFF).constant == 0xFF, "verbatim mirror"


def test_gr_rejects_out_of_byte_range_constant():
    """Ground-truth guard: this GR ``and_const_bb`` accepts only 0..255 (an
    unsigned byte). -1 and 256 both raise — proving the constant is NOT a signed
    char in this build, so the block's ``& 0xFF`` mirror is the faithful mapping."""
    for bad in (-1, 256):
        with pytest.raises(RuntimeError):
            _gr_and_const([0, 1, 2], bad)
    # a full byte range is accepted
    assert _gr_and_const([200, 100], 255) == [200, 100]


# =============================================================================
# ORIENTATION INVARIANCE (INV-23): identical output in all 8 D4 orientations
# =============================================================================

def test_orientation_invariant():
    stim = _random_bytes(31, 32)
    constant = 0x0F
    gr = _gr_and_const(stim, constant)

    def run(orient):
        # return the full DUTResult; the default comparator matches .outputs_q15
        # exactly and treats a build/route failure in any orientation as a FAIL.
        return _run_dut(stim, constant, orient=orient)

    ok, report = check_orientation_invariance(run)
    for row in report:
        print(f"  orient {row['orient']:<28} ok={row['ok']} {row['detail']}")
    assert ok, f"orientation variance: {[r for r in report if not r['ok']]}"
    # and the identity orientation is itself bit-exact vs GR
    ident = _run_dut(stim, constant, orient=list(D4_ORIENTATIONS[0]))
    assert ident.outputs_q15 == [b & constant for b in stim] == gr


# =============================================================================
# MANDATORY mutation tests (INV-4) — the gate MUST FAIL on each
# =============================================================================

def test_mutation_or_instead_of_and_fails():
    """A DUT computing OR instead of AND must FAIL the gate — proves the block
    masks (AND) rather than sets (OR)."""
    stim = _random_bytes(7)
    constant = 0x0F
    gr = _gr_and_const(stim, constant)
    mutated = [(s | constant) & 0xFF for s in stim]   # OR, not AND
    res = compare_against_grc(mutated, _ref_floats(gr),
                              metric=Metric.EXACT, delay=0)
    assert not res.passed, "gate failed to detect OR-instead-of-AND!"


def test_mutation_wrong_constant_fails():
    """A DUT masked with the WRONG constant must FAIL (upper-bit-sensitive)."""
    stim = _random_bytes(9)
    gr = _gr_and_const(stim, 0x0F)
    wrong = [(s & 0x1F) & 0xFF for s in stim]   # 0x1F instead of 0x0F
    res = compare_against_grc(wrong, _ref_floats(gr),
                              metric=Metric.EXACT, delay=0)
    assert not res.passed, "gate failed to detect a wrong-constant mask!"


def test_mutation_identity_fails():
    """A DUT that passes the input through unmasked (identity) must FAIL for a
    real mask (constant != 0xFF)."""
    stim = _random_bytes(13)
    gr = _gr_and_const(stim, 0x0F)
    identity = [s & 0xFF for s in stim]   # no mask applied
    res = compare_against_grc(identity, _ref_floats(gr),
                              metric=Metric.EXACT, delay=0)
    assert not res.passed, "gate failed to detect an unmasked (identity) output!"


def test_mutation_one_sample_offset_fails():
    """A +1-sample latency error must FAIL the gate."""
    stim = _random_bytes(17)
    constant = 0x3C
    dut = _run_dut(stim, constant)
    assert dut.ok, dut.reason
    gr = _gr_and_const(stim, constant)
    shifted = [0x00] + list(dut.outputs_q15[:-1])
    res = compare_against_grc(shifted, _ref_floats(gr),
                              metric=Metric.EXACT, delay=0)
    assert not res.passed, "gate failed to detect a 1-sample latency error!"


def test_mutation_upper_bit_error_caught_by_exact():
    """The METRIC-CHOICE guard: an upper-bit-only error (bit 7) is a real bug the
    LSB-only DECISION metric MISSES but EXACT catches. This is why the gate is
    EXACT, not DECISION, for a full-byte AND."""
    stim = [0x80, 0xC0, 0xFF, 0x81]          # all have bit7 set
    constant = 0xFF                           # identity mask
    gr = _gr_and_const(stim, constant)        # == stim
    mutated = [s & 0x7F for s in stim]        # clear bit7 only
    res_exact = compare_against_grc(mutated, _ref_floats(gr),
                                    metric=Metric.EXACT, delay=0)
    res_dec = compare_against_grc(mutated, _ref_floats(gr),
                                  metric=Metric.DECISION, delay=0)
    assert not res_exact.passed, "EXACT must catch an upper-bit error!"
    assert res_dec.passed, ("DECISION(LSB) is expected to MISS an upper-bit "
                            "error — which is exactly why the gate uses EXACT")


def test_empty_output_fails():
    gr = _gr_and_const(EDGE_BYTES, 0x0F)
    res = compare_against_grc([], _ref_floats(gr), metric=Metric.EXACT, delay=0)
    assert not res.passed


# =============================================================================
# Folded / single-cell / legal build sanity
# =============================================================================

def test_single_cell_fold():
    blk = AndConstBlock("ref", constant=0x0F)
    assert blk.cell_count == 1, "AND-with-immediate is a single-cell op"
    assert blk.constant == 0x0F
    dut = _run_dut(EDGE_BYTES, 0x0F)
    assert dut.ok and dut.n_words > 0, dut.reason


# =============================================================================
# dashboard report
# =============================================================================

def test_emit_report():
    dut, res, gr = _run_and_compare(EDGE_BYTES, 0x0F)
    assert res.passed, res.summary()
    write_report("AndConstBlock", res, coverage={
        "edge": True, "random": 3, "constant_sweep": 11,
        "orientation": True, "mutation": True, "bit_exact": True})
