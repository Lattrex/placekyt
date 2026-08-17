# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify DelayBlock against GNU Radio blocks.delay.

Integer-sample delay line: y[n] = x[n-delay] (prepend ``delay`` zeros, then the
input stream). Pure data movement (a ``delay``-deep shift register of Q15 samples,
all initialised to 0), so the output is BIT-EXACT to the input, shifted later in
time by exactly ``delay`` samples.

Alignment is asserted the RIGHT way (INV-2): a delay block is a KNOWN, non-zero
integer shift, so the tests assert that exact shift — the first ``delay`` outputs
are zero and ``dut[delay+k] == x[k]`` — NOT a free best-match. GR ``blocks.delay``
emits ``[0]*delay + x`` (it flushes its line for ``delay`` extra samples); the
per-sample DUT harness emits one word per trigger, so the DUT stream is
``[0]*delay + x[:N-delay]`` = GR's first ``N`` outputs, compared with ``delay=0``
(both streams already carry the prepended zeros; the compare `delay` param models a
DUT that DROPS leading ref samples, which is the opposite of a delay). The mandatory
mutations (INV-4) — a +1 EXTRA delay, a no-delay pass-through, an ADVANCE, a wrong
delay, empty — must all FAIL the gate.

HARDWARE LIMIT: the delay line + its shift program must fit ONE 32-word cell, so the
depth is capped at ``DelayBlock.MAX_DELAY`` (12); a larger delay RAISES (never
clamps). Verified: 1..12 emit the correct delayed stream; 13 builds but emits no
output; 14 raises.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_delay.py -x -q
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
    run_block_dut, run_gnuradio_ref, compare_against_grc, write_report, Metric)
from gr_kyttar.placement.blocks.delay_block import DelayBlock  # noqa: E402

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_AVAILABLE),
    reason="chip yaml or GNU Radio interpreter absent")


# --------------------------------------------------------------------------- util
def _s16(v):
    if v is None:
        return None
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def _random(seed, n=40):
    rng = random.Random(seed)
    # keep off the lone -1.0 corner is unnecessary (no arithmetic), but use the
    # full Q15 range so a byte-level corruption anywhere is caught.
    return [rng.randint(0, 0xFFFF) for _ in range(n)]


def _gr_delay(stim, delay):
    """LIVE GNU Radio blocks.delay(delay) over ``stim`` (Q15 words)."""
    return run_gnuradio_ref(
        input_q15=stim,
        gnuradio_script="""
from gnuradio import gr, blocks
tb = gr.top_block()
src = blocks.vector_source_f(input_float, False)
dly = blocks.delay(gr.sizeof_float, delay)
snk = blocks.vector_sink_f()
tb.connect(src, dly); tb.connect(dly, snk)
tb.run()
output_float = list(snk.data())
""",
        extra_args={"delay": delay},
    )


def _dut(stim, delay):
    return run_block_dut("DelayBlock", stim, params={"delay": delay},
                         chip_yaml=CHIP_YAML)


# =============================================================================
# 1. Core equivalence vs LIVE GNU Radio blocks.delay
# =============================================================================
def test_delay_default_matches_gr():
    """delay=1 (GR default) — DUT bit-exact to blocks.delay(1)."""
    stim = _random(7)
    gr = _gr_delay(stim, 1)
    dut = _dut(stim, 1)
    assert dut.ok, dut.reason
    res = compare_against_grc(dut.outputs_q15, gr.floats, metric=Metric.EXACT,
                              delay=0)
    assert res.passed, res.summary()
    write_report("DelayBlock", res,
                 coverage={"edge": True, "random": 3, "param_sweep": 8,
                           "mutation": True})


