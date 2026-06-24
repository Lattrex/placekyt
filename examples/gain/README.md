<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Gain — the simplest demo (start here)

A single **gain** block (multiply every sample by a constant). This is the
simplest possible Kyttar design, and it is the best place to learn how to *use*
blocks: how to place and wire one in GNU Radio Companion, host it in placeKYT,
and watch it run — all the core user features in one tiny package.

> Want to learn how to **build and verify your own** block instead of just using
> one? See [`verification/examples/gain_reference/`](../../verification/examples/gain_reference/) —
> the same gain block, used as the reference for the verification workflow. These
> are two different lessons: this one is *using* blocks; that one is *making* them.

## Files

| File | What it is |
|------|------------|
| `gain.grc` | The GNU Radio flowgraph: a source → the Kyttar gain block → a sink. Open in **both** placeKYT (to host the chip) and `gnuradio-companion` (to drive it). |
| `gain.kyt` | The placeKYT design hosting a single gain block on the cell array, already placed and routed. Open it directly to explore the canvas, inspector, and simulator. |

## Run it

The flow is the same as every Kyttar demo (see [`../README.md`](../README.md)):

1. **Host the chip.** Launch placeKYT, then **File → Open** → `gain.kyt`
   (or **File → Import GNURadio Flowgraph…** → `gain.grc`). Then
   **Simulation → Run as GNURadio Server** (binds port **58950**).
2. **Drive it.** `gnuradio-companion gain.grc`, press **▶ Run**. The output is the
   input scaled by the gain — the smoke test that the whole GNU Radio ↔ placeKYT
   pipeline is live.

## What to explore here

Because it's a single block, this is the ideal design to learn the placeKYT UI on:

- **Open `gain.kyt`** and click the gain cell to see its program in the inspector.
- **Run the simulator** and watch the cell execute and the data move through the
  fabric with the transaction log and the per-cell output arrows.
- **Open the waveform viewer**, drag the input and output ports into it, and see
  the output is exactly the input scaled by the gain.
- **Change the gain** parameter, rebuild, and re-run to see it change.
- **Parameter sync from GNU Radio.** With the chip hosted, change the gain in the
  flowgraph and re-run: placeKYT detects the drift and shows an "out of sync —
  click to resync" indicator in the status bar. Clicking it re-applies the GRC
  parameters (re-placing and re-routing if the change resizes a block). The
  policy is configurable in **Edit → Preferences → On GRC parameter change**
  (*Notify only* — default; *Auto place & route* — resync automatically;
  *Re-anchor only* — resize in place and surface any DRC violations).

Once this makes sense, the [coherent BPSK receiver](../coherent_bpsk_rx/) shows the
same workflow on a real multi-block receiver.
