<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# FFT128 on the 2P2S board — a 128-point transform across a real chain

A **128-point FFT split across two physical dies** on the **2P2S dev board**
(`placekyt/resources/boards/dev2p2s.kdb` — four dies in two parallel
daisy-chains of two). Chain A's head runs stage 0, its tail runs stages 1..6,
and the board's own on-carrier series link joins them.

200 samples in, 400 words out, **every one bit-exact** against the whole
128-point transform on the real placed and routed build — and **DRC-clean
against the board file itself**.

```
  chain A                                                    (carries the transform)
  ┌──────────────────────┐            ┌──────────────────────┐
  │ chip 0  A0 (head)    │            │ chip 1  A1 (tail)    │
  │ FFT128Die0           │            │ FFT128Die1           │
  │ stage 0 — the        │            │ stages 1..6          │
  │ period-64 octant     │            │ (delays 32/16/…/1)   │
  │ fold                 │            │                      │
  └──────────────────────┘            └──────────────────────┘
   x16_in ─▶ die0 ─▶ x16_out ════════▶ x16_in ─▶ die1 ─▶ x16_out
      ▲                  the board's ON-CARRIER series link       │
   chainA_in                (the FPGA never sees it)          chainA_out
      │                                                           ▼
  ════╪═══════════════════════  FPGA  ═══════════════════════════╪════
      │                                                           │
  chain B    chip 2  B0 ════════════▶ chip 3  B1        (wired, and IDLE)
```

## Run it

### 1. Headless — the fastest way to see it work

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python \
    examples/fft128_2p2s/fft128_2p2s_demo.py --samples 200
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

RESULT: EXACT — the N=128 transform computed across CHAIN A of the
        2P2S board, 200 samples word-for-word equal to whole(x).
```

Useful flags while debugging:

- `--concurrency` — the per-die work + clock table shown above. This is the
  measurement behind the concurrency section.
- `--trace` — the per-trigger table: what each trigger emitted, what the
  carrier link should be carrying at that sample, and the run's
  rounds/events/quiescence.
- `--pattern batched` — **reproduces the failure on demand.** The un-paced
  drive, kept so the trap is demonstrable rather than folklore.
- `--samples N` — fewer than ~140 only exercises the zero-fill transient. The
  demo says so out loud rather than reporting a vacuous pass.

### 2. In placeKYT — open, inspect, watch both dies work

```bash
.venv/bin/python placekyt/main.py
```

**File → Open** → `examples/fft128_2p2s/fft128_2p2s.kyt`. You get the board's
four dies: chip 0 carrying die 0's fold, chip 1 carrying die 1's spine, chips
2 and 3 empty, and both carrier links wired. Select a block to highlight its
routes; run the simulation to watch the cell animation — **both dies now
animate together** (see the concurrency section for why they used to appear
one-after-the-other).

### 3. Through GNU Radio — the live vehicle

Host the chips (terminal 1): open the `.kyt` as above, then **Simulation → Run
as GNURadio Server**. The project has four chips, so placeKYT hosts the
**multi-chip** server (the status bar says `… (multi-chip)`). Note the printed
port.

Drive it (terminal 2):

```bash
gnuradio-companion examples/fft128_2p2s/fft128_2p2s.grc
```

Set `server_port` to the printed port — the shipped value is **58950**,
placeKYT's default. A `server_port` of `0` makes `kyttar_source` silently
no-op: it never connects, and the window stays blank with no error.

> **First-time GR setup:** this example uses the markers `kyttar_fft128_die0`
> and `kyttar_fft128_die1`. Install the OOT so `gnuradio-companion` sees them:
> `cd gr-kyttar && ./install.sh` (needs sudo for the system dirs). Until then
> they show as red **Missing Block**.

The flowgraph carries only `stream_id` (`"fft"`); placeKYT owns which chip,
port, hop and tag that maps to, resolved from the placed design.

## Why chain A, and why this die order

The board gives the FPGA exactly two handles per chain: the chain HEAD's
`x16_in` and the chain TAIL's `x16_out`. A stage-boundary cut of a
feed-forward pipeline needs **one crossing in one direction**, and the board
provides exactly that as an on-carrier series link. So the transform's input
enters the chain head, the partially-transformed stream crosses the carrier
link, and the bins leave the chain tail — the design's dataflow and the
board's wiring are the same shape. Putting die 0 on the TAIL would require the
stream to run backwards over a link the carrier does not provide.

**Chain B is deliberately left empty**, and that is a feature of the retarget
rather than waste: the board's two chains are independent, a 2-die design
occupies one of them, and the free chain is where a second instance would go.
`test_chain_b_stays_silent` asserts that driving chain A puts not one word —
and not one trace event — into chain B.

> The board also supports a **1P4S** re-chaining, where the FPGA passes chip
> 2's output into chip 3's input to form one 4-long chain. This design does not
> need it: the transform splits into exactly two dies, so a 2-long chain is the
> right shape and the second chain stays free.

## Why it is split at all

N=128 does not fit ONE die, and not for want of area: its 7-stage ctl/out
spine needs **14 rows in a single column** against a 12-row array. The cut is
after **stage 0**, which is measured rather than balanced — cutting in the
middle looks tidier and does not place at all. Die 1 is the same shape as the
verified FFT64 and *computes* FFT64: the DIF angle identity
`stage_table(128, s+1) == stage_table(64, s)` holds word for word.

Correctness reduces to `whole(x) == die1(die0(x))`, which
`verification/tests/test_fft128_split.py` asserts word for word. That is the
arithmetic. **This example is the other half: the transport.**

## Do the dies run concurrently? — the measured answer

Watching the cell animation it can look as though chip 0 runs to completion
before chip 1 starts. The question is worth answering precisely, because it has
two halves and they have different answers.

### The engine does NOT batch the dies

Measured per-die trace events, per trigger, over a 200-sample run
(`--concurrency` prints this table):

```
  triggers where BOTH dies did work: 200/200
    trig   die0 ev   die1 ev    die0 clock    die1 clock
     194      1107      2877       2293222       5173453
     195      1107      2967       2303376       5202354
     196      1107      2793       2313530       5228081
     197      1107      2882       2323684       5255452
     198      1107      2879       2333838       5282737
     199      1107      2948       2343992       5311385
