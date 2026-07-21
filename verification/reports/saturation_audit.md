<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Saturation-safety audit (2026-07-21)

A block is **saturation-safe** when, driven back-to-back under a saturated / pipelined
input stream (`queue_words_physical`, one continuous `run()`, NO drain between samples),
it produces the SAME output word COUNT and the SAME output VALUES as under per-sample
(inject-and-flush) drive — which is the drive under which every block was GNU-Radio-verified,
so its per-sample on-chip egress is the golden reference. This is the streaming analogue of
the orientation-invariance gate (INV-23).

**Audit result (as run): 37 PASS / 3 FAIL / 1 XFAIL — 41 'done' blocks.** The 3 FAILs
(FrequencyModulatorBlock, NCOBlock, ComplexMixerBlock at their DEFAULT `pipeline_lock=False`)
are the INV-20 reconvergent-fan-in class. **All 3 are now FIXED via the opt-in serialize-LOCK
(`pipeline_lock=True`)** — see INV-20 + the 2026-07-21 lessons_log entry.

This report is a point-in-time record. The LIVE, CANONICAL saturation gate is
`verification/tests/test_pipeline_saturation.py` (every 'done' block is in one of its coverage
sets or in `NEEDS_BESPOKE` with a reason; a coverage test forbids silent omission). The one-off
audit harness/tool that produced this table were removed as redundant with that gate.

## PASS/FAIL table

| Block | Drive | Status | Per-sample words | Saturated words | Values match |
|-------|-------|--------|------------------:|----------------:|:------------:|
| ComplexMixerBlock | cplx_out | **FAIL** | 64 | 0 | NO |
| FrequencyModulatorBlock | rate_cplx | **FAIL** | 64 | 32 | NO |
| NCOBlock | cplx_out | **FAIL** | 64 | 34 | NO |
| DualFloatToComplexBlock | two_face | **XFAIL** | 0 | 0 | n/a |
| AGCBlock | real1 | **PASS** | 32 | 32 | yes |
| AbsBlock | real1 | **PASS** | 32 | 32 | yes |
| AddBlock | two_in | **PASS** | 32 | 32 | yes |
| BandPassFilter | real1 | **PASS** | 32 | 32 | yes |
| BandRejectFilter | real1 | **PASS** | 32 | 32 | yes |
| ComplexBandPassFilter | cplx_out | **PASS** | 64 | 64 | yes |
| ComplexBandRejectFilter | cplx_out | **PASS** | 64 | 64 | yes |
| ComplexFIRFilterBlock | cplx_out | **PASS** | 64 | 64 | yes |
| ComplexHighPassFilter | cplx_out | **PASS** | 64 | 64 | yes |
| ComplexLowPassFilter | cplx_out | **PASS** | 64 | 64 | yes |
| ComplexToFloatBlock | cplx_out | **PASS** | 64 | 64 | yes |
| ComplexToImagBlock | two_in | **PASS** | 32 | 32 | yes |
| ComplexToMagSquaredBlock | two_in | **PASS** | 32 | 32 | yes |
| ComplexToRealBlock | two_in | **PASS** | 32 | 32 | yes |
| ComplexUpsamplerBlock | cplx_out | **PASS** | 128 | 128 | yes |
| ConjugateBlock | cplx_out | **PASS** | 64 | 64 | yes |
| DCBlockerBlock | real1 | **PASS** | 32 | 32 | yes |
| FIRFilterBlock | real1 | **PASS** | 32 | 32 | yes |
| FSK4SlicerBlock | rate | **PASS** | 64 | 64 | yes |
| FSK4SymbolMapperBlock | rate | **PASS** | 16 | 16 | yes |
| FSK4SyncTimingRecoveryBlock | rate | **PASS** | 12 | 12 | yes |
| FloatToComplexBlock | cplx_out | **PASS** | 64 | 64 | yes |
| GainBlock | real1 | **PASS** | 32 | 32 | yes |
| HighPassFilter | real1 | **PASS** | 32 | 32 | yes |
| IIRBiquadBlock | real1 | **PASS** | 32 | 32 | yes |
| IQUpconvertBlock | two_in | **PASS** | 32 | 32 | yes |
| KeepOneInNBlock | rate | **PASS** | 16 | 16 | yes |
| LowPassFilter | real1 | **PASS** | 32 | 32 | yes |
| MovingAverageBlock | real1 | **PASS** | 32 | 32 | yes |
| MultiplyBlock | two_in | **PASS** | 32 | 32 | yes |
| PSKSymbolMapperBlock | rate | **PASS** | 20 | 20 | yes |
| QuadratureDemodBlock | two_in | **PASS** | 32 | 32 | yes |
| RRCPulseShaperBlock | real1 | **PASS** | 32 | 32 | yes |
| SoftDemodulatorBlock | real1 | **PASS** | 32 | 32 | yes |
| SquelchBlock | real1 | **PASS** | 32 | 32 | yes |
| SubtractBlock | two_in | **PASS** | 32 | 32 | yes |
| UpsamplerBlock | rate | **PASS** | 128 | 128 | yes |

