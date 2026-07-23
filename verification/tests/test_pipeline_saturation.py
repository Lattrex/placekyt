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
    # Bit-stream / LLR single-in single-out blocks (proven saturated 2026-07-14).
    "LFSRScramblerBlock": ("sample", "out", {}),
    "SoftDemodulatorBlock": ("sample", "llr", {}),
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
    # bit-stream -> symbols (emits every N bits) — proven saturated 2026-07-14.
    "PSKSymbolMapperBlock": ("sample", "out_i", {}),
    # M17 4FSK blocks (2026-07-21): 4FSK symbol MAPPER packs 2 bits/symbol (2:1
    # rate-REDUCING — a symbol emerges every 2 input bits); 4FSK SLICER emits 2 bits
    # per symbol (1:2 rate-EXPANDING). Both are memoryless per (di)bit and saturation-
    # safe — the saturated flat stream equals the per-sample flat stream.
    "FSK4SymbolMapperBlock": ("sample", "out", {}),
    "FSK4SlicerBlock": ("sample", "out", {}),
}

# COMPLEX-in / COMPLEX-out (yi,yq 2-word egress): (in_ports, out_port, params). Same
# drive as REAL_2IN but the per-sample reference AND the saturated stream are the FULL
# interleaved yi/yq. ComplexMixer carries its OWN serialize-LOCK interlock (the dual-FACE
# unlock folded into the mixer), so this is a strong saturation cross-check that the lock
# releases cleanly under back-to-back drive without a phantom re-trigger deadlock.
COMPLEX_2IN2OUT = {
    # pipeline_lock=True selects the saturation-safe (serialize-LOCK) variant. It is
    # NOT the shipping default (that stays False so the compact SSB Weaver + the GRC
    # importer/net-resolution keep the 11-cell footprint); this gate proves the LOCKED
    # path is bit-exact under saturation so consumers can opt in for high-rate pipelines.
    "ComplexMixerBlock": (("xi", "xq"), "yi", {"pipeline_lock": True}),
    # NCO: complex TRIGGER in (xi,xq ignored) / complex cos+j·sin out. Same phase->2-arm
    # ->emit reconvergent fan-in as ComplexMixer; the pipeline_lock=True serialize-LOCK
    # (relay arm-serializer + emit dual-FACE unlock) makes it saturation-safe (INV-20).
    "NCOBlock": (("xi", "xq"), "yi", {"pipeline_lock": True}),
    # FEED-FORWARD complex-in / complex-out blocks — proven bit-exact saturated 2026-07-14
    # (they were in NEEDS_BESPOKE only because the gate reads ONE out port, not the yi/yq
    # 2 rails; run_block_dut_complex + run_block_dut_pipelined both drain the interleaved
    # pair). No block changes were needed — pure harness coverage.
    "FloatToComplexBlock": (("re", "im"), "out_re", {}),
    "ComplexToFloatBlock": (("re", "im"), "out_re", {}),
    "ConjugateBlock": (("re", "im"), "out_re", {}),
    "ComplexRRCMatchedFilterBlock": (("xi", "xq"), "yi", {}),
    "ComplexLowPassFilter": (("xi", "xq"), "out_i",
                             {"gain": 0.9, "samp_rate": 32000.0,
                              "cutoff_freq": 1200.0, "transition_width": 2500.0}),
    # generic ComplexFIR with a small-gain tap set (Σ|h|<=1 fits the multi-cell build);
    # this is the shared datapath for the whole Complex{Low,High,Band}Pass/Reject family,
    # so proving it saturates covers the family (the firdes high/band variants only fail
    # to INSTANTIATE at high gain — a Σ|h|<=1 build constraint, not a saturation defect).
    "ComplexFIRFilterBlock": (("xi", "xq"), "yi",
                              {"coefficients": [0.2, 0.3, 0.2, 0.1, 0.05]}),
}

