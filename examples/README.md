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
| [`coherent_bpsk_rx/`](coherent_bpsk_rx/) | The headline demo: a full coherent BPSK receiver (RRC matched filter → Costas carrier recovery → Gardner timing recovery → BPSK slicer) recovering bits with a carrier + timing offset, BER 0. |
| [`gain/`](gain/) | The simplest possible flow: one gain block. Good first wiring to confirm GNU Radio ↔ placeKYT is working end to end. |
