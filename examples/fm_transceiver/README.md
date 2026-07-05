<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# FM transceiver (on-chip) — real blocks

An **FM transceiver** built from two real Kyttar DSP blocks — the modulator (VCO) and the
discriminator (FM demod):

```
audio → FrequencyModulator(sens) → [complex FM baseband] → QuadratureDemod(gain) → audio
        (VCO, input-driven)                                (2-cell discriminator)
```

- **TX (VCO):** `out = exp(j·phase); phase += sensitivity·audio` — the
  `FrequencyModulator` block (drop-in for `analog.frequency_modulator_fc`). It is
  **input-driven**: each audio sample IS the trigger for its phase step, so there is no
  separate oscillator to clock.
- **RX (discriminator):** `out = gain·Im(x[n]·conj(x[n-1]))` — the `QuadratureDemod`
  block, the standard 2-cell FM discriminator (drop-in for `analog.quadrature_demod_cf`).

**Why FM is the fabric-friendliest analog mode:** neither block needs a *shared* oscillator.
The VCO's oscillator is fed by the audio itself; the discriminator has no oscillator at all
(it's a MAC of the conjugate product). So there is no dead-NCO / carrier-fan-out problem —
the whole transceiver is a straight line that auto-P&R routes trivially. (Contrast AM/SSB,
which need mixers with fused oscillators; see `dev_docs/OSCILLATOR_TOPOLOGY_ANALYSIS.md`.)

`sensitivity = 0.8`, `gain = 1/sensitivity`, `fs = 32 kHz`. Verified: the Q15 chain
recovers the audio at **corr 0.9998** (VCO → discriminator).

## Files
| File | What it is |
|------|------------|
| `fm_transceiver.grc` | The GNU Radio flowgraph — **import into placeKYT** (File → Import GNURadio Flowgraph…) to auto-P&R it onto the chip. Open in `gnuradio-companion` to drive the hosted chip. |
| `gen_grc.py` | Regenerates `fm_transceiver.grc` (edit sensitivity here). |

## Status
Imports into placeKYT as **2 chip blocks** (VCO + discriminator), and **auto-P&R routes all
4 nets on one chip, 0 failed** — a clean linear filament, no NCO, no fan-out. Runs the same
way as the AM/BPSK demos: host the chip (Run as GNURadio Server), then drive it from GRC and
compare the input-audio and recovered-audio scopes.
