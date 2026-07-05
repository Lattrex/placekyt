<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# SSB Weaver transceiver (on-chip) — debug demo

A **full SSB (Weaver / third-method) transceiver** built from real Kyttar DSP blocks, using
the **fabric-native fused-oscillator topology** (no shared NCO, no carrier fan-out):

```
audio → cmix(fa) ─┬ yi=cos ─ LowPass I ─ iqup(fc) xi → cos-rail ┐
                  └ yq=sin ─ LowPass Q ─ iqup(fc) xq → −sin-rail ┘→ Add → SSB
     → cmix(fc) ─┬ yi=cos ─ LowPass I ─ iqup(fa) xi → cos-rail ┐
                 └ yq=sin ─ LowPass Q ─ iqup(fa) xq → −sin-rail ┘→ Add → Gain ×4 → audio
```

**Why this shape (the important part).** This chip is clockless — there is no free-running
oscillator; a standalone NCO drawn as a source gets no trigger and is DEAD on-chip. So each
mixer carries its OWN oscillator: the DOWN-mix is one `ComplexMixer` (emits both cos+sin
rails as two ports), and each UP-mix is a lean 6-cell `IQUpconvert` producing one rail
(`xi`→`sig·cos`, `xq`→`−sig·sin`; the negation makes the Weaver combine an **Add**). No
shared NCO → no dead trigger; no shared carrier → no fan-out. This is **77 cells / 120**
(the earlier all-`ComplexMixer` version was 100). See
`dev_docs/OSCILLATOR_TOPOLOGY_ANALYSIS.md`.

(USB; `fa=1500 Hz` audio-band centre, `fc=6000 Hz` carrier, `fs=32 kHz`, LPF cutoff
1200 Hz. The fused-mixer Weaver DSP is verified at **corr 0.976**.)

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