# Blocks that need bespoke stimulus (documented, reported as skips — no silent gap).
NEEDS_BESPOKE = {
    "ComplexCostasLoopBlock": "complex I/Q loop; own gate proto_costas_pipe.py (BER0)",
    "CoherentRXBlock": "complex I/Q RX loop; own gate proto_rx_bisect.py (BER0)",
    "GardnerTimingRecovery": "2-sps timing loop; own gate proto_gardner_race.py",
    # High/Band firdes variants only fail to INSTANTIATE at high gain (Σ|h|>1 build
    # constraint); their datapath == ComplexFIR/LowPass which ARE gated saturated above.
    "ComplexHighPassFilter": "Σ|h|>1 firdes build constraint; datapath == ComplexLowPass (gated)",
    "ComplexBandPassFilter": "Σ|h|>1 firdes build constraint; datapath == ComplexLowPass (gated)",
    "ComplexBandRejectFilter": "Σ|h|>1 firdes build constraint; datapath == ComplexLowPass (gated)",
    "DualFloatToComplexBlock": "2-face rendezvous (own gate proto_dual_f2c)",
    # INV-20 fan-in FIXED (2026-07-21): NCO + FrequencyModulator had the SAME phase->2-arm
    # ->emit reconvergent fan-in as the pre-fix ComplexMixer. The pipeline_lock=True
    # serialize-LOCK (relay arm-serializer + emit dual-FACE unlock + sign-inline interp)
    # makes them saturation-safe. NCOBlock is now gated in COMPLEX_2IN2OUT above.
    # FrequencyModulator has a REAL input (x) / complex out — the COMPLEX_2IN2OUT harness
    # drives 2 in-ports, so FM has its OWN saturated gate (test_fm_saturation_safe below);
    # its datapath is NCO's (verified in COMPLEX_2IN2OUT) with only the phase cell changed.
    "FrequencyModulatorBlock": "real-in/complex-out; own gate test_fm_saturation_safe (locked, bit-exact)",
    "ComplexUpsamplerBlock": "rate-EXPANDING complex (2-rail zero-stuff), no complex-rate harness; MEMORYLESS (no feedback/state carried across samples) so per-sample == saturated by construction — cf. UpsamplerBlock (RATE_1IN, gated)",
    "BPSKSlicerBlock": "packed-bit output timing",
    "SoftDemodulatorBlock": "complex input",
    "LFSRScramblerBlock": "bit-stream input",
    "BandPassFilter": "band filter — covered by FIR family",
    "BandRejectFilter": "band filter — covered by FIR family",
    # M17 4FSK sync-timing recovery: correlates the LSF sync word then GATE-decimates
    # 2:1 — its output is CONDITIONAL on sync detection, so a flat random stimulus emits
    # nothing (no lock). Needs a FRAMED (preamble+sync+payload) burst; driven saturated
    # in its own gate (proto_fsk4_sync_model / the fsk4 modem RX BER harness), which
    # proves the whole RX chain recovers BER 0 pipelined (the real saturation proof).
    "FSK4SyncTimingRecoveryBlock": "sync-gated decimator; needs framed burst — own gate (fsk4 RX BER0 pipelined)",
    # 16-QAM modem blocks. The mapper is bit-in / COMPLEX-egress (I,Q pair per symbol)
    # and the slicer is COMPLEX-in (I,Q) / 4-bit-symbol-out — neither fits the single-
    # rail RATE_1IN / REAL_1IN harnesses. The Costas is a data-FEEDBACK loop (like
    # ComplexCostasLoopBlock above). All three are driven SATURATED end-to-end by the
    # qam16_modem BER-0 acceptance test (placekyt/tests/test_qam16_modem_ber.py): the
    # random 16-QAM burst streams through Costas->slicer with no inter-sample quiescence
    # and recovers BER 0 — the real saturation proof for the whole chain.
    "QAM16SymbolMapperBlock": "bit-in/complex-egress mapper; saturated in the qam16_modem RX BER0 acceptance (test_qam16_modem_ber)",
    "QAM16SlicerBlock": "complex-in/4-bit-out slicer; saturated in the qam16_modem RX BER0 acceptance (test_qam16_modem_ber)",
    "QAM16ComplexCostasLoopBlock": "complex I/Q DD carrier loop; saturated in the qam16_modem RX BER0 acceptance (test_qam16_modem_ber)",
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


def _drive_fm_saturated(sens: float, xs_q15):
    """Build FM (locked) x16_in -> FM(yi,yq) -> x16_out, drive the whole burst
    SATURATED via queue_words_physical, and return the interleaved [yi,yq,...] signed
    egress. Driven directly (not through run_block_dut_pipelined) so the complex 2-rail
    yi/yq egress is drained in emit order without the single-out-port harness's pairing
    assumptions."""
    import numpy as np  # noqa: PLC0415
    import simkyt  # noqa: PLC0415
    from PySide6.QtWidgets import QApplication  # noqa: PLC0415
    from engine.catalog import BlockCatalog  # noqa: PLC0415
    from engine.io.chip_type_io import load_chip_type  # noqa: PLC0415
    from engine.build import BuildEngine  # noqa: PLC0415
    from engine.registry import ChipTypeRegistry  # noqa: PLC0415
    from engine.port_config import stream_targets  # noqa: PLC0415
    from ui.controller import AppController  # noqa: PLC0415
    from model.connection import BlockEndpoint, ChipPortEndpoint  # noqa: PLC0415

    QApplication.instance() or QApplication([])
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    key = getattr(ct, "name", None) or "kyttar_10x12"
    ctrl = AppController(catalog=cat)
    ctrl.new_project("fmsat", key)
    fm = ctrl.place_block("FrequencyModulatorBlock", 0, 3, 1,
                          library="lattrex.official",
                          params={"sensitivity": sens, "pipeline_lock": True})
    R = ctrl.add_route
    R(ChipPortEndpoint(chip=0, port="x16_in"), BlockEndpoint(block=fm, port="x"), [])
    R(BlockEndpoint(block=fm, port="yi"), ChipPortEndpoint(chip=0, port="x16_out"), [])
    R(BlockEndpoint(block=fm, port="yq"), ChipPortEndpoint(chip=0, port="x16_out"), [])
    assert ctrl.auto_route_all({key: ct}, auto_orient=True, use_bus="always").ok
    for conn in ctrl.project.connections:
        s = getattr(conn, "source", None)
        if s is not None and getattr(s, "port", None) == "x16_in":
            conn.stream_id = "tx"
    bres = BuildEngine(cat, CHIP_YAML).build(ctrl.project, {key: ct})
    assert bres.ok, getattr(bres, "errors", None)
    reg = ChipTypeRegistry()
    reg.register_file(CHIP_YAML)
    tg = stream_targets(ctrl.project, reg, cat, 0, build_result=bres)["tx"]
    entry, hop, a0 = tg["entry_addr"], tg["hop_count"], tg["data_addrs"][0]

    def _w(a):
        return (0x6 << 12) | ((hop & 0x1F) << 5) | (int(a) & 0x1F)

    def _j():
        return (0x7 << 12) | ((hop & 0x1F) << 5) | (int(entry) & 0x1F)

    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(bres.words(0))
    stream = []
    for w in xs_q15:
        stream += [_w(a0), int(w) & 0xFFFF, _j()]
    chip.queue_words_physical("x16_in", stream)
    chip.run(max_events=max(300000, 20000 * len(stream)))

    def _s16(u):
        u &= 0xFFFF
        return u - 0x10000 if u >= 0x8000 else u
    return [_s16(int(v)) for (v, _d, _t) in chip.read_port_words_timed("x16_out")]


@pytest.mark.skipif(not _CHIP_OK, reason="chip yaml absent")
def test_fm_saturation_safe():
    """FrequencyModulator (REAL input x / complex yi,yq out) under SATURATED drive ==
    its bit-exact ``process_reference_q15`` (the GR-equivalent oracle). FM has a single
    REAL input, so it fits neither the 2-in COMPLEX_2IN2OUT harness nor the single-word
    ``run_block_dut`` complex drain; drive it saturated directly and compare the
    interleaved [yi,yq,...] egress pair-for-pair. The pipeline_lock=True serialize-LOCK
    (INV-20) must deliver EVERY input's I/Q pair 1:1 — without it the reconvergent
    fan-in drops every other sample (measured 352 in -> 176 out)."""
    from gr_kyttar.placement.blocks.frequency_modulator_block import (  # noqa: PLC0415
        FrequencyModulatorBlock)

    sens = 1.5707963267948966
    # Reference takes the ORIGINAL signed floats (NOT _STIM_Q15/32768 — those are
    # UNSIGNED Q15 words, so a negative value would reconstruct as a large positive
    # float and mis-drive the reference). The chip is driven with the same _STIM_Q15
    # words process_reference_q15 re-quantises internally, so they agree bit-for-bit.
    ref = FrequencyModulatorBlock("ref", sensitivity=sens).process_reference_q15(_STIM)

    out = _drive_fm_saturated(sens, _STIM_Q15)
    n = len(ref)
    assert len(out) >= 2 * n, (
        f"FrequencyModulator: saturated produced {len(out)} words for {n} inputs "
        f"(expected {2 * n} = 1 I/Q pair each) — pipeline DROPPED samples "
        f"(serialize-LOCK did not release / fan-in starved)")
    got = [(out[2 * k], out[2 * k + 1]) for k in range(n)]
    ref_s = [(FrequencyModulatorBlock._s16(a), FrequencyModulatorBlock._s16(b))
             for (a, b) in ref]
    bad = [k for k in range(n) if got[k] != ref_s[k]]
    assert not bad, (
        f"FrequencyModulator: saturated output diverges from reference at pair "
        f"{bad[0]}: got {got[bad[0]]}, ref {ref_s[bad[0]]}")


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
