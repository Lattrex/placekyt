<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# FFT128 across TWO DIES — a 128-point transform that does not fit one chip

**Status: WORKING and VERIFIED BIT-EXACT end to end.** 200 samples driven into
chip 0, 400 words out of chip 1, every one equal to the whole 128-point
transform, on the real placed + routed two-chip build. The crossing that was
previously quarantined as livelocked is proven, and the fault has been found —
see [What was actually wrong](#what-was-actually-wrong) below, because the
answer is a genuine trap and not a one-line bug.

```
  chip 0                          chip 1
  ┌──────────────────────┐        ┌──────────────────────┐
  │ FFT128Die0           │        │ FFT128Die1           │
  │ stage 0 — the        │        │ stages 1..6          │
  │ period-64 octant     │        │ (delays 32/16/…/1)   │
  │ fold, delay 64       │        │ 84 cells             │
  │ 30 cells             │        │                      │
  └──────────────────────┘        └──────────────────────┘
   x16_in ─▶ die0 ─▶ x16_out ────▶ x16_in ─▶ die1 ─▶ x16_out
                        the crossing:              the transform's bins
                        one complex stream,        (bit-reversed, FFT/128)
                        one direction
```

## Why it is split at all

N=128 does not fit ONE die, and not for want of area: its 7-stage ctl/out
spine needs **14 rows in a single column** against a 12-row array, and the
spine height is not negotiable. `FFT128Block` raises rather than emitting an
unroutable layout. The supported topology is a **stage-boundary split**.

The cut is after **stage 0**, which is measured rather than balanced. Cutting
in the middle (70/44 cells) looks tidier and *does not place at all* — three
chains of 30, 24 and 16 cells around a six-row spine fail at every spine
column, at 59% array occupancy. Shape is binding, not area. Cutting after
stage 0 places, and its imbalance is a feature:

- **die 0** is the one stage that could not be anything else — the period-64
  octant fold, which exists only at N=128;
- **die 1** is the same shape as the verified FFT64, and it *computes* FFT64:
  the DIF angle identity `stage_table(128, s+1) == stage_table(64, s)` holds
  word for word. It inherits geometry already proven on a chip.

So the only genuinely new things in the design are die 0 and the crossing.

## The correctness argument

R2SDF stages are a pure feed-forward pipeline — the only feedback is *inside*
a stage, from its own `out` back to its own `ctl` — so a stage-boundary cut
needs exactly **one** complex stream crossing, in one direction. Correctness
reduces to

    whole(x) == die1(die0(x))

which `verification/tests/test_fft128_split.py` asserts word for word over the
startup transient plus three full frames at three seeds, with INV-4 teeth
(wrong boundary / crossing forgotten / dies swapped all FAIL it). That is the
arithmetic. **This example is the other half: the transport.**

## What was actually wrong

The two-die design was quarantined as `needs_human` with the report *"0 of 520
words, livelocks from trigger 1"*, after three real inter-chip **build-path**
defects had already been found and fixed. Those three fixes were correct and
necessary. The remaining fault was not in the design, the placement, the
routes, the build or the crossing — **it was in the DRIVE.**

A complex sample is a **three-part transaction**:

    WRITE xi  ─▶  WRITE xq  ─▶  JUMP     (one trigger, for the pair)

On the multi-chip path each part must be **pumped to quiescence before the
next is injected**. Queue all three back to back and the single-outstanding
input handshake is overrun; the system makes no forward progress and the run
looks exactly like a livelock. Same bitstream, same crossing, same everything
— only the pacing differs:

| drive shape | words out of chip 1 (12 samples) |
|---|---|
| all three parts queued, then one settle run | **0** of 24 |
| two operand WRITEs queued, then JUMP + settle | **0** of 24 |
| one WRITE + one JUMP *per word* (the generic head path) | 48 of 24 — **double-fires** |
| **WRITE, pump, WRITE, pump, JUMP, settle** (shipped) | **24** of 24 ✅ |

Two things are worth separating here, because they are different mistakes:

1. **The pumps are load-bearing**, not defensive padding.
2. **A bigger budget is not a safer budget.** `run(events, rounds)` is
   *events-per-chip-per-round × rounds*. The original investigation reached
   for `run(400_000, 4000)` — an enormous number that is the wrong *shape*:
   it lets a single round churn far past the point where the missing pump
   would have been noticed, and it takes hours. The shipped budgets
   (`PUMP = (60_000, 5)`, `SETTLE = (200_000, 50)`) are derived from what a
   sample actually has to do, and a 200-sample run finishes in minutes.

The honest summary is that **the fault was never localised to the crossing at
all** — the crossing was innocent, and it was being blamed because the two
dies had been verified separately and the driver had not. Verifying the parts
separately is what made this tractable, but the *driver* is a part too.

### What the crossing really carries

Watched at chip 1's landing cell, at a trigger past die 0's delay-64 latency:

```
WRITE 0x63c1 -> reg 1, data 0xfee6     die 0's out_i
WRITE 0x63c2 -> reg 2, data 0x37b4     die 0's out_q
JUMP  0x73cc -> entry 12               ONE trigger, after BOTH writes
```

and `(0xfee6, 0x37b4)` is exactly what die 0's output stream carries at that
sample. The boundary is a **transparent wire**: die 0's exit cell emits both
rails and the JUMP carrying a hop composed *past* the boundary, and they
arrive on die 1 in that order. It is not a value relay that re-triggers per
word — which matters, because a per-word trigger would fire die 1 twice per
sample on a half-primed operand pair. `test_fft128_2die_example.py` pins this
packet shape.

## Files

| File | What it is |
|------|------------|
| `fft128_2die.kyt` | The two-chip design — **open this in placeKYT**. Both dies placed at their declared anchors, all six nets routed, the crossing wired. Reloads to a byte-identical bitstream (gated). |
| `fft128_2die.grc` | The GNU Radio flowgraph — drives the pair through the multi-chip server. Open in `gnuradio-companion`. |
| `fft128_2die.py` | The design + the drive. `build_two_die()`, `open_engine()`, `drive()` — shared by the demo, the `.kyt` writer and the gate, so they cannot drift. |
| `fft128_2die_demo.py` | **The debugging vehicle.** Drives the design and reports where the words stop: per-trigger yield, the crossing's traffic, the first trigger that fails to reach quiescence, then a word-for-word compare. |
| `build_kyt.py` | Regenerates `fft128_2die.kyt` from the real place-and-route. |
| `gen_grc.py` | Regenerates `fft128_2die.grc`. |

## Run it

### 1. Headless — the fastest way to see it work

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python \
    examples/fft128_2die/fft128_2die_demo.py --samples 200
```

Expected tail:

```
  WORD ACCOUNTING
    die 1 egress   : 400 words (200 samples) of 400 expected
    per-trigger yield: [2] (a healthy run is [2] — out_i and out_q per trigger)
    first trigger emitting NOTHING: none
    first non-quiescent trigger: none

  CORRECTNESS vs the whole-transform reference
    BIT-EXACT — 200/200 samples, 73 of them non-zero

RESULT: EXACT — the N=128 transform computed across TWO DIES,
        200 samples word-for-word equal to whole(x).
```

Useful flags while debugging:

- `--trace` — the per-trigger table: what each trigger emitted, what the
  crossing should be carrying at that sample, and the run's rounds/events/
  quiescence.
- `--pattern batched` — **reproduces the failure on demand.** The un-paced
  drive, kept so the trap is demonstrable rather than folklore.
- `--samples N` — fewer than ~140 only exercises the zero-fill transient. The
  demo says so out loud rather than reporting a vacuous pass.

### 2. In placeKYT — open, inspect, step

```bash
.venv/bin/python placekyt/main.py
```

**File → Open** → `examples/fft128_2die/fft128_2die.kyt`. You get two chips:
chip 0 carrying die 0's 30-cell fold, chip 1 carrying die 1's 84-cell spine,
and the crossing wired chip0.`x16_out` → chip1.`x16_in`. Select a block to
highlight its routes; run the simulation to watch the cell animation.

### 3. Through GNU Radio — the live vehicle

Host the chips (terminal 1): open the `.kyt` as above, then **Simulation → Run
as GNURadio Server**. The project has two chips, so placeKYT hosts the
**multi-chip** server (the status bar says `… (multi-chip)`). Note the printed
port.

Drive it (terminal 2):

```bash
gnuradio-companion examples/fft128_2die/fft128_2die.grc
```

Set `server_port` to the printed port — the shipped value is **58950**,
placeKYT's default. A `server_port` of `0` makes `kyttar_source` silently
no-op: it never connects, and the window stays blank with no error.

> **First-time GR setup:** this example adds two markers (`kyttar_fft128_die0`,
> `kyttar_fft128_die1`). Install the OOT so `gnuradio-companion` sees them:
> `cd gr-kyttar && ./install.sh` (needs sudo for the system dirs). Until then
> they show as red **Missing Block**.

The flowgraph carries only `stream_id` (`"fft"`); placeKYT owns which chip,
port, hop and tag that maps to, resolved from the placed design. The scope
shows the transform's output words off chip 1's `x16_out`.

## Reading the output

**The output is in BIT-REVERSED bin order** (standard DIF, no reorder buffer):
slot *k* of each 128-sample frame carries bin `bit_reverse_7(k)` — slot 0 →
bin 0, slot 1 → bin 64, slot 2 → bin 32, slot 3 → bin 96. The map is an
involution, so applying it twice returns natural order.

Scale is FFT/128. Latency is **127 samples** (64 from die 0, 63 from die 1),
so the first 127 outputs are the deterministic startup values of the
zero-initialised pipeline — not a bug, and the reason a short run proves
nothing.

**A single die's output is not frequency bins.** Die 0 emits a *partially
transformed* stream; asking either die for a bin map raises rather than
returning a plausible-looking wrong answer.

## What is verified

```
$ QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest \
      verification/tests/test_fft128_2die_example.py -q
```

- the design **places, routes and builds** as two chips (all six nets routed,
  one crossing, both dies landing off the port cell so the routed-input path
  is genuinely exercised);
- the shipped `.kyt` **reloads to the verified design and builds** — same
  dies, same chips, same declared anchors, same six routed nets, same
  crossing, same bitstream size on both chips;
- **200/200 samples bit-exact** through the real two-chip system, 73 of them
  non-zero, one complex sample per trigger, every trigger reaching quiescence;
- the crossing **carries the complex pair and one trigger** — asserted at a
  trigger where die 0 is emitting real data;
- **INV-4 teeth**: a single corrupted word, swapped rails and a dropped sample
  each FAIL the comparison;
- the **un-paced drive** is held as a gate, so the root cause cannot be
  re-introduced by "simplifying" the pumps away;
- the **build is deterministic** and die 1's egress is a shortest path of the
  pinned length — see below.

### A second bug found on the way, worth knowing repo-wide

The `.kyt` gate above ("rebuilds to the verified bitstream") failed on roughly
a coin-flip while the design was provably correct. Chasing that rather than
loosening it found that **the build was not deterministic.**

`cpsat_router` ran an 8-worker CP-SAT portfolio with **no fixed random seed**,
and its objective (minimise active cell-faces; sharing is free) has *ties* by
construction. So a design with several equally-optimal routings got whichever
one the workers reached first. Measured here: five builds with identical
placement, occupancy, transit cells, faces and net order — and the routing
BFS returning byte-identical results — yet **three distinct 17-cell routes**
for die 1's egress corner, the one net with tied optima. Every other net was
stable, and every variant ran bit-exact on chip.

So it was never a correctness bug. What it did was make builds
irreproducible, and silently defeat any gate that compares bitstreams. It is
**fixed at the source** — the solver now sets `random_seed` and
`interleave_search`, which constrain *which* optimum is returned and never how
good it is. Six builds now produce identical routes and identical bitstreams
on both chips, so `test_the_build_is_deterministic` holds the property and the
`.kyt` gate is a real check again rather than a race.

The arithmetic gates live separately in
`verification/tests/test_fft128_split.py` (the composition identity, both
dies' cell contracts, the fold strides, the declared anchors).

### The HOSTED user path (added 2026-08-25)

```
$ KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen .venv/bin/python \
      -m pytest verification/tests/test_examples_grc_userpath.py \
      -k fft128_2die -q          # run user-path gates STANDALONE (port 58950)
```

This example previously had **no user-path gate**, and it carried a real
defect that only the hosted path could see: the `.grc` left `output_words` on
`"auto"`, which ties **raw int16** output to `complex_in` — the *bit-packing
receiver* convention. This chain's output is a **Q15 value** (the transform's
bins), so the sink emitted raw ±30000 word floats while every consumer applied
the documented q15/32768 convention, under which raw words **alias**
(`14746.0 → 0x0000`, `11469.0 → 0x8000`). Zero is a fixed point of that
aliasing, and this `.grc` drives two pure tones, so only the **4 of 384**
energy-bearing samples were wrong — the burst looked nearly right. On the
display side the same stream is a flat off-scale line against the scope's
`-1..1` axis.

Fixed in `gen_grc.py` with `output_words="q15"`. The same fault was present in
the sibling `examples/fft128_2p2s` (the two generators are the same file modulo
comments); see INV-42 and that example's README for the full write-up.
