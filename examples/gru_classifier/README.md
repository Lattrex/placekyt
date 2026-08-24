<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# GRU modulation classifier

A small **GRU modulation classifier**: a complex baseband stream in, one **class
word** out every 32 samples — `0 = SSB`, `1 = BPSK`, `2 = 4-FSK`, `3 = noise`.
Both features are computed by library DSP blocks, and the recurrent network
itself — gates, activation tables, hidden state, and the 4-class readout — is one
placed block whose weights come from the trained model in [`ml/`](ml/).

## Status — shipped and verified end to end

**This example runs on a real placed and routed chip.** The whole chain builds
as one design at **102 of 120 cells** and classifies the shipped stimulus with
its class stream **bit-identical** to the offline chip-exact model.

| | Status |
|---|---|
| Feature front end (RMS + ZCR arms) vs the trained model's own `ml/features.py` | **verified** — ZCR bit-exact, RMS inside a derived bound |
| The whole chain placed and routed on one chip | **verified** — every net routes, builds at 102/120 |
| End-to-end run on the placed + routed array | **verified** — 480 windows, agreement **1.000000** vs the golden |
| On-chip classification of all four classes | **verified** — segment votes `[0, 1, 2, 3]`, on-chip == offline |
| The `.grc` under the real GRC compiler | **verified** — opens, generates, instantiates |

On-chip per-step accuracy after burn-in: SSB **1.000**, BPSK **0.811**,
4-FSK **0.856**, noise **1.000** — mean **0.917**, and *exactly equal* to the
offline model's on the same clip (asserted, not eyeballed: the gate compares the
two accuracy vectors).

**What is NOT verified.** The `.grc` is gated by
`test_examples_grc_instantiate.py` — it opens, GRC-generates, and instantiates
against the repo ymls with the repo markers. It has **not** been run against a
live hosted server the way the transceivers are by
`test_examples_grc_userpath.py`, and no one has watched the scopes paint in the
GUI. The end-to-end evidence above is the headless on-chip run through the real
built bitstream, which is the stronger claim about the *chip*; the GUI display
path is a separate claim and is not made here.

### How it got unblocked: the fold, not the chain

For four dispatches this chain was **always exactly one net short**, and which
net failed rotated as blocks moved — the signature of a saturated array rather
than one bad anchor. Three explanations were measured and ruled out:

* **Not capacity.** The blocks total 65 of 120 cells.
* **Not the hop ceiling.** INV-36 lifted the 31-hop limit; no `hop_overflow` in
  5039 measured layouts.
* **Not the arm.** Shrinking the RMS arm was swept over boxcar lengths
  32/16/8/4 with and without `Sqrt`: 65 block cells → 4 nets short, 62 → 2,
  57 → 1, 56 → 1. Even a 4-tap boxcar with `Sqrt` dropped — no longer the
  model's feature — was one net short.

