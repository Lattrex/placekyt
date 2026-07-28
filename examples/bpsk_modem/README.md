<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# BPSK modem (full-duplex, on-chip)

A complete **BPSK modem** built from real Kyttar DSP blocks: a **transmit** chain
and a coherent **receive** chain that share ONE chip, demuxed by `stream_id`
(`"tx"` / `"rx"`) — the same transceiver pattern as the AM/FM/SSB demos, here on a
digital link.

```
TX (stream 'tx'):  bits ─▶ PSKSymbolMapper ─▶ Upsampler ─▶ RRCPulseShaper ─▶ IQUpconvert ─▶ BPSK passband
RX (stream 'rx'):  RRC-BPSK I/Q ─▶ ComplexRRCMatchedFilter ─▶ CostasLoop ─▶ Gardner ─▶ BPSKSlicer ─▶ recovered bits
```

- **TX (modulator):** maps bits to BPSK symbols, zero-stuffs to `sps` samples per
  symbol, RRC pulse-shapes, and upconverts to a real passband — the transmit side
  of a BPSK link.
- **RX (demodulator):** an RRC matched filter, a Costas loop for carrier recovery,
  Gardner timing recovery, and a slicer — recovers the bits from an RRC-shaped
  BPSK burst carrying a carrier **and** a fractional timing offset.

The RX chain here is the same receiver as [`../coherent_bpsk_rx/`](../coherent_bpsk_rx/);
this demo adds the matching **transmitter** on the same chip so you can see a whole
modem in one design.

## Performance

Measured on the built chip driven at **saturation** (back-to-back samples), recovering
at **BER 0**, from the chip's own performance report (the figures the Stream Summary
panel shows). Two operating points: **simplex** (one direction running flat-out alone)
and **full-duplex** (TX and RX co-resident, contending for the shared port).

| Direction | Simplex (alone) | Full-duplex (both) |
|-----------|----------------:|-------------------:|
| **RX** (demod) | 186 kSa/s | 61 kSa/s |
| **TX** (mod)   | 481 kSa/s | 481 kSa/s |

**~9.8 mW** active, **~0.4 mW** idle, **~18 nJ** per recovered symbol. The array is
asynchronous — only active cells draw power. Simplex is the peak per-chain rate; in
full-duplex the two chains time-slice the single shared input/output port.

## Files

| File | What it is |
|------|------------|
| `bpsk_modem.grc` | The GNU Radio flowgraph: a TX source/sink pair and an RX source/sink pair, both targeting the same placeKYT-hosted chip by `stream_id`. Open in **both** placeKYT (to host the chip) and `gnuradio-companion` (to drive it). |

This demo is **flowgraph-first** — it ships the `.grc` and placeKYT auto-places and
routes it on import (there is no pre-built `.kyt` to open directly).

## Run it

Two terminals, two commands — run both **from the repo root** (`placekyt/`).

**1. Host the chip** (terminal 1) — launch placeKYT (there's no pre-built `.kyt`,
so start it blank and import the flowgraph):

```bash
.venv/bin/python placekyt/main.py
```

In placeKYT: **File → Import GNURadio Flowgraph…** → `examples/bpsk_modem/bpsk_modem.grc`
(placeKYT places and routes both chains onto one cell array), then **Simulation →
Run as GNURadio Server** (binds port **58950**). Leave placeKYT running.

**2. Drive it** (terminal 2) — open the flowgraph and press **▶ Run** (F6):

```bash
gnuradio-companion examples/bpsk_modem/bpsk_modem.grc
```

You'll see the TX passband (the modulated BPSK) and the RX side's recovered bits,
both coming back from the one hosted chip.

> **Watch it work.** Tick **Enable cell animation** on the placeKYT Simulation
> toolbar before you run to see both chains light up at once — TX modulating on one
> part of the array, RX recovering on another, all on the same die. See
> [`../README.md`](../README.md#watch-the-data-flow--the-cell-animation-button).

See [`../README.md`](../README.md) for the workflow shared by every demo, and
[`../../INSTALL.md`](../../INSTALL.md) for the full setup.

## Key parameters

Set at the top of the flowgraph (GNU Radio variables): `samp_rate = 32000`,
`sps = 4` (→ 8 kBaud symbol rate), TX `carrier = 8000` Hz, and RRC `alpha = 0.35`,
`span = 8` on both the pulse shaper and the matched filter. The Costas loop runs at
`loop_bw = 0.05`.
