<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Gain — HARDWARE streaming demo

The **hardware** counterpart to [`../gain/`](../gain/). Two independent gain
blocks on one chip (`gain_hw.kyt`), each driven by its own **continuous live
sine** instead of a bounded vector burst, so real-time samples stream through
the Kyttar dev-kit board.

## How this differs from the sim `gain/` demo

| | `gain/` (simulator) | `gain_hw/` (hardware) |
|---|---|---|
| Design | one gain block (`gain.kyt`) | two gain blocks, streams a/b (`gain_hw.kyt`) |
| Stimulus | `vector_source` (one fixed burst, `repeat`) | two `sig_source` sines (continuous) |
| Data flow | **batch** — one `process_batch` RPC per burst | **streaming** — samples flow continuously |
| Why | the sim is ~7.6k samples/s, too slow to stream | the board runs at USB speed, so it streams |

**The Kyttar source/sink blocks are identical in both.** They auto-detect batch vs
streaming from the placeKYT **server** (it reports its mode over the wire): simulator →
batch, hardware → streaming. You never set a mode on the blocks — you only toggle
**Hardware Mode** in placeKYT. Same blocks, same workflow, the plumbing adapts.

## Run it (two terminals)

**1. Host the chip on hardware** (terminal 1):

```bash
.venv/bin/python placekyt/main.py examples/gain_hw/gain_hw.kyt
```

Then in placeKYT:
1. **Hardware → Check Connection** — the `HW:` status (bottom-right) should turn green
   "connected".
2. **Hardware → Hardware Mode** (toggle on) — programs the design onto the board; `HW:`
   shows "ON (streaming)".
3. **Simulation → Run as GNURadio Server** — binds port **58950**.

> Prerequisite: a Kyttar dev-kit board with its gateware flashed. The dev-kit
> gateware is not part of this release; without a board, run the simulator
> `gain/` demo instead — the flowgraph works there too (batch mode).

**2. Drive it with the live flowgraph** (terminal 2):

```bash
gnuradio-companion examples/gain_hw/gain_hw.grc
```

Press **▶ Run**. The time sink shows both streams' **input sines** vs their
**gained outputs**, live and continuous.

## What you'll see

Each output is its input scaled by that chip cell's gain **coefficient**. The
`gain_hw.kyt` design programs the coefficients into the two gain cells on load.
Because the gain is **run-time reprogrammable** (a WRITE to the coefficient
cell), the multipliers change on the fly.

## Live gain sliders (sim AND hardware)

The flowgraph's **gain A / gain B** sliders retune the RUNNING fabric — no reflash,
no rebuild. Dragging a slider fires the block's `set_gain` callback, which pushes the
new value to the placeKYT server (`set_grc_params`); the server WRITEs the Q15
coefficient word straight into that block's cell — the simulator injects the
IDENTICAL WRITE word the hardware path sends over USB, so the two backends behave
the same. Each burst's header also carries the current values, so a missed push
self-heals on the next Run.

Each `kyttar_gain` in the .grc names its placeKYT block explicitly
(`block_name: "gain"` / `"gain_2"`). This is REQUIRED when a design has several
blocks of one type: GRC's code-generation CONSTRUCTION order is not the .grc walk
order, so the automatic name matching can pair same-type blocks swapped — slider A
would retune cell B. With `block_name` set, each slider addresses its own cell,
verbatim (gate: `placekyt/tests/test_live_coeff_writes.py`).

A block is live-tunable when it is single-cell and stores the param as a same-named
data word (`engine.port_config.live_coeff_writes` resolves the map at server start).

## Notes

- `samp_rate` (default 4000) sets the streaming rate into the chip. Keep it modest for a
  legible plot; the USB link and board can handle far more.
- The design is the flowgraph's exact counterpart: two `kyttar` source→gain→sink
  triples addressing `block_name` "gain" and "gain_2".
