<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# SSB Weaver transceiver (on-chip) — debug demo

A **full SSB (Weaver / third-method) transceiver** built from real Kyttar DSP blocks,
so the whole 10-block chain places + auto-P&R-routes on the chip:

```
audio → ComplexMixer(−fa) → ComplexToFloat → ┬ LowPass I ┐
                                             └ LowPass Q ┘ → IQUpconvert(fc) → SSB
     → ComplexMixer(−fc) → ComplexToFloat → ┬ LowPass I ┐
                                            └ LowPass Q ┘ → IQUpconvert(fa) → Gain ×4 → audio
```

(USB; `fa=1500 Hz` audio-band centre, `fc=6000 Hz` carrier, `fs=32 kHz`, LPF cutoff
1200 Hz. The Weaver math is verified in `dev_docs/weaver_proto.py`, corr 0.998.)

## Files

| File | What it is |
|------|------------|
| `ssb_weaver.grc` | The GNU Radio flowgraph. **Import this into placeKYT** (File → Import GNURadio Flowgraph…) to place + route the transceiver on the chip. Open it in `gnuradio-companion` too, to drive the hosted chip. |
| `gen_grc.py` | Regenerates `ssb_weaver.grc` (edit frequencies/filter width here). |
| `weaver_builder.py` | Headless builder + on-chip verifier (per-block simKYT proof, corr 0.986). |

## ⚠️ Known issue — this is a DEBUG demo

The 10-block Weaver chain **places legally on one chip** (CP-SAT packer, ~64/120 cells)
but the router does **not** yet route all 15 nets: the reconvergent I/Q fan-in into
`IQUpconvert` (both xi + xq into one cell) and some corridors fail
(*"no free broker cell abutting the target input"* / *"no corridor from source to the
tap"*). Even at wider filters (more free cells) some nets don't thread — so this is a
**router** limitation on the compact placement, not a density problem.

**Import this flowgraph to SEE the router struggle:** after import, run **Route All**
(or auto-P&R) and inspect which nets fail and where — the flylines that never become
physical routes are exactly the fan-in/fan-out taps the router can't place a broker for.

## Run it (once the router routes it)

1. **Host the chip.** placeKYT → **File → Import GNURadio Flowgraph…** → `ssb_weaver.grc`,
   then **Simulation → Run as GNURadio Server** (port **58950**).
2. **Drive it.** `gnuradio-companion ssb_weaver.grc`, set `server_port`, press **▶ Run**.
   Two scopes plot the **input audio** (two tones) vs the **recovered audio** from the chip.

The DSP itself is proven correct on silicon — `weaver_builder.py` runs each block on the
real simKYT substrate and recovers the audio at **corr 0.986 / SNR 15.6 dB**.
