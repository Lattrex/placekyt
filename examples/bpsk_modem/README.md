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

## Files

| File | What it is |
|------|------------|
| `bpsk_modem.grc` | The GNU Radio flowgraph: a TX source/sink pair and an RX source/sink pair, both targeting the same placeKYT-hosted chip by `stream_id`. Open in **both** placeKYT (to host the chip) and `gnuradio-companion` (to drive it). |

This demo is **flowgraph-first** — it ships the `.grc` and placeKYT auto-places and
routes it on import (there is no pre-built `.kyt` to open directly).

## Run it

1. **Host the chip.** Launch placeKYT, **File → Import GNURadio Flowgraph…** →
   `bpsk_modem.grc`. placeKYT places and routes both chains onto one cell array.
   Then **Simulation → Run as GNURadio Server** (binds port **58950**). Leave
   placeKYT running.
2. **Drive it.** `gnuradio-companion bpsk_modem.grc`, press **▶ Run**. You'll see
   the TX passband (the modulated BPSK) and the RX side's recovered bits, both
   coming back from the one hosted chip.

See [`../README.md`](../README.md) for the workflow shared by every demo, and
[`../../INSTALL.md`](../../INSTALL.md) for the full setup.

## Key parameters

Set at the top of the flowgraph (GNU Radio variables): `samp_rate = 32000`,
`sps = 4` (→ 8 kBaud symbol rate), TX `carrier = 8000` Hz, and RRC `alpha = 0.35`,
`span = 8` on both the pulse shaper and the matched filter. The Costas loop runs at
`loop_bw = 0.05`.