@pytest.mark.parametrize("delay", [1, 2, 3, 4, 5, 6, 7, 8])
def test_delay_sweep_1_to_8_matches_gr(delay):
    """Sweep the delay across 1..8 vs live GR blocks.delay — bit-exact each."""
    stim = _random(100 + delay, n=48)
    gr = _gr_delay(stim, delay)
    dut = _dut(stim, delay)
    assert dut.ok, dut.reason
    res = compare_against_grc(dut.outputs_q15, gr.floats, metric=Metric.EXACT,
                              delay=0)
    assert res.passed, f"delay={delay}: {res.summary()}"


@pytest.mark.parametrize("seed", [11, 22, 33])
def test_delay_random_seeds_match_gr(seed):
    """Three random seeds at a mid depth — bit-exact vs GR."""
    stim = _random(seed, n=48)
    gr = _gr_delay(stim, 5)
    dut = _dut(stim, 5)
    assert dut.ok, dut.reason
    res = compare_against_grc(dut.outputs_q15, gr.floats, metric=Metric.EXACT,
                              delay=0)
    assert res.passed, f"seed={seed}: {res.summary()}"


def test_delay_max_supported_matches_gr():
    """The deepest supported delay (MAX_DELAY=12) still emits + matches GR."""
    D = DelayBlock.MAX_DELAY
    stim = _random(999, n=48)
    gr = _gr_delay(stim, D)
    dut = _dut(stim, D)
    assert dut.ok, dut.reason
    assert None not in dut.outputs_q15, "max-depth delay dropped an output"
    res = compare_against_grc(dut.outputs_q15, gr.floats, metric=Metric.EXACT,
                              delay=0)
    assert res.passed, f"delay={D}: {res.summary()}"


# =============================================================================
# 2. Edge cases
# =============================================================================
def test_delay_zero_is_identity():
    """delay=0 — a pure pass-through (identity), bit-exact to the input & GR."""
    stim = _random(5)
    gr = _gr_delay(stim, 0)
    dut = _dut(stim, 0)
    assert dut.ok, dut.reason
    # identity: every output equals its input, no shift.
    assert [_s16(v) for v in dut.outputs_q15] == [_s16(v) for v in stim]
    res = compare_against_grc(dut.outputs_q15, gr.floats, metric=Metric.EXACT,
                              delay=0)
    assert res.passed, res.summary()


def test_delay_impulse_shifts_by_exactly_delay():
    """An impulse in -> impulse out shifted by EXACTLY `delay` (the alignment
    assertion, INV-2): the impulse must land at index `delay`, nowhere else."""
    for D in (1, 3, 6):
        N = 24
        stim = [0] * N
        stim[0] = 0x4000  # +0.5 impulse at index 0
        dut = _dut(stim, D)
        assert dut.ok, dut.reason
        out = [_s16(v) for v in dut.outputs_q15]
        # the impulse must appear at index D and ONLY there.
        assert out[D] == 0x4000, f"D={D}: impulse not at index {D}: {out[:D+2]}"
        assert all(out[i] == 0 for i in range(N) if i != D), \
            f"D={D}: spurious energy off the impulse index: {out}"


def test_delay_leading_zeros_and_shift_alignment():
    """Assert the EXACT integer shift directly against the RAW input (not GR, not a
    free lag): dut[:D] are all zero and dut[D+k] == x[k]. This is the teeth that a
    wrong shift / advance / pass-through cannot satisfy."""
    for D in (1, 2, 5, DelayBlock.MAX_DELAY):
        stim = _random(4000 + D, n=48)
        dut = _dut(stim, D)
        assert dut.ok, dut.reason
        out = [_s16(v) for v in dut.outputs_q15]
        assert all(v == 0 for v in out[:D]), f"D={D}: leading zeros wrong: {out[:D]}"
        xs = [_s16(v) for v in stim]
        for k in range(len(out) - D):
            assert out[D + k] == xs[k], \
                f"D={D}: shift misaligned at k={k}: {out[D+k]} != {xs[k]}"


