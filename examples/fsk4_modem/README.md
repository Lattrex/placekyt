<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# 4FSK modem (full-duplex, on-chip)

A full-duplex **4-level FSK (4FSK) modem** built from real Kyttar DSP blocks: a
**transmit** (modulator) chain and a **receive** (demodulator) chain that share ONE
chip, demuxed by `stream_id` (`"tx"` / `"rx"`). Carries **2 bits/symbol** as four
frequency-deviation levels.

```
TX (stream 'tx'):  bits ─▶ FSK4SymbolMapper ─▶ Upsampler(sps=2) ─▶ RRCPulseShaper(β=0.5) ─▶ FrequencyModulator ─▶ 4FSK passband
RX (stream 'rx'):  4FSK FM ─▶ QuadratureDemod ─▶ RRCPulseShaper(matched) ─▶ FSK4SyncTimingRecovery ─▶ FSK4Slicer ─▶ dibits
```

- **TX (modulator):** maps each **dibit** (2 bits, LSB-first Gray:
  `(1,0)→+3, (0,0)→+1, (0,1)→−1, (1,1)→−3`) to one of four PAM deviation levels,
  zero-stuffs to `sps=2`, RRC pulse-shapes, and FM-modulates it (`FrequencyModulator`,
  `sensitivity = π/2`, so a full-scale `+3` level advances π/2 rad/sample = **+2400 Hz**;
  the four levels give **±2400/±800 Hz** deviations at a 9600-symbol/s rate).
- **RX (demodulator):** an FM discriminator (`QuadratureDemod`), an RRC matched filter,
  **sync-word timing recovery**, and a 4FSK hard-decision slicer — recovers the dibits
  at **BER 0**.

## Timing recovery

`FSK4SyncTimingRecovery` recovers symbol timing by **cross-correlating a known sync
word**: it slides the sync word's ±1 template (`{+3,+3,+3,+3,−3,−3,+3,−3}`) over the
matched-filter stream, locks on the correlation peak, and decimates 2:1 at the locked
symbol phase — pure MAC + compare, no atan, divide, or feedback loop. Each frame opens
with a short alternating +3/−3 preamble followed by the sync word; the `fsk4_demo_stim`
module prepends both to the TX bit stream and the RX FM burst.

## The chain

The importer maps these flowgraph blocks by id + params:

| GRC block id | placeKYT block | key params |
|--------------|----------------|-----------|
| `kyttar_fsk4_symbol_mapper` | FSK4SymbolMapperBlock | — |
| `kyttar_upsampler` | UpsamplerBlock | `sps=2`, `io_type=float` |
| `kyttar_rrc_pulse_shaper` (TX shaper) | RRCPulseShaperBlock | `alpha=0.5`, `span=8`, `io_type=float` |
| `kyttar_frequency_modulator` | FrequencyModulatorBlock | `sensitivity=π/2` |
| `kyttar_quadrature_demod` | QuadratureDemodBlock | `gain=1.0` |
| `kyttar_rrc_pulse_shaper` (RX MF) | RRCPulseShaperBlock | `alpha=0.5`, `span=8`, `io_type=float` |
| `kyttar_fsk4_sync_timing_recovery` | FSK4SyncTimingRecoveryBlock | — |
| `kyttar_fsk4_slicer` | FSK4SlicerBlock | — |

**Design point.** The modem runs at **2 samples/symbol**. Drive the RX so the outer
symbols reach ~full-scale: the sync correlation threshold and the slicer's ±2/3
threshold both assume outer ≈ ±1.0.

## Performance

Measured on the built chip driven at **saturation** (back-to-back samples), recovering
at **BER 0**, from the chip's own performance report (the figures the Stream Summary
panel shows). Two operating points: **simplex** (one direction running flat-out alone)
and **full-duplex** (TX and RX co-resident, contending for the shared port).

| Direction | Simplex (alone) | Full-duplex (both) |
|-----------|----------------:|-------------------:|
| **RX** (demod) | 542 kSa/s | 115 kSa/s |
| **TX** (mod)   | 225 kSa/s | 225 kSa/s |

**~6.7 mW** active, **~0.4 mW** idle, **~12 nJ** per recovered dibit. The array is
asynchronous — only active cells draw power. Simplex is the peak per-chain rate; in
full-duplex the two chains time-slice the single shared input/output port.

## Files

| File | What it is |
|------|------------|
| `fsk4_modem.kyt` | **The pre-placed, pre-routed design — open THIS.** A DRC-clean, fully-routed placeKYT layout of both chains on one cell array. Open it directly (**File → Open**) to host the chip; **do not import the `.grc`** (see the note below). |
| `fsk4_modem.grc` | The GNU Radio flowgraph that **drives** the hosted chip: a TX source/sink pair and an RX source/sink pair, both targeting the same chip by `stream_id`. Open it in `gnuradio-companion` (terminal 2) — not in placeKYT. |
| `batch_check.py` | A headless verifier: streams a framed 4FSK burst through the hosted chip and reports the recovered dibits + symbol BER. No GNU Radio GUI needed. |

> **Open the `.kyt` — don't import the `.grc`.** This modem ships as a hand-placed,
> pre-routed design: **open `fsk4_modem.kyt`** (File → Open) to host the chip. The
> `.grc` is for driving the hosted chip from `gnuradio-companion`, not for import.

## Run it

Two terminals, two commands — run both **from the repo root** (`placekyt/`).

**1. Host the chip** (terminal 1) — launch placeKYT with the pre-built design:

```bash
.venv/bin/python placekyt/main.py examples/fsk4_modem/fsk4_modem.kyt
```

placeKYT opens the placed-and-routed modem directly. Then **Simulation → Run as
GNURadio Server** (binds port **58950**). Leave placeKYT running.

**2. Drive it** (terminal 2) — open the flowgraph and press **▶ Run** (F6):

```bash
gnuradio-companion examples/fsk4_modem/fsk4_modem.grc
```

You'll see the TX 4FSK baseband (separate **I** and **Q** traces) and the RX side's
recovered dibits (0..3), both coming back from the one hosted chip.

## Headless check (no GNU Radio GUI)

With placeKYT hosting the chip (step 1 above, **Run as GNURadio Server**, port 58950):

```bash
.venv/bin/python examples/fsk4_modem/batch_check.py --port 58950
# framed 4FSK -> FM -> chip -> recovered dibits
# prints:  Symbol BER = 0.0000  (0 errors, lag L)
```

Expect **BER 0** — the sync-word timing recovery locks on the sync word and the
slicer recovers the dibits exactly.
