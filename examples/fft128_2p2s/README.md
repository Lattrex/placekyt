<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# FFT128 on the 2P2S board — a 128-point transform across a real chain

**Status: WORKING and VERIFIED BIT-EXACT end to end.** 200 samples driven into
chain A's head, 400 words out of its tail, every one equal to the whole
128-point transform, on the real placed + routed 4-die build — and **DRC-clean
against the board file itself**.

This is the `fft128_2die` design retargeted from an ad-hoc two-chip project
onto the **2P2S dev board** (`placekyt/resources/boards/dev2p2s.kdb`): four
dies in two parallel daisy-chains of two, which is hardware that exists.

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

This was asked directly, after the cell animation looked like chip 0 ran to
completion before chip 1 started. It is worth answering precisely, because the
question has two halves and they have different answers.

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
more. Anyone "fixing" this by tuning `run(events, rounds)` is chasing the wrong
thing.

What genuinely overlaps on hardware is **sample k+1 in die 0 against sample k
in die 1** — pipelining ACROSS samples. The shipped drive deliberately does not
do that, because a complex sample is a three-part transaction that must be
pumped to quiescence (below); that pacing is what makes the design work at all.
`test_within_one_trigger_the_dies_are_causally_sequential` pins the measurement
so that if a future change ever *does* produce the crossing word early, the
gate says so rather than silently passing.

### The animation was rendering it wrong — that part was a real bug

Both dies working every trigger, yet the GUI showed one then the other. The
fault was in the multi-chip refresh, which built its flash-step list by
**concatenating each chip's steps in chip order**:

```python
for cid in sorted(by_chip):
    m_steps += list(self._steps_from_events(by_chip[cid], cid))
```

The canvas replays that list in order, so chip 0's entire burst played before
chip 1's — exactly what was reported.

Sorting the merged steps by `time_ns` does **not** fix it, and this is the trap
worth recording: each chip keeps its **own sim clock**, and those clocks
**diverge** — measured above, die 1's clock runs **2.27×** die 0's, and the gap
grows every sample because die 1 does more work per sample. A strictly-later
clock never interleaves with a strictly-earlier one, so a global time sort
reproduces the same batched playback.

The fix interleaves on each chip's **progress through its own burst** —
round-robin across the per-chip step lists, so every die that did work in a
refresh lights up together, and a busier die keeps flashing after the others
drain. It is a rendering order only: it moves no data and changes no
arithmetic. See `SimController._interleave_chip_steps`, gated by
`test_the_animation_interleaves_the_dies_rather_than_batching_them` (which
includes teeth asserting the old concatenated order does not satisfy it).

Measured on a real 6-sample run's trace (24,489 chip-tagged events → 15,802
flash steps), which chip lights on each of the first 24 steps:

```
  OLD: 0 . 0 0 0 0 0 . 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0
  NEW: 0 1 . 1 0 1 0 1 0 1 0 1 0 1 . 1 0 1 0 1 0 1 0 1

  OLD (concatenate): chip 1's FIRST flash is step 4687 of 15802 — 29.7% in
  NEW (interleave):  chip 1's FIRST flash is step 1 of 15802  —  0.0% in
```

Under the old order chip 0 animated alone for the first 29.7% of the run,
which is precisely the "chip 0 works for a long time, chip 1 idle" that was
reported. Under the new order the dies alternate from the first step.

## Three LIVE-PATH defects the headless gates could not see

The headless suite drives `MultiChipSimEngine` directly and was green at
200/200 bit-exact. The **user path** — host the `.kyt`, run the `.grc` through
the real GR client stack — returned a stream of only two distinct values. The
first two faults are in the multi-chip bridge and the third is in the `.grc`
itself; all three are invisible to any test that does not go through a hosted
server.

**1. A chain that continues across the CARRIER WIRE resolved `out_tag=None`.**
`stream_targets` finds a chain's egress tag by walking **block → block within
one chip**. In this design the stream's input net is on chip 0 (die 0) while
the tagged egress net belongs to die 1 on chip 1, joined by an *inter-chip
wire* rather than a block-to-block net — so the walk never reaches it. The
tail's words *are* tagged on the fabric, so a `None` here made the host demux
drop every one of them: **data flowed on chip and the flowgraph showed
nothing.** Fixed by recovering the tag from the tail chip's own egress net
(`port_config._tail_egress_tag`), and only when the chain genuinely spans
chips, so no single-chip behaviour changes.

**2. The multi-chip demux kept only ONE tag of a complex pair.** A complex
exit cell emits I then Q **from one cell on tags `(out_tag, out_tag+1)`** —
measured at chain A's tail: `{7: 140, 8: 140}`. The single-chip path already
owns both tags; the multi-chip drain matched `d == out_tag` only, discarding
every Q word. That is the more dangerous of the two, because it does not look
like a failure: the stream arrives at half length with the imaginary part
gone, which for a transform reads as a plausible wrong answer.

