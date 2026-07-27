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

**Complex I/Q.** The FM passband is complex (both I and Q carry signal). The RX source
streams it into the chip with `Input Type = I/Q (complex)` (interleaved xi/xq), and the
TX sink is `Input Type = I/Q (complex)` since the VCO ahead of it produces complex.

**The VCO is input-paced.** Each audio sample is both the trigger and the phase
increment (`phi += sensitivity·x`), so the `FrequencyModulator` emits the FM passband
with no carrier fan-out. The discriminator has no oscillator at all — it's a MAC of the
conjugate product.

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
`verification/tests/test_fm_transceiver_grc.py` (6/6 pass).

## Run it

Two terminals, two commands — run both **from the repo root** (`placekyt/`).

**1. Host the chip** (terminal 1) — launch placeKYT (no pre-built `.kyt`, so start
blank and import):

```bash
.venv/bin/python placekyt/main.py
```

In placeKYT: **File → Import GNURadio Flowgraph…** →
`examples/fm_transceiver/fm_transceiver.grc` (auto-P&Rs onto one chip), then
**Simulation → Run as GNURadio Server** (port **58950**). Leave placeKYT running.

**2. Drive it** (terminal 2) — open the flowgraph and press **▶ Run** (F6):

```bash
gnuradio-companion examples/fm_transceiver/fm_transceiver.grc
```

Compare the input-audio scope against the recovered-audio scope coming back from
the hosted chip.

> **Watch it work.** Tick **Enable cell animation** on the placeKYT Simulation
> toolbar before running to see the VCO's I/Q rails egress on two tags and the
> quadrature discriminator recover the audio. See
> [`../README.md`](../README.md#watch-the-data-flow--the-cell-animation-button).
