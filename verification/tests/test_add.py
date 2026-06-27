# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify AddBlock / SubtractBlock against GNU Radio blocks.add_ff / sub_ff.

These are the generic two-stream combiners — GR's ``blocks.add_ff``
(``out = a + b``) and ``blocks.sub_ff`` (``out = a - b``) of two real input
streams. On chip each is a single cell: one ADD/SUB plus a SATURATING clamp
(production fixed-point — the Q15 ALU would otherwise WRAP, turning 0.6+0.6 into
a sign-flipped -0.8). add_ff/sub_ff take no params (full GRC parity).

The two streams use the proven complex-burst fan-in (the Costas xi/xq tap): each
sample is ``WRITE a -> R0`` + ``WRITE b -> R1`` + one ``JUMP``. The harness carries
them as one complex array (real = a, imag = b); the real output lands in I.

Two reference tiers:
  * DSP equivalence — DUT vs GR add_ff/sub_ff, AMPLITUDE, on IN-RANGE stimulus
    (|a±b| < 1, where the float result is Q15-representable so saturate ≡ true sum).
  * Bit-exact substrate — DUT vs process_reference_q15 (the SATURATING add/sub),
    EXACT, including the overflow corners.

Saturation is verified directly: an out-of-range sum must PIN to ±full-scale (no
wrap / sign flip). Per INV-4 every gate is paired with a mutation that must FAIL;
subtract additionally checks swapped-stream (a-b ≠ b-a), while add does not (a+b
is commutative — documented). Memoryless → delay=0.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_add.py -x -q
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
    run_block_dut_complex, run_block_dut_nstream, run_gnuradio_ref_complex,
    compare_against_grc, write_report, Metric)
from gr_kyttar.placement.blocks.add_block import AddBlock, SubtractBlock  # noqa: E402

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_AVAILABLE),
    reason="chip yaml or GNU Radio interpreter absent")

# (block_type, GR factory, class) — parameterizes the whole suite over add & sub.
_VARIANTS = {
    "add": ("AddBlock", "blocks.add_ff()", AddBlock),
    "sub": ("SubtractBlock", "blocks.sub_ff()", SubtractBlock),
}


def _s16(v):
    if v is None:
        return None
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def _q15(v: float) -> int:
    q = int(round(v * 32768.0))
    return max(-32768, min(32767, q)) & 0xFFFF


# In-range edge pairs (|a±b| < 1 for BOTH add and sub).
_EDGE_A = [0.0, 0.3, -0.3, 0.49, -0.49, 0.25, -0.4, 0.1, -0.1, 0.45]
_EDGE_B = [0.49, 0.3, 0.3, 0.49, -0.49, -0.25, 0.4, -0.45, 0.45, -0.45]
EDGE = [complex(a, b) for a, b in zip(_EDGE_A, _EDGE_B)]


def _random(seed, n=24, amp=0.45):
    rng = random.Random(seed)
    return [complex(rng.uniform(-amp, amp), rng.uniform(-amp, amp))
            for _ in range(n)]


def _run_dut(block_type, stim):
    dut = run_block_dut_complex(
        block_type, stim, chip_yaml=CHIP_YAML,
        in_ports=("a0", "a1"), words_per_sample=1)
    assert dut.ok, dut.reason
    return dut


def _gr(gr_factory, stim):
    return run_gnuradio_ref_complex(
        stim,
        gnuradio_script=f"""
from gnuradio import gr, blocks
tb = gr.top_block()
sa = blocks.vector_source_f(input_i, False)
sb = blocks.vector_source_f(input_q, False)
op = {gr_factory}
snk = blocks.vector_sink_f()
tb.connect(sa, (op, 0)); tb.connect(sb, (op, 1)); tb.connect(op, snk)
tb.run()
output_float = list(snk.data())
""")


def _compare(dut, gr):
    # one ADD/SUB, memoryless: op_count=1, delay=0.
    return compare_against_grc(dut.i_q15, gr.i, metric=Metric.AMPLITUDE,
                               delay=0, op_count=1)


# --- structure / smoke --------------------------------------------------------

