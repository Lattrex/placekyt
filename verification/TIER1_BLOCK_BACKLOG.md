# Tier-1 block backlog (for the VM agent)

**Scope rule (CM-locked):** the most-used GNU Radio **signal-processing** blocks that
map cleanly to a chip cell (memoryless or simple-state, fully auto-placeable and
auto-routable). EXCLUDES host-side flowgraph plumbing that is not a chip block —
GUI sinks/sources, Throttle, File source/sink, Virtual source/sink, Variable, etc.
(those are handled by the host bridge or are GRC-only).

**Acceptance per block (same as existing Tier-1):** `compare_against_grc` layered
gate — DUT == simKYT bit-exact AND DUT ≈ GRC within the derived Q15 tolerance;
add to `verification/manifest.json` (tier 1, status `done` once green); the block
must auto-place + auto-route + build with NO manual steps. Work in the order below
(roughly descending popularity / ascending effort).

## Already covered (do NOT redo)
Gain, MultiplyConst (cc/ff), AddConst, ComplexMixer (multiply_cc), NCO (sig_source_c),
QuadratureDemod, FreqXlatingFIR, DC blocker, FIR (+ low/high/band-pass, band-reject,
decimator, RRC), IIR biquad, SoftDemod, binary slicer, Costas, symbol_sync, AGC*,
Squelch*  (*= present but Tier-2/verify-pending; not Tier-1 agent work).

## Tier-1 backlog — build these (popularity-first)

1. **Multiply (two-stream)** — `blocks.multiply_cc` / `multiply_ff`. Two data inputs,
   product out. (We have multiply_CONST and the mixer; the generic two-stream
   multiply is the missing staple — AM detect, squaring, etc.)
   **DONE (multiply_ff):** `MultiplyBlock` — single MULQ, verified vs
   `blocks.multiply_ff` (manifest, 2026-06-26). `multiply_cc` deferred (below).
2. **Add / Subtract (two-stream)** — `blocks.add_ff` / `add_cc` / `sub_ff`. Two
   inputs → sum/difference. Ubiquitous (combiners, error nodes).
   **DONE (add_ff, sub_ff):** `AddBlock` / `SubtractBlock` — single cell, ADD/SUB +
   saturating clamp, verified vs `blocks.add_ff` / `blocks.sub_ff` (manifest,
   2026-06-26). `add_cc`/`sub_cc` deferred (below, same 4-operand reason as multiply_cc).
3. **Complex → Float / Float → Complex** — `blocks.complex_to_float`,
   `float_to_complex`. The single most common type-conversion pair in any I/Q graph.
   **DONE:** `ComplexToFloatBlock` / `FloatToComplexBlock` — single-cell identity
   I/Q passthrough, EXACT vs GR (manifest, 2026-06-26).
4. **Complex → Mag / Mag² / Arg** — `blocks.complex_to_mag`, `complex_to_mag_squared`,
   `complex_to_arg`. Envelope/power/phase — used in every detector & AGC.
   **DONE (mag_squared):** `ComplexToMagSquaredBlock` — single cell (MULQ+MACQ,
   saturating), verified vs `blocks.complex_to_mag_squared` (manifest, 2026-06-26).
   `complex_to_mag` (sqrt) and `complex_to_arg` (atan2) deferred (below).
5. **Conjugate** — `blocks.conjugate_cc`. Trivial (negate Q); needed for correlators
   and conjugate-multiply.
6. **Float → Short / Short → Float / scaling** — `blocks.float_to_short`,
   `short_to_float`, `blocks.multiply_const` already covers scale; add the int casts.
7. **Abs / Negate / RMS** — `blocks.abs_ff`, the unary sign flip, `blocks.rms_cf`.
8. **Interleave / Deinterleave** — `blocks.interleave`, `deinterleave` (stream rate
   change; simple counter+route — may be 2-cell, still Tier-1-ish).
9. **Repeat / Keep-1-in-N (decimate-by-drop)** — `blocks.repeat`,
   `blocks.keep_one_in_n`. Rate adapters distinct from the FIR decimator.
10. **Moving Average** — `blocks.moving_average_ff`. Box filter; a very common
    smoother (could also be a FIR-of-ones, but the GRC block is its own thing).
11. **Complex → Real / Imag selectors** — `blocks.complex_to_real`,
    `complex_to_imag` (subset of #3 but separate GRC blocks people wire directly).

## Deferred to Tier-2 (needs human review)

- **`blocks.multiply_cc` (two EXTERNAL complex streams)** — deferred 2026-06-26.
  The on-chip block is a single cell (the 4-MULQ complex product, same datapath
  the ComplexMixer's `mixer` cell already proves), so the COMPUTE is Tier-1. What
  blocks it is DELIVERY: two external complex streams = **four** input operands
  per trigger (ai, aq, bi, bq), but the proven complex-burst fan-in (and its
  `run_block_dut_complex` driver) delivers exactly **two** operands (xi@R0, xq@R1).
  Verifying multiply_cc needs a 4-operand burst driver — a verification-harness
  extension, i.e. human review — so it is NOT autonomous Tier-1. (The common
  multiply-by-a-complex-exponential case is already covered by ComplexMixerBlock;
  `multiply_const_cc` — complex × CONSTANT — is the planned tier-3 MultiplyConstComplex.)
- **`blocks.add_cc` / `blocks.sub_cc` (two EXTERNAL complex streams)** — deferred
  2026-06-26. Same blocker as multiply_cc: a complex combiner of two external
  complex streams needs FOUR input operands per trigger (ai, aq, bi, bq); the
  proven complex-burst fan-in delivers exactly two. The compute is trivial (two
  saturating adds, the AddBlock datapath twice), so this is purely a 4-operand
  burst-driver (harness) extension — human review, not autonomous Tier-1.

- **`blocks.complex_to_mag` (|z| = √(re²+im²))** — deferred 2026-06-26. Needs a
  Q15 square root accurate to ~1 LSB to match GR's float sqrt within the gate. No
  sqrt/CORDIC machinery exists in the codebase; the magnitude estimators that ARE
  single-cell (alpha-max-plus-beta-min) are APPROXIMATIONS (several-% error) that
  fail a sqrt-exact gate. A real Q15 sqrt (reciprocal-sqrt Newton with a table seed,
  or CORDIC vectoring) is a new multi-step algorithm needing its own design +
  verification — human review, not autonomous single-cell Tier-1.
- **`blocks.complex_to_arg` (atan2(im, re))** — deferred 2026-06-26. Needs a
  full-range four-quadrant arctangent. No atan/CORDIC machinery exists (the NCO is a
  FORWARD sin/cos table; arg is its inverse). A table-atan needs a divide (im/re)
  the ISA lacks; CORDIC vectoring needs ~12 iterations (multi-cell). New algorithm,
  human review — Tier-2. (Closely related to the planned tier-3 QuadratureDemod,
  which is also atan-based; build the shared CORDIC once for both.)

## Notes for the agent
- Mirror the GRC block's params verbatim; derive the Q15 internals (the GRC-parity
  rule). A block that needs a param GRC expresses as a float → store the fixed-point
  conversion in the block, expose the float.
- Two-input blocks (multiply/add) need the complex-burst broker delivery already
  proven for the Costas xi/xq tap — reuse it; don't reinvent fan-in.
- If a candidate turns out to be multi-cell or to change topology with a param, it
  is NOT Tier-1 — flag it and move on (it becomes a Tier-2 item for CM).
- Commit per block; update the manifest + the generated status dashboard.
