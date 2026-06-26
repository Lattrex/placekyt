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
   and conjugate-multiply. **DONE:** `ConjugateBlock` — single cell, EXACT vs GR
   (manifest, 2026-06-26).
6. **Float → Short / Short → Float / scaling** — `blocks.float_to_short`,
   `short_to_float`, `blocks.multiply_const` already covers scale; add the int casts.
   **NOT A DISTINCT CHIP BLOCK (resolved 2026-06-26):** see the deferred section —
   on a uniformly-16-bit Q15 substrate these are host-side representation casts whose
   only on-chip computation is the constant scale, already verified as GainBlock.
7. **Abs / Negate / RMS** — `blocks.abs_ff`, the unary sign flip, `blocks.rms_cf`.
   **DONE (abs):** `AbsBlock` — single cell, conditional negate, verified vs
   `blocks.abs_ff` (manifest, 2026-06-26). NEGATE = `GainBlock(gain=-1)` (already
   covered, no separate block). `rms_cf` deferred (sqrt + state; see below).
8. **Interleave / Deinterleave** — `blocks.interleave`, `deinterleave` (stream rate
   change; simple counter+route — may be 2-cell, still Tier-1-ish).
   **DEFERRED to Tier-2 (below):** multi-stream, multi-rate, N-stream param changes
   topology, and it's pure reordering (no DSP) — doesn't fit the single-rate harness.
9. **Repeat / Keep-1-in-N (decimate-by-drop)** — `blocks.repeat`,
   `blocks.keep_one_in_n`. Rate adapters distinct from the FIR decimator.
   **DONE (keep_one_in_n):** `KeepOneInNBlock` — single cell, mod-n emit gate,
   verified vs `blocks.keep_one_in_n` (manifest, 2026-06-26). `repeat` deferred (below).
10. **Moving Average** — `blocks.moving_average_ff`. Box filter; a very common
    smoother (could also be a FIR-of-ones, but the GRC block is its own thing).
    **DONE:** `MovingAverageBlock` — subclasses FIRFilterBlock with constant box
    taps [scale]*length, verified vs `blocks.moving_average_ff` (manifest, 2026-06-26).
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

- **`blocks.float_to_short` / `blocks.short_to_float`** — not built (resolved
  2026-06-26): host-side representation casts, not a distinct chip computation. On
  the Kyttar bus EVERY datum is a 16-bit word; a Q15 "float" (w/32768) and an int16
  "short" are the SAME 16 bits. float_to_short(scale) = round((w/32768)·scale)
  saturated-to-int16 = exactly a constant multiply by scale/32768 — i.e. GainBlock /
  multiply_const_ff (already verified), differing only in round-vs-truncate. At the
  natural scale=32768 it is the IDENTITY; at scale=1 it is the degenerate round
  to {−1,0}. short_to_float is its inverse (divide by scale = gain 32768/scale,
  which exceeds Q15 range). No new, faithfully-GRC-verifiable on-chip block exists
  beyond GainBlock, so none is added (matches the backlog scope rule excluding
  host-side plumbing, and the item's own "multiply_const already covers scale" note).
- **`analog.rms_cf` / `blocks.rms_*` (running RMS)** — deferred 2026-06-26. RMS =
  √(single-pole-averaged |z|²): it needs BOTH the deferred Q15 sqrt (see
  complex_to_mag) AND a stateful IIR averager (an `alpha` param + persistent
  accumulator). Stateful + sqrt → Tier-2, not autonomous single-cell Tier-1.

- **`blocks.interleave` / `blocks.deinterleave`** — deferred 2026-06-26. Multi-rate
  AND multi-stream: interleave merges N input streams into one at N× rate;
  deinterleave splits one into N at 1/N rate. The stream count N is a param that
  changes the block's PORT TOPOLOGY (N input or N output ports), and the operation
  is pure sample REORDERING (no arithmetic). The verify harness models one logical
  rate with ≤2 fan-in operands and one primary output; an N-port multi-rate reorder
  needs a multi-stream driver — harness extension + human review, not Tier-1.
- **`blocks.repeat` (upsample-by-repetition)** — deferred 2026-06-26. repeat(interp)
  emits each input sample `interp` times → output rate = interp× input. The compute
  is trivial (write the input `interp` times per trigger), but `run_block_dut`
  records only ONE word per trigger (`got[-1]`), so it cannot capture the multiple
  copies that ARE the block's behavior; verifying the repeat COUNT needs an
  all-words-per-trigger capture (a harness option) — human review. (keep_one_in_n,
  the downsampling twin, IS done — the harness already records None for dropped
  samples, the decimator path.)

## Notes for the agent
- Mirror the GRC block's params verbatim; derive the Q15 internals (the GRC-parity
  rule). A block that needs a param GRC expresses as a float → store the fixed-point
  conversion in the block, expose the float.
- Two-input blocks (multiply/add) need the complex-burst broker delivery already
  proven for the Costas xi/xq tap — reuse it; don't reinvent fan-in.
- If a candidate turns out to be multi-cell or to change topology with a param, it
  is NOT Tier-1 — flag it and move on (it becomes a Tier-2 item for CM).
- Commit per block; update the manifest + the generated status dashboard.
