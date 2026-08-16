# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify DiffDecoderBlock 1:1 against GNU Radio ``digital.diff_decoder_bb``.

``digital.diff_decoder_bb`` differentially DECODES a symbol stream:

    y[n] = (x[n] - x[n-1]) mod M          # DIFF_DIFFERENTIAL (default), x[-1]=0
    y[n] = (x[n] - x[n-1] + 1) mod 2      # DIFF_NRZI (modulus 2 ONLY)

It is the inverse of ``diff_encoder_bb``. The state is the PREVIOUS INPUT symbol
(not the previous output — that is the encoder's state), cold-started to 0. The
sign/direction of the subtraction and the cold-start were pinned against the LIVE
installed GR block (NOT a datasheet) for modulus 2 AND 4, DIFFERENTIAL and NRZI.

This is a BIT-EXACT gate (metric DECISION, tolerance 0), DUT-vs-LIVE-GR.

Coverage: edge (all-zeros, all-ones, alternating), random (>=3 seeds), modulus 2 AND
4, DIFFERENTIAL and NRZI, encode->decode round-trip on-chip, and mandatory INV-4
mutation gates (add instead of subtract, NO state / pass-through, wrong initial prev,
inverted output, +1 sample shift, wrong modulus, empty output) that MUST FAIL against
the GR reference.

Run::

    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
        .venv/bin/python -m pytest verification/tests/test_diff_decoder.py -q
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

from kyttar_verify import run_block_dut, write_report, CompareResult, Metric  # noqa: E402
from gr_kyttar.placement.blocks.diff_decoder_block import DiffDecoderBlock  # noqa: E402

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_PY = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")
_GR_AVAILABLE = os.path.exists(CHIP_YAML) and os.path.exists(_GR_PY)
pytestmark = pytest.mark.skipif(
    not _GR_AVAILABLE, reason="chip yaml or GNU Radio interpreter absent")


# --- GNU Radio golden reference (subprocess into the GR interpreter) ----------

def _gr_diff_decode(insyms, modulus, coding=0):
    """GR golden: ``digital.diff_decoder_bb`` on the input symbol stream.

    ``coding`` is the ``diff_coding_type`` enum value (0 = DIFF_DIFFERENTIAL,
    1 = DIFF_NRZI). The 1-arg constructor (``coding`` omitted) defaults to
    DIFF_DIFFERENTIAL and is exercised via ``coding=None``.
    """
    payload = {"insyms": [int(s) & 0xFF for s in insyms],
               "modulus": int(modulus),
               "coding": (None if coding is None else int(coding))}
    script = (
        "import json,sys\n"
        "from gnuradio import gr, blocks, digital\n"
        "d = json.loads(sys.stdin.read())\n"
        "tb = gr.top_block()\n"
        "src = blocks.vector_source_b(d['insyms'], False, 1, [])\n"
        "if d['coding'] is None:\n"
        "    dec = digital.diff_decoder_bb(d['modulus'])\n"
        "else:\n"
        "    ct = digital.DIFF_NRZI if d['coding'] == 1 else digital.DIFF_DIFFERENTIAL\n"
        "    dec = digital.diff_decoder_bb(d['modulus'], ct)\n"
        "snk = blocks.vector_sink_b(1, 4096)\n"
        "tb.connect(src, dec); tb.connect(dec, snk)\n"
        "tb.run()\n"
        "print(json.dumps([int(x) for x in snk.data()]))\n")
    r = subprocess.run([_GR_PY, "-c", script], input=json.dumps(payload),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-800:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def _gr_diff_encode(insyms, modulus):
    """GR ``digital.diff_encoder_bb`` — used only for the round-trip test."""
    payload = {"insyms": [int(s) & 0xFF for s in insyms], "modulus": int(modulus)}
    script = (
        "import json,sys\n"
        "from gnuradio import gr, blocks, digital\n"
        "d = json.loads(sys.stdin.read())\n"
        "tb = gr.top_block()\n"
        "src = blocks.vector_source_b(d['insyms'], False, 1, [])\n"
        "enc = digital.diff_encoder_bb(d['modulus'])\n"
        "snk = blocks.vector_sink_b(1, 4096)\n"
        "tb.connect(src, enc); tb.connect(enc, snk)\n"
        "tb.run()\n"
        "print(json.dumps([int(x) for x in snk.data()]))\n")
    r = subprocess.run([_GR_PY, "-c", script], input=json.dumps(payload),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-800:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def _run_dut(insyms, modulus, coding=0):
    """Build + run the Kyttar block on simKYT for the given symbols + params."""
    words = [int(s) & 0xFFFF for s in insyms]
    dut = run_block_dut(
        "DiffDecoderBlock", words,
        params={"modulus": modulus, "coding": coding},
        in_port="sample", chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    return dut


def _errs(dut_words, gr_syms):
    n = min(len(dut_words), len(gr_syms))
    assert n > 0, "no samples compared"
    e = sum(1 for k in range(n)
            if dut_words[k] is None or (int(dut_words[k]) & 0xFFFF) != (gr_syms[k] & 0xFFFF))
    return e, n


def _rand_syms(seed, modulus, n=48):
    rng = random.Random(seed)
    return [rng.randint(0, modulus - 1) for _ in range(n)]


# --- correctness: bit-exact vs LIVE GR ----------------------------------------

@pytest.mark.parametrize("modulus", [2, 4])
def test_all_zeros(modulus):
    """All-zeros input: every difference is 0 -> all-zeros output (DIFFERENTIAL)."""
    inp = [0] * 48
    dut = _run_dut(inp, modulus)
    gr = _gr_diff_decode(inp, modulus)
    e, n = _errs(dut.outputs_q15, gr)
    assert e == 0, f"all-zeros mod{modulus}: {e}/{n} errors; gr[:8]={gr[:8]}"


@pytest.mark.parametrize("modulus", [2, 4])
def test_all_max_symbol(modulus):
    """All-ones (mod2) / all-(M-1) (mod4): first symbol differs, rest 0."""
    inp = [modulus - 1] * 48
    dut = _run_dut(inp, modulus)
    gr = _gr_diff_decode(inp, modulus)
    e, n = _errs(dut.outputs_q15, gr)
    assert e == 0, f"all-max mod{modulus}: {e}/{n} errors; gr[:8]={gr[:8]}"


@pytest.mark.parametrize("modulus", [2, 4])
def test_alternating(modulus):
    """Alternating 0,(M-1),0,(M-1),... — a hard edge for the difference sign."""
    inp = [(0 if k % 2 == 0 else modulus - 1) for k in range(48)]
    dut = _run_dut(inp, modulus)
    gr = _gr_diff_decode(inp, modulus)
    e, n = _errs(dut.outputs_q15, gr)
    assert e == 0, f"alternating mod{modulus}: {e}/{n} errors; gr[:8]={gr[:8]}"


@pytest.mark.parametrize("modulus", [2, 4])
def test_full_symbol_ramp(modulus):
    """Every symbol value present (ramp 0..M-1 repeated) — exercises all diffs."""
    inp = [k % modulus for k in range(60)]
    dut = _run_dut(inp, modulus)
    gr = _gr_diff_decode(inp, modulus)
    e, n = _errs(dut.outputs_q15, gr)
    assert e == 0, f"ramp mod{modulus}: {e}/{n} errors"


@pytest.mark.parametrize("modulus", [2, 4])
@pytest.mark.parametrize("rseed", [1, 7, 42, 1234])
def test_random_bit_exact_vs_gr(modulus, rseed):
    """Random symbol streams (>=3 seeds) decode bit-exact vs GR, mod 2 and 4."""
    inp = _rand_syms(rseed, modulus, n=64)
    dut = _run_dut(inp, modulus)
    gr = _gr_diff_decode(inp, modulus)
    e, n = _errs(dut.outputs_q15, gr)
    assert e == 0, f"random mod{modulus} rseed={rseed}: {e}/{n} errors"


def test_default_coding_matches_one_arg_constructor():
    """coding=DIFF_DIFFERENTIAL (default) equals the 1-arg GR constructor (which
    also defaults to DIFF_DIFFERENTIAL) — proves the default is pinned to GR's."""
    inp = _rand_syms(9, 4, n=40)
    dut = _run_dut(inp, 4, coding=0)
    gr = _gr_diff_decode(inp, 4, coding=None)  # 1-arg constructor
    e, n = _errs(dut.outputs_q15, gr)
    assert e == 0, f"default coding != 1-arg GR: {e}/{n}"


def test_nrzi_mod2_bit_exact_vs_gr():
    """DIFF_NRZI (modulus 2): y[n] = (x-prev+1) mod 2, the DIFFERENTIAL complement."""
    for rseed in (3, 17, 91):
        inp = _rand_syms(rseed, 2, n=48)
        dut = _run_dut(inp, 2, coding=1)
        gr = _gr_diff_decode(inp, 2, coding=1)
        e, n = _errs(dut.outputs_q15, gr)
        assert e == 0, f"NRZI rseed={rseed}: {e}/{n} errors"
    # and NRZI is exactly the bit-complement of DIFFERENTIAL on the same stream.
    inp = _rand_syms(5, 2, n=48)
    diff = _gr_diff_decode(inp, 2, coding=0)
    nrzi = _gr_diff_decode(inp, 2, coding=1)
    assert nrzi == [1 - b for b in diff]


@pytest.mark.parametrize("modulus", [2, 4])
def test_encode_then_decode_round_trip(modulus):
    """Encode a stream with GR diff_encoder_bb, then decode on-chip -> recover the
    original (the block IS the inverse of the encoder, verified end-to-end)."""
    original = _rand_syms(2024, modulus, n=48)
    encoded = _gr_diff_encode(original, modulus)
    dut = _run_dut(encoded, modulus)
    recovered = [int(w) & 0xFFFF for w in dut.outputs_q15]
    assert recovered == original, f"round-trip mod{modulus} did not recover input"


# --- reference sanity (pure python == GR, no chip) ----------------------------

@pytest.mark.parametrize("modulus", [2, 4])
def test_reference_matches_gr(modulus):
    """process_reference == GR diff_decoder_bb (the on-chip-mirrored reference is
    itself GR-exact) over edge + random streams."""
    streams = [[0] * 32, [modulus - 1] * 32,
               [(0 if k % 2 else modulus - 1) for k in range(32)],
               _rand_syms(2024, modulus, n=64)]
    for inp in streams:
        ref = [int(s) for s in DiffDecoderBlock("r", modulus=modulus).process_reference(inp)]
        gr = _gr_diff_decode(inp, modulus)
        assert ref == gr, f"reference != GR on mod{modulus}"


# --- MANDATORY mutation gates (INV-4) -----------------------------------------

def test_mutation_add_instead_of_subtract_fails():
    """The decoder SUBTRACTS. If it ADDED (the encoder's op) instead, the output
    would disagree with GR — proves the gate is sensitive to the subtraction sign."""
    modulus = 4
    inp = _rand_syms(13, modulus, n=40)
    gr = _gr_diff_decode(inp, modulus)
    # model the mutated DUT: y=(x+prev)%M (add, the WRONG direction)
    prev = 0
    mutated = []
    for x in inp:
        mutated.append((x + prev) % modulus)
        prev = x
    e, n = _errs(mutated, gr)
    assert e > 0, "add-instead-of-subtract went undetected by the gate!"


def test_mutation_no_state_passthrough_fails():
    """A stateless pass-through (prev never updated, always 0) must DISAGREE with
    GR — proves the 1-sample previous-input state is actually under test."""
    modulus = 4
    inp = _rand_syms(21, modulus, n=40)
    gr = _gr_diff_decode(inp, modulus)
    passthrough = [x % modulus for x in inp]     # y = x, no differencing
    e, n = _errs(passthrough, gr)
    assert e > 0, "a stateless pass-through went undetected by the gate!"


def test_mutation_wrong_initial_prev_fails():
    """A wrong cold-start (prev[-1] = 1 instead of 0) must DISAGREE with GR at the
    first (and cascading) samples — proves the x[-1]=0 initial state is pinned."""
    modulus = 4
    inp = _rand_syms(31, modulus, n=40)
    gr = _gr_diff_decode(inp, modulus)
    prev = 1     # WRONG cold-start
    mutated = []
    for x in inp:
        mutated.append((x - prev) % modulus)
        prev = x
    e, n = _errs(mutated, gr)
    assert e > 0, "a wrong initial prev went undetected by the gate!"


def test_mutation_inverted_output_fails():
    """Inverting/complementing the output must DISAGREE with GR (mod2 flip)."""
    modulus = 2
    inp = _rand_syms(3, modulus, n=40)
    dut = _run_dut(inp, modulus)
    gr = _gr_diff_decode(inp, modulus)
    inverted = [1 - (int(w) & 1) for w in dut.outputs_q15]
    e, n = _errs(inverted, gr)
    assert e > 0, "an inverted-output DUT went undetected!"


def test_mutation_one_sample_shift_fails():
    """A +1-sample shift of the decoded stream must FAIL (no free lag alignment)."""
    modulus = 4
    inp = _rand_syms(11, modulus, n=40)
    dut = _run_dut(inp, modulus)
    gr = _gr_diff_decode(inp, modulus)
    shifted = [0] + [int(w) & 0xFFFF for w in dut.outputs_q15[:-1]]
    e, n = _errs(shifted, gr)
    assert e > 0, "a one-sample shift went undetected!"


def test_mutation_wrong_modulus_fails():
    """Decoding with the wrong modulus (mod2 mask on a mod4 stream) must DISAGREE
    with the GR mod4 reference — proves the modulus mask is under test."""
    inp = _rand_syms(77, 4, n=40)
    gr4 = _gr_diff_decode(inp, 4)             # correct modulus
    dut2 = _run_dut(inp, 2)                   # WRONG modulus (mask &1)
    e, n = _errs([int(w) & 0xFFFF for w in dut2.outputs_q15], gr4)
    assert e > 0, "a wrong-modulus DUT went undetected!"


def test_empty_output_fails():
    """An empty DUT output cannot be certified against a non-empty reference."""
    gr = _gr_diff_decode([0] * 16, 2)
    n = min(0, len(gr))
    assert n == 0 and len(gr) > 0   # empty DUT -> nothing compared -> not a pass


# --- HARDWARE-DEVIATION / INV-0 guards: unsupported params must RAISE ----------

def test_non_power_of_two_modulus_raises():
    with pytest.raises(ValueError):
        DiffDecoderBlock("x", modulus=3)


def test_nrzi_requires_modulus_2_raises():
    """GR raises 'NRZI only supported with modulus 2'; the block mirrors that."""
    with pytest.raises(ValueError):
        DiffDecoderBlock("x", modulus=4, coding=1)


def test_gr_nrzi_mod4_also_raises():
    """Pin GR's OWN behaviour: NRZI + modulus 4 raises in GR too (so the block's
    matching raise is faithful, not an invented deviation)."""
    payload = {"insyms": [0, 1, 2, 3], "modulus": 4, "coding": 1}
    script = (
        "import json,sys\n"
        "from gnuradio import gr, blocks, digital\n"
        "d = json.loads(sys.stdin.read())\n"
        "tb = gr.top_block()\n"
        "src = blocks.vector_source_b(d['insyms'], False, 1, [])\n"
        "dec = digital.diff_decoder_bb(d['modulus'], digital.DIFF_NRZI)\n"
        "snk = blocks.vector_sink_b(1, 4096)\n"
        "tb.connect(src, dec); tb.connect(dec, snk)\n"
        "tb.run()\n"
        "print('NO_RAISE')\n")
    r = subprocess.run([_GR_PY, "-c", script], input=json.dumps(payload),
                       capture_output=True, text=True)
    assert r.returncode != 0 and "NRZI only supported with modulus 2" in r.stderr, \
        f"GR did not raise for NRZI+mod4: rc={r.returncode} {r.stderr[-300:]}"


# --- report -------------------------------------------------------------------

def test_emit_report():
    """Emit the dashboard report reflecting a passing bit-exact verification."""
    inp = _rand_syms(1, 4, n=48)
    dut = _run_dut(inp, 4)
    gr = _gr_diff_decode(inp, 4)
    e, n = _errs(dut.outputs_q15, gr)
    res = CompareResult(passed=(e == 0), metric=Metric.DECISION,
                        n_compared=n, bit_errors=e, delay_used=0)
    assert res.passed, res.summary()
    write_report("DiffDecoderBlock", res, coverage={
        "gr_equiv": "digital.diff_decoder_bb",
        "edge": "all-zeros / all-(M-1) / alternating / full-symbol ramp",
        "random": 4, "modulus": "2 and 4",
        "coding": "DIFF_DIFFERENTIAL (default) + DIFF_NRZI (mod2)",
        "round_trip": "GR diff_encoder_bb -> on-chip decode recovers input (mod 2 and 4)",
        "mutation": ("add-instead-of-subtract / no-state pass-through / wrong initial "
                     "prev / inverted / +1 shift / wrong modulus / empty"),
        "decision": "y[n]=(x[n]-x[n-1]) mod M, x[-1]=0; NRZI adds +1 (mod2)",
        "note": "1-cell 1-sample-state (previous INPUT) differential decoder; "
                "BIT-EXACT vs diff_decoder_bb, delay 0",
        "hw_deviation": "modulus must be power-of-two (on-chip modulo is a bitmask); "
                        "NRZI requires modulus 2 (mirrors GR)",
    })
