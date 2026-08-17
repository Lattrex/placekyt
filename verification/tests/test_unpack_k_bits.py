# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify UnpackKBitsBlock 1:1 against GNU Radio ``blocks.unpack_k_bits_bb``.

``blocks.unpack_k_bits_bb(k)`` takes one input byte and emits the LOW ``k`` bits of
that byte MOST-SIGNIFICANT bit FIRST, as ``k`` separate output bytes (0/1)::

    out = [(byte >> (k-1)) & 1, (byte >> (k-2)) & 1, ..., (byte >> 0) & 1]

It is the exact INVERSE of ``pack_k_bits_bb``. This is pure bit manipulation (no
Q15 arithmetic), so the comparison is BIT-EXACT — metric DECISION, tolerance 0.

This is a RATE-EXPANDING block (1 in -> k out), so it uses ``run_block_dut_rate``,
which drains the whole per-trigger burst.

Coverage: edge bytes (0, 0xFF, 0x80, 0x01) at k=2/4/8; random (>=3 seeds); a full
k sweep 2..8; plus the mandatory INV-4 mutation gates (LSB-first instead of
MSB-first, wrong k, an extra/missing bit, empty output) that MUST FAIL against the
live GR reference.

Run::

    cd verification
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
        .venv/bin/python -m pytest tests/test_unpack_k_bits.py -v
"""
from __future__ import annotations

import json
import os
import random
import subprocess
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
    run_block_dut_rate, write_report, CompareResult, Metric)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_PY = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")
_GR_AVAILABLE = os.path.exists(CHIP_YAML) and os.path.exists(_GR_PY)
pytestmark = pytest.mark.skipif(
    not _GR_AVAILABLE, reason="chip yaml or GNU Radio interpreter absent")


# --- golden reference: LIVE GNU Radio unpack_k_bits_bb -------------------------

def _gr_unpack(bytes_in, k) -> list[int]:
    """GR golden: ``blocks.unpack_k_bits_bb(k)`` over the input byte stream."""
    script = (
        "import json,sys\n"
        "from gnuradio import gr, blocks\n"
        "d = json.loads(sys.stdin.read())\n"
        "k = d['k']\n"
        "tb = gr.top_block()\n"
        "src = blocks.vector_source_b([int(x) & 0xFF for x in d['bytes']], False)\n"
        "u = blocks.unpack_k_bits_bb(k)\n"
        "snk = blocks.vector_sink_b()\n"
        "tb.connect(src, u, snk)\n"
        "tb.run()\n"
        "print(json.dumps([int(x) for x in snk.data()]))\n")
    r = subprocess.run([_GR_PY, "-c", script],
                       input=json.dumps({"bytes": list(bytes_in), "k": int(k)}),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-800:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def _run(bytes_in, k):
    """DUT unpacked bit stream (flat) + the GR reference."""
    inq = [int(b) & 0xFFFF for b in bytes_in]
    dut = run_block_dut_rate("UnpackKBitsBlock", inq, params={"k": k},
                             chip_yaml=CHIP_YAML, in_port="byte", out_port="out")
    assert dut.ok, dut.reason
    gr = _gr_unpack(bytes_in, k)
    return dut, gr


def _bit_errors(dut_words, gr_bits):
    n = min(len(dut_words), len(gr_bits))
    assert n > 0, "no bits compared"
    errs = sum(1 for i in range(n)
               if dut_words[i] is None or (int(dut_words[i]) & 0xFFFF) != gr_bits[i])
    return errs, n


# --- stimulus families --------------------------------------------------------
# Edge bytes: all-zero, all-ones, MSB-only (0x80), LSB-only (0x01), alternating.
EDGE = [0x00, 0xFF, 0x80, 0x01, 0xAA, 0x55, 0x7F, 0xC3]


def _random_bytes(seed, n=24):
    rng = random.Random(seed)
    return [rng.randint(0, 0xFF) for _ in range(n)]


# --- correctness: bit-exact vs GR ---------------------------------------------

@pytest.mark.parametrize("k", [2, 4, 8])
def test_edge_bit_exact_vs_gr(k):
    """Edge bytes (0, 0xFF, 0x80, 0x01, ...) unpack bit-for-bit like GR
    unpack_k_bits_bb — MSB-first, low k bits."""
    dut, gr = _run(EDGE, k)
    # rate check: every trigger produced exactly k bits.
    assert all(len(t) == k for t in dut.per_trigger), \
        [len(t) for t in dut.per_trigger]
    assert len(dut.outputs_q15) == k * len(EDGE)
    errs, n = _bit_errors(dut.outputs_q15, gr)
    print(f"\nedge k={k}: {errs} bit errors / {n}")
    assert errs == 0, f"k={k}: {errs}/{n} bit errors vs unpack_k_bits_bb"


def test_specific_vectors_k2():
    """Nail the exact GR mapping: low k bits, MSB-first (not the upper bits)."""
    dut, gr = _run([0xAA, 0x80, 0x03, 0x01], 2)
    # 0xAA low2=10, 0x80 low2=00, 0x03 low2=11, 0x01 low2=01
    assert gr == [1, 0, 0, 0, 1, 1, 0, 1], f"GR reference unexpected: {gr}"
    got = [int(w) & 0xFFFF for w in dut.outputs_q15]
    assert got == [1, 0, 0, 0, 1, 1, 0, 1], f"DUT wrong: {got}"


@pytest.mark.parametrize("seed", [1, 7, 42, 1234])
def test_random_bit_exact_vs_gr(seed):
    bytes_in = _random_bytes(seed)
    for k in (2, 5, 8):
        dut, gr = _run(bytes_in, k)
        errs, n = _bit_errors(dut.outputs_q15, gr)
        print(f"\nrandom seed={seed} k={k}: {errs} bit errors / {n}")
        assert errs == 0, f"seed {seed} k={k}: {errs}/{n} bit errors vs GR"


@pytest.mark.parametrize("k", [2, 3, 4, 5, 6, 7, 8])
def test_k_sweep_bit_exact_vs_gr(k):
    """Full k sweep 2..8: bit-exact against live GR for each k."""
    bytes_in = _random_bytes(99, n=16)
    dut, gr = _run(bytes_in, k)
    errs, n = _bit_errors(dut.outputs_q15, gr)
    print(f"\nk-sweep k={k}: {errs} bit errors / {n}")
    assert errs == 0, f"k={k}: {errs}/{n} bit errors vs GR"


# --- reference sanity (pure python == GR) -------------------------------------

def test_reference_matches_gr_over_range():
    """process_reference_q15 == GR unpack_k_bits_bb over a full byte sweep."""
    from gr_kyttar.placement.blocks.unpack_k_bits_block import UnpackKBitsBlock
    bytes_in = list(range(256))
    for k in (2, 4, 8):
        ref = UnpackKBitsBlock("r", k=k).process_reference_q15(bytes_in)
        gr = _gr_unpack(bytes_in, k)
        assert ref == gr, f"reference disagrees with GR at k={k}"


# --- MANDATORY mutation gates (INV-4) -----------------------------------------

def test_mutation_lsb_first_fails():
    """A DUT that emitted the bits LSB-first (reversed order) must DISAGREE with
    GR's MSB-first stream — proving the gate rejects a wrong bit order."""
    dut, gr = _run(EDGE, 8)
    got = [int(w) & 0xFFFF for w in dut.outputs_q15]
    # reverse each group of 8 -> LSB-first corruption
    corrupt = []
    for i in range(0, len(got), 8):
        corrupt.extend(reversed(got[i:i + 8]))
    errs, n = _bit_errors(corrupt, gr)
    assert errs > 0, "an LSB-first (reversed) unpack went undetected by the gate!"


