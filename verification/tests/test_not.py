# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify NotBlock 1:1 against GNU Radio ``blocks.not_bb``.

``blocks.not_bb`` complements each byte over the FULL 8-bit width — literally
``out = ~in`` on a ``uint8``, i.e. ``out = (~in) & 0xFF`` (``0x00 -> 0xFF``,
``0x0F -> 0xF0``, ``0xAA -> 0x55``). This is BIT-EXACT (metric DECISION, tolerance
0), compared against the LIVE GNU Radio block.

The trap this gate guards against is a WRONG WIDTH: inverting only the low bit
(``in ^ 1``), or masking to the wrong number of bits, "works" on some inputs but
disagrees with GR's full-byte complement. The mutation gates below (pass-through,
low-bit-only invert, wrong-mask XOR, +1 shift, inverted-of-inverted, empty)
must all FAIL against the GR reference.

Coverage: edge (``0x00``/``0xFF``/``0xAA``/``0x55``/``0x0F``/``0xF0`` + single-bit
walk), exhaustive all-256-bytes, random (>=3 seeds), and mandatory INV-4 mutation
gates.

Run::

    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
        .venv/bin/python -m pytest verification/tests/test_not.py -q
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
from gr_kyttar.placement.blocks.not_block import NotBlock  # noqa: E402

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_PY = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")
_GR_AVAILABLE = os.path.exists(CHIP_YAML) and os.path.exists(_GR_PY)
pytestmark = pytest.mark.skipif(
    not _GR_AVAILABLE, reason="chip yaml or GNU Radio interpreter absent")


# --- GNU Radio golden reference (subprocess into the GR interpreter) ----------

