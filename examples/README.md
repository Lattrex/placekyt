<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# placeKYT examples

Each folder here is a self-contained demo: a GNU Radio flowgraph (`.grc`), often a
pre-built placeKYT design (`.kyt`), plus any helper scripts and its own README with
a walkthrough. They all run the same way — you **host the chip in placeKYT** and
**drive it from GNU Radio Companion** — so once you've done one, you've done them
all.

New to placeKYT? Start with **[`gain/`](gain/)**, then
**[`coherent_bpsk_rx/`](coherent_bpsk_rx/)**. The full setup-to-demo walkthrough
(installing GNU Radio, the Kyttar blocks, and the two-terminal run) is in
**[`../INSTALL.md`](../INSTALL.md)**.

## The demos

| Demo | What it is | Blocks | Open |
|------|------------|:------:|------|
| **[gain/](gain/)** | **Start here.** The simplest possible design — one gain block (multiply by a constant). The best place to learn the placeKYT UI and the GNU Radio ↔ placeKYT workflow end to end. | 1 | `.kyt` or `.grc` |
| **[coherent_bpsk_rx/](coherent_bpsk_rx/)** | The headline demo: a complete coherent BPSK **receiver** — RRC matched filter → Costas carrier recovery → Gardner timing recovery → BPSK slicer. The input carries a carrier **and** a timing offset; the chip recovers the bits at **BER 0**. Includes a headless `batch_check.py`. | 4 | `.grc` or `.kyt` |
| **[bpsk_modem/](bpsk_modem/)** | A full-duplex BPSK **modem** — a transmit chain and a coherent receive chain sharing one chip, demuxed by `stream_id`. The transceiver pattern on a digital link. | 6 (TX+RX) | `.grc` |
| **[am_transceiver/](am_transceiver/)** | A double-sideband **AM** transceiver: a coherent product modulator and detector sharing one chip. The simplest analog transceiver. | 8 (TX+RX) | `.grc` |
| **[fm_transceiver/](fm_transceiver/)** | An **FM** transceiver: a VCO modulator (`FrequencyModulator`) and a quadrature discriminator (`QuadratureDemod`) sharing one chip. | 6 (TX+RX) | `.grc` |
| **[ssb_weaver/](ssb_weaver/)** | A single-sideband **SSB** transceiver built the Weaver (third-method) way, using the complex-FIR filter blocks. The most involved analog demo. | 11 (TX+RX) | `.kyt` or `.grc` |

**Open `.kyt`** — the demo ships a pre-placed, pre-routed design you can open
directly (**File → Open**) and explore on the canvas without importing anything.
**Open `.grc`** — you import the flowgraph (**File → Import GNURadio
Flowgraph…**) and placeKYT auto-places and routes it. Either way, you then **Run
as GNURadio Server** and drive it from `gnuradio-companion`.

## The common workflow (every demo)

1. **Host the chip.** Launch placeKYT (`.venv/bin/python placekyt/main.py`), then
   either **File → Open** the demo's `.kyt` or **File → Import GNURadio
   Flowgraph…** the demo's `.grc`. Then **Simulation → Run as GNURadio Server** —
   the status bar shows the bound port (default **58950**). Leave placeKYT running.
2. **Drive it.** In a second terminal, `gnuradio-companion <demo>.grc`, and press
   **▶ Run** (F6). A plot window opens showing the demo's input against the output
   coming back from the placeKYT-hosted chip.

The DSP always runs **on the chip inside placeKYT**; the GNU Radio blocks are the
front-end that streams stimulus in and plots the result. To change a design, edit
it in placeKYT (or in the flowgraph and re-import) and re-host.

> On **Run**, GNU Radio may pop up a harmless *"x-terminal-emulator is missing"*
> warning — close it and the flowgraph runs normally. See
> [`../INSTALL.md`](../INSTALL.md) for the one-line way to silence it.

## The transceiver pattern (AM · FM · SSB · BPSK modem)

The four transceivers share one structure: a **transmit** chain and a **receive**
chain live on the *same* chip, kept separate by a `stream_id` tag (`"tx"` / `"rx"`)
on the source and sink blocks. That is how a single Kyttar array hosts a
full-duplex link. Each of those folders' READMEs draws the TX and RX signal path
and names the exact GNU Radio block each Kyttar block is equivalent to.

## Regenerating a flowgraph (advanced)

The AM, FM, and SSB demos include a `gen_grc.py` that regenerates the `.grc` from a
script — the DSP parameters (sample rate, carrier, filter cutoffs) live at the top
of that file. Edit them and re-run it to rebuild the flowgraph; it comes out with
script-default block positions, so re-arrange to taste in `gnuradio-companion`. The
`.grc` files checked in here are already laid out for readability, so you only need
this if you want to change the signal parameters.

---

> Building or verifying your **own** block (rather than using one)? The gain block
> is also the reference for that workflow — see
> [`../verification/examples/gain_reference/`](../verification/examples/gain_reference/)
> and [`../BLOCK_AUTHORING_GUIDE.md`](../BLOCK_AUTHORING_GUIDE.md). These examples
> *use* blocks; those show how to *make* them.
