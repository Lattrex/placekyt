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

**Why this shape.** This chip is clockless — a standalone NCO drawn as a source gets no
trigger and is DEAD on-chip. Each mixer carries its OWN oscillator (the fused-oscillator
trade: cheap per-mixer phase accumulators, no scarce carrier fan-out). The complex-FIR
version is the topology that fits ONE 10x12 die.

(USB; `fa=1500 Hz` audio-band centre, `fc=6000 Hz` carrier, `fs=32 kHz`, LPF cutoff 1200 Hz.
The Weaver DSP is verified on-chip at **corr 0.986** — `weaver_builder_cfir.py`.)

## Files

| File | What it is |
|------|------------|
| `ssb_weaver.kyt` | **The demo.** A hand-placed, hand-routed design of the full transceiver on one 10x12 die. **Open this directly** (File → Open) to host the chip — see "Run it" below. |
| `ssb_weaver.grc` | The GNU Radio flowgraph. Open it in `gnuradio-companion` to **drive** the hosted chip (stimulus + plots). |
| `gen_grc.py` | Regenerates `ssb_weaver.grc` (edit frequencies/filter width here). |
| `weaver_builder.py` / `weaver_builder_cfir.py` | Headless builders + on-chip verifiers (per-block simKYT proof, corr 0.986). |
| `ssb_hand_place_script.py` | A runnable placeKYT command trace that reproduces the hand-placement deterministically (advanced/reference). |

## ⚠️ This demo is HAND-PLACED — open the `.kyt`, don't import the `.grc`

Unlike the other demos, **you cannot auto-place-and-route this one.** It's a dense
transceiver (11 chip blocks) whose complex-packet **fan-in** nets (both `xi` and `xq`
landing on one mixer/upconvert cell) exceed what the auto-router threads at this
utilisation — importing the `.grc` places the blocks but leaves several nets unrouted,
so the build fails. The demo therefore ships a **hand-placed, hand-routed `ssb_weaver.kyt`**
that you **open directly**.

> This is expected and normal for compact, high-utilisation designs — hand layout is the
> intended workflow above ~50% cell usage (the auto-router is best for looser designs).
> The auto-route path is a known limitation, tracked as `xfail` in
> `verification/tests/test_ssb_weaver_grc.py`.

The on-chip DSP is verified **end to end**: the hand-placed chip emits the SSB passband
(TX) at **corr 0.98** vs the `ssb_demo_stim` reference, and the RX chain **recovers the
audio at corr ~0.97** (the block chain is proven at corr 0.986 by `weaver_builder_cfir.py`).
Both streams are gated in `verification/tests/test_ssb_weaver_grc.py`.

## Run it

1. **Host the chip.** Launch placeKYT → **File → Open** → `ssb_weaver.kyt` (open the
   `.kyt` — do **not** import the `.grc`; auto-P&R will not route this design). Then
   **Simulation → Run as GNURadio Server** (port **58950**). Leave placeKYT running.
2. **Drive it.** `gnuradio-companion ssb_weaver.grc`, press **▶ Run**. Two scopes plot
   the **input audio** (two tones) against the **recovered audio** coming back from the
   chip.
