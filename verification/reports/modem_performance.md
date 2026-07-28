<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Modem & transceiver performance — simplex, saturated

All seven example designs run on **one 120-cell (10×12) Kyttar asynchronous array**,
each a full transceiver (transmit and receive chains co-resident on the array, sharing
one input port and one output port). The figures below are the **simplex** operating
point: each direction driven **alone**, **saturated** (whole burst queued back-to-back),
so each chain reaches its own compute-bound ceiling with no cross-chain contention —
the peak per-chain rate.

Reported per direction is the **sink (output) sample rate**: the rate at which the
chain emits results (recovered symbols for a demod, passband/audio samples for a
modulator). It is the `settled_sps` from `TraceModel.stream_summary()` — the exact
"Settled rate" the GUI Stream Summary panel shows.

Correctness was verified at the same saturated operating point: **BER 0** for the
digital modems, **audio correlation** vs the input for the analog transceivers.

> Simplex only, by design: the full-duplex (interleaved) rate depends on how the two
> chains time-slice the single shared port and is contention-window sensitive, which
> makes it easy to misread. Simplex is the clean, reproducible per-chain number. To see
> the full-duplex figure yourself, set the Kyttar Source **Duplex schedule =
> Interleaved** (see the block help); Sequential reproduces the numbers here.

## Results

Per direction: the sink (output) sample rate and the **total power drawn** (active +
idle) while that chain runs alone. RX and TX differ because each lights up a different
set of cells.

| Design  | Modulation   | RX rate    | RX power | TX rate    | TX power | Correctness |
|---------|--------------|-----------:|---------:|-----------:|---------:|:-----------:|
| BPSK    | 1 bit/sym    | 188 kSa/s  | 9.6 mW   | 481 kSa/s  | 7.6 mW   | BER 0       |
| QPSK    | 2 bit/sym    | 172 kSa/s  | 8.2 mW   | 460 kSa/s  | 9.1 mW   | BER 0       |
| 4FSK    | 2 bit/sym    | 542 kSa/s  | 15.2 mW  | 225 kSa/s  | 4.2 mW   | BER 0       |
| 16-QAM  | 4 bit/sym    | 146 kSa/s  | 8.9 mW   | 460 kSa/s  | 11.5 mW  | BER 0       |
| AM      | analog audio | 460 kSa/s  | 10.1 mW  | 479 kSa/s  | 5.9 mW   | corr 0.998  |
| FM      | analog audio | 1.93 MSa/s | 6.3 mW   | 429 kSa/s  | 5.7 mW   | corr 0.996  |
| SSB     | analog audio | 346 kSa/s  | 14.0 mW  | 346 kSa/s  | 14.4 mW  | corr 0.97   |

Notes:

- **Idle power ~0.4–0.6 mW** across all designs (included in the totals above) — the
  array is asynchronous, so only active cells draw power.
- **FM RX (1.93 MSa/s on 3 active cells)** is the fastest demodulator: its receive path
  is a bare quadrature discriminator (a MAC of the conjugate product, no feedback loops).
- **SSB (~14 mW each way, ~35–38 active cells)** is the heaviest — the Weaver
  third-method topology's complex-FIR filtering.
- All figures freshly measured on the current simkyt build; deterministic (identical
  across repeated runs). Power is the chip's own `performance_report()` `total_power_mw`.

## Method

- Real path only: **open the shipped `.kyt`** (hand-placed, as a user does), build,
  host on the SimServer. No hand-built reconstructions, no re-import/re-route.
- **Simplex, saturated:** one `process_batch_duplex` RPC with
  `schedule="sequential", pipelined=True` — each direction's whole burst runs alone,
  back-to-back (the GUI "Duplex schedule = Sequential" + "Full-speed (saturated)" Run).
- Each direction runs on its OWN freshly-hosted chip, so both the trace and the
  `performance_report()` reflect ONLY that direction — that's what makes the RX and TX
  power numbers separable.
- Per-direction sink rate = `TraceModel.stream_summary()` `settled_sps` for that
  chain's output stream (matched by `out_tag`) — the GUI Stream Summary methodology.
  Per-direction power = `chip.performance_report()` `total_power_mw`.
- Reproduce with: `QT_QPA_PLATFORM=offscreen .venv/bin/python verification/simplex_rates.py`