@pytest.mark.parametrize("variant", ["add", "sub"])
def test_drives_and_captures(variant):
    block_type, _, _ = _VARIANTS[variant]
    dut = _run_dut(block_type, _random(1, 12))
    assert dut.words_per_sample == 1
    assert dut.in_regs == (0, 1), "the two streams should land a@R0, b@R1"
    assert all(v is not None for v in dut.i_q15)


# --- DSP equivalence vs GNU Radio ---------------------------------------------

@pytest.mark.parametrize("variant", ["add", "sub"])
def test_edge_vectors(variant):
    block_type, gr_factory, _ = _VARIANTS[variant]
    dut = _run_dut(block_type, EDGE)
    gr = _gr(gr_factory, EDGE)
    res = _compare(dut, gr)
    print(f"\n{variant} edge:", res.summary())
    assert res.passed, res.summary()


@pytest.mark.parametrize("variant", ["add", "sub"])
@pytest.mark.parametrize("seed", [1, 7, 42])
def test_random_vectors(variant, seed):
    block_type, gr_factory, _ = _VARIANTS[variant]
    stim = _random(seed)
    dut = _run_dut(block_type, stim)
    gr = _gr(gr_factory, stim)
    res = _compare(dut, gr)
    print(f"\n{variant} random seed={seed}:", res.summary())
    assert res.passed, res.summary()


@pytest.mark.parametrize("variant", ["add", "sub"])
@pytest.mark.parametrize("amp", [0.1, 0.25, 0.4, 0.49])
def test_amplitude_sweep(variant, amp):
    """Parity across amplitude regimes (the param-sweep analogue), kept in range."""
    block_type, gr_factory, _ = _VARIANTS[variant]
    stim = _random(99, n=24, amp=amp)
    dut = _run_dut(block_type, stim)
    gr = _gr(gr_factory, stim)
    res = _compare(dut, gr)
    print(f"\n{variant} amp={amp}:", res.summary())
    assert res.passed, res.summary()


# --- bit-exact substrate ------------------------------------------------------

@pytest.mark.parametrize("variant", ["add", "sub"])
@pytest.mark.parametrize("seed", [3, 17, 256])
def test_bitexact_reference(variant, seed):
    """DUT matches the SATURATING Q15 reference EXACTLY over a long stream that
    INCLUDES out-of-range sums (amp 0.9 → frequent saturation)."""
    block_type, _, cls = _VARIANTS[variant]
    stim = _random(seed, n=80, amp=0.9)
    dut = _run_dut(block_type, stim)
    blk = cls("ref")
    a = [_q15(c.real) for c in stim]
    b = [_q15(c.imag) for c in stim]
    ref = blk.process_reference_q15(a, b)
    res = compare_against_grc(dut.i_q15, [_s16(r) / 32768.0 for r in ref],
                              metric=Metric.EXACT, delay=0)
    print(f"\n{variant} bit-exact seed={seed}:", res.summary())
    assert res.passed, res.summary()


@pytest.mark.parametrize("variant,corner,rail", [
    ("add", complex(0.9, 0.9), 32767),     # 1.8  -> +full
    ("add", complex(-0.9, -0.9), -32768),  # -1.8 -> -full
    ("sub", complex(0.9, -0.9), 32767),    # 0.9-(-0.9)=1.8 -> +full
    ("sub", complex(-0.9, 0.9), -32768),   # -0.9-0.9=-1.8  -> -full
])
def test_saturates_not_wraps(variant, corner, rail):
    """An out-of-range result must PIN to ±full-scale (no wrap / sign flip)."""
    block_type, _, cls = _VARIANTS[variant]
    stim = [complex(0.1, 0.1), corner, complex(-0.2, 0.2), corner]
    dut = _run_dut(block_type, stim)
    assert _s16(dut.i_q15[1]) == rail and _s16(dut.i_q15[3]) == rail, \
        f"{variant} must saturate to {rail}, got {_s16(dut.i_q15[1])}"


# --- num_inputs parity: GR add_xx/sub_xx expose num_inputs (N streams) ---------
# add sums all N; sub computes a0 - a1 - … - a(N-1). The block mirrors num_inputs
# (chained saturating ADD/SUB). N>2 fan-in needs the N-operand driver.

