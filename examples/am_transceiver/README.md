<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# DSB-AM transceiver (on-chip) — real blocks

A **double-sideband AM transceiver** built from real Kyttar DSP blocks — the textbook
product modulator + coherent product detector, all real signals:

```
audio → oscMix(fc) ──[passband]── oscMix(fc) → LowPass → ×2 → recovered audio
        (self-carrier)            (self-carrier)
```

- TX: `s = audio · cos(2π·fc·t)` — an **oscillator-mixer** (`kyttar_iq_upconvert`): it
  takes the real audio and multiplies by its OWN internal carrier cos.
- RX: `y = s · cos(2π·fc·t)` then `LowPass` — the coherent product detector
  (`audio·cos² = audio·(1+cos 2fc)/2 → LPF → audio/2`), then `×2` gain.

**Why oscillator-mixers, not a shared NCO?** This chip is clockless — every cell fires
only when a neighbour triggers it, so there is no free-running oscillator. A standalone
NCO drawn as a source (GNU Radio style) gets NO trigger on-chip and is DEAD. The fix is
to **fuse the oscillator into the mixer**: the arriving audio sample is both the trigger
AND the data, and the mixer runs its own carrier. Both mixers start at phase 0 from
sample 0, so TX/RX carriers are coherent. This also removes the carrier fan-out — the
chain is a clean linear filament. (See `dev_docs/OSCILLATOR_TOPOLOGY_ANALYSIS.md`.)

`fc = 6000 Hz`, `fs = 32 kHz`, RX cutoff 3000 Hz. Verified: the Q15 fused-mixer chain
recovers the audio at **corr 1.0**.

## Files
| File | What it is |
|------|------------|
| `am_transceiver.grc` | The GNU Radio flowgraph — **import into placeKYT** (File → Import GNURadio Flowgraph…) to auto-P&R it onto the chip. Open in `gnuradio-companion` to drive the hosted chip. |
| `gen_grc.py` | Regenerates `am_transceiver.grc` (edit fc/cutoff here). |

## Status
Imports into placeKYT as **4 chip blocks** (2 oscillator-mixers + LowPass + Gain — NO NCO),
and **auto-P&R routes all 5 nets on one chip, 0 failed** — the fused-mixer chain is a clean
linear filament with no carrier fan-out, so it routes trivially under the default placer.
Runs the same way as the BPSK demo: host the chip (Run as GNURadio Server), then drive it
from GRC and compare the input-audio and recovered-audio scopes.
