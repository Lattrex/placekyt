<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# M17 4FSK modem (full-duplex, on-chip)

A complete **M17 4-level FSK (C4FM) modem** built from real Kyttar DSP blocks: a
**transmit** (modulator) chain and a **receive** (demodulator) chain that share ONE
chip, demuxed by `stream_id` (`"tx"` / `"rx"`) — the same full-duplex transceiver
pattern as the [QPSK modem](../qpsk_modem/), here carrying the M17 4FSK waveform.

```
TX (stream 'tx'):  bits ─▶ FSK4SymbolMapper ─▶ Upsampler(sps=2) ─▶ RRCPulseShaper(β=0.5) ─▶ FrequencyModulator ─▶ 4FSK passband
RX (stream 'rx'):  4FSK FM ─▶ QuadratureDemod ─▶ RRCPulseShaper(matched) ─▶ FSK4SyncTimingRecovery ─▶ FSK4Slicer ─▶ dibits
```

- **TX (modulator):** maps each **dibit** (2 bits, LSB-first) to one of the four M17
  PAM deviation levels, zero-stuffs to `sps=2`, RRC pulse-shapes, and FM-modulates it
  (`FrequencyModulator`, `sensitivity = 2π·2400/9600 = π/2`, so a full-scale `+3` level
  advances π/2 rad/sample = **+2400 Hz**; the four levels give the M17 **±2400/±800 Hz**
  deviations). The dibit→symbol Gray map is pinned **LSB-first** (RULE #0):
  `(1,0)→+3, (0,0)→+1, (0,1)→−1, (1,1)→−3`.
- **RX (demodulator):** an FM discriminator (`QuadratureDemod`), an RRC matched filter,
  **sync-word timing recovery**, and a 4FSK hard-decision slicer — recovers the M17
  dibits at **BER 0**.

## Why sync-word timing recovery (not Gardner)

A **Gardner** (or any decision-feedback) timing loop does **not** lock a 4-level FSK
signal: the 4-PAM eye (inner levels ±1/3) is far narrower than a 2-level BPSK eye, so
the loop jitters across the inner thresholds (measured BER ~0.3). Real M17 receivers
(e.g. `mobilinkd/m17-cxx-demod`) recover timing by **cross-correlating the known sync
word** instead. `FSK4SyncTimingRecovery` does exactly that — it slides the M17 LSF
sync word's ±1 template (`{+3,+3,+3,+3,−3,−3,+3,−3}`) over the matched-filter stream,
locks on the correlation peak, and decimates 2:1 at the locked symbol phase. Pure
MAC/add + compare — no atan, no divide, no feedback loop.

Every frame therefore opens with a short **alternating +3/−3 preamble** (AGC/coarse)
followed by the **M17 LSF sync word**; the `fsk4_demo_stim` module prepends both to the
TX bit stream and to the RX FM burst.

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

**Design point.** The modem runs at **2 samples/symbol** — the on-chip operating
point at which the loops and the RRC are proven bit-exact (same as the QPSK/BPSK
modems). Drive the RX so the outer symbols reach ~full-scale: the sync correlation
threshold and the slicer's ±2/3 threshold both assume outer ≈ ±1.0.

> **Note on the two RRC blocks after import.** The `kyttar_rrc_pulse_shaper` GRC
> binding exposes `alpha`/`span` but the placeKYT `RRCPulseShaperBlock` is
> parameterised by `sampling_freq`/`ntaps`; the importer keeps the block's default
> (a 4-sps, 33-tap RRC). For the **2-sps** operating point set each RRC block to
> `sampling_freq = 2`, `symbol_rate = 1`, `ntaps = 17` in the placeKYT inspector after
> import (same as the BPSK modem's real RRC). The self-contained acceptance test
> builds the chain with these values directly.

## Files

| File | What it is |
|------|------------|
| `fsk4_modem.kyt` | **The pre-placed, pre-routed design — open THIS.** A DRC-clean, fully-routed placeKYT layout of both chains on one cell array. Open it directly (**File → Open**) to host the chip; **do not import the `.grc`** (see the note below). |
| `fsk4_modem.grc` | The GNU Radio flowgraph that **drives** the hosted chip: a TX source/sink pair and an RX source/sink pair, both targeting the same chip by `stream_id`. Open it in `gnuradio-companion` (terminal 2) — not in placeKYT. |
| `batch_check.py` | A headless verifier: streams a framed 4FSK burst through the hosted chip and reports the recovered dibits + symbol BER. No GNU Radio GUI needed. |

> **Open the `.kyt` — don't import the `.grc`.** Like the [SSB Weaver](../ssb_weaver/),
> this modem is a dense design the auto-router **cannot fully route from a fresh
> import** (the tall `FrequencyModulator` and `FSK4SyncTimingRecovery` blocks box in
> the shared input-port fan-out and the single-cell slicer, leaving nets unrouted). The
> shipped `fsk4_modem.kyt` is a hand-tuned, DRC-clean placement + routing of exactly
> this flowgraph — so **open the `.kyt`** to host the chip. Importing the `.grc` into
> placeKYT will leave fly lines and the build will fail.

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

Expect **BER 0** — the sync-word timing recovery locks on the M17 sync word and the
slicer recovers the dibits exactly.
