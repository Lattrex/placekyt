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
| `gain_stimulus.kbs` | The default simulator stimulus the `.kyt` references. |
| `gain_golden.kbs` | The expected output the simulator checks against. |

## Run it

Two terminals, two commands — run both **from the repo root** (`placekyt/`). The
flow is the same as every Kyttar demo (see [`../README.md`](../README.md)).

**1. Host the chip** (terminal 1) — this opens the pre-built gain design directly:

```bash
.venv/bin/python placekyt/main.py examples/gain/gain.kyt
```

Then in placeKYT: **Simulation → Run as GNURadio Server** (binds port **58950**).
Leave it running.

**2. Drive it** (terminal 2) — open the flowgraph and press **▶ Run** (F6):

```bash
gnuradio-companion examples/gain/gain.grc
```

The output is the input scaled by the gain — the smoke test that the whole GNU
Radio ↔ placeKYT pipeline is live.

> Prefer to place-and-route it yourself instead of opening the pre-built `.kyt`?
> Launch placeKYT with no argument (`.venv/bin/python placekyt/main.py`) and
> **File → Import GNURadio Flowgraph…** → `examples/gain/gain.grc` — placeKYT
> auto-places and routes it. Either way you then **Run as GNURadio Server**.

> **See the data move.** On the **Simulation toolbar**, tick **Enable cell
> animation** before you run: the gain cell **glows green as it executes** and a
> per-word arrow follows each sample from the input port, through the cell, out to
> the output port. It's off by default (flat-out, no overhead); the **Speed**
> slider beside it paces the animation. On a one-cell design it's the simplest
> possible picture of the host-and-drive model — well worth turning on here first.
> Full details are in [`../README.md`](../README.md#watch-the-data-flow--the-cell-animation-button).

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

  > **Save, then Run — otherwise placeKYT never sees the change.** GNU Radio
  > Companion's **▶ Run** regenerates and executes the flowgraph from the
  > **saved `.grc` file**, so an edit you haven't saved yet simply isn't in the
  > run. And the Kyttar GRC blocks are passive markers that only advertise their
  > parameters when the flowgraph runs and dispatches a batch — there is no
  > channel for GNU Radio to notify placeKYT at save time. So the sequence is:
  > **edit the parameter → Save (Ctrl+S) → Run**. After that the out-of-sync
  > indicator appears in placeKYT. Skip the Save and you'll re-run the *old* value
  > and wonder why nothing changed. (The full sample trace is retained
  > start-to-end from the first run — you do not need to nudge the Speed slider to
  > see it.)

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

Once this makes sense, the flagship [BPSK modem](../bpsk_modem/) shows the same
workflow on a full transmit-and-receive digital link — the best next demo to study.
