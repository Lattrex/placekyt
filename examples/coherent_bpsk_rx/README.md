<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Coherent BPSK receiver demo

A complete coherent BPSK receiver running on the Kyttar cell array: an RRC matched
filter, a Costas loop for carrier recovery, Gardner timing recovery, and a BPSK
slicer. The input is an RRC pulse-shaped BPSK signal carrying **both** a carrier
frequency offset and a fractional timing offset; the chip recovers the bits with
**BER 0**.

## Files

| File | What it is |
|------|------------|
| `coherent_bpsk_rx.grc` | The GNU Radio flowgraph: BPSK stimulus → Kyttar receiver blocks → QT GUI plots. Open this in **both** placeKYT (to host the chip) and `gnuradio-companion` (to drive it). |
| `coherent_bpsk_rx.kyt` | The pre-built placeKYT design (the three real catalog blocks — ComplexCostasLoop → Gardner → BPSKSlicer — auto-placed and bus/broker-routed). Open this directly if you'd rather not import the `.grc`. |
| `batch_check.py` | A headless verifier: streams the burst through the hosted chip and prints the recovered bits + BER. No GNU Radio needed. |

## Run it (the demo)

1. **Host the chip.** Launch placeKYT, **File → Import GNURadio Flowgraph…** →
   `coherent_bpsk_rx.grc`. Then **Simulation → Run as GNURadio Server** (port
   **58950**). Leave placeKYT running.
2. **Drive it.** `gnuradio-companion coherent_bpsk_rx.grc`, press **▶ Run**. A
   window plots the input I waveform against the recovered bits coming back from
   the chip.

## Headless check (no GNU Radio GUI)

With the server running from step 1:

```bash
../../.venv/bin/python batch_check.py --port 58950
# streams the BPSK burst through the hosted chip and reports recovered bits + BER
```
