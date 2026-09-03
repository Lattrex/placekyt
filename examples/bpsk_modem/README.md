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
  BPSK burst carrying a carrier **and** a fractional timing offset. The Gardner
  timing block is a **verified drop-in** for GNU Radio's
  `symbol_sync_cc(TED_GARDNER)` — bit-exact on chip across the fractional-offset
  sweep; see [`verification/STATUS.md`](../../verification/STATUS.md).

The RX chain here is the same receiver as [`../coherent_bpsk_rx/`](../coherent_bpsk_rx/);
this demo adds the matching **transmitter** on the same chip so you can see a whole
modem in one design.

## Performance

**Simplex**, driven at **saturation** (whole burst back-to-back), recovering at
**BER 0**. Each direction runs alone at its compute-bound ceiling; the rate is the
sink (output) sample rate — the "Settled rate" the Stream Summary panel shows.

| Direction | Sink rate | Power |
|-----------|----------:|------:|
| **RX** (demod) | 188 kSa/s | 9.6 mW |
| **TX** (mod)   | 481 kSa/s | 7.6 mW |

Power is total draw (active + idle) while that direction runs alone; **idle ~0.5 mW**.
The array is asynchronous — only active cells draw power. To reproduce: open the `.kyt`,
Run as GNURadio Server, set the Kyttar
Source **Full-speed (saturated) = Yes** and **Duplex schedule = Sequential**, Run, and
read each direction's Settled rate. (Set schedule = Interleaved for the full-duplex
rate.)

## Files

| File | What it is |
|------|------------|
| `bpsk_modem.kyt` | The pre-placed design — open directly, or import the `.grc` and auto-P&R. |
| `bpsk_modem.grc` | The GNU Radio flowgraph: a TX source/sink pair and an RX source/sink pair, both targeting the same placeKYT-hosted chip by `stream_id`. Open in **both** placeKYT (to host the chip) and `gnuradio-companion` (to drive it). |

## Run it

Two terminals, two commands — run both **from the repo root** (`placekyt/`).

**1. Host the chip** (terminal 1) — open the pre-built design directly:

```bash
.venv/bin/python placekyt/main.py examples/bpsk_modem/bpsk_modem.kyt
```

Then **Simulation → Run as GNURadio Server** (binds port **58950**). Leave
placeKYT running. *(Prefer to auto-P&R it yourself? Launch placeKYT blank and
**File → Import GNURadio Flowgraph…** → `examples/bpsk_modem/bpsk_modem.grc`
instead — placeKYT places and routes both chains onto one cell array.)*

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
`sps = 4` (→ 8 kBaud symbol rate), TX `carrier = 8000` Hz, and RRC `alpha = 0.35`
on both filters. TX pulse shaper: `ntaps = 33` (span 8 at 4 sps). RX matched filter:
span 8 at **2 sps** (17 taps) — the RX stream (`stim.rx_burst`) is a 2-sps RRC-BPSK
burst, independent of the TX `sps = 4`. The Costas loop runs at `loop_bw = 0.05`.