## Per-fail evidence + root-cause hypothesis

### FrequencyModulatorBlock  (FAIL)

- Expected (per-sample) output words: **64**
- Actual (saturated) output words: **32**
- Rate: 32/64 = 0.500 of expected
- First value mismatch at egress word index 1
- **Root-cause hypothesis:** Saturated emitted 32 words vs 64 per-sample (~2.00x fewer), and the run REACHED quiescence (drops samples, does NOT livelock). Drops EVERY OTHER sample: the 2-word (yi/yq) emit cell does not re-arm its input handshake before the next back-to-back sample arrives, so alternate triggers are swallowed by the still-in-flight 2-word emit (multi-write emit races the JUMP). Needs a serialize-LOCK / re-arm on the emit cell (INV-19/INV-20).

### ComplexMixerBlock  (FAIL)

- Expected (per-sample) output words: **64**
- Actual (saturated) output words: **0**
- **Root-cause hypothesis:** Saturated emitted 0 of 64 expected words but the run REACHED quiescence (not a livelock): with back-to-back samples the reconvergent fan-in (phase -> 2 NCO columns + a relay -> emit) STALLS — a 2nd sample's fast-path operands occupy the fan-in cell's input registers before the 1st sample's slow-path operand arrives, so no trigger ever completes an emit. The shipped default (pipeline_lock=False) has no serialize-LOCK; the pipeline_lock=True variant IS saturation-safe (64/64, bit-exact) — INV-20.

### NCOBlock  (FAIL)

- Expected (per-sample) output words: **64**
- Actual (saturated) output words: **34**
- Rate: 34/64 = 0.531 of expected
- First value mismatch at egress word index 0
- **Root-cause hypothesis:** Saturated emitted 34 words vs 64 per-sample (~1.88x fewer), and the run REACHED quiescence (drops samples, does NOT livelock). Drops EVERY OTHER sample: the 2-word (yi/yq) emit cell does not re-arm its input handshake before the next back-to-back sample arrives, so alternate triggers are swallowed by the still-in-flight 2-word emit (multi-write emit races the JUMP). Needs a serialize-LOCK / re-arm on the emit cell (INV-19/INV-20).

### DualFloatToComplexBlock  (XFAIL)

- Expected (per-sample) output words: **0**
- Actual (saturated) output words: **0**
- **Root-cause hypothesis:** Saturated build/run failed: 

## Method / notes

- Stimulus: 32 deterministic fractional real samples (or 32 (i,q) pairs for 2-input / complex-out blocks), longer than the deepest block's state so a hazard has co-resident samples (INV-12).
- COUNT is the primary gate: a saturation-unsafe block drops, duplicates, stalls, or deadlocks, changing the egress word count.
- VALUES are compared bit-exact against the per-sample egress (the Q15 reference) position-for-position.
- Bespoke-drive blocks (face-distinct rendezvous) are marked XFAIL and measured best-effort; they have their own gates.

_Audit run took 2s._
