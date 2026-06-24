<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Block verification — per-block lessons log

Append-only, newest first. One entry per block as it is verified: what was tried,
what passed/failed, the derived tolerance, and any block-specific gotcha. Promote
anything that generalizes across block classes into `invariants.md`.

---

## FIRFilterBlock — verified (2..64 taps) 2026-06-24

- **Status:** PASS / DONE. Verified vs GNU Radio `filter.fir_filter_fff` from 2 to
  64 taps (the headline target) within the derived per-tap tolerance (op_count =
  tap count → tolerance = taps+1 LSB). 1-7 taps single-cell; 8+ a multi-cell
  chained partial-sum (systolic) wavefront. Coverage: edge + 3 random seeds +
  single-cell sweep 2..7 + multi-cell sweep {8,9,13,16,32,64} + 4 mutations
  (inverted, wrong-taps, +1 delay, **deep-cell tap**). Result: corr 1.0000,
  error well inside tolerance (e.g. 64-tap: 40 LSB of 65). Probing shows the same
  design stays correct to ~360 taps (72 cells).
- **GR convention (unchanged):** `fir_filter_fff` convolves latest-sample-first —
  pass `reversed(coefficients)` to the reference. The single-cell datapath and
  the multi-cell datapath BOTH match this; keep them on one convention.
- **Two substrate bugs fixed (promoted to invariants):**
  - **Multi-cell egress (INV-11):** the auto-router/placer resolved the block's
    PortMap from the bare type (default = single-cell), so a 13-tap FIR routed
    its output from cell 0, not the real last cell → no egress. Fix: thread
    `block.params` into PortMap resolution across autoroute/bus_router/controller
    AND the autoplacer footprint/port-map providers (an arity adapter keeps old
    2-arg providers working).
  - **Single-cell budget (INV-7):** the old `<=12 taps => 1 cell` threshold
    overflowed the ~31-register cell at 8 taps. Real ceiling is 7; 8+ now fold to
    multi-cell.
- **The bug the OLD 'green' suite hid (INV-12):** the borrowed RRC multi-cell code
  reversed each coefficient SEGMENT — correct only for SYMMETRIC taps. The prior
  suite used EDGE (10 samples) + uniform positive taps, so the deep cells never
  saw data and the mis-ordering cancelled. Under >2*ntaps random input with
  asymmetric taps even an 8-tap (2-cell) FIR failed (corr ~0). Fix: FIR now has
  its OWN multi-cell builder; each cell takes `coeff[N-offset_{m+1} : N-offset_m]`
  in FORWARD order (derived from the cascaded-delay structure, validated against
  the single-cell datapath in float before touching the chip).
- **Layout FOLD (INV-8/9/10) — the GUI revealed it; the harness hid it:** the
  base-class auto-snake laid 8 cells as a 1x8 LINE, so input and output sat on
  OPPOSITE edges → the single bus can't tap both → in GUI place+route the block
  built but the gain→FIR net would not route (a flyline), even though the headless
  verification harness "passed" (it injects/drains directly, not via the bus).
  Fix: FIR now authors an explicit `default_layout` — a column-major serpentine
  fold (down a column of FOLD_HEIGHT=4, over one, up the next). 40 taps (8 cells)
  → the canonical **2x4** with input @(0,0) and output @(1,0) SIDE BY SIDE on one
  edge → `portmap.io_colocated=True`, and the bus taps both. Consecutive cells
  stay adjacent so the wavefront forwarding is unchanged (verification still 21/21).
  LESSON: a headless DUT-vs-GR pass does NOT prove a block places+routes in the
  real GUI/bus flow — verify both. (Now in layout_rules.md + INV-8/9/10.)
- **Known limit (guarded, genuine substrate wall):** ~400 taps (80 cells) exceeds
  the 10x12 array's routing capacity (≤8 cells across per INV-9). The folded
  footprint can't leave a bus channel. `test_fir_routing_capacity_limit` asserts
  it fails to route; flips if the array grows. NOT a tap cap faked to pass.
- **Method note:** model the datapath in plain float FIRST (single-cell vs
  multi-cell) to localise a structural index bug in seconds, before paying for
  build+sim+GNU-Radio round trips.

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