```

Every trigger has both dies doing real work. Neither die is ever idle for a
stretch of triggers while the other runs. **There is no batching**: the model
does not run die 0 to completion over the whole stimulus and then hand die 1 a
block of results. `test_the_dies_are_concurrent_across_the_run` holds this.

### Within ONE trigger the dies are causally sequential — and that is correct

Inside a single sample's settle, the dies genuinely do run one then the other.
Measured at single-round granularity, past the transform's latency:

```
  WRITE_i round 0: c0=   10 [1506672..1506712]   c1=    0
  WRITE_q round 0: c0=   10 [1506722..1506762]   c1=    0
  JUMP    round 0: c0= 1209 [1506772..1518871]   c1=    0
  JUMP    round 1: c0=    0                      c1= 2877 [3481344..3508558]
  JUMP    round 2: QUIESCENT
```

The reason is **causal, not a scheduler artifact**. Die 0's crossing word for
a sample is the very LAST thing die 0 produces for it: its egress reaches the
port cell (9, 0) at event **1208 of a 1209-event burst (99.9% through)**. Die 1
therefore *cannot* start on that sample any earlier — there is nothing yet to
start on.

The corollary matters, because it rules out the obvious wrong fix: **shrinking
the per-round event budget does not create overlap.** Measured at budgets of
400, 200 and 60 events per chip per round, the number of rounds in which both
dies advanced was **10 in every case** — one handoff round per sample, never
more. Tuning `run(events, rounds)` does not change it.

What genuinely overlaps on hardware is **sample k+1 in die 0 against sample k
in die 1** — pipelining ACROSS samples. The shipped drive deliberately does not
do that, because a complex sample is a three-part transaction that must be
pumped to quiescence (below); that pacing is what makes the design work at all.
`test_within_one_trigger_the_dies_are_causally_sequential` pins the measurement
so that if a future change ever *does* produce the crossing word early, the
gate says so rather than silently passing.

### How the animation shows two dies at once

Each chip keeps its **own sim clock**, and those clocks **diverge**: die 1's
runs about **2.27×** die 0's here, and the gap grows every sample because die 1
does more work per sample. That has a direct consequence for the cell animation
— a global sort by timestamp would replay one die's whole burst before the
other's, because a strictly-later clock never interleaves with a strictly-earlier
one.

The animation therefore interleaves on each chip's **progress through its own
burst** rather than on absolute time: every die that did work in a refresh lights
up together, and a busier die keeps flashing after the others drain. It is a
rendering order only — it moves no data and changes no arithmetic.

So what you see in the animation is both dies alternating from the first step,
which is what the measured event timeline above says is really happening.

## Contracts a hosted (GNU Radio) run depends on

Three settings must be right for the live path — hosting the `.kyt` and running
the `.grc` through GNU Radio — to deliver this chain's stream. They are already
correct in the shipped files; they are listed because anyone building a *new*
cross-chip flowgraph has to set them too.

* **The egress net carries an explicit `out_tag`.** A chain that continues
  across the carrier wire cannot have its tag inferred by walking blocks within
  one chip, so the tail die's egress net names the tag directly. Without it the
  host demux has nothing to match and drops the stream.
* **A complex exit is ONE cell emitting a tag PAIR.** The exit cell emits
  `out_i` on `out_tag` and `out_q` on `out_tag + 1`; only the I rail is
  net-wired. A demux that keeps a single tag silently delivers half a stream.
* **Value-output chains set `output_words = "q15"`.** The `"auto"` setting ties
  the raw-word convention to a complex *input*, which is right for a modem
  returning packed bits and wrong for a chain returning VALUES. This example
  returns transform words, so it declares `"q15"` explicitly.

## From chip words to a spectrum plot

What leaves the chip is not a spectrum, and plotting it directly gives a plot
that looks plausible and reads wrong. **Four** transformations separate the two,
and every one of them is load-bearing:

| # | What | Why the plot is wrong without it |
|---|------|----------------------------------|
| 1 | **De-interleave** the complex pair | The chain tail is a COMPLEX exit cell: `out_i` then `out_q` from one cell, so the stream carries **two float words per bin**. A time sink draws one bin's energy as two adjacent samples. |
| 2 | **Strip the 127-sample latency** | The first 127 complex outputs of a burst are the zero-initialised pipeline's startup values, not a frame. |
| 3 | **Un-reverse** the DIF slot order | Slot *k* carries bin `bit_reverse_7(k)`. Plotting slots is a **scrambled spectrum that still looks plausible** — clean lines, wrong frequencies. |
| 4 | **fftshift** | Natural order runs 0 → +fs/2 and then *jumps* to −fs/2 → 0. No single linear axis can label that; rolling by N/2 makes the vector monotonic so `set_x_axis(-samp_rate/2, bin_hz)` labels every point. |

The fix is the one `examples/fft_spectrum` already ships, with N and the
complex de-interleave being what differ:

```
kyttar_sink ─▶ spectrum (de-interleave, strip, un-reverse, fftshift, |z|²)
            ─▶ to_db (10·log10) ─▶ qtgui_vector_sink_f
                                   x_start = -samp_rate/2, x_step = bin_hz,
                                   x_units = "Hz"
