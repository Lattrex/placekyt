<!--## FIRFilterBlock — verified (in range) 2026-06-23

- **Status:** IN PROGRESS. Verified 2-7 taps (single cell): filters correct vs
  GNU Radio `filter.fir_filter_fff` within the derived per-tap tolerance
  (op_count = tap count, so tolerance = taps+1 LSB). Edge + 3 random seeds +
  tap-count sweep 2..7 + mutation tests (inverted, wrong-taps, delay offset).
  Result: 1-4 LSB error, correlation 1.0.
- **GR convention:** GNU Radio `fir_filter_fff` convolves with taps in
  latest-sample-first order — pass `reversed(coefficients)` to the reference.
- **KNOWN LIMITS (not done until fixed):** 8+ taps fail the single-cell register
  budget (INV-7); 13+ taps build multi-cell but produce no egress through the
  single-block harness (output exits the last cell, not cells[0]). Guarded by
  executable known-limit tests that flip when fixed.
- **Harness fix it forced:** entry must be resolved WITH the block's params, not
  the bare type name — see INV-6. Without it the FIR echoed its input.

---

 SPDX-License-Identifier: GPL-3.0-or-later -->

# Block verification — per-block lessons log

Append-only, newest first. One entry per block as it is verified: what was tried,
what passed/failed, the derived tolerance, and any block-specific gotcha. Promote
anything that generalizes across block classes into `invariants.md`.

---

## GainBlock — verified 2026-06-23

- **Status:** PASS. Edge + 3 random seeds + gain sweep {0.25, 0.5, 0.75, 0.9}.
- **Metric:** amplitude, delay=0, op_count=1 → derived tolerance 2 LSB.
- **Result:** max_abs_err 1 LSB, NMSE ~-90 dB, corr 1.0000. The 1-LSB error is
  correct Q15 rounding of a single MULQ (e.g. 0x7FFF*0.5 = 0x3FFF).
- **Mutation tests:** inverted output, wrong gain, +1 sample offset, empty output
  all correctly FAIL the gate.
- **Gotcha:** hit the placement-dependent hop-count trap (zero output) before the
  fix — see invariants.md INV-1. GainBlock is the template for feed-forward,
  single-cell, single-MULQ blocks.
