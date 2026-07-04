<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# DSB-AM transceiver (on-chip) — real blocks

A **double-sideband AM transceiver** built from real Kyttar DSP blocks — the textbook
product modulator + coherent product detector, all real signals, no complex blocks:

```
audio → ×cos(fc) ──[passband]── ×cos(fc) → LowPass → ×2 → recovered audio
          └── one shared carrier NCO drives both mixers ──┘
```

- TX: `s = audio · cos(2π·fc·t)` — a `Multiply` of audio with the carrier cos.
- RX: `y = s · cos(2π·fc·t)` then `LowPass` — the coherent product detector
  (`audio·cos² = audio·(1+cos 2fc)/2 → LPF → audio/2`), then `×2` gain.

`fc = 6000 Hz`, `fs = 32 kHz`, RX cutoff 3000 Hz. The DSP is verified: the Q15 chain
recovers the audio at **corr 1.0** (`dev_docs/am_ref.py`).

## Files
| File | What it is |
|------|------------|
| `am_transceiver.grc` | The GNU Radio flowgraph — **import into placeKYT** (File → Import GNURadio Flowgraph…) to auto-P&R it onto the chip. Open in `gnuradio-companion` to drive the hosted chip. |
| `gen_grc.py` | Regenerates `am_transceiver.grc` (edit fc/cutoff here). |

## Status
Opens with **0 errors** in GNU Radio Companion, imports into placeKYT as **5 chip blocks**
(NCO + 2 Multiply + LowPass + Gain), and **auto-P&R routes all 7 nets on one chip** — the
compact linear chain routes cleanly under the abutment-first placer. Runs the same way as
the BPSK demo: host the chip (Run as GNURadio Server), then drive it from GRC and compare
the input-audio and recovered-audio scopes.
