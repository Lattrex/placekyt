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
  parameters (re-placing and re-routing **only if** the change resizes a block —
  a value-only change like the gain updates the parameter in place and leaves
  every block's placement and every route untouched). The policy is configurable
  in **Edit → Preferences → On GRC parameter change** (*Notify only* — default;
  *Auto place & route* — resync automatically; *Re-anchor only* — resize in place
  and surface any DRC violations).

  > Detection happens **on Run**, not on Save. The Kyttar GRC blocks are passive
  > markers that only advertise their parameters when the flowgraph runs and
  > dispatches a batch — there is no channel for GNU Radio to notify placeKYT at
  > save time. So after editing a parameter, **re-run** the flowgraph and the
  > indicator appears. (The full sample trace is retained start-to-end from the
  > first run — you do not need to nudge the speed slider to see it.)

> **How the sync detection is wired (end to end).** Each Kyttar GRC DSP block
> (`gain`, `fir_filter`, `dc_blocker`, `decimator`, `iir_biquad`,
> `lfsr_scrambler`, the complex-RX markers, …) advertises its current params into
> a process-global per-device `BatchSession` (`gr-kyttar/.../_batch_session.py`,
> `register_params`) at flowgraph `start()`. It keys each block by the placeKYT
> block NAME the importer would assign — the type's default name (`GainBlock` →
> `gain`), with the importer's `_2`/`_3` suffix for repeats. On dispatch, the
> source ships that `{block name: params}` map as the additive `grc_params` field
> on its single `process_batch` RPC; placeKYT's SimServer routes it to
> `on_grc_params`, which re-diffs against the placed design and drives the
> out-of-sync indicator. This SEND side completes the link whose receiving half
> (detection, the wire ops, the three preference modes) shipped earlier.
>
> *Deferred:* placeKYT→GRC write-back (editing the `.grc` from placeKYT) is NOT
> implemented — placeKYT detects and indicates the mismatch so you update GRC.
> *Limitation:* the name reconstruction assumes the placed design was imported
> from this flowgraph (importer naming) with matching per-type instance order; a
> manually-renamed or reordered block simply won't match (no false sync, no
> crash) — robust per-instance keying needs the GRC instance id, which a
> `gr.sync_block` does not expose to its own Python instance.

Once this makes sense, the [coherent BPSK receiver](../coherent_bpsk_rx/) shows the
same workflow on a real multi-block receiver.
