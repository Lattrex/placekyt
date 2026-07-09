<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Coherent BPSK receiver demo

A complete coherent BPSK receiver running on the Kyttar cell array: an RRC matched
filter, a Costas loop for carrier recovery, Gardner timing recovery, and a BPSK
slicer. The input is an RRC pulse-shaped BPSK signal carrying **both** a carrier
frequency offset and a fractional timing offset; the chip recovers the bits with
**BER 0**.

> **This is the receiver on its own — an *extra* view, not the main demo.** The
> flagship [**BPSK modem**](../bpsk_modem/) contains this whole recovery chain
> *plus* the transmit side, so if you're picking one digital demo to study, study
> that one. This folder is here to show the receiver in isolation, with a headless
> BER check you can run without GNU Radio.

## Files

| File | What it is |
|------|------------|
| `coherent_bpsk_rx.grc` | The GNU Radio flowgraph: BPSK stimulus → Kyttar receiver blocks → QT GUI plots. Open this in **both** placeKYT (to host the chip) and `gnuradio-companion` (to drive it). |
| `coherent_bpsk_rx.kyt` | The pre-built placeKYT design (the three real catalog blocks — ComplexCostasLoop → Gardner → BPSKSlicer — auto-placed and bus/broker-routed). Open this directly if you'd rather not import the `.grc`. |
| `batch_check.py` | A headless verifier: streams the burst through the hosted chip and prints the recovered bits + BER. No GNU Radio needed. |

## Run it (the demo)

Two terminals, two commands — run both **from the repo root** (`placekyt/`).

**1. Host the chip** (terminal 1) — open the pre-built receiver design directly:

```bash
.venv/bin/python placekyt/main.py examples/coherent_bpsk_rx/coherent_bpsk_rx.kyt
```

Then in placeKYT: **Simulation → Run as GNURadio Server** (port **58950**). Leave
placeKYT running. *(Prefer to auto-P&R it yourself? Launch placeKYT blank and
**File → Import GNURadio Flowgraph…** → `examples/coherent_bpsk_rx/coherent_bpsk_rx.grc`
instead.)*

**2. Drive it** (terminal 2) — open the flowgraph and press **▶ Run** (F6):

```bash
gnuradio-companion examples/coherent_bpsk_rx/coherent_bpsk_rx.grc
```

A window plots the input I waveform against the recovered bits coming back from
the chip.

> **Watch it recover.** Tick **Enable cell animation** on the placeKYT Simulation
> toolbar before running to see the carrier/timing loops churn as the receiver
> locks. See [`../README.md`](../README.md#watch-the-data-flow--the-cell-animation-button).

## Headless check (no GNU Radio GUI)

With the server running from step 1:

```bash
../../.venv/bin/python batch_check.py --port 58950
# streams the BPSK burst through the hosted chip and reports recovered bits + BER
```
