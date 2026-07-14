# SPDX-License-Identifier: GPL-3.0-or-later
"""Pipeline-SATURATION gate for every block: the on-chip output when driven
SATURATED (whole burst enqueued via queue_words_physical, one continuous run,
NO drain between samples) MUST equal the per-sample output (already the
GNU-Radio-verified reference). A block that diverges when the pipeline is full
has a feedback/handshake hazard the per-sample harness cannot see — CM's rule:
EVERY block must work saturated, or it is not right and must be fixed.

This is the general regression net that would have caught the ComplexCostasLoop
dphase-feedback collapse (phase NCO races ahead open-loop under continuous drive
while pd_pi lags) BEFORE it shipped. See ``run_block_dut_pipelined``.

Coverage note (NO silent gaps): this file drives the SINGLE-REAL-INPUT blocks
(the bulk) uniformly. Blocks needing bespoke stimulus — complex-input, multi-
input, rate-changing, or source blocks — are listed in ``NEEDS_BESPOKE`` with a
reason and are reported as skips, NOT silently omitted. ComplexCostasLoopBlock
has its own saturated gate in ``verification/kyttar/tests/proto_costas_pipe.py``.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 \
      <venv>/python -m pytest verification/tests/test_pipeline_saturation.py -q
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PLACEKYT = Path(__file__).resolve().parents[2] / "placekyt"
_VERIFY = Path(__file__).resolve().parents[1]
_RUNTIME = Path(__file__).resolve().parents[2] / "runtime" / "python"
for _p in (str(_PLACEKYT), str(_VERIFY), str(_RUNTIME)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_CHIP_OK = os.path.exists(CHIP_YAML)


def _q15(f: float) -> int:
    q = max(-32768, min(32767, int(round(f * 32768.0))))
    return q & 0xFFFF


# A deterministic small real stimulus (fractional, in [-1,1)) — enough to expose a
# feedback divergence but short enough to keep the per-block build+run cheap.
_STIM = [0.10, -0.20, 0.30, -0.40, 0.55, 0.25, -0.15, 0.35,
         -0.50, 0.45, -0.05, 0.60, -0.35, 0.15, -0.25, 0.05]
_STIM_Q15 = [_q15(v) for v in _STIM]

# Single-real-input, single-real-output blocks driven uniformly: (in_port, out_port,
# params). These are the ones ``run_block_dut`` already verifies per-sample.
REAL_1IN = {
    "GainBlock": ("sample", "out", {"gain": 0.5}),
    "AbsBlock": ("sample", "out", {}),
    "DCBlockerBlock": ("in", "out", {}),
    "MovingAverageBlock": ("in", "out", {}),
    "AGCBlock": ("in", "out", {}),
    "SquelchBlock": ("in", "out", {}),
    "IIRBiquadBlock": ("in", "out", {}),
    # raw FIR needs explicit taps (the LowPass/HighPass/RRC variants supply their own).
    "FIRFilterBlock": ("in", "out", {"coefficients": [0.25, 0.5, 0.25]}),
    "LowPassFilter": ("in", "out", {}),
    "HighPassFilter": ("in", "out", {}),
    "RRCPulseShaperBlock": ("in", "out", {}),
}

# TWO-input, single-real-output blocks: (in_ports, out_port, params). Driven with a
# 2-operand sample. Per-sample reference = run_block_dut_complex (drains flat);
# pipelined = run_block_dut_pipelined. Covers the complex->real converters (re,im ->
# scalar) and the N-input arithmetic (a0,a1 -> out).
REAL_2IN = {
    "ComplexToRealBlock": (("re", "im"), "out", {}),
    "ComplexToImagBlock": (("re", "im"), "out", {}),
    "ComplexToMagSquaredBlock": (("re", "im"), "out", {}),
    "AddBlock": (("a0", "a1"), "out", {}),
    "MultiplyBlock": (("a0", "a1"), "out", {}),
    "SubtractBlock": (("a0", "a1"), "out", {}),
    # IQUpconvert is complex-in / single-real-out AND carries its OWN LOCK interlock
    # (the same idiom the Costas/RX fix uses) — the strongest saturation cross-check.
    "IQUpconvertBlock": (("xi", "xq"), "out", {}),
}

# RATE-CHANGING blocks (single real in, single real out, N-in-M-out): (in_port,
# out_port, params). Per-sample ref = run_block_dut_rate (drains each trigger's burst);
# pipelined = run_block_dut_pipelined (flat egress). The saturated flat stream must
# equal the per-sample flat stream — a rate block that mis-paces under saturation
# (drops/duplicates a burst) is caught here.
RATE_1IN = {
    "UpsamplerBlock": ("x", "out", {}),        # rate-EXPANDING (fixed factor)
    "KeepOneInNBlock": ("x", "out", {"n": 2}),  # rate-REDUCING
}

# COMPLEX-in / COMPLEX-out (yi,yq 2-word egress): (in_ports, out_port, params). Same
# drive as REAL_2IN but the per-sample reference AND the saturated stream are the FULL
# interleaved yi/yq. ComplexMixer carries its OWN serialize-LOCK interlock (the dual-FACE
# unlock folded into the mixer), so this is a strong saturation cross-check that the lock
# releases cleanly under back-to-back drive without a phantom re-trigger deadlock.
COMPLEX_2IN2OUT = {
    "ComplexMixerBlock": (("xi", "xq"), "yi", {}),
}

# Blocks that need bespoke stimulus (documented, reported as skips — no silent gap).
NEEDS_BESPOKE = {
    "ComplexCostasLoopBlock": "complex I/Q loop; own gate proto_costas_pipe.py (BER0)",
    "CoherentRXBlock": "complex I/Q RX loop; own gate proto_rx_bisect.py (BER0)",
    "GardnerTimingRecovery": "2-sps timing loop; own gate proto_gardner_race.py",
    "ComplexFIRFilterBlock": "complex I/Q output",
    "ComplexLowPassFilter": "complex I/Q output",
    "ComplexHighPassFilter": "complex I/Q output",
    "ComplexBandPassFilter": "complex I/Q output",
    "ComplexBandRejectFilter": "complex I/Q output",
    "ComplexRRCMatchedFilterBlock": "complex I/Q output",
    "FloatToComplexBlock": "complex I/Q output",
    "DualFloatToComplexBlock": "2-face rendezvous (own gate proto_dual_f2c)",
    "ComplexToFloatBlock": "complex I/Q output (out_re,out_im)",
    "ConjugateBlock": "complex I/Q output (out_re,out_im)",
    "NCOBlock": "source (no data input)",
    "FrequencyModulatorBlock": "VCO / complex output",
    "PSKSymbolMapperBlock": "bit-stream input",
    "BPSKSlicerBlock": "packed-bit output timing",
    "SoftDemodulatorBlock": "complex input",
    "LFSRScramblerBlock": "bit-stream input",
    "BandPassFilter": "band filter — covered by FIR family",
    "BandRejectFilter": "band filter — covered by FIR family",
}


@pytest.mark.skipif(not _CHIP_OK, reason="chip yaml absent")
@pytest.mark.parametrize("block_type", sorted(REAL_1IN))
def test_pipelined_equals_per_sample(block_type):
    """Saturated on-chip output == per-sample on-chip output (the GR-verified ref)."""
    from kyttar_verify.dut_runner import (  # noqa: PLC0415
        run_block_dut, run_block_dut_pipelined)

    in_port, out_port, params = REAL_1IN[block_type]

    seq = run_block_dut(block_type, _STIM_Q15, params=params, chip_yaml=CHIP_YAML,
                        in_port=in_port, out_port=out_port)
    assert seq.ok, f"per-sample build/run failed: {seq.reason}"
    seq_out = [x for x in seq.outputs_q15 if x is not None]

    pipe = run_block_dut_pipelined(block_type, [(w,) for w in _STIM_Q15],
                                   params=params, chip_yaml=CHIP_YAML,
                                   in_ports=(in_port,), out_port=out_port)
    assert pipe.ok, f"pipelined build/run failed: {pipe.reason}"

    # The saturated egress must reproduce the per-sample stream (prefix-compare — a
    # feedback-collapsing block emits far FEWER words, which this catches loudly).
    n = len(seq_out)
    assert len(pipe.outputs_q15) >= n, (
        f"{block_type}: saturated produced {len(pipe.outputs_q15)} words, "
        f"per-sample produced {n} — pipeline STALLED (feedback/handshake hazard)")
    assert pipe.outputs_q15[:n] == seq_out, (
        f"{block_type}: saturated output diverges from per-sample at index "
        f"{next(i for i in range(n) if pipe.outputs_q15[i] != seq_out[i])}")


@pytest.mark.skipif(not _CHIP_OK, reason="chip yaml absent")
@pytest.mark.parametrize("block_type", sorted(REAL_2IN))
def test_pipelined_equals_per_sample_2in(block_type):
    """Same saturation gate for TWO-input, single-real-output blocks (complex->real
    converters + N-input arithmetic). Per-sample ref = run_block_dut_complex (drives
    2 operands + JUMP per sample, drains flat); pipelined = run_block_dut_pipelined."""
    from kyttar_verify.dut_runner import (  # noqa: PLC0415
        run_block_dut_complex, run_block_dut_pipelined)

    in_ports, out_port, params = REAL_2IN[block_type]

    # Two-operand stimulus: reuse _STIM for BOTH operands (deterministic, exercises
    # sign/magnitude). run_block_dut_complex takes (i,q) pairs.
    pairs = [(_STIM[k], _STIM[(k + 3) % len(_STIM)]) for k in range(len(_STIM))]

    seq = run_block_dut_complex(block_type, pairs, params=params, chip_yaml=CHIP_YAML,
                                in_ports=in_ports, out_port=out_port)
    assert seq.ok, f"per-sample build/run failed: {seq.reason}"
    # flatten the per-trigger word lists to a single stream (1 word/sample here)
    seq_out = [w for g in seq.outputs_q15 for w in g]

    samples = [(_q15(i), _q15(q)) for (i, q) in pairs]
    pipe = run_block_dut_pipelined(block_type, samples, params=params,
                                   chip_yaml=CHIP_YAML, in_ports=in_ports,
                                   out_port=out_port)
    assert pipe.ok, f"pipelined build/run failed: {pipe.reason}"

    n = len(seq_out)
    assert len(pipe.outputs_q15) >= n, (
        f"{block_type}: saturated produced {len(pipe.outputs_q15)} words, per-sample "
        f"produced {n} — pipeline STALLED (feedback/handshake hazard)")
    assert pipe.outputs_q15[:n] == seq_out, (
        f"{block_type}: saturated output diverges from per-sample at index "
        f"{next(i for i in range(n) if pipe.outputs_q15[i] != seq_out[i])}")


@pytest.mark.skipif(not _CHIP_OK, reason="chip yaml absent")
@pytest.mark.parametrize("block_type", sorted(COMPLEX_2IN2OUT))
def test_pipelined_equals_per_sample_complex(block_type):
    """Saturation gate for COMPLEX-in / COMPLEX-out (yi,yq) blocks. The per-sample
    reference AND the saturated stream are the FULL interleaved [yi,yq,...] egress; a
    block whose serialize-LOCK fails to release under back-to-back drive (phantom
    re-trigger, corridor loop) either STALLS or diverges — caught bit-exact here. This
    is the ComplexMixer's own gate now that its dual-FACE unlock is proven."""
    from kyttar_verify.dut_runner import (  # noqa: PLC0415
        run_block_dut_complex, run_block_dut_pipelined)

    in_ports, out_port, params = COMPLEX_2IN2OUT[block_type]
    pairs = [(_STIM[k], _STIM[(k + 3) % len(_STIM)]) for k in range(len(_STIM))]

    # Per-sample reference (takes FLOAT pairs); flatten the per-trigger [yi,yq] lists.
    seq = run_block_dut_complex(block_type, pairs, params=params, chip_yaml=CHIP_YAML,
                                in_ports=in_ports, out_port=out_port)
    assert seq.ok, f"per-sample build/run failed: {seq.reason}"
    seq_out = [w for g in seq.outputs_q15 for w in g]

    # Saturated drive (takes PRE-QUANTIZED uint16 pairs).
    samples = [(_q15(i), _q15(q)) for (i, q) in pairs]
    pipe = run_block_dut_pipelined(block_type, samples, params=params,
                                   chip_yaml=CHIP_YAML, in_ports=in_ports,
                                   out_port=out_port)
    assert pipe.ok, f"pipelined build/run failed (deadlock/livelock?): {pipe.reason}"

    n = len(seq_out)
    assert len(pipe.outputs_q15) >= n, (
        f"{block_type}: saturated produced {len(pipe.outputs_q15)} words, per-sample "
        f"produced {n} — pipeline STALLED (serialize-LOCK did not release)")
    assert pipe.outputs_q15[:n] == seq_out, (
        f"{block_type}: saturated output diverges from per-sample at index "
        f"{next(i for i in range(n) if pipe.outputs_q15[i] != seq_out[i])}")


