<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Gain — HARDWARE streaming demo

The **hardware** counterpart to [`../gain/`](../gain/). Same gain block on the same
`gain.kyt` chip design, but driven by a **continuous live sine** instead of a bounded
vector burst, so real-time samples stream through the **real Kyttar** on the ZTEX board.

## How this differs from the sim `gain/` demo

| | `gain/` (simulator) | `gain_hw/` (hardware) |
|---|---|---|
| Stimulus | `vector_source` (one fixed burst, `repeat`) | `sig_source` → `throttle` (continuous sine) |
| Data flow | **batch** — one `process_batch` RPC per burst | **streaming** — samples flow continuously |
| Why | the sim is ~7.6k samples/s, too slow to stream | the board runs at USB speed, so it streams |

**The Kyttar source/sink blocks are identical in both.** They auto-detect batch vs
streaming from the placeKYT **server** (it reports its mode over the wire): simulator →
batch, hardware → streaming. You never set a mode on the blocks — you only toggle
**Hardware Mode** in placeKYT. Same blocks, same workflow, the plumbing adapts.

## Run it (two terminals)

**1. Host the chip on hardware** (terminal 1):

```bash
.venv/bin/python placekyt/main.py examples/gain/gain.kyt
```

Then in placeKYT:
1. **Hardware → Check Connection** — the `HW:` status (bottom-right) should turn green
   "connected".
2. **Hardware → Hardware Mode** (toggle on) — programs the design onto the board; `HW:`
   shows "ON (streaming)".
3. **Simulation → Run as GNURadio Server** — binds port **58950**.

> Prerequisite: the devkyt gateware `.bit` must be flashed to the ZTEX
> (`devkyt/fpga/scripts/flash.sh`). See `devkyt/`.

**2. Drive it with the live flowgraph** (terminal 2):

```bash
gnuradio-companion examples/gain_hw/gain_hw.grc
```

Press **▶ Run**. The time sink shows the **input sine** (top) vs the **gained output**
(bottom), live and continuous.

## What you'll see

The output is the input scaled by the chip's gain **coefficient**. The `gain.kyt` design
programs its own coefficient into the gain cell on load. Because the gain is **run-time
reprogrammable** (a WRITE to the coefficient cell), the multiplier can be changed on the
fly — the same mechanism a future GRC "gain slider → live coefficient" hookup would use.

## Notes

- `samp_rate` (default 4000) sets the streaming rate into the chip. Keep it modest for a
  legible plot; the USB link and board can handle far more.
- The `throttle` paces the stream so the flowgraph doesn't free-run the CPU.
- This is `gain.kyt` (the same design as `gain/`); only the *flowgraph stimulus* differs.