```

`fft128_2p2s_spectrum.py` and `fft128_2p2s_to_db.py` are the display blocks,
embedded verbatim into the `.grc` by `gen_grc.py` so the readable file and the
flowgraph cannot drift.

### What the plot reads now

At the repo convention `samp_rate = 32000` and N = 128 each bin is
**250 Hz** wide. The shipped stimulus is two ON-BIN tones:

| tone | natural bin | chip SLOT (`bit_reverse_7`) | centred index | **frequency** | power | dBFS |
|---|---|---|---|---|---|---|
| A (amplitude 0.45) | 9 | 72 | 73 | **+2250 Hz** | 0.2025 | −6.93 |
| B (amplitude 0.35) | 37 | 82 | 101 | **+9250 Hz** | 0.1225 | −9.12 |

Every other one of the 128 points is **exactly 0.0** — an on-bin tone leaks
nowhere — so the plot is two clean lines on the −90 dBFS floor. Measured on
the real 4-die board *and* on the trace the vector sink is actually drawn
with, tapped live through the hosted server.

The stimulus scope (`input |x[n]| (two tones)`) is unchanged: it is a
time-domain signal, so a time sink is the right sink for it.

### Why the display drops each burst's ragged tail

`burst_len` is 384 while `latency + 2*n_fft` is **383**, so every burst ends
with exactly **one** leftover sample. A frame reader that kept consuming across
the boundary would build its next "frame" from that one real sample plus 127 of
the next burst's zero-fill — an all-zero spectrum, and with `server_repeat`
looping the batch it would recur forever in a regular good / good / blank cycle.

The display block therefore drops the ragged tail at each burst boundary. If you
adapt this display for a different `burst_len`, keep that rule.

## The drive is part of the design

A complex sample is a **three-part transaction**:

    WRITE xi  ─▶  WRITE xq  ─▶  JUMP     (one trigger, for the pair)

On the multi-chip path each part must be **pumped to quiescence before the
next is injected**. Queue all three back to back and the single-outstanding
input handshake is overrun; the system makes no forward progress and the run
looks exactly like a livelock.

| drive shape | words out of the chain tail (12 samples) |
|---|---|
| all three parts queued, then one settle run | **0** of 24 |
| two operand WRITEs queued, then JUMP + settle | **0** of 24 |
| one WRITE + one JUMP *per word* (the generic head path) | 48 of 24 — **double-fires** |
| **WRITE, pump, WRITE, pump, JUMP, settle** (shipped) | **24** of 24 ✅ |

(Measured during the original bring-up — see the knowledge-base lessons log;
the shipped gate asserts the unpaced drive delivers strictly fewer words than
the paced one.)

Two things are worth separating, because they are different mistakes:

1. **The pumps are load-bearing**, not defensive padding.
2. **A bigger budget is not a safer budget.** `run(events, rounds)` is
   *events-per-chip-per-round × rounds*. The shipped budgets
   (`PUMP = (60_000, 5)`, `SETTLE = (200_000, 50)`) are derived from what a
   sample actually has to do.

`--pattern batched` reproduces the failure on demand, and
`test_the_unpaced_drive_is_what_stalls` holds it as a gate so the pumps cannot
be "simplified" away.

## Files

| File | What it is |
|------|------------|
| `fft128_2p2s.kyt` | The 4-die board design — **open this in placeKYT**. Both dies placed and routed on chain A, both carrier links wired, the board named. |
| `fft128_2p2s.grc` | The GNU Radio flowgraph — drives chain A through the multi-chip server. Open in `gnuradio-companion`. |
| `fft128_2p2s.py` | The design + the drive. `build_2p2s()`, `open_engine()`, `drive()` — shared by the demo, the `.kyt` writer and the gate, so they cannot drift. |
| `fft128_2p2s_demo.py` | **The debugging vehicle.** Per-trigger yield, the carrier link's traffic, the first non-quiescent trigger, the per-die concurrency table, then a word-for-word compare. |
| `fft128_2p2s_spectrum.py` | **The display contract.** Interleaved I/Q chip words → a centred 128-bin POWER vector: de-interleave, strip the 127-sample latency, un-reverse the DIF slots, fftshift. Embedded verbatim into the `.grc`. |
| `fft128_2p2s_to_db.py` | Per-bin power → dBFS for the log spectrum plot. Embedded verbatim into the `.grc`. |
| `build_kyt.py` | Regenerates `fft128_2p2s.kyt` from the real place-and-route. |
| `gen_grc.py` | Regenerates `fft128_2p2s.grc`, reading the two display blocks from the files above so they cannot drift. |

## Reading the output

**The output is in BIT-REVERSED bin order** (standard DIF, no reorder buffer):
slot *k* of each 128-sample frame carries bin `bit_reverse_7(k)` — slot 0 →
bin 0, slot 1 → bin 64, slot 2 → bin 32, slot 3 → bin 96. The map is an
involution, so applying it twice returns natural order.

Scale is FFT/128. Latency is **127 samples** (64 from die 0, 63 from die 1),
so the first 127 outputs are the deterministic startup values of the
zero-initialised pipeline — not a bug, and the reason a short run proves
nothing.

**Bin → Hz.** A bin index is dimensionless; the array is asynchronous and has
no clock, so the sample rate is **declared** by your stimulus (`samp_rate` in
the `.grc`). Bin *k* of *N* at rate *fs* is `k*fs/N`, with bins at or above
*N*/2 being the negative frequencies `(k-N)*fs/N`. At the shipped
`samp_rate = 32000` each bin is **250 Hz**. `fft128_2p2s.py` publishes this as
`bin_hz()`, `bin_to_hz()`, `fftshift_order()` and `axis_hz()`, and the plot,
the README and the gates all cite that one statement.

**A single die's output is not frequency bins.** Die 0 emits a *partially
transformed* stream; asking either die for a bin map raises rather than
returning a plausible-looking wrong answer.

## What is verified

```
$ QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest \
      verification/tests/test_fft128_2p2s_example.py -q
```

The suite covers the design (places, routes and builds on all four dies;
DRC-clean against `dev2p2s.kdb`; the shipped `.kyt` reloads to the same
bitstream), the data (**200/200 samples bit-exact** through the real board, 73
non-zero, chain B silent throughout, both dies working on every trigger), and
**the display as a separate claim** — the drawn trace, tapped live through the
hosted server, is the same two-line spectrum at +2250 Hz and +9250 Hz in every
frame.

Each claim carries mutation coverage: a corrupted word, swapped rails, a dropped
sample, a missing un-reversal, a wrong-*N* map, a missing fftshift and an
unstripped latency all fail their gate.

The arithmetic gates live separately in
`verification/tests/test_fft128_split.py` (the composition identity, both
dies' cell contracts, the fold strides, the declared anchors).