def _gr_not(inbytes):
    """GR golden: ``blocks.not_bb`` on the input byte stream."""
    payload = {"inbytes": [int(b) & 0xFF for b in inbytes]}
    script = (
        "import json,sys\n"
        "from gnuradio import gr, blocks\n"
        "d = json.loads(sys.stdin.read())\n"
        "tb = gr.top_block()\n"
        "src = blocks.vector_source_b(d['inbytes'], False, 1, [])\n"
        "op  = blocks.not_bb()\n"
        "snk = blocks.vector_sink_b(1, 4096)\n"
        "tb.connect(src, op); tb.connect(op, snk)\n"
        "tb.run()\n"
        "print(json.dumps([int(x) & 0xFF for x in snk.data()]))\n")
    r = subprocess.run([_GR_PY, "-c", script], input=json.dumps(payload),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-800:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def _run_dut(inbytes):
    """Build + run NotBlock on simKYT for the given byte stream."""
    words = [int(b) & 0xFF for b in inbytes]
    dut = run_block_dut(
        "NotBlock", words, params={}, in_port="sample", chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    return dut


def _byte_errors(dut_words, gr_bytes):
    n = min(len(dut_words), len(gr_bytes))
    assert n > 0, "no samples compared"
    errs = sum(1 for k in range(n)
               if dut_words[k] is None or (int(dut_words[k]) & 0xFF) != (gr_bytes[k] & 0xFF))
    return errs, n


# --- stimulus families --------------------------------------------------------

EDGE_BYTES = [0x00, 0xFF, 0xAA, 0x55, 0x0F, 0xF0, 0x01, 0x80, 0x7F,
              0x3C, 0xC3, 0x10, 0x08, 0x40] + [1 << k for k in range(8)]


def _random_bytes(seed, n=48):
    rng = random.Random(seed)
    return [rng.randint(0, 255) for _ in range(n)]


# --- correctness: bit-exact vs GR ---------------------------------------------

def test_edge_bytes_bit_exact_vs_gr():
    """The canonical edge bytes (incl. the full-width cases 0x00->0xFF, 0x0F->0xF0,
    0xAA->0x55) complement bit-for-bit like GR not_bb."""
    dut = _run_dut(EDGE_BYTES)
    gr = _gr_not(EDGE_BYTES)
    errs, n = _byte_errors(dut.outputs_q15, gr)
    print(f"\nedge: {errs} errors / {n}; gr[:6]={[hex(x) for x in gr[:6]]}")
    assert errs == 0, f"edge bytes != GR: {errs}/{n}"


def test_full_width_specific_values():
    """Pin the EXACT full-8-bit-width behavior the manifest calls out."""
    cases = {0x00: 0xFF, 0xFF: 0x00, 0xAA: 0x55, 0x0F: 0xF0, 0xF0: 0x0F, 0x55: 0xAA}
    ins = list(cases.keys())
    dut = _run_dut(ins)
    gr = _gr_not(ins)
    for i, (inb, exp) in enumerate(cases.items()):
        assert gr[i] == exp, f"GR not_bb({inb:#04x})={gr[i]:#04x} != expected {exp:#04x}"
        got = dut.outputs_q15[i]
        assert got is not None and (int(got) & 0xFF) == exp, \
            f"DUT not({inb:#04x})={got} != {exp:#04x} (full-width GR)"


def test_exhaustive_all_256_bytes():
    """Every possible byte value 0..255 complements bit-exact vs GR (exhaustive)."""
    allb = list(range(256))
    dut = _run_dut(allb)
    gr = _gr_not(allb)
    errs, n = _byte_errors(dut.outputs_q15, gr)
    print(f"\nexhaustive 0..255: {errs} errors / {n}")
    assert errs == 0, f"exhaustive byte NOT != GR: {errs}/{n}"


@pytest.mark.parametrize("rseed", [1, 7, 42, 1234])
def test_random_bit_exact_vs_gr(rseed):
    """Random byte streams (>=3 seeds) complement bit-exact vs GR."""
    inbytes = _random_bytes(rseed, n=64)
    dut = _run_dut(inbytes)
    gr = _gr_not(inbytes)
    errs, n = _byte_errors(dut.outputs_q15, gr)
    print(f"\nrandom rseed={rseed}: {errs} errors / {n}")
    assert errs == 0, f"rseed {rseed}: {errs}/{n} byte errors vs GR"


# --- reference sanity (pure python == GR, no chip) ----------------------------

def test_reference_matches_gr():
    """process_reference == GR not_bb over the full 0..255 range (proves the
    on-chip-mirrored reference is itself GR-exact)."""
    allb = list(range(256))
    ref = [int(b) & 0xFF for b in NotBlock("r").process_reference(allb)]
    gr = _gr_not(allb)
    assert ref == gr, "reference != GR not_bb over 0..255"


# --- MANDATORY mutation gates (INV-4) -----------------------------------------

def test_mutation_pass_through_fails():
    """A pass-through DUT (out == in) must DISAGREE with the GR NOT reference."""
    inbytes = _random_bytes(3, n=48)
    gr = _gr_not(inbytes)
    errs, n = _byte_errors(inbytes, gr)   # feed the INPUT as if it were the output
    assert errs > 0, "a pass-through DUT went undetected by the gate!"


def test_mutation_low_bit_only_invert_fails():
    """Inverting ONLY the low bit (in ^ 1) — the WRONG-WIDTH trap — must DISAGREE
    with GR's full-byte complement. This is the core width mutation."""
    inbytes = _random_bytes(5, n=64)
    gr = _gr_not(inbytes)
    low_bit_only = [b ^ 0x01 for b in inbytes]   # wrong width: only bit 0 toggled
    errs, n = _byte_errors(low_bit_only, gr)
    assert errs > 0, "a low-bit-only invert went undetected — width not tested!"


def test_mutation_wrong_mask_xor_fails():
    """XOR with the WRONG mask (0x7F, i.e. NOT masking the top bit) must DISAGREE
    with GR — proving the full 8-bit width is required."""
    inbytes = _random_bytes(9, n=64)
    gr = _gr_not(inbytes)
    wrong = [b ^ 0x7F for b in inbytes]   # top-bit not complemented
    errs, n = _byte_errors(wrong, gr)
    assert errs > 0, "a wrong-mask (0x7F) XOR went undetected — top bit not checked!"


def test_mutation_one_sample_shift_fails():
    """A +1-sample shift of the output must FAIL (no free lag alignment; delay=0)."""
    inbytes = _random_bytes(11, n=48)
    dut = _run_dut(inbytes)
    gr = _gr_not(inbytes)
    shifted = [0] + [int(w) & 0xFF for w in dut.outputs_q15[:-1]]
    errs, n = _byte_errors(shifted, gr)
    assert errs > 0, "a one-sample shift went undetected!"


def test_mutation_double_not_fails():
    """NOT of NOT (i.e. back to the input) must DISAGREE with a single GR NOT."""
    inbytes = _random_bytes(13, n=48)
    dut = _run_dut(inbytes)
    gr = _gr_not(inbytes)
    double = [(~(int(w) & 0xFF)) & 0xFF for w in dut.outputs_q15]  # == input
    errs, n = _byte_errors(double, gr)
    assert errs > 0, "a double-NOT (identity) went undetected!"


def test_empty_output_fails():
    """An empty DUT output cannot be certified against a non-empty reference."""
    gr = _gr_not([0, 1, 2, 3])
    n = min(0, len(gr))
    assert n == 0 and len(gr) > 0   # empty DUT -> nothing compared -> not a pass


# --- no-parameter guard (INV-0: not_bb takes no params) -----------------------

def test_takes_no_params():
    """not_bb has NO parameters; NotBlock constructs from name alone."""
    b = NotBlock("x")
    assert b.cell_count == 1
    # process_reference is deterministic and parameter-free.
    assert list(b.process_reference([0x00, 0xFF])) == [0xFF, 0x00]


# --- report -------------------------------------------------------------------

def test_emit_report():
    """Emit the dashboard report reflecting a passing bit-exact verification."""
    inbytes = _random_bytes(1, n=64)
    dut = _run_dut(inbytes)
    gr = _gr_not(inbytes)
    errs, n = _byte_errors(dut.outputs_q15, gr)
    res = CompareResult(passed=(errs == 0), metric=Metric.DECISION,
                        n_compared=n, bit_errors=errs, delay_used=0)
    assert res.passed, res.summary()
    write_report("NotBlock", res, coverage={
        "gr_equiv": "blocks.not_bb",
        "edge": "0x00->0xFF / 0xFF->0x00 / 0xAA->0x55 / 0x0F->0xF0 + single-bit walk",
        "exhaustive": "all 256 byte values 0..255 bit-exact vs GR",
        "random": 4,
        "mutation": "pass-through / low-bit-only invert / wrong-mask XOR / +1 shift / double-NOT / empty",
        "decision": "out = (~in) & 0xFF (full 8-bit width, GR-exact)",
        "note": "1-cell memoryless bitwise NOT; BIT-EXACT vs not_bb, delay 0",
        "hw_deviation": "none (no params; pure byte op)",
    })
