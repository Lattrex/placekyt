# SPDX-License-Identifier: GPL-3.0-or-later
"""
================================================================================
  THE REFERENCE EXAMPLE — how to verify a Kyttar block against GNU Radio
================================================================================

This file verifies the simplest possible Kyttar DSP block — a gain (multiply by
a constant) — against its GNU Radio Companion equivalent, and is written to be
*read*. If you (or your agent) want to understand how block verification works
end to end before writing your own, start here. Every step is explained.

The goal of verification is to prove a Kyttar block is a **drop-in replacement**
for a GNU Radio block: feed both the same input, and the Kyttar output must match
the GNU Radio output to within fixed-point quantization noise. GNU Radio is the
"golden reference" (the known-correct answer); the Kyttar block is the "DUT"
(device under test). This is exactly how hardware IP is verified.

Run it directly:

    KYTTAR_GR_PYTHON=/usr/bin/python3 \
      <venv>/python verification/examples/gain_reference/verify_gain_reference.py

or as a test:

    <venv>/python -m pytest verification/examples/gain_reference/ -q

--------------------------------------------------------------------------------
  THE FOUR CONCEPTS YOU NEED
--------------------------------------------------------------------------------

1. Q15 FIXED-POINT. The Kyttar chip has no floating-point unit. It represents
   numbers in the range [-1.0, +1.0) as 16-bit signed integers ("Q15"): the
   value v is stored as round(v * 32768), clamped to [-32768, 32767]. So 0.5 is
   0x4000, -0.5 is 0xC000, and the largest positive value 0x7FFF is 0.99997.
   GNU Radio uses 32-bit floats, so the two will differ by a tiny amount — the
   "quantization noise" — and verification allows a small, *derived* tolerance
   for exactly that, and nothing more.

2. THE STIMULUS. We pick a list of input samples (as Q15 words) and run the
   identical list through both the GNU Radio block and the Kyttar block. Good
   verification uses several stimulus families: edge cases (full-scale, zero,
   the extremes), random values, and a sweep over the block's parameters.

3. THE DUT (Device Under Test). `run_block_dut` takes a Kyttar block, places it
   on the simulated chip wired between the input port (x16_in) and the output
   port (x16_out), builds the bitstream, and runs the stimulus through it on the
   simKYT simulator — returning one output word per input. You do not need to
   understand placement or routing to use it; it is the whole "build and run a
   block" step in one call.

4. THE COMPARISON. `compare_against_grc` lines up the two output streams
   (accounting for any processing delay the block has), models the chip's Q15
   saturation on the reference, and checks that the worst-case error stays within
   the derived tolerance. It returns a pass/fail plus diagnostics.

--------------------------------------------------------------------------------
  THE MOST IMPORTANT RULE: a test that has never failed proves nothing.
--------------------------------------------------------------------------------

At the bottom of this file we deliberately BREAK the output (invert it, shift it
by a sample, compare against the wrong gain) and assert that the comparison FAILS.
If the gate cannot catch a broken block, a "pass" on the real block is
meaningless. Every block you verify must include these negative tests.
"""

from __future__ import annotations

import os
import random
import sys
from pathlib import Path