@pytest.mark.skipif(not _CHIP_OK, reason="chip yaml absent")
@pytest.mark.parametrize("block_type", sorted(RATE_1IN))
def test_pipelined_equals_per_sample_rate(block_type):
    """Saturation gate for RATE-CHANGING blocks. Per-sample ref = run_block_dut_rate
    (drains each trigger's output burst); pipelined = run_block_dut_pipelined. The flat
    saturated stream must equal the flat per-sample stream."""
    from kyttar_verify.dut_runner import (  # noqa: PLC0415
        run_block_dut_rate, run_block_dut_pipelined)

    in_port, out_port, params = RATE_1IN[block_type]

    seq = run_block_dut_rate(block_type, _STIM_Q15, params=params, chip_yaml=CHIP_YAML,
                             in_port=in_port, out_port=out_port)
    assert seq.ok, f"per-sample build/run failed: {seq.reason}"
    seq_out = list(seq.outputs_q15)  # already the flat stream

    pipe = run_block_dut_pipelined(block_type, [(w,) for w in _STIM_Q15],
                                   params=params, chip_yaml=CHIP_YAML,
                                   in_ports=(in_port,), out_port=out_port)
    assert pipe.ok, f"pipelined build/run failed: {pipe.reason}"

    n = len(seq_out)
    assert len(pipe.outputs_q15) >= n, (
        f"{block_type}: saturated produced {len(pipe.outputs_q15)} words, per-sample "
        f"produced {n} — pipeline STALLED / mis-paced (rate hazard)")
    assert pipe.outputs_q15[:n] == seq_out, (
        f"{block_type}: saturated output diverges from per-sample at index "
        f"{next(i for i in range(n) if pipe.outputs_q15[i] != seq_out[i])}")


def test_bespoke_coverage_is_documented():
    """Every catalog block is EITHER driven here OR listed in NEEDS_BESPOKE with a
    reason — no block silently escapes the saturation gate."""
    from engine.catalog import BlockCatalog  # noqa: PLC0415

    cat = BlockCatalog.from_gr_kyttar()
    all_types = {b.type_name for b in cat.all()}
    covered = (set(REAL_1IN) | set(REAL_2IN) | set(RATE_1IN)
               | set(COMPLEX_2IN2OUT) | set(NEEDS_BESPOKE))
    missing = all_types - covered
    assert not missing, (
        "blocks with NO saturation coverage and NO bespoke reason (add to REAL_1IN, "
        f"REAL_2IN, RATE_1IN, or NEEDS_BESPOKE): {sorted(missing)}")