def test_mutation_wrong_k_fails():
    """A k=4 DUT stream must FAIL against a k=8 GR golden (length + content)."""
    bytes_in = _random_bytes(7, n=12)
    dut4, _ = _run(bytes_in, 4)
    gr8 = _gr_unpack(bytes_in, 8)
    errs, n = _bit_errors(dut4.outputs_q15, gr8)
    assert errs > 0, "a wrong-k unpack went undetected by the gate!"


def test_mutation_one_bit_shift_fails():
    """A +1-bit shift of the unpacked stream must FAIL (no free lag alignment)."""
    dut, gr = _run(_random_bytes(42), 8)
    shifted = [0] + [int(w) & 0xFFFF for w in dut.outputs_q15[:-1]]
    errs, n = _bit_errors(shifted, gr)
    assert errs > 0, "a one-bit shift went undetected!"


def test_mutation_missing_bit_fails():
    """Dropping one bit (shorter stream) must FAIL — the emitted count matters."""
    dut, gr = _run(EDGE, 8)
    got = [int(w) & 0xFFFF for w in dut.outputs_q15]
    dropped = got[:-1]  # one bit missing at the end
    # length mismatch: compare over GR's full length, missing tail is an error.
    n = len(gr)
    errs = sum(1 for i in range(n)
               if i >= len(dropped) or dropped[i] != gr[i])
    assert errs > 0, "a missing bit went undetected!"


def test_empty_output_fails():
    """An empty DUT output cannot be certified against a non-empty reference."""
    gr = _gr_unpack(EDGE, 8)
    assert len(gr) > 0
    errs, n = _bit_errors([None] * len(gr), gr)
    assert errs == n and errs > 0, "empty output must not certify"


# --- report -------------------------------------------------------------------

def test_emit_report():
    dut, gr = _run(EDGE, 8)
    errs, n = _bit_errors(dut.outputs_q15, gr)
    res = CompareResult(passed=(errs == 0), metric=Metric.DECISION,
                        n_compared=n, bit_errors=errs, delay_used=0)
    assert res.passed, res.summary()
    write_report("UnpackKBitsBlock", res, coverage={
        "gr_equiv": "blocks.unpack_k_bits_bb",
        "edge": True, "random": 4, "k_sweep": [2, 3, 4, 5, 6, 7, 8],
        "mutation": True,
        "decision": "low k bits of the input byte, MSB-first (GR-exact)",
        "note": "1-cell memoryless rate-expander (1 byte -> k bits); "
                "bit-exact vs unpack_k_bits_bb, delay 0",
    })