def _nstreams(seed, n, *, amp=0.45, ns=20):
    rng = random.Random(seed)
    return [[rng.uniform(-amp, amp) for _ in range(ns)] for _ in range(n)]


def _gr_nstream(grblk, streams):
    """GR golden: add_ff/sub_ff with N connected input ports (the GRC num_inputs)."""
    import json
    import subprocess
    script = f"""
from gnuradio import gr, blocks
import json, sys
st = json.loads(sys.stdin.read())
tb = gr.top_block(); op = blocks.{grblk}()
srcs = [blocks.vector_source_f(list(s), False) for s in st]
snk = blocks.vector_sink_f()
for i, s in enumerate(srcs):
    tb.connect(s, (op, i))
tb.connect(op, snk); tb.run()
print(json.dumps(list(snk.data())))
"""
    gr_py = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")
    r = subprocess.run([gr_py, "-c", script], input=json.dumps(streams),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-500:]
    return json.loads(r.stdout.strip().splitlines()[-1])


import json  # noqa: E402

_N_VARIANTS = {
    "add": ("AddBlock", "add_ff", AddBlock),
    "sub": ("SubtractBlock", "sub_ff", SubtractBlock),
}


@pytest.mark.parametrize("variant", ["add", "sub"])
@pytest.mark.parametrize("n", [3, 4])
def test_num_inputs_vs_gr(variant, n):
    """N-stream add/sub matches GR (N connected ports) within the floor.

    The GR-equivalence stimulus stays IN-RANGE: amplitude is scaled by 1/N so the
    N-way running sum/difference stays inside [-1, 1) where the block SATURATES =
    the float result (out of range the block clamps where GR float keeps growing —
    that production saturation is gated by the bit-exact reference test below)."""
    block_type, grblk, _ = _N_VARIANTS[variant]
    streams = _nstreams(5, n, amp=0.9 / n)
    in_ports = tuple(f"a{i}" for i in range(n))
    dut = run_block_dut_nstream(
        block_type, streams, params={"num_inputs": n},
        chip_yaml=CHIP_YAML, in_ports=in_ports)
    assert dut.ok, dut.reason
    gr = _gr_nstream(grblk, streams)
    got = [_s16(w) / 32768.0 for w in dut.outputs_q15]
    max_err = max(abs(g - r) for g, r in zip(got, gr))
    print(f"\n{variant} N={n}: max|dut-GR| = {max_err * 32768:.2f} LSB")
    assert max_err * 32768 <= 2.0, f"{max_err * 32768:.2f} LSB too high"


@pytest.mark.parametrize("variant", ["add", "sub"])
@pytest.mark.parametrize("n", [3, 4])
def test_num_inputs_bitexact(variant, n):
    """N-stream DUT matches its chained-saturating Q15 reference EXACTLY."""
    block_type, _, cls = _N_VARIANTS[variant]
    streams = _nstreams(9, n)
    in_ports = tuple(f"a{i}" for i in range(n))
    dut = run_block_dut_nstream(
        block_type, streams, params={"num_inputs": n},
        chip_yaml=CHIP_YAML, in_ports=in_ports)
    assert dut.ok, dut.reason
    ref = cls("r", num_inputs=n).process_reference_q15(
        *[[_q15(x) for x in s] for s in streams])
    assert [_s16(w) for w in dut.outputs_q15] == [_s16(r) for r in ref], \
        "N-stream chained add/sub must match the Q15 reference exactly"


@pytest.mark.parametrize("variant", ["add", "sub"])
def test_num_inputs_saturates(variant):
    """A 3-stream sum/difference that overflows must SATURATE (no wrap)."""
    block_type, _, cls = _N_VARIANTS[variant]
    # add: 0.5+0.5+0.5 = 1.5 -> +full;  sub: -0.5-0.5-0.5 = -1.5 -> -full.
    if variant == "add":
        streams = [[0.5], [0.5], [0.5]]
        rail = 32767
    else:
        streams = [[-0.5], [0.5], [0.5]]
        rail = -32768
    dut = run_block_dut_nstream(
        block_type, streams, params={"num_inputs": 3},
        chip_yaml=CHIP_YAML, in_ports=("a0", "a1", "a2"))
    assert dut.ok, dut.reason
    assert _s16(dut.outputs_q15[0]) == rail, \
        f"{variant} N=3 overflow must saturate to {rail}, got {_s16(dut.outputs_q15[0])}"


