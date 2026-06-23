<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# placeKYT examples

Each subdirectory is a self-contained demo: the GNU Radio flowgraph (`.grc`), the
placeKYT host design (`.kyt`), and any helper script, all in one place.

Every demo follows the same two-process flow:

1. **placeKYT hosts the chip.** Open the `.grc` in placeKYT
   (**File → Import GNURadio Flowgraph…**), then **Simulation → Run as GNURadio
   Server** (binds port **58950**). placeKYT places, routes, builds, and simulates
   the chip.
2. **GNU Radio drives it.** Open the same `.grc` in `gnuradio-companion` and press
   **Run**. The Kyttar blocks stream samples to the placeKYT-hosted chip over the
   socket and plot what comes back.

The blocks you see in GNU Radio are thin front-ends — all the DSP runs on the chip
inside placeKYT. See `../INSTALL.md` for the one-time setup.

| Demo | What it shows |
|------|---------------|
| [`gain/`](gain/) | **Start here.** The simplest possible flow: one gain block. The best place to learn the placeKYT UI and the GNU Radio ↔ placeKYT workflow end to end. |
| [`coherent_bpsk_rx/`](coherent_bpsk_rx/) | The headline demo: a full coherent BPSK receiver (RRC matched filter → Costas carrier recovery → Gardner timing recovery → BPSK slicer) recovering bits with a carrier + timing offset, BER 0. |

> Learning to **build and verify your own** block (rather than use an existing
> one)? The gain block is also the reference for that — see
> [`../verification/examples/gain_reference/`](../verification/examples/gain_reference/).
> These examples *use* blocks; that one shows how to *make* them.
