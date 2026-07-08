<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# DSB-AM transceiver (on-chip) — real blocks

A **double-sideband AM transceiver** built from real Kyttar DSP blocks. This is a
true **transceiver**: a SEPARATE transmit chain and a SEPARATE receive chain that
SHARE ONE chip, demuxed by `stream_id` — exactly the structure the BPSK modem
example (`../bpsk_modem`) uses.

```
TX (stream 'tx'):  audio ─▶ oscMix(fc) ────────────────────────▶ AM passband
RX (stream 'rx'):  AM passband ─▶ oscMix(fc) ─▶ LowPass ─▶ ×2 ─▶ recovered audio
```

- **TX (modulator)**: `s = audio · cos(2π·fc·t)`. The audio goes through a
  `float_to_complex` (audio → I, Q = 0, the GR-idiomatic real→complex converter),
  then the complex-only `kyttar_iq_upconvert` forms the suppressed-carrier DSB-AM
  passband: `s = Re{(audio + 0j)·e^{jωt}} = audio·cos(2π·fc·t)`.
- **RX (demodulator)**: same `float_to_complex` in front, then `iq_upconvert`,
  `LowPass`, `×2` — the coherent product detector
  (`audio·cos² = audio·(1+cos 2fc)/2 → LPF → audio/2`, then `×2`).

Both chains use the shared `x16_in` / `x16_out` ports; the placeKYT server resolves
each `stream_id` to its own chain's landing cell and demuxes the two output streams
by tag (`engine.port_config.stream_targets`), so TX and RX run independently on one
chip.

**Why oscillator-mixers, not a shared NCO?** This chip is clockless — every cell
fires only when a neighbour triggers it, so there is no free-running oscillator. A
standalone NCO drawn as a source gets NO trigger on-chip and is DEAD. The fix is to
**fuse the oscillator into the mixer**: the arriving sample is both the trigger AND
the data, and the mixer runs its own carrier. Both mixers start at phase 0 from
sample 0, so TX/RX carriers are coherent.

`fc = 6000 Hz`, `fs = 32 kHz`, audio tones 800/1500 Hz, RX cutoff 2000 Hz
(fc/message-BW = 3).

## Files
| File | What it is |
|------|------------|
| `am_transceiver.grc` | The GNU Radio flowgraph — **import into placeKYT** (File → Import GNURadio Flowgraph…) to auto-P&R it. Open in `gnuradio-companion` to drive the hosted chip. |
| `gen_grc.py` | Regenerates `am_transceiver.grc` (edit fc/cutoff here). |

## Status — WORKING
Imports into placeKYT as **4 chip blocks** (2 oscillator-mixers + LowPass + Gain;
the two `float_to_complex` + `null_source` converters are logical-only and spliced
away on import), auto-P&Rs + builds on ONE chip, and runs LIVE end to end over the
SimServer batch bridge as a true duplex transceiver:

- **TX** produces the DSB-AM passband `audio·cos(2π·fc·t)` — corr **1.0** vs the
  reference (accounting for the NCO's phase pre-increment).
- **RX** recovers the transmitted audio — corr **0.998** (after the on-chip LowPass
  group delay).

Both streams run on the SAME shared chip, demuxed by `stream_id`. Gate:
`verification/tests/test_am_transceiver_grc.py` (5/5 pass). Run it like the BPSK
modem: host the chip (Run as GNURadio Server), then drive it from GRC and compare the
input-audio and recovered-audio scopes.