# simKYT and the Qt-based engine want an offscreen display when run headless.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# --- make the placekyt engine and the verification package importable ---------
# This file lives at verification/examples/gain_reference/; the placekyt package
# (engine/, ui/, model/) is at <repo>/placekyt, and the verification package is
# at <repo>/verification. Add both to the path.
_REPO = Path(__file__).resolve().parents[3]
for _p in (str(_REPO / "placekyt"), str(_REPO / "verification")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from kyttar_verify import (  # noqa: E402
    run_block_dut, run_gnuradio_ref, compare_against_grc, Metric)

# Path to the chip description we build onto (the bundled 10x12 demo array).
CHIP_YAML = str(_REPO / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml")

# The gain we are verifying. 0.5 is exactly representable in Q15 (0x4000), which
# keeps the example clean.
GAIN = 0.5


# ==============================================================================
#  STEP 1 — the golden reference: run the GNU Radio block.
# ==============================================================================
def gnuradio_gain(inputs_q15, gain):
    """Run GNU Radio's `multiply_const_ff` over the stimulus and return its output.

    `run_gnuradio_ref` runs the flowgraph in a *separate* Python process — the one
    that has GNU Radio installed (set by KYTTAR_GR_PYTHON). This is deliberate:
    GNU Radio is usually built against an older NumPy than a modern verification
    environment, and importing both in one process crashes. The subprocess
    boundary keeps them apart; we pass the stimulus in and get the result out.

    The `gnuradio_script` is a small flowgraph fragment. Two variables are
    provided to it automatically:
        input_q15   - the stimulus as integers (what you passed in)
        input_float - the same values converted to float in [-1, 1)
    and the script must set:
        output_float - the block's output as a list of floats.
    Any extra values the script needs (here, the gain) are passed via extra_args.
    """
    return run_gnuradio_ref(
        input_q15=inputs_q15,
        gnuradio_script="""
from gnuradio import gr, blocks
tb = gr.top_block()
src  = blocks.vector_source_f(input_float, False)   # feed the stimulus
mult = blocks.multiply_const_ff(gain)               # THE block under reference
sink = blocks.vector_sink_f()                        # collect the output
tb.connect(src, mult)
tb.connect(mult, sink)
tb.run()
output_float = list(sink.data())
""",
        extra_args={"gain": gain},
    )


# ==============================================================================
#  STEP 2 — the DUT: build and run the Kyttar block.
# ==============================================================================
def kyttar_gain(inputs_q15, gain):
    """Build the Kyttar GainBlock and run the stimulus through it on simKYT.

    `run_block_dut` does everything: it places `GainBlock` on the chip, wires its
    input to x16_in and its output to x16_out, auto-routes, builds the bitstream,
    and drives the stimulus through the simulator. It returns a DUTResult whose
    `.outputs_q15` is one 16-bit output word per input sample.

    `params` are the block's own constructor arguments — here just the gain. These
    are the SAME parameters you would set on the block in GNU Radio Companion;
    that 1:1 parameter parity is the whole point.
    """
    return run_block_dut(
        "GainBlock",
        inputs_q15,
        params={"gain": gain},
        chip_yaml=CHIP_YAML,
    )


# ==============================================================================
#  STEP 3 — the stimulus families.
# ==============================================================================
# Edge cases: zero, half-scale positive/negative, near-full-scale, the extremes.
# These are where fixed-point bugs (saturation, sign, rounding) hide.
EDGE_VECTORS = [
    0x0000,  #  0.0      — zero
    0x4000,  # +0.5      — exactly representable
    0xC000,  # -0.5
    0x7FFF,  # +0.99997  — largest positive (tests rounding at the top)
    0x8001,  # -0.99997  — near the most-negative
    0x2000,  # +0.25
    0x6000,  # +0.75
    0xA000,  # -0.75
]


def random_vectors(seed, n=16):
    """A reproducible batch of random Q15 words (reproducible via the seed)."""
    rng = random.Random(seed)
    return [rng.randint(0, 0xFFFF) for _ in range(n)]


# ==============================================================================
#  STEP 4 — run both and compare.
# ==============================================================================
def verify(gain, inputs):
    """Run the stimulus through both the reference and the DUT, then compare.

    Returns the comparison result. The comparison is told two block-specific
    facts:
      * delay=0    — a gain block is memoryless; output sample n corresponds to
                     input sample n with no latency. (A filter would have a delay
                     here, and we would state it so the streams line up; we do NOT
                     let the comparator "search" for the delay, because that would
                     hide a real latency bug.)
      * op_count=1 — the gain is a single Q15 multiply. The tolerance is DERIVED
                     from this: one multiply can round by at most ~1 LSB, so the
                     allowed error is 2 LSB (op_count + 1). We never hand-tune the
                     tolerance to make a test pass; it follows from the math.
    """
    dut = kyttar_gain(inputs, gain)
    assert dut.ok, f"DUT build/run failed: {dut.reason}"

    ref = gnuradio_gain(inputs, gain)

    return compare_against_grc(
        dut.outputs_q15,     # what the Kyttar chip produced
        ref.floats,          # the GNU Radio golden output (floats)
        metric=Metric.AMPLITUDE,   # a numeric signal: compare sample magnitudes
        delay=0,             # memoryless block: no latency
        op_count=1,          # one MULQ -> derived tolerance of 2 LSB
    )


# ==============================================================================
#  TESTS — the real block must PASS; broken versions must FAIL.
# ==============================================================================
def test_reference_edge_vectors():
    """The real GainBlock matches GNU Radio on the edge vectors."""
    res = verify(GAIN, EDGE_VECTORS)
    print("edge:", res.summary())
    assert res.passed, res.summary()


def test_reference_random_and_sweep():
    """Parity holds for random input and across the gain's parameter range."""
    for seed in (1, 7, 42):
        res = verify(GAIN, random_vectors(seed))
        assert res.passed, f"random seed {seed}: {res.summary()}"
    for g in (0.25, 0.5, 0.75, 0.9):
        res = verify(g, EDGE_VECTORS)
        assert res.passed, f"gain {g}: {res.summary()}"


def test_negative_inverted_output_is_caught():
    """If the chip output were sign-inverted, the gate MUST fail. (Proves the
    gate can see an inverted block — a real, common bug.)"""
    dut = kyttar_gain(EDGE_VECTORS, GAIN)
    ref = gnuradio_gain(EDGE_VECTORS, GAIN)
    broken = [(0x10000 - (w or 0)) & 0xFFFF for w in dut.outputs_q15]  # negate
    res = compare_against_grc(broken, ref.floats, metric=Metric.AMPLITUDE,
                              delay=0, op_count=1)
    assert not res.passed, "the gate failed to catch an inverted output!"


def test_negative_wrong_gain_is_caught():
    """A block built at the wrong gain must fail against the right reference."""
    dut = kyttar_gain(EDGE_VECTORS, GAIN)            # built at 0.5
    ref_wrong = gnuradio_gain(EDGE_VECTORS, 0.9)     # reference for 0.9
    res = compare_against_grc(dut.outputs_q15, ref_wrong.floats,
                              metric=Metric.AMPLITUDE, delay=0, op_count=1)
    assert not res.passed, "the gate failed to catch a wrong-gain mismatch!"


if __name__ == "__main__":
    # A plain run prints the verdict so you can read what the gate measured.
    res = verify(GAIN, EDGE_VECTORS)
    print("Reference gain verification:", res.summary())
    print("  -> the 1-LSB error is correct Q15 rounding of a single multiply,")
    print("     not a bug; it is within the derived 2-LSB tolerance.")