@pytest.mark.parametrize("variant", ["add", "sub"])
def test_num_inputs_over_limit_raises(variant):
    """num_inputs above the cell budget MUST raise (loud HW limit)."""
    _, _, cls = _N_VARIANTS[variant]
    with pytest.raises(ValueError, match="HARDWARE LIMIT"):
        cls("x", num_inputs=cls.MAX_INPUTS + 1)
    with pytest.raises(ValueError):
        cls("x", num_inputs=1)


# --- MANDATORY mutation tests -------------------------------------------------

def _setup(variant):
    block_type, gr_factory, _ = _VARIANTS[variant]
    stim = _random(7, 32)
    dut = _run_dut(block_type, stim)
    gr = _gr(gr_factory, stim)
    return dut, gr, stim, gr_factory


@pytest.mark.parametrize("variant", ["add", "sub"])
def test_mutation_inverted_output_fails(variant):
    dut, gr, _, _ = _setup(variant)
    mutated = [(0x10000 - (w or 0)) & 0xFFFF for w in dut.i_q15]
    res = compare_against_grc(mutated, gr.i, metric=Metric.AMPLITUDE,
                              delay=0, op_count=1)
    assert not res.passed, "gate failed to detect an inverted output!"


@pytest.mark.parametrize("variant", ["add", "sub"])
def test_mutation_wrong_second_stream_fails(variant):
    block_type, gr_factory, _ = _VARIANTS[variant]
    stim = _random(7, 32)
    dut = _run_dut(block_type, stim)
    other = _random(8, 32)
    wrong = [complex(s.real, o.imag) for s, o in zip(stim, other)]
    gr_wrong = _gr(gr_factory, wrong)
    res = compare_against_grc(dut.i_q15, gr_wrong.i, metric=Metric.AMPLITUDE,
                              delay=0, op_count=1)
    assert not res.passed, "gate failed to detect a wrong second stream!"


def test_subtract_mutation_swapped_streams_fails():
    """Subtract is NOT commutative: a-b ≠ b-a, so a swapped-stream DUT must FAIL.
    (Add is commutative — no such mutation; documented in the module.)"""
    block_type, gr_factory, _ = _VARIANTS["sub"]
    # build the DUT on (a,b) but compare to GR on (b,a) — a real corruption.
    stim = _random(11, 32)
    swapped = [complex(c.imag, c.real) for c in stim]
    dut = _run_dut(block_type, stim)
    gr_swapped = _gr(gr_factory, swapped)
    res = compare_against_grc(dut.i_q15, gr_swapped.i, metric=Metric.AMPLITUDE,
                              delay=0, op_count=1)
    assert not res.passed, "gate failed to detect swapped subtract operands!"


@pytest.mark.parametrize("variant", ["add", "sub"])
def test_mutation_one_sample_offset_fails(variant):
    dut, gr, _, _ = _setup(variant)
    shifted = [0x0000] + list(dut.i_q15[:-1])
    res = compare_against_grc(shifted, gr.i, metric=Metric.AMPLITUDE,
                              delay=0, op_count=1)
    assert not res.passed, "gate failed to detect a 1-sample latency error!"


@pytest.mark.parametrize("variant", ["add", "sub"])
def test_empty_output_fails(variant):
    _, gr, _, _ = _setup(variant)
    res = compare_against_grc([], gr.i, metric=Metric.AMPLITUDE,
                              delay=0, op_count=1)
    assert not res.passed


# --- dashboard reports --------------------------------------------------------

@pytest.mark.parametrize("variant", ["add", "sub"])
def test_emit_report(variant):
    block_type, gr_factory, _ = _VARIANTS[variant]
    dut = _run_dut(block_type, EDGE)
    gr = _gr(gr_factory, EDGE)
    res = _compare(dut, gr)
    assert res.passed, res.summary()
    write_report(block_type, res, coverage={
        "edge": True, "random": 3, "amplitude_sweep": 4, "bit_exact": True,
        "saturation": True, "mutation": True})
