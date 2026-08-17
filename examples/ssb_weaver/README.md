<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# SSB Weaver transceiver (on-chip)

A **full SSB (Weaver / third-method) transceiver** built from real Kyttar DSP blocks. Like
the AM/FM/BPSK demos it is a TRUE transceiver: a SEPARATE transmit chain and a SEPARATE
receive chain SHARE ONE chip, demuxed by `stream_id` (`tx`/`rx`). It uses the **complex-FIR
topology** — each half is a straight complex filament, no split/recombine fan-out:

```
TX (stream 'tx'):  audio ─▶ ComplexMixer(-fa) ─▶ ComplexLowPass ─▶ IQUpconvert(fc) ─▶ SSB passband
RX (stream 'rx'):  SSB ─▶ ComplexMixer(-fc) ─▶ ComplexLowPass ─▶ IQUpconvert(fa) ─▶ Gain×4 ─▶ audio
```

- `ComplexMixer` = GNU Radio `multiply_cc(signal, sig_source_c)` — the full complex product
  (fused oscillator, no shared NCO). It takes a **complex** baseband, so the real audio (and
  the real RX passband) go through a `float_to_complex` (+ a `null_source` Q rail) — the
  GR-idiomatic real→complex converter, which placeKYT **splices** on import (audio → mixer
  `xi`, Q=0). Feeding a real signal straight into a `complex_in='complex'` source is a dtype
  conflict; this is the AM/FM real→complex-in-front pattern.
- `ComplexLowPass` = `fir_filter_ccf` filters BOTH rails of the complex packet in ONE block,
  so the classic Weaver's complex→2-real-LPF fan-out and 2-real→1 recombine both vanish.
- `IQUpconvert` = complex baseband → real passband (`out = I·cos − Q·sin`), the SSB combine.

**Topology.** Each mixer carries its own oscillator (fused per-mixer phase accumulators,
no carrier fan-out). This complex-FIR Weaver form fits one 10×12 die.

(USB; `fa=1500 Hz` audio-band centre, `fc=6000 Hz` carrier, `fs=32 kHz`, LPF cutoff 1200 Hz.
The Weaver DSP is verified on-chip at **corr 0.986** — `weaver_builder_cfir.py`.)

## Performance

**Simplex**, driven at **saturation** (whole burst back-to-back), recovering the
audio at **corr 0.97** vs the input. Each direction runs alone at its compute-bound
ceiling; the rate is the sink (output) sample rate — the "Settled rate" the Stream
Summary panel shows.

| Direction | Sink rate | Power |
|-----------|----------:|------:|
| **RX** (demodulator) | 346 kSa/s | 14.0 mW |
| **TX** (modulator)   | 346 kSa/s | 14.4 mW |

Power is total draw (active + idle) while that direction runs alone; **idle ~0.4 mW** —
the heaviest of the analog demos (~35–38 active cells of complex-FIR filtering). The
array is asynchronous — only active cells draw power. To
reproduce: open the `.kyt`, Run as GNURadio Server, set the Kyttar Source **Full-speed
(saturated) = Yes** and **Duplex schedule = Sequential**, Run, and read each
direction's Settled rate. (Set schedule = Interleaved for the full-duplex rate.)

## Files

| File | What it is |
|------|------------|
| `ssb_weaver.kyt` | **The demo.** A hand-placed, hand-routed design of the full transceiver on one 10x12 die. **Open this directly** (File → Open) to host the chip — see "Run it" below. |
| `ssb_weaver.grc` | The GNU Radio flowgraph. Open it in `gnuradio-companion` to **drive** the hosted chip (stimulus + plots). |
| `gen_grc.py` | Regenerates `ssb_weaver.grc` (edit frequencies/filter width here). |
| `weaver_builder.py` / `weaver_builder_cfir.py` | Headless builders + on-chip verifiers (per-block simKYT proof, corr 0.986). |
| `ssb_hand_place_script.py` | A runnable placeKYT command trace that reproduces the hand-placement deterministically (advanced/reference). |

## This demo is HAND-PLACED — open the `.kyt`, don't import the `.grc`

This modem ships as a hand-placed, pre-routed design: **open `ssb_weaver.kyt`**
(File → Open) to host the chip. The `.grc` is for driving the hosted chip from
`gnuradio-companion`, not for import. Hand layout is the intended workflow for compact,
high-utilisation designs like this one.

The on-chip DSP is verified end to end: the chip emits the SSB passband (TX) at **corr
0.98** vs the `ssb_demo_stim` reference, and the RX chain **recovers the audio at corr
~0.97**. Both streams are gated in `verification/tests/test_ssb_weaver_grc.py`.

## Run it

Two terminals, two commands — run both **from the repo root** (`placekyt/`).

**1. Host the chip** (terminal 1) — open the hand-placed design directly. **Do NOT
import the `.grc`** for this demo — it's dense and hand-routed, and auto-P&R will not
route it (see above). Opening the `.kyt` is the whole point here:

```bash
.venv/bin/python placekyt/main.py examples/ssb_weaver/ssb_weaver.kyt
```

Then in placeKYT: **Simulation → Run as GNURadio Server** (port **58950**). Leave
placeKYT running.

**2. Drive it** (terminal 2) — open the flowgraph and press **▶ Run** (F6):

```bash
gnuradio-companion examples/ssb_weaver/ssb_weaver.grc
```

Two scopes plot the **input audio** (two tones) against the **recovered audio**
coming back from the chip.

> **Watch it work.** Tick **Enable cell animation** on the placeKYT Simulation
> toolbar before running to see this dense hand-routed Weaver transceiver flow — the
> most involved layout of all the demos. See
> [`../README.md`](../README.md#watch-the-data-flow--the-cell-animation-button).
