<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Block verification — substrate invariants

Hard-won, model-agnostic rules that apply across block classes. An agent building
or verifying a Kyttar block should read these first. Each is a *constraint* ("always
/ never X"), not a one-block idiosyncrasy. Per-block fixes go in `lessons_log.md`.

---

## RULE FOR WRITING IN THIS FILE — a stated LIMIT must carry its evidence

Added 2026-08-29, after an audit found several fabricated limits in here.

**A false limit is worse than a missing one.** Every agent reads this file before
building, so a wrong "you cannot do X" is applied as a design constraint by every
future builder — it makes correct designs get abandoned, shrunk, or quarantined,
and it propagates into commit messages, plans and reports where it is even harder
to dislodge. Three such claims were found in this file, and one of them
(a fabricated panel cell cap) kept a working block quarantined.

So, before writing any sentence of the form **"cannot" / "limit" / "maximum" /
"caps at" / "is not placeable" / "does not fit"**:

1. **MEASURE IT.** Run it, or point at a test that fails without the claim. Do not
   derive a limit by reading code — `lessons_log.md` has said
   *"DO NOT DERIVE A 'KNOWN LIMIT' FROM CODE-READING — MEASURE IT"* since long
   before these errors, and it was ignored anyway.
2. **CHECK THE SHIPPED BLOCKS FIRST.** There are 100+ verified blocks. If one of
   them already does the thing you are about to declare impossible, the claim is
   dead. This alone would have caught every false limit the audit found:
   `FFT64Block` is 9×12 (refutes "≤8 across"); `GolayDecoderBlock` is a 7-cell
   panel block (refutes the panel cap).
3. **SAY WHICH LAYER.** Hardware/ISA limits are permanent. **Toolchain limits are
   FIXABLE and must say so**, with a pointer at the code that would change. Calling
   a placer gap a "substrate wall" is how a fixable bug becomes folklore.
4. **STATE THE REACH.** "Measured on N blocks" is not "all blocks". A true
   observation about a handful of cases, written as a universal law, is the single
   most common failure mode found in the audit — every one of the false claims had
   this shape.
5. **NEVER DELETE A CORRECTION.** When a claim here turns out to be wrong, mark it
   corrected, in place, with the date and the evidence that killed it. The wrong
   version has usually been copied elsewhere, and a reader needs to recognise it.

---

## INV-0 — MATCH THE GNU RADIO BLOCK'S PARAMETERS EXACTLY (deviate ONLY for hardware, and say so LOUDLY)

**This is the most important rule. Violating it silently makes automated block
generation worthless — the block does not mean what its name claims.**

A Kyttar block that maps to a GNU Radio block MUST expose the **same parameters**,
with the **same generality and units**, as that GNU Radio block:

- GR takes an **arbitrary table** (constellation `symbol_table`, FIR `taps`, IIR
  tap lists) → the Kyttar block takes an arbitrary table. **NOT** a fixed enum of
  presets, **NOT** a hardcoded set.
- GR takes a **real-world** param (`frequency` Hz + `sample_rate`, `db`, `alpha`)
  → expose that. **NEVER** a hardware-internal proxy (`freq_word`, raw Q15 coeff)
  *instead*; derive the internal value inside the block.
- GR bundles a behavior as a **parameter** (e.g. `fir_filter_fff(decimation, taps)`)
  → it stays a parameter on ONE block. **Do NOT split a GR block into two Kyttar
  blocks to dodge a parameter.**
- Match GR's exact param **names and defaults**.

**The ONLY permitted deviation is a genuine HARDWARE/ISA limit** (Q15 `[-1,1)`,
32 words/cell, finite cells, one-output-per-input). "It was easier", "the demo only
needs X", or "it fits the common cases" are NOT hardware limits. If the full GR
parameter is implementable within the ISA, you MUST implement it (e.g. a 32-entry
constellation table FITS 32 words/cell → there is no excuse for a 3-mode enum).

When a deviation is a REAL hardware limit, document it **CLEARLY and LOUDLY** in
THREE places, and **raise** (never silently clamp/ignore) on an unsupported value:
1. a `# HARDWARE DEVIATION:` comment at the param in `__init__`,
2. a `Hardware deviations from <gr_block>:` heading in the class docstring,
3. the manifest `notes`, prefixed `HW-DEVIATION:`.

**`done` bar:** a block is `done` ONLY when its test sweeps the WHOLE declared
parameter space (every enum value; representative points across each continuous
range; arbitrary tables exercised with several real tables) against a GR golden
built from the SAME params. One default config is not "done".

**If unsure whether a deviation is truly hardware-forced: STOP and ASK. Never
decide it unilaterally and never deviate without saying so.**

---

## INV-1 — The port target hop count is PLACEMENT-DEPENDENT, never a constant

**Symptom:** a block builds and routes fine but produces **zero outputs** on simKYT.

**Root cause:** the value passed as `target_hop_cnt` to `inject_data_physical` /
`inject_jump_physical` must equal `31 - distance`, where `distance` is the number of
cells the word transits from the `x16_in` port cell to the block's landing cell
(inclusive of the port's own edge cell). The demo tests hardcode `30` only because
their auto-placed head block happens to sit 1 hop from the port. A block placed
elsewhere needs a different hop, and a wrong hop means the WRITE/JUMP is consumed at a
transit cell short of (or past) the block → the program never fires → no output.

**Fix:** derive it from the landing cell position:
`hop = 31 - (abs(landing.x - port.cell_x) + abs(landing.y - port.cell_y) + 1)`.
The **same** hop must be used for both the data inject and the jump inject. (Verified:
GainBlock at (1,1), x16_in at (0,0) → distance 3 → hop 28. Hop 29/30 give 0 output.)
`run_block_dut` in `kyttar_verify` does this for you.

**REFINEMENT (distance = ROUTED CORRIDOR LENGTH, not manhattan span).** `distance` is the
number of cells the word actually TRANSITS on its `fwd_face` corridor — which equals the
manhattan span ONLY when the router drew a straight corridor. Under some D4 orientations
the auto-router **snakes** the corridor (e.g. a single-real-rail block whose input cell
lands on the far edge: routed length 10 vs manhattan 8), and a manhattan-derived hop stops
the injected word 2 cells SHORT → **zero/None output that looks exactly like a datapath /
rotation bug but is a HARNESS bug**. This masqueraded as an "NCO anti-orientation
invariance failure" for a long time. So when the port→block input net is ROUTED and
anchored at the port cell, derive `dist = len(route)` (the point list `[port_cell,
...transit..., landing]`); fall back to the manhattan span only for the unrouted /
direct-on-port case. `run_block_dut` now does this. NOTE: the LIVE production path
(`build._resolve_input_landings` → `stream_targets`) already uses corridor-accurate hops
from the built faces, so the real modem was never affected — only the self-placing DUT
harness used the manhattan shortcut. When a rotated single-rail block gives zero output,
suspect the harness hop BEFORE the block.

**Applies to:** every block driven through x16_in by a harness that places blocks itself.

---

## INV-2 — DUT-vs-reference alignment uses the PREDICTED delay, not a lag search

**Symptom (latent):** a block with a real group-delay / off-by-one bug passes anyway.

**Root cause:** cross-correlation lag search picks whatever offset maximizes
similarity, so it slides the streams until a latency bug disappears. Free alignment is
a bug-eraser.

**Fix:** state the block's known group delay (FIR ≈ `(ntaps-1)/2`, a memoryless block
= 0) and assert the DUT exhibits it; compare `y[n]` to `ref[n-delay]`. A `+1` latency
mutation must FAIL when `delay=0` is asserted — that mutation test is mandatory.

**Applies to:** all amplitude/decision metrics.

---

## INV-3 — Model Q15 saturation on the float reference before comparing

**Symptom:** full-scale edge vectors (±1.0, 0x7FFF, 0x8001) false-fail on a correct block.

**Root cause:** Q15 saturates; GR float does not. At full scale the DUT clips where the
float reference keeps growing → spurious large error on exactly the edge vectors we
emphasize. "Fixing" this by loosening the global tolerance hides real errors elsewhere.

**Fix:** clip the float reference to the Q15 range and quantize it *before* diffing.
(`compare_against_grc` does this.) The `0x7FFF * 0.5 = 0x3FFF` single-LSB result is
correct Q15 rounding, not an error — expect ≤1 LSB on a single MULQ.

**Applies to:** every amplitude-metric block; especially gain/mixer/filter at full scale.

---

## INV-4 — A verification gate is worthless until it is proven to FAIL

**Symptom:** "all green" that certifies nothing because the gate can't detect a bug.

**Root cause:** loose tolerance, free lag alignment, transient trimming, or stimulus
that never excites the bug each let a broken block pass green.

**Fix:** every block's test suite MUST include mutation/negative tests that corrupt the
DUT (invert output, wrong parameter, +1 sample delay, empty output) and assert the gate
FAILS. Only then does a green result mean "the gate looked and found nothing," vs
"the gate can't see." Tolerances are derived/locked, never tuned by the agent to pass.

**Applies to:** every block, no exceptions.

---

## INV-5 — Single-block build recipe (the proven path)

`new_project(name, chip_type)` → `place_block(type, 0, x, y, library="lattrex.official",
params=...)` → two `add_logical_connection` calls wiring the block's input port to
`ChipPortEndpoint(0,"x16_in")` and its output port to `ChipPortEndpoint(0,"x16_out")` →
`auto_route_all({chip_type: ct})` → `BuildEngine(cat, yaml).build(project, {chip_type: ct})`
→ drive `simkyt.Chip` (load_bitstream_physical, set_port_entry_address, then per sample:
inject_data_physical + run, inject_jump_physical + run, drain output_available /
read_port_i16 / release_output_ack). Entry + input register come from
`catalog.resolved_io(type)`. Port names for a simple block are `sample` (in) / `out`.

**Gotcha:** setting `project.chip_type` alone is NOT enough — use `new_project(...)` so
the chip instance is initialized; otherwise `block.placement` is None and the router
reports "source block unplaced or port unknown." Pass `library="lattrex.official"`.

**Applies to:** any headless single-block build (the DUT side of verification).

---

## INV-6 — Resolve a block's entry address WITH its params, never the bare type

**Symptom:** a parameterized block (FIR, anything whose program size varies)
**echoes its input** unchanged, or produces garbage, while the build and route
succeed.

**Root cause:** v2 blocks pack data low and instructions high, so a block's
program length — and therefore its **entry address** — shifts with its
parameters. `resolved_io(type_name)` with NO params constructs the block's
*default* (e.g. a 1-tap FIR → entry 27); the actually-placed block (e.g. a 3-tap
FIR → entry 23) has a different entry. JUMPing to the default entry lands
mid-program (past the input-load and accumulator-prime), so the datapath never
computes a clean output and the raw input passes through.

**Fix:** always resolve with the instance's real params:
`entry, ins = cat.resolved_io(type_name, params)`. `run_block_dut` does this.
(GainBlock hid this — its program length is fixed regardless of gain, so its
entry never moves and any block-class harness tested only on Gain would miss it.)

**Applies to:** every block whose program size depends on its parameters — i.e.
every scaling block (FIR, decimator, IIR, interleaver, …).

---

## INV-7 — A block's per-cell register budget (~31) caps a single-cell design

**Symptom:** a scaling block's build fails with "no register space" past some
parameter size, or builds as multi-cell but produces **no egress** through the
single-block harness.

**Root cause:** each cell has ~31 usable registers (R0 is the accumulator). A
single-cell FIR holds its coefficients + delay line + scratch + program; past
**6** taps that exceeds the budget (`MAX_SINGLE_CELL_TAPS = 6` in
`fir_filter_block.py`; measured: 6 taps → 1 cell, 7 taps → 2). Above the
single-cell ceiling the block becomes a multi-cell **wavefront** whose output
egresses from the *last* cell.

**Fix / status:** verify scaling blocks across their *proven* parameter range and
record the ceiling as an explicit known limit (executable guard tests that flip
when fixed) rather than claiming the block is fully done.

**CORRECTED 2026-08-29:** this used to end "multi-cell egress (driving the last
cell, not the first) is a harness capability still to be built." That was STALE
and contradicted INV-11 *in this same file*. It was built: `dut_runner.py` wires
`x16_in -> block -> x16_out` by PORT NAME and lets the router resolve the output
cell, and `test_fir_filter.py` verifies 7/8/11/16/20/32/**64**-tap FIRs
bit-exact. The "~7 taps" figure above was also off by one.

**Applies to:** FIR, decimator, IIR, and any block that grows past one cell.

---

## INV-8 — A multi-cell block must FOLD; I/O on the SAME edge (not a line)

**Symptom:** a multi-cell block builds fine but **never routes** — no output, no
error naming the cause. Or a feedback block needs an absurd full-width return path.

**Root cause:** the router runs a single **bus** along one edge of the block and
taps it. If input and output sit on *opposite* edges (the default for a straight
line of cells), the bus can't tap both, and the route fails silently. A feedback
loop laid out as a line puts producer and consumer at opposite ends, forcing a
full-width return (the early Costas: 7 cells + a 6-cell return for a 1-sample
delay = 13 cells).

**Fix:** lay the block out as a **serpentine fold**, with the external input
port(s) and the output port on the **same** (bus-facing) edge, within ~2 cells —
`portmap.py` then derives `io_colocated=True`. This is *observed, not enforced*:
nothing forces it, but a non-colocated block does not tap a single bus → does not
route on this chip. Author an explicit `default_layout` to fold; the base-class
auto-snake makes the block *compact* but does NOT guarantee same-edge I/O.

**Applies to:** every block of 2+ cells, especially anything with feedback.
See `layout_rules.md` for the full rationale and the canonical 2×4 fold.

---

## INV-9 — A fold is chosen for the SHAPE of the free space it leaves; ≤8 across is a DEFAULT for small blocks, not a law

**CORRECTED 2026-08-29.** This invariant previously read "keep a block ≤ 8 cells
across … a block wider than 8 in either dimension leaves no channel → routes fail
silently." That is **FALSE**, and it is **backwards for large blocks** — the case
where it was doing the most damage.

**Refuted by shipped, verified blocks:** `FFT64Block` **9×12** (84 cells, done),
`FFT128Die1` 9×12, `FFT32Block` 9×10, `GRUCellBlock` **10×6** — and, with **no
`CHIP_SCALE` waiver at all**, `ComplexToMagBlock` **9×2** and `CoherentRXBlock`
**10×2**. `layout_caps()` has no placer or DRC caller; the number was never
enforced anywhere.

**Backwards, measured — see INV-40.** `GRUCellBlock` folded to the *compliant*
8×7 left fragmented perimeter free space and its chain stayed **one net short
across ~8200 layouts**. The same 51 cells at the *non-compliant* **10×6** left six
full-width free rows and the identical chain routed and built at **102/120**.
Obeying this rule is what prevented that design from routing.

**The true rule:**
* **Small block → ≤8 across is a good default.** It leaves a channel on each side
  and makes I/O placement forgiving.
* **Block that dominates the array → go FULL width and declare `CHIP_SCALE`.**
  Full width leaves whole free ROWS, i.e. one contiguous through-channel; an
  8-wide fold leaves fragmented perimeter, and a closed-ring block can never
  enclose a channel at all.
* **The trade:** nothing reaches the far side of a full-width block, so its I/O
  must be on ONE edge facing the chip ports.

**What IS enforced:** a block extending past the fabric is caught as
`unplaced_cell` by the DRC — that failure is loud, not silent.

**Applies to:** any multi-cell block on the 10×12 array.

---

## INV-10 — A wavefront block's output exits the LAST cell, not cell 0

**Symptom:** a multi-cell filter injects correctly but reads back **nothing**.

**Root cause:** in a chained partial-sum (wavefront) block the input enters cell 0
but the partial sum flows 0 → 1 → … → N-1, and only the **last** cell produces the
finished output. A harness/driver that derives its drain from
`placement.cells[0]` reads the wrong cell and gets nothing.

**Fix:** declare the output port on the last cell; anything draining the block
(verification harness, bus tap) must target that cell's position, not cells[0].
Extending the single-block harness to drive cell 0 but drain the last cell is the
known capability gap behind the FIR multi-cell ceiling (INV-7).

**Applies to:** FIR, RRC, decimator, and any chained/wavefront multi-cell block.
---

## INV-11 — Resolve a block's PortMap (routing geometry) WITH its params

**Symptom:** a multi-cell, parameter-scaling block builds and routes with no
error but produces **no output** — the output egress goes nowhere. (For FIR this
read as the "multi-cell egress" limit: 13+ taps build but emit nothing.)

**Root cause:** the auto-router/auto-placer resolve a block's `PortMap` (its
input/output cell offsets) from the **bare type name**, NOT the placed instance's
params. A block whose footprint scales with params (FIR: cells = ⌈taps/5⌉) then
has its OUTPUT port located on the *default* construction's cell — a 1-tap FIR is
single-cell, so the output port is cell 0. The block→`x16_out` net is therefore
sourced from the FIRST cell, while the wavefront's result actually leaves the
LAST cell. The output WRITE (hop computed for the cell-0 route) fires from a cell
that isn't on that route → the word never reaches the port. This is the routing
twin of **INV-6** (which is about the *entry address*; this is about *port
geometry*).

**Fix:** thread the placed block's params into EVERY PortMap resolution on the
routing path — `catalog.port_map(type, block.params, library=...)`. In placeKYT
this is `engine/autoroute.py` (`_endpoint_cell`, `_block_out_anchor`,
`orient_for_flow`, …), `engine/bus_router.py`, and the `port_cells`/`port_maps`
provider closures in `ui/controller.py`. Make the provider callbacks accept an
optional 3rd `params` arg and pass it; keep an arity adapter so older
2-arg providers still work. (Verified: a 13-tap FIR routed its `out` net from
cell 0 (1,1) instead of the last cell (3,1); with params the net sources the
real exit and the wavefront egresses correctly.)

**Applies to:** every multi-cell block whose footprint/output cell depends on
params — FIR, decimator, and any scaling filter routed by the auto-P&R.

---

## INV-12 — Stimulus must be LONGER than the block's state depth

**Symptom:** a scaling/stateful block passes a green suite yet is actually broken;
the bug appears only at larger sizes or under different stimulus.

**Root cause:** a short stimulus never fills a deep delay line / state, so the
deep cells only ever multiply ZERO. The output then depends only on the first few
taps, and a bug in any later cell (wrong coefficient order, a dead handoff) is
invisible. A uniform / symmetric / all-positive tap set hides it further (many
wrong orderings coincide). This is exactly how a multi-cell FIR shipped a
coefficient-ordering bug under an "all green" gate (EDGE = 10 samples, uniform
positive taps): the deep cells were never exercised.

**Fix:** drive ≥ `2 * state_depth` samples (FIR: `2*ntaps`) of RANDOM input, with
an ASYMMETRIC parameter set, so every cell sees real data; and add a mutation
that perturbs the DEEPEST cell's parameter and asserts the gate FAILS — proof the
deep datapath is actually under test, not just the head. (This is INV-4 sharpened
for stateful blocks: a gate the stimulus never reaches certifies nothing.)

**Applies to:** FIR, IIR, decimator, equalizers, correlators — any block whose
internal state spans more than a couple of samples.

---

## INV-13 — Saturate a Q15 MAC chain with COEFFICIENT HEADROOM, not per-tap or end-only clamping

**Symptom:** a high-gain MAC-chain block (FIR/IIR/mixer, Σ|coeff| > 1) under
overload ROLLS OVER — the output sign-flips / folds back to small values instead
of pinning at the ±full-scale rails. (Concrete: a 40-tap all-0.5 FIR, gain 20, on a
steady 0.9 input rolled to `[…0.9, −0.875…]` — wrap garbage — instead of pinning at
+1.0.) Or: an attempt to fix it explodes the cell count (a 40-tap FIR → 40 cells).

**Root cause — the V flag is NOT sticky.** The cell ALU has NO auto-saturating
mode — `MACQ`/`ADD` WRAP (modulo 2^16) and only ADD/SUB/ADC/SBC set V at all. In a
high-gain MAC chain the running sum can overflow a MID-chain `MACQ` and WRAP BACK
into range by the final op, so V on the LAST op reflects nothing. Both naive fixes
therefore fail or are unacceptable:
  * **End-only clamp** (clamp R0 once, on the final op, off its V flag) MISSES the
    overflow whenever an intermediate sum wrapped and the final op landed back in
    range → it still rolls over. (The earlier INV-13 endorsed this; it was WRONG
    for Σ|coeff| > 1.)
  * **Per-tap clamp** (clamp after every accumulation) is correct but costs ~3
    instructions PER TAP, collapsing TAPS_PER_CELL to 1 (a 40-tap FIR → 40 cells).
    Rejected.

**Fix — COEFFICIENT HEADROOM (accumulator scaling).** Pre-scale the coefficients
so the running sum can NEVER overflow internally, then restore the gain + saturate
at the END:
  1. `S = max(0, ceil(log2 Σ|coeff|))` (from the ORIGINAL coeffs, at construction).
     Normalized filter (Σ|coeff| ≤ 1) → S = 0, a no-op (identical to a plain Q15
     FIR, bit-exact with GR). High-gain → S > 0.
  2. Scale every coeff by `2^-S` before Q15 conversion (store the SCALED coeffs).
     Now `Σ|scaled| ≤ 1`, so `|Σ scaled·input| ≤ 1` — the accumulator is in range
     at EVERY tap and EVERY cell; intermediate wrap is IMPOSSIBLE.
  3. At the very END (single cell: after the last MACQ; multi-cell: on the LAST
     cell after its final ADD) restore the gain with a SATURATING left shift by S.
     The shift is the ONLY place a true overdrive overflows, and it pins to
     ±full-scale. Intermediate cells forward their in-range scaled partial UNCLAMPED
     — no overflow can happen there, which is the whole point.

**The saturating left shift (S > 0), and why SHL alone won't do it.** `SHL` reports
NO overflow (V stays 0), so a V-flag clamp after SHL never fires. Detect overflow in
O(1) instructions with a bias-and-shift test — `acc<<S` overflows iff
`acc ∉ [−2^(15−S), 2^(15−S)−1]`, which `(acc + 2^(15−S)) >> (16−S) != 0` (logical)
decides — then pin to the rail of the ORIGINAL sign via `0x7FFF + signbit`
(one shared `0x7FFF` word yields both +0x7FFF and −0x8000):
```
    MOVE acc_save, R0
    ADD  acc_save, bias        ; bias = 2^(15-S)
    SHR  R0, #(16-S)           ; 0 ⟺ in range
    BR.NZ _sat
    SHL  acc_save, #S          ; in range -> result; emit; HALT
    {write}; {jump}; HALT
_sat:
    SHR  acc_save, #15         ; sign bit
    ADD  R0, satpos            ; 0x7FFF + bit
    {write}; {jump}
```
Exhaustively verified equal to `clamp(acc·2^S)` for all acc, S∈0..15.

**Build-engine gotcha (cost me real time):** do NOT use a `GOTO`/branch whose target
LABELS a `{write}`/`{jump}` placeholder — the build engine rewrites that jump with
the placeholder's OUTPUT routing (it becomes a stray output JUMP, not a local goto),
silently corrupting control flow (a pre-existing latent bug also present in
SquelchBlock's `GOTO update`). Instead, branch to a label on a REAL instruction, and
use the two-path / duplicated-`{write}` + terminal `HALT` structure above (the
in-range path's HALT is REQUIRED — a remote JUMP does NOT stop local execution, so
without it the in-range path falls into the sat block and double-emits).

**Budget / fold.** The headroom restore lives on ONE cell only (the single cell, or
the last multi-cell cell). For S=0 the per-cell density is UNCHANGED (TAPS_PER_CELL=5,
a 20-tap FIR = 4 cells). For S>0 the last cell caps its segment (≤3 taps) to fit the
restore, so a high-gain FIR may use one extra cell (a 40-tap gain-20 FIR = 9 cells vs
8 normalized); single-cell ceiling drops 6→4 when S>0.

**Verification.** The bit-exact reference models scaled wrapping accumulation + the
final saturating shift (NOT the float ideal). In-range (S=0) it equals GR float
clipped to Q15 (the GR drop-in claim — assert on NORMALIZED taps, Σ<1, so S=0
deterministically and no headroom precision loss). The overload/rail test uses a
HIGH-GAIN (S>0) filter at full scale so the shift fires and the DUT pins; the
wrap-mutation models the OLD no-headroom UNSCALED+wrap DUT and must FAIL the gate.

**Applies to:** FIR, IIR, complex mixer, correlators — any Q15 MAC chain. See
[[layout_rules]] for how the per-cell tap density + the S>0 last-cell cap set the fold.

---

## INV-14 — A serpentine fold co-locates I/O on one edge ONLY with an EVEN column count

**Symptom:** a multi-cell block's INPUT and OUTPUT cells keep landing on OPPOSITE
edges (e.g. input top-left, output bottom-left) — so the routing bus can't tap both
from one side, the routes are long, and the recurring "input and output on opposite
sides" placement complaint appears no matter how the orienter is tuned.

**Root cause — column-major snake parity.** A folded block lays its cells column by
column, snaking: cell 0 at the TOP of column 0, DOWN column 0, OVER, UP column 1,
OVER, DOWN column 2, … The INPUT is cell 0 (top of column 0). Where the OUTPUT (the
last datapath cell) lands depends on the parity of the COLUMN COUNT:

  * column 0 snakes DOWN → ends at the bottom,
  * column 1 snakes UP → ends at the top,
  * column 2 DOWN, column 3 UP, …

So after an **EVEN** number of columns the snake ends going UP → at the **TOP** of the
last column → the **SAME edge** as the input (I/O co-located, `io_colocated=True`).
After an **ODD** number of columns it ends going DOWN → at the **BOTTOM** → the
**OPPOSITE edge**. This is pure geometry, independent of D4 orientation: rotating an
odd-column fold still leaves I/O on opposite edges.

**Fix / guideline (not a hard DRC — a layout constraint). NO PADDING
(maintainer decision).** Choose the most COMPACT fold (tallest column `H ≤ FOLD_HEIGHT` ⇒ fewest
columns) and PREFER one whose `n` cells fill an **EVEN number of FULL columns** — then
the snake ends going UP at the top, the output co-locates with the input on the top
edge, and there is **no relay padding** in the egress (the output is just the last
datapath cell at the top of the last column). The FIR chooser scans
`H = FOLD_HEIGHT…1` and takes the first that divides `n` with an even quotient
(`fir_filter_block.py:_fold_geometry`). Examples that fold cleanly: n=2 → 2×1; n=4 →
2×2; n=8 → 2×4; n=20 cells… (per tap count).

**Width cap (≤8 across, INV-9).** The even-column preference must REJECT any fold
wider than the array allows: a cell count whose ONLY even-quotient divisor is
`H=1` (e.g. `n=26` → its even folds are just `26×1`) would otherwise pick a
degenerate full-width LINE that runs off the 10-wide array and cannot route (a
125-tap/26-cell dc_blocker hit exactly this: `_fold_geometry`
returned `(26,1)` and placement failed `unplaced_cell outside fabric`). The
chooser only accepts an even-column fold whose column count is `≤ 8`
(`MAX_CELLS_ACROSS`); otherwise it falls through to the compact fold (n=26 →
`7×4`). Co-location is still a preference, routability is not.

When `n` has **no** even-full-column fold (e.g. a prime-ish cell count like 3, or 13),
do **NOT pad to force it** — padding the last column with transit relays puts a relay
cell in the OUTPUT EGRESS path, and the auto-router starts its corridor one cell
*outside* the block's emit face, so the source-exit WRITE hop is computed from the
relay, not the output cell → the output WRITE lands one hop short and the block
produces NO output (verified: a padded 13-tap FIR built `res.ok` but emitted zero
samples). Instead take the most compact fold as-is and let the **router** connect the
output from wherever the last cell lands (a row off the input edge at worst). "Get
close, then let the router hook it up" — co-location is a preference, not a hard
requirement, and is NOT worth a fragile egress-relay mechanism.

A partial last column breaks the parity argument (an up-going partial column doesn't
reach the top row); we simply accept the off-by-a-row landing in that case rather than
pad.

**Applies to:** every folded multi-cell block (FIR, and any future block whose
`default_layout` serpentines). See `layout_rules.md` for the fold conventions this
refines.

---

## INV-15 — A Q15 coefficient with |c| > 1 is stored HALVED and applied TWICE

**Symptom:** a block whose math needs a coefficient outside Q15's [-1, +1) range
(an IIR feedback tap `a1 = -2cos(omega)`, |a1| up to ~2; any gain > 1; a loop
constant > 1) either overflows mid-chain OR — worse — was silently CLAMPED to ±1.0
and now computes a completely different, wrong function with no error.

**Root cause:** Q15 represents only [-1, +1). `float_to_q15(c)` for |c|>1 saturates
to ±0x7FFF/0x8000, so storing the coefficient directly LOSES it. Clamping it to fit
("min(1,max(-1,c))") is the trap — it builds the wrong filter/gain quietly.

**Fix — store HALVED, apply TWICE.** Store `c/2` (representable whenever |c|<2) and
apply its multiply-op TWICE: `MACQ Ra,c_half` twice == `+c*Ra`; `MSUQ Ra,c_half`
twice == `-c*Ra` (`MSUQ` is `R0 -= (Ra*Rb)>>15` — MAC opcode MODE=11; see
PROGRAMMING_GUIDE.md, MAC MODE table). Each `(Ra * c/2)>>15` product is in range, so there is NO intermediate
overflow and NO new ISA/guard bits. For |c|>2, cascade: store `c/4` and apply four
times, etc. (Distinct from the FIR's COEFFICIENT HEADROOM [[INV-13]], which scales
the WHOLE coefficient set down and restores once with a saturating shift; that's for
keeping an ACCUMULATOR in range. INV-15 is for representing a SINGLE out-of-range
coefficient. They compose.) A naturally-bounded output (a stable IIR's `y`) keeps
the accumulator in range with no extra clamp; an unbounded one still needs INV-13.

**Verify:** the block must be BIT-EXACT with a `process_reference_q15` that models
the exact halved-and-doubled op order; and a MANDATORY mutation that CLAMPS the
coefficient (the original bug) must FAIL the gate. The disassembler must decode the
MAC/MUL MODE sub-field [11:10] so `MACQ/MSU/MSUQ/MULQ/MULHI` show their real
mnemonic (a top-level-opcode-only table mislabels them all as "MAC"/"MUL").

**Applies to:** IIR biquads (the canonical case), high-gain blocks, any loop/filter
coefficient that can exceed unity in Q15.

---

## INV-16 — Coefficient parity vs a GR designer is a Q15-EXACT gate, not a float-bit gate

**Symptom:** a convenience/designer block (firdes Low/High/Band filters, any block
whose taps come from a GR algorithm) is held to "float taps BIT-EXACT to GNU
Radio" and false-fails by ~1 float32 ULP, OR an agent weakens the gate to a loose
relative tolerance to get green.

**Root cause — float bit-exactness is interpreter-dependent and not the thing that
matters.** Two sub-ULP sources put the last bit out of reach: (a) GR's C++ is
compiled with fused multiply-add (e.g. `firdes` `coswindow`), not portably
reproducible in Python; (b) the production runtime (the modem `.venv`) links a
DIFFERENT libm than the GR verification host, so `sin`/`cos`/`exp` differ in the
last bit. Neither changes what reaches the chip: the tap is QUANTIZED TO Q15 (15
fractional bits) before it is used, and a sub-ULP float difference (~1e-8) is ~5e-4
of one Q15 LSB (3.05e-5) — it never crosses a rounding boundary.

**Fix — gate on the Q15-quantized tap, which IS the hardware coefficient.** Assert
`float_to_q15(block_tap) == float_to_q15(gr_designer_tap)` BIT-EXACT for every tap
and every parameter/window combination — this is a strong, honest, hardware-
determining gate (the on-chip filter provably equals the GR filter). Keep a SECOND
float-level assertion as a DERIVED floor (e.g. `max|Δ| < 1e-6`, far below ½ Q15
LSB) to prove it is the same design up to floating-point rounding — but do NOT
require float bit-equality, and do NOT loosen the floor to a percentage. The
end-to-end DSP gate (DUT output vs the GR block fed the GR-designed taps, within
the inherited Q15 FIR floor) and the bit-exact `process_reference_q15` gate are
unchanged. (Verified: firdes taps are float bit-exact for Hamming/Hann/Rectangular/
Kaiser on the GR host but ~1 ULP off in the `.venv`; Q15-exact for ALL six windows
in both.)

**Applies to:** every block that reproduces a GNU Radio coefficient designer in
pure Python because GR is absent at runtime — the firdes filters ([[INV-13]]
headroom still governs the datapath), and any future windowed/designed-tap block.

---

## INV-17 — A COMPLEX-OUTPUT cell must budget for the FAN-OUT program form

**Symptom:** a complex block (a cell emitting TWO real rails `yi`/`yq` from one
output cell — the ComplexMixer's `mixer` cell, an NCO, any I/Q source) works when
its output feeds ONE downstream complex block, but when the two rails are wired to
TWO DIFFERENT downstream blocks (the SSB Weaver's `mixer.yi → LowPass_I`,
`mixer.yq → LowPass_Q`) the chip produces HALF the streams — one filter gets the
data (poor corr), the other gets **nothing** (0 output). Disassembling the output
cell shows both rail WRITEs collapsed to the SAME dest + a SINGLE JUMP.

**Root cause — two DELIVERY SHAPES, one output cell.** A complex output rail pair
has two legitimate on-chip forms:
  * **COMPLEX PACKET** (rails → the SAME downstream cell, e.g. mixer→Costas): emit
    `WRITE yi; WRITE yq; JUMP` — two operands into the target's R0/R1, ONE trigger
    so the target fires ONCE per sample with both rails fresh. This is the default
    and is correct; do NOT change it.
  * **FAN-OUT** (rails → TWO DIFFERENT downstream cells): each rail needs its OWN
    trigger — `WRITE yi; JUMP trig_i; WRITE yq; JUMP trig_q`, each WRITE+JUMP pair
    steered to its own broker — so each downstream block fires independently.
The build performs the FAN-OUT transform automatically (a general `_apply_brokers`
pass, keyed on the two rails resolving to DISTINCT broker cells). But the transform
adds an EXTRA JUMP to the output cell's program, so the cell needs one free word for
it. If the cell is at its full memory budget, the fan-out won't fit.

**The protocol for AUTHORS of complex-output blocks (do this, always):**
  1. Keep the template as the COMPLEX PACKET form (`WRITE yi; WRITE yq; JUMP`).
     The build re-sequences it to the fan-out form only when needed — you do NOT
     author two triggers.
  2. **BUDGET the output cell for the fan-out form.** Leave at least ONE free word
     in the output cell so the build can insert the second JUMP. A cell packed to
     32/32 words CANNOT fan out.
  3. **Add a BLOCK-VERIFICATION memory test** that asserts the output cell's program
     + the extra fan-out JUMP fits in 32 words. This catches an over-full complex
     output cell at BLOCK-VERIFY time — where it belongs — so a user NEVER hits an
     overflow at chip-build time (which would be an opaque, terrible failure). Build
     time may then SAFELY assume the room exists because verification guaranteed it.

**Fix / status:** the general fan-out transform lives in `engine/build.py`
`_apply_brokers` (proven: mixer→2×LowPass hits corr ~1.0; complex→complex packet
path is byte-identical to before — mixer→Costas still BER 0). The ComplexToFloat
"split" BLOCK is a dead end — a complex pair is ALREADY two words multiplexed on the
bus (I in R0, Q in R1); a downstream block that wants only the I rail just reads R0.
Steering is a build/routing job (which register each downstream WRITE targets), NOT
a physical block. (If GRC ergonomics ever need a visible "split" node, it must be a
LOGICAL-ONLY adapter that DISSOLVES at import — 0 cells on hardware — never a placed
block.)

**Applies to:** ComplexMixer, NCO, IQUpconvert, and EVERY block whose output cell
emits an I/Q rail pair — i.e. any block a future agent writes with a complex output.


## INV-18 — A complex FILTER stage is ONE block (fir_filter_ccf), not a split + 2 FIRs

**Rule:** to filter a complex I/Q stream on the fabric, use `ComplexFIRFilterBlock`
(or a firdes wrapper: `ComplexLowPassFilter` / `ComplexHighPassFilter` /
`ComplexBandPassFilter` / `ComplexBandRejectFilter`) — complex in, complex out, ONE
shared real tap set = GNU Radio `fir_filter_ccf`. Do NOT wire a complex source into
two real `LowPassFilter`s (one per rail) and recombine; that is the split-fan-out /
reconvergent-fan-in shape [[INV-17]] guards, and it wastes cells + can leave a filter
starved. The complex FIR keeps the whole chain as same-source complex PACKETS
(mixer→filter→upconvert), which is what let the SSB Weaver fit ONE 10x12 die.

**Datapath (for whoever extends it):** each cell carries TWO delay segments (`di{i}`
I-history + `dq{i}` Q-history) SHARING the coeff words `c{i}`, runs the MULQ/MACQ chain
twice, forwards BOTH partial sums + BOTH shifted-out samples to the next cell; last
cell emits the pair with ONE trigger. Coeffs start at R2 (`xi`=R0, `xq`=R1 are the
fixed landing regs). One shared `osave` temp forwards each rail's oldest sample.

**HARD CONSTRAINT:** a MULTI-cell complex FIR needs `Σ|h| ≤ 1` (`head_shift==0`) — the
last cell runs a saturating-restore emit PER RAIL and two of them overflow 32 words.
The block RAISES a clear ValueError instead of silently rescaling (matches GR magnitude
— RULE #0). A low-pass at `gain≤1` is Σ|h|≤1 by construction; band filters at gain=1.0
often exceed it, so pass a smaller gain (0.4–0.9) — correlation is gain-invariant.
Gated by placekyt/tests/test_complex_fir_budget.py + verification/tests/test_complex_fir.py
(GNU-Radio parity, 12 tests).

## INV-19 — A FEEDBACK block must survive SATURATED (pipelined) drive, not just inject-and-flush

**Rule:** any block with an internal FEEDBACK loop whose feedback is a *data-only*
write (read by "the next sample") MUST still produce the correct output when the input
is driven SATURATED — the whole burst enqueued back-to-back with NO quiescence between
samples (`queue_words_physical`, the real GNU-Radio / hardware streaming condition). The
per-sample harness (`run_block_dut` — inject one, run to quiescence, inject next) HIDES
feedback hazards: each sample fully settles before the next arrives, so a loop that has
NO real backpressure still "works". Under saturation it collapses.

**The failure (the Costas cautionary tale, round 2):** the Costas carrier loop wrote
`dphase` back to the `phase` NCO as pure data, assuming phase reads it "next sample".
Pipelined, `phase` raced ahead OPEN-LOOP on the streamed samples while `pd_pi` lagged;
the loop decoupled and died after ~3 symbols (per-sample: BER 0; pipelined: 1 bit out).
Gardner's timing loop is the same shape. This is invisible to `run_block_dut`.

**The fix — arbiter LOCK, the SAME idiom `iq_upconvert` already ships (don't invent):**
1. The loop's LANDING cell (the NCO/accumulator) LOCKs its arbiter to the FEEDBACK face
   after it launches a sample forward: `MOVE [LOCK_FACE], R{data:face}; MOVE [LOCK],
   R{one}`. Gate-all-but-LOCK_FACE holds the NEXT input sample (single-outstanding) until
   the loop closes. `face` is an `is_face=True` DataWord so it transforms with orientation.
2. The LAST datapath cell (the PI filter) CLEARS that lock inline with a backward
   `WRITE.CFG @N, 4` (R0=0 → the landing cell's CONFIG[4]=LOCK). The build's
   `_apply_internal_feedback` patches `@N` to the SAME resolved feedback-corridor hop it
   patches the data-feedback WRITE to — a fixed authored hop deadlocks a re-placed layout.
3. First sample runs unlocked (cold dphase=0 = GR's cold start); the lock engages after.

**WHY A WRITE.CFG SURVIVES IN ONE CELL BUT VANISHES IN ANOTHER (the trap that cost a
day):** the lock-clear `WRITE.CFG` must live INLINE in the PI-filter cell that ALSO emits
the data feedback (like the standalone Costas `pd_pi`, and `iq_upconvert`'s upmix). Do
NOT hive it into a dedicated relay cell on the feedback corridor: that makes the relay a
feedback SOURCE, and the build's exit-hop defaulting (`_set_cell_hop1`) rewrites feedback-
source cells and CLOBBERS the config-write. `iq_upconvert`'s WRITE.CFG survives because
its cell has NO backward internal_connection; the standalone Costas `pd_pi`'s survives
because it is NOT the block's exit cell and holds the feedback WRITE alongside it. When a
config-write disappears in the built cell, the cause is ALWAYS the cell's structural role
(exit-cell / feedback-source), never the assembler — find the WORKING cell of the same
kind and MATCH ITS STRUCTURE (inline vs relay, exit vs mid, feedback-source or not).

**Register squeeze:** the lock-clear costs ~1 state + 2 instrs. Reclaim without changing
behaviour: emit an output while its operand is still in its INPUT reg (drop a saved-copy
state + its reload); merge a `0`-valued face DataWord into an existing `zero`
(SOUTH==0); reuse the reg that already holds the feedback value as the post-CFG reload.
The CoherentRX `_pdpi_with_yitap` did all three to fit the WRITE.CFG at 30/31 words.

**Gate:** `run_block_dut_pipelined` (saturated queue_words drive) MUST equal the
per-sample output (the GR-verified reference). `verification/tests/test_pipeline_saturation.py`
enforces this for every catalog block (feed-forward + stateful blocks were already safe;
only the Costas-class carrier loop had the hazard). Standalone Costas + the full
CoherentRX both recover BER 0 pipelined (and FASTER — no inter-sample flush).

**HARNESS SAFETY (do NOT remove):** `run_block_dut_pipelined` caps `chip.run()` at a
generous but FINITE `max_events` (never `None`). A block that deadlocks/livelocks under
saturation leaves the event queue permanently non-empty; an UNCAPPED `run()` then spins
at 100% CPU forever (this melted the machine once). The harness now treats a non-`completed`
run as a livelock FAILURE (clean `ok=False`), not a hang. Any new saturated-drive harness
MUST do the same — bound the run and check `res["completed"]`.

**GARDNER — DONE 2026-07-14 (commit 0e18759), the predicted INV-19 case.** GardnerTimingRecovery
(the 2-sps timing loop: resampler→ted→loop_filter→period_relay, feedback period_relay.pout→
resampler.inst_next) drifted under saturation exactly as predicted (the resampler strobes again
before the PI filter's corrected `inst_next` feeds back → stale). FIX = the serialize-LOCK
(pipeline_lock=True default): the resampler LOCKs its arbiter on a STROBE ONLY (lock tail placed
after `{jump:val}`, so the no-strobe `done` path never locks and non-strobe samples keep flowing
to advance phase); period_relay clears it with a backward `WRITE.CFG @1,4` after `{write:pout}`
(the existing pout→inst_next feedback edge co-patches the WRITE.CFG hop). PROVEN: saturated
recovered BITS == per-sample bits, 0-diff, fracs 0.3/0.5/0.7. NOTE: word
values differ by sub-LSB interpolation amounts that never flip the sign — for a rate/timing block
the SATURATION GATE MUST assert BIT-equality (sign), not word-equality. Also: use a REAL RRC-shaped
2-sps BPSK stimulus — a piecewise-linear synthetic can't be locked in EITHER mode (false "divergence").

**TWO REGISTER-RECLAIM TRICKS (for fitting a serialize-LOCK into a budget-tight landing cell):**
1. **LOCK_FACE need not be written if the feedback face == the CONFIG reset default = SOUTH (00).**
   The cell CONFIG resets LOCK_FACE to SOUTH. If the feedback corridor arrives on the
   cell's SOUTH face (place the feedback cell SOUTH, emitting NORTH), the lock tail is just
   `MOVE R0,<nonzero>; MOVE [LOCK],R0` (2 instrs, not 4 — skip the LOCK_FACE writes). CAVEAT: only
   valid for the UN-rotated layout; if auto-orient rotates the block, restore the is_face lock_face
   DataWord + the two LOCK_FACE writes (needs 2 more slots freed).
2. **Merge two DataWords that hold the IDENTICAL value into one** (Gardner: `inc` and `one_q14`
   were both 1<<14 → one word, freeing a slot). ~~Also: LOCK engages with ANY nonzero, so reuse any
   existing nonzero data word for the `MOVE [LOCK],Rn` — no dedicated `one` word needed.~~
   **CORRECTED (2026-08-23, measured on the ChirpGenerator):** the LOCK CONFIG enable reads
   **BIT 0** of the written value — `MOVE [LOCK], R{rate=0x1000}` left the cell UNLOCKED
   (saturated symbols barged into a mid-flight iteration; A/B: value 0x4000 fails, value 1
   locks). Reuse an existing data word for the lock-set ONLY if its **bit 0 is set**;
   otherwise spend the dedicated `one` word. (Matches PROGRAMMING_GUIDE §3's "write 1".)

---

## INV-20 — A FEED-FORWARD block with a RECONVERGENT fan-in DEADLOCKS under saturation unless samples are serialized

**Rule:** a multi-cell block where one landing cell FANS OUT to ≥2 paths of *different
lengths* that later RECONVERGE at a fan-in cell (e.g. an NCO column + a signal-relay column
both feeding a final `mixer`) is NOT automatically safe under saturated drive, EVEN WITH NO
feedback loop. With one sample it settles and emits; with two samples back-to-back it
DEADLOCKS. This is distinct from INV-19 (that is a *data-feedback* hazard; this is a pure
*fan-in ordering* hazard) and is invisible to `run_block_dut` (inject-and-flush).

**The evidence (ComplexMixer, proven at the sim event level):** the 11-cell `multiply_cc`
mixer — `phase` fans out to two NCO columns (sin/cos) + a `relay` carrying xi/xq, all
reconverging at `mixer` (4 inputs: cosv, sinv, xi, xq, arriving via paths of length 4, 4,
and 2). Bounded probe:
`N=1 → completed=True (QueueEmpty), 2 output words` — perfect.
`N=2 → completed=False, stop_reason=Deadlock, 0 output words` — locks the instant a second
sample enters. The sim reports `Deadlock` EXPLICITLY (not EventLimit/flood). Cause: a
circular wait — sample-2's fast-path operands occupy the fan-in cell's input registers
before sample-1's slow-path operand arrives, and neither can proceed.

**The fix (same LOCK idiom as INV-19, applied to a NON-feedback block):** serialize samples
through the block so no two are co-resident in the reconvergent fan-in. The landing/fan-out
cell (`phase`) LOCKs its input arbiter after launching a sample, and the fan-in/exit cell
(`mixer`) CLEARS the lock (backward `WRITE.CFG @N, 4`) once it has consumed all four
operands and emitted. Result: the port FIFO still pipelines input, but the block interior
processes one sample at a time — no fan-in race. This is the SAME mechanism as the RX loop
lock; the only difference is the lock-clear rides the block's EXIT cell (which here has no
backward internal_connection, so `_set_cell_hop1` does not clobber it — cf. the INV-19 trap).

**Consequence for the catalog:** every multi-column complex block with a reconvergent
fan-in (ComplexMixer, the ComplexFIR family's I/Q recombine, IQUpconvert already carries a
lock) needs this serialization to pass `run_block_dut_pipelined`. Check the block's
`internal_connections`: if two paths of unequal length reconverge on one cell, it needs the
serialize-LOCK.

**ComplexMixer resolution (the reference implementation) + the durable traps found
on the way:**
- The unlock is folded INLINE into the mixer/exit cell with a DUAL-FACE flip
  exactly like iq_upconvert's upmix: emit yi/yq on `face_tap` (the routed
  output face), flip to a DISTINCTLY-NAMED `unlock_face` (name it NOT
  `face_internal` so the orientation pass — not the tap pass — rotates it), do
  `WRITE.CFG @2,4` down a 1-cell `transit_unlock` corridor to the landing
  cell. The config-only backward edge is declared as
  `("mixer","unlock","phase","xi")` and resolved by
  `_apply_internal_feedback`'s `_src_port=="unlock"` branch (it patches the
  WRITE.CFG by config-bit alone, never a data reg — so no output-register
  collision).
- **A `__terminate__` trig is not a no-op — it emits a JUMP word on the
  current FACE.** A dual-FACE cell MUST restore its output face
  (`MOVE [FACE], face_tap`) after the WRITE.CFG, or the trailing trig fires
  into the unlock corridor and forms a self-sustaining datapath loop that
  deadlocks once the real samples drain.
- **Register-reclaim facts:** `MULQ A,B` writes R0 and does NOT clobber its
  operand STATE registers — a scratch copy of a state var is unnecessary. But
  an INPUT register is NOT stably re-readable across multiple ops (the async
  operand latch is consumed): operating on input regs directly corrupted the
  Q rail (corr ~0.7) while the pipeline probe stayed green — **after ANY
  register-reclaim on a compute cell, re-run the block's bit-exact gate, not
  just the pipeline/deadlock probe.**
- **A fixed-authored `WRITE.CFG @N` in the exit cell is clobbered by
  `_patch_last_write_handoff`** (which patches the highest-address WRITE)
  unless the block's real output WRITE is strictly LAST — emit yi/yq last,
  WRITE.CFG before them.
- The landing cell's default_layout FACE is its DATAPATH emission and is
  INDEPENDENT of LOCK_FACE (an arbiter-gate CONFIG word) — pointing the face
  at the unlock corridor sends the fan-out into the corridor and stalls the
  block.
- Sim `get_trace()` (per-cell events) + bounded `run(max_events=)` are the
  diagnostic tools — not blind face/hop guesses (a whole class of "@1 vs @2"
  dead-ends was a probe decode bug; the hop field is bits[9:5]).
- PROVEN: `run_block_dut_pipelined` (saturated) == per-sample GR reference
  BIT-EXACT, both placements (`test_pipeline_saturation.py` COMPLEX_2IN2OUT).
  DEFAULT stays `pipeline_lock=False` (opt-in — flipping the default
  regressed the GRC importer's net resolution + the compact SSB footprint).

**NCO + FrequencyModulator RESOLVED (2026-07-21) — the same INV-20 fan-in, now saturation-safe
(opt-in `pipeline_lock=True`).** They have the identical `phase → sin-arm(4) + cos-arm(4) → emit`
reconvergent fan-in as ComplexMixer, but NO `relay` and a 4-operand emit. Porting the ComplexMixer
lock needed THREE NCO-specific moves (each was a distinct failure mode found via `get_trace`):
1. **Collapse emit to 2 operands.** The 4-operand emit (cos_mag, sin_mag, cos_neg, sin_neg) STARVES
   under the lock — only 2 of the 4 operands co-arrive before emit fires. Move the quadrant SIGN
   INLINE into the interp cells (emit a single SIGNED `mag`, drop the `negf` output) exactly like
   ComplexMixer's interp; emit then takes just (cos_mag, sin_mag). This ALSO frees the register room
   the lock's 2 face DataWords + 4 instrs need. **Update `process_reference` to sign-BEFORE-amp**
   (`(neg?-mag:mag)*amp>>15`) to match — it differs from amp-before-sign by ≤1 LSB on negatives, a
   rounding-order choice; the bit-exact gate then agrees. (Offset ADD writes R0 already — do NOT add
   a trailing `MOVE R0,cv` after it, that drops the offset; a whole day's-worth of the same class of
   R0-vs-state confusion.)
2. **The `relay` must FORWARD DATA, not be trigger-only.** A pure `{jump:trig}` relay does NOT
   reliably re-fire on the substrate (exec_tick=1 for many triggers) → the cos arm never fires →
   emit starves. Route the cos-arm phase THROUGH it: `phase→relay→cos_fold` (relay holds `ph_cos`
   and re-forwards it), mirroring ComplexMixer's relay forwarding xi/xq. This is what makes cos_fold
   fire and serializes the arms.
3. **`relay` MUST be inserted in DICT ORDER between the arms; `emit` MUST stay the LAST cell in
   `build_cell_programs()`.** Appending relay AFTER emit made relay the exit cell and the build WIPED
   emit's whole program to a bare JUMP (only `output_cell_id()="emit"` + last-dict-position keep the
   exit/complex-egress handoff on emit). Layout: 6-cell col0 ending in relay(0,5)→cos_fold(1,5)
   corner, emit(1,1) egress EAST, transit_unlock(1,0) NORTH of emit — EXACTLY ComplexMixer's.
- PROVEN in ISOLATION: locked FM 40 in → 40 unit-circle pairs (|IQ|=1.0), 60/60 + 16/16 BIT-EXACT
  vs `process_reference_q15` under saturated `queue_words` drive. Unlocked stays bit-exact (70/70).
  Gate: NCO in COMPLEX_2IN2OUT, FM in `test_fm_saturation_safe` (real-in/complex-out, driven direct).
- RESOLVED (2026-08-16) — the former KNOWN LIMIT ("unlock corridor not placement-invariant
  under auto-P&R") is closed. The corridor itself was ALWAYS rigid: the block's cells (incl.
  `transit_unlock`) transform as a unit and `_apply_internal_feedback` re-derives the unlock
  hop from the placed geometry; the old adjacency-loss sightings were the re-fold SET-dedup
  self-overlap bugs (fixed 2026-07-22). The REAL residual was placement-INDEPENDENT: a locked
  block feeding a DOWNSTREAM BLOCK delivered SHIFTED rails ((yq, 0) at the consumer) because
  three build patchers counted/clobbered the emit cell's trailing lock-clear `WRITE.CFG` as a
  data WRITE (`_patch_complex_packet_last_handoff` tail selection; the single-net
  complex+carries-handoffs branch; the ABUTTED-pair last-write LAST-WINS that auto_pnr's
  compact pack triggers). All three now skip config WRITEs / patch the last N DATA writes
  once (`_patch_complex_abutment_tail_handoff`). GATE:
  `verification/tests/test_locked_chain_autopnr.py` — locked FM chain saturated bit-exact
  across ≥3 sampled placements + a full auto_pnr pack, with port-egress AND block-consumer
  shapes (INV-4 proven: pre-fix every consumer case failed), plus a locked-NCO consumer case.
  The fsk4 modem's hand-placed `.kyt` remains the shipped form for DENSITY reasons only (a
  fresh import of the full modem does not route).

---

## INV-21 — SATURATED "pipelined" drive means the SLOWEST BLOCK is saturated, NOT the input port; a raw-word input stream cannot be demuxed by decoding word bits

Two distinct facts about the pipelined/full-speed path (sim_bridge `process_batch`
`pipelined:true` → `queue_words_physical` whole burst + one continuous `run()`),
both surfaced getting the coherent BPSK RX full-speed GRC demo live.

**A. The coherent RX IS pipelined and the array is ~95% BUSY — it is THROUGHPUT-bound
by DSP instruction count, ~675 instr per recovered symbol.** (Two earlier readings —
"one slow block" and "mostly idle" — came from measuring ONE cell's utilisation and
mistaking it for the array; measure a whole STEADY-STATE OUTPUT INTERVAL.) Facts for the
coherent BPSK RX .kyt driven saturated (throughput_bench.py + timeline/interval probes):
- Throughput ~0.15 MSym/s out (2 sps → ~0.33 MSa/s in), ~10 µs fill latency.
- IT IS PIPELINED. The front ingests fast (JUMP-injections 405–810 ns apart, MF head
  fires every ~358 ns) and **13 samples are in flight before the first output emerges**
  — inputs do NOT wait for outputs. Steady state is then gated by the OUTPUT drain rate
  (one symbol every ~6540 ns); the port back-pressures only once the pipe is ~13 deep.
  (The "3010 ns mean input gap" was a misleading blend of the fast front rate and the
  back-pressured stall — use the OUTPUT interval, not the mean input gap.)
- THE ARRAY IS ~95% BUSY, not idle. In one 6561 ns output interval: **675 exec_ticks**
  across ~24 cells, only ~5% of the interval with NOTHING executing. A single cell is
  ~12% utilised, but 24 cells × ~12% ≈ the array running near-full.
- ROOT CAUSE = raw DSP cost. The receiver executes **~675 instructions per recovered
  symbol** (RRC matched filter ~16 complex taps + Costas complex derotate + Costas PI +
  Gardner interpolate + Gardner TED + Gardner PI + slice). At 40 MIPS (27.43 ns/instr),
  675 × 27 ≈ 6540 ns/symbol ⇒ 0.15 MSym/s. The number is HONEST DSP throughput, not
  waste and not a slow block. 40 MIPS ÷ 675 instr/sym = 0.15 MSym/s, full stop.
- ⇒ To speed ONE chain: cut instructions/symbol (leaner blocks — the MF taps and the two
  PI loops dominate) — bounded, since it's real arithmetic. The BIG lever is PARALLELISM:
  ~24 cells/chain on a 400-cell array ⇒ ~16 concurrent independent RX ⇒ aggregate
  ~2.4 MSym/s per chip. Throughput-per-CHIP (parallel chains) is the architecture's
  headline, NOT throughput-per-chain. The single-outstanding port limits pipeline DEPTH
  (~13) but the array is already compute-saturated, so deeper overlap wouldn't help much
  here — the work itself is the limit.
HARDWARE: the FX3/FPGA FIFO provides real backpressure so on silicon the streaming
source paces natively — the sim `queue_words_physical` path EMULATES that (no sim FIFO).
Report chip-time
numbers (`simulation_time`, per-word `time_ns`), NOT host wall-clock.

**B. A raw-word input trace must be demuxed by POSITION, never by decoding word bits.**
The pipelined path injects a pre-encoded stream — per complex sample:
`WRITE(hop,d0) → xi → WRITE(hop,d1) → xq → JUMP`. simKYT records a `port_injection`
per word and recovers `(hop,dest)` by DECODING each word's bits. That's fine for control
words, but a bare Q15 DATA payload in `[0x6000,0x7FFF]` is BIT-IDENTICAL to a WRITE/JUMP
opcode, so ~1 in 8 payloads decodes as a spurious `WRITE(hop=N)` → a phantom "hop N"
input trace (the waveform panel showed a pile of flat overlaid traces). simKYT can't
disambiguate a payload from bits, and neither can the panel. **FIX (trace_model
`port_streams_by_tag`): a per-port WRITE→DATA state machine.** The stream STRICTLY
alternates WRITE→DATA (an addressing WRITE is ALWAYS followed by exactly one payload —
it can never be otherwise), so when expecting DATA the next event IS the payload
UNCONDITIONALLY, whatever its bits decode to; JUMP terminates the packet. Applied only
to ports with a `target_hop==0` injection (the raw-word path); the per-sample
`inject_data_physical` path (one addressed value-event per operand, no hop-0 payloads)
is untouched. This artifact ONLY appears on the raw-word/saturated path — every prior
demo used per-sample injection (one clean addressed event per sample). Regression:
`test_trace_model.py::TestRawWordInputCoalescing` (opcode-colliding payloads).

**A-SPEC (the DSP-engineer number).** The honest single-chain spec, measured by
CRITICAL-PATH depth (not cell count): one output symbol takes ~243 SERIAL instruction-
times (243 × 27.43 ns ≈ 6.6 µs = the observed 6540 ns/symbol). Instantaneous parallelism
is only **~2.78×** (mean 2.62 cells firing per 27 ns bin, NOT 24) — so the ~675 instr/
symbol are mostly SERIAL on the two PI feedback loops + the sequential MF accumulate.
⇒ **sustained complex input ≈ 0.33 MSa/s/chain** at this node (~0.165 MBaud, ~0.33 MHz
occupied BW). Node shrink scales ~linearly: ~3.3 MSa/s at 10×, ~6.6 MSa/s at 20×.
COMMERCIAL REALITY (the maintainer's framing): as a SINGLE serial DSP engine this is SLOW — a
commodity FPGA/RFSoC/C66x demod core does tens–hundreds of MSa/s complex, i.e. ~100–
1000× faster per chain; even at 20× shrink one chain trails badly. Parallelism does NOT
rescue a chain too slow for the target signal ("a million too-slow chains still
can't demod a fast signal"). So Kyttar is viable ONLY for NARROWBAND many-channel work
(the skimmer: hundreds of kBaud–low-MBaud channels — PSK31/RTTY/AFSK/voice/low-rate PSK-
FSK), NOT wideband single-channel. THE LEVER for one-chain speed is SHORTENING THE
CRITICAL PATH (243 serial instr/symbol), not adding chains: cut the serial spine (the 2
PI loops + MF accumulate) and spread MF taps across more cells so real parallelism rises
from 2.78× toward the ~10× the tap count allows. throughput_bench.py measures it.


## INV-22 — A block is NOT done until its GRC binding exposes EVERY param and resolves in GRC

A block whose verification test is green but has no (or an incomplete) GRC binding is
**not usable** and therefore **not done**. This is the product boundary: the value is a
1:1 drop-in for a GRC block, and a block you cannot place in a flowgraph — or one whose
params you cannot set from GRC — delivers none of that value. Two failures this rule
exists to prevent, both hit while shipping the QPSK receiver blocks:

**A. Missing binding ⇒ "Missing Block".** A block with no `gr-kyttar/grc/<id>.block.yml`
renders in GRC as a red **Missing Block: kyttar_<id>** and cannot be instantiated —
even after `install.sh`, because there is nothing to install. (QPSKSlicerBlock had a
green bit-exact test and NO `.block.yml`.) The GRC binding is TWO files:
`gr-kyttar/grc/<id>.block.yml` (the block definition) + `gr-kyttar/python/kyttar/<name>.py`
(the shim the YAML `make:` template calls, when the block needs one). `install.sh` copies
both — a stale install shadows repo edits, so re-run it and re-open GRC to confirm.

**B. Hidden params / dropped ports.** The `.block.yml` must expose **every** parameter
the block class accepts, with the SAME name/default/units as the GR counterpart (INV-0),
AND list every input/output the block presents **for those params** — including
param-DEPENDENT ports. A param on the class but absent from the YAML is invisible to the
user; a param-dependent port absent from the YAML collapses that rail (the importer
resolves ports WITH params per INV-6/11 — e.g. the Costas `order=4` yi/yq complex pair,
a decimated output, a second input rail). Concretely, when a new param changes the
block's port set, the binding must change too: add the param under `parameters:`, add/adjust
the `inputs:`/`outputs:`, and if the importer needs it, an `_INSTANCE_PARAMS`/`_TYPE_OVERRIDES`
entry in `placekyt/engine/grc_import.py` so `catalog.port_map(btype, params)` sees them.

**The check:** run `gr-kyttar/install.sh`, open the block in GRC, and confirm — no
"Missing Block", every param settable, ports match the built block. Only then is the
Definition-of-done GRC-binding box (§4 of AGENTS.md) satisfied. See lessons_log entries
for the QPSK receiver blocks (Costas `order`, QPSKSlicer, MF `decimation`).

**ENFORCED (2026-08-08):** this was a rule with NO automated gate, so it drifted — an
audit found 18 done blocks with no binding at all + ~18 with param mismatches (RRC still
exposed legacy `span`/`sps` not the class's GR-verbatim `sampling_freq`/`symbol_rate`;
`FSK4SyncTimingRecovery` missing `threshold`; filters missing `decimation`/
`interpolation`). `verification/tests/test_grc_binding_complete.py` now HARD-FAILS for any
done block whose binding doesn't resolve (via placeKYT's own `_grc_id_to_type`) or doesn't
expose every class param. A param the block INTENTIONALLY omits (a documented HW-deviation
that raises) must be declared in the class attribute `GRC_UNSUPPORTED_PARAMS = (...)` — the
ONLY legitimate way to omit a param; `*_range` GUI-slider hints are auto-excluded. "Green
DSP test" is not done; "placeable in GRC with every param" is.


## INV-23 — A block must be ORIENTATION-INVARIANT; its internal cells are FIRST-CLASS

A block placed on the array is a **rigid unit**. Rotating or mirroring it (any of the 8
D4 orientations — 4 rotations × 2 mirrors) may change *where it sits* and *which way its
ports face*, but must **NEVER change what it computes**. A block whose on-chip output
changes (or vanishes) under some orientation is BROKEN — its per-cell faces, internal
handoffs, or feedback corridors do not transform with the block. The user rotates blocks
at will; the toolchain must make them orientation-invariant, NOT forbid rotation.

Two coupled requirements:

**A. Internal cells are FIRST-CLASS block cells.** A block's internal feedback/relay
cells (authored as `transit_*` ids in `default_layout`) are PART OF THE BLOCK: they
carry the block's identity/color, count in the block's footprint/bbox, transform
rigidly with the block, and follow the SAME rules as any other cell — NOT disguised as
light-blue "routing cells" split into a separate list. (Model: they live in
`placement.cells` as `PlacedCell`s with a `transit_*` id; `placement.transit_cells` is a
filtering property; `is_transit_cell()` is the single tag check. The `transit_*` prefix
is retained ONLY so the build never picks an internal cell as an I/O port and stamps the
universal routing program into it — behavior, not second-class status.)

**B. Identical output in all 8 D4 orientations.** The DATAPATH is invariant by
construction when the transforms are complete: `Placement.transform` D4-maps every cell's
position + face; `_apply_orientation_face_words` D4-maps in-program `is_face` FACE
constants; `_apply_internal_feedback` TRACES the feedback corridor (never assumes a
direction). The bugs that break invariance are NOT in the block or those transforms —
they are in the build/router HANDOFF selection keying on orientation-dependent geometry:
a complex 2-rail output that ABUTS its consumer at identity but routes via a BROKER after
rotation must still steer its rails to distinct regs (the brokered single-net branch must
carry the same 2-rail guard the abutted branch has — `output_registers > 1`, not bare
write-count, which over-broadly caught single-rail IQUpconvert).

**The gate (mandatory, every block, at 100%):** `verification/tests/test_orientation_
invariance.py` drives each block at all 8 D4 orientations and asserts the on-chip output
EQUALS the identity output (via the `orient=` param on `run_block_dut`/`_complex`/`_rate`
+ `check_orientation_invariance`). As of this cycle the gate is **fully green — 0 xfailed**
(every catalog block D4-invariant, verified stable across repeated runs). The
anti-orientation cases that were previously xfailed as a "harness/router residual" were
NOT invariance failures of the blocks — they were four concrete handoff/harness bugs, now
fixed (see below). Do NOT re-introduce an xfail to hide an orientation failure; a zero /
mismatched output under some orientation is a real bug in one of these handoff sites.

**The four anti-orientation failure modes (all FIXED — the checklist to trace a new one):**

1. **Internal-face restore keyed on integer cell ids only.** `_reassert_internal_forward_
   faces` (build.py) restored a block's authored internal-forward face only for cells whose
   internal-handoff source id is an `int`. Blocks that NAME their cells (strings like
   `phase`/`sin_fold`/`emit` — ComplexMixer, ComplexRRC, NCO) got a NO-OP, so the input cell
   kept the incoming ROUTE face instead of its authored face and the internal wavefront
   died. Fix: resolve string ids against each cell's `cell_id`. Symptom: zero output at the
   180°-family orientations for a named-cell block.

2. **Port complex fan-in double-relayed.** `broker_plan` (bus_router.py) expanded EACH rail
   of a 2-rail port fan-in into the full xi+xq operand packet and coalesced them → 4 relays
   (2 stale zeros) that clobbered the input. Fix: emit the operand group ONCE per
   `(broker, target-input cell)`.

3. **Router wove egress / a fan-in THROUGH the block body.** CP-SAT drew a block's output
   egress corridor across the block's OWN input cell (one cell, one fwd_face → the egress
   word is swallowed), or split a complex fan-in's two rails onto divergent corridors. Fix:
   read-only report validators in `controller._run_router` (`_routes_cross_block_body`,
   `_port_complex_fanin_split`) escalate ONLY an invalid report to the node-disjoint maze
   router (block cells = hard obstacles). Clean designs never trigger it.

4. **Harness manhattan hop on a snaked corridor.** See [[invariants]] INV-1 REFINEMENT —
   the DUT harness stopped the injected word short when the router snaked the corridor. A
   HARNESS bug that looks exactly like a block-invariance failure; the live modem path was
   never affected. This was the last "NCO residual."

**Related build machinery (read before touching):** `_apply_port_diverts` (build.py) — the
SHARED input-port fan-out primitive (INV-24), also the place a single-rail anti-orientation
divert would live if a future block needs the port cell to relay toward a rotated input
cell it cannot reach on the port's static face.

**Applies to:** every block; especially multi-cell feedback/complex blocks (Costas,
Gardner, complex MF, IQUpconvert, ComplexMixer, NCO). See [[layout_rules]] and the
lessons_log orientation entries.

---

## INV-24 — A SHARED input port that fans out to 2+ blocks must FORK at a broker cell, never diverge AT the port cell

**Symptom:** a full-duplex modem (TX + RX on one array, sharing ONE `x16_in` port,
multiplexed by `stream_id` / hop tags — the [[layout_rules]] duplex pattern) recovers ONE
stream but the OTHER produces **zero output**. (The QPSK modem: RX BER 0, TX passband
present, but the mapper never fires — or vice-versa depending on which way the port faces.)

**Root cause:** the chip input port cell has exactly ONE `fwd_face` (§1.3). When two
input nets leave the port cell in DIFFERENT directions (RX net leaves EAST to the matched
filter, TX net leaves SOUTH to the mapper), the port can forward only ONE of them; the
other is injected and immediately mis-forwarded → lost. Two private corridors that share
ONLY the port cell (diverge AT it) CANNOT both work. `_resolve_input_landings`' divert
scan SKIPS the port cell (index 0), so it silently mis-declares the losing net as "rides
straight" and the host injects it at a hop that goes nowhere.

**The rule:** all nets off one input port must leave the port cell in ONE shared direction
(a common bus prefix) and FORK at a shared cell BEYOND the port — where the fork cell
BROKERS one stream off (delivers it toward its block) while FORWARDING the other(s) onward
(HOP<31 transit). One shared fork/broker cell that splits to two block inputs; never two
diverging corridors. (Ground truth: the working hand-built modem's net6 brokered at (0,1),
net8 transited (0,1); the broken variant differed by exactly one cell — net6 leaving the
port EAST into a private corridor.)

**The build primitive:** `_apply_port_diverts` (build.py) promotes the port cell to a
broker for a diverting net: it lands the host burst AT the port cell (HOP_CNT==31), a turn
entry flips the face toward the net's first waypoint and relays the operand(s) one hop to
the net's DOWNSTREAM broker (which finishes delivery into the block), then RESTORES the
port's `fwd_face` so the OTHER (transiting) stream still forwards on the bus direction.
Chaining two `@1` brokers spans an intermediate routing cell, so a broker can deliver to a
target that is NOT directly adjacent (the corner case the original single-hop broker never
handled). The router side: `route_all_bus`'s `portfork` ordering + the shared-fork logic
in `_route_chip_bus` (a DIFFERENT-block port fan-out shares the bus prefix; a same-block
I/Q fan-in keeps its own broker). Regression: `verification/tests/test_router_port_fanout.py`.

**PLACEMENT PRECONDITION (2026-08-11):** the fork geometry is only constructible
when the port's exit cell is FREE — a fan-out port with a fed block's INPUT CELL
directly abutting the port cell is UNROUTABLE-SOUND (the single fwd_face must
point INTO the abutting block for one arm; the sibling's corridor cannot transit
a block cell), and the maze escalation then ships a silently-wrong two-direction
port (tremolo's 200-zeros). The CP-SAT placer hard-forbids it (fed input cells at
manhattan ≥2 from a fan-out port) and auto_pnr's acceptance gate
(`_port_fanout_abuts_port`) rejects any layout that violates it.

**Applies to:** any full-duplex / shared-input-port design (the BPSK modem, the QPSK
modem, the upcoming 4FSK modem) — a TX and an RX chain multiplexed on one `x16_in`. Prove
it end-to-end on the HOSTED chip (load `.kyt` → build → `stream_targets` → `SimServer` →
drive both `stream_id`s), NOT with a synthetic single-block harness — the shared-port fork
only exercises when both streams are present. See [[layout_rules]] duplex pattern.

---

## INV-25 — A `poc` block is NOT verified; treat it as unproven and expect latent bugs

**Symptom:** a block used in a shipped, BER-0 example (so it "obviously works") turns out
to disagree with its GNU Radio counterpart the moment it goes through per-block
equivalence verification — on inputs the example never happened to exercise.

**Root cause:** the manifest distinguishes three states, and only ONE means "trustworthy":
`done` = verified BER-0 vs GNU Radio; `planned` + **`poc: true`** = **code exists but was
never verified**; `planned` + no `poc` = no code yet. A PoC was written to make ONE
design work and was never held against GNU Radio across its full input range. It can carry
a real bug that hides because the one place it's used stays in range.

**Ground truth:** `ComplexGainBlock` was a `poc` load-bearing in the shipped `qam16_modem`
(BER 0). Verification found it **wrapped mod 2^16 instead of saturating** on `gain>1`
overload — max error 27,525 LSB on the Q rail — undetected only because the 16-QAM
constellation happens to stay in range at gain 2.4. Both its own reference and GR
`multiply_const_cc` saturate. Fixed via the INV-13 coefficient-headroom idiom
(store `gain/4` in Q15, MULQ each rail, restore with a **saturating** `<<2`).

**The rule:** when the manifest entry is `poc: true`, "building" it means *finalize +
VERIFY the existing code against GNU Radio across the full parameter range* — NOT trust
it because an example uses it. Sweep the whole valid parameter range (not just the value
the example uses), include full-scale/overload edges, and expect to find and fix real
bugs. Do not mark it `done` on the strength of an example; only the gate (INV-4) makes it
done. "It's used in a working modem" is not verification.

**Applies to:** every `poc: true` entry in the manifest (`ComplexRRCMatchedFilterBlock`,
`ComplexCostasLoopBlock`, `GardnerTimingRecovery`, `BPSKSlicerBlock`, …). See
[[FACTORY]] for the queue and the per-block dispatch, and the manifest-status note in
`AGENTS.md` §5.

---

## INV-26 — A block's own "green" tests can validate it OUTSIDE GR's operating regime

**Symptom:** a block ships with passing tests, is load-bearing in a BER-0 demo, yet fails
per-block GR-equivalence — because its tests compared it against a *self-generated*
stimulus that GNU Radio itself cannot process correctly.

**Ground truth:** `GardnerTimingRecovery` had green `test_gardner_convergence` /
`test_gardner_complex_reference` — but they drove it with a synthetic 2-samples/symbol
stimulus (`_make_bpsk_2sps`) on which **GR's own `symbol_sync_cc(Gardner)` recovers at
BER ~0.45** (i.e. does not lock). The block was tuned to that non-Nyquist stimulus. On the
true matched-filter Nyquist channel — where GR locks at BER 0 across the timing-offset
sweep — the Q15 Gardner TED (which `>>1`-halves both samples to fit int16 before the
product) jitters too hard and recovers at BER ~4–12%. The block was quarantined
(`needs_human`); MMTimingRecovery is the verified alternative. Extends the documented
Gardner 4-PAM limit (see the M17/4FSK lessons_log entries) to 2-level BPSK on a Nyquist channel.

**The rule:** a verification stimulus is only valid if GR ITSELF produces the correct
answer on it. Before comparing DUT-vs-GR, assert the GOLDEN is real — GR meets the pass
bar (e.g. `test_gr_gardner_locks_ber0_on_matched_channel` asserts GR BER==0 first). A
"green" test that never checks GR's own competence on its stimulus proves nothing. This is
INV-25's sibling: INV-25 = a poc was never held against GR at all; INV-26 = it was held
against GR on a channel GR can't do, which is just as blind.

**Applies to:** any recovery/adaptive/timing block whose test uses a hand-built channel.
Pin GR's competence on the channel FIRST, then gate the DUT against it.

---

## INV-27 — Validate `manifest.json` as JSON after EVERY conflict resolution (orchestrator)

**Symptom:** the queue tool / dashboard suddenly can't read the manifest; a merge or
cherry-pick "succeeded" but the committed `manifest.json` is not valid JSON.

**Root cause:** when merging parallel factory builders, two blocks touch adjacent manifest
entries and git leaves a `<<<<<<< / ======= / >>>>>>>` conflict inside the JSON. If the
orchestrator `git add`s + `--continue`s WITHOUT resolving that file, the markers get
committed — invalid JSON, silently, because git doesn't parse content.

**Ground truth:** during this tier-2/3 batch merge, `MultiplyConstComplex`'s manifest
entry was committed with live conflict markers (`json.load` raised
`Expecting property name` at line 421). Caught by validating, fixed by taking the
builder's (verified) notes side, `git commit --amend`.

**The rule (orchestrator merge hygiene):** after resolving ANY conflict that includes
`manifest.json` — and before `cherry-pick --continue` — run
`python -c "import json;json.load(open('verification/manifest.json'))"`. It must exit 0.
Do the same for any other JSON report touched. Never rely on git's clean exit; git
validates nothing about file content. Also: builders that "sync a block into the main tree
to run the venv gate" can LEAVE a stray edit in the main checkout's manifest (a block
flipped to `in_progress`) — `git checkout -- verification/manifest.json` before cherry-pick
if the working tree is unexpectedly dirty.

---

## INV-28 — Parallel factory builders MUST NOT mutate the shared editable-install finder

**Symptom (orchestrator):** after a parallel batch, `python -c "import gr_kyttar;
print(gr_kyttar.__file__)"` resolves to some builder's WORKTREE, not the main checkout —
so the orchestrator's verification runs against a random builder's tree, and any builder
whose gate ran mid-race tested the wrong code.

**Root cause:** the venv's editable install
(`site-packages/__editable___gr_kyttar_0_1_0_finder.py`) has a module-level
`MAPPING = {'gr_kyttar': '<path>', ...}`. It is GLOBAL, shared by every process using that
venv. A builder that repoints `MAPPING['gr_kyttar']` at its own worktree to run the gate,
then "restores" it, RACES with every other concurrent builder doing the same — restores
clobber each other and the finder is left pointing at whoever wrote last. Observed in the
tier-1 batch: the finder was left aimed at the AndConstBlock worktree; DiffDecoder's and
others' registrations were intermittently invisible.

**The rule:** a builder in a worktree runs its gate by PREPENDING its own tree to the
import path for that process only — `PYTHONPATH=<worktree>/runtime/python` (or
`sys.path.insert(0, ...)`), which cleanly SHADOWS the editable install without touching
global state. The NotBlock/DelayBlock/MapBB builders did exactly this and had no race. NEVER
edit the shared `MAPPING`/`.pth`/`.so` in `site-packages` from a parallel builder. The
orchestrator, before verifying a merged batch, must CHECK the finder points at the main
checkout and restore it if a builder left it dirty (this batch required a manual restore).
Related: the OOT-install boundary and stale-`.pth` shadowing lessons in lessons_log.md
(same shadowing mechanics, different trigger). Add this to the builder dispatch prompt's
environment note.

---

## INV-29 — Table-heavy protocol/waveform logic does NOT fit a Q15 cell; it needs the SRAM panel

**Symptom:** a block whose CORRECTNESS is a large lookup table or long adaptive state —
a character codec, a Morse/Varicode map, a long envelope table, an adaptive decoder FSM —
builds fine as a Python golden but its on-chip `build_cell_programs()` cannot fit the table
+ state + program into a cell.

**Ground truth (the PSK31 + CW/Morse wave, 2026-08-07):** 5 ham-mode blocks all
quarantined on the SAME wall, and it is now a measured, repeatable substrate boundary:
- a cell is **32 words total** (chip yaml `memory_words: 32`), shared by program + data +
  state; the `LOAD`-indirect table addresses **32** entries (`mem[Rn] & 0x1F` — that
  is the ISA limit). **CORRECTED 2026-08-29:** this used to say "~21 usable entries
  (`mem[Rn] & 0x1F`, the MapBB `MAX_TABLE=21` ceiling)", which conflated two
  different numbers. 21 is MapBB's OWN instruction-budget arithmetic for its own
  program, not a substrate cap; how many of the 32 a given block can actually spend
  on a table depends on that block's program and state. Compute your own budget —
  do not inherit MapBB's.
- **VaricodeEncoder:** 128-entry variable-length table = ~6× over; plus a data-dependent
  3–12-word burst emit with no variable-length WRITE-loop primitive.
- **VaricodeDecoder:** reverse code→char map = a **1024-entry** LUT (codeword values to 955)
  or ~200-node trie = ~49× over.
- **RaisedCosineEnvelope:** an sps-entry cosine table (sps=256 → 129 even after symmetry
  folding) + an sps-sample lookahead delay line.
- **CWKeyer:** ~48-entry Morse table + timing FSM + a raised-cosine click-suppression edge
  LUT together overflow the cell (program budget measured −2…−56 across sizes).
- **CWDecoder:** 64-entry reverse Morse LUT + **adaptive** unit-estimate/run-length/
  element-buffer state that is global+sequential (not wavefront-splittable like FIR taps)
  → ≥39 data words before a single instruction. The long-memory-state-in-SRAM case.

**The rule / what fits vs what doesn't:** the Q15 cell substrate hosts *DSP math* (filters,
mixers, loops, small constellations, per-sample transforms) but NOT *table-heavy protocol/
waveform logic*. The dividing line is the ~21-entry LOAD table and the 32-word cell. Such
blocks need the **external SRAM panel** driven by `SramControllerBlock` (the hardened
FPGA-controller macro — see INV-31) — a memory-interface construction, not a single-cell
drop-in. When scoping a block, estimate the table/state footprint FIRST; if it exceeds ~21
LOAD entries or ~31 registers, it is an SRAM-panel block, and building it single-cell will
QUARANTINE. This is a real capability boundary (a paper data point about the architecture),
NOT a builder failure. The long ON/OFF emit-loop itself IS representable (proven) — the wall
is the register footprint, so on-the-fly generation (NCO cosine) beats a table where possible.
Note the honest workflow value: each such block still ships a bit-exact Python GOLDEN
(Varicode from fldigi, Morse from ITU-R M.1677) so the spec is proven and the SRAM-panel
build has a reference. See the SRAM-backed-block-wave lessons_log entries.

---

## INV-30 — Rebase builder worktrees onto current HEAD BEFORE dispatch (stale-base corrupts the manifest)

**Symptom:** after merging a parallel batch, `manifest.json` has DUPLICATE entries, MISSING
entries (total block count drops), or reverted statuses — even though every block's source/
test/report committed fine.

**Root cause:** the `Agent(isolation:"worktree")` mechanism branches the worktree from a base
that can be OLDER than the orchestrator's current branch HEAD (observed twice: the tier-1
batch branched from a pre-batch commit; the ham batch branched from `40c9460`). A builder
edits its stale copy of `manifest.json`; on cherry-pick, a naive **take-theirs** resolution
wholesale-replaces the correct manifest with the builder's stale one, DROPPING every entry
added on main since the fork. A keep-both resolution instead DUPLICATES entries. Either way
the manifest is corrupted while the block files are fine.

**The rule:** (1) BEFORE dispatching a parallel batch, ensure worktrees branch from current
HEAD — if the isolation mechanism won't guarantee it, have each builder `git merge` current
main into its worktree as step 0 (the CWDecoder builder did this correctly and had no
corruption). (2) NEVER resolve a `manifest.json` conflict by take-theirs against a possibly-
stale base. The safe repair (used here): reset `manifest.json` to the known-good HEAD
version, then re-apply ONLY the batch's per-block status changes by name + adopt each
builder's richer notes. (3) ALWAYS assert `len(blocks)` didn't shrink and there are no
duplicate `kyttar_block` names after a batch merge (INV-27 sibling). See
`verification/tools/_merge_resolve.py` (whose take-theirs on manifest.json is UNSAFE against
a stale base — a known limitation).

---

## INV-31 — The SRAM panel is the substrate's memory tier; drive it with `SramControllerBlock`

**What it is:** the answer to INV-29 (table-heavy/long-memory logic doesn't fit a 32-word
cell). An **SRAM panel** (`placekyt/engine/sram_panel.py`, `SramPanelDevice`) is dumb
storage + a tiny register protocol, HOST-SIDE (the FPGA implements it in the demo; embedded
panels in the array on the next chip). A **`SramControllerBlock`** — a 1-cell HARDENED
FPGA-controller macro — sits at the panel port and drives it with the EXISTING ISA (no new
primitives): `WRITE @N, dest` for panel data registers, `JUMP @N, entry` for panel triggers.

**The protocol (full contract in [[SRAM_PANEL]] / `verification/SRAM_PANEL.md`):** panel
regs R0=write-commit trigger (JUMP), R1=read trigger (JUMP), R2=payload (WRITE), R3/R4=
read-out WRITE/JUMP descriptors (WRITE), R5+=address (WRITE). The **decisive mechanism** is
the self-driven **push-read**: the controller pre-writes R3/R4 (*where the answer should
land + which entry to kick*), writes the address, and JUMPs R1 — the panel then ORIGINATES
a `WRITE(dest)=value` (+optional `JUMP(entry)`) into a chip INPUT port, delivering the
looked-up word back out a **different port** to the destination hop. The controller does not
poll; the read is asynchronous and panel-originated.

**The rule / when to reach for it:** a block whose correctness is a big LUT (Varicode
128/1024-entry, Morse ~64, constellation >21), long memory (interleaver depth, correlator
history, Viterbi survivors), or unbounded adaptive state → do NOT force it single-cell (it
QUARANTINEs per INV-29). Build it as an **SRAM-backed** design per the SRAM_PANEL.md §6
recipe: table lives in the panel (unbounded), small index/logic stays in cells, the answer
arrives via the push-read. This is the heterogeneous compute-next-to-memory pattern the next
chip generalizes to embedded panels.

**Ground truth:** `SramControllerBlock` is VERIFIED — `placekyt/tests/test_sram_panel.py`
(21 tests) incl. a write-then-read-back-out-the-port round-trip through REAL routing
(`0xCAFE`→addr 3→emerges on `x16_out`) + the runnable `engine/sram_demo.py`. The older,
naive `FpgaRamBlock` (2-cell orchestrator, passive pull-read, no address registers) was
DELETED 2026-08-07 — do not resurrect it; `SramControllerBlock` is the one true SRAM path.

---

## INV-32 — Routes are STRICT shortest paths, and a block's output corridor must NEVER transit its own input-delivery broker (deadlock cycle)

**Symptom A (quality):** shipped `.kyt`s carried routes like 21 cells for a
manhattan-5 hop and 25-for-3 weaving beside their own path; every one-free-cell
block pair staircased up-and-over to a far-side broker. Ugly in the GUI — and a
real saturation hazard: the longer a shared corridor, the more back-to-back
in-flight words pile into it.

**Symptom B (correctness — the hard one):** a design passes EVERY per-sample
gate, then under saturated (pipelined / Full-speed) drive the sim reports a hard
`Deadlock` with ZERO output (data_link: 2103 events then stuck). Per-sample
pacing fully hides it.

**Root cause A:** the bus router priced corridor sharing as a DISCOUNT (fresh
cell 5, bus cell 1) — sharing could buy a ~5× longer route; the broker was
picked by bus/spine membership BEFORE routing (distance-blind, so routes
wrapped their target); a net could not broker on its OWN source's emit cell
(the reservation guarding FOREIGN emit cells was applied to all nets).

**Root cause B:** a strictly-shortest route can legally pass THROUGH the broker
cell that DELIVERS INTO the routed net's own source block. The block's output
words then occupy the single-outstanding link its own NEXT input must cross —
a closed 4-cell wait cycle (`src → … → own-input-broker → src`). This is the
cross-net sibling of INV-19/20: not a block bug, a ROUTING topology bug.

**The rules (all enforced in `engine/bus_router.py`, 2026-08-11):**
1. Path length strictly dominates (`_HOP_COST` per cell); corridor sharing,
   emit-face starts, and straight-over-staircase are SUB-HOP tie-breaks only.
2. A net may broker on its own source's emit cell; FOREIGN emit cells stay
   last-resort. Brokers are chosen by ROUTED distance over all legal
   candidates (ties → fewest turns).
3. HARD guard, both routing orders: a net sourced at block B treats every
   broker delivering into B as impassable; a broker candidate for a net
   targeting B must not sit on B's committed output corridor. An unroutable
   net (named failure) is always preferred over shipping a cycle.
4. A terminal broker's onward direction stays UNCOMMITTED until a transiting
   net claims it (the build's `broker_through_face` restores to exactly that);
   pre-committing the arrival direction walls same-source fan-out arms.
5. The v2 single-backbone result is kept only when the legacy per-net loop
   cannot route everything strictly shorter; orderings are scored by
   (nets routed, total corridor length), never first-full-wins.

**Gates:** `verification/tests/test_route_quality.py` (per-net excess ≤ 8 vs
manhattan, no revisits, per-file pinned totals over every shipped example
`.kyt`); `test_one_gap_pair_brokers_on_own_emit_cell` (minimal hand-off form +
on-chip compute); `test_data_link_example.py::test_shipped_kyt_saturated_matches_per_sample`
(the deadlock-cycle pin); `test_audio_meter_example.py::test_shipped_kyt_saturated_matches_per_sample`
(duplex saturated bit-exactness). Remaining route excess in the ratchet is
PLACEMENT-forced (walls of folded blocks); the next quality lever is picking
the auto_pnr attempt by total route length, not the first fully-routed one.

**Review-driven hardening (2026-08-11, adversarial code review of the fix):**
- The dominance invariant (tie-breaks < one hop) is now ASSERTED per chip
  against the real fabric size (`_HOP_COST > 2·W·H + 2` in `route_all_bus`) —
  the constant was hand-derived for 120 cells while chip dims are
  YAML-parameterized and >31-hop routes are relay-extended.
- The own-block delivery cycle is ALSO caught at the DRC (`check_bus` check
  (c)): any net sourced at block B transiting a broker that delivers into B is
  a named deadlock — covering v2-routed and HAND-LAID routes the router-level
  guard never sees. SCOPE IS DELIBERATE: a GENERAL through-block cycle test
  (block supernodes in the wait graph) was tried and FALSE-POSITIVED the
  proven-saturated coherent RX — a static cell-cycle over independent corridor
  segments cannot be distinguished from a true circular wait without
  rate/buffer modeling. The own-block shape is deadlock-CERTAIN (same stream,
  causally chained); anything broader is owned by the saturated example gates
  (the empirical check). `test_bus_drc_block_cycles.py` pins the caught
  shapes, the 2-cycle, the non-flagged clean chain, AND the out-of-scope
  logical-loop case.
- The hazard-DISABLED fallback (`sc_cells=None` re-route) regained dedicated
  coverage: `test_walled_corner_fallback_demotes_to_named_failure` forces the
  one-free-neighbour corner and asserts the unsafe route is DEMOTED to a named
  failure by the router's own DRC gate, never returned silently ok.

**Port-input hardening (2026-08-16, the FLLBandEdge pinch):** the own-broker
guard did NOT cover CHIP-PORT input deliveries, and — the decisive gap — the
guard lived only in `route_all_bus`: the controller's MAZE/heuristic ESCALATION
paths had no chip-port awareness at all, so a pinched design (a wide block ring
sealing the side channels against a corner port) shipped a corridor THROUGH the
USED x16_in port cell + its delivery broker as a silent success (route ok, build
ok, injections swallowed). Rule now: **a corridor may not OCCUPY a chip-port
cell that any connection USES as its I/O terminus unless it owns that port (or
its source/target block sits ON the cell — the direct-injection idiom); an
UNUSED port cell stays a plain routing cell (soft-penalized only).** Enforced as
(1) `bus_drc.check_port_transits` — check (d) in `check_bus`, kind
`port_transit`, surfaced as a hard error at project DRC/build (covers hand-laid
routes); (2) hard walls in the bus/maze/heuristic routers (CP-SAT always had
one) so a detour is taken when one exists; (3) a `_demote_port_transits`
backstop over `_run_router`'s final report; (4) a named bus-router failure
reason ("port-transit hazard") via a relaxed diagnostic probe. Gates:
`placekyt/tests/test_port_transit_guard.py` (INV-4 proven pre-fix).

**Applies to:** every auto-routed design; any future router change must keep
the ratchet green and the saturated example gates green.

## INV-33 — The cell register-allocation CONTRACT: inputs < data < state; R0 is the ACCUMULATOR, not a mailbox

**Symptom (all hit building the LMS equalizer):** (a) "No register space for
state 'X'" on a cell that physically fits; (b) an input value silently ZERO /
stale by the time the program reads it; (c) a state var's initial value lands in
an INPUT register and gets clobbered by the first delivery.

**The resolver's allocation rule** (`compute_state_registers`): explicit-address
data words are honored; ``state`` is auto-allocated ONLY into the gap
``range(max_data_address + 1, 31 - instr_count)`` — it never scans holes below
the data words and never avoids INPUT registers outside that gap. So the
authoring contract is strict: **input registers at the LOW addresses, data words
ABOVE them, state auto-lands above data, instructions fill the top.** A hole
between inputs (e.g. an absent acc input on the first tap of a chain) is usable
ONLY by PINNING state into it explicitly (``StateVar(register=N)``).

**NO-DATA-WORDS corollary (hit building the CORDIC blocks):** a cell with ZERO
data words has ``max_data_address = -1``, so the auto-scan starts at **R0** and
state lands ON TOP of R0 and the input registers — the block builds cleanly and
runs garbage (every temporary write clobbers an input). ANY cell whose state is
not explicitly pinned needs at least one data word below it; in practice: **pin
every StateVar register explicitly, always.** (The CORDIC XY cells: inputs @1,2
→ pin state @3,4,5.)

**R0 rules:** every ALU op writes R0, so an input delivered to R0 is destroyed
by the FIRST ALU/MOVE-to-R0 instruction. Land inputs at R0 only as a deliberate
idiom: the ACCUMULATOR-DELIVERY trick — the upstream cell WRITEs a running sum
into the downstream's R0 as its LAST write before the trigger, and the
downstream's FIRST instructions are the MAC(Q)s that consume it (saving the seed
MOVE that otherwise doesn't fit). The consumed-and-reemitted value must then be
WRITTEN FIRST so the next hop's R0 delivery is never disturbed by later writes.

**Positional pairing:** ``build_cell_programs()`` dict order MUST equal
``default_layout()`` order (cells are paired BY INDEX; face-only ``transit_*``
entries go LAST in the layout). A mismatch assigns program A to cell B with no
error — the block builds and runs garbage.

**Internal-feedback pass hazard:** ``_apply_internal_feedback`` re-patches every
connection that runs BACKWARD in program order, matching the source cell's WRITE
**by destination register** — ambiguous when a broadcast cell fans out several
writes sharing a dest register (the LMS y_i hop was clobbered by the g-fan-out's
patch). Order broadcast-source cells EARLY in the program dict so their edges
are FORWARD and the pass never touches them.

**THE OVERLAP HALF — a cell at EXACTLY 32/32 words pins its state ON TOP of its
own first instruction** (found on FFT64; three cells were in this state). The
word-count check every block writes,
``max_addr + 1 + instr_count <= 32``, PASSES a cell that is exactly full. But the
resolver lays instructions DOWNWARD from address 30 to
``base_addr = 31 - instr_count``, and it honours an explicitly-pinned
``StateVar(register=N)`` wherever it is told — *including inside that range*. Its
own guard (`"Not enough register space"`) only compares DATA against
``base_addr``; **it never checks state.** So the cell assembles, the bitstream
loads, the program runs ONCE, and its first ``MOVE R{state}, R0`` zeroes the
instruction word the next trigger enters at.

*Symptom:* the block emits exactly ONE sample and goes quiescent — which is
indistinguishable by inspection from a serialize-LOCK that never clears, and cost
a full dispatch chasing the lock.

*The gate* (static, cheap, catches it before any chip run): for every authored
cell assert no data address and no state register is
``>= 31 - instr_count``. Pair it with an INV-4 negative that re-inflates the
pre-fix shape.

*Freeing a word must not change arithmetic.* Two moves are usually enough, and
both must be proven by running the reduced cell against the UNREDUCED one over
its whole input domain: (a) delete provably dead words — a branch pad whose
target already holds the right value, or a ``CMP`` re-deriving a flag an earlier
``SUB`` set (MOVE does not touch flags); (b) move a merely-forwarded value to the
ACCUMULATOR-DELIVERY idiom above — it arrives in R0 and the cell's FIRST
instruction re-emits it, freeing both the input register and the staging MOVE.

## INV-39 — A dispatch ENTRY that no jump targets is DEAD CODE, and only the chip can tell you

**The rule:** in the multi-entry dispatch idiom (a cell whose PATH identity travels
as *which entry* the next cell is jumped at — TwiddleMultiply, the octant fold,
every trivial/numeric split), **every declared ``EntryPoint`` must be the target of
at least one ``internal_jumps`` edge.** An entry nothing jumps at is unreachable;
the cell still assembles, still fits its budget, and still runs — down the wrong
path, forever.

**How it presents (FFT64's octant fold):** ``sign`` declared ``num`` and ``triv``
entries, but ``swap`` was given ONE jump port wired unconditionally to ``num``. The
two structurally trivial twiddle slots (``k = 0`` and ``k = N/4``) then emitted
numeric words instead of the sentinel encoding the downstream cell dispatches on.
Thirty of thirty-two slots were right, so nothing looked broken — but two wrong
twiddles per cycle put the ENTIRE odd-bin half of every output frame wrong.

**Three traps that hid it, each general:**

1. **A cell-level harness must read the dispatch decision OFF the cell, never
   re-decide it.** The standalone fold check chose ``sign``'s entry itself from the
   control word, so it exercised code the built chip could not reach and reported
   all 32 slots correct. Derive the decision from observable cell behaviour (here:
   the ``triv`` exit is the only path that writes no magnitudes) and ASSERT it
   agrees with the control word.
2. **Size a streaming gate by what it REACHES, not by how many samples it sends.**
   An 80-sample FFT64 run was bit-exact and proved nothing: the first valid output
   is at index ``N-1``, and frame slots ``0..N/2-1`` are the EVEN bins (the sum
   branch). The twiddled half is not touched until output ``N-1 + N/2``. State the
   reach of every streaming gate explicitly.
3. **When no arithmetic model reproduces an on-chip divergence, stop modelling and
   read the intermediate state off the chip.** Exhaustive searches over wrong
   twiddle words, slot substitutions, pointer stalls, ring-timing offsets and
   counter drift ALL failed to reproduce it. Reading the ``steer`` cell's latched
   twiddle pair after each trigger found it in one run: wrong at exactly triggers
   0 and 16 — the two trivial slots — and nowhere else.

**Sharpest stimulus for this class:** a single IMPULSE. It makes the ideal output
a constant across the frame, so a phase error reads directly as a rotation
(``512 -> (510, -50)``, which is exactly ``512 * W_64^1``) instead of as noise.

## INV-34 — ISA: shift counts are IMMEDIATE instruction fields; the silicon design is the authority for ISA claims

**The rule:** a shift/rotate word is `OP | ROT[11] | RSVD[10] | CNT[9:6] |
SRC[5:0]` — CNT is an immediate count (0-15) and bit[10] is reserved
(PROGRAMMING_GUIDE §4.3). There is no register-count shift form; the assembler rejects
`[Rm]` count syntax at the root and the decoder treats bit[10] as reserved.

**Expressing data-dependent shift amounts with immediate counts** (all
proven in shipping blocks):
- **Fixed-position extraction over a left-aligned working register**: align
  the data once (at pack/table-build time when possible), then walk it with
  `SHR #15` / `SHL #1` per step (VaricodeEncoder's packed-word walk).
- **Arithmetic identity for a 0/1 count**: `x << b == x + x*b` — a branchless
  MUL/ADD pair (VaricodeDecoder's pending-zero commit).
- **CMP-guarded `SHL #1`**: CMP sets flags WITHOUT touching R0, so the value
  being shifted survives the test.
- **Shift-by-one loop** with a counter, for genuinely variable counts.

**Authority order for any ISA claim: the silicon design > the spec's
FIELD TABLES > simulator/assembler prose.** The instruction field tables
(PROGRAMMING_GUIDE.md §4) are canonical; prose and examples elsewhere can lag
them, and the simulator is kept conformant to the design. Before relying on a
"discovered" feature that appears only in prose or in simulator behavior,
check it against the field tables.

Gate: `verification/tests/test_silicon_isa_subset.py` — source-scans every
block for `[Rm]` shift syntax and asserts the assembler rejects it.

Confirmed-real for contrast: `GOTO label` is assembler sugar for a local
`JUMP hop_cnt=31`; WRITE/MOVE preserving FLAGS is real hardware behavior
(only ALU/logic/shift/CMP ops update the flags).

---

## INV-35 — `default_layout` is a POSITIONAL INDEX: program cells first (in program order), transits last; the external-egress cell LAST of all

The layout dict is not just geometry. Two different parts of the toolchain read
it as an ORDERED list, and a multi-cell block that ignores either ordering
builds and routes cleanly and then computes garbage.

**1. Program cells come first, in exactly `build_cell_programs()` order;
FACE-only `transit_*` cells come last.** The router resolves a declared
internal handoff by indexing the PLACED CELL LIST positionally against the
`cell_programs` keys — `pb.cells[keys.index(dst_cell_id)]` — and `pb.cells`
is built from `default_layout`. Emitting a transit at its natural *chain*
position (the obvious thing to do when the layout function walks a serpentine
in order) shifts every later program cell's resolved destination by the number
of preceding transits. Nothing errors: the writes land in some other cell's
registers.

**2. A cell whose ONLY output port is the block's EXTERNAL egress must be the
LAST program cell.** Its hop is stamped by the build's egress patcher, but
until that pass runs the router's positional-default branch ("hand this port
off to the NEXT dict cell") is still live for the port, and that default
traces the ROUTE-TIME faces. On a CLOSED ring the trace can go the long way
round: measured on GRUCellBlock, 6 hops at identity and **44** under the two
180-degree D4 orientations, which the assembler rejects with
`Distance must be 0-31` before the patcher can overwrite it. This is invisible
at identity — only the 8-orientation gate (INV-23) catches it.

Two fixes that look right and are NOT (both measured):

* Declaring the egress port `__terminate__` in `internal_connections` does
  silence the positional default, but it also makes the cell an
  internal-handoff SOURCE, so the build's "restore the authored internal
  forwarding face" pass overwrites the route's face on that cell and the
  egress dies — no output at all, and a ring that stalls with its state
  frozen. (`__terminate__` in `internal_jumps` alone is harmless, but it does
  not address the WRITE.)
* Moving the egress relay off-abutment leaves the source cell's in-program
  `MOVE [FACE]` flip aimed at a Manhattan fallback that does not follow the
  fold.

**Audit it structurally, don't rediscover it.** Assert in the block's own
suite that (a) `list(default_layout())[:n] == list(build_cell_programs())`
with only `transit_*` entries after, and (b) the last program cell is
`output_cell_id()`. Both are one-line tests; see
`verification/tests/test_gru_cell.py`
(`test_every_program_cell_precedes_every_transit_in_the_layout`,
`test_egress_relay_is_the_last_program_cell`). This sits alongside the
ROUTE-TIME FACE RULE (lessons_log, FFT16): that one governs which face a cell
ends up with, this one governs which CELL a handoff resolves to.

---

## INV-36 — The hop field stops at 31; a longer route must be SPLIT at RELAY cells

A `WRITE`/`JUMP` carries a **5-bit HOP_CNT**, so a single emission can address a
cell **at most 31 hops away**. This is a hard field limit, not a heuristic: the
assembler rejects `@32` outright (`Distance must be 0-31`), and a word travels by
being forwarded until HOP_CNT reaches 31, at which point the arriving cell
executes it locally instead of forwarding (`execute_locally` in the trace).

So a route longer than 31 hops cannot be delivered by one emission at all. The
router used to plan relay cells for these and then fail the net, because the
build never programmed them.

**The fix — segment the route.** The word is addressed to LAND on an intermediate
plain routing cell, whose relay program flips to the route's continuation face
and re-emits the payload + trigger with a FRESH budget:

```
MOVE [FACE], <exit_face>     ; point at the rest of the route
MOVE R0, R{in:burst}         ; the landed burst
WRITE @<next_seg>, <dest>    ; re-emit the PAYLOAD
JUMP  @<next_seg>, <entry>   ; re-emit the TRIGGER
HALT
```

This is the SAME land→flip→re-emit primitive the CrossoverBlock demux already
proves on-chip, specialised to one track — do not invent a second mechanism.

Four rules that make it correct:

1. **Emit the WRITE *and* the JUMP.** A relay that forwards only the payload
   delivers a silent stream that never triggers the destination. The final relay
   re-emits the net's ORIGINAL `dest`/`entry` (read from the source's
   already-patched exit WRITE/JUMP), so the destination sees exactly what a
   hop-legal route would have delivered.
2. **Chain backward.** Program the LAST relay first: each relay must address the
   *resolved entry* of the relay after it, and the SOURCE is finally re-pointed
   at the FIRST relay. Segments are independent — each carries its own budget.
3. **Never relay onto a cell that is in use.** A block cell, a USED chip-port
   cell, or an existing broker is a HARD rejection (INV-32 / the port_transit
   class) — landing a word on one runs someone else's program with a routing
   payload. When no free candidate exists the route is a NAMED failure, never a
   relay overlaid on live programming.
4. **One planner, three gates.** The router, the build, the DRC's `hop_overflow`,
   and the controller's `add_route` guard all call the SAME `_plan_relays`, so a
   long route is never accepted by one and rejected by another.

**Cost is real and must be reported.** A relay consumes one array cell per ~30
hops of route; `ChipBuild.relay_cells` / `.relay_cost` surface it so the area is
visible rather than silently spent.

Relays CHAIN, so there is no practical hop ceiling left beyond available array
area — a measured 96-hop route runs correctly through three relays. Gated by
`placekyt/tests/test_relay_emit.py`, including the on-chip bit-exactness of a
relayed net against a hop-legal control (the relay must be transparent to data)
and the mutations that must fail: relay omitted, stale hop count, mis-faced relay.

---

## INV-37 — A baked `is_face=True` constant PINS the fold; derive it from the layout

**Symptom:** a block is re-folded (its `default_layout` reshaped — a different
serpentine, a transpose, a different start cell on the same closed ring). Every
*geometric* gate still passes: the ring is a valid closed cycle, both bbox dims
are within the cap, the route-time face rule is clean, the layout dict ordering
is right, placement is legal. The build succeeds. And then the block **computes
the wrong answer** — in the observed case a recurrent block's hidden state froze
at its first timestep (timestep 0 exactly correct, every later one identical to
it), and the whole-block simulation ran to `EventLimit` without completing.

**Root cause:** cell programs may carry `DataWord(..., is_face=True)` constants —
literal face codes (`SOUTH 0, EAST 1, WEST 2, NORTH 3`) the program MOVEs into
`[FACE]` or `[LOCK_FACE]`. They encode an ABSOLUTE direction that was true of the
fold **as authored**, e.g.

* an egress cell's `face_out` = "the direction my off-ring output relay lies in",
* its `face_ring`  = "the direction my ring SUCCESSOR lies in" (the face to
  resume on after emitting), and
* an input-landing cell's `LOCK_FACE` = "the direction my ring PREDECESSOR lies
  in" (the face the feedback write-back and the unlock `WRITE.CFG` arrive on, and
  which must stay admitted while the port corridor is held — INV-19).

The D4 orientation machinery transforms these correctly when the WHOLE block is
rotated, which is why the 8-orientation gate does not catch it. But a RE-FOLD
changes the *relative* geometry — which side of the egress cell its relay sits
on, which way the ring leaves it, which side the closure re-enters the head cell
— while the baked literal stays put. The cell then rests on the wrong face: words
are forwarded into a neighbour that is not their destination, the feedback never
lands, and the barrier never clears. **Nothing in the geometry gates can see
this**, because the geometry is fine; only the DATA is wrong.

**Fix — derive every face constant from the fold, never write it down.** Make the
fold a single source of truth (one method returning the ring positions and the
off-ring relay position), and compute each `is_face` word from it:

```python
def _face_from(self, a, b):          # a, b adjacent cells
    d = (b[0] - a[0], b[1] - a[1])
    return self._FACE_CODE[{(0, 1): "south", (0, -1): "north",
                            (1, 0): "east", (-1, 0): "west"}[d]]
```

Then `face_out = _face_from(egress_cell, relay)`, `face_ring =
_face_from(egress_cell, ring_successor)`, `LOCK_FACE = _face_from(head,
ring_predecessor)`. Re-folding is now a one-method change and the constants
follow. Verified on `GRUCellBlock`: with the three literals baked, three
independent re-folds (a 5x10 Hamiltonian ring, a transpose, a reversed
traversal) each failed 20 of 52 gates with h frozen; with them derived, the same
transposed fold passes all 52 unchanged.

**How to catch it:** the block's own behavioural gates ARE the detector — an
h-trajectory / state-persistence test that compares more than the final output.
A re-fold must re-run the FULL suite, not just the layout gates. If a block has
`is_face=True` words, treat its `default_layout` as load-bearing for CORRECTNESS,
not just routability, and say so in the layout docstring.

**Applies to:** any block with `is_face=True` data words — dual-face emitters,
`LOCK_FACE` barriers (INV-19/20 serialize-lock), ring/serpentine folds with an
off-chain relay. Related: INV-23 (orientation invariance, which does NOT cover
this), INV-35 (the layout dict is also a positional index).
## INV-43 — A remote JUMP does NOT stop local execution; a branch-gated cell needs a HALT (the `GOTO` claim here was FALSE — see below)

**Symptom (measured on GardnerTimingRecovery; the FIR block's saturating-restore
carries the same warning):** a two-path cell whose STROBE path ends in
`{jump:next}` and whose NO-STROBE path is the fall-through target *below* it
fires **both** triggers on every strobe. Downstream, the second entry runs after
the first and overwrites what the first computed.

```asm
start:
    MOVE R0, R{in:mu}
    AND  R0, R0
    BR.N nostrobe
    ...compute...
    {jump:trig}          ; remote JUMP — kicks the target and KEEPS GOING
    HALT                 ; <-- REQUIRED. Without it, execution falls into:
nostrobe:
    {jump:ns_trig}       ; ...and this fires too, every strobe.
```

`JUMP` sets a **remote** cell's PC. It is not a local branch, and it does not
end the issuing cell's instruction stream — the thread runs straight into the
next word. **This half is VERIFIED on the simulator**: the instruction after
`JUMP @1,0` executes.

**CORRECTED 2026-08-29 — the `GOTO` half was FALSE.** This used to claim "the
same is true of `GOTO` … the current thread continues into the following
instruction." Measured side-by-side on the simulator in one harness with a
passing control: after a remote `JUMP` the next instruction RAN (R20=222); after
a `GOTO` it did **not** (R20=0), and the target did not run inline either. So
`GOTO` neither falls through nor transfers inline, and the fall-through hazard
this invariant is about does **not** apply to it.

The real `GOTO` hazard is a **TOOLCHAIN** defect, not hardware, and it is
fixable: the build's output-handoff pass rewrites every opcode-0x7 word in an
EXIT cell into an external JUMP, so a local `GOTO` in such a cell is silently
turned into something else (see the notes in `rms_block.py` and `add_block.py`).
Avoid `GOTO` in an exit cell for that reason — not because of fall-through.

**Why it is expensive to find: the failure is ARITHMETIC-SHAPED.** Gardner's
`loop_filter` has a `strobe` entry that captures the timing error and a
`nostrobe` entry that forces it to zero. With the missing `HALT`, both ran on
every strobe and the no-strobe one arrived second, so the error was zeroed
*after* the integrator had already consumed it. The observable was:

* `vi` (the integral state) matched the reference **bit for bit**, every sample;
* `v` (the PI output) equalled `vi` **exactly**, every sample.

That is, one term of a two-term sum silently vanished and a 2nd-order loop
degraded to a pure integrator. On-chip BER 0.22 against a reference BER of 0,
with every intermediate looking plausible. Nothing about it points at control
flow.

**The rules:**

1. **Terminate every non-final path.** A cell path that ends in a remote
   `{jump:...}` and is followed by *any* other code must end with `HALT`.
2. **Never use `GOTO` to skip a fall-through block.** Order the two entries so
   the path you want falls through NATURALLY, and have the other branch FORWARD
   over it. `MOVE` does not touch the flags, so the compare must be explicit:

```asm
nostrobe:
    MOVE R{state:es}, R{data:zero}
    CMP  R{data:zero}, R{data:zero}   ; sets Z unconditionally
    BR.Z pi
strobe:
    ...egress...
pi:
    ...shared tail...
```

3. **Diagnose it from the LOOP'S OWN STATE, not the output stream.** Dump each
   loop-carried register per sample and diff it against the reference's. "The
   integrator is exact but the output equals the integrator" names the defect in
   one line; comparing only the emitted stream tells you nothing but "it
   diverges at symbol 12".

**Applies to:** any cell with more than one entry or more than one exit path —
strobe/no-strobe gates, parity branches, saturation restores, decision paths.
Related: INV-33 (the register contract, whose "emits exactly ONE sample then goes
quiescent" symptom is a sibling false-lead), INV-39 (a dispatch entry no jump
targets is dead code — this is the converse: an entry that fires when it
shouldn't).

---

## INV-44 — A block's EXTERNAL-EGRESS cell and its INTERNAL-FEEDBACK source must be DIFFERENT cells

**Symptom:** a feedback block places, routes and builds clean, egresses at the
correct rate, and its forward datapath is bit-exact — but the loop never adapts.
Its feedback relay never executes, the fed-back state stays at its reset value,
and on-chip results sit in the uncanny band between "broken" and "working"
(GardnerTimingRecovery measured BER 0.03–0.15 against a reference BER of 0).

**Cause: four independent build passes each claim an exit cell's WRITE/JUMP
words, and they conflict when one cell holds both roles.**

* `output_at_last_write` patches the cell's HIGHEST-ADDRESS WRITE with the output
  hop — a SINGLE-WRITE contract. It cannot express "the last write of the strobe
  path but not of the no-strobe path", so a two-entry egress cell mis-patches one
  path.
* `_apply_routes` rewrites EVERY WRITE in a ROUTED exit cell to the output
  corridor, and the `feedback_blocks` preserve-set only protects cells that are
  themselves feedback SOURCES in chain order.
* `_apply_internal_feedback` re-patches the cell's highest-address JUMP when the
  feedback edge is declared as a connection — which on a fused cell is the
  EXTERNAL EGRESS trigger.
* Reordering the cells to make the feedback edge backward fixes the WRITE and
  breaks the JUMP, and vice versa. Both orderings were built and measured.

Observable signatures, each of which looks like an unrelated bug: the egress
WRITE left at its authored `@1` (builds and routes clean, emits **nothing**); the
feedback WRITE repointed at the output corridor (the relay never runs, the block
emits **exactly ONE** sample then goes quiescent — indistinguishable from the
INV-33 overlap); strobe gating lost (**exactly 2x** the expected output count).

**The fix is structural and costs one cell.** Fan the last datapath cell out:

* the recovered result goes to a **DEDICATED egress cell** — exactly one WRITE,
  one JUMP, one face, no state, no feedback — on one face;
* the loop correction goes to the **feedback relay** on a PERPENDICULAR face;
* the relay is ordered **LAST in the program dict**, so its edge back to the
  landing cell is the block's ONLY backward internal connection and
  `_apply_internal_feedback` has exactly one edge to resolve.

Making the egress contract single-valued BY CONSTRUCTION is the point: the
dedicated cell has nothing else in it for the output passes to clobber, and
nothing in it that the feedback pass wants. `MMTimingRecoveryBlock` and
`GardnerTimingRecovery` both use this shape (`loop_filter` → `qout` +
`period_relay`), and both close their loops on the first build.

**This is NOT a substrate limit.** It was recorded as one after a failed 5-cell
fused design; splitting the roles fixed it with no change to
`placekyt/engine/build.py`. Do not "save a cell" by fusing them — the fused
shape's failure mode is silent, and the split costs exactly one cell.

**But DO spend effort on the fold.** The split cell is cheap; a sprawling fold is
not. Gardner's first working split fold was 6x2 with a four-cell transit lane (11
cells), and it broke the shipped full-duplex BPSK modem: one of eleven nets
stopped routing, taking 21 tests down. Re-folding to 7 cells with zero transits
restored those — and then the fold's ASPECT cost two more iterations, because TWO
DIFFERENT placer behaviours punish the two rectangular folds and each is invisible
from inside the block (all three folds are BER 0 and bit-exact in its own suite):

* a **TALL** fold trips the packer's FIT-DRIVEN ROTATION of a feedback block whose
  authored height would overflow the current band (`autoplace._pack_compact`:
  `h > w and row_top + h > height` -> rotate `cw`). The flyline orienter
  deliberately leaves feedback blocks at identity, but this fit path overrides it,
  and once ONE block rotates the orienter re-orients everything downstream into a
  much worse global packing. Measured: a 2x4 Gardner gives the shipped duplex BPSK
  modem 6/11 nets at the compact reserve and 9/11 after the whole 45 s auto-P&R
  sweep (11/11 only at a 300 s budget — raising the budget "fixes" the gate by
  making an interactive operation take four minutes, which is not a fix).
* a **4-WIDE** fold walls the coherent-RX chain's matched-filter -> Costas bus
  channel: 5/7 nets, `no bus path from source to the broker tap`.

A **SQUARE** fold triggers neither. **Enumerate the legal folds, then score the
candidates by RUNNING THE DESIGNS THE BLOCK SHIPS IN** — there were 144 legal 3x3
zero-transit folds for this ring and the first one scored passed both families
(modem 11/11 in 2 s, production RX 7/7). Area alone is not the criterion and the
block's own suite cannot see the difference.

**PARITY IS A PROPERTY OF THE DECOMPOSITION, NOT THE BLOCK.** A grid is
bipartite, so only EVEN rings close by abutment. Gardner's ring was recorded as a
five-cycle needing a mandatory transit — but moving the delay line out of the NCO
into its own cell made it a SIX-cycle, and the transit vanished. Before accepting
"this ring needs a transit lane", check whether splitting or merging one cell
flips the parity.

**Note on INV-35 clause 2.** INV-35 says the external-egress cell must be the
LAST program cell. That clause is about a cell whose ONLY output is the egress,
where the router's positional default would otherwise trace the ring the long
way. In this shape the egress cell is second-to-last (the feedback relay is
last) and its trigger is explicitly `__terminate__`-ed, which silences that
default; both blocks are verified across all 8 D4 orientations. Audit the
positional rule as INV-35 describes, but read its clause 2 as "no cell may be
BOTH the egress and a feedback source", which is what it is really protecting.

**A related trap once the roles are split: the face the feedback LEAVES on is not
the face it ARRIVES on.** The landing cell's arbiter LOCK gates every face except
the one the feedback arrives on, so it needs its own constant — separate from the
"which way does the loop filter emit v" one. Gardner had them equal in one fold
and different in the next; the mismatch left the lock permanently engaged and the
block emitted **exactly one symbol and went quiescent**, a signature INV-33 warns
is indistinguishable from a state/instruction overlap. Derive BOTH from
`default_layout` and assert it (`test_faces_match_the_layout`), per INV-37.

**Audit it structurally:** assert in the block's own suite that
`output_cell_id()` sources no internal connection, and that there is exactly ONE
backward internal edge. See
`placekyt/tests/test_gardner_build.py::test_places_with_split_egress_and_feedback_cells`
and `::test_feedback_closes_under_rotation` (the latter matters because a
rotation that puts a feedback transit under an external corridor kills the loop
SILENTLY — the route pass runs first and overwrites the transit's authored face).

---

## INV-38 — A verification report is an ARTIFACT of a verified session, never a literal; ABSENCE must be the safe state

`verification/reports/<Block>.json` is the file the dashboard reads as *"this
block was verified against GNU Radio."* It is the project's unit of evidence.
That makes a report writer the one place where a single hardcoded `True` can
convert a **failing** session into a **green** claim — the exact failure the
whole verification harness exists to eliminate, arriving through the harness's
own front door.

**The rule, in one line:** a report file must be a *function of the session that
produced it*, and if that function cannot be evaluated, **no file must exist**.

### How this was found (the real instance)

A block's `test_zz_write_report` wrote `"passed": true` into its payload
unconditionally. A session in which the saturated-drive gate **FAILED** still
emitted a green report, because pytest continues past a failure by default and
the writer runs in a *later* test. The dashboard would have read a pass that
never happened. The builder caught it in its own code, deleted the false report,
and fixed the writer; this invariant generalises that fix to every writer in the
repository.

### The three shapes of the defect

All three write a green file that the session did not earn. Recognise all three:

1. **The literal.** `report = {"passed": True, ...}` — the author asserts the
   verdict. Nothing in the session can contradict it.
2. **The fabricated result.** `write_report(name, CompareResult(passed=True, ...))`
   — the same literal wearing the harness's own type. The call site looks correct;
   the *result it is handed was invented*. This is the sneakiest shape, because
   `write_report` genuinely does derive `passed` from `result.passed`.
3. **The stale green.** A writer that derives its verdict honestly from ONE
   comparison while OTHER gates in the same file — mutation, orientation,
   saturation — failed. And, worse, a report left on disk by an earlier passing
   session that a later crashed / killed / failing session never removed. The file
   then attests to a state of the code that was never verified.

Shape 3 is why "the writer asserts `res.passed` first" is **not** sufficient. A
per-comparison verdict is not a session verdict.

### The mechanism (uniform, in `kyttar_verify/session_report.py`)

Every report goes through `write_session_report(block, payload)`, which does three
things in this order — the order is load-bearing:

1. **Unlink first.** Delete any existing report for that block *before the verdict
   is known*. From that moment absence is the state of the world unless the call
   completes. A crash, a kill, or a later failure all leave **no file**, and the
   dashboard reads absence as "not verified" — which is true.

   **The one documented limit, measured and gated:** unlink-first happens *inside*
   the writer, so under `-x` — where the session aborts at the first failure and the
   writer never runs — a green report from an *earlier* session survives. That is
   the boundary of what a single-process mechanism can promise, and it is exactly
   why the provenance audit treats a report as evidence only when its own suite has
   been re-run **to completion**. A full run (no `-x`) does clear it. See
   `test_dash_x_leaves_a_pre_existing_report_UNTOUCHED` — the limit is asserted, not
   assumed, so it cannot be quietly forgotten or quietly regress.
2. **Ask the test runner, not the module — scoped to the writer's OWN suite.**
   Read the accumulated per-test outcomes recorded by
   `verification/tests/conftest.py` on the running pytest `Config`, filtered to
   the calling test FILE. Any failure or error **in that file** means **no
   write**, and the writer itself FAILS,

   **UPDATED 2026-08-29 — this used to be session-wide, and that was
   destructive.** Combined with unlink-first, ONE failing gate anywhere deleted
   the evidence for every block whose writer sorted after it: measured at ~57 of
   118 reports lost in a single full-suite run, ~56 individual suite re-runs to
   recover, three times in one session. It also made the suite NON-IDEMPOTENT —
   two identical invocations reported 14 and 60 failures, because the count
   depended on how many reports happened to exist when the run started, which
   made every downstream diagnosis unreliable. Scoping to the caller's own file
   keeps the guarantee that matters (a report cannot claim a pass its OWN tests
   did not earn — all three defect shapes above remain impossible) and drops only
   the part that was never evidence about this block: a DIFFERENT block's
   failure. Gated by
   `test_report_provenance.py::test_failure_scope_is_the_writers_own_file_not_the_session`,
   proven to fail when the scoping is reverted.
   naming the offending gates so a report-less run is never mistaken for a skip.
3. **Stamp the provenance.** The written body carries `"provenance": "session"`,
   so an auditor can tell an artifact from a literal by inspection.

`verdict=False` records a **quarantine** — a block that does not work, whose suite
passes because it asserts the documented failure. The session gate still applies
in full, so `verdict` can only make a record *worse* than the session, never better.

### Why the record lives on the pytest `Config`

Not in a module-level global. A global is *exactly* the thing a crashed session
leaves stale, and the whole point is that a dead session must leave nothing usable
behind. `Config` is created per session, per process: it is invisible to a parallel
invocation (each worker sees only its own outcomes), it cannot survive the process
that made it, and if the conftest plugin is absent the record is simply missing —
in which case the writer **refuses**, rather than assuming success. There is
deliberately no "assume it passed" path.

### The two gates (`verification/tests/test_report_provenance.py`)

* **MECHANISM.** Runs a real writer in a child pytest session that also contains a
  synthetic FAILING test, and asserts NO file is produced and the writer fails.
  With a passing control (a writer that never writes would trivially satisfy "no
  file"), an unlink-first case (a pre-seeded green report must not survive), and
  `-x` / `-p no:randomly` / parallel cases. This is INV-4 applied to the writer:
  **a writer never shown to REFUSE certifies nothing.**
* **GUARD.** AST-scans `verification/tests/` for all three shapes and fails if a
  new one appears, with its own INV-4 teeth: each shape is deliberately
  reintroduced in a fixture tree and the guard is proven to fire on it, plus a
  negative control proving it stays silent on a correct writer.

### What this does NOT do

It changes only **whether and when a report file is written**. It asserts nothing
about any DUT and weakens no gate. If a suite was green before, it is green now
and its report says the same thing — with provenance attached.

### The general rule, for any evidence artifact

This is not really about JSON. Any file, dashboard row, or status string that
**claims** a verification happened must be produced by the verification, and must
be **absent** when the verification did not complete. A green default is a lie
waiting for a crash. If you cannot compute the verdict, emit nothing.


---

## INV-40 — A dominant block's free space has a SHAPE; widening it (CHIP_SCALE) can buy a through-channel the ≤8-across cap forbids

**Symptom:** a chain whose blocks fit comfortably (measured: 65 of 120 cells)
will not route, and is *always exactly one net short* — with WHICH net fails
rotating as blocks move. Every obvious lever measures as ruled out: not
capacity, not the hop ceiling (INV-36), not the size of the small blocks. Every
legal fold of the dominant block measures IDENTICAL free-space quality.

**Root cause — the free space is the wrong SHAPE, and the cap chose the shape.**
INV-9's ≤ 8-across convention exists so a bus can pass a block on either side,
which makes I/O placement forgiving. But it also bounds a large block's possible
bounding boxes very tightly: a 51-cell block has only THREE (7×8, 8×7, 8×8).
Within that set, a CLOSED-RING block's free space is all perimeter — a cycle
cannot jump a gap, so it can never enclose a free through-channel — and
perimeter fragments around an 8-wide body. Ten nets looking for a corridor find
none.

Going **wider than the cap** changes the shape, not the amount: a 10-wide block
on a 10-wide array leaves its free rows as *whole rows*, i.e. one contiguous
through-channel. Measured on `GRUCellBlock`: the 8×7 fold left five free rows
fragmented by the block's body and the chain stayed one net short across ~8200
layouts; the 10×6 wide-flat fold left six FULL-WIDTH free rows and the same
chain, with the same 65 block cells and the same ten nets, routed and built at
**102/120**.

**The mechanism — `CHIP_SCALE`, declared per class.** `KyttarBlock.CHIP_SCALE`
(with `CHIP_SCALE_ORIENTATIONS` and `layout_caps()`) already existed for the FFT
family. It is **never a global loosening**: a block that does not declare it
stays bound by the ordinary cap and the full 8-orientation D4 gate.

**The trade is mandatory, not optional.** The ≤8-across cap is what makes I/O
placement forgiving — with a free channel on each side, any edge will do. At
full width there is no way to reach the far side, so a chip-scale block **MUST
put its input and output on ONE edge**, and that edge must face the chip's
ports. State it in the fold's docstring and assert it in the block's suite
(`GRUCellBlock`: `fin` and `oout` three cells apart on the north edge, facing
the 10×12's two row-0 ports).

**Do not measure the wrong thing.** A wide fold is not necessarily cheaper for
its OWN port corridors, and judging it that way inverts the result. Measured:
the wide `GRUCellBlock` costs 58 cells (block + both corridors) at its best
anchor against 64 for the 8×7 — but the chain seats it 6 rows down, where it
costs 70, precisely to give the front end the port-side rows. **The figure that
settles a fold is the WHOLE CHAIN's, not the block's.** Quote a block's port
cost at its best anchor and document the trade separately.

**Orientation coverage must not fall through the gap.** A full-width fold cannot
rotate, so a chip-scale block is removed from the shared full-D4 sweep and gated
on its DECLARED `CHIP_SCALE_ORIENTATIONS` in its own suite (the FFT32 pattern) —
plus a `test_rotated_footprint_genuinely_does_not_fit` that DEMONSTRATES rather
than narrates why the other images are not shipped. Removing it from both places
leaves it gated NOWHERE while every suite stays green;
`verification/tests/test_chip_scale_blocks_are_gated_elsewhere.py` is the
coverage gate that makes that state impossible.

**Expect to pay in the SMALL blocks' route quality.** A full-width block confines
every other block to the remaining rows, so their corridors lose lanes and take
placement-forced wall detours. Measured on the classifier: `test_route_quality`
failed the new `.kyt` at +6 total excess, both detours traceable to the
confinement (one corridor rounds a 2×4 footprint, another drops to the single
free row between the front end and the wide block and climbs back). That is a
legitimate re-pin *with the explanation written down* — never a reason to loosen
`MAX_NET_EXCESS`.

**When to reach for this:** a block that is a large fraction of the array, whose
chain will not route, and whose free space you have measured to be fragmented
perimeter. Before fusing blocks to delete nets (a much larger change), ask
whether the dominant block's free space can be made contiguous instead.

**Applies to:** any block approaching the array's width. Related: INV-9 (the
convention this deliberately waives, per class), INV-23/`CHIP_SCALE_ORIENTATIONS`
(orientation), INV-37 (derive `is_face` constants from the fold — without which a
re-fold this large is a silent-garbage trap rather than a one-method change).

---

## INV-41 — A complex block port is TWO rails but ONE stored net; never identify a rail by its net alone

**Symptom (display-side).** A complex input port's two waveform traces carry the
**same label** — measured on `examples/fft_spectrum`, both `x16_in` rails read
`fft64.xi`. The user cannot tell the real rail from the imaginary one, and the
design reads as if it were driven by a real signal. The same collision was
present on every single-net complex input in the shipped examples
(`channel_selector`'s `floattocomplex.re/.im`, `css_transceiver`'s
`conjchirpmixer.xi/.xq`, `lms_equalizer`).

**Root cause.** GNU Radio collapses an I/Q pair into ONE complex port, so the
importer — and `AppController.add_logical_connection` — wires only the **I-half**
and **SYNTHESISES** the Q-half sibling (`grc_import._iq_sibling`). The project
therefore stores exactly **one** `Connection` for a port that physically carries
**two** tagged rails, landing on two consecutive registers of one cell (measured:
`FFT64Block` entry 12, hop 26, `data_addrs [1, 2]`). Any code that answers "what
is this tag?" by looking up the nets touching the port and, finding one,
returning its name, necessarily returns the SAME answer for both rails.

**The rule.** Identify a rail by the **register the word was addressed to**, not
by the net. The waveform tag for an input injection is `(hop, dest)`; resolve the
block's ports with its params (INV-6/11) via `catalog.resolved_io(...)` -> the
ordered input registers, pair the I-half with `_iq_sibling`, and match `dest`
against `in_regs[0]` / `in_regs[1]`. A port with no sibling (a real scalar input)
must keep its plain net label — the rule has to be a no-op there or every
single-rail example grows a phantom second rail.

**Why it matters beyond cosmetics.** "Both rails look like one" is ALSO the
symptom of a genuine data defect — the un-named-`stream_id` landing bug, where
I goes to register 0 and Q to register 1 while the block expects `[1, 2]`, so it
receives a **real** input whose conjugate-symmetric spectrum splits the tone into
two quarter-power peaks. A clean plot does not distinguish the two causes. So do
not reason about it: **READ THE RAILS.** Drive the built chip with
`chip.enable_trace()`, ingest into a `TraceModel`, and pull
`port_streams_by_tag()` — the same call the GUI plots. Measured on the honest
chain (N=64, amplitude 0.9 tone at bin 11): `xi = 29491, 13902, -16384, …`,
`xq = 0, 26009, 24521, …` — cosine and sine, bit-exact vs the reference, with
`xq[0] = sin(0) = 0` and `xi[0] = round(0.9*32768)` the cheapest check that the
rails are neither duplicated nor swapped.

**Gate it.** Two separate assertions, because only one of them was ever broken:
the rails carry **different** words each matching the right half of the stimulus
(the data claim), and the pane names them **differently** (the label claim). See
`verification/tests/test_fft_spectrum_example.py`
(`test_the_two_input_rails_carry_different_data`,
`test_waveform_labels_the_two_rails_distinguishably`, and the mutation suite that
rejects duplicated / swapped / empty / one-sample-delayed rails).

**Applies to:** any complex-input block placed from a `.grc` or via
`add_logical_connection` — i.e. every complex example. Related: INV-6/11 (resolve
ports WITH params), and the `fft_spectrum` lessons-log entries for the landing
bug this shares a symptom with.

---

## INV-42 — `output_words` is the chain's OUTPUT SEMANTICS, not a formatting preference; `auto` is right for BITS and wrong for VALUES

**Symptom.** A hosted example returns *almost* the right answer. Measured on
`examples/fft128_2p2s`: a 384-sample two-tone transform came back with **4
samples wrong** — the only 4 the reference has energy in — while the other 380
matched bit-exactly. On the display side the same chain draws a flat off-scale
line against a `-1..1` axis. Both the headless gate and an offline replay of the
server's own drive are BIT-EXACT throughout, so nothing points at the encoding.

**Root cause.** `kyttar_source`'s `output_words="auto"` ties **raw int16** output
to `complex_in`. That default encodes the **bit-packing receiver** convention —
a slicer's decoded bit lives in the word's LSB, and Q15 scaling would crush it.
A chain whose output is a **Q15 VALUE** (transform bins, equalized I/Q, CORDIC
magnitude/phase) is the opposite case and must set `output_words="q15"`.
`complex_in` describes the chain's INPUT; it cannot decide the OUTPUT's meaning.

**Why it hides — the part that generalizes. Zero is a FIXED POINT of the
aliasing.** Consumers apply the documented q15/32768 convention,
`round(w × 32768) & 0xFFFF`. A raw word aliases under it (`14746.0 -> 0x0000`,
`11469.0 -> 0x8000`) but `0.0 -> 0x0000` survives unchanged. So on any **sparse**
signal — a tone transform, a mostly-idle stream, a sparse constellation — the
mismatch is confined to exactly the samples that carry information, and the
result reads as "nearly right" rather than "obviously broken". The sparser the
signal, the smaller the visible damage and the more misleading the symptom.

**A `.grc` enum that matches no option is SILENTLY replaced by the default.**
Both FFT128 `.grc`s carried `output_words: 'False'` (a stale boolean from before
the enum existed) and `repeat: '''yes'''` (double-quoted, matching neither
option). Neither is a build error: GRC resolves each back to its default. So the
`.grc` text is NOT the authority — read the **generated Python**
(`kyttar.source(..., output_words="auto")`) to see what the flowgraph actually
runs with.

**Rules.**
1. If the chain's egress is a **value**, set `output_words="q15"` explicitly.
   Leave `auto` only for a bit/symbol-packing receiver, and say which in a
   comment — the default is silently wrong for the other half of the cases.
2. A generator's comment claiming a scale (`"at the q15/32768 scale"`) and its
   param must agree; when they disagree the comment usually records the intent
   and the param is the bug.
3. Verify at the **boundary**, not by inference. Instrument what the server
   RETURNED against what the client DECODED. A derived index pattern (periods,
   spacings) is a shadow of a root cause and can point confidently at the wrong
   layer — this fault was recorded for a cycle as a "sink/batch-session"
   defect on exactly such a reading.
4. An offline reproduction that does not carry the encoding flag proves nothing
   about the encoding. Three independent BIT-EXACT results (headless, server
   drive-shape offline at three event budgets) all skipped the `raw` flag.

**Gated by:** `verification/tests/test_examples_grc_userpath.py` —
`test_fft128_2p2s_shipped_grc_user_path` asserts bit-exactness of the recovered
stream against the whole-transform reference, plus non-vacuity (the
energy-bearing samples must be non-zero, so a dead chain cannot pass on the
zeros alone). Teeth: reverting the `.grc` to `"auto"` makes the gate fail.

**Applies to:** every hosted example whose chain emits VALUES rather than packed
bits. `fft_spectrum`, `cordic_polar`, `complex_math`, `lms_equalizer` and
`fm_transceiver` already set `"q15"`; the FFT128 examples were the outliers, and
the sibling two-die project this one was cloned from had no user-path gate at
all — a copied generator clones the fault, so fixing one sibling is not fixing
the class.
Related: INV-22 (a binding that resolves is not a binding that is correct).

---

## INV-45 — Multi-word (>16-bit) arithmetic: CARRYING a wide value costs more than COMPUTING on it

**The rule.** On this 16-bit ALU a value wider than 16 bits lives as a tuple of
16-bit registers, and a cell that must FORWARD that tuple pays `MOVE R0, Rw` +
`WRITE` = **2 instructions per word, per hop**. For a live set of `W` words that is
`2W + 1` instructions of pure transport before the cell computes anything, out of
the 31 usable words:

    W held words + 2W relay + 1 jump  =  3W + 1  words consumed
    body budget (data + state + instructions)  =  31 - (3W + 1)

At `W = 8` (four 32-bit values, the ChaCha20 quarter-round live set) that leaves
**6**. A 32-bit ADD (4) fits; a 32-bit XOR (4) fits; anything larger does not, and
must be reshaped until it splits into stages that each fit. This — not the ALU — is
what sizes a multi-word block: **ChaCha20QRBlock is 17 cells for 53 instructions of
arithmetic** — one hop of its 8-word frame costs 17 instructions, the arithmetic done
at that hop costs 3-4. Design against the transport ceiling first, then pick the
algorithm that fits it.

**The four primitives, measured (ChaCha20QRBlock, 2026-08-29).** These are counts
from built cell programs, not estimates. Reuse them; do not re-derive:

| 32-bit op on hi/lo halves | instructions | construction |
|---|---|---|
| `ADD` (mod 2^n, wrapping) | **4** | `ADD lo,lo / MOVE lo,R0 / ADC hi,hi / MOVE hi,R0` |
| `XOR` / `AND` / `OR` | **4** | two 16-bit ops + their parks |
| `ROTL(x, half-width)` | **0** | the hi/lo swap, folded into the relay (below) |
| `ROTL(x, n)`, n < half-width | **7** over 2 cells | 4 (`ROL` each half) + 3 (masked merge) |

**1. `ADC` is the carry — never synthesise it.** `MOVE` is flag-preserving (only ALU
ops touch the flags, guide §4.2/§4.8), so the park between the low `ADD` and the high
`ADC` does not disturb the carry. No `CMP`, no mask, no compare-against-operand trick.
`SBC` is the same story for subtraction. Extends to any width: N halves = N-1 `ADC`s.

**2. A rotate by exactly the half-width is FREE — zero instructions, not two MOVEs.**
`ROTL32(x, 16)` IS the hi/lo swap. The cheap implementation moves two registers; the
free one moves nothing and instead swaps **which register each relay `MOVE` reads**.
A stage that already re-reads its live set to forward it gets the rotate for nothing.
Any wide-value pipeline should place its half-width rotates on a relay boundary.

**3. Cross-half rotates: rotate each half, then TRADE the low bits.** The obvious
form needs both original halves alive while writing both results (11 instructions,
2 scratch — over budget). Use the 16-bit **rotate** (`ROL` = `SHL` with the `ROT`
bit) on each half independently. With `u = ROL16(hi, n)`, `v = ROL16(lo, n)`,
`M = (1 << n) - 1`:

    hi' = u ^ ((u ^ v) & M)          lo' = v ^ ((u ^ v) & M)

`ROL16(hi, n)` already contains `hi << n` in its high bits and `hi >> (16-n)` in its
low `n` bits — the two pieces the cross-half form assembles — so only the low `n`
bits need trading, via one shared `k` and two XORs. Splits into a 4-instruction cell
and a 3-instruction cell (5 written, 2 of which ARE the relay writes they replace),
both of which fit beside an 8-word relay.

**4. Wrapping, not saturating.** Multi-word integer arithmetic is exact mod 2^N; it
must NOT reuse the Q15 saturating idioms (INV-13). Gate it with operands that force
a carry out of the top bit AND with operands sitting on the Q15 rails
(`0x7FFFFFFF + 1`), which a saturating datapath would clamp.

**The budget trap that comes with it (INV-33's overlap half, sharpened).** A relay or
egress cell that is ONE word over budget assembles, loads, places and routes
cleanly, and produces a WRONG answer that looks like a routing fault — the resolver's
space guard compares only DATA against `base_addr`, never state or pinned inputs.
Measured: an 8-word egress cell (8 inputs + 24 instructions, `base_addr` = 7) put
R7/R8 on its first two instruction words and dropped the LEADING word of every burst
while the other seven stayed bit-exact. **Every multi-word block needs the static
gate** — for each cell, assert no data address, state register or pinned input
register is `>= 31 - instr_count` — paired with an INV-4 negative that re-inflates
the over-budget shape. When a cell is one word over, the fix is INV-33's
ACCUMULATOR DELIVERY: have the upstream stage write one word into the cell's **R0**
as its LAST write and make that word's `WRITE` the cell's FIRST instruction, which
recovers both an instruction and a register. It requires an ordering hook on the
upstream relay tail, because any later write would disturb the delivered R0.

**Gated by:** `verification/tests/test_chacha20_qr.py` — the RFC 8439 §2.1.1 and
§2.2.1 vectors on chip, 7 wrapping corners, and mutation gates that each rotate
constant, each add-as-xor, and the DROPPED CARRY are proven to fail; plus
`test_no_cell_overlaps_its_own_instructions` and its INV-4 negative.

**Applies to:** every block computing on values wider than 16 bits — ChaCha20
(quarter round and keystream), Poly1305 (five radix-2^26 limbs, each spanning two
registers), wide CRCs, any extended-precision accumulator. Related: INV-33 (the
register contract and the overlap half), INV-34 (shift counts are immediates), INV-13
(Q15 saturation — which multi-word arithmetic must NOT inherit).

## INV-46 — The LOCK-rotation rendezvous is generic over N; every constraint and DRC that serves it must be too, and the FACE BUDGET (N + 2) decides how far it goes

**The mechanism.** On this clockless array, N independent producers firing at
asynchronous times can be told apart ONLY by the physical channel they arrive on —
the FACE. A rendezvous cell uses the arbiter LOCK (`LOCK`/`LOCK_FACE`, CONFIG 4/3) to
accept from exactly ONE face at a time and ROTATES that lock across the N arms, so an
early or bursty producer is simply held by the arbiter until its turn. The family:
`DualFloatToComplexBlock` (N=2), `FeaturePairJoinBlock` (N=2), `TMRVoterBlock` (N=3).
The cold start is always BAKED into the boot CONFIG (`initial_lock_face`) — arming via
a JUMP is a race, because a word arriving before the arm-JUMP is accepted on an
unlocked face and mis-pairs, which is the exact failure the LOCK exists to prevent.

**Rule 1 — anything that serves the family must be generic over N.** The mechanism
generalises; the code serving it silently did not, in THREE separate places, and none
of the three announced itself — each produced a layout that built and routed cleanly
and then misbehaved for a reason that pointed somewhere else:

* `cpsat_placer` constrained only the FIRST TWO drivers (`d0, d1 = drvs[0], drvs[1]`)
  and skipped multi-cell blocks (`if len(pl.cells) != 1: continue`). The third arm was
  unconstrained and the whole block exempt; the symptom was the build DRC firing
  `dual_input_same_face` — a correct complaint about the wrong culprit.
* `bus_router` REUSED one broker for every net into a target cell (`_broker_abutting`)
  — which for a face-locking target IS the same-face bug. Measured: three arms
  funnelled through one broker, all arriving WEST. (The MAZE router already handled N
  correctly; the BUS router, which runs first and *succeeds*, did not — so the bad
  report was never escalated.)
* `build._apply_internal_feedback` hardcoded CONFIG 4 (LOCK) as the target of a
  backward `unlock` edge. A block whose interlock RE-POINTS a rotating face lock
  (CONFIG 3 = LOCK_FACE) had its authored write rewritten into a lock-CLEAR, which
  un-gates every face: it voted correctly for two samples and then desynced. Blocks
  now declare `UNLOCK_CFG_ADDR` (default 4, so existing blocks are byte-identical).

Corollary: a same-face verdict belongs IN the router-selection loop
(`controller._rendezvous_input_same_face`), not only in the build DRC — otherwise a
router that "succeeds" with a same-face landing returns `ok=True` and the failure
surfaces much later as an unexplained build error.

**Rule 2 — the FACE BUDGET.** A cell has FOUR faces. An N-arm rendezvous needs

    N (one per arm — the face IS the path identity)
  + 1 (forward into the block's datapath)
  + 1 (a serialize-LOCK release corridor coming back)
  = N + 2

* **N=2**: 4 — fits. And the shipped N=2 blocks are SINGLE-CELL, so they need neither
  a forward nor a release; the budget never came up.
* **N=3**: FIVE needed, four available. Everything about such a block's shape follows:
  the rendezvous MUST be a **LEAF of the fold** (exactly one in-block neighbour, so
  three faces stay free), the block is forced into a longitudinal chain rather than a
  compact square, and the serialize-LOCK release cannot have a corridor of its own —
  it must come back through the one abutting cell.
* **N ≥ 4**: not placeable as a single rendezvous on a 4-face cell. Build a TREE of
  N=2/N=3 rendezvous blocks instead.

A compact 2×2 fold gives EVERY cell two in-block neighbours and therefore cannot host
an N=3 rendezvous at all: the maze router reports *"no free DISTINCT-face broker for a
face-locking block's input"* and the chain does not route.

**Rule 3 — the rotation has N+1 stops, not N (the INV-19 corollary).** Re-locking
straight back to arm 0 at the end of the last arm's entry is correct per-sample and
DEADLOCKS under load: it re-admits the next sample's first arm the instant the current
group is dispatched, groups pile into the downstream chain, and the sim reports an
explicit `Deadlock` after ONE packet. The last arm's entry must instead lock to the
INTERNAL FORWARD face — which no external arm arrives on, so all N are barred — and a
downstream cell re-points `LOCK_FACE` at arm 0 via a backward `WRITE.CFG` once the
group has cleared. How deep that release can sit is decided by Rule 2: at N=3 it can
only ride the ONE cell abutting the rendezvous, which bounds the block to one group in
flight. Deeper release points were built and measured, all blocked — a `WRITE.CFG`
transiting a live datapath cell is re-forwarded on that cell's committed face and lands
on a real entry (spurious packet); a dedicated `transit_*` lane needs a face that does
not exist; a backward JUMP into a relay entry is rewritten by the exit-default.

**Rule 4 — probe the layout, or ship a flake.** `auto_pnr` is a CP-SAT search and is
not deterministic. Measured on the N=3 voter: ~4% of layouts that route, build, AND
present N distinct input landings still mis-deliver an arm. Across the ~20 chain
builds in one suite run that became a ~50% per-run failure, spread across whichever
gate happened to draw a bad layout — indistinguishable from a real block bug. Any
harness for a face-locking block must SMOKE each candidate layout and move to the next
anchor if it fails. Two details are load-bearing: (a) a healthy group is NOT a
sufficient probe — a mis-delivered arm still votes "all agree" when every arm carries
the same value, so fault ONE ARM AT A TIME; (b) probe on a THROWAWAY chip instance,
because driving a group advances the lock rotation and latches arm state, so smoking
the chip a gate is about to use leaks the probe's values into that gate's first result.

**Rule 4a — the probe's own failure mode: a broken block collapses the suite into
SKIPS, not failures.** The probing loop of Rule 4 ends in `pytest.skip` when no anchor
survives — correct for a flaky CP-SAT run, and dangerous for a genuinely broken block,
which fails the probe at EVERY anchor. Measured on the N=2 `XorJoinBlock`: corrupting
its `XOR` to an `AND` turned **35 tests into skips and only 6 into failures**, a suite
that still reads "passed" at a glance. So every face-locking block's suite needs ONE
gate that FAILS rather than skips when the probing path cannot produce a working chain
(`test_the_probing_harness_actually_routes_this_block`). The pattern this family
requires is what creates the hazard, so the guard belongs with the pattern.

**Rule 5 — mutate the BLOCK, not a model of it; at N=2 both operands alias R0.** The
family declares BOTH input ports on the SAME register (R0), because each operand
arrives on its own face-gated trigger. A consequence that has already produced one
worthless mutation test: in a `MOVE R0, R{in:b}` / `XOR` pair the MOVE assembles to
`MOVE R0, R0`, so REORDERING those two lines is a NO-OP — the mutant builds, runs, and
emits the correct answer. Any "emit before latching the second operand" mutation
written that way certifies nothing. Mutate the ALU op, the latch, or the re-lock
instead, corrupt the REAL block, and REBUILD ON CHIP (measured for `XorJoinBlock`:
drop the XOR → forwards `b`; AND instead of XOR → `a & b`; drop the `a` latch →
forwards `b`; drop the re-lock → 2 words then desync). A model of a mutation is not a
mutation.

**Gate:** `verification/tests/test_tmr_voter.py` (N=3, 54 tests: all 6 arrival
permutations, 8 D4 orientations, the saturated gate, the depth guard, and INV-4
mutations) + `test_feature_pair_join.py` / `test_dual_float_to_complex.py` /
`test_xor_join.py` (N=2; the last carries the Rule 4a anti-skip guard and the Rule 5
substrate mutations, and its INV-19 saturated gate PASSES — at N=2 the whole
rendezvous is one cell, so the LOCK alone is the serialization).
Related: INV-19/20 (the serialize-LOCK idiom this extends), INV-23 (every face
constant is `is_face` so it D4-transforms), INV-39 (a multi-entry dispatch cell's
entries must all be jumped).

---

## INV-48 — A panel block is auto-placed only for its NAMED ROLE cells; and a word is forwarded on each TRANSIT CELL'S OWN face, not the sender's

**Symptom:** a panel-backed block whose cell programs are individually correct — every
cell inside its 32-word budget, every hand-off verified on the real chip through the
real panel — cannot be placed. `auto_pnr` does not report a routing failure; it raises
a `PlacementError` (or a `TypeError` on a param the template assumes) from
`engine/panel_pnr.py` before any router runs.

**Root cause A — the panel path BYPASSES the generic placer/router.**
`AppController.auto_pnr` branches on `panel_backed_blocks(...)` and, for ANY design
containing one, calls `apply_panel_template(...)` and then only `auto_route_all` for
the leftover block→block nets. It never reaches the CP-SAT pack / perturbation sweep
that every non-panel design uses. So a panel block gets exactly the placement the
template hard-codes, and nothing else.

**Root cause B — the templates encode a SHAPE, not a size.** There are two, and each
pins a fixed set of cells:

* the **TX shape** (`apply_panel_template`) pins **2**: the embedded controller at the
  `x1_out` port cell and the push-read consumer at `(0,1)`;
* the **RX shape** (`_apply_rx_template`, taken when `panel_requirements()` declares an
  `input_cell` distinct from the `controller_cell`) pins **4**: controller, kicker,
  input, consumer — and additionally writes Varicode-decoder-specific params
  (`read_addr_hop`, `read_dest`, `read_entry`) straight into `blk.params`, so a block
  that does not accept them fails with a `TypeError` from its own constructor.

**THERE IS NO CELL-COUNT CAP. An earlier revision of this invariant claimed one and
it was FALSE** — corrected 2026-08-29 after an audit. The false text read "every
panel-backed block shipped to date is 2 or 3 cells … a panel-backed block of 5+
cells is not placeable today." It was refuted by a block that had already shipped:
**`GolayDecoderBlock` is SEVEN cells, panel-backed, `status: done`, BER 0**
(`verification/STATUS.md`; `panel_requirements()` → `controller_cell 6,
input_cell 0, return_cell 5`), and it landed 2026-08-16, **thirteen days before**
the claim was written. `panel_pnr.py` contains **no `cell_count` check of any
kind** — its only count limit is `len(backed) > 2` at `panel_pnr.py:71`, which
caps how many *blocks* may be panel-backed in one design (the chip has one
`x1_out`/`x1_in` pair), and says nothing about how large any of them is.

**What is actually true is narrower and fixable:** each template places only the
cells NAMED as roles in `panel_requirements()` — 2 for the TX shape, 3-4 for the
RX shape. A block whose `cell_count` exceeds its named-role count has **no
position assigned** for the extra cells: they are silently absent from the
resulting `Placement`, and the failure surfaces later as something that looks
unrelated. Golay (7 cells, 3 named roles) is placeable **by hand** and verified on
a real chip with a real `SramPanelDevice`; what it cannot do is go through
`auto_pnr`. That is a **role-coverage gap in the template**, not a property of the
substrate, and the fix is to extend the template (or have a block declare every
cell as a role).

**Root cause C — the durable, block-independent half: A WORD IS FORWARDED ON EACH
TRANSIT CELL'S OWN FACE, NOT THE SENDER'S.** This is the rule everything else in
this section follows from, and it is the one that is easy to get wrong.

A word leaves its source on the **source cell's** face. Every cell it then arrives
at forwards it on **that cell's own** resting face. So from any cell there is
exactly ONE outgoing walk through the face field, and all of that cell's internal
targets must lie along it, in the order the walk visits them.

**Measured, from a simkyt trace** (2026-08-29), not inferred:

```
cell 117 exec_tick pc=23 word=0x6348 result=external_write
cell 117 output_ready face=W ... neighbor_id=116     <- router sends WEST
cell 116 instr_arrival face=E word=0x6348 hop_cnt=27
         action=forward exit_face=E                  <- emit forwards EAST
cell 116 output_ready face=E ... neighbor_id=117     <- straight back
```

The straight-line "ray" model — the word keeps the sender's direction for the whole
corridor — is **FALSE**, and believing it produces a layout that places clean,
builds clean, passes DRC, and then **ping-pongs at run time**. That is exactly how
the LZ4 decoder got a 7×1 row whose every edge "checks out" on paper and hangs on
the chip.

**FAN-OUT IS NOT A WALL.** Checked against the shipped library before writing this:

| block | cells | max fan-out | backward edges | status |
|---|---|---|---|---|
| `LMSEqualizerBlock` | 14 | **6** | 5 | done |
| `FFT64Block` | 84 | 4 | **6** | done |
| `FFT32Block` | 60 | 4 | 5 | done |
| `LZ4DecoderBlock` | 7 | 4 | 3 | *not done* |

Two shipped blocks are strictly worse than LZ4 on both counts. Whatever stops LZ4,
it is not the size of its graph.

**The two idioms that make a many-target cell work**, both shipped and verified:

1. **Targets consecutive along one walk.** `LMSEqualizerBlock`'s `bcast` sits at
   (7,1) facing west and its six targets are the six cells west of it, so one walk
   delivers all six. This is what a serpentine fold is *for*.
2. **An in-program FACE FLIP.** Declare `DataWord(..., is_face=True)` and emit
   `MOVE [FACE], R{data:face_x}` … WRITEs … `MOVE [FACE], R{data:face_y}` to point
   the cell somewhere else for a burst and then restore it. See `LMSEqualizerBlock`
   `f0`/`bcast` and `MMTimingRecoveryBlock`. **Cost: 2 instructions + 1 data word
   per extra direction** — so a cell's spare words are what bound how many
   directions it can serve.

**The LZ4 decoder's actual, measured blocker** is a BUDGET one, not a topology one
— and it is now **one word**, refined 2026-08-29 by a pass that measured the
template's real constraints rather than assuming them.

**UPDATED — the binding cell is the EMIT cell's FACE COUNT, not the token cell.**
An earlier version of this paragraph said the `token` cell's 1 free word was
co-binding. With the template's real rules modelled (below), an exhaustive search
over cols 5-9 × rows 10-11 gives **24 feasible placements, and ALL 24 require the
emit cell to serve THREE directions** (panel east, egress, gohead) = 6 words.

**The `set_addr` removal is CORRECT and was PROVEN on silicon** (not merely
proposed): the read and write paths do **not** share a counter — `write` drives
`wraddr`, `lookup`/`read` drive `rdaddr`, and each re-writes the panel's single R5
address latch from its own counter before its own trigger. Verified byte-exact on
a real chip + real `SramPanelDevice` across literal runs, a match boundary, an
overlapping match (`offset=3, len=11`), an `offset==1` byte run, back-to-back
matches, and match→literal, with `wpos == wraddr` at the end. It frees exactly 3
words (emit 2 → 5). *An earlier attempt saw this "break" the panel tests; that was
a HARNESS artifact — `_panel_match_run` presets `wpos=8` mid-stream while `wraddr`
boots at 0, so with `set_addr` gone the harness must seed both.*

**So the shortfall is 5 words available against 6 needed — exactly ONE word.** No
legitimate source was found: `EAST`'s face code is numerically 1, equal to the
`one` constant, but sharing them breaks orientation rotation (`is_face` words are
rewritten by the placer); zeroing `mat` by self-subtract trades a data word for an
instruction, net zero. **The untaken lever is to SPLIT THE EMIT CELL IN TWO**
(INV-46's "prefer more cells doing less"), which changes the block shape and needs
its own silicon pass.

**Two template rules this pass discovered, which any panel block must respect:**
* **The egress cell is NOT transparent.** A blank cell on the emit cell's resting
  walk becomes the egress cell and is turned toward the output corridor, which
  **deflects the panel words** — measured: correct hops, zero panel writes.
* **The egress column is a full-height wall** for horizontal walks.

**Reach of this claim:** the forwarding rule is HARDWARE and permanent. The
three-face requirement is exhaustive over **three** independent windows with the
CURRENT cell programs — widening west and adding a row both fail to help:

| window | feasible placements | emit-cell faces needed |
|---|---|---|
| cols 5-9 × rows 10-11 | 24 | **3 in all 24** |
| cols 4-9 × rows 10-11 | 20 | **3 in all 20** |
| cols 6-9 × rows 9-11 | 20 | **3 in all 20** |

Best cost is unchanged at 9 flip-words total, with the egress invariably at
(8,10) reached northward and the emit cell invariably at (8,11) abutting the
controller. Still not a claim about ALL layouts — but it is no longer an artifact
of one narrow window. The budget arithmetic
changes the moment the programs do. See also INV-50: a router distance bug was
found while measuring this, and it is still open.

**The rule / what to do about it.**

1. **Declare every cell as a named role in `panel_requirements()`, or expect to hand-place.**
   Cell COUNT is not the test — `GolayDecoderBlock` runs 7 cells on real silicon.
   The test is whether the template has a position for each cell, i.e. whether the
   cell is a named role. Sizing the FSM up front is still worth doing (budget is
   `31 - (data words + state vars + input registers)`, at best 28, realistically
   25-26), but a large count is not by itself a wall.
2. **Keep at most ONE backward internal edge per cell.** `build._apply_internal_feedback`
   restores the **highest-address** `JUMP` per cell for a backward internal jump; a
   second backward jump in the same cell is silently lost. Prefer a DATA-only backward
   `WRITE` (no trigger) wherever the target is re-entered by the normal stream anyway —
   that is free, and it is what let the LZ4 router's phase register be updated from two
   different handler cells.
3. **Keep a hot loop LOCAL.** Moving the LZ4 match-run counter out of the length-parsing
   cell and INTO the emit cell turned a cross-cell back-trigger into a `BR.NZ`, removed
   an entire register (`inmatch` — the counter doubles as the literal/match
   discriminator via the sign of one shared `SUB`), and removed one whole edge from the
   layout problem. Loop back-edges are the expensive ones.
4. **CHECK A LAYOUT AGAINST THE WALK, NOT A RAY.** Simulate the forwarding rule
   above before believing a layout: from each cell, follow the faces and confirm
   every target appears on that walk before the controller does. Placement, build
   and DRC will all pass a layout that fails this — the symptom is a HANG, not an
   error. Budget the face flips at the same time (3 words each): a cell with 1 free
   word cannot serve a second direction, and that, not the graph, is usually what
   binds.
5. **BUILD-PATH BUGS THIS SHAPE OF BLOCK EXPOSES** (all three found and fixed
   2026-08-29 while placing the LZ4 decoder; each was silent):
   * `router._find_output_target` matched `internal_connections` first and returned
     the target cell's DEFAULT entry, so a port carrying BOTH a WRITE and a JUMP
     fired the wrong entry. A cell driving several entries of one companion (the
     LZ4 emit cell → the controller's `set_addr`/`write`/`lookup`) collapsed them
     all onto the first. Now the entry is taken from `internal_jumps` for the same
     port.
   * `router._fixup_write_instructions`'s sink fallback rewrote **every** WRITE and
     JUMP in the exit cell to the output-port hop. Correct for a plain sink;
     destroys a cell that also speaks a protocol. Now honours `RAW_OUTPUT_HOPS`
     (new `BlockDefinition.raw_output_hops`, set by the build).
   * `build._apply_output_port_routes` had no `RAW_OUTPUT_HOPS` guard, unlike its
     sibling `_apply_routes`. Same failure, different pass.
   * `build._patch_one_handoff` identifies the WRITE to patch by DESTINATION
     REGISTER alone and takes the lowest-addressed match. When one cell drives two
     different cells' registers that share a NUMBER, the backward edge's hop lands
     on the forward WRITE. Worked around by pinning the colliding `StateVar` to a
     distinct register; the pass itself is still ambiguous and would be worth
     making port-aware.

**Ground truth:** `verification/tests/test_lz4_decoder.py`. The block's DSP
correctness is NOT in doubt — the token nibble split, the little-endian offset
assembly, a whole match copy, and the `offset == 1` byte run all run on a real chip
through a real `SramPanelDevice`, with each match byte push-read at the **computed**
address `wpos - off`. Placement, build and DRC are green through
`_apply_self_contained_template`. What is NOT yet true is end-to-end operation: see
`test_internal_edges_that_do_not_route_are_exactly_the_known_gap` and
`test_emit_cell_cannot_reach_the_controller_in_a_placed_layout`, which pin the
remaining gap and its arithmetic, and the skipped
`test_auto_placed_design_decodes_on_chip`, which is written and ready. Related:
INV-29 (why it needs the panel), INV-31 (the panel contract), INV-32 (port-cell
transit), INV-33 (the register-allocation contract the cell budgets come from),
INV-36 (the 31-hop cap).

## INV-47 — When a wide value cannot be CARRIED, make it RESIDENT and turn the index into ADDRESS ARITHMETIC — the panel is the only computed-destination path

> ### ⚠️ CORRECTED 2026-08-29 — the ceiling below is REAL but was OVER-SCOPED
>
> The original text read: *"Solving `3W + 1 <= 31` gives a hard ceiling: **a live
> set wider than 10 sixteen-bit words cannot transit a cell at all.**"* That
> sentence is **FALSE as written**, and it was derived by algebra over a formula
> — never measured. It is left here because it was copied into a commit message,
> a factory report and a manifest note, and a reader needs to recognise it.
>
> **What is true:** `3W + 1` prices exactly ONE construction — a relay that
> HOLDS all `W` words in its own registers and forwards each with
> `MOVE R0, Rw` + `WRITE`. For that shape the bound is real, and it is
> **`W <= 9`, not `W <= 10`** — at `W = 10` the inequality is satisfied with
> equality, which leaves zero words and still collides with the instructions.
>
> **What is false:** the generalisation to "a cell". A **STREAMING** relay (one
> word in, the same word straight out, holding nothing) costs a *constant* 3
> instructions at any frame width, so there is nothing to solve for. **Measured
> on the real placed+routed chip: frames of 8/10/12/16/24/32/64/128 words cross
> 1 and 3 cells bit-exact.** W=32 is the full ChaCha20 state — the exact case
> this invariant declared impossible.
>
> **Layer:** neither hardware nor toolchain. It is a property of a CODE SHAPE,
> so it is avoidable by writing a different relay.
>
> **Reach:** measured on the transit fixture at 1 and 3 cells deep, widths 8-128.
>
> **Gated by:** `verification/tests/test_wide_transit_ceiling.py` (38 tests) —
> which asserts BOTH halves: that wide frames do transit, and that the
> hold-and-forward shape really does overrun at `W >= 10`.
>
> **Consequence for this invariant's advice:** "make it resident" is still
> often right, but it is a *cost* argument, not a *possibility* argument, and
> the panel is NOT the only option. See the correction to the second half below.

**The rule.** INV-45 prices carrying a `W`-word live set at `3W + 1` of a cell's
31 usable words. Solving `3W + 1 <= 31` gives a hard ceiling: **a live set wider
than 10 sixteen-bit words cannot transit a cell at all.** Past that ceiling the
answer is not a bigger fold or a cleverer relay — it is to stop moving the value.
Put it in an **SRAM panel** (INV-31) and move *addresses* instead. Addresses are
one word wide, so the transport cost collapses from `3W + 1` to `~3`, and the
expensive compute block becomes a small REUSED engine the data is streamed
through rather than a structure replicated per iteration.

**The second half, which is the part that is easy to miss.** Making the value
resident also solves a problem the substrate otherwise cannot solve at all. A
`WRITE`'s `HOP_CNT` and `DEST` are **instruction fields** (guide §4), and there
is no cross-cell register addressing — so **a cell cannot compute where to send a
word**. Any algorithm whose dataflow is data-dependent (a permutation, a
gather/scatter, an indexed shuffle) is therefore un-expressible as ROUTING. The
panel is the one place on this substrate where a destination is a DATA value: the
address register R5 and the push-read descriptors R3/R4 are all written, not
encoded. **A data-dependent dataflow must be re-expressed as panel addressing, or
it does not fit.**

> ### ⚠️ NARROWED 2026-08-29 — "the panel is the ONLY computed-destination path" is too strong
>
> The `HOP_CNT`/`DEST` half is **correct and permanent** (hardware/ISA): they
> are instruction bits `[9:5]`/`[4:0]`, so a cell genuinely cannot compute where
> to send a word. But three other data-dependent mechanisms exist, all used by
> shipped blocks, and a builder who reads "panel or nothing" will reach for the
> panel when it is not needed:
>
> 1. **`FACE` is runtime-writable** (CONFIG **1**, read/write; guide §3). A cell
>    can compute its output *direction* mid-program (`MOVE [FACE], Rn`), and
>    `DataWord(is_face=True)` exists so the placer D4-transforms the constant.
>    So *direction* is data-dependent even though hop count and destination are
>    not. Shipped: `costas_loop_block`, `agc_cc_block`, `freq_xlating_fir_block`,
>    `iq_upconvert_block`.
> 2. **`LOAD [Rn]` is a computed in-cell table lookup** — `R0 = mem[mem[Rn] &
>    0x1F]`, up to 32 entries (~21 practical). A data-dependent *word choice*
>    inside a cell costs one register, no panel. Shipped: `hamming_decoder`,
>    `fsk4_symbol_mapper`, `dot_product_mac`, `golay_decoder`.
> 3. **A fixed schedule is not a computed dataflow at all.** Check whether the
>    "permutation" actually varies at runtime before concluding it needs a
>    computed destination — see the worked case below, where it does not.
>
> **Reach:** the three mechanisms are each demonstrated by shipped, verified
> blocks; items 1 and 2 were additionally re-measured on chip 2026-08-29.

**Worked case (ChaCha20 keystream, measured 2026-08-29).** 16 x 32-bit state = 32
sixteen-bit words; relaying it through one cell costs `3*32 + 1 = 97` words of a
31-word cell — over 3x the whole cell. The round permutation (column vs diagonal)
selects which four state words feed each quarter round, i.e. exactly the
data-dependent dataflow above. Resident-in-panel fixes both, and the schedule the
RFC states as eight literal index quadruples collapses to one closed form:

    index(k) = 4*k + ((j + k*shift) & 3)        shift = 1 if diagonal else 0

Three instructions (`SHL #2` / `ADD` / `AND #3`), no table. **Proven on the real
placed+routed chip:** a built `gather` cell emitted the exact RFC panel-address
sequence for all 8 quadruples, both halves, including the wrap-around diagonals.

> ### ⚠️ CORRECTED 2026-08-29 — the ChaCha20 permutation is NOT data-dependent
>
> The claim above that the round permutation is *"exactly the data-dependent
> dataflow"* is **wrong**, and it is the reason this block was routed toward the
> panel. Measured facts:
>
> * All **80** quarter-round invocations are **ten identical repeats of a fixed
>   8-step cycle**: only 8 distinct index quadruples exist in the whole cipher,
>   and `schedule[i] == cycle[i % 8]` for every `i`. Nothing varies at runtime.
>   The schedule is a **constant**, so it is AUTHORED, never computed.
> * Better: **every quadruple takes exactly one word from each of the four rows**
>   `{0-3} {4-7} {8-11} {12-15}` — that is the defining property of the ChaCha
>   schedule (it is why each half round partitions the state). So if row `k`
>   lives in its own cell, the `4*k` term of the closed form is *which row*,
>   already resolved by WHICH CELL is addressed. All that remains is the
>   within-row selector `(j + k*shift) & 3` — a **2-bit number**, which is a
>   `LOAD [Rn]` index, not a destination.
>
> **Consequence:** the 32-word state can live in **cell registers** across eight
> lane cells, with no panel, no computed destination, and no gather/scatter
> protocol. Measured on the real placed+routed chip: a lane cell reads the
> selected word (`LOAD`-indirect) and writes a result back into the same slot
> (4-way `CMP`/`BR`, since the ISA has **no `STORE [Rn]`**) correctly for all 32
> (row, half, selector) combinations, carrying the real RFC 8439 §2.3.2 state;
> the sequencer emits all 80 invocations of the RFC schedule exactly; and the
> shipped 17-cell `ChaCha20QRBlock` sustains all 80 sequential invocations
> bit-exact, reproducing the §2.3.2 output state after the add-back.
>
> **The general lesson, which is the transferable part:** before concluding a
> dataflow needs a computed destination, check whether the schedule actually
> VARIES. A permutation stated as a literal table in a spec is usually a
> constant, and a constant is wiring.

**How to build it (the three rules that cost real time to learn).**

1. **Drive the panel from an AUTHORED cell when the addresses are computed.**
   `SramControllerBlock` carries ONE fixed descriptor pair plus its own
   auto-incrementing address — right for streaming a table, wrong for a
   gather/scatter at N computed addresses. Author the cell and emit the protocol
   directly (`WRITE @ph,5` address / `WRITE @ph,2` payload / `JUMP @ph,0` commit;
   `JUMP @ph,1` to read). This is `CWKeyerBlock`'s fetch-cell pattern.
2. **Keep the address/payload/commit triple in ONE cell.** The panel commits
   whatever is in R2 to whatever is in R5. Split the address and the payload
   across two cells and they are a RECONVERGENT FAN-IN (INV-20) whose arms can
   interleave under load and silently store a word at the WRONG ADDRESS. One
   cell's instruction stream is already ordered — no lock needed, and none
   available (the panel has no arbiter to rotate).
3. **The READ direction is cheap if the consumer serialises.** One fixed
   descriptor pair suffices when the destination shifts arriving words into a
   frame itself (the quarter round's `in0`/`in1` collectors do exactly this), so
   N reads need N addresses but only ONE descriptor preload.

**The budget trap that comes with it — carry the static gate from commit one.**
Building this shape put a cell over the 31-word budget **four separate times**,
and every time the design still ASSEMBLED, PLACED, ROUTED AND BUILT CLEAN while
silently overlaying its own instruction words (INV-45's trap, INV-33's overlap
half). Assert per cell that no pinned input register, state var or data address
is `>= 31 - instruction_count`, with an INV-4 negative that re-inflates the
over-budget shape. Note the trade direction, too: converting a stored constant
into an instruction (`SUB Rx, Rx` for a zero) frees a register but costs
instructions, and on an already-tight cell that makes the overrun WORSE.

**And one wiring rule this shape trips over.** `engine/catalog.py`'s
`resolved_io` selects the landing cell as **the first cell that declares
inputs** — so the ORDER of `build_cell_programs` decides where the block's
external trigger arrives. An iterative block must put its SEQUENCER first;
ordering a mid-loop stage first starts the loop with its schedule registers
uninitialised, and (again) the design builds and routes clean and emits nothing.

**Gated by:** `verification/tests/test_chacha20_keystream_golden.py` (the closed
form vs the RFC's literal quadruples, the half-round partition property, and the
INV-4 negatives: wrong round count, missing final addition, stuck counter,
swapped permutation, wrong diagonal stride).

**Applies to:** any block whose working set exceeds the ~10-word transit ceiling
or whose dataflow is data-dependent — ChaCha20 (keystream), Poly1305, wide
interleavers, LZ4's history window, sort/permutation networks, any indexed
gather/scatter. Related: INV-45 (the transport ceiling this starts from), INV-31
(the panel + its protocol), INV-20 (the fan-in hazard rule 2 avoids), INV-33 (the
register/overlap contract), INV-29 (the table-heavy case).

## INV-51 — A closed RING traps its interior; the layout dicts must PAIR BY POSITION; and a "gap" is a dead end for an internal WRITE

**Why this exists.** Three separate mechanisms, all discovered while folding one
block, all of which produce a design that **places, routes, builds and passes
DRC and then does the wrong thing in silence**. None of them is reported as an
error. Two are hardware/ISA-permanent, one is a toolchain contract.

---

### 1. A CLOSED RING TRAPS ITS INTERIOR. (hardware/ISA — permanent)

A word is forwarded on each **transit cell's own** face (INV-48 root cause C).
So if a block is folded as a closed loop — every cell facing its successor
around a rectangle perimeter — then a word emitted from a cell *inside* that
loop, **in any of the four directions**, arrives at a ring cell and from then on
follows the ring. Forever. There is no walk from the inside to the outside.

**Measured**, not derived: on a 26-cell ring around a 10×5 rectangle, an
interior cell facing east reaches the ring's east column, whose own face
continues *along* the ring; the same holds for all four directions and all
rotations of the ring.

**Consequences, in the order they bite:**

* A block's **egress cannot be an interior cell** — its face-neighbour is where
  the routing corridor starts, and that neighbour is always another block cell.
* It cannot be a plain **ring** cell either, because a ring cell's face is
  load-bearing: the datapath frame walks *through* it.
* A **corner** cell of a ring is doubly stuck: its only interior neighbour is
  its own ring predecessor, so it can never turn inward at all. This is what
  made an interior control cell unplaceable at every one of 26 rotations.

**What to do:** if any cell of the fold must reach the block's edge, **do not
fold as a closed ring — fold as a serpentine**, which has free ends and free
edges. A ring is only right when the block is entirely self-contained. Note this
is a stronger statement than layout_rules §3's "a closed-ring block can never
enclose a channel": the problem is not just that the ring encloses no channel,
it is that the ring is a one-way trap for everything inside it.

### 2. POSITIONAL PAIRING: the two dicts must ITERATE in the same order. (TOOLCHAIN)

`build_cell_programs()` returns `{cell_id: CellProgram}` and `default_layout()`
returns `{cell_id: (dx, dy, face)}`. The router (`placement/router.py`) and the
build walk **the programs and the placed cells in lockstep BY POSITION** —
`for cell_pos, (cell_idx, cell_prog) in enumerate(block_def.cell_programs.items())`
against `pb.cells[cell_pos]`.

Both dicts are keyed by cell id, which is exactly why this is dangerous: **the
ids hide the mismatch.** A layout whose insertion order differs from the
programs' silently pairs each program with the wrong cell. The design places,
routes, builds and DRCs clean, and **whole cells come out with empty memory**.

**Measured symptom:** a 40-cell block that built green and emitted nothing; the
state cells read `0x0000` in the bitstream while the resolver, called directly,
produced the correct seeded memory for the same program.

**The rule:** end `default_layout()` by reindexing against the programs —

```python
order = list(self.build_cell_programs().keys())
assert set(order) == set(lay)
return {cid: lay[cid] for cid in order}
```

and assert it in the block's test suite. `layer`: TOOLCHAIN — the pairing could
be made id-keyed in `router.py` and `build.py`, and should be.

### 3. A faced, PROGRAMLESS cell DOES forward — but nothing gives it that face inside a block. (hardware + toolchain)

**Measured on the real chip:** a `WRITE` transits cells that carry a `fwd_face`
and **no program at all**, at distances 2, 3, 4 and 6. Transit needs no program.

**But** the build sets `fwd_face` on a non-block cell only where a **route**
claims it (`build._apply_routes` walks each routed net's waypoints). So an
unoccupied position *inside a block's own footprint* has no face, and is
therefore a **dead end for a block-internal `WRITE`**.

Both halves matter and they pull in opposite directions:

* do **not** assume a gap breaks a chip-level route — it does not;
* do **not** assume a gap carries a block-internal edge — it does not.

If several of a block's cells must share one walk (an N-way broadcast, or N
sources converging on one sink), the walk has to be **paved with real cells**.
Cheap: a one-word pass-through is 3 instructions.

---

**SAY WHICH LAYER.** (1) and (3)'s first half are hardware/ISA and permanent.
(2) is a TOOLCHAIN contract in `placement/router.py` + `engine/build.py` and is
fixable there. (3)'s second half is a toolchain behaviour in
`build._apply_routes`.

**REACH.** (1) measured on one 26-cell ring over all 26 rotations and four
emit directions. (2) measured on one 40-cell block, but the mechanism is in the
shared router/build path and applies to every multi-cell block. (3) measured at
four distances on a single row.

**Gated by:** `verification/tests/test_chacha20_fixed_tap_ring.py` — the
positional-pairing contract, the per-cell budget gate with an INV-4 negative
that re-inflates a known-bad shape, and the on-chip transit measurement.

**Related:** INV-48 (the forwarding rule all of this follows from), INV-33 (the
register/overlap contract and the positional-pairing clause this sharpens),
INV-40 and layout_rules §3 (fold for the shape of the free space), INV-49 (the
recirculation this fold was built to carry).

## INV-49 — A datapath can be REUSED for N sequential passes; check whether a "permutation" is a CONSTANT before paying for a computed destination

**Why this exists.** Two blocks in this campaign were quarantined against walls
that a measurement would have removed, and both quarantines were algebra over a
formula rather than something anyone ran. This invariant records the two
mechanisms that dissolve the usual "it does not fit" verdict for an iterative
block, and both are gated.

**1. RECIRCULATION — one datapath, N sequential passes.** A block whose unrolled
form does not fit does not have to be unrolled. A backward `JUMP` that re-enters
a cell **mid-program at a named entry**, with the loop counter held in that
cell's state, runs the same datapath again with its registers intact.

Measured on the real placed+routed chip at **1, 2, 4, 8, 10, 20 and 80 passes**,
exact every time. 80 is the number that matters: ChaCha20's 80 quarter-round
invocations price out at `17 cells x 80 = 1360` against a 120-cell array, and
reuse is what makes the cipher fit at all.

Three rules the shape must respect:

* **At most ONE backward `JUMP` per cell.** `build._apply_internal_feedback`
  restores the highest-address `JUMP` per cell and a second is **silently lost**.
  Nest loops with LOCAL `BR` branches, never with extra JUMPs. (Same rule INV-48
  rule 2 states for panel blocks; it is general.)
* **The loop exit must leave the loop AXIS.** A cell has one forward face, so
  the finished value cannot travel back along the path the recirculated value
  uses — the neighbour that bounces the loop would consume it (INV-32). Switch
  `FACE` (CONFIG 1 is runtime-writable) and drop the result off-axis into a
  dedicated egress cell.
* **Deliver the recirculated word into the register the body already reads.**
  Aliasing the loop's return input onto the body's accumulator register makes
  re-entry free — no copy, no extra instruction.

**2. A "permutation" is usually a CONSTANT, and a constant is WIRING.** Before
concluding that an indexed dataflow needs a computed destination (and therefore
the panel — INV-47), check whether the schedule actually VARIES AT RUNTIME.
Specs state permutations as literal tables, which *looks* data-dependent and
almost never is.

The diagnostic, in order:

1. **Enumerate the whole schedule and count DISTINCT patterns.** ChaCha20's 80
   invocations are ten identical repeats of an 8-step cycle — 8 distinct
   quadruples in the entire cipher. A short cycle means a counter, not a lookup.
2. **Factor the index into "which cell" and "which word within the cell".** The
   part that selects a *cell* is static routing (free). Only the part that
   selects a *word inside* a cell needs a mechanism, and `LOAD [Rn]`
   (`R0 = mem[mem[Rn] & 0x1F]`) is that mechanism — a computed word choice for
   one register, no panel. ChaCha20's `index(k) = 4k + ((j + k*shift) & 3)`
   factors exactly this way: `4k` IS the row, so it is which lane cell is
   addressed, and only the 2-bit `(j + k*shift) & 3` is ever computed.
3. **Remember there is no `STORE [Rn]`.** `LOAD` reads at a computed address but
   nothing writes at one. The write-back is a static N-way `CMP`/`BR` branch
   (measured: exactly the selected slot changes, the other N-1 untouched). For
   N=4 that is ~11 instructions — cheap. For large N it is not, and *that* is
   the real threshold at which the panel wins.

**The trade this implies.** Cells are usually the resource in surplus (a 120-cell
array) and WORDS are the scarce one (31 per cell). So prefer MORE cells doing
LESS each: an 8-way demux does not fit one cell (~48 instructions) but a chain of
eight one-slot cells fits easily at ~7 each. Splitting a cell is nearly always
the right answer to a budget overrun.

**SAY WHICH LAYER.** Everything above is hardware/ISA-permanent EXCEPT the
one-backward-JUMP-per-cell rule, which is a **TOOLCHAIN** limit
(`placekyt/engine/build.py::_apply_internal_feedback` keeps only the
highest-address `JUMP` per cell) and is fixable there.

**REACH.** Recirculation measured on a 2-cell loop at 7 pass counts up to 80.
The permutation factoring is demonstrated on ChaCha20 (measured end to end:
lane read/write over all 32 (row, half, selector) combinations, the full 80-step
schedule on chip, and the shipped `ChaCha20QRBlock` sustaining 80 sequential
invocations bit-exact). It is a method, not a proof about all indexed dataflows.

**Gated by:** `verification/tests/test_wide_transit_ceiling.py` (38 tests) —
recirculation at 7 pass counts plus a negative that different pass counts give
different answers; and the INV-47 corrections it also pins.

**Related:** INV-47 (the panel path, whose "only" this narrows), INV-45 (the
transport pricing), INV-48 rule 2 (the same one-backward-JUMP rule, stated for
panel blocks), INV-32 (why the loop exit must leave the axis), INV-34 (shift
counts are immediates — the reason CORDIC unrolled instead of looping).

---

## INV-50 — `_get_routing_distance` falls back to MANHATTAN when its face-walk fails, silently returning a wrong hop count

**Found 2026-08-29 while finishing LZ4DecoderBlock. Measured, not derived — and
NOT fixed yet.** Recorded because it is a live defect that outlives that block,
and because the naive fix regresses orientation.

**The mechanism.** `runtime/python/gr_kyttar/placement/router.py::_get_routing_distance`
walks cell to cell following each cell's own `fwd_face` — which is CORRECT, and is
the real routing model (INV-48 root cause C: a word leaves on its SOURCE cell's
face, but every cell it arrives at forwards it on THAT CELL'S OWN face). But when
the walk does not reach the target, it does not fail. It returns:

```python
    # Fallback to Manhattan distance
    return abs(tx - fx) + abs(ty - fy)
```

That is the **straight-line ray model** — the exact model INV-48 proved false —
reinstated as a fallback. The caller receives a plausible number and patches a
`WRITE`/`JUMP` with a hop count that does not correspond to any path the word will
actually take. Nothing raises.

**Measured on the LZ4 decoder's fold: 6 of 15 internal edges got a wrong hop**
(e.g. `1.mat_seed → 4`: true walk 4 hops, Manhattan 2). Correcting the distance
made the FSM run — the token nibble split, `lit` counting down, `mat` seeded,
bytes reaching the emit cell — where before it hung.

**Reach.** `_get_routing_distance` has **eight call sites** in `router.py`
(internal edges, exit→entry handoffs, and output-port routes), so any block whose
internal geometry makes the face-walk miss is exposed. The 116 shipped blocks are
demonstrably unaffected in practice — they are verified bit-exact on chip — most
likely because their folds keep the walk reachable. **This is not a claim that the
fallback never fires elsewhere; it is a claim that no shipped block currently
depends on it being wrong.**

**Why it is still open.** The naive fix (return a failure instead of a Manhattan
guess) regresses `test_rotated_feedback_block_computes_identically`, because
block-declared faces are stored UNROTATED while the placer rotates the geometry —
so the walk must apply the placement's orientation to each cell's `fwd_face`
before stepping (INV-23). A correct fix does that; a fix that does not, breaks
every rotated block. The builder that found this reverted rather than ship the
half-fix, which was right.

**What to do when you touch this:** rotate the faces in the walk, then make the
non-reaching case an ERROR rather than a guess — a hop count that cannot be
derived is not a hop count. Re-run `placekyt/tests/` (1219 at baseline) and the
orientation suite before believing it.

**Applies to:** any multi-cell block; most acutely to blocks with dense internal
graphs or cells that must send against their resting face.