The fourth explanation was the **fold**, and it was right but under-scoped.
`GRUCellBlock` was first re-folded to **8×7** (I/O on the north edge facing the
chip's two row-0 ports, port cost 11 → 7 cells), which took the search from two
nets short to **one**; 4180 further layouts stayed at one. The conclusion drawn
then was that no fold could close it, resting on a sound structural argument:
**a closed ring can never contain a free through-channel** — a cycle cannot jump
a gap — so all of its free space is perimeter, and free-space quality measured
*identical* across every legal fold.

That argument is correct. Its **conclusion** was scoped to the three bounding
boxes [INV-9](../../verification/KNOWLEDGE_BASE/invariants.md)'s ≤ 8-across
convention allowed for a 51-cell block. Waiving that convention for this block —
the **`CHIP_SCALE` placement class**, declared per class and never a global
loosening — admits a **10 × 5** box, and the perimeter free space of a 10-wide
block *is* six contiguous full-width rows. That is the through-channel the ten
nets could never find.

`CHIP_SCALE` comes with an explicit trade, and the block honours it: nothing can
reach the far side of a 10-wide block, so **its input and output must share one
edge**. `GRUCellBlock`'s `fin` and `oout` sit three cells apart on its north
edge.

The wide fold is **not** cheaper for the block's own corridors at the anchor the
example uses. Measured at its best anchor (row 0) the block plus its two port
corridors builds in **58 cells**, against 64 for the 8×7 — but the example seats
it at row 6 (**70 cells**, +2 per row of descent) precisely so the front end gets
the six port-side rows. The fold wins on the **shape** of the free space it
leaves, not on its own cost, and 102/120 for the whole chain is the number that
settles it.

Both re-folds preserved behaviour **exactly**, re-verified on chip each time:
36,000 on-chip steps at agreement **1.000000**, clip vote **0.9667 on-chip ==
0.9667 offline** over 120 held-out clips, all block gates green.

Re-folding also required a real fix, now
[INV-37](../../verification/KNOWLEDGE_BASE/invariants.md): the block baked three
`is_face=True` constants as literals, silently pinning it to the authored fold.
Every re-fold built clean, passed every geometric gate, and computed garbage
(the recurrence never landed; `h` froze at timestep 0). Those constants are
derived from the fold now — which is exactly why this second, much larger
re-fold was a one-method change.

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
| `GRUCellBlock` | 51 | the GRU + 4-class readout + argmax (wide-flat 10×6) |
| **total** | **65** | of a 120-cell array — **102** with the routed corridors |

On the array the GRU occupies a solid band across rows 6–11 and the six
front-end blocks live in the free rows above it, between the chip's row-0 ports
and the GRU's north-edge input.

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

## Run it

**The demo (headless, ~1 minute)** — prints the stimulus, the feature
measurements, the placement measured live, and the **on-chip classification**:

```sh
PYTHONPATH=runtime/python:placekyt QT_QPA_PLATFORM=offscreen \
    .venv/bin/python examples/gru_classifier/gru_classifier_demo.py
```

**In the GUI / GNU Radio Companion.** Open `gru_classifier.kyt` in placeKYT
(**open it — do not import the `.grc`**; the design is hand-placed, see below),
start its server, then open `gru_classifier.grc` in GRC and run it. The
**Class index over time** scope is fed the chip's verdict and should track the
stimulus as it walks SSB → BPSK → 4-FSK → noise, beside a **TRUE class** scope
showing the ground truth. The flowgraph targets **port 58950**, placeKYT's
default host port — `server_port: 0` silently no-ops and leaves a blank window
with a plausible axis — and both scopes are sized below their burst, because a
QT time sink draws nothing until a full `size` buffer arrives and the GR
scheduler strands the tail of a finite stream.

> This GUI path has **not** been run: the `.grc` is gated only as far as
> opening, generating and instantiating (see *What is NOT verified* above).
> Everything the chip does is verified headlessly; the display is not.

**Rebuild the `.kyt`:**

```sh
PYTHONPATH=runtime/python:placekyt QT_QPA_PLATFORM=offscreen \
    .venv/bin/python examples/gru_classifier/build_kyt.py
```

### Why the design is hand-placed

The chain fills 102 of 120 cells and a 400-layout random search over the free
band found **exactly one** arrangement that both routes and builds. The generic
auto-placer does not find it, so the anchors are pinned in
`gru_classifier.BEST_KNOWN_ANCHORS` and `build_kyt.py` writes that design out.
This is the same convention the FSK4 and 16-QAM modems use: **open the `.kyt`,
don't import the `.grc`**.

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

`verification/tests/test_gru_classifier_example.py` — **34 tests, all green**:

* the stimulus' load-bearing properties (Q15 headroom, trained distribution);
* the two feature arms against `ml/features.py` — ZCR bit-exact, RMS inside the
  derived level-dependent bound, plus a proof the bound is **not vacuous**;
* the offline chain into the chip-exact GRU golden;
* **the on-chip run**: 480 windows through the real bitstream, asserted
  word-for-word against the offline golden and against the shipped golden file,
  every segment vote correct, and on-chip accuracy asserted *equal* to offline;
* **the shipped `.kyt` FILE itself** — loaded from disk, built, and simulated:
  it reproduces the golden exactly. (A separate gate checks its geometry
  matches the design; this one closes the gap by proving the *file* computes,
  since the file is what a user opens.)
* the other shipped artefacts — the `.grc` targets port 58950 and sizes its
  scopes below the burst so they actually paint, and the installed
  `kyttar.gru_demo_stim` produces a clip **identical** to `gru_stimulus.py`;
* **seven mutations proven to fail** — four offline (swapped word order, wrong
  weights, zero features, a saturating clip breaking the RMS bound) and three
  **on-chip** (swapped I/Q rails, a starved rendezvous arm, and the exact-
  baseline check that makes those two meaningful).

The on-chip rail-swap mutation is not hypothetical: it is the bug this example
actually hit. The complex `(Re, Im)` pair is **one** delivery into a shared
broker, not two independent ones, and driving it as two left `Im` stuck at 0 —
power silently became `re²`, every downstream stage still looked plausible, and
the clip classified 9/12 instead of exactly. See the INGRESS PROTOCOL note in
`gru_classifier.py`.

## Files

| File | What |
|---|---|
| `gru_classifier.kyt` | **the shipped design** — placed, routed, built, 102/120 cells |
| `gru_classifier.grc` | the GNU Radio Companion flowgraph (server port 58950) |
| `gru_classifier_golden.json` | the class stream the chip produces for the shipped stimulus |
| `gru_classifier.py` | the chain: topology, anchors, the on-chip runner, feature references |
| `gru_stimulus.py` | the shipped 4-segment stimulus |
| `build_kyt.py` | regenerates the `.kyt` from the pinned anchors |
| `gru_classifier_demo.py` | the headless end-to-end demo |
| `ml/` | the offline pipeline: signals, features, training, references |
