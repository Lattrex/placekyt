<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Receiver audio tail + S-meter — two analog streams duplex on one array

One test signal (a 1 kHz tone with a DC offset, then silence) rides TWO
independent placed chains on the same chip, demuxed by stream tags — the same
duplex machinery as the BPSK modem:

```
audio ─▶ DCBlocker(32, short) ─▶ AGC(0.02, ref 0.3, gain 0.999, max 0.999) ─▶ BandReject(3300..3700) ─▶ Squelch(−25 dB) ─▶ out
meter ─▶ Abs ─▶ MovingAverage(8, ⅛) ─▶ Nlog10(10·log10) ─▶ dB out
```

The audio tail cleans the signal (DC removal, level control, a notch, and a
squelch that actually CLOSES on the tail silence); the meter leg is a
receiver's S-meter (rectify → average → dB, on the block's documented `/64`
Q15 wire scale).

## Full-speed (saturated) drive

The shipped `.grc` runs both streams at Full-speed (`pipelined: 'yes'`), and
that claim is PROVEN, not assumed: `test_shipped_kyt_saturated_matches_per_sample`
queues both interleaved bursts back-to-back with NO quiescence and asserts
both rails recover BIT-EXACTLY what the per-sample drive recovers (680/680
samples each, 0 diffs). Both chains are feed-forward 1:1 single-arm, the
saturation-safe shape.

## What is verified — and what "match" means here

Analog Q15 chains are **not bit-exact** vs float GNU Radio. The golden is the
IDENTICAL stock-GR flowgraph under the real GR interpreter, and the acceptance
bounds are **derived from the per-block verified error reports — never tuned**:

- audio: sum of the stage tolerances (DCB 59 + AGC 80 + BRF 79 + Squelch 4 =
  **222 LSB**), after the AGC gate's own 40-sample loop-transient trim.
  Measured: **148 LSB** worst.
- meter: linear tolerance through the log slope above a 0.02 FS floor +
  the Nlog10 wire tolerance = **0.066 dB**. Measured: **0.0044 dB** worst.

Two regime facts the golden must mirror (both bit us before they were pinned):

- `agc_ff` runs with **max_gain = 0.999** — the chip gain register is Q15, so
  the block implements the attenuating regime (exactly how its per-block gate
  drives GR). Uncapped GR gain exceeds 1.0 near zero-crossings and the whole
  loop transient splits (~15 % for hundreds of samples).
- the squelch power IIR (α = 0.01) decays ~0.044 dB/sample, so the tail
  silence must be ≥ ~450 samples for the gate to close inside the run.

`verification/tests/test_audio_meter_example.py` (7 tests) covers: import →
duplex auto-P&R → build, both streams within the derived bounds on real
simKYT, the squelch actually closing, shipped-`.kyt` parity, derived out_tags
confined to the 5-bit 2..31 range (the engine regression this example
uncovered), and a **mutation** (a halved AGC reference must blow the bound).

`placekyt/tests/test_gr_client_loop_examples.py::test_audio_meter_real_gr_client_two_stream_duplex`
runs the **genuine GR client** — two real `kyttar.source`/`kyttar.sink` pairs
through the DuplexRendezvous against the hosted server, exactly what pressing
Run in GRC executes minus the literal Qt window — and holds the same bounds.

```
$ python examples/audio_meter/audio_meter_demo.py
   audio: 680/680 samples, worst |err| 148 LSB (bound 222)
   meter: 680/680 samples, 166 above floor, worst |err| 0.0044 dB (bound 0.0659)
RESULT: WITHIN DERIVED BOUNDS — both placed streams match stock GNU Radio
```

95/120 cells, 7 blocks. Whole-chain proof for 7 analog blocks that previously
had only per-block gates: DCBlocker, AGC, BandRejectFilter, Squelch, Abs,
MovingAverage, Nlog10. Not verified: the literal Qt window rendering.

NOTE: building this example also fixed the installed-OOT `kyttar.dc_blocker`
marker (it took a long-dead `alpha` arg; GRC Run would TypeError) — re-run
`gr-kyttar/install.sh` for the GUI to pick it up.

## Run it

Two terminals, two commands — run both **from the repo root** (`placekyt/`).

**1. Host the chip** (terminal 1):

```bash
.venv/bin/python placekyt/main.py examples/audio_meter/audio_meter.kyt
```

Then **Simulation → Run as GNURadio Server** (port **58950**). Leave placeKYT running.

**2. Drive it** (terminal 2) — open the flowgraph in GNU Radio Companion and press
**▶ Run** (F6):

```bash
gnuradio-companion examples/audio_meter/audio_meter.grc
```

(Or run the generated top-block directly: `python3 examples/audio_meter/audio_meter.py`.)

| File | What |
|------|------|
| `audio_meter.grc` | GRC-first source (kyttar markers, two tagged streams). |
| `audio_meter.kyt` | Auto-generated placed+routed project. |
| `build_kyt.py` | Regenerates the `.kyt`. |
| `audio_meter_demo.py` | Headless END-TO-END demo. |
