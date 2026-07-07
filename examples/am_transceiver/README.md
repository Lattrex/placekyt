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

- **TX (modulator)**: `s = audio · cos(2π·fc·t)` — an oscillator-mixer
  (`kyttar_iq_upconvert`) forms the suppressed-carrier DSB-AM passband.
- **RX (demodulator)**: `y = s · cos(2π·fc·t)` then `LowPass` — the coherent
  product detector (`audio·cos² = audio·(1+cos 2fc)/2 → LPF → audio/2`), then `×2`.

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

`fc = 6000 Hz`, `fs = 32 kHz`, RX cutoff 3000 Hz.

## Files
| File | What it is |
|------|------------|
| `am_transceiver.grc` | The GNU Radio flowgraph — **import into placeKYT** (File → Import GNURadio Flowgraph…) to auto-P&R it. Open in `gnuradio-companion` to drive the hosted chip. |
| `gen_grc.py` | Regenerates `am_transceiver.grc` (edit fc/cutoff here). |

## Status
Imports into placeKYT as **4 chip blocks** (2 oscillator-mixers + LowPass + Gain),
and **auto-P&R + build succeed** with both streams wired to the shared ports. A
single-chain AM path runs live end-to-end (|corr| ≈ 0.95).

**Known limitation (in progress):** the full DUPLEX live run is not yet correct on
this compact placement — the two mixer input corridors share an input-corridor cell
whose forwarding face can't serve both a broker and a straight transit at once (TX
misdelivers), and the RX product-detector's free-running NCO needs carrier coherence
(RX corr ~0.5). The `verification/tests/test_am_transceiver_grc.py` live-recovery
tests are `xfail(strict)` on exactly these two blockers and flip green the moment the
placer/router + coherence fix lands. See the
`project_am_transceiver_duplex_blockers` note.
