<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# placeKYT examples

Each folder here is a self-contained demo: a GNU Radio flowgraph (`.grc`), often a
pre-built placeKYT design (`.kyt`), plus any helper scripts and its own README with
a walkthrough. They all run the same way — you **host the chip in placeKYT** and
**drive it from GNU Radio Companion** — so once you've done one, you've done them
all.

New to placeKYT? Start with **[`gain/`](gain/)**, then the flagship
**[`bpsk_modem/`](bpsk_modem/)**. The full setup-to-demo walkthrough
(installing GNU Radio, the Kyttar blocks, and the two-terminal run) is in
**[`../INSTALL.md`](../INSTALL.md)**.

Every demo's README is **click-and-run**: it gives you the two exact commands
(host the chip, drive it) as copy-paste code blocks. Open a README, copy the
first command into a terminal, copy the second into another, and you're running.

## The demos

| Demo | What it is | Blocks | Open |
|------|------------|:------:|------|
| **[gain/](gain/)** | **Start here.** The simplest possible design — one gain block (multiply by a constant). The best place to learn the placeKYT UI and the GNU Radio ↔ placeKYT workflow end to end. | 1 | `.kyt` or `.grc` |
| **[bpsk_modem/](bpsk_modem/)** | **The flagship.** A full-duplex BPSK **modem** — a transmit chain **and** a complete coherent receive chain sharing one chip, demuxed by `stream_id`. It contains everything the coherent receiver does *plus* the transmit side, so it's the one to study: the full digital link on a single Kyttar array. | 6 (TX+RX) | `.grc` |
| **[coherent_bpsk_rx/](coherent_bpsk_rx/)** | The coherent BPSK **receiver** on its own — RRC matched filter → Costas carrier recovery → Gardner timing recovery → BPSK slicer. The input carries a carrier **and** a timing offset; the chip recovers the bits at **BER 0**. An extra, receiver-only view of the same recovery chain the modem uses. Includes a headless `batch_check.py`. | 4 | `.grc` or `.kyt` |
| **[qpsk_modem/](qpsk_modem/)** | The coherent **QPSK** receiver — RRC matched filter → **order-4** Costas carrier recovery → **complex** Gardner timing recovery → QPSK slicer. Fully complex (both I and Q carried through every handoff), 2 bits per symbol; recovers the symbols at **BER 0** through a carrier **and** a timing offset. The QPSK analog of `coherent_bpsk_rx/`, with a headless `batch_check.py`. | 4 | `.grc` or `.kyt` |
| **[fsk4_modem/](fsk4_modem/)** | A full-duplex **M17 4FSK (C4FM)** modem — a transmit (`FSK4SymbolMapper → Upsampler → RRC → FrequencyModulator`) and a receive (`QuadratureDemod → RRC matched filter → FSK4SyncTimingRecovery → FSK4Slicer`) chain sharing one chip. Because a Gardner loop can't lock a 4-level FSK eye, timing is recovered by **cross-correlating the M17 sync word** — exactly what real M17 receivers do; recovers the dibits at **BER 0**. Ships a headless `batch_check.py`. Like the SSB Weaver, this one is **hand-placed: open the `.kyt` directly, don't import the `.grc`** (see its README). | 8 (TX+RX) | `.kyt` only |
| **[qam16_modem/](qam16_modem/)** | The coherent **16-QAM** receiver — a **decision-directed** complex Costas loop (16-QAM is non-constant-modulus, so the QPSK/BPSK phase detectors fail) → 16-QAM slicer, **4 bits per symbol** on the square 16-point `digital.constellation_16qam()` grid; recovers the symbols at **BER 0** through a carrier offset. The next step up from `qpsk_modem/`, with a headless `batch_check.py`. Hand-placed (the 10-cell DD Costas must abut the input port): **open the `.kyt`, don't import the `.grc`** (see its README). | 2 | `.kyt` only |
| **[am_transceiver/](am_transceiver/)** | A double-sideband **AM** transceiver: a coherent product modulator and detector sharing one chip. The simplest analog transceiver. | 8 (TX+RX) | `.grc` |
| **[fm_transceiver/](fm_transceiver/)** | An **FM** transceiver: a VCO modulator (`FrequencyModulator`) and a quadrature discriminator (`QuadratureDemod`) sharing one chip. | 6 (TX+RX) | `.grc` |
| **[ssb_weaver/](ssb_weaver/)** | A single-sideband **SSB** transceiver built the Weaver (third-method) way, using the complex-FIR filter blocks. The most involved analog demo — **hand-placed: open the `.kyt` directly, don't import the `.grc`** (see its README). | 11 (TX+RX) | `.kyt` only |

**Open `.kyt`** — the demo ships a pre-placed, pre-routed design you can open
directly (**File → Open**) and explore on the canvas without importing anything.
**Open `.grc`** — you import the flowgraph (**File → Import GNURadio
Flowgraph…**) and placeKYT auto-places and routes it. Either way, you then **Run
as GNURadio Server** and drive it from `gnuradio-companion`.

> Two demos are the exception — the **SSB Weaver** and the **FSK4 modem**: they're
> dense, hand-placed designs the auto-router can't fully route, so you **must open
> their `.kyt` directly** (importing the `.grc` will leave nets unrouted and the build
> will fail). Each README explains why. Every other demo places and routes from the
> `.grc`.

## The common workflow (every demo)

Every demo runs the same two-terminal way. Run both commands **from the repo
root** (`placekyt/`), with the venv already set up (see [`../INSTALL.md`](../INSTALL.md)).
Each demo's README repeats these two commands filled in for that demo — so you can
copy-paste straight from there.

**1. Host the chip** (terminal 1) — launch placeKYT. For a demo that ships a `.kyt`,
pass it and placeKYT opens it directly; otherwise launch placeKYT and **File →
Import GNURadio Flowgraph…** the demo's `.grc`:

```bash
.venv/bin/python placekyt/main.py examples/gain/gain.kyt
```

Then in placeKYT: **Simulation → Run as GNURadio Server** — the status bar shows
the bound port (default **58950**). Leave placeKYT running.

**2. Drive it** (terminal 2) — open the flowgraph in GNU Radio Companion and press
**▶ Run** (F6):

```bash
gnuradio-companion examples/gain/gain.grc
```

A plot window opens showing the demo's input against the output coming back from
the placeKYT-hosted chip.

The DSP always runs **on the chip inside placeKYT**; the GNU Radio blocks are the
front-end that streams stimulus in and plots the result. To change a design, edit
it in placeKYT (or in the flowgraph and re-import) and re-host.

## Watch the data flow — the cell-animation button

placeKYT can **animate the chip as it runs**, so you can literally see data move
through the cell array. On the **Simulation toolbar**, tick **Enable cell
animation**; then run — from the in-tool stimulus or from a GNU Radio drive. Cells
**glow green as they execute**, and per-word arrows show each value hopping
cell-to-cell along its route toward the output port. It's the clearest way to *see*
what a design is doing: where the signal enters, which cells compute, how it snakes
to the egress port. The **Speed** slider beside the checkbox paces it — the chip
steps in lockstep with the animation, so a stall or a dead route is visible as it
happens.

It's **off by default** — leaving it off runs flat-out with no visual overhead, and
the slider is greyed. Turn it on when you want to understand or debug a layout; turn
it off for a fast run. Worth trying from the very first demo (`gain/`): one cell
lighting up as each sample passes through makes the whole host-and-drive model click.

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
