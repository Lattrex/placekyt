# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify CharToFloatBlock is a Q15 drop-in for GNU Radio ``blocks.char_to_float``.

GR ``char_to_float(scale)`` computes ``out = in / scale`` on an int8 input. On the
Kyttar fabric a "float" is a Q15 value in [-1, 1), so the faithful, representable
domain is ``scale >= 128`` (the int8 range maps into [-1, 1)); GR's default scale=1
is NOT representable and the block RAISES on it (see the class docstring / INV-0
HW-DEVIATION). This suite:

  * feeds the SAME int8 chars to the DUT (as sign-extended words) and to a LIVE
    GNU Radio ``char_to_float`` (the golden reference), comparing within the derived
    single-MULQ Q15 amplitude floor over the WHOLE int8 domain,
  * sweeps scale = 128, 256, 512 and scale points that are NOT powers of two,
  * covers edges (0, +/-127, -128) + random seeds,
  * proves the HW-DEVIATION (scale < 128, incl. the GR default 1, raises), and
  * includes the mandatory mutation tests (INV-4): the gate MUST fail on an
    inverted output, a wrong-scale reference, a +1-sample delay, and empty output.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_char_to_float.py -q
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
for p in (str(_PLACEKYT), str(_VERIFY)):
    if p not in sys.path:
        sys.path.insert(0, p)

from kyttar_verify import (  # noqa: E402
    run_block_dut, run_gnuradio_ref, compare_against_grc, write_report, Metric)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")

_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_AVAILABLE),
    reason="chip yaml or GNU Radio interpreter absent")


# --- char <-> word helpers ----------------------------------------------------
def _char_to_word(c: int) -> int:
    """A signed int8 char carried as a sign-extended 16-bit fabric word."""
    return c & 0xFFFF


def _gr_char_to_float(words, scale):
    """LIVE GNU Radio char_to_float over the int8 chars in ``words`` (low byte)."""
    return run_gnuradio_ref(
        input_q15=words,
        gnuradio_script="""
from gnuradio import gr, blocks
# reconstruct the signed int8 char from each fabric word's low byte
chars = [((w & 0xFF) ^ 0x80) - 0x80 for w in input_q15]
tb = gr.top_block()
src = blocks.vector_source_b([c & 0xFF for c in chars], False, 1)
c2f = blocks.char_to_float(1, scale)
sink = blocks.vector_sink_f()
tb.connect(src, c2f); tb.connect(c2f, sink)
tb.run()
output_float = list(sink.data())
""",
        extra_args={"scale": scale},
    )


# --- stimulus families --------------------------------------------------------
# int8 edges: zero, +/-1, full-scale +127 / -128, and mid values.
EDGE_CHARS = [0, 1, -1, 127, -128, 63, -64, 32, -32, 100, -100]
EDGE = [_char_to_word(c) for c in EDGE_CHARS]


def _random_chars(seed, n=24):
    rng = random.Random(seed)
    return [rng.randint(-128, 127) for _ in range(n)]


