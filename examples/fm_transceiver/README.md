<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# FM transceiver (on-chip) — real blocks

An **FM transceiver** built from real Kyttar DSP blocks. This is a true
**transceiver**: a SEPARATE transmit chain and a SEPARATE receive chain that
SHARE ONE chip, demuxed by `stream_id` — exactly the structure the AM transceiver
(`../am_transceiver`) and the BPSK modem (`../bpsk_modem`) use.

```
TX (stream 'tx'):  audio ─▶ FrequencyModulator(sens) ──────────────▶ complex FM passband
RX (stream 'rx'):  complex FM I/Q ─▶ QuadratureDemod(gain) ─────────▶ recovered audio
```

- **TX (modulator / VCO):** `phi += sensitivity·audio; out = exp(j·phi)` — the
  `FrequencyModulator` block (drop-in for GNU Radio `analog.frequency_modulator_fc`).
  A REAL audio input drives the instantaneous phase of a unit-amplitude complex
  exponential. It is a **complex-output** block, so it egresses the passband on
  `x16_out` as the **I and Q rails interleaved** (`[I0,Q0,I1,Q1,…]`).
- **RX (demodulator / discriminator):** `y = gain·arg(x[n]·conj(x[n-1]))` — the
  `QuadratureDemod` block (drop-in for `analog.quadrature_demod_cf`), the standard
  quadrature FM discriminator. With `gain = 1/sensitivity` it recovers the audio:
  `gain·arg(exp(jΔphi)) = gain·sensitivity·audio = audio`.

The RX input burst (`fm_iq`) is the SAME complex FM passband the TX chain emits,
generated in `fm_demo_stim` from the identical audio + sensitivity, so the RX chain
independently recovers the transmitted audio — a true end-to-end transceiver across
the shared chip.

**Complex I/Q the proven way.** The FM passband is genuinely complex (both I and Q
carry signal, unlike DSB-AM where Q=0). The RX source streams it INTO the chip with
`Input Type = I/Q (complex)` — the interleaved xi/xq path already proven by the
coherent BPSK RX demo. The TX sink is set to `Input Type = I/Q (complex)` too, since
the VCO ahead of it produces complex.

**Why FM is fabric-friendly.** This chip is clockless — every cell fires only when a
neighbour triggers it, so there is no free-running oscillator. The `FrequencyModulator`
VCO is **input-paced**: each audio sample is BOTH the trigger AND the phase increment
(`phi += sensitivity·x`), so a clean linear filament emits the FM passband with no
carrier fan-out. The discriminator has no oscillator at all (it's a MAC of the
conjugate product).

`fs = 32 kHz`, `f_dev = 1500 Hz`, `sensitivity = 2π·f_dev/fs`, `gain = 1/sensitivity`.

## Files
| File | What it is |
|------|------------|
| `fm_transceiver.grc` | The GNU Radio flowgraph — **import into placeKYT** (File → Import GNURadio Flowgraph…) to auto-P&R it. Open in `gnuradio-companion` to drive the hosted chip. |
| `gen_grc.py` | Regenerates `fm_transceiver.grc` (edit f_dev/sensitivity here). |

## Status — WORKING
Imports into placeKYT as **2 chip blocks** (FrequencyModulator + QuadratureDemod),
auto-P&Rs + builds on ONE chip, and runs LIVE end to end over the SimServer batch
bridge as a true duplex transceiver:

- **TX** produces the complex FM passband `exp(j·phi)` — the I rail tracks `cos(phi)`
  and Q tracks `sin(phi)` at corr **1.0** vs the reference. Being complex-output, the
  VCO egresses its I and Q rails on **two distinct dest tags** (`out_tag`, `out_tag+1`)
  — the output-side mirror of the complex input (xi→a0, xq→a1). The placeKYT waveform
  therefore shows **two clean traces** (I = cos φ, Q = sin φ), and the GR complex sink
  reassembles the interleaved I/Q. (If you ever see one jagged interleaved band, that's
  the old single-tag behavior — the two rails must be separate tags.)
- **RX** recovers the transmitted audio — corr **0.99999** (quadrature discriminator).

Both streams run on the SAME shared chip, demuxed by `stream_id`. Gate:
`verification/tests/test_fm_transceiver_grc.py` (5/5 pass). Run it like the AM
transceiver: host the chip (Run as GNURadio Server), then drive it from GRC and
compare the input-audio and recovered-audio scopes.
