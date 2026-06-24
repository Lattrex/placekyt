<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Block verification — per-block lessons log

Append-only, newest first. One entry per block as it is verified: what was tried,
what passed/failed, the derived tolerance, and any block-specific gotcha. Promote
anything that generalizes across block classes into `invariants.md`.

---

## FIRFilterBlock — SATURATION fix (Q15 overload) 2026-06-24

- **The bug:** the multi-cell FIR let the Q15 accumulator WRAP on signed overflow
  (modulo 2^16) — which flips sign on overload and produces garbage. GNU Radio's
  `fir_filter_fff` is FLOAT and never overflows, so the only correct fixed-point
  equivalent is a SATURATING accumulator (clamp to ±full-scale), as every
  production fixed-point FIR does (TI C5x/C6x). Under full-scale random input the
  chained partial sums overflow and the old block returned corr ~0.5–0.8 vs a
  correct saturating reference.
- **The fix — per-step software clamp:** the ALU has no auto-saturating mode;
  MACQ/ADD WRITE the wrapped value but set the V (signed-overflow) flag. On
  overflow the wrapped result's sign (N) is INVERTED vs the true sum, so the
  3-instruction clamp **`BR.NV +2 ; SHR R0,#15 ; SUB satneg,R0`** computes
  `0x8000 − (R0>>15)` = `N? 0x7FFF : 0x8000` — exactly the right rail. One branch
  on the hot path, two instructions on the (rare) overflow path, ONE shared
  `satneg=0x8000` data word per cell. Verified bit-exact vs a true clamping
  accumulator over millions of random cases AND against the live simulator.
- **DO NOT clamp the priming MULQ.** A single Q15 product `(a·b)>>15` is always
  representable, but **MULQ sets V from the RAW 32-bit product** (which almost
  always exceeds i16). Clamping on it saturates spuriously — the first cut did,
  pinning every output at the rails even in-range. Clamp only the running MACQ
  taps and the cross-cell partial ADD (whose V truly signals acc overflow).
- **Budget/fold impact (INV-7/9):** the clamp costs ~3 extra instrs/tap, so the
  per-cell register budget fills far sooner. Re-derived with the resolver's own
  allocator: single-cell ceiling **7 → 3 taps**, **TAPS_PER_CELL 5 → 2** (a mid
  cell at L=3 overflows the 32-word cell; L=2 fits first/mid/last). `satneg` must
  get an EXPLICIT address (after the coeffs) — an auto address packs at 0 = R0
  and corrupts the accumulator; `partial_reg` shifted +1 to account for it.
- **The verified range moved (more cells/tap):** 64 taps is now 32 cells (was 13)
  but still routes (FOLD_HEIGHT=4 serpentine = 8 wide). The routing wall dropped
  from ~400 taps to **96 taps / 48 cells** ("no free corridor"); 80 taps / 40
  cells still routes. Guard test updated to 96 (the `corridor` reason string is
  unchanged so the check still matches).
- **Reference = bit-exact predictor, not the float ideal (INV-3 sharpened):**
  `compare_against_grc`'s `_saturate_ref_q15` only clips the FINAL value, not
  each step, so it cannot predict a per-step-saturating DUT once an INTERMEDIATE
  sum overflows. Added `process_reference_q15` which models (a) the per-step
  clamp and (b) the CELL-ACCURATE wavefront: each cell holds its own segment
  delay line, ingests the PREVIOUS cell's shifted-out oldest sample (the inter-
  cell forwarding IS the delay — a naive global-delay-line index is WRONG, it
  failed at corr 0.86 on asymmetric taps while the DUT held corr 1.0 vs GR). The
  scaling/overload/deep-cell gates compare the DUT against this reference EXACTLY
  (Metric.EXACT, 0 LSB). A separate test proves the saturating reference equals
  GR-float-clipped where no overflow occurs — so it is real DSP, not circular.
- **Mandatory mutation (INV-4):** `test_fir_overload_wrap_mutation_fails`
  synthesises the OLD wrapping output for an overload case and asserts the gate
  REJECTS it — a gate that can't tell saturate from wrap certifies the bug.
  `test_fir_overload_saturates` additionally asserts the DUT outputs are pinned
  at the rails (proof it clamped, not coincidentally landed in range).

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