> Note the second fault's shape: **only the I rail is wired to a net** (wiring
> a second net to the same port kills egress), so the fabric emits a tag the
> project graph never mentions. Detecting "is this egress complex?" from the
> nets alone therefore cannot work for a chip-scale complex block — it has to
> come from the terminal block's declared output registers.

**A gate lesson from the same episode.** The first version of the user-path
gate asserted the recovered stream "looks busy" (enough distinct values, enough
non-zero words). That assertion is **wrong for this flowgraph**: the `.grc`
drives two pure tones at exactly bins 9 and 37 of 128, so a *correct* transform
is nearly all zeros — measured, **3 distinct values over 768 words**. The
liveliness heuristic fails on a correct chain and would have been "fixed" by
weakening it. The gate now asserts **bit-exactness against the whole-transform
reference for the `.grc`'s own stimulus**, which cannot pass on a broken chain
and cannot fail on a working one.

**3. The `.grc` left `output_words` on `"auto"` for a VALUE-output chain.**
This was the last fault, and the one that kept the user-path gate `xfail`.
`output_words="auto"` ties **raw int16** output to `complex_in` — the
**bit-packing receiver** convention, where a slicer's decoded bit lives in the
word's LSB and Q15 scaling would crush it. This chain is the exact opposite
case: its output is a **Q15 value**, the transform's bins. So the sink emitted
raw ±30000 word floats while every consumer applied the documented q15/32768
convention, under which those **alias**:

| the chip's word | the sink emitted | decoded as q15 | should be |
|---|---|---|---|
| `0x399a` (14746) | `14746.0` | `round(14746 × 32768) & 0xFFFF` = `0x0000` | `0x399a` |
| `0x2ccd` (11469) | `11469.0` | `round(11469 × 32768) & 0xFFFF` = `0x8000` | `0x2ccd` |
| `0x0000` | `0.0` | `0x0000` ✓ | `0x0000` |

Only the **non-zero** bins alias — zero survives the aliasing untouched. This
`.grc` drives two pure tones, so a correct transform is nearly all zeros and
just **4 of 384** samples were wrong. The burst came back looking almost
right rather than obviously broken, which is why the earlier measurement read
as a strange re-framing (an apparent period 192) instead of a scaling bug.

The **display** told the same story from the other side: raw ±30000 against
the scope's `ymin/ymax` of −1/1 is a flat off-scale line, and the `.grc`'s own
comment already claimed the plotted stream was "at the q15/32768 scale".

Fixed in `gen_grc.py` with `output_words="q15"` — the same fix, and the same
failure class, as the LMS equalizer's missing-constellation report. Every
other value-output complex example in the tree (`fft_spectrum`,
`cordic_polar`, `complex_math`, `lms_equalizer`, `fm_transceiver`) already
set `"q15"`; the two FFT128 `.grc`s were the outliers, both carrying a stale
`output_words: 'False'` that GRC silently resolved back to the `"auto"`
default.

> **`fft128_2die` had the identical defect and no gate at all.** The two
> `gen_grc.py` files are the same file modulo comments, so the fault was
> cloned. It is fixed there too, and
> `test_fft128_2die_shipped_grc_user_path` now gates it — a fix proven on a
> sibling is not a fix proven here.

`test_fft128_2p2s_shipped_grc_user_path` now **passes**, asserting
bit-exactness of the full complex stream against the whole-transform
reference, that the four energy-bearing samples are non-zero (so a dead chain
cannot pass on the zeros alone), and that `server_repeat` loops the genuine
burst cleanly. Reverting the `.grc` to `"auto"` makes it fail with exactly the
4/384 signature above — the gate has teeth.

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
| `build_kyt.py` | Regenerates `fft128_2p2s.kyt` from the real place-and-route. |
| `gen_grc.py` | Regenerates `fft128_2p2s.grc`. |

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
      verification/tests/test_fft128_2p2s_example.py -q
```

- the design **places, routes and builds** on all four board dies, with the
  transform on chain A and both carrier links wired;
- it is **DRC-clean against `dev2p2s.kdb` itself**, and that check has teeth —
  a cross-chain link the carrier does not provide is REJECTED;
- the shipped `.kyt` **reloads to the verified design and rebuilds to the same
  bitstream** on every die, names its board, and leaves chain B empty;
- **200/200 samples bit-exact** through the real board, 73 of them non-zero,
  one complex sample per trigger, every trigger reaching quiescence;
- **chain B stays silent** — no words, no trace events — while chain A runs;
- **die concurrency**: both dies do real work on all 200 triggers, and the
  within-trigger causal ordering is pinned with its measured 99.9% figure;
- the **animation interleaves** the dies rather than concatenating them, with
  teeth against the old order;
- **INV-4 teeth**: a single corrupted word, swapped rails and a dropped sample
  each FAIL the comparison;
- the **un-paced drive** is held as a gate, so the root cause cannot be
  re-introduced by "simplifying" the pumps away.

The arithmetic gates live separately in
`verification/tests/test_fft128_split.py` (the composition identity, both
dies' cell contracts, the fold strides, the declared anchors).
