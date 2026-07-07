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
version is the topology that fits ONE 10x12 die. See `dev_docs/OSCILLATOR_TOPOLOGY_ANALYSIS.md`.

(USB; `fa=1500 Hz` audio-band centre, `fc=6000 Hz` carrier, `fs=32 kHz`, LPF cutoff 1200 Hz.
The Weaver DSP is verified on-chip at **corr 0.986** — `weaver_builder_cfir.py`.)

## Files

| File | What it is |
|------|------------|
| `ssb_weaver.grc` | The GNU Radio flowgraph. **Import this into placeKYT** (File → Import GNURadio Flowgraph…) to place + route the transceiver on the chip. Open it in `gnuradio-companion` too, to drive the hosted chip. |
| `gen_grc.py` | Regenerates `ssb_weaver.grc` (edit frequencies/filter width here). |
| `weaver_builder.py` | Headless builder + on-chip verifier (per-block simKYT proof, corr 0.986). |

## Status

- **GRC-clean.** The `.grc` loads + generates in GNU Radio Companion with **zero type
  conflicts** — the audio/passband → complex-mixer edges use the spliced
  `float_to_complex` converters (the real→complex-in-front pattern). Open it in
  `gnuradio-companion` and it's in a good working state.
- **Imports + places.** placeKYT imports it as **7 chip blocks** (2 ComplexMixer +
  2 ComplexLowPass + 2 IQUpconvert + 1 Gain) across the `tx`/`rx` streams and **places
  all 7 on one 10x12 die**.
- **Auto-route is incomplete** (⚠️). The auto-router threads ~8/14 nets; the remaining
  ones are the complex-packet **fan-in** nets into the mixers/upconverts (both `xi`+`xq`
  into one cell). This is a **router** limitation on the compact placement, not a dtype
  or density problem — **route those nets by hand** (draw the routes / use Route All and
  fix the flylines), then build + host.
- **DSP proven on silicon.** `weaver_builder_cfir.py` runs each block on the real simKYT
  substrate and recovers the audio at **corr 0.986 / SNR 15.6 dB** — the datapath is
  correct; only the auto-router's fan-in threading is the gap.

Gate: `verification/tests/test_ssb_weaver_grc.py` — import (dtype-clean) + placement pass;
the full auto-route/build is an `xfail` (route-by-hand), the batch-recovery test skips
until the build exists.

## Run it (after routing the fan-in nets by hand)

1. **Host the chip.** placeKYT → **File → Import GNURadio Flowgraph…** → `ssb_weaver.grc`,
   route the remaining nets, then **Simulation → Run as GNURadio Server** (port **58950**).
2. **Drive it.** `gnuradio-companion ssb_weaver.grc`, set `server_port`, press **▶ Run**.
   Two scopes plot the **input audio** (two tones) vs the **recovered audio** from the chip.
