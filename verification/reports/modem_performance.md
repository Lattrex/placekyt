<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Modem & transceiver performance — full-duplex, saturated

All seven example designs run on **one 120-cell (10×12) Kyttar asynchronous array**,
full-duplex (transmit and receive chains co-resident, sharing one input port and one
output port). Each was driven at **saturation** (back-to-back samples, one continuous
run) through the real path (import the `.grc` / open the `.kyt`), and its throughput,
power, and energy come from the chip's own `performance_report()` — the same figures
the GUI Stream Summary panel shows.

Two operating points are reported per direction:

- **Simplex** — one direction running flat-out alone (peak per-chain rate).
- **Full-duplex** — TX and RX co-resident, time-slicing the shared input/output port
  (the sustained rate when both directions run at once).

Correctness was verified at the same saturated operating point: **BER 0** for the
digital modems, **audio correlation** vs the input for the analog transceivers.

## Results

| Design  | Modulation   | RX simplex | RX full-duplex | TX simplex | TX full-duplex | Active power | Idle power | Energy / output | Correctness  |
|---------|--------------|-----------:|---------------:|-----------:|---------------:|-------------:|-----------:|----------------:|:------------:|
| BPSK    | 1 bit/sym    | 186 kSa/s  | 61 kSa/s       | 481 kSa/s  | 481 kSa/s      | 9.8 mW       | 0.37 mW    | 18 nJ/sym       | BER 0        |
| QPSK    | 2 bit/sym    | 172 kSa/s  | 172 kSa/s      | 460 kSa/s  | 350 kSa/s      | 13.6 mW      | 0.34 mW    | 28 nJ/sym       | BER 0        |
| 4FSK    | 2 bit/sym    | 542 kSa/s  | 115 kSa/s      | 225 kSa/s  | 225 kSa/s      | 6.7 mW       | 0.40 mW    | 12 nJ/dibit     | BER 0        |
| 16-QAM  | 4 bit/sym    | 146 kSa/s  | 146 kSa/s      | 460 kSa/s  | 148 kSa/s      | 11.3 mW      | 0.26 mW    | 41 nJ/sym       | BER 0        |
| AM      | analog audio | 481 kSa/s  | 481 kSa/s      | 488 kSa/s  | 481 kSa/s      | 14.2 mW      | 0.43 mW    | 16 nJ/sample    | corr 0.998   |
| FM      | analog audio | 1872 kSa/s | 429 kSa/s      | 427 kSa/s  | 427 kSa/s      | 6.8 mW       | 0.52 mW    | 6 nJ/sample     | corr 0.996   |
| SSB     | analog audio | 346 kSa/s  | 346 kSa/s      | 346 kSa/s  | 346 kSa/s      | 25.7 mW      | 0.23 mW    | 40 nJ/sample    | corr 0.97    |

Notes:

- **Idle power ~0.2–0.5 mW** across all designs — the array is asynchronous, so only
  active cells draw power.
- **FM RX (1872 kSa/s simplex)** is the fastest demodulator: its receive path is a bare
  quadrature discriminator (a MAC of the conjugate product, no feedback loops).
- **SSB (~26 mW active)** is the heaviest — 11 cells of complex-FIR filtering (the
  Weaver third-method topology).
- Full-duplex vs simplex differs per design by how the two chains time-slice the single
  shared port; the full-duplex figure is the sustained contended-region rate (verified
  stable: settled == mean, not a burst-ratio artifact).

## Method

- Real path only: `import the .grc → auto-place-and-route → build`, or `open the shipped
  .kyt`. No hand-built reconstructions.
- Saturated drive: the whole burst queued via `queue_words_physical`, run to completion,
  drained by tag — the `pipelined` path the shipped `.grc` files now select by default.
- Throughput/power from `chip.performance_report()` (`output_throughput_per_us`,
  `total_power_mw`, `energy_per_output_pj`) and per-stream rates from
  `TraceModel.stream_summary()` (the GUI's own methodology).
- All figures are deterministic (identical across repeated runs).
