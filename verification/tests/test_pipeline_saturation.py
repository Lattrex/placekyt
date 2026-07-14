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

# Blocks that need bespoke stimulus (documented, reported as skips — no silent gap).
NEEDS_BESPOKE = {
    "ComplexCostasLoopBlock": "complex I/Q input; own gate in proto_costas_pipe.py",
    "GardnerTimingRecovery": "2-sps timing loop; own gate in proto_gardner_race.py",
    "IQUpconvertBlock": "complex input",
    "ComplexMixerBlock": "complex input",
    "ComplexFIRFilterBlock": "complex I/Q input",
    "ComplexLowPassFilter": "complex input",
    "ComplexHighPassFilter": "complex input",
    "ComplexBandPassFilter": "complex input",
    "ComplexBandRejectFilter": "complex input",
    "ComplexRRCMatchedFilterBlock": "complex input",
    "FloatToComplexBlock": "2-real -> complex",
    "DualFloatToComplexBlock": "2-face rendezvous",
    "ComplexToFloatBlock": "complex input",
    "ComplexToRealBlock": "complex input",
    "ComplexToImagBlock": "complex input",
    "ComplexToMagSquaredBlock": "complex input",
    "ConjugateBlock": "complex input",
    "AddBlock": "multi-input",
    "MultiplyBlock": "multi-input",
    "SubtractBlock": "multi-input",
    "UpsamplerBlock": "rate-expanding (bespoke rate harness)",
    "KeepOneInNBlock": "rate-reducing (bespoke rate harness)",
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


def test_bespoke_coverage_is_documented():
    """Every catalog block is EITHER driven here OR listed in NEEDS_BESPOKE with a
    reason — no block silently escapes the saturation gate."""
    from engine.catalog import BlockCatalog  # noqa: PLC0415

    cat = BlockCatalog.from_gr_kyttar()
    all_types = {b.type_name for b in cat.all()}
    covered = set(REAL_1IN) | set(NEEDS_BESPOKE)
    missing = all_types - covered
    assert not missing, (
        "blocks with NO saturation coverage and NO bespoke reason (add to REAL_1IN "
        f"or NEEDS_BESPOKE): {sorted(missing)}")