# =============================================================================
# 3. Bit-exact vs the block's own Q15 reference (the on-chip datapath model)
# =============================================================================
@pytest.mark.parametrize("delay", [0, 1, 4, 9, 12])
def test_delay_bit_exact_vs_own_reference(delay):
    stim = _random(700 + delay, n=48)
    dut = _dut(stim, delay)
    assert dut.ok, dut.reason
    ref = DelayBlock("d", delay=delay).process_reference_q15(stim)
    res = compare_against_grc(dut.outputs_q15, [_s16(v) / 32768.0 for v in ref],
                              metric=Metric.EXACT, delay=0)
    assert res.passed, f"delay={delay}: {res.summary()}"


# =============================================================================
# 4. Mutation / negative tests — the gate MUST FAIL on a corrupted DUT (INV-4)
# =============================================================================
def _corrupt_stream(stim, delay):
    """Build the correct DUT output once; the mutation tests perturb it."""
    dut = _dut(stim, delay)
    assert dut.ok, dut.reason
    return [_s16(v) for v in dut.outputs_q15]


def test_mutation_plus_one_delay_fails():
    """A +1 EXTRA sample of delay (shift the whole DUT stream one more) must FAIL
    against the correct GR reference."""
    stim = _random(31, n=48)
    gr = _gr_delay(stim, 4)
    good = _corrupt_stream(stim, 4)
    shifted = [0] + good[:-1]  # one extra sample of delay
    res = compare_against_grc(shifted, gr.floats, metric=Metric.EXACT, delay=0)
    assert not res.passed, "gate did NOT catch a +1 extra-delay mutation"


def test_mutation_no_delay_passthrough_fails():
    """A no-delay pass-through (delay ignored, raw input echoed) must FAIL for a
    non-zero commanded delay."""
    stim = _random(32, n=48)
    gr = _gr_delay(stim, 4)          # reference is delayed by 4
    passthrough = [_s16(v) for v in stim]  # DUT that ignores the delay
    res = compare_against_grc(passthrough, gr.floats, metric=Metric.EXACT, delay=0)
    assert not res.passed, "gate did NOT catch a no-delay (pass-through) mutation"


def test_mutation_advance_instead_of_delay_fails():
    """An ADVANCE (y[n]=x[n+delay], drop the leading samples instead of delaying)
    must FAIL — a delay and an advance are opposite shifts."""
    stim = _random(33, n=48)
    D = 4
    gr = _gr_delay(stim, D)
    advanced = [_s16(v) for v in stim[D:]] + [0] * D  # shifted the wrong way
    res = compare_against_grc(advanced, gr.floats, metric=Metric.EXACT, delay=0)
    assert not res.passed, "gate did NOT catch an advance-instead-of-delay mutation"


def test_mutation_wrong_delay_fails():
    """A DUT built with the WRONG delay (3) fails against the GR reference for the
    commanded delay (4)."""
    stim = _random(34, n=48)
    gr = _gr_delay(stim, 4)
    wrong = _corrupt_stream(stim, 3)  # actually a 3-sample delay line
    res = compare_against_grc(wrong, gr.floats, metric=Metric.EXACT, delay=0)
    assert not res.passed, "gate did NOT catch a wrong-delay (3 vs 4) mutation"


def test_mutation_empty_fails():
    """Empty DUT output must FAIL (green must not be reachable by producing
    nothing)."""
    stim = _random(35, n=48)
    gr = _gr_delay(stim, 4)
    res = compare_against_grc([], gr.floats, metric=Metric.EXACT, delay=0)
    assert not res.passed, "gate did NOT catch an empty DUT output"


# =============================================================================
# 5. Hardware limit — the depth ceiling RAISES (never clamps), INV-0
# =============================================================================
def test_delay_over_budget_raises():
    """A delay past the single-cell budget RAISES a ValueError — never silently
    clamps/truncates the delay (that would be a different block)."""
    with pytest.raises(ValueError, match="exceeds the single-cell"):
        DelayBlock("d", delay=DelayBlock.MAX_DELAY + 1)


def test_delay_negative_raises():
    with pytest.raises(ValueError, match="delay must be >= 0"):
        DelayBlock("d", delay=-1)
