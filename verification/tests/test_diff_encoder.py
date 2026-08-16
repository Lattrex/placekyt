# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify DiffEncoderBlock 1:1 against GNU Radio ``digital.diff_encoder_bb``.

The differential encoder implements the DBPSK/DQPSK precoder::

    DIFF_DIFFERENTIAL (GR default):  y[n] = (x[n] + y[n-1])     mod M
    DIFF_NRZI:                       y[n] = (x[n] + y[n-1] + 1)  mod M

cold-started at y[-1]=0. It is a 1-cell block with a 1-sample carry state (the
running ``y``). The recurrence and the exact byte output were CONFIRMED against
LIVE GNU Radio (not a datasheet) for modulus 2 and 4 and both coding types.

The gate compares the on-chip integer symbol stream to the GR ``diff_encoder_bb``
byte stream BIT-EXACT (metric EXACT, tolerance 0). The symbols are tiny integers
in [0, M-1] delivered as raw words, so we compare the integer lists directly (the
FSK4-slicer convention) — NOT the Q15-float path, which would rescale a byte 1
into 32767.

Mandatory mutation tests (INV-4) prove the gate FAILS on a corrupted DUT:
no-feedback (pass-through), subtract-instead-of-add, wrong initial state, and a
one-symbol shift. A round-trip through ``diff_decoder_bb`` proves the encoder is
the exact inverse of the decoder.

Run (GNU Radio lives in the system Python)::

    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
        .venv/bin/python -m pytest verification/tests/test_diff_encoder.py -q
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PLACEKYT = Path(__file__).resolve().parents[2] / "placekyt"
_VERIFY = Path(__file__).resolve().parents[1]
_RUNTIME = Path(__file__).resolve().parents[2] / "runtime" / "python"
for p in (str(_RUNTIME), str(_PLACEKYT), str(_VERIFY)):
    if p not in sys.path:
        sys.path.insert(0, p)

from kyttar_verify import run_block_dut, write_report, CompareResult, Metric  # noqa: E402

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_PY = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")
_GR_AVAILABLE = os.path.exists(_GR_PY)
pytestmark = pytest.mark.skipif(
    not _GR_AVAILABLE, reason="GNU Radio interpreter not available")


# --- golden reference: LIVE GNU Radio diff_encoder_bb / diff_decoder_bb --------

