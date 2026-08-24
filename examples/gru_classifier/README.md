<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# GRU modulation classifier

A small **GRU modulation classifier**: a complex baseband stream in, one **class
word** out every 32 samples — `0 = SSB`, `1 = BPSK`, `2 = 4-FSK`, `3 = noise`.
Both features are computed by library DSP blocks, and the recurrent network
itself — gates, activation tables, hidden state, and the 4-class readout — is one
placed block whose weights come from the trained model in [`ml/`](ml/).

## Status — read this first

**This example is NOT finished, and it is not shipped as a runnable demo.** The
chain does not yet place and route as one chip, so there is no `.kyt`, no `.grc`,
and no end-to-end on-chip run. What *is* done, and gated:

| | Status |
|---|---|
| Feature front end (RMS + ZCR arms) vs the trained model's own `ml/features.py` | **verified** — ZCR bit-exact, RMS inside a derived bound |
| The classifier on the shipped stimulus, through the bit-exact GRU golden | **verified** — all four segments voted correctly |
| The `FeaturePairJoin → GRUCell` tail, placed and routed on a real chip | **verified** — routes at 72/120 cells |
| The **whole chain** placed and routed on one chip | **BLOCKED** — always exactly one net short |
| End-to-end run on a placed + routed array | **not done** |

Per [`../../AGENTS.md`](../../AGENTS.md) §5b an example is not done until it has
been observed producing the correct output on a real placed and routed chip.
This one has not. The measured shortfall, the search that established it, and
what a human should look at are in the `gru_classifier example` entry of
[`../../verification/KNOWLEDGE_BASE/lessons_log.md`](../../verification/KNOWLEDGE_BASE/lessons_log.md).

## The chain

```
        +-- ComplexToMagSquared -> MovingAverage(32) -> Sqrt -> KeepOneInN(32) --+
        |          |z|^2               mean power       rms        1 per window  |
 I/Q ---+                                                                        +-> FeaturePairJoin -> GRUCell -> class
        |                                                                        |
        +-- ZeroCrossingRate(32) ------------------------------------------------+
                   zcr
```

| Block | Cells | What it contributes |
|---|--:|---|
| `ComplexToMagSquaredBlock` | 1 | instantaneous power `re² + im²` |
| `MovingAverageBlock(32, 1/32)` | 7 | boxcar mean over the 32-sample window |
| `SqrtBlock` | 3 | Q15 square root → the RMS feature |
| `KeepOneInNBlock(32)` | 1 | one RMS word per window (phase 31) |
| `ZeroCrossingRateBlock(32)` | 1 | the ZCR feature, one word per window |
| `FeaturePairJoinBlock` | 1 | orders the two features into one timestep |
| `GRUCellBlock` | 51 | the GRU + 4-class readout + argmax |
| **total** | **65** | of a 120-cell array |

### Why the RMS arm is four blocks

The model's feature is `rms = sqrt(mean |x|²)` over a **non-overlapping**
32-sample window (`ml/features.py` is the authoritative definition).
`MovingAverage(32, scale=1/32)` is a boxcar mean of the last 32 power samples —
exactly that window mean — and `KeepOneInN(32)` keeps phase 31, the sample at
which the boxcar has consumed precisely one whole window.

The library also has an RMS block, but it is an **exponential IIR** — a different
filter, not the model's feature — so it is deliberately not used here.

### Why the arms stay in step

`KeepOneInN(n)` keeps phase `n−1` and `ZeroCrossingRate(32)` emits on input
indices 31, 63, … — the same indices. The arms are index-aligned by construction
at one word each per 32 input samples, so no re-synchroniser is needed.

**Word order is part of the contract.** `ml/config.json` pins
`features = ["rms", "zcr"]`, so word 0 is RMS and word 1 is ZCR. Swapping them
feeds the trained weights a transposed feature vector; the suite proves that
mutation fails.

## Run what exists

```sh
PYTHONPATH=runtime/python:placekyt QT_QPA_PLATFORM=offscreen \
    .venv/bin/python examples/gru_classifier/gru_classifier_demo.py
```

This prints the stimulus, the feature-equivalence measurements, the classifier
verdict, and — measured live, not quoted — the placement status of both the tail
and the whole chain.

## The stimulus

`gru_stimulus.py` builds one clip from the example's own seeded generators
(`ml/signals.py`): four class segments concatenated in order. Two properties are
load-bearing and are asserted by the gate, not assumed:

* **It stays in the trained distribution.** Each segment's gain comes from
  `ml/config.json`'s `gain_range` and its frequency offset from
  `freq_offset_hz`. Clips outside that range classify worse — including through
  the *offline* reference, so it is the model's property, not the chip's.

* **It leaves Q15 headroom.** `ComplexToMagSquared` computes `re² + im²` in Q15
  and **saturates** at full scale, so any sample with `|z| ≥ 1` clips and biases
  that window's mean power downward. The shipped clip's `peak |z|` is **0.862**.

  These pull against each other, and the tension is real: `gain` sets a segment's
  *RMS* while saturation is driven by its *peak*, and the classes' crest factors
  differ a lot (4-FSK 1.27, BPSK 1.71, noise 3.10, SSB 3.59 median). Over the
  trained set, `peak |z| > 1` for **100 % of SSB and 79 % of noise clips** — a
  float feature front end never notices; a Q15 one clips hard (measured: error
  blows out to −1247 LSB). The per-segment gains are therefore pinned at the low
  end of the trained range (`SEGMENT_GAIN`), chosen for headroom.

## Feature tolerances (derived, not tuned)

**ZCR is bit-exact** — 0 mismatches over 480 windows — once the block's pinned
convention is accounted for: each window counts the 32 pairs *ending* at its
samples (so the inter-window boundary pair is included), preceded by one implicit
non-negative sample. Against plain `ml/features.py` (31 strictly-interior pairs)
it therefore reads **+1 crossing = +1024 Q15 LSB** on the windows whose boundary
pair happens to cross — 116 of 480 on the shipped clip. Derived, not noise.

**RMS is bounded downward by truncation.** Every stage truncates:

| stage | worst-case deficit |
|---|--:|
| `ComplexToMagSquared` — two truncating Q15 products | 2 LSB of power |
| `MovingAverage(32)` — 32 truncating taps (tap = 1024 = 1/32 exactly) | 32 LSB of power |
| `SqrtBlock` vs the rounded ideal | −4 … +1 LSB |

The power deficit propagates through the square root by `dy = dP / (2√P)`, so in
Q15 LSB the bound is **input-level dependent**:

```
−(34 · 16384 / y) − 4  ≤  (chip − ideal)  ≤  +1        (y = the RMS word)
```

Measured over 1600 windows spanning four classes and five peak levels:
**0 violations**, tightest case at **64 %** of the bound. On the shipped clip the
errors run **−14 … −75 LSB**. A flat tolerance would have been wrong — the same
chain reads −14 LSB on a loud window and −218 on a quiet one.

## Verification

`verification/tests/test_gru_classifier_example.py` — **19 tests, all green**:
the stimulus' properties, the two feature arms against `ml/features.py`, the
classifier verdict, four mutations proven to **fail** (swapped word order, wrong
weights, zero features, a saturating clip breaking the RMS bound), and three
known-limit guards that pin the placement wall and will **fail the day it lifts**.

## Files

| File | What |
|---|---|
| `gru_classifier.py` | the chain: topology, feature references, goldens, live route measurement |
| `gru_stimulus.py` | the shipped 4-segment stimulus |
| `gru_classifier_demo.py` | what runs today, including the placement status |
| `ml/` | the offline pipeline: signals, features, training, references |
