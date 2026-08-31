# SPDX-License-Identifier: GPL-3.0-or-later
"""Pipeline-SATURATION gate for every block: the on-chip output when driven
SATURATED (whole burst enqueued via queue_words_physical, one continuous run,
NO drain between samples) MUST equal the per-sample output (already the
GNU-Radio-verified reference). A block that diverges when the pipeline is full
has a feedback/handshake hazard the per-sample harness cannot see — the rule:
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
    # Fan-out relay: a stateless single-cell identity (no arithmetic, no
    # feedback) — the saturated stream must equal the per-sample stream.
    "StreamSplitterBlock": ("x", "out", {}),
    "FloatToCharBlock": ("sample", "out", {"scale": 127.0}),
    # Integer-sample delay line: a depth-`delay` shift register. Stateful but with
    # NO feedback corridor and NO reconvergent fan-in (one straight-through cell), so
    # it is saturation-safe by construction — the saturated stream must equal the
    # per-sample stream (INV-19/INV-20 hazards don't apply; no LOCK needed).
    "DelayBlock": ("sample", "out", {"delay": 3}),
    "AbsBlock": ("sample", "out", {}),
    # Q15 activations (shared two-cell fold->lut engine): feed-forward
    # straight chain, memoryless, no feedback corridor / no reconvergent
    # fan-in (INV-19/20 N/A). The lut's runtime patch-slot operand rides the
    # same 4-word delivery packet as the data operands, so the saturated
    # stream must equal the per-sample stream bit-for-bit.
    "SigmoidBlock": ("sample", "out", {"dshift": 0}),
    "TanhBlock": ("sample", "out", {"dshift": 0}),
    "AddConstBlock": ("sample", "out", {"const": 0.3}),
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
    # Rate-REDUCING bit packer (k bits -> 1 byte): single cell with a packing
    # accumulator + bit counter across samples, no feedback loop / no reconvergent
    # fan-in, so saturated egress == per-sample (the None-gap samples are filtered).
    "PackKBitsBlock": ("sample", "out", {"k": 8}),
    # CSS symbol mapper — PackKBits re-parameterized (m = 2^k alphabet, raw
    # 16-bit symbol word out, GR byte cap lifted): the identical single-cell
    # accumulator/counter datapath, so saturated egress == per-sample for the
    # same reason (None-gap samples filtered).
    "ChirpSymbolMapperBlock": ("sample", "out", {"m": 16}),
    # Rate-REDUCING frame CRC-16 (frame_len bytes -> 1 CRC word): single cell with
    # a CRC shift register + frame down-counter across samples, no feedback loop /
    # no reconvergent fan-in, so saturated egress == per-sample (the None-gap
    # samples are filtered; frame_len=4 on the 16-word stimulus emits 4 REAL CRC
    # words — a non-empty, init=0xFFFF-seeded stream, so the drive is proven live).
    "Crc16Block": ("sample", "out", {"frame_len": 4}),
    # CSS preamble sync (K-consecutive-equal-argmax run detector): 1:1 raw
    # index word in -> packed sync word out. Single cell with a previous-index
    # register + saturating run counter, feed-forward, NO feedback corridor /
    # NO reconvergent fan-in (INV-19/20 N/A). NOTE: the shared _STIM has no
    # equal-adjacent pair, so this entry exercises the sentinel/re-arm path
    # only; the LOCK-asserting saturated gate (repeated-index stimulus) is
    # bespoke in test_chirp_sync.py::test_saturated_equals_per_sample_with_real_runs.
    "ChirpSyncBlock": ("idx", "out", {"k": 2}),
    # Differential decoder: symbol-in symbol-out, 1-sample previous-INPUT state.
    # Single cell serializes the state naturally (no cross-cell feedback), so the
    # saturated stream equals the per-sample output with no lock (INV-19 N/A).
    "DiffDecoderBlock": ("sample", "out", {"modulus": 4}),
    # digital.map_bb per-symbol LUT remap (out = map[in]). MEMORYLESS (a single
    # LOAD-indirect table read, no state carried across samples), single real rail
    # in/out, one word per input — saturation-safe by construction; the saturated
    # flat stream must equal the per-sample flat stream.
    "MapBBBlock": ("sample", "out", {"map": [3, 2, 1, 0]}),
    # Bitwise NOT byte op (blocks.not_bb): memoryless single-cell, no state/feedback
    # across samples, so per-sample == saturated by construction.
    "NotBlock": ("sample", "out", {}),
    # Bitwise AND-with-immediate (blocks.and_const_bb): memoryless single-cell mask,
    # no state/feedback, so per-sample == saturated by construction.
    "AndConstBlock": ("sample", "out", {"constant": 0x0F}),
    # char->float type convert (blocks.char_to_float): memoryless single-cell
    # (SHL+MULQ), no state, saturation-safe by construction.
    "CharToFloatBlock": ("sample", "out", {"scale": 128.0}),
    # Differential ENCODER: symbol-in symbol-out, 1-sample OUTPUT-feedback state in a
    # single cell (serializes naturally, no cross-cell feedback corridor), so the
    # saturated stream equals the per-sample output with no LOCK (INV-19 N/A) — the
    # encoder twin of DiffDecoderBlock above.
    "DiffEncoderBlock": ("sample", "out", {"modulus": 2}),
    # nlog10 (blocks.nlog10_ff): 2-cell feed-forward power->dB (mantissa/exp split +
    # cubic), no feedback corridor / no reconvergent fan-in across samples, so the
    # saturated flat stream equals the per-sample flat stream.
    "Nlog10Block": ("sample", "out", {"n": 10.0, "k": 0.0}),
    # rms (blocks.rms_ff): 4-cell feed-forward chain (power+IIR -> normalize ->
    # quartic -> denorm). The IIR state lives INSIDE the head cell (no cross-cell
    # feedback corridor) and the chain is a straight pipeline (no reconvergent
    # fan-in), so back-to-back drive serializes on the link handshakes and the
    # saturated stream must equal the per-sample stream bit-for-bit. The norm/
    # denorm cells have DATA-DEPENDENT run lengths (shift loops) — exactly the
    # kind of jitter only this saturated gate can prove harmless.
    "RMSBlock": ("sample", "out", {"alpha": 0.25}),
    # sqrt (blocks.transcendental 'sqrt'): the RMS family's sqrt TAIL as a
    # standalone block (normalize -> quartic poly -> denorm), 3 cells, straight
    # feed-forward with NO state carried across samples and no reconvergent
    # fan-in. Like RMSBlock it has DATA-DEPENDENT run lengths (the normalize
    # shift-count loop and the denorm SHR-#1 loop), so this saturated gate is
    # exactly what proves that timing jitter harmless under back-to-back drive.
    "SqrtBlock": ("sample", "out", {}),
    # rows x cols BLOCK interleaver: 3-cell STRAIGHT feed-forward pipeline
    # (rgen -> wctl -> store) with a runtime-patched computed-destination store.
    # STATEFUL (2N-word ping-pong buffer + column-walk/ring pointers) but NO
    # feedback corridor and NO reconvergent fan-in (a single linear trigger
    # thread), so the INV-19/20 hazards don't apply — and the store cell
    # consumes ALL its per-sample deliveries (patch slot, R0 sample, read
    # address) BEFORE its potentially-backpressured output WRITE, so a stalled
    # egress cannot be overtaken by the next sample's deliveries. PROBED
    # saturated == per-sample bit-exact across configs incl. the full-depth
    # 12x1 (test_block_interleaver.py::test_saturated_pipelined_bit_exact).
    "BlockInterleaverBlock": ("sample", "out", {"rows": 2, "cols": 3}),
    # PSK31 raised-cosine ENVELOPE shaper (on-the-fly NCO cosine, PATH B): 7-cell
    # feed-forward pipeline (ingest -> phasegen -> NCO sine column -> shape). The sine
    # column reconverges at `shape` like the NCO's, but the datapath is PURELY
    # feed-forward (NO feedback corridor, no serialize-LOCK), so the saturated stream
    # equals the per-sample stream by construction — verified saturated == per-sample.
    "RaisedCosineEnvelopeBlock": ("sample", "out", {"samples_per_symbol": 8}),
}

# TWO-input, single-real-output blocks: (in_ports, out_port, params). Driven with a
# 2-operand sample. Per-sample reference = run_block_dut_complex (drains flat);
# pipelined = run_block_dut_pipelined. Covers the complex->real converters (re,im ->
# scalar) and the N-input arithmetic (a0,a1 -> out).
REAL_2IN = {
    "ComplexToRealBlock": (("re", "im"), "out", {}),
    "ComplexToImagBlock": (("re", "im"), "out", {}),
    "ComplexToMagSquaredBlock": (("re", "im"), "out", {}),
    # rms_cf (blocks.rms_cf): the complex twin of RMSBlock (REAL_1IN above) —
    # same 4-cell feed-forward chain with a |z|^2 front; complex (re,im) in, ONE
    # real RMS word out, IIR state inside the head cell, no feedback corridor /
    # no reconvergent fan-in, so saturated == per-sample bit-for-bit.
    "RMSCFBlock": (("re", "im"), "out", {"alpha": 0.25}),
    "AddBlock": (("a0", "a1"), "out", {}),
    "MultiplyBlock": (("a0", "a1"), "out", {}),
    "SubtractBlock": (("a0", "a1"), "out", {}),
    # IQUpconvert is complex-in / single-real-out AND carries its OWN LOCK interlock
    # (the same idiom the Costas/RX fix uses) — the strongest saturation cross-check.
    "IQUpconvertBlock": (("xi", "xq"), "out", {}),
    # Bitwise XOR of two byte streams (blocks.xor_bb): memoryless 2-input reconvergent
    # fan-in, one word out per pair, no state — saturated flat stream == per-sample.
    "XorBlock": (("a", "b"), "out", {}),
    # QPSK slicer: single cell, MEMORYLESS per (I,Q) pair (quadrant decision ->
    # 2-bit Gray index at R0; I lands @R0, Q @R1 as one paired packet, no
    # cross-symbol state) — the saturated flat stream must equal per-sample.
    "QPSKSlicerBlock": (("in_i", "in_q"), "out", {}),
}

# RATE-CHANGING blocks (single real in, single real out, N-in-M-out): (in_port,
# out_port, params). Per-sample ref = run_block_dut_rate (drains each trigger's burst);
# pipelined = run_block_dut_pipelined (flat egress). The saturated flat stream must
# equal the per-sample flat stream — a rate block that mis-paces under saturation
# (drops/duplicates a burst) is caught here.
RATE_1IN = {
    "UpsamplerBlock": ("x", "out", {}),        # rate-EXPANDING (fixed factor)
    # ChaCha20 quarter round (RFC 8439 §2.1, no GR counterpart): one raw 16-bit
    # word per trigger in, an 8-word result frame burst out on every 8th
    # trigger (8:8 with framing, so most triggers emit 0 words and one emits
    # 8). 17 cells, purely FEED-FORWARD — no data-feedback loop and no
    # reconvergent fan-in (INV-19/20 N/A) — so the saturated flat stream must
    # equal the per-sample flat stream. The frame counter lives in the second
    # collector cell and is the only cross-sample state.
    "ChaCha20QRBlock": ("x", "out", {}),
    # Polyphase L/M rational resampler (GR rational_resampler_fff): single cell,
    # K-deep input-rate delay line + a countdown mod-M gate across the L unrolled
    # arms. Feed-forward, NO feedback corridor, NO reconvergent fan-in (INV-19/20
    # N/A) — the saturated flat stream must equal the per-sample flat stream
    # (2:3 = a genuinely fractional rate; some triggers emit 0 words, some 1..2).
    "RationalResamplerBlock": ("sample", "out",
                               {"interpolation": 2, "decimation": 3,
                                "taps": [0.4, 0.25, -0.2, 0.1]}),
    "RepeatBlock": ("x", "out", {}),           # rate-EXPANDING (hold-upsample)
    "KeepOneInNBlock": ("x", "out", {"n": 2}),  # rate-REDUCING
    # Windowed zero-crossing rate (window_size samples -> 1 Q15 rate word,
    # rate-REDUCING): single cell with a previous-sample sign register + crossing
    # counter + window down-counter across samples, feed-forward, NO feedback
    # corridor / NO reconvergent fan-in (INV-19/20 N/A), so the saturated flat
    # stream must equal the per-sample flat stream (window_size=4 on the 16-word
    # stimulus emits 4 real rate words — a live, non-empty drive).
    "ZeroCrossingRateBlock": ("sample", "out", {"window_size": 4}),
    # Framewise argmax (n samples -> 1 raw index word, rate-REDUCING): single
    # cell with a running-max register + argmax snapshot + frame down-counter
    # across samples, feed-forward, NO feedback corridor / NO reconvergent
    # fan-in (INV-19/20 N/A), so the saturated flat stream must equal the
    # per-sample flat stream (n=4 on the 16-word stimulus emits 4 real index
    # words — a live, non-empty drive).
    "BinArgmaxBlock": ("sample", "out", {"n": 4}),
    # bit-stream -> symbols (emits every N bits) — proven saturated 2026-07-14.
    "PSKSymbolMapperBlock": ("sample", "out_i", {}),
    # M17 4FSK blocks (2026-07-21): 4FSK symbol MAPPER packs 2 bits/symbol (2:1
    # rate-REDUCING — a symbol emerges every 2 input bits); 4FSK SLICER emits 2 bits
    # per symbol (1:2 rate-EXPANDING). Both are memoryless per (di)bit and saturation-
    # safe — the saturated flat stream equals the per-sample flat stream.
    "FSK4SymbolMapperBlock": ("sample", "out", {}),
    "FSK4SlicerBlock": ("sample", "out", {}),
    # unpack_k_bits (blocks.unpack_k_bits_bb): one byte -> k bits (1:k rate-EXPANDING),
    # counted-loop emit with no cross-sample feedback — memoryless per byte, so the
    # saturated flat stream equals the per-sample flat stream.
    "UnpackKBitsBlock": ("sample", "out", {"k": 8}),
    # RFC 8439 keystream serializer (placeKYT-native, no GR counterpart): hi/lo
    # half-word pairs -> the 32-bit word's 4 bytes little-endian, one byte per
    # word (1:2 rate-EXPANDING; the hi trigger emits 0 words, the lo trigger 4).
    # Single cell, feed-forward, NO feedback corridor and NO reconvergent fan-in
    # (INV-19/20 N/A); the only cross-sample state is the hi/lo parity + the
    # held hi half, so the saturated flat stream must equal the per-sample one.
    "KeystreamSerializerBlock": ("word", "out", {}),
    # Hamming(7,4) FEC encoder: 4 bits -> one 7-bit codeword burst (4:7 rate-
    # EXPANDING). A straight 2-cell feed-forward chain (pack accumulator ->
    # expand burst) with NO feedback corridor and NO reconvergent fan-in
    # (INV-19/20 N/A, no lock) — the saturated flat stream must equal the
    # per-sample flat stream (the burst must neither drop nor duplicate bits).
    "HammingEncoderBlock": ("sample", "out", {}),
    # Hamming(7,4) syndrome decoder (7:4 rate-REDUCING with a 4-bit burst emit):
    # 2-cell linear pipeline (fused pack+syndrome front -> LUT-correct/emit fix),
    # feed-forward, no feedback corridor, no reconvergent fan-in — the saturated
    # flat bit stream must equal the per-sample flat stream (no dropped or
    # duplicated group, the 7:4 output-count bar).
    "HammingDecoderBlock": ("sample", "out", {}),
    # Fixed-coefficient K-vector dot product (correlator pattern, K:1 rate-
    # REDUCING — one weighted-sum word per K inputs, fresh vector each time, no
    # delay line). Gated in the RESTORED S>0 config (the 2-cell mac->restore
    # linear chain): straight feed-forward, NO feedback corridor and NO
    # reconvergent fan-in (INV-19/20 N/A) — the saturated flat stream must
    # equal the per-sample flat stream. The raw single-cell config is gated in
    # test_dot_product_mac.py::test_saturated_equals_per_sample.
    "DotProductMACBlock": ("sample", "out",
                           {"coefficients": [0.9, -0.7, 0.8, 0.6],
                            "bias": 0.2, "k": 4, "mode": "restored"}),
    # Extended Golay (24,12) encoder: 12 bits -> one 24-bit codeword burst
    # (12:24 rate-EXPANDING). A straight 4-cell feed-forward chain (pack
    # accumulator -> par1 -> par2 LOAD-table parity -> emit burst) with NO
    # feedback corridor and NO reconvergent fan-in (each cell has exactly one
    # upstream cell; the two-word D/p handoffs ride the same corridor —
    # INV-19/20 N/A, no lock) — the saturated flat stream must equal the
    # per-sample flat stream (the burst must neither drop nor duplicate bits).
    "GolayEncoderBlock": ("sample", "out", {}),
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
    # CSS dechirp: the ComplexMixer reconvergent fan-in (phase -> 2 NCO arms +
    # relay -> prods) with the free-running double-accumulator phase cell and
    # the MultiplyCC saturating prods/combine tail. pipeline_lock=True selects
    # the serialize-LOCK variant (the combine's @1-abutment WRITE.CFG unlock
    # into phase) — this gate proves the lock releases cleanly and the
    # saturated stream is bit-exact under back-to-back drive.
    "ConjChirpMixerBlock": (("xi", "xq"), "yi",
                            {"n": 16, "pipeline_lock": True}),
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
    # Multi-cell distributed complex delay line (depth 7 = a 2-cell chain, so the
    # inter-cell forwarding handoff is exercised): a pure feed-forward LINEAR
    # chain (each cell fed by exactly ONE predecessor — no feedback loop, no
    # reconvergent fan-in of unequal-length arms, INV-19/20 do not apply), so it
    # needs no serialize-LOCK; gated here to PROVE the saturated (back-to-back
    # queue_words) egress is bit-exact to the per-sample GR-verified stream.
    "ComplexDelayLineBlock": (("xi", "xq"), "out_i", {"depth": 7}),
    # TRUE complex-constant multiply (2 cells: mul -> sat). A FEED-FORWARD wavefront —
    # mul forms both accumulators and forwards them to sat in a single LINEAR handoff
    # (no feedback loop, no reconvergent fan-in of unequal-length arms), so it is
    # saturation-safe by construction (no serialize-LOCK needed — INV-19/20 do not
    # apply). Gated here to PROVE the pipelined (back-to-back) output == the per-sample
    # GR-verified output.
    "MultiplyConstComplex": (("xi", "xq"), "yi", {"re": 0.7, "im": 0.5}),
    # Per-sample table-selected twiddle rotator (radix-2 FFT primitive). A
    # FULLY SERIAL 6-cell chain — EVERY sample transits every cell whatever
    # its kind (trivial entries take pass-through ENTRIES of the same cells),
    # so the trivial and non-trivial paths have EQUAL chain length: no
    # reconvergent fan-in, no overtaking, no serialize-LOCK (INV-19/20 do not
    # apply). The mixed table exercises the identity, -j and 4-MULQ paths
    # back-to-back under saturated drive.
    "TwiddleMultiplyBlock": (("xi", "xq"), "yi",
                             {"twiddles": [1, 0.7071067811865476
                                           - 0.7071067811865476j, -1j,
                                           -0.5 + 0.25j]}),
    # 16-point streaming R2SDF FFT: FOUR serialize-LOCKed stage rings (each
    # stage's delay-feedback write-back races the next sample without its
    # lock — INV-19 by construction). The locks are ALWAYS ON (correctness,
    # not an option): each stage ctl LOCKs after dispatch; the stage's out
    # cell clears it (backward WRITE.CFG) only after the stage's packet has
    # been accepted downstream, so at most one sample is in flight per stage
    # and the fill/butterfly paths' unequal chain lengths cannot reorder.
    # Saturated == per-sample bit-exact is THE gate that proves all four
    # locks release cleanly under back-to-back drive.
    "FFT16Block": (("xi", "xq"), "out_i", {}),
    # 32-point streaming R2SDF FFT: the same FIVE always-on serialize-LOCKed
    # stage rings, folded on the vertical CTL/OUT SPINE. Anchored at (0, 0) —
    # a 9-wide x 10-tall CHIP-SCALE fold does not fit the default (1, 1)
    # placement, and the spine deliberately leaves column 9 and rows 10-11
    # free for the port corridors.
    "FFT32Block": (("xi", "xq"), "out_i", {}, (0, 0)),
    # Complex AGC (GR analog.agc_cc): a 20-cell serialize-LOCKED gain-feedback
    # ring (hold locks, upd's backward WRITE.CFG releases — INV-19). The lock is
    # LOAD-BEARING: with pipeline_lock=False the saturated stream diverges from
    # per-sample in ~90% of words (the gain feedback races open-loop, the Costas
    # dphase failure shape) — pinned in test_agc_cc.py. rate=0.05/reference=0.3
    # so the feedback actually moves the gain within the 16-sample stimulus.
    "AGCCCBlock": (("xi", "xq"), "yi_tap",
                   {"rate": 0.05, "reference": 0.3, "gain": 1.0,
                    "max_gain": 0.0}),
}

# Blocks that need bespoke stimulus (documented, reported as skips — no silent gap).
_CORDIC_COVERAGE = {
    # CORDIC vectoring chains: complex in, ONE real word out per trigger (the
    # flatten handles any words-per-trigger). Fully feed-forward/stateless —
    # saturation-safe by construction; ALSO gated bit-exact in
    # test_cordic_blocks.py::test_chip_saturated_drive_bit_exact.
    # ANCHOR (0,1) [4th element]: at the default (1,1) the router lays the
    # 9-wide Mag chain's egress corridor through the col-0 input-delivery
    # cells — ingress and egress contend under saturation and livelock (the
    # single-chip cousin of INV-32's corridor-sharing rule). At (0,1) the
    # corridors are disjoint.
    "ComplexToMagBlock": (("xi", "xq"), "mag", {}, (0, 1)),
    "ComplexToArgBlock": (("xi", "xq"), "z", {}, (0, 1)),
}
COMPLEX_2IN2OUT.update(_CORDIC_COVERAGE)

NEEDS_BESPOKE = {
    "Poly1305MACBlock": "BESPOKE BY CONSTRUCTION — a one-time MAC, not a "
        "streaming N:M converter: it consumes exactly msg_words words and "
        "emits one 8-word tag, and its saturation hazard is a NEXT WORD "
        "arriving mid-compute (the pack chain's word registers and the limb "
        "accumulators would be overwritten). That is closed by the INV-20 "
        "serialize-LOCK on the input landing and gated on the real placed+"
        "routed+built chip by test_poly1305_mac.py::"
        "test_saturated_backtoback_drive_bit_exact, which enqueues the whole "
        "RFC 8439 S2.5.2 message back-to-back via queue_words_physical, runs "
        "ONE continuous run, and asserts the full 8-word tag bit-exact (with "
        "stop_reason == QueueEmpty). The generic 1-in drivers here also use "
        "a manhattan landing guess, which is off by one for this block's "
        "corridor (INV-60: the build resolves hop 29, the guess gives 28).",
    "ChaCha20KeystreamBlock": "BESPOKE BY CONSTRUCTION — a SOURCE, not a "
        "streaming N:M converter: one trigger produces one whole 64-byte "
        "keystream block and there is no per-sample data input, so the "
        "1-in/1-out saturated drivers here cannot express it. Its load hazard "
        "is the BATCH BOUNDARY instead, and that is gated on the real placed+"
        "routed+built chip by test_chacha20_fixed_tap_ring.py::"
        "test_a_second_batch_recomputes_the_block_bit_exact_on_chip, which "
        "applies the resolved batch_reset_writes exactly as the hosted "
        "bridge's process_batch does and asserts the second batch's 32 words "
        "are bit-exact equal to the first (measured failing without the "
        "reset spec: the re-trigger ran to the event limit and emitted "
        "nothing). The same suite's on-chip gate pins all sixteen RFC 8439 "
        "S2.3.2 state words in order.",
    "LZ4EncoderBlock": "BESPOKE BY CONSTRUCTION — this block is not a streaming "
        "N:M rate converter and the generic saturated driver cannot express it. "
        "It is TWO PASSES over a whole block: pass 1 streams the input into the "
        "SRAM panel and emits NOTHING, and pass 2 (started by an out-of-band "
        "END-OF-BLOCK sentinel word) emits the entire compressed block in a "
        "burst whose length depends on the data. There is no per-sample output "
        "count to check, and 'the whole burst enqueued back-to-back with no "
        "inter-sample quiescence' is ALREADY how it is driven — the panel link "
        "is single-outstanding (SRAM_PANEL.md §5), so every panel round trip is "
        "a held-ack handshake with the upstream stalled behind it. Its coverage "
        "is test_lz4_encoder.py, whose LAYER 6 gates drive the auto-placed, "
        "routed, BUILT design on a real chip through a real SramPanelDevice and "
        "assert the compressed output round-trips AND is accepted by the "
        "INDEPENDENT reference C decoder. Those gates also read stop_reason and "
        "fail on 'Deadlock' (INV-56), which is the saturation hazard this suite "
        "exists to catch.",
    "LZ4DecoderBlock": "NOT DONE — manifest status planned (re-opened 2026-08-29 "
        "after an audit showed its quarantine cited a panel-template cell cap that "
        "does not exist; GolayDecoderBlock is a 7-cell panel-backed block with "
        "status done). The DSP is proven against the reference C implementation in "
        "BOTH directions and on a real chip, but the block is not yet auto-placeable, "
        "so there is no assembled design to drive saturated. Its coverage is "
        "test_lz4_decoder.py. Remove this entry when the block ships.",
    "FFT64Block": "NOT DONE — manifest status needs_human. The block places, "
        "routes and builds, but is NOT yet bit-exact on chip (see the lessons "
        "log: two cells hit the INV-33 state/instruction OVERLAP and stall the "
        "pipeline after one sample). A saturation gate on a block that is not "
        "even correct per-sample would certify nothing. Its coverage is "
        "test_fft64_fit_limit.py (structural + arithmetic only, explicitly NOT "
        "an on-chip correctness claim). NOTE: the P=16 half of that overlap is "
        "the SAME defect FFT32 hit and fixed (the direct table cell's "
        "cross-forward, removed in fft_large._fetch_cell), so FFT64's s1_fetch_d "
        "is already repaired; its s0_mcalc fold cell is not. Move this entry "
        "into COMPLEX_2IN2OUT when the block reaches done.",
    "FFT128Block": "NOT DONE — manifest status needs_human, and it does not "
        "even construct on this array: 7 stages need a 14-row ctl/out spine "
        "against a 12-row panel, so the constructor raises "
        "LargeFFTGeometryError. There is nothing to drive. The stage-boundary "
        "2-die split is its supported topology and is not built. It is also "
        "in catalog._EXCLUDED_BLOCKS for exactly this reason, so it no longer "
        "reaches this coverage sweep — the entry is kept as the documented "
        "reason, and so re-catalogueing it can never silently skip the gate.",
    "FFT128Die0": "HALF A TRANSFORM, and MULTI-CHIP. This shared harness drives "
        "a block alone on ONE chip and compares it to its own reference; a die "
        "of the N=128 split is only meaningful as half of a two-chip pair, and "
        "its output is a PARTIALLY transformed stream, not the transform's "
        "bins. Its saturated behaviour is gated BESPOKE and end-to-end instead, "
        "on the REAL two-chip system: test_fft128_2p2s_example.py drives 200 "
        "samples through die0 -> the inter-chip crossing -> die1 and asserts "
        "200/200 BIT-EXACT vs the whole transform, ONE complex sample per "
        "trigger (the rate check), and QUIESCENCE on every trigger — which is "
        "the property a saturation gate exists to establish. It also holds the "
        "drive shape itself: a complex sample is WRITE xi, pump, WRITE xq, "
        "pump, JUMP, settle, and the un-paced variant is gated as a KNOWN "
        "failure (test_the_unpaced_drive_is_what_stalls), so the pacing that "
        "makes the pair flow cannot be simplified away.",
    "FFT128Die1": "HALF A TRANSFORM, and MULTI-CHIP — same reason as "
        "FFT128Die0, from the other end: die 1 consumes DIE 0'S OUTPUT stream, "
        "not a raw signal, so driving it standalone against a whole-transform "
        "reference would certify nothing. Covered by the same end-to-end "
        "two-chip gate (test_fft128_2p2s_example.py), which is strictly "
        "stronger than the shared harness here: it exercises the placement, "
        "the routes, the build, the boundary packet shape and the crossing, "
        "none of which a single-chip saturated drive touches.",
    "GRUCellBlock": "RATE-REDUCING 2:1 on ONE port (two Q15 feature words per "
        "timestep in -> one RAW class-index word out) with an INTERNAL "
        "recurrence, so it fits neither the 1:1 REAL_1IN harness nor the "
        "RATE_1IN one (which assumes a rate set by a decimation param). Its "
        "saturated gate is BESPOKE and stronger than the shared one: "
        "test_gru_cell.py drives the whole burst via queue_words_physical and "
        "asserts saturated == per-sample == golden for BOTH the class stream "
        "AND the four hidden-state words read straight out of the umB{i} "
        "cells' pinned hs registers (test_saturated_equals_per_sample_and_"
        "golden), plus the 2:1 output COUNT under saturation "
        "(test_saturated_output_count_is_the_2_to_1_rate), 36000 saturated "
        "on-chip timesteps in the long-stream gate, and a livelock assertion "
        "on the capped run. The barrier it exercises (fin's arbiter LOCK "
        "cleared by amx's chain-END WRITE.CFG) is the INV-19/20 serialize-LOCK "
        "applied to a ~50-cell recurrent macro-loop.",
    # Two-EXTERNAL-complex-stream combiners: a sample is 4 operands delivered as
    # TWO complex packets (two JUMPs) into the landing cell's counting join —
    # the shared harnesses here (run_block_dut_pipelined & co) emit exactly ONE
    # JUMP per sample, which would leave the join half-fired. Their saturated
    # gate is BESPOKE and bit-exact: test_add_sub_cc.test_pipelined_equals_per_
    # sample (+ the drive-non-vacuity probe) via run_block_dut_complex2_pipelined.
    "AddCCBlock": "2-complex-stream (two packet JUMPs/sample, counting join); own "
        "saturated gate test_add_sub_cc.py::test_pipelined_equals_per_sample "
        "(queue_words drive, bit-exact, non-vacuity probe)",
    "SubCCBlock": "2-complex-stream (two packet JUMPs/sample, counting join); own "
        "saturated gate test_add_sub_cc.py::test_pipelined_equals_per_sample "
        "(queue_words drive, bit-exact, non-vacuity probe)",
    "MultiplyCCBlock": "2-complex-stream (two packet JUMPs/sample, counting join); own "
        "saturated gate test_multiply_cc.py::test_pipelined_equals_per_sample "
        "(queue_words drive, bit-exact, conjugate-b non-vacuity probe)",
    "R2ButterflyBlock": "2-complex-stream (two packet JUMPs/sample, counting "
        "join) AND two complex OUTPUT pairs (sum/diff on separate cells, "
        "demuxed by per-rail out_tag); own saturated gate "
        "test_r2_butterfly.py::test_pipelined_equals_per_sample "
        "(run_block_dut_complex2_dual pipelined, bit-exact per tagged stream, "
        "non-vacuity probe)",
    "LMSEqualizerBlock": "PER-SAMPLE CONTRACT (INV-19 RECORDED LIMIT, guarded by "
        "test_lms_equalizer.test_saturated_drive_known_limit_guard): under "
        "saturated drive the backward gradient broadcast races the next "
        "sample's forward pass and the design does not quiesce (EventLimit). "
        "Every GRC batch drives per-sample. The serialize-LOCK choreography "
        "(IN locks until BCAST unlocks — the Costas pipeline_lock idiom) is "
        "the recorded follow-up to lift this.",
    "CrossoverBlock": "ROUTING INFRASTRUCTURE, not a DSP block: the corridor-sharing "
        "demux cell (one fwd_face per JUMP entry) has no sample I/O ports the generic "
        "driver could stimulate. Gated structurally (placekyt/tests/"
        "test_crossover_router.py, verification/tests/test_kyt_route_transits.py) and "
        "ridden end-to-end by the cw/psk31 duplex transceiver corridors (panel-paced "
        "per-sample by construction — the panels cannot saturate).",
    "FFT64Block": "CHIP-SCALE (84 cells, sole occupant of the die, anchored "
        "at (0,0)) and — the reason it cannot be a row here — its LATENCY is "
        "63, while this module's shared stimulus is 16 samples. A 16-sample "
        "saturated run of a 64-point streaming FFT never reaches a single "
        "valid output: it exercises only the zero-fill transient and would "
        "pass VACUOUSLY. Its saturated gate is bespoke and sized by REACH — "
        "test_fft64.py::test_saturated_equals_per_sample_bit_exact drives "
        "LATENCY + N = 127 samples (a whole frame past the transient) through "
        "run_block_dut_pipelined in one bounded run and asserts the saturated "
        "stream is bit-exact against BOTH the per-sample chip run and the "
        "streaming golden, so all SIX stage serialize-LOCKs are proven to "
        "release under back-to-back drive. Same mechanism as FFT16Block "
        "(gated in COMPLEX_2IN2OUT above), two more stages of it.",
    "FFT128Block": "not buildable on one die (raises LargeFFTGeometryError: a "
        "7-stage ctl/out spine needs 14 rows in ONE column against a 12-row "
        "array). Nothing to drive until the 2-die split ships.",
    "ComplexCostasLoopBlock": "complex I/Q loop; own gate proto_costas_pipe.py (BER0)",
    "CoherentRXBlock": "complex I/Q RX loop; own gate proto_rx_bisect.py (BER0)",
    "GardnerTimingRecovery": "2-sps timing loop; own gate proto_gardner_race.py",
    # High/Band firdes variants only fail to INSTANTIATE at high gain (Σ|h|>1 build
    # constraint); their datapath == ComplexFIR/LowPass which ARE gated saturated above.
    "ComplexHighPassFilter": "Σ|h|>1 firdes build constraint; datapath == ComplexLowPass (gated)",
    "ComplexBandPassFilter": "Σ|h|>1 firdes build constraint; datapath == ComplexLowPass (gated)",
    "ComplexBandRejectFilter": "Σ|h|>1 firdes build constraint; datapath == ComplexLowPass (gated)",
    "DualFloatToComplexBlock": "2-face rendezvous (own gate proto_dual_f2c)",
    "TMRVoterBlock": "THREE-FACE LOCK-ROTATION RENDEZVOUS with THREE "
        "INDEPENDENT upstream producers: a 'sample' is one word on each of "
        "three DISTINCT faces from three SEPARATE blocks, so no shared harness "
        "can drive it (they all inject one stream through one port landing). "
        "Its saturated behaviour is gated BESPOKE in "
        "verification/tests/test_tmr_voter.py: "
        "test_saturated_equals_per_sample drives each triple's THREE ARM WORDS "
        "back-to-back as raw queue_words (the three producers race at the "
        "rendezvous — the hazard the LOCK exists to survive) over a long run "
        "and asserts equality with the per-sample reference. That gate FOUND a "
        "real construction bug: re-locking straight to face_a at the end of "
        "got_c re-admits the next sample's first arm the instant the current "
        "triple is dispatched, and the sim reports an explicit Deadlock after "
        "ONE packet; the fix is the INV-19/20 serialize-LOCK (got_c locks to "
        "the INTERNAL forward face, which no arm arrives on, and `agree` "
        "re-points LOCK_FACE at arm A with a backward WRITE.CFG @N,3). The "
        "RESIDUAL whole-burst limit — two or more complete triples queued "
        "before running deadlocks — is a FACE-BUDGET wall (N arms + 1 forward "
        "+ 1 release corridor = N+2 faces; N=3 needs 5, a cell has 4) and is "
        "pinned by test_known_limit_saturated_burst_depth_is_one.",
    "FeaturePairJoinBlock": "2-FACE LOCK RENDEZVOUS with TWO INDEPENDENT "
        "upstream producers: a 'sample' is one word on each of two DISTINCT "
        "faces from two SEPARATE blocks, so no shared harness can drive it "
        "(they all inject one stream through one port landing). Its saturated "
        "behaviour is gated BESPOKE and is stronger than a queue_words replay: "
        "test_feature_pair_join.py builds the REAL two-upstream chain (two "
        "independently rate-reducing KeepOneInN arms -> join) on a placed + "
        "routed chip and drives the arms back-to-back in ADVERSARIAL relative "
        "orders — A-then-B, B-then-A, bursty, random over 3 seeds, and a "
        "starved arm — asserting matched, ORDERED pairs every time "
        "(test_random_interleavings_preserve_pairs_and_order, "
        "test_back_to_back_timesteps_do_not_mix). The arm-overhang depth limit "
        "the LOCK mechanism imposes (shared with DualFloatToComplexBlock) is "
        "pinned by test_known_limit_arm_overhang_depth_is_two. The whole-chain "
        "saturated proof against the REAL toggle consumer is "
        "test_real_consumer_chain_matches_the_direct_feed (join -> GRUCellBlock, "
        "bit-identical class words at the correct 1:1 pair rate).",
    "XorJoinBlock": "2-FACE LOCK RENDEZVOUS with TWO INDEPENDENT upstream "
        "producers (the FeaturePairJoinBlock topology with an XOR instead of a "
        "pair-emit): a 'sample' is one word on each of two DISTINCT faces from "
        "two SEPARATE blocks, so no shared harness can drive it — they all "
        "inject one stream through one port landing. Its saturated behaviour is "
        "gated BESPOKE in test_xor_join.py::test_saturated_equals_per_sample, "
        "which enqueues the WHOLE burst (both arms, every sample) as raw "
        "WRITE/DATA/JUMP words via queue_words_physical with NO inter-sample "
        "quiescence anywhere, and asserts both the VALUES and the 1:1 COUNT "
        "against the per-sample drive. It PASSES: unlike the N=3 voter this "
        "block needs no serialize-LOCK, because the arbiter LOCK it already "
        "carries IS the serialization INV-19 prescribes, and at N=2 the face "
        "budget (INV-46: N + 2 = 4) lets the whole rendezvous be ONE cell — "
        "there is no internal datapath for queued samples to pile into. "
        "test_saturated_drive_is_not_vacuous pins that the stimulus would SHOW "
        "a mis-pairing (every cross-sample XOR is disjoint from every correct "
        "one), which matters more here than elsewhere because a desynced XOR "
        "emits a plausible-looking byte rather than an obvious failure.",
    # INV-20 fan-in FIXED (2026-07-21): NCO + FrequencyModulator had the SAME phase->2-arm
    # ->emit reconvergent fan-in as the pre-fix ComplexMixer. The pipeline_lock=True
    # serialize-LOCK (relay arm-serializer + emit dual-FACE unlock + sign-inline interp)
    # makes them saturation-safe. NCOBlock is now gated in COMPLEX_2IN2OUT above.
    # FrequencyModulator has a REAL input (x) / complex out — the COMPLEX_2IN2OUT harness
    # drives 2 in-ports, so FM has its OWN saturated gate (test_fm_saturation_safe below);
    # its datapath is NCO's (verified in COMPLEX_2IN2OUT) with only the phase cell changed.
    "FrequencyModulatorBlock": "real-in/complex-out; own gate test_fm_saturation_safe (locked, bit-exact)",
    "ChirpGeneratorBlock": "rate-EXPANDING (1 symbol -> n complex samples) real-in/complex-out burst source — fits no shared harness; own bespoke saturated gate test_chirp_generator.py::test_saturated_equals_per_sample (queue_words whole burst, one bounded run, bit-exact + exact 2n count; exercises the always-on arbiter serialize-LOCK + the self-paced emit->sweep return kick)",
    "ComplexUpsamplerBlock": "rate-EXPANDING complex (2-rail zero-stuff), no complex-rate harness; MEMORYLESS (no feedback/state carried across samples) so per-sample == saturated by construction — cf. UpsamplerBlock (RATE_1IN, gated)",
    "ComplexGainBlock": "complex (2-rail) fixed-gain scaler, no complex-in/complex-out per-sample harness; MEMORYLESS (no feedback/state across samples) so per-sample == saturated by construction — cf. GainBlock (REAL_1IN, gated). Driven SATURATED end-to-end in the qam16_modem BER-0 acceptance test (pipelined RX gain-stage).",
    "BPSKSlicerBlock": "GR-equiv 'bit' mode is MEMORYLESS (sign slice, no state across samples) so per-sample == saturated by construction — cf. GainBlock (REAL_1IN). The optional 'byte'/'word' packing carries a bit-counter across samples; that path is driven SATURATED end-to-end in the coherent BPSK RX BER-0 chain (test_slicer_out_mode packed RX). GR digital.binary_slicer_fb takes no params + emits 1 byte/sample = the 'bit' mode.",
    "SoftDemodulatorBlock": "complex input",
    # FM demod (analog.quadrature_demod_cf): complex I/Q in -> real out; the per-sample
    # harness drives single-REAL-input blocks, so it can't drive this. Its 1-sample
    # differentiator feedback is serialized in-cell (no cross-cell corridor). Now visible
    # in the catalog after its GRC binding landed (was uncurated) — classify it here.
    "QuadratureDemodBlock": "complex I/Q input; own DSP gate test_quadrature_demod.py",
    "BandPassFilter": "band filter — covered by FIR family",
    "BandRejectFilter": "band filter — covered by FIR family",
    # M17 4FSK sync-timing recovery: correlates the LSF sync word then GATE-decimates
    # 2:1 — its output is CONDITIONAL on sync detection, so a flat random stimulus emits
    # nothing (no lock). Needs a FRAMED (preamble+sync+payload) burst; driven saturated
    # in its own gate (proto_fsk4_sync_model / the fsk4 modem RX BER harness), which
    # proves the whole RX chain recovers BER 0 pipelined (the real saturation proof).
    "FSK4SyncTimingRecoveryBlock": "sync-gated decimator; needs framed burst — own gate (fsk4 RX BER0 pipelined)",
    # QUARANTINED ham-mode blocks (INV-29): the on-chip build RAISES the table/state
    # substrate wall (32-word cell / ~21-entry LOAD table), so there is no on-chip
    # datapath to saturate — their Python golden is bit-exact-verified in their own
    # test_*.py instead. Listed here so the coverage gate stays green (build-raises,
    # not silently uncovered). Each needs the external SRAM panel (a human-scoped build).
    # SRAM controller (INV-31): a memory-interface macro that drives a host-side SRAM
    # panel over a chip port via WRITE/JUMP protocol — it does NOT transform a sample
    # stream, so the per-sample==saturated model doesn't apply. Its own dedicated gate
    # (placekyt/tests/test_sram_panel.py, 21 tests) covers the single-outstanding,
    # no-FIFO panel handshake under real routing (the saturation-equivalent property).
    "SramControllerBlock": "memory-interface controller (INV-31); own gate test_sram_panel.py (21 tests, single-outstanding no-FIFO handshake)",
    # VaricodeEncoder is now SRAM-BACKED + verified (no longer quarantined): its emit
    # cell is memoryless per push-read; its per-symbol correctness through the real panel
    # round-trip is gated end-to-end in test_varicode_encoder_sram.py (bit-exact vs golden).
    "VaricodeEncoderBlock": "SRAM-backed (INV-31): emit cell driven by the panel push-read; own gate test_varicode_encoder_sram.py (full-chain bit-exact through real routing)",
    # VaricodeDecoder is now SRAM-BACKED + verified (no longer quarantined): the accumulate
    # cell forms the codeword integer + pulls the panel read trigger on the '00' boundary, and
    # the emit cell consumes the push-read; the full accumulate->panel->emit chain is gated
    # bit-exact + round-trip through real routing in test_varicode_decoder_sram.py.
    # (a stale duplicate "QUARANTINE (INV-29)" key for VaricodeDecoderBlock used to
    # follow this entry and, being second, silently WON the dict merge — removed.)
    "VaricodeDecoderBlock": "SRAM-backed (INV-31): accumulate cell forms the codeword + pulls the panel read trigger, emit cell consumes the push-read; own gate test_varicode_decoder_sram.py (full-chain bit-exact + round-trip through real routing)",
    # GolayDecoder is SRAM-BACKED (INV-31) and rides the PER-SAMPLE PANEL CONTRACT:
    # the server forces per-sample pacing for panel designs, and every 24-bit codeword
    # takes a panel push-read round-trip (syndrome -> error-pattern LUT), so the
    # saturated-flat-stream harness does not model its real drive. Its own gate
    # (test_golay_decoder.py) proves the full pack->syndrome->panel->emit chain
    # bit-exact + round-trip vs the golden encoder through the REAL panel.
    "GolayDecoderBlock": "SRAM-backed (INV-31), per-sample panel contract (the server forces per-sample for panel designs); own gate test_golay_decoder.py (full-chain bit-exact + round-trip vs the golden encoder through the real panel)",
    # CWDecoder is SRAM-BACKED (INV-31): a decision block that consumes a whole keyer
    # ENVELOPE and needs the SRAM panel (LUT + run scratch) + a two-pass decode, so it
    # does not fit the single-rail RATE_1IN/REAL_1IN harnesses. Its per-message
    # correctness through the real panel round-trip is gated end-to-end in
    # test_cw_decoder_sram.py (exact vs the ITU-R golden through real scratch commits +
    # LUT push-reads).
    "CWDecoderBlock": "SRAM-backed (INV-31): two-pass envelope decoder, panel LUT + run scratch; own gate test_cw_decoder_sram.py (round-trip exact vs golden through the real panel)",
    "RaisedCosineEnvelopeBlock": "QUARANTINE (INV-29): sps-entry cosine table + lookahead exceed the cell; golden gated in test_raised_cosine_envelope.py; build RAISES",
    # CWKeyer is now SRAM-BACKED + verified (no longer quarantined): the timing FSM runs
    # off-cell at build time, its keying schedule (run records) lives in the panel, and the
    # on-chip cell is a run player driven by the panel push-read (one input char -> many
    # output samples: rate-changing, so bespoke). Full-chain bit-exact through the real panel
    # round-trip is gated in test_cw_keyer_sram.py.
    # (A stale duplicate key carrying the pre-SRAM QUARANTINE string sat below this
    # entry from 2026-08-29 to 2026-08-30 and, being later, silently WON the dict
    # merge — flagged twice in lessons_log before removal.)
    "CWKeyerBlock": "SRAM-backed (INV-31): run-record player driven by the panel push-read (rate-changing, char->envelope); own gate test_cw_keyer_sram.py (full-chain bit-exact through real routing)",
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
    # M&M (Mueller & Muller) decision-directed 16-QAM timing loop — a data-FEEDBACK
    # loop (like Gardner/Costas). It is UNCONDITIONALLY saturation-safe (INV-19): the NCO
    # counter LOCKs its arbiter to the (orientation-safe, is_face) feedback face every
    # sample, serialising the interior
    # (land fan -> 2 Farrow rails -> decision-directed ted -> PI -> period_relay) so one
    # sample closes the loop before the next enters; period_relay clears the lock with a
    # backward WRITE.CFG. Like Gardner/Costas it needs a REAL RRC-shaped 2-sps stimulus
    # to lock — the generic synthetic 16-sample drive here produces NO strobes / NO
    # egress in either mode (nothing to compare) and the empty locked pipeline never
    # quiesces. Proved saturated in its OWN gate on the real 16-QAM RRC channel:
    # test_mm_timing_recovery.test_saturated_equals_per_sample (bit-exact I&Q, 0-diff,
    # toff 0.0/0.3/0.5).
    "MMTimingRecoveryBlock": "2-sps 16-QAM M&M timing loop; own gate test_mm_timing_recovery.test_saturated_equals_per_sample (bit-exact saturated, real RRC channel)",
    # FLL band-edge: a 21+-cell serialized RING loop (INV-19 lock on phase, cleared
    # by pi). Saturation-SAFE and bit-exact, but the fully-serial ring costs ~2500
    # sim events/sample — over this file's shared 2000/sample budget — so it runs
    # its OWN saturated gate with a justified event budget:
    # test_fll_band_edge.test_saturated_equals_per_sample (bit-exact I&Q, N=100).
    "FLLBandEdgeBlock": "serialized serpentine loop ~2500 events/sample; own gate test_fll_band_edge.test_saturated_equals_per_sample (bit-exact saturated)",
    # Freq-xlating decimating FIR (channelizer): fused NCO down-mixer + real-tap complex
    # FIR + decimation. Its NCO/mixer front has the SAME phase->sin/cos+relay->mixer
    # RECONVERGENT fan-in as ComplexMixer/NCO (INV-20), so it DEADLOCKS under saturated
    # drive (0 output) UNLESS serialize-locked. But the ComplexMixer/NCO serialize-LOCK
    # rides the BLOCK EXIT cell; here the mixer is MID-chain (exit = the FIR I-rail last
    # cell), and _apply_internal_feedback's config-only unlock branch assumes the unlock
    # rides output_cell_id — so a mid-chain unlock builds but emits nothing (verified
    # 2026-08-06). pipeline_lock RAISES rather than ship a silently-empty variant. The
    # block is verified per-sample BIT-EXACT vs GR (test_freq_xlating_fir.py, 32 tests)
    # + orientation-invariant; saturation is BESPOKE pending a build-engine mid-chain
    # unlock. Drive un-saturated / at the channel rate. See lessons_log 2026-08-06.
    "FreqXlatingFIRBlock": "fused mixer(fan-in)+FIR channelizer; saturation needs a mid-chain serialize-LOCK unlock (build-engine gap, INV-20); verified per-sample bit-exact vs GR — own gate test_freq_xlating_fir.py",
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

    entry = COMPLEX_2IN2OUT[block_type]
    in_ports, out_port, params = entry[:3]
    # Optional 4th element: block anchor (see _CORDIC_COVERAGE — some wide
    # chains need a placement whose egress corridor is disjoint from ingress).
    place = entry[3] if len(entry) > 3 else (1, 1)
    pairs = [(_STIM[k], _STIM[(k + 3) % len(_STIM)]) for k in range(len(_STIM))]

    # Per-sample reference (takes FLOAT pairs); flatten the per-trigger [yi,yq] lists.
    seq = run_block_dut_complex(block_type, pairs, params=params, chip_yaml=CHIP_YAML,
                                in_ports=in_ports, out_port=out_port,
                                place_xy=place)
    assert seq.ok, f"per-sample build/run failed: {seq.reason}"
    seq_out = [w for g in seq.outputs_q15 for w in g]

    # Saturated drive (takes PRE-QUANTIZED uint16 pairs).
    samples = [(_q15(i), _q15(q)) for (i, q) in pairs]
    pipe = run_block_dut_pipelined(block_type, samples, params=params,
                                   chip_yaml=CHIP_YAML, in_ports=in_ports,
                                   out_port=out_port, place_xy=place)
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