def _run_and_compare(scale, words):
    dut = run_block_dut("CharToFloatBlock", words, params={"scale": scale},
                        chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    ref = _gr_char_to_float(words, scale)
    # memoryless single-MULQ feed-forward: delay 0, one Q15 op -> derived floor.
    return dut, compare_against_grc(
        dut.outputs_q15, ref.floats, metric=Metric.AMPLITUDE,
        delay=0, op_count=1)


# --- equivalence: DUT vs LIVE GR over the representable domain -----------------

def test_edge_vectors_scale256():
    dut, res = _run_and_compare(256.0, EDGE)
    print("\nedge scale=256:", res.summary(), "| hop", dut.hop_count,
          "| words", dut.n_words)
    assert res.passed, res.summary()


@pytest.mark.parametrize("scale", [128.0, 256.0, 512.0])
def test_param_sweep_pow2(scale):
    """Power-of-two scales are BIT-EXACT (within the 1-LSB floor)."""
    dut, res = _run_and_compare(scale, EDGE)
    print(f"\nscale={scale}:", res.summary())
    assert res.passed, res.summary()


@pytest.mark.parametrize("scale", [200.0, 300.5, 1000.0])
def test_param_sweep_non_pow2(scale):
    """Non-power-of-two scales differ from GR's float divide by <=1 Q15 LSB
    (MULQ truncates where GR rounds) — inside the derived amplitude floor."""
    dut, res = _run_and_compare(scale, EDGE)
    print(f"\nscale={scale}:", res.summary())
    assert res.passed, res.summary()


def test_full_int8_domain_scale256():
    """The WHOLE int8 domain [-128, 127] matches GR within the floor."""
    words = [_char_to_word(c) for c in range(-128, 128)]
    dut, res = _run_and_compare(256.0, words)
    print("\nfull int8 domain scale=256:", res.summary())
    assert res.passed, res.summary()


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_random_vectors(seed):
    words = [_char_to_word(c) for c in _random_chars(seed)]
    dut, res = _run_and_compare(256.0, words)
    print(f"\nrandom seed={seed}:", res.summary())
    assert res.passed, res.summary()


# --- HW-DEVIATION: the Q15 range limit is enforced, not silently wrapped ------

def test_scale_below_128_raises():
    """scale < 128 (incl. GR default scale=1) is NOT representable in Q15 and
    MUST raise loudly (INV-0 HW-DEVIATION), never silently wrap/clamp."""
    from gr_kyttar.placement import blocks
    cls = blocks.all_block_classes()["CharToFloatBlock"]
    for bad in (1.0, 2.0, 64.0, 127.9):
        with pytest.raises(ValueError):
            cls("bad", scale=bad)


def test_scale_128_is_accepted():
    from gr_kyttar.placement import blocks
    cls = blocks.all_block_classes()["CharToFloatBlock"]
    b = cls("ok", scale=128.0)          # boundary: must NOT raise
    assert b.scale == 128.0


# --- MANDATORY mutation tests (INV-4): the gate must DETECT corruption ---------

def test_mutation_inverted_output_fails():
    dut = run_block_dut("CharToFloatBlock", EDGE, params={"scale": 256.0},
                        chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    ref = _gr_char_to_float(EDGE, 256.0)
    mutated = [(0x10000 - (w or 0)) & 0xFFFF for w in dut.outputs_q15]  # negate
    res = compare_against_grc(mutated, ref.floats, metric=Metric.AMPLITUDE,
                              delay=0, op_count=1)
    assert not res.passed, "gate failed to detect an inverted output!"


def test_mutation_wrong_scale_fails():
    """A DUT built at scale=256 must FAIL against a scale=512 reference."""
    dut = run_block_dut("CharToFloatBlock", EDGE, params={"scale": 256.0},
                        chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    ref_wrong = _gr_char_to_float(EDGE, 512.0)      # different scale
    res = compare_against_grc(dut.outputs_q15, ref_wrong.floats,
                              metric=Metric.AMPLITUDE, delay=0, op_count=1)
    assert not res.passed, "gate failed to detect a wrong-scale mismatch!"


def test_mutation_one_sample_offset_fails():
    dut = run_block_dut("CharToFloatBlock", EDGE, params={"scale": 256.0},
                        chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    ref = _gr_char_to_float(EDGE, 256.0)
    shifted = [0x0000] + list(dut.outputs_q15[:-1])   # +1 sample delay
    res = compare_against_grc(shifted, ref.floats, metric=Metric.AMPLITUDE,
                              delay=0, op_count=1)
    assert not res.passed, "gate failed to detect a 1-sample latency error!"


def test_empty_output_fails():
    ref = _gr_char_to_float(EDGE, 256.0)
    res = compare_against_grc([], ref.floats, metric=Metric.AMPLITUDE)
    assert not res.passed


def test_emit_report():
    """Emit the dashboard report (records verified metrics + coverage). Runs
    last so it reflects a passing verification."""
    dut, res = _run_and_compare(256.0, EDGE)
    assert res.passed, res.summary()
    write_report("CharToFloatBlock", res, coverage={
        "edge": True, "random": 3, "param_sweep": 6,
        "full_int8_domain": True, "hw_deviation_raise": True, "mutation": True})