def _gr_diff(symbols, modulus, coding="DIFF_DIFFERENTIAL", decode=False):
    """Run GR ``diff_encoder_bb`` (or ``diff_decoder_bb``) in a subprocess and
    return the integer byte stream. The alphabet symbols are passed verbatim as
    bytes (0..M-1) — they index the modular recurrence, not a Q15 value."""
    which = "diff_decoder_bb" if decode else "diff_encoder_bb"
    script = f"""
from gnuradio import gr, blocks, digital
import json, sys
d = json.loads(sys.stdin.read())
syms = [int(v) & 0xFF for v in d["s"]]
coding = getattr(digital, d["coding"])
tb = gr.top_block()
src = blocks.vector_source_b(syms, False)
blk = digital.{which}(int(d["M"]), coding)
snk = blocks.vector_sink_b()
tb.connect(src, blk, snk); tb.run()
print(json.dumps([int(v) for v in snk.data()]))
"""
    r = subprocess.run(
        [_GR_PY, "-c", script],
        input=json.dumps({"s": list(symbols), "M": modulus, "coding": coding}),
        capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-800:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def _run(symbols, modulus, coding="DIFF_DIFFERENTIAL"):
    """Build + run the on-chip DUT; return (dut_words, gr_bytes)."""
    params = {"modulus": modulus}
    if coding != "DIFF_DIFFERENTIAL":
        params["coding"] = coding
    dut = run_block_dut("DiffEncoderBlock", list(symbols), params=params,
                        chip_yaml=CHIP_YAML, in_port="sample", out_port="out")
    assert dut.ok, dut.reason
    ref = _gr_diff(symbols, modulus, coding)
    return dut, ref


def _exact(dut_words, gr_bytes):
    """BIT-EXACT integer comparison (metric EXACT semantics, tol 0). The DUT
    words are raw uint16 symbol values; GR emits bytes — compare directly."""
    if not dut_words or any(w is None for w in dut_words):
        return CompareResult(False, Metric.EXACT, reason="missing DUT output")
    n = min(len(dut_words), len(gr_bytes))
    a = [int(w) & 0xFFFF for w in dut_words[:n]]
    b = [int(v) & 0xFFFF for v in gr_bytes[:n]]
    errs = sum(1 for x, y in zip(a, b) if x != y)
    res = CompareResult(errs == 0, Metric.EXACT, n_compared=n,
                        max_abs_err=max((abs(x - y) for x, y in zip(a, b)),
                                        default=0))
    if errs:
        res.reason = f"{errs}/{n} symbols differ (dut {a[:12]} vs gr {b[:12]})"
    return res


# --- correctness: BIT-EXACT vs GR, modulus 2 AND 4 -----------------------------

def test_modulus2_random_bitexact():
    """A pseudo-random bit stream, M=2, matches diff_encoder_bb bit-for-bit."""
    bits = [(i * 13 + 7) & 1 for i in range(64)]
    dut, ref = _run(bits, 2)
    res = _exact(dut.outputs_q15, ref)
    print("\nM2 random:", res.summary(), "| words", dut.n_words)
    assert res.passed, res.summary()


def test_modulus4_random_bitexact():
    """A pseudo-random 4-ary stream, M=4, matches diff_encoder_bb bit-for-bit."""
    syms = [(i * 7 + 2) % 4 for i in range(64)]
    dut, ref = _run(syms, 4)
    res = _exact(dut.outputs_q15, ref)
    print("\nM4 random:", res.summary())
    assert res.passed, res.summary()


def test_edge_all_zeros_all_ones_alternating():
    """Edge stimulus for M=2 and M=4: all-0, all-max, alternating."""
    for M in (2, 4):
        for syms in ([0] * 24, [M - 1] * 24, [0, M - 1] * 12):
            dut, ref = _run(syms, M)
            res = _exact(dut.outputs_q15, ref)
            print(f"\nM{M} edge {syms[:4]}...:", res.summary())
            assert res.passed, res.summary()


def test_random_multiple_seeds_m2_m4():
    """>=3 seeds, both moduli, all bit-exact vs GR — exercises the carry state
    over many transitions (INV-12: stimulus longer than the 1-sample state)."""
    import random
    for M in (2, 4):
        for seed in (1, 7, 42, 1234):
            rng = random.Random(seed)
            syms = [rng.randrange(M) for _ in range(80)]
            dut, ref = _run(syms, M)
            res = _exact(dut.outputs_q15, ref)
            assert res.passed, f"M={M} seed={seed}: {res.summary()}"


def test_nrzi_coding_matches_gr():
    """DIFF_NRZI (the +1 recurrence) is mirrored and bit-exact vs GR.

    GR ``diff_encoder_bb`` only supports NRZI at modulus 2 (it raises otherwise),
    so this is the ONLY valid NRZI configuration — verified against LIVE GR."""
    syms = [(i * 5 + 1) % 2 for i in range(48)]
    dut, ref = _run(syms, 2, coding="DIFF_NRZI")
    res = _exact(dut.outputs_q15, ref)
    print("\nM2 NRZI:", res.summary())
    assert res.passed, res.summary()


def test_nrzi_modulus4_raises():
    """NRZI at modulus != 2 MUST raise, mirroring GR's own restriction."""
    from gr_kyttar.placement.blocks.diff_encoder_block import DiffEncoderBlock
    with pytest.raises(ValueError, match="NRZI only supported with modulus 2"):
        DiffEncoderBlock("x", modulus=4, coding="DIFF_NRZI")


# --- round-trip: encoder is the EXACT inverse of diff_decoder_bb ---------------

def test_roundtrip_encode_then_gr_decode_identity():
    """encode(x) then GR diff_decoder_bb == x, for M=2 and M=4 (both codings).

    The differential encoder is the precoder; the decoder recovers the original
    stream. This pins the recurrence direction end to end."""
    import random
    # (modulus, coding) pairs GR actually supports: DIFFERENTIAL at any M; NRZI
    # only at M=2 (GR raises otherwise).
    configs = [(2, "DIFF_DIFFERENTIAL"), (4, "DIFF_DIFFERENTIAL"),
               (2, "DIFF_NRZI")]
    for M, coding in configs:
        rng = random.Random(99 + M + len(coding))
        syms = [rng.randrange(M) for _ in range(80)]
        dut, _ = _run(syms, M, coding=coding)
        enc = [int(w) & 0xFF for w in dut.outputs_q15]
        back = _gr_diff(enc, M, coding, decode=True)
        assert back[:len(syms)] == syms, (
            f"round-trip M={M} {coding} failed:\n in  {syms[:16]}\n out {back[:16]}")


# --- MANDATORY mutation gates (INV-4): the gate MUST fail on a corrupted DUT ---

def test_mutation_no_feedback_passthrough_fails():
    """If the block had NO feedback (y = x, pass-through), the gate MUST fail —
    proving the 1-sample carry is actually under test."""
    bits = [(i * 13 + 7) & 1 for i in range(48)]
    _, ref = _run(bits, 2)
    passthrough = list(bits)  # y[n] = x[n], no accumulation
    res = _exact(passthrough, ref)
    assert not res.passed, "gate failed to detect a pass-through (no-feedback) encoder!"


def test_mutation_subtract_instead_of_add_fails():
    """Recurrence y=(y_prev - x) mod M instead of (x + y_prev) mod M must FAIL."""
    syms = [(i * 7 + 2) % 4 for i in range(48)]
    _, ref = _run(syms, 4)
    y = 0
    wrong = []
    for x in syms:
        y = (y - x) % 4
        wrong.append(y)
    res = _exact(wrong, ref)
    assert not res.passed, "gate failed to detect subtract-instead-of-add!"


def test_mutation_wrong_initial_state_fails():
    """A non-zero cold-start (y[-1]=1) diverges from GR (y[-1]=0) and MUST fail."""
    bits = [(i * 13 + 7) & 1 for i in range(48)]
    _, ref = _run(bits, 2)
    y = 1  # wrong initial carry
    wrong = []
    for x in bits:
        y = (x + y) % 2
        wrong.append(y)
    res = _exact(wrong, ref)
    assert not res.passed, "gate failed to detect a wrong initial state!"


def test_mutation_shifted_stream_fails():
    """A one-symbol delay must be caught (EXACT, no realignment)."""
    syms = [(i * 5 + 3) % 4 for i in range(40)]
    dut, ref = _run(syms, 4)
    shifted = [0] + list(dut.outputs_q15[:-1])
    res = _exact(shifted, ref)
    assert not res.passed, "gate failed to detect a one-symbol stream shift!"


def test_empty_output_fails():
    _, ref = _run([0, 1, 1, 0], 2)
    res = _exact([], ref)
    assert not res.passed


# --- param / HW-limit guards ---------------------------------------------------

def test_bad_modulus_raises():
    from gr_kyttar.placement.blocks.diff_encoder_block import DiffEncoderBlock
    with pytest.raises(ValueError):
        DiffEncoderBlock("x", modulus=1)
    with pytest.raises(ValueError, match="HARDWARE LIMIT"):
        DiffEncoderBlock("x", modulus=0x4001)


def test_bad_coding_raises():
    from gr_kyttar.placement.blocks.diff_encoder_block import DiffEncoderBlock
    with pytest.raises(ValueError):
        DiffEncoderBlock("x", coding="DIFF_BOGUS")


# --- report --------------------------------------------------------------------

def test_emit_report():
    bits = [(i * 13 + 7) & 1 for i in range(64)]
    dut, ref = _run(bits, 2)
    res = _exact(dut.outputs_q15, ref)
    assert res.passed
    write_report("DiffEncoderBlock", res, coverage={
        "modulus": "2 and 4",
        "coding": "DIFF_DIFFERENTIAL (default) + DIFF_NRZI",
        "patterns": "random x4 seeds, all-0, all-max, alternating",
        "mutation": True,
        "roundtrip": "encode -> diff_decoder_bb == identity (M2/M4, both codings)",
        "gr_equiv": "digital.diff_encoder_bb(modulus, coding)",
        "note": "1 cell, 1-sample carry state; y[n]=(x[n]+y[n-1]+bias) mod M, bit-exact",
    })
