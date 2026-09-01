<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Block verification — substrate invariants

Hard-won, model-agnostic rules that apply across block classes. An agent building
or verifying a Kyttar block should read these first. Each is a *constraint* ("always
/ never X"), not a one-block idiosyncrasy. Per-block fixes go in `lessons_log.md`.

*(Navigation note, 2026-08-30: entries appear in LANDING order, not numeric order —
parallel builds landed out of sequence, so e.g. INV-39 sits between INV-33 and
INV-34, and INV-48 before INV-47. All of INV-0..INV-73 exist exactly once; search
for `## INV-N —` to find one. Numbers are never reassigned.)*

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

### ADDENDUM (2026-08-30) — a FILE-level mutant can leave a stale `.pyc` serving the MUTANT bytecode

Measured twice, same day, by the orchestrating session. A mutation gate that
edits a block's SOURCE FILE, runs the test, and restores the file can complete
the whole mutate→test→restore cycle **within ONE SECOND**. Python validates a
cached `.pyc` by source mtime (SECONDS granularity) and size — and a
one-character mutation keeps the size identical — so after the restore,
`__pycache__` still holds the MUTANT's bytecode and serves it to every later
import. Consequence observed both times: the restored, correct block appeared
broken across multiple subsequent runs until `__pycache__` was cleared, which
reads exactly like a real regression.

**Rule:** a file-level mutant must clear the module's `__pycache__` on BOTH the
mutate AND the restore. Program-level mutants (in-memory `CellProgram`
mutation, no file edit) are immune and remain the preferred form.

**Layer:** verification harness — but the underlying behavior (pyc validation =
seconds-granularity mtime + size) is permanent Python, so the harness
discipline is permanent too.

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
| `LZ4DecoderBlock` | 8 | 4 | 3 | **done** |

Two shipped blocks are strictly worse than LZ4 on both counts, and LZ4 shipped
too. Whatever stopped it, it was never the size of its graph.

**The THREE idioms that make a many-target cell work**, all shipped and verified:

1. **Targets consecutive along one walk.** `LMSEqualizerBlock`'s `bcast` sits at
   (7,1) facing west and its six targets are the six cells west of it, so one walk
   delivers all six. This is what a serpentine fold is *for*.
2. **An in-program FACE FLIP.** Declare `DataWord(..., is_face=True)` and emit
   `MOVE [FACE], R{data:face_x}` … WRITEs … `MOVE [FACE], R{data:face_y}` to point
   the cell somewhere else for a burst and then restore it. See `LMSEqualizerBlock`
   `f0`/`bcast` and `MMTimingRecoveryBlock`. **Cost: 2 instructions + 1 data word
   per extra direction** — so a cell's spare words are what bound how many
   directions it can serve.
3. **SPLIT THE CELL and put the new one ON the walk.** When a cell cannot afford
   the flips its direction count demands, move one target's work into its own
   cell and place that cell BETWEEN the source and another target, resting toward
   that target. An occupied cell is transparent to a hop-counted word (measured
   — see below), so ONE flip now serves both: the new cell at hop 1 and the far
   target at hop 2 through it. This is what unblocked `LZ4DecoderBlock`, and it
   is cheaper than a flip whenever a spare cell exists, which on a 120-cell array
   is almost always. Cells are the surplus resource; words and FACES are not.

**RESOLVED 2026-08-29 — the LZ4 decoder is `done`.** It decodes on the
auto-placed, routed, built design, byte-exact, on a real chip through a real
`SramPanelDevice`, including blocks from the reference C compressor. What follows
is what the wall actually was and what moved it, because the SHAPE of the answer
generalises.

**The wall was a FACE COUNT, not a word count.** With the template's real rules
modelled, an exhaustive search over three independent windows (cols 5-9 × rows
10-11 → 24 feasible; cols 4-9 × rows 10-11 → 20; cols 6-9 × rows 9-11 → 20) found
that **every** placement required the emit cell to serve THREE directions (panel,
egress, ring-forward) = 6 words against the 5 it had. That measurement was
CORRECT and it held: widening west and adding a row both failed, exactly as
recorded.

**The lever was an extra CELL, and it bought a FACE.** Moving ONLY the egress to
an 8th cell drops the emit cell to TWO directions — and placing that cell
BETWEEN the emit cell and the controller, resting toward the controller, collapses
those two into **ONE** flip: the eastward burst delivers to cell 7 at hop 1 and to
the controller at hop 2 *through* it. Thirteen of the fold's fifteen internal
edges then ride resting faces with no flip at all. The block is 8 cells; there is
no cell cap. This is INV-46's "prefer more cells doing less" paying off on the
axis that was short — FACES, not instructions.

**The `set_addr` removal is CORRECT and PROVEN on silicon**, and is now also
proven end to end: the read and write paths do **not** share a counter — `write`
drives `wraddr`, `lookup`/`read` drive `rdaddr`, and each re-writes the panel's
single R5 address latch from its own counter before its own trigger. It frees
exactly 3 words (emit 2 → 5), which is what pays for the one flip. *A harness
that presets `wpos` mid-stream must ALSO seed `wraddr`, since the address is no
longer re-sent per byte; an earlier attempt read that mismatch as a DSP bug and
reverted a correct change.*

**THREE SUBSTRATE FACTS, each MEASURED on a real chip, each the opposite of the
cautious guess.** These are hardware, permanent, and they are what make the
"egress cell in the middle" arrangement legal:

* **An OCCUPIED cell is TRANSPARENT to a hop-counted word.** A cell carrying a
  real program forwards a transiting word on its own face without executing; only
  a word that LANDS (HOP_CNT == 31) runs the program. Measured both ways: with the
  middle cell faced along the walk the target receives; faced across it, nothing
  does.
* **A cell may FLIP its face while words TRANSIT it.** Measured at 180 concurrent
  transits across a cell running flip → write → restore bursts: ZERO losses, zero
  misdeliveries. The race everyone assumes exists does not, and assuming it does
  rules out the entire class of layouts that work.
* **A BLANK cell faced across the walk DOES deflect.** The egress cell is not
  transparent — this half of the earlier finding is true, and it is why the egress
  must belong to a cell whose walk can afford to end there. Measured signature:
  correct hops, zero panel writes.

**One more template rule, measured:** the corridor is made of PLAIN TRANSIT cells,
so an egress WRITE must carry the hop count for the WHOLE corridor plus the port
exit — aiming it at the first corridor cell (`@1`) parks the byte there and the
port stays silent while everything inside the block still looks perfect.

**What the per-cell gates could not see.** Three defects survived every per-cell
chip gate, the whole FSM model, the golden and the reference-C cross-check, and
were caught only by running the placed design: cell 4 kicking the emit cell's
RETURN entry instead of `fetch`; the OFFSET cell (the landing cell for the WHOLE
match phase) dropping a match-length CONTINUATION byte once `nb` hit 0, stalling
every match longer than 18 bytes; and the egress hop above. Each is now a
mutation gate that corrupts the real block and rebuilds on chip.

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
   binds. When a cell cannot afford its flips, SPLIT IT and put the new cell ON
   the walk (idiom 3 above) rather than hunting for one more word.
4a. **A RING closes the hand-backs for free.** On a LINE the phase/state
   hand-backs run against the traffic and every arrangement needs a flip
   somewhere it cannot be afforded; on a closed walk the word simply continues
   round to the target. The LZ4 fold is a 3×2 ring over cells 0..5 with the
   controller and the egress cell on a tail, and 13 of its 15 internal edges ride
   resting faces. Cells on a ring must all rest along it, so a ring cell that
   ALSO needs another direction must be able to afford a flip AND restore it —
   an unrestored flip leaves the cell facing the wrong way for the words that
   TRANSIT it, which is a different bug from the one being fixed.
5. **TEMPLATE ROLES ARE PER-FUNCTION, NOT PER-CELL.** The self-contained panel
   template names five: `controller_cell`, `input_cell`, `return_cell` (where the
   push-read lands — must sit on the `x1_in` row), `panel_client_cell` (whose
   WRITE/JUMPs must REACH the controller) and `output_cell` (which owns the
   EGRESS walk). The last two DEFAULT to the return cell and are the same cell in
   the Varicode shape; separating them is exactly what a block needs when its
   emit cell cannot afford a third face. The template validates each walk
   separately, following the real forwarding rule rather than a straight line.
6. **BUILD-PATH BUGS THIS SHAPE OF BLOCK EXPOSES** (found and fixed 2026-08-29
   while placing the LZ4 decoder; each was silent):
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
     REGISTER alone and takes the lowest-addressed match — and returns after the
     FIRST hit, so a cell that issues the SAME hand-off from several branches
     gets only one of them patched (the LZ4 token cell writes `st_set` from three
     branches). Both halves are moot once the RESOLVER gets the right distance
     (INV-50), which is the real fix; the pass itself is still ambiguous and
     would be worth making port-aware.
   * `panel_pnr._apply_self_contained_template` set the block's placement-derived
     `emit_hop`/`out_dest` only `if "emit_hop" in blk.params` — i.e. only when the
     CALLER happened to pass it. A design built from an empty params dict silently
     kept the constructor's hard-coded default, which is wrong for every geometry
     but the one it was written against. Now keyed on what the block CLASS
     accepts.

**Ground truth:** `verification/tests/test_lz4_decoder.py` — 62 tests, no skips.
The token nibble split, the little-endian offset assembly, a whole match copy and
the `offset == 1` byte run all run on a real chip through a real
`SramPanelDevice`, with each match byte push-read at the **computed** address
`wpos - off`; placement, build and DRC are green through
`_apply_self_contained_template`; and — the one that matters —
`test_auto_placed_design_decodes_on_chip` (8 payload classes) plus
`test_auto_placed_design_decodes_REFERENCE_C_blocks_on_chip` decode byte-exact on
the placed, routed, built design. Routability is now a POSITIVE gate
(`test_every_internal_edge_DELIVERS_under_the_real_forwarding_rule`), as is the
budget arithmetic that used to pin the wall
(`test_the_emit_cell_can_afford_the_ONE_flip_the_layout_asks_of_it`) — an
assertion that a limit HOLDS passes precisely while the block is broken, so both
were inverted rather than deleted. Related: INV-29 (why it needs the panel),
INV-31 (the panel contract), INV-32 (port-cell transit), INV-33 (the
register-allocation contract the cell budgets come from), INV-36 (the 31-hop
cap), INV-46 (prefer more cells doing less — the rule that unblocked it), INV-50
(the router distance bug found while measuring this, now CLOSED).

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

## INV-50 — an INTERNAL hop must be WALKED, never estimated; the walk needs the right FIRST step AND the right TRANSIT faces

**Found 2026-08-29 while finishing LZ4DecoderBlock. CLOSED 2026-08-29 by TWO
independent passes** — `ChaCha20KeystreamBlock` and `LZ4DecoderBlock` — which hit
opposite halves of it, fixed the half they hit, and were merged. Kept in full
because *four* plausible fixes were tried across the two passes and **three of
them build clean and silently compute the wrong answer**; each was caught only by
a shipped block's bit-exactness gate.

> **Merge note (evidence rule).** Two entries for this invariant existed briefly,
> one per pass. This is the reconciled one. Neither mechanism was dropped: they
> address **different steps of the same walk** and the fix needs both. What WAS
> dropped is one redundant code path — see "what got dropped" below.

**The mechanism.** `runtime/python/gr_kyttar/placement/router.py::_get_routing_distance`
walks cell to cell following each cell's own `fwd_face` — which is CORRECT, and is
the real routing model (INV-48 root cause C: a word leaves on its SOURCE cell's
face, but every cell it arrives at forwards it on THAT CELL'S OWN face). But when
the walk did not reach the target, it did not fail. It returned:

```python
    # Fallback to Manhattan distance
    return abs(tx - fx) + abs(ty - fy)
```

That is the **straight-line ray model** — the exact model INV-48 proved false —
reinstated as a fallback. The caller received a plausible number and patched a
`WRITE`/`JUMP` with a hop count that corresponds to no path the word will take.
Nothing raised.

**Measured on two independent folds:**

* **LZ4 decoder — 6 of 15 internal edges got a wrong hop** (e.g.
  `token.mat_seed → matchlen`: true walk 3, Manhattan 1). Correcting the distance
  made the FSM run — the token nibble split, `lit` counting down, `mat` seeded,
  bytes reaching the emit cell — where before it hung.
* **ChaCha20 keystream — 233 internal edges classified by emit face**: resting
  **211 correct / 0 wrong**, flipped **16 correct / 6 wrong**, and all 16
  "correct" only by coincidence (the resting walk missed, Manhattan fired, and
  Manhattan happened to equal the true distance — every one a straight-line 1 or
  2). One edge was sized 3 against a true 1 and cost 75 of the cipher's 80 laps.

The second table is the more useful one, because it names the failure mode that
is *worse* than the fallback: a resting walk that **succeeds spuriously** on a
path the word never takes. Nothing looks anomalous — a plausible walk, a
plausible number, no fallback taken.

### THE WALK HAS TWO INDEPENDENT HALVES, AND IT NEEDS BOTH

A walk is *"leave the source on face F, then turn at every occupied cell you
cross."* Getting a hop right therefore needs **the right first step** AND **the
right transit faces**, and the two passes each found one of them broken. Neither
subsumes the other; a fix with only one half still returns wrong numbers.

**HALF 1 — the FIRST step: which face does THIS PORT leave on?**
(from the `ChaCha20KeystreamBlock` pass)

A cell may re-point itself mid-program (`MOVE [FACE], …`) and emit a WRITE/JUMP
while flipped, so the word leaves on the FLIPPED face, not the resting one. A
block declares this per edge:

```python
def emit_faces(self):            # {(cell_id, port): neighbour_cell_id}
    return {("tap3", "collect"): "collector"}
```

**The value is a CELL ID, not a compass direction** — the router derives the face
from the two cells' PLACED coordinates, which the placer has already rotated. So
the declaration is orientation-correct **by construction** (INV-23), which is
precisely what the by-hand-rotation attempt got wrong. A declared face is
AUTHORITATIVE: no other face is tried for that edge, because a block that
declares an edge and gets it wrong should see the error rather than have the
router quietly find some other face that happens to work.

The same pass found the mechanism that made this invisible: `_place_block_cells`
looked programs up by the raw positional INDEX (`if i in block_def.cell_programs`),
**False for every string-keyed block**, so such a block got no program copied
there and never had its declared `CellProgram.fwd_face` honoured at all.

**HALF 2 — EVERY step after it: which face does each TRANSIT cell forward on?**
(from the `LZ4DecoderBlock` pass)

At the time internal hops are resolved the `cell_map` does **not** hold the
block's faces. It holds the ROUTER's positional guess (`_configure_block_cells`
faces a cell toward whichever internal connection the dict yielded first); the
caller's authored faces are applied later, by `build._apply_block_cell_faces`. So
even a perfect first step then walks the fold on fictional faces, misses, and
takes the Manhattan fallback. New `BlockDefinition.cell_faces`, filled by
`build._translate` from the MODEL placement — so these faces are **already
orientation-transformed** too (`Placement.transform` maps a cell's face together
with its coordinates), the same by-construction property that makes `emit_faces`
safe.

*Why Half 1 alone is not enough:* it fixes the departure direction, but the LZ4
ring's `token → matchlen` edge departs on its RESTING face and still needs three
correct transit faces to be sized at 3 instead of Manhattan's 1.
*Why Half 2 alone is not enough:* a flipped edge departs on a face no amount of
transit-face accuracy can infer — which is the residual the ChaCha pass named as
"the worse half".

### THREE FIXES THAT BUILD CLEAN AND COMPUTE THE WRONG ANSWER

**(a) WRONG — "rotate the declared face constants by hand."** The reason
`emit_faces` takes a cell id. Rotating direction constants by hand is what
regressed `test_rotated_feedback_block_computes_identically` on the first attempt.

**(b) WRONG — "take the resting face whenever it delivers."** A fold is usually a
CLOSED walk, so the resting face reaches an abutting neighbour the long way round.
`LMSEqualizerBlock`'s `(2,1) → (2,2)` is **1 hop SOUTH and 13 hops around the
serpentine**; the cell flips and writes `@1`, and charging it 13 sends the word
past its target. LMS went from 14 passed to 6 failed, diverging at sample 2. This
is the same hazard the ChaCha pass measured as a *spuriously succeeding* resting
walk — the worst class, because nothing looks anomalous.

**(c) WRONG — "take the shortest of all four faces."** That hands a cell a
direction it has NO WAY to take. LZ4's `token → matchlen` is 1 hop SOUTH, but the
token cell declares no face word and cannot flip; its word really costs the 3 hops
of its westward ring walk. All eight end-to-end payload classes broke.

**(d) THE RULE THAT SURVIVES** — one function, `Router._internal_distance`:

1. If the block DECLARED this edge's emit face (`emit_faces()`), use it and only
   it. Authoritative.
2. Otherwise: the source cell's RESTING face, plus **only the faces the cell's own
   program declares** via `DataWord(is_face=True)` — those being the faces a
   `MOVE [FACE], …` can actually select — with the SHORTEST delivering walk
   winning among them. These constants ARE stored unrotated (the build rewrites
   them later), so they are rotated here by `BlockDefinition.orientation` via
   `Router._face_after`, verified identical to `model.enums.face_code_after` over
   all 336 (op-sequence, face) combinations. Without that rotation,
   `MMTimingRecoveryBlock` and `ChirpGeneratorBlock` lose 18 orientation cases.
3. Every step after the first uses `cell_faces`, never the router's guess.
4. **A DIRECT ABUTMENT IS ALWAYS 1**, whatever the faces say. A cell may aim a
   word at an edge-adjacent neighbour on a face this function cannot see: a
   DUAL-FACE output cell's TAP direction is not an `is_face` word at all — it is
   filled in later from the drawn route (`build._apply_rotate_tap_face`).
   Refusing that edge broke `CoherentRXBlock` (`pd_pi.yi_tap → yi_relay`,
   (6,0) → (7,0)).

Rule 2 is the *inference* path and rule 1 is the *declaration* path. Prefer
declaring: inference knows which faces a cell HAS, never which port uses which.

### WHAT GOT DROPPED IN THE MERGE

One code path, no behaviour: `_get_routing_distance` briefly had a **second copy
of the walk loop** for the `start_face` case. It is the same walk with a different
first step, so it is now written once — `start_face` seeds step 1, `authored`
supplies the rest. Both mechanisms' semantics are preserved exactly, including
the deliberate choice that a DECLARED emit face which misses falls through to the
legacy Manhattan number rather than raising: that keeps the declaration from ever
being a regression, and the block's own fold gate is what must catch it.

**Failure behaviour.** For an INTERNAL edge whose target is unreachable AND
non-adjacent, the Manhattan fallback is gone: a named `RouterError` names the two
cells and the rule. For BLOCK-TO-BLOCK and BLOCK-TO-PORT edges the fallback
stays, because there the `cell_map` genuinely holds drawn-route faces and the
estimate is an estimate rather than a fiction.

**Verified:** `placekyt/tests/` 1219 passed (identical to baseline, twice);
`test_orientation_invariance.py` 364 passed; the panel-backed family (Varicode,
CW, Golay, the panel-param refresh) 268 passed; LMS + FFT + Costas + ChaCha20 +
M&M + the rendezvous family 649 passed; 220 example/transceiver integration tests
passed; `test_lz4_decoder.py` 62 passed with no skips.

**Reach.** `_get_routing_distance` has **eight call sites** in `router.py`. The
three INTERNAL ones (declared `internal_connections`, declared `internal_jumps`,
and the positional cell-n → cell-n+1 default) now go through `_internal_distance`
and the rule above; the five block→block / block→port ones are unchanged. The
declared edges RAISE on an unreachable target; the positional default keeps the
estimate, because an UNDECLARED hand-off has no authored intent to check against
— a guess about what the block meant should not become a hard error.

**LAYER: TOOLCHAIN, fixable, fixed.** Nothing here is a substrate property; the
substrate rule is INV-48's forwarding rule, which is hardware and permanent.

**Reach of the CLAIM, stated honestly.** The three suites above cover every
shipped multi-cell block that has an on-chip bit-exactness gate, at all 8 D4
orientations, plus every shipped example end to end. What is NOT claimed is that
no other block's internal geometry could still take the positional-default
estimate and be wrong — that path deliberately keeps the old behaviour, and a
block that relies on it would show up as a datapath divergence, not an error.
The three regressions this fix passed through (LMS, MMTiming/Chirp orientation,
CoherentRX) were each found by exactly that kind of gate, which is the argument
that the coverage is real.

**Applies to:** any multi-cell block; most acutely to blocks with dense internal
graphs or cells that must send against their resting face.

**THE "SPURIOUS SUCCESS" RESIDUAL IS CLOSED.** An intermediate revision of this
entry named it as the worse, unfixed half: for an *undeclared* flipped edge the
resting-face walk can succeed on a path the word never takes, with no fallback
and nothing anomalous to see. Rule 2(d) closes it — the inference no longer
accepts a resting walk merely because it arrives. It considers the resting face
AND the faces the program declares it can flip to, and takes the shortest, so the
abutting flip beats the long way round the fold. That is exactly the ChaCha
`tap → collector` case (real 1 east, resting walk 3 via `adder → egress`): the
flip face is now among the candidates and 1 wins.

What remains a deliberate guess, and is NOT claimed fixed: the **positional
default** hand-off (cell *n* → cell *n+1*, undeclared in either
`internal_connections` or `internal_jumps`) keeps the Manhattan estimate rather
than raising. An undeclared hand-off has no authored intent to check against, and
a guess about what the block meant should not become a hard error. Declare the
edge if you care about its hop.

---

## INV-52 — The FACE register is CELL state: it persists across entries, it steers TRANSITING words, and the router cannot see it

*(Number assigned at landing — this entry was authored by a parallel builder
before its number existed; three numbering collisions had already happened
because parallel builders cannot see each other's KB additions.)*

**Found 2026-08-30 while finishing `ChaCha20KeystreamBlock`. Every claim below is
MEASURED on a real placed + routed + built chip, from execution traces.** It
sharpens INV-48 root cause C from "a word is forwarded on each transit cell's own
face" into the three consequences that actually bite, and it explains *why* INV-50
is so hard to notice.

### 1. The face register PERSISTS across entries. (hardware — permanent)

`MOVE [FACE], R{data:x}` writes a **cell** register, not a per-entry one. An entry
that does not set it inherits whatever the last path to run left behind. Two of
this block's cells flipped for one burst and never restored, so the *next* entry
fired its triggers in the previous entry's direction — no output, no error.

**The rule: every path RESTORES the resting face before it ends**, so every path
may assume the resting face on entry. Restore at the TAIL, not the head: a head
restore protects the cell's own edges but leaves the face dirty between
activations, which is what clause 2 punishes.

### 2. A cell's flip deflects words that merely TRANSIT it. (hardware — permanent)

A transiting word is forwarded on the transit cell's **live** face register, not
on the face its layout gave it. So a cell that leaves itself flipped silently
re-routes every walk that crosses it.

**Measured:** the write-back cell sat on the sequencer's walk down to the state
line and left its face pointing at the control column. Every lap-start trigger the
sequencer issued bounced off it straight back into the sequencer, which re-entered
its own step entry, decremented the lap counter again and ping-ponged. The ring
completed exactly ONE lap and then oscillated forever.

**Consequence for layout:** a cell's resting face is a contract with every walk
that crosses it, not only with its own edges. In this block the sequencer's
resting face is *forced* by another cell's jump needing to transit it, so the
sequencer pays for a flip-and-restore on every one of its own edges.

**BOUNDED 2026-08-29 (LZ4 pass) — it is the UNRESTORED face that deflects, not
the flip WINDOW.** This clause reads as "never flip a cell anything transits",
which would forbid a whole class of working layouts. The narrower truth was
measured on one harness with exactly one variable — whether the flipping cell
restores its resting face at the tail:

| mid cell | transits delivered | deflected |
|---|---|---|
| flips → writes → **RESTORES** | **160 / 160** | 0 |
| flips → writes → does NOT restore | **0 / 160** | all of them |

So a flip-and-restore burst is SAFE to run on a cell that other walks cross, even
under concurrent load (60 bursts against 60 transits, interleaved, zero losses).
This is what let `LZ4DecoderBlock` put its egress cell BETWEEN the emit cell and
the SRAM controller: cell 7 rests facing the controller so the panel words transit
it untouched, and flips north for its own egress — the arrangement that closed a
three-directions-for-six-words budget wall (INV-48).

Two details the same measurement pins, both sharpening clause 1:
* the deflected words are **not dropped** — they are delivered to the WRONG entry
  (SINK's count went to *double* its expected value, having absorbed DST's
  traffic). That is why an unrestored face presents as a ping-pong or a
  double-count, not as silence;
* the restore must be at the **TAIL**. A head-only restore protects the cell's
  own edges and still leaves the face dirty between activations, which is the
  window this table's second row measures.

So clause 1's rule ("every path RESTORES the resting face before it ends") is not
merely good hygiene — it is the entire precondition that makes clause 2 survivable.

### 3. The router cannot size a FLIPPED edge. (TOOLCHAIN — partly fixed)

`router._get_routing_distance` walks **resting** faces. For an edge emitted while
flipped it therefore walks a path the word never takes. Classified over all 233
internal edges of this block:

| emit face | resolved correctly | resolved WRONG |
|---|---|---|
| resting | **211** | **0** |
| flipped | 16 | 6 |

**Every flipped edge that worked did so by coincidence** — the resting walk missed,
Manhattan fired, and Manhattan happened to equal the true distance. All 16 are
straight-line hops of 1 or 2. The failures are the ones where the resting walk
*succeeds spuriously*: a tap with connections both east (to the collector, 1 hop)
and north (to its adder, 1 hop) was faced north, and the walk found the real path
`tap -> adder -> egress -> collector` and returned **3**. At run time the word left
east and overshot by two. Every frame reached the collector two words short, the
mod-8 frame counter never aligned, and the cipher ran five laps and stalled.

**This is worse than INV-50's Manhattan fallback**, because nothing looks
anomalous: a plausible walk, a plausible number, no fallback taken.

**Three fixes landed, all regression-tested at the 1219 baseline** — the first two
from this pass, the third from the `LZ4DecoderBlock` pass that closed the residual
this clause originally left open. **INV-50 is now the single reconciled account of
all three; read it for the merged rule.**

* `router._place_block_cells` looked programs up by the raw positional INDEX
  (`if i in block_def.cell_programs`), which is **False for every string-keyed
  block**. Such a block got no program copied there and — sharply — **never had
  its declared `CellProgram.fwd_face` honoured**, so the router kept its own guess
  (face whichever internal connection the dict yielded first) and sized every edge
  against it. Now looks up by the positional KEY, falling back to the index.
* `_get_routing_distance` takes an optional `start_face`, and a block may declare
  `emit_faces() -> {(cell_id, port): neighbour_id}` for ports it emits while
  flipped. **The value is a CELL ID, not a compass direction** — the router derives
  the face from the two cells' PLACED coordinates, which the placer has already
  rotated. That is orientation-correct by construction (INV-23), rather than
  needing the by-hand rotation that regressed
  `test_rotated_feedback_block_computes_identically` on the previous attempt.
* **The spurious-success residual is closed.** The walk now takes the block's
  AUTHORED transit faces (`BlockDefinition.cell_faces`, from the model placement)
  instead of the router's positional guesses, and for an UNDECLARED edge it
  considers the resting face *and* the faces the program declares it can flip to,
  shortest wins — so a resting walk no longer wins merely by arriving. The
  `tap → collector` case above (real 1 east, resting walk 3 via
  `adder → egress`) now resolves to 1. What is still a deliberate guess is the
  POSITIONAL default hand-off (cell *n* → *n+1*, undeclared), which keeps the
  Manhattan estimate: declare the edge if you care about its hop.

### 4. A fold checker must iterate the face to a FIXPOINT

INV-51's method note said a checker must read `MOVE [FACE]` out of the real
programs. Necessary, not sufficient. A checker that assumes each entry starts on
the resting face cannot see clause 1 or clause 2 — and those were half the bugs
here. **Symbolically execute every path, over both sides of every branch, and
iterate: seed with the resting face, collect the faces each path can LEAVE behind,
re-seed, repeat until nothing new appears.**

### 5. Changing a control cell's LENGTH can re-target another cell's JUMP

Entry addresses are params-dependent (INV-6/11) and that hazard reaches INTERNAL
edges. Deleting one redundant instruction from the sequencer moved its `step`
entry from 15 to 14; the build then mis-resolved a *different* cell's backward
jump to it instead of to the row publish entry, which is also at 15. The
realignment ran perfectly and then handed control to the lap counter — the ring
stopped at the first boundary with a flawless trace up to that point. **After any
change to a control cell's instruction count, re-read the BUILT words, not just
the budget.**

**SAY WHICH LAYER.** (1) and (2) are hardware and permanent — design around them,
and note (2) is bounded: the flip WINDOW is safe, the UNRESTORED face is what
deflects (measured, table above). (3) is toolchain in `placement/router.py` and is
now CLOSED for declared and inferable edges alike; only the positional-default
hand-off keeps an estimate, by choice. (4) is method. (5) is toolchain, in the
build's jump resolution, and is ~~still open~~ **closed — RECONCILED 2026-08-30:
INV-53 names the exact mechanism** (`_apply_internal_feedback` resolves a
backward jump by rewriting the source cell's HIGHEST-ADDRESSED `JUMP`, and an
entry-address coincidence can mask the damage). INV-63 and INV-64 §2 record the
two later-found build passes that contest that same highest-addressed slot.

**REACH.** Measured on one 40-cell block over 233 internal edges and ~167k
simulated events, plus a second, independent fold (`LZ4DecoderBlock`, 8 cells /
15 internal edges) and a dedicated 320-transit harness for clause 2's bound.
(1), (2) and (5) are mechanisms in the shared hardware/build
path and apply to every multi-cell block; (3)'s table is this block's fold, but
`_get_routing_distance` has eight call sites in `router.py`.

**Gated by:** `verification/tests/test_chacha20_fixed_tap_ring.py` — the
fixpoint fold checker, its INV-4 negative (re-point each original face constant
and assert it misses), the `emit_faces()` abutment/consistency gate, and an
on-chip gate that pins the full RFC 8439 schedule plus the first state word.

**Related:** INV-48 (root cause C, which this sharpens), INV-50 (the sibling
distance defect — this is why it hides), INV-51 (the fold/pairing traps from the
same block), INV-43 (a remote JUMP does not stop local execution — which is what
makes a tail restore after a `{jump:…}` work at all), INV-6/11 (params-dependent
entry addresses, clause 5).

---

## INV-53 — A backward JUMP is resolved by ADDRESS, not by name: it rewrites the source cell's HIGHEST-ADDRESSED jump, and an entry-address COINCIDENCE can hide the damage indefinitely

**This closes INV-52 clause 5**, which was recorded as "toolchain, in the build's
jump resolution, and is still open". The mechanism is now exact, and the failure
it produces is the worst kind: silent, and *self-concealing*.

**THE RULE.** `build._apply_internal_feedback` resolves a **backward** internal
jump — one whose destination cell precedes its source in `build_cell_programs`
order — by rewriting the source cell's **highest-addressed `JUMP` instruction**,
whichever instruction that happens to be. It does not match on the port name; it
cannot, because the routed-exit patch pass may already have clobbered the dest.
It takes `max(jaddrs)` and overwrites it.

Two corollaries, and the second one is the one nobody states:

1. **At most ONE backward jump per cell** (this half was already in INV-48 rule 2
   and INV-49): a second one overwrites the first, silently.
2. **The backward jump must BE the cell's highest-addressed `JUMP`.** A cell with
   exactly one backward jump is still corrupted if some *other* jump sits at a
   higher address — that other jump is the one that gets rewritten, and it is
   redirected to the backward edge's target.

**MEASURED.** `ChaCha20KeystreamBlock`'s `wbk` declares one backward jump,
`step → seq.step`. Its highest-addressed `JUMP` was `back → row0.pub`, the
realignment's hand-back. The build therefore rewrote `back` to point at
`seq.step`. Read straight out of the built words: address 30, `0x73d2` — `JUMP
dist=1 entry=18`, where 18 is `seq.step` and `row0.pub` is 15.

**WHY IT SURVIVED A WHOLE PASS UNDETECTED — the coincidence.** In the previous
revision `seq.step` and `row0.pub` **both resolved to address 15**. The corrupted
jump therefore still landed on the correct entry, purely because the two numbers
were equal. The block ran, the trace was clean, and the defect was invisible.

It became visible only when `seq` was shortened by three instructions for an
unrelated reason: that moved `seq.step` from 15 to 18, decoupled the pair, and
the realignment's hand-back started firing into the lap counter instead of the
row publish. The symptom was maximally misleading — 80 laps still ran, the
boundary count was still right, and every output word was wrong.

This is the same hazard the previous pass recorded from the other side ("dropping
`seq`'s redundant `MOVE half, four` FITS AND BREAKS THE BLOCK"). It was read then
as *"shortening a cell can break an internal edge"*. That is the symptom. The
cause is that the edge was **already broken** and an address collision was
masking it. Shortening the cell did not break anything; it *revealed* a defect
that had been there all along.

**THE GENERAL LESSON.** An equality between two entry addresses is not a
coincidence you can leave unexamined — it can be load-bearing for a bug. When two
entries a mis-resolution could confuse collide numerically, the mis-resolution is
undetectable. Prefer to **assert the pair distinct** rather than to rely on the
collision; if a design genuinely depends on two entries being equal, that
dependency is a defect wearing a disguise.

**THE FIX IS FREE.** Emit the entry containing the backward jump **last**, so its
`{jump:…}` is the cell's highest-addressed one. In `wbk` that meant moving the
`default` entry after `bnd`, which cost one `HALT` — recovered by hoisting the
realignment spins common to both half-boundaries (row1 once, row2 twice, row3
once) out of the branch, leaving only the two that differ.

**HOW TO CHECK IT, statically and cheaply** — both clauses, no chip required:

```python
order = list(block.build_cell_programs())
idx = {c: i for i, c in enumerate(order)}
backward = {}
for s, sp, d, dp in block.internal_jumps():
    if idx[d] < idx[s]:
        backward.setdefault(s, []).append((sp, d, dp))
for cid, edges in backward.items():
    assert len(edges) == 1                      # clause 1
    code = [ln.strip() for ln in progs[cid].assembly_template.splitlines()
            if ln.strip() and not ln.strip().endswith(":")]
    last_jump = max(i for i, ln in enumerate(code) if "{jump:" in ln)
    assert code[last_jump] == "{jump:%s}" % edges[0][0]      # clause 2
```

**PROGRAM ORDER IS A DESIGN LEVER, not bookkeeping.** Whether a jump counts as
"backward" is decided entirely by the order `build_cell_programs` yields cells.
A control cell that fires several triggers at cells listed *after* it pays
nothing; the same cell listed *after* them keeps one trigger and silently drops
the rest. `ChaCha20KeystreamBlock`'s drain sequencer fires five triggers at the
state rows and is listed **before** them for exactly this reason — as an interior
cell appended at the end it would have kept one and lost four.

**SAY WHICH LAYER.** Toolchain, `placekyt/engine/build.py`
`_apply_internal_feedback`. It is fixable there — match the authored port name
rather than `max(jaddrs)` — but the rewrite exists precisely because the dest may
already be clobbered, so the fix needs care. Until then it is a **block-authoring
contract**, and it is cheap to honour and cheap to gate.

**REACH.** The resolution rule is in the shared build path and applies to every
multi-cell block that declares a backward internal jump. Measured on
`ChaCha20KeystreamBlock` (41 cells, 250+ internal edges) by reading the built
bitstream words back off the loaded chip.

**Gated by:** `verification/tests/test_chacha20_fixed_tap_ring.py` —
`test_at_most_one_backward_internal_jump_per_cell` (both clauses) and
`test_entry_addresses_stay_distinct_where_edges_resolve` (the masking
coincidence).

**Related:** INV-52 clause 5 (which this closes), INV-48 rule 2 and INV-49 (the
"at most one backward jump" half, which is necessary but not sufficient),
INV-6/11 (params-dependent entry addresses — the reason the collision moved).

**Cross-references added 2026-08-30 (at landing):** two later entries touch the
SAME highest-addressed-rewrite mechanism from other sides, and a reader landing
here should know all three exist — **INV-63** (an EXIT cell's highest-addressed
WRITE/JUMP are contested by this feedback pass AND the output-port sink fixup,
so a declared backward jump and the external port pair cannot share a cell;
`RAW_OUTPUT_HOPS` + build-time-resolved literals are the escape) and
**INV-64 §2** (the WRITE-side sibling: a declared backward WRITE delivered on a
hop-1 FLIP is re-patched with the RESTING-corridor hop and must be an authored
literal instead).

---

## INV-54 — A BRACKETED schedule needs its LAST closing bracket issued explicitly, and a zero-width bracket will hide the omission

**THE SHAPE.** When a loop body is bracketed — some setup before it and the
inverse afterwards — and the brackets are issued *at the boundaries between
iterations*, the final closing bracket has no following boundary to hang off. It
must be issued explicitly by whatever ends the loop. Issuing `n - 1` closing
brackets for `n` iterations is an off-by-one that no *counting* check detects,
because every count in sight is internally consistent.

**MEASURED.** `ChaCha20KeystreamBlock` realigns its state at each half-round
boundary: row `k` is spun `k` times before the diagonal half and `4 - k` times
after. Ten double rounds therefore need **ten opening and ten closing brackets**.
Nineteen boundaries fall between laps and were issued; the twentieth — the
closing bracket of the *last* diagonal half — was never issued at all. On chip
the spin counts were **37/38/39** for rows 1/2/3 where the schedule requires
**40/40/40**: exactly `10a + 9b` against `10a + 10b`.

**WHY IT SURVIVED A WHOLE PASS — the zero-width bracket.** Row 0's bracket is
`0` spins in both directions. So **row 0 was always correctly aligned**, and row
0's head is the RFC's *first output word*. The block emitted `0xE4E7F110`,
bit-exact, while rows 1, 2 and 3 were each left rotated by `4 - k` too little and
drained slot `k` instead of slot 0. The previous pass's on-chip gate asserted
"19 half-boundary realignments and 37/38/39 spins" *as the exact counts the
schedule requires*, and asserted the value of **word 0 only**. Every assertion
passed. The recorded numbers were the bug, written down as the specification.

**TWO RULES.**

1. **A value gate must cover the DEGENERATE element, not just the first one.**
   The element whose bracket is zero-width, whose coefficient is 1, whose shift
   is 0 — that is the element that stays correct under the bug, and it is very
   often the one a "check the first word" gate lands on. Assert **all** outputs,
   or deliberately assert the element with the *largest* parameter.
2. **Derive the bracket count from the algebra, then assert it.** `n` iterations
   of a bracketed body means `n` openings and `n` closings — not `n` and
   `n - 1`, and not "one per boundary". Write the count as an expression of the
   loop bound (`10 * a + 10 * b`), not as the number that was observed.

**THE FIX WAS FREE**, which is the other half of the lesson: the boundary handler
already alternated between the opening and closing schedules on a toggle, and on
the twentieth entry that toggle is already even. Firing it once more from the
loop's terminal path issues the closing bracket with **no new schedule logic at
all** — and, as a bonus, gave that path the face restore it had been exempted
from needing.

**SAY WHICH LAYER.** Block program — an authoring/verification-method lesson, not
a substrate or toolchain limit.

**REACH.** Any block whose loop body is bracketed by setup/teardown issued at
iteration boundaries: realignments, windowing, save/restore of a rotating tap,
scale-in/scale-out around a fixed-point kernel.

**Gated by:** `verification/tests/test_chacha20_fixed_tap_ring.py` —
`test_the_realignment_needs_TWENTY_brackets_not_nineteen` (an INV-4 negative that
proves the omission is invisible in row 0 and fatal in rows 1-3), and the on-chip
gate, which now asserts **all sixteen** state words rather than the first.

**Related:** INV-4 (mutation testing — this is a mutant that four existing
mutants could not distinguish), INV-52 (the same block's face defects, found the
same way: by measuring rather than by reading the recorded counts).


---

## INV-55 — A REORDER is a COLLECTOR problem: fix the order where the stream is SERIALISED, not where it is produced; and a band whose neighbours all face one way is SEALED

Added 2026-08-29, from `ChaCha20KeystreamBlock`'s emission-order defect. Two
separable rules; the first is a design heuristic with teeth, the second a
geometric property that decides where anything can be put.

### 1. Reorder at the collector, not at the producers

A block whose output comes out in the wrong ORDER (right values, wrong
positions) invites the fix at the PRODUCERS: change when each source publishes,
add a per-source loop, permute the schedule. That is usually the expensive end,
and on this substrate it is often the impossible one, because the producers'
schedule is what the datapath's own wiring already pins.

**Ask instead what the ORDER IS A FUNCTION OF at the point the stream
serialises.** Writing the emission index as an arithmetic expression usually
collapses the problem:

* ChaCha20's drain emits, at output position `4L + k`, the word `state[4k + L]`
  — the 4x4 transpose. Read the other way round, the word wanted at position
  `4k + L` is the one produced by SOURCE `k` on PASS `L`. So "output group `k` is
  source `k`'s four words in pass order", and the whole transpose is *hold each
  source's words, release source by source* — a per-source BUFFER, needing no
  counter that reaches the producers at all.

**MEASURED, on that block:** the producer side has literally zero freedom. The
boot-time load map (which would cost nothing — it is only `initial_value`
constants) is FORCED: each (row, slot) is read on the laps where that row's
rotation offset matches, and the datapath demands a specific word there, so
walking the schedule pins all sixteen cells with no conflict and none left free.
And all `4^4 x 4^4 x 4!` combinations of pre-drain rotation, inter-pass spin and
source publish order were searched: none gives the wanted order, best 4 of 16.
The structural reason generalises — **if one pass visits every source exactly
once, the source index is the fast-varying half of the output position, and no
permutation of passes or sources can exchange the nibbles.**

**The cost, so the trade is real:** a per-source buffer of `d` words of width
`w` costs `d*w/16` registers plus a shift; the release needs a counter and a
re-entry unless something else can trigger it `d` times. On ChaCha20 that came
to 10 live registers + 8 shift instructions, and the counter's `SUB`/`MOVE`/`BR`
triple overflowed the cell by **exactly three words** — measured both ways
(16 instructions/`base_addr` 15/12 live without it; 20/11/13 with it). The
documented fix is to halve the per-cell depth (two stages of `d/2`), which frees
`d*w/32` registers per cell and needs one more cell per source.

**SAY WHICH LAYER.** Block program / fold — FIXABLE. The three-word gap is the
per-cell register-file budget (32 addresses shared by code and data,
`base_addr = 31 - instruction_count`), not a substrate or ISA wall.

**REACH.** Any block that serialises N sources over M passes and needs an output
order different from the one the passes impose: transposes, interleavers,
deinterleavers, matrix/FFT output reordering, block codes read out column-wise.
Measured in detail on one block; the arithmetic argument (fast/slow nibble) is
general, the three-word figure is not.

### 2. A band whose neighbours all face one way is SEALED

A cell forwards on its own resting face and so does every cell a word merely
transits, so a whole BAND of the fold can become unreachable from the rest of
the block without anything flagging it.

**MEASURED:** ChaCha20's finish row — its top band — can reach **NO free slot on
ANY face**, checked over every free slot of the fold's bounding box x all four
faces. North leaves the block (the array row above is the chip's I/O corridor,
and a block-internal `WRITE` into a cell the block does not own is a dead end,
INV-51). South lands on the state line, every cell of which rests EAST because
three different control cells need that one eastward walk, so the word is swept
along it and out. East and west stay on the row.

**The consequence that matters:** anything that must talk to a cell in that band
has to LIVE in that band, so the band's free slot count is a hard budget, and a
cell in it cannot be relieved by the block's free space however much there is.
This is the general form of a narrower claim an earlier pass recorded as "a
per-row sequencer serving row3 has ZERO candidate placements" — that was true
but attributed to the wrong cause (`tap3`'s position); the real cause is that
the band is enclosed.

**How to check it, cheaply:** for each cell of the band, walk every face against
a probe placed in each free slot, using the block's own resting-face map. If
nothing escapes, the band is sealed. **Do not infer sealing from a drawing** —
the state line looks like a wall but is actually a conveyor, and the difference
is what decides whether a word is lost or delivered.

**The re-fold that opens a sealed band:** give it a neighbour band that does not
all face one way — in ChaCha20's case, shifting the fold down one array row and
adding a second band on top, so the producer's northward walk passes THROUGH the
old band (occupied cells are transparent to a hop-counted word) into the new
one. Verified walk by walk before building: every existing control relationship —
the state line's hop-1/3/5/7 broadcast, the tap-to-adder abutment, the
`wb -> seq -> wbk` hand-off — survives the shift, and the one new walk is hop 1.

**SAY WHICH LAYER.** Block fold — a property of a LAYOUT, not of the substrate.
Every sealed band is unsealed by a re-fold.

**REACH.** Any multi-cell block with a uniformly-faced band, which is the normal
shape for a broadcast line (one walk serving several consecutive targets — the
`LMSEqualizerBlock` idiom). The more cells that share one walk, the more likely
the band on its far side is sealed.

**Gated by:** `verification/tests/test_chacha20_fixed_tap_ring.py` —
`test_the_boot_load_map_is_FORCED_by_the_quarter_round_schedule`,
`test_no_drain_side_knob_can_produce_row_major_order`,
`test_the_transpose_is_a_PER_ADDER_buffer_not_a_per_row_loop`,
`test_the_reorder_buffer_misses_this_folds_cell_budget_by_three_words`,
`test_the_two_row_reorder_band_is_BUILT_and_every_walk_resolves` — each with a
proven INV-4 mutant. (The two gates that described the OLD sealed fold and the
re-fold as a *proposal* were replaced when the re-fold was built; see the
addendum below.)



### 3. ADDENDUM (2026-08-29, pass 7) — the band was BUILT, and what it cost

The two-row re-fold rule 2 prescribes is now in the tree. Recording what the
build MEASURED, because two of the numbers differ from the prediction and one
avenue that looked obvious is dead.

**The depth-2 pair fits, with room.** Predicted "frees four registers"; measured
the stage at **22 instructions against a `base_addr` of 9 with eight live
registers**, against the depth-4 form's 20/11/13. The saving is bigger than
halving the state, because depth 2 also **removes the release counter
outright**: a two-slot cell emits BOTH its words from one straight-line entry,
so there is no re-entry, no `SUB`/`MOVE`/`BR` triple and no westward hop.

**FIFO order IS pass order — the reorder needs no schedule at all.** With the
pair wired `first -> second`, after N passes the second stage holds the OLDEST
words. Releasing second-then-first, source by source, is exactly the wanted
order. So the "hold and release" of rule 1 is not a program: it is the FIFO's
own behaviour, and the only thing that has to be arranged is that the stages
sit along the output conveyor **in release order**.

**That ordering is what pins the LAYOUT, and it is tighter than it looks.**
Measured on ChaCha20: the adders are pinned under their taps; the second stage
must be WEST of the first (older releases first on an eastward conveyor); and
therefore the chain's HEAD is one column west of where the adder sits. Getting
a trigger to that head is the whole difficulty — see below.

**A uniformly-faced band is a WALL from below, not just a seal from above.**
Rule 2 says a band whose neighbours all face one way is sealed. The converse
was measured here and matters as much: ChaCha20's state line is a uniform EAST
conveyor, so **no word from the control corner can climb through it** — checked
over every cell x every face. The only cells that can lift a word off such a
line are the ones that already own an off-axis flip (here the four taps, which
flip north to their adders). **Design consequence: put the thing that must be
reachable where an existing flip already points**, rather than adding a relay —
the reorder row's columns were chosen so a tap's inward walk lands on the chain
head, which costs ZERO new face constants.

**Two relay routes were MEASURED DEAD, both on the register budget:**
* via the write-back cell (the control corner's one turn north): 22
  instructions with its two face constants pinned at R8/R9, because the eight
  frame words fill R0..R7 — INV-33's silent overlap, and neither the frame
  width nor the constants can move;
* via the row-trigger cell: 4 spare words where a third face constant plus its
  flip pair needs exactly 4.
Freeing a word by **sharing a face constant with a numeric one** does work and
is the general trick (`EAST` is numerically 1, so it doubles as a decrement or
compare operand — the `wbk` idiom). It is what paid for the tap's relay.

**~~STILL OPEN~~ — CORRECTED 2026-08-29 (pass 8). The "dead jump" reading was
WRONG.** Pass 7 recorded this as "the first stage's `rel` entry executes and
neither of its outgoing jumps lands", and looked for the fault in jump
resolution. **Both jumps resolve correctly.** Read straight out of the built
bitstream, `bufB0`'s `rel` entry is:

```
[21] 0x4060  <== ENTRY 'rel'
[22] 0x62e1  WRITE @8 -> R1      (out.v0h)
[24] 0x62e2  WRITE @8 -> R2      (out.v0l)
[26] 0x62e3  WRITE @8 -> R3      (out.v1h)
[28] 0x62e4  WRITE @8 -> R4      (out.v1l)
[29] 0x72f3  JUMP  @8 entry=19   (out.default — entry 19 IS out's entry addr)
[30] 0x73d5  JUMP  @1 entry=21   (bufA0.rel — entry 21 IS bufA0's rel addr)
```

Every field is right, and the trace shows the words physically leaving: the
first release word is `0xe4e7` then `0xf110` — `0xE4E7F110`, RFC 8439's word 0,
correct. **The real fault is a DEADLOCK, not a resolution failure**; see
INV-56 below. `simkyt` reports it as `stop_reason == "Deadlock"`, which pass 7
never read because it only inspected the trace and the emitted-word count.

**And the missing evidence was already there.** Pass 7 recorded that
`BuildResult` "does not expose the resolved assembly and hop counts". It does:
`BuildResult.chips[N].cells` is `{(x, y): {"entry", "memory"[32], "face",
"cell_id", "block", "routing_only", "classes"}}`, which is the complete resolved
per-cell image. With `_is_instruction_addr`'s rule (an address below `entry` is
a data word) and the v0.11 encoding (`op = word & 0xF000`, `HOP_CNT = bits[9:5]`
with `@N = 31 - HOP_CNT`, entry/dest = `bits[4:0]`), a fifteen-line test-side
disassembler prints the whole band. No engine change was needed.

**REACH.** The fitting result (depth-2 halves the state and deletes the
counter) is arithmetic and general. The layout-pinning argument is general for
any block whose sources are pinned under a uniformly-faced line. The specific
instruction counts are one block's.

**Related:** INV-33 (the register contract and its silent overlap half — the
three-word gap is exactly that budget), INV-46 ("prefer more cells doing less",
which is what the two-stage buffer applies), INV-48/INV-52 (the forwarding and
face rules that make a band sealable at all), INV-49 (check whether a
"permutation" is a CONSTANT before paying for a computed destination — here it
is, and that is why the load map has no freedom), INV-51 (a gap inside the
footprint is a dead end).

### 4. CLOSURE (2026-08-30) — the collector fix SHIPPED; the block is DONE

The per-adder depth-2 buffer-pair collector of addendum 3 is the shipped
design: `ChaCha20KeystreamBlock` is `done` at **51 cells**, and **all sixteen
RFC 8439 §2.3.2 state words leave the built chip bit-exact, IN ORDER**
(`verification/manifest.json`; `test_chacha20_fixed_tap_ring.py`'s on-chip
ordered-value gate and second-batch gate). Any copy of this entry's earlier
language found elsewhere — "the fold emits the transpose", "row-major does not
fit this fold" — describes the PRE-FIX **producer** side only, and is
SUPERSEDED as a statement about the block: it remains true (and still gated)
that no producer-side knob can produce row-major order, and the order was
fixed at the COLLECTOR — which is this invariant's rule 1 working exactly as
stated. The deadlock the built reorder band then hit, and its landed fix (BOTH
wave-separation shapes plus the port-cell egress rule), are recorded in
INV-56's 2026-08-30 addendum.

---

## INV-56 — A single-file conveyor carrying TWO waves in OPPOSITE directions deadlocks; and `stop_reason` is the first thing to read when a block emits nothing

Added 2026-08-29 from `ChaCha20KeystreamBlock` pass 8. Three separable rules.
The first is the defect; the second is the cheap static check that finds it; the
third is a method lesson that cost this campaign an entire pass.

### 1. READ `stop_reason` BEFORE ANYTHING ELSE

`simkyt`'s `chip.run(...)` returns a dict whose keys are
`completed`, `events_processed`, `simulation_time_ns`, **`stop_reason`** and
`total_energy_pj`. When a block emits nothing, `stop_reason` distinguishes the
two entirely different failure modes immediately:

* `"QueueEmpty"` — the chip ran to quiescence. Nothing is stuck; the words were
  never produced, or were produced and mis-addressed. **Look at the program.**
* `"Deadlock"` — the chip is WEDGED. Some set of cells is in a circular wait.
  The program may be perfect. **Look at the geometry.**

**MEASURED, and this is the whole lesson:** the pass-7 fold reports
`stop_reason == "Deadlock"`. Suppressing only the release trigger (one jump
removed, nothing else) flips it to `"QueueEmpty"`. That one-line experiment
localises the fault to the release path in a single run, and it is available
before any disassembly.

Note that `completed` is `False` for BOTH, and the emitted-word count is `0` for
both, so neither of the two signals a driver usually checks tells them apart.
A test-loop that spins `for _ in range(50): chip.run(...)` on a deadlocked chip
gets `events_processed == 0` from the second iteration onward, forever.

**Corollary (added 2026-08-30, so the rule appears ONCE, here): read
`stop_reason` for EVERY case, not one.** `Poly1305MACBlock` measured the SAME
defective layout reporting `"Deadlock"` on some seed values and `"QueueEmpty"`
on others (the measurement is in INV-59), so one case's `stop_reason` is a
sample, not a diagnosis. And when `stop_reason` alone cannot tell a COST story
from a NON-TERMINATION story (`"EventLimit"` with plausible long hops in
sight), count EXECUTIONS PER CELL against the model's expected counts in one
traced run — the reading that disproved `LZ4EncoderBlock`'s "ring saturation"
diagnosis (INV-61's CORRECTED block). Two further pairings sharpen the read:
`stop_reason` + the store-count signature (rule 2 below) localises a wave
collision; `stop_reason == "QueueEmpty"` + a TINY event count means the work
never STARTED (a wrong injection landing, INV-60), not a block bug.

### 2. TWO WAVES, ONE CONVEYOR, OPPOSITE DIRECTIONS — the deadlock

**THE RULE.** A cell forwards on its own resting face and holds its outgoing
word until the neighbour accepts it. So a row of cells that all rest EAST is a
one-way conveyor, and **any traffic that must travel WEST along that same row
must never be in flight at the same time as eastward traffic**. When both are,
two abutting cells each hold a word the other must accept, and the whole row
backs up behind them.

**MEASURED on `ChaCha20KeystreamBlock`'s reorder band.** The band is one
eastward row of eight buffer stages. It carries two different waves:

* the STORE wave, westward — each A stage spills its oldest word WEST into its
  own B stage, once per drain lap;
* the RELEASE wave, eastward — each stage's four words ride the row east to the
  egress.

On the fourth (last) drain lap they overlap, and the chip wedges. The trace
shows the circular wait exactly:

```
bufA3 (9,0)  output_ready face=W -> neighbor 8 (bufB3)   # store spill, westward
bufB3 (8,0)  output_ready face=E -> neighbor 9 (bufA3)   # release word, eastward
```

with `bufA2`, `bufB2`, `bufA1`, `bufB1`, `bufA0`, `bufB0` all queued behind, and
`out` never executing at all. The store-count signature is diagnostic and cheap:
**every stage stored 4 times except `bufB3`, which stored 3** — the one spill
that never landed.

**The timing, so the margin is on record.** On each of the first three laps
`bufA3.default` is followed ~207 ns later by `bufB3.default`. On the fourth,
`drn` fires the release at t=1796686.6, which is **83 ns** after `bufA3` stored
at t=1796603.6 — while that spill is still in flight. The two waves are not
merely adjacent in time, they overlap.

**A HEAD-ON RESTING-FACE PAIR is the degenerate two-cell case**, and this fold
has one of those too: `out` rests NORTH at (9,1) and `bufA3` rests SOUTH at
(9,0), pointing directly at each other. That happened because **the chip's
`x16_out` port cell IS (9,0)** (`kyttar_10x12.yaml`: `x16_out` at
`{x: 9, y: 0, face: east}`) and the fold placed the band's last stage on top of
it, so the router had to bring the egress net NORTH from `out` into a cell the
block itself owns: `blk_out route = [(9,1), (9,0)]`.

**Measured dead ends, so the next pass does not repeat them:**
* sweeping `out`'s resting face over all four directions — **all four
  deadlock.** Breaking the head-on pair alone is NOT sufficient, because the
  general two-wave collision at `bufB3` remains;
* removing `bufA3`'s spill — still deadlocks (the collision just moves west);
* shifting the band one column west to put `out` on the port cell — `overlap`
  DRC, `bpad0` has nowhere to go;
* `bufA3` resting EAST onto the port — breaks the conveyor:
  `bufA0.o0h -> out.v0h` is then undeliverable;
* `out` at (9,1) with `bufA3` at (9,1)/(9,0) swapped — DRC rejects
  `bufB3.nxt -> bufA3.rel` as not deliverable on any face.

**THE FIX is a re-fold, and it is one of two shapes** *(superseded 2026-08-30
— the landed fix needed BOTH shapes, plus the port-cell egress rule; see the
pass-9 addendum below)*. Either (a) separate the
two waves in TIME — the release must not start until the last store wave has
fully drained, which needs a quiescence signal the drain does not currently have
(firing it from the END of the store wave rather than from `drn` in parallel
with it); or (b) separate them in SPACE — give the store wave its own row so the
release row carries eastward traffic only. (b) is the INV-46 move and is
structurally safer; (a) is cheaper if a trigger can be sourced from `bufB3`.

**SAY WHICH LAYER.** Block fold — **FIXABLE**, not a substrate or ISA wall. The
substrate behaviour (a cell holds its outgoing word until the neighbour accepts)
is permanent and correct; what is wrong is a LAYOUT that runs two opposed waves
over one single-file row.

### 3. THE STATIC CHECK — a head-on pair costs nothing to find

Nothing in place, route, build or DRC reports either shape. The two-cell case is
findable from `_geometry()` alone, with no chip run:

```python
delta = {"south": (0, 1), "east": (1, 0), "west": (-1, 0), "north": (0, -1)}
at = {(x, y): cid for cid, (x, y, _f) in lay.items()}
for cid, (x, y, face) in lay.items():
    dx, dy = delta[face]
    nbr = at.get((x + dx, y + dy))
    if nbr is None:
        continue
    nx, ny, nface = lay[nbr]
    ndx, ndy = delta[nface]
    assert (nx + ndx, ny + ndy) != (x, y), f"{cid} and {nbr} rest facing each other"
```

The general N-cell case is not static — it depends on which waves are live at
once — but the store/release overlap has a cheap on-chip signature: **compare
each stage's store count.** A FIFO whose stages do not all store the same number
of times has lost a word to a collision.

**REACH.** Measured in detail on one block. The mechanism is general to any
multi-cell block with a shared uniformly-faced conveyor carrying traffic in both
directions — reorder buffers, shift chains that spill backward, systolic arrays
with a reverse accumulation path, any FIFO whose fill and drain share a row.
The head-on static check is general to every multi-cell block.

**Gated by:** `verification/tests/test_chacha20_fixed_tap_ring.py` —
`test_no_two_block_cells_rest_facing_each_other` (RED at the time this entry
was written, reporting `[('bufA3', 'out')]` — the real defect, correctly; GREEN
since the 2026-08-30 re-fold, see the addendum below) with its INV-4
negative `test_the_head_on_gate_catches_a_facing_pair`.

**Related:** INV-48 (the forwarding rule this follows from), INV-52/INV-55
(the face and sealed-band rules — INV-55 rule 2 says a uniformly-faced band
seals what is BEYOND it; this says the band itself cannot carry both
directions), INV-19/INV-20 (the serialize-LOCK, which is the existing remedy for
a different contention class — reconvergent fan-in — and is NOT what this needs).
### ADDENDUM (2026-08-30, pass 9) — the fix LANDED, and it took BOTH shapes

The block is `done`: all sixteen RFC 8439 §2.3.2 words, bit-exact and in
order, on the built chip, `stop_reason == "QueueEmpty"`, and the head-on gate
(`test_no_two_block_cells_rest_facing_each_other`) is GREEN. Three sharpenings
of this invariant, all measured on the way:

1. **Shape (b) alone is NOT sufficient.** With every spill moved onto its own
   corridor (row 1 relay pads), the chip still wedged in a **FOUR-cell
   circular wait** `add3(N) -> bufB3(E) -> bufA3(S) -> out(W) -> add3` — the
   head-on pair's N-cell generalisation. Disjoint corridors can still form a
   CYCLE of faces, and the cycle fills the moment the two waves overlap in
   time. Shape (a) — the lap-close baton sourced from the END of the store
   wave (`bufA3`, after its spill hand-off) — is what closed it: the release
   is then causally later than every spill. **When a fold has any face cycle,
   apply (a) as well as (b); the static 2-cycle check does not see N-cycles.**

2. **A port word is NOT consumed independently of the cell it transits.** An
   egress one cell away from the chip's output-port cell bursts every port
   word THROUGH whatever block cell owns that port cell, and the word waits on
   that cell's queues — measured as the exact two-cell circular wait rule 3
   predicts (`out` at (9,1) holding north at `bufA3`'s cell while `bufA3`
   held south at `out`; zero words on the wire). **The egress of a block that
   covers its own output-port cell must BE the port cell.** From there the
   write leaves on the chip edge and touches no other cell, and a
   single-waypoint egress route never re-faces the cell (the route patch
   faces an exit cell toward its first waypoint otherwise).

3. **The store-count signature is now a live gate**, not just a diagnostic:
   the on-chip gate asserts every stage stores 4/4 and releases 1/1, so a
   reintroduced two-wave collision fails with the same reading that found it.

**Gated by (updated):** `test_chacha20_fixed_tap_ring.py` — the head-on gate
GREEN with its INV-4 negative, the 16-word ordered on-chip value gate, the
store-count signature, and the walk-by-walk fold gate over the final 51-cell
geometry (spill corridors, port-cell egress, `bufA3`'s hop-21 lap baton).

---

## INV-57 — `MUL`/`MULHI` are SIGNED, so an exact unsigned 16x16->32 product needs BOTH operands under 2^15

*(Number assigned at landing.)*

Found 2026-08-29 building `Poly1305MACBlock`. **MEASURED on a real placed +
routed + built chip**, not read out of a table.

**THE RULE.** PROGRAMMING_GUIDE §4.4 describes `MUL` as "the low 16 bits" and
`MULHI` as `(A x B) >> 16`, which reads as an unsigned pair. It is not: both
are halves of the **SIGNED** 32-bit product.

| x | `x * 0xFFFF` measured | unsigned truth |
|---|---|---|
| `0x0001` | `0xFFFFFFFF` | `0x0000FFFF` |
| `0x0002` | `0xFFFFFFFE` | `0x0001FFFE` |
| `0x7FFF` | `0xFFFF8001` | `0x7FFE8001` |
| `0x8000` | `0x00008000` | `0x7FFF8000` |
| `0xFFFF` | `0x00000001` | `0xFFFE0001` |

Every row is the signed product exactly. With **both operands in `[0, 0x7FFF]`**
the same pair is exact AND equals the unsigned product (verified across the
range, e.g. `0x7FFF * 0x7FFF -> 0x3FFF0001`).

**WHY IT MATTERS BEYOND ONE BLOCK.** Any block doing extended-precision
*integer* arithmetic (as opposed to Q15 DSP, where operands are signed by
construction) has to keep every multiplicand non-negative in 16 bits, and that
constraint propagates all the way back into the **choice of representation**.
For Poly1305 it eliminated the textbook radix outright: the fold `2^130 = 5`
needs a radix dividing 130 (only 2, 5, 10, 13, 26), and radix `2^26` has 26-bit
limbs while radix `2^13` produces a folded coefficient `5*r[j]` reaching
`0x9FFB` — both outside 15 bits. Radix `2^10` with 13 limbs is the largest that
survives. A plan written against the usual five-limb decomposition is therefore
not implementable here, and no amount of code reading would have said so.

**COROLLARY, also measured:** the carry survives a `{write}` between `ADD` and
`ADC`, not only a `MOVE` — `WRITE` is flag-preserving too. Only ALU ops clobber
the flags. But `MULHI` **is** an ALU op, which is the trap in the next entry.

**SAY WHICH LAYER.** Hardware/ISA — permanent.
**REACH.** The signedness is a property of the ALU and applies to every block
using `MUL`/`MULHI`. The radix conclusion is specific to Poly1305's modulus; the
*method* (let the 15-bit multiplicand rule pick the representation) is general.

**Related:** INV-45 (multi-word arithmetic pricing), INV-13 (the Q15 saturation
idioms multi-word integer code must NOT inherit), INV-34 (the sibling case of an
ISA claim that had to be measured rather than read).

---

## INV-58 — A 32-bit MAC must compute the HIGH half FIRST; and a SYSTOLIC stage cannot adopt and forward in one entry

*(Number assigned at landing.)*

Found 2026-08-29 building `Poly1305MACBlock`'s 13-cell multiply ring. Two
independent rules, both measured on a real placed + routed + built chip, both
producing answers that are wrong in ways a casual gate cannot see.

### 1. `acc += a*b` on a hi/lo pair is SEVEN instructions, and the order is load-bearing

INV-45 prices a 32-bit `ADD` at 4 instructions and notes that the park between
`ADD` and `ADC` may be a flag-preserving `MOVE`. A 32-bit **MAC** is not that
shape, because the high half comes from `MULHI` — **an ALU op, which sets all
flags and therefore DESTROYS the carry the `ADD` just produced.**

```
WRONG (6):  MUL c,a / ADD R0,lo / MOVE lo,R0 / MULHI c,a / ADC R0,hi / MOVE hi,R0
RIGHT (7):  MULHI c,a / MOVE t,R0 / MUL c,a / ADD R0,lo / MOVE lo,R0
            / ADC t,hi / MOVE hi,R0
```

**MEASURED:** the wrong order carries a constant `+0x10000` error from the first
accumulation onward, **while the low word stays bit-exact** in every one of six
successive MACs — so a gate that checks one word, or only the low half, sees
nothing. Computing the high half first and parking it keeps
`ADD` and `ADC` adjacent in flag terms. Verified exact over six successive
accumulations; the right order then held 13/13 accumulators bit-exact over 11
cases including the all-maximum corner.

### 2. A systolic stage's READ and WRITE must be separated by a full sweep

A rotating datapath — cells that each hold a value, compute on it, and forward
it to their successor — invites the obvious cell program:

```
adopt the predecessor's value  ->  compute  ->  forward to the successor
```

**That is wrong in ALL SIX permutations of those three steps, in BOTH trigger
orders** (twelve variants, enumerated exhaustively against the reference; zero
correct). A cell entry is **atomic**, so whichever cell runs second in a pass
already sees the first cell's forward, and one value sweeps the entire ring in a
single pass instead of advancing one position. On chip this presents as every
cell's register holding the *same* value.

**The fix is to STAGE the sweeps as two separate entries**, fired by two
separate fan cells:

```
sweep 1 (entry `mac`)   :  acc += c * a ;  successor's a_in <- a
sweep 2 (entry `adopt`) :  a <- a_in
```

The whole ring finishes sweep 1 before any cell runs sweep 2, so no cell can
observe the current pass's forward. Verified over 500 random inputs.

**And BOTH sweeps must fire in REVERSE ring order.** Staging alone still left
**twelve of thirteen cells exact and the wrap cell exactly one pass stale**: a
cell's `JUMP`s are issued in program order, but the substrate is asynchronous,
so "later in the entry" is not "later in time" at a distant cell — the closing
write around the ring landed after the first cell's adopt. Firing the ring
backwards puts the longest-latency trigger first.

**Three corollaries, each measured after a wrong answer:**

* **A per-edge fix-up CELL cannot be ordered between the two sweeps.** A
  dedicated cell applying a constant on the ring's closing edge had its write
  land one pass late no matter which cell fired it (tried from the ring's last
  cell, from the MAC fan, and from the adopt fan). Fold such work into the
  producing cell's **forward**, where it becomes an ordinary sweep-1 write.
* **The egress must not ride a compute cell.** Folding the drain onto one MAC
  cell (two `is_face` words plus a flip-and-restore) shifted that cell's
  register map and left **its accumulator, and only its accumulator, wrong**.
  Twelve-of-thirteen is exactly the shape a one-value gate cannot see.
* **The collector must not sit ON the ring.** Placed there it lands on the
  coefficient BROADCAST walk; the two fan cells then disagree about which walk
  position is which cell (one skipped distance 8, the other 9) and the collector
  was triggered during a compute sweep, emitting the coefficient. INV-52 clause
  2 from the other side: a cell on a walk is not neutral.

**SAY WHICH LAYER.** Both rules are hardware — (1) is the ALU's flag behaviour,
(2) follows from atomic cell entries and asynchronous delivery. Permanent.

**REACH.** (1) applies to every 32-bit multiply-accumulate on this ISA. (2)
applies to any block whose datapath is a rotating line of cells that both
consume and forward the same value — systolic convolutions, ring accumulators,
shift-register folds. Measured on one 13-cell ring, over 11 input cases; the
mechanism is in the shared execution model.

**Related:** INV-45 (the multi-word pricing this extends to MAC), INV-33 (the
register/overlap contract — the static gate caught the wrap cell one word over,
twice), INV-46 (more cells doing less, which is why the fan is two cells and the
egress is its own), INV-52 (the face/walk rules the collector violated), INV-54
(a value gate must cover more than one element — met here as twelve-of-thirteen).

**Status note (2026-08-30, reconciled with INV-64):** the systolic ring this
entry's rule 2 was measured on is Poly1305's PASS-1 architecture; the SHIPPED
`Poly1305MACBlock` (`done`, 100 cells, RFC 8439 §2.5.2 tag exact on chip,
gated by `test_poly1305_mac.py`) was rebuilt ALL-SERIAL on one conveyor cycle
— INV-64 §1 — precisely so that this whole concurrent-sweep hazard class
cannot occur. Rule 1 (the seven-instruction MAC order) is in the shipped
block; rule 2's measurements stay real and binding for any design that DOES
run concurrent sweeps.

---

## INV-59 — A PROGRAMMED cell on a broadcast walk stops the sweep; only a face-only `transit_*` is transparent

*(Number assigned at landing — authored while main's highest entry was INV-56.)*

Added 2026-08-30 from `Poly1305MACBlock`'s normalise ring. Measured on a real
placed + routed + built chip.

**THE RULE.** A cell that broadcasts to N downstream cells does it with N
hop-counted `WRITE`/`JUMP` pairs down ONE walk (that much is proven and
useful — see the sibling entry on the signed multiplier). Every cell the walk
crosses forwards the word (INV-48). **But a cell with a PROGRAM does not
forward a trigger past itself the way a face-only relay does.** Put one on the
walk and the sweep stops there.

**MEASURED.** A 13-cell ring driven by fan cells, with the collector placed ON
the ring at walk distance 11: cells at distances 4..10 ran, and **cells at
12..17 were never triggered at all**. Replacing the collector with a face-only
`transit_*` cell at the same position, and hanging the collector off the walk,
made all thirteen run. The build's own resolved jumps were correct in BOTH
layouts — the hop numbers skip the transit correctly — so nothing in the
bitstream looks wrong.

**AND THE TWO FAILURE MODES LOOK DIFFERENT ON DIFFERENT INPUTS.** The same
defective layout reported `stop_reason == "Deadlock"` for some seed values and
`"QueueEmpty"` for others. This is why INV-56's rule ("read `stop_reason`
first") needs its corollary: **read it for EVERY case, not one**, because a
layout fault can present as a wedge on one input and as silence on the next.
*(2026-08-30: that corollary is now stated in INV-56 §1 itself, with this
entry as its measurement — one home, cross-referenced, per the evidence rule.)*

**SAY WHICH LAYER.** Hardware/toolchain boundary — the forwarding rule is
hardware; which cells get a program is the block's own layout. Fixable in the
block, always, by moving the programmed cell off the walk.

**REACH.** Measured on one 13-cell ring with two layouts. The mechanism is the
shared forwarding path and applies to any block that broadcasts along a walk
crossing its own cells.

**Related:** INV-48 (the forwarding rule), INV-52 (a cell on a walk is not
neutral — this is the same lesson for triggers rather than data), INV-56
(`stop_reason`, which this sharpens).

---

## INV-60 — Read the hop the BUILD resolved; a manhattan guess is a harness bug that presents as a dead block

*(Number assigned at landing.)*

Added 2026-08-30 from `Poly1305MACBlock`. This is INV-1's refinement, restated
because it cost real time again and because the fix is now a one-liner.

**THE RULE.** `BuildResult.chips[N].input_landings` gives the injection
parameters the build actually resolved:

```python
{'i': {'cell': (2, 0), 'entry': 25, 'hop': 28, 'data_addrs': [1]}}
```

Use them. A harness that derives the hop from `31 - (|dx| + |dy| + 1)` against
the block's *nominal* input cell is guessing at three things at once — the
landing CELL, the ENTRY address and the HOP — and the block's real landing may
be none of them.

**MEASURED.** With the manhattan guess (hop 30, the block's default entry) the
jump run processed **8 events** and no cell changed state — indistinguishable
by inspection from a dead datapath, a bad fold or a missing trigger, and it
survived a full disassembly of the fan cells because *the bitstream was
correct all along*. With the resolved landing (hop 28, entry 25) the same
bitstream processed **1445 events** and every cell ran.

**The diagnostic that separates it from a block bug:** `stop_reason` is
`QueueEmpty` and `events_processed` is TINY. A block bug typically runs the
events and computes the wrong value; a wrong landing means the work never
starts. Check the event count before suspecting the program.

**SAY WHICH LAYER.** Harness — always fixable, never a substrate limit.
`run_block_dut`/`run_block_dut_rate` already do this correctly; the trap is
only for hand-rolled drivers, which any block with a non-standard input
protocol needs.

**REACH.** The mechanism is in the shared build path and applies to every
hand-rolled driver.

**Related:** INV-1 (the placement-dependent hop, and its corridor-accurate
refinement), INV-6/11 (params-dependent entry addresses — the entry is
resolved for you here too), INV-56 (`stop_reason` plus the event count is the
pair that localises this in one run).

## INV-61 — Five silent failures a placed panel-backed block can have: a multi-region panel, a stale-flag branch, a stale FACE constant, an input landing off cell 0, and a fold whose edges all deliver but slowly

*(Number assigned at landing — authored by a parallel builder; parallel
builders cannot see each other's KB additions, and three numbering collisions
had already happened.)*

**Found 2026-08-30 building `LZ4EncoderBlock`, the first block to use TWO panel
regions at once.** Five separable rules, grouped because they share one property
that makes them expensive: **every one of them places, routes, DRCs clean and
BUILDS, and then does the wrong thing without raising anything.** Three are
invisible to any model-level gate; two are invisible to the static layout checks
as well. Each is stated with what it was measured to cost.

Quick index:
1. two panel regions that alias → a wrong answer of the RIGHT LENGTH;
2. a branch with no flag-setter before it → the wrong branch, silently;
3. an in-program FACE constant that outlived its fold → a run-time head-on
   deadlock the layout check cannot see;
4. an input landing that resolves off cell 0 → the whole first pass is a no-op;
5. a fold where every edge delivers but the walks are long → saturation, not
   deadlock *(this DIAGNOSIS was later DISPROVEN by trace — the fold-method
   rule survived; see the CORRECTED 2026-08-30 block after the gate list)*.

### 1. TWO REGIONS IN ONE PANEL MUST BE PROVEN DISJOINT. (toolchain — fixable, and now guarded)

INV-31 introduced the panel as the memory tier and INV-47 as the place where a
destination can be a DATA value. Both describe ONE table. A block that needs two
— here the stored input (`address == byte position`) and a hash table — is
partitioning a single 65536-word address space, and **`SramPanelDevice` wraps
every address modulo its size** (`engine/sram_panel.py`, `addr % size_words`).

**MEASURED:** with `window_words = 2**16` the hash table's base of 65536 aliased
onto history address 0. The encoder then read hash slots as input bytes and
produced a block that was **format-legal, the RIGHT LENGTH, and decoded to the
WRONG payload.** Nothing raised. This is exactly the shape INV-33 describes for a
cell whose state overlays its own instructions — an overlap that assembles,
places, routes and returns a wrong answer silently — one tier up.

**THE RULE:** a block declaring more than one panel region must (a) compute every
region base from its own parameters, (b) REJECT in its constructor any
combination whose regions do not fit disjointly in the panel, and (c) keep the
region-base arithmetic in ONE cell, so the partition is checkable in one place
rather than spread across every reader. In this block only the ADDR cell adds a
base; no other cell ever holds an absolute panel address.

**Corollary for `panel_requirements()`:** `words` must be the SUM of the regions,
not the largest one. A short `words` under-allocates and the wrap returns.

### 2. `MOVE` DOES NOT SET THE FLAGS. A branch after one reads whatever the last ALU op left. (hardware/ISA — permanent)

This is stated in `PROGRAMMING_GUIDE.md` §4 ("The non-ALU ops — `HALT`, `MOVE`,
`BR`, `WRITE`, `JUMP`, `LOAD` — do **not** touch the flags") and it is still the
easiest defect to write, because the sequence *reads* like a test:

```asm
    MOVE R0, R{state:mat}     ; load the value...
    BR.N  mzero               ; ...and branch on its sign   <-- WRONG
```

**MEASURED on a real chip**, twice in one block:

* the TOKEN cell's `BR.N` read the stale `N` from a preceding `CMP mat, f15`,
  which is negative for every `mat < 15`. The literals-only-tail path was
  therefore taken for EVERY ordinary match: `lit=3 mat=1` emitted `0x30` for
  `0x31`, `10/11` emitted `0xa0` for `0xab`, `14/14` emitted `0xe0` for `0xee`;
* the length-run engine's caller dispatch used `BR.GE` after a `MOVE`, so which
  caller it believed it had was whatever the last arithmetic left behind.

**THE FIX IS FREE:** `SUB Rx, Rzero` both loads the accumulator AND sets the
flags from the value itself, in one instruction — the same word count as the
`MOVE` it replaces. The same trick gives a cheap "unconditional" local branch
(INV-13 forbids a real `GOTO` near a `{write}`/`{jump}`): `SUB Rzero, Rzero` sets
`Z`, and `BR.Z` after it always fires.

**WHY IT MATTERS DISPROPORTIONATELY:** the Python model of a block has no flags,
so **no model-level gate can see this class of defect at all** — not the golden,
not the FSM twin, not a round-trip. Only the chip can. It is also silent: the
program runs to completion, `stop_reason` is `"QueueEmpty"`, and the output is
merely *wrong*.

**THE CHEAP STATIC CHECK**, which needs no chip:

```python
code = [l.strip() for l in cp.assembly_template.splitlines()
        if l.strip() and not l.strip().endswith(":")]
for i, ln in enumerate(code[1:], 1):
    if ln.startswith("BR.") and code[i - 1].startswith("MOVE"):
        raise AssertionError(f"{ln} reads the flags of {code[i-1]!r}")
```

It over-reports where a label lets control arrive from elsewhere, so pair it with
a per-block allowlist naming each exception and WHY the flags are known there —
not with deletion.

### 3. AN IN-PROGRAM FACE CONSTANT MUST BE DERIVED FROM THE LAYOUT. (authoring contract)

INV-52 clause 1 says every path must RESTORE the resting face. This is the
sibling failure: the path restores faithfully, **to the wrong face**, because the
`is_face` DataWord is a LITERAL that outlived the fold it was written for.

**MEASURED.** `LZ4EncoderBlock`'s OUT cell was copied from `LZ4DecoderBlock`,
whose OUT rests EAST, and `DataWord("face_rest", 1)` came with it — while this
block's fold rests OUT **WEST**. The cell restored itself to EAST after every
egress burst; its EAST neighbour rests WEST. The two ping-ponged a single WRITE
forever, its hop count climbing 22 → 31, and the run reported
`stop_reason == "Deadlock"` after emitting only the first byte of the sequence.

**Why the static head-on check does not catch it:** INV-56 clause 3 walks
`default_layout()`, and the LAYOUT is innocent — it has no head-on pair. The
PROGRAM created one at run time. Both checks are needed, and neither subsumes the
other.

**THE RULE:** never write a face code as a literal. Derive it:

```python
_FACE_CODE = {"south": 0, "east": 1, "west": 2, "north": 3}

def _resting_face(self, cid):                 # a cell's own resting face
    return _FACE_CODE[str(self.default_layout()[cid][2])]

def _face_to(self, src, dst):                 # the face pointing src -> dst
    ...                                       # from the two placed positions
```

Then a re-fold updates every constant automatically, and a copy-paste from
another block fails loudly (the cells are not on a common row/column) instead of
silently deadlocking. Keep `default_layout()` free of any call back into
`build_cell_programs()` so the helpers cannot recurse.

### 4. THE INPUT LANDING CELL MUST BE CELL 0. (toolchain — fixable)

The corridor and the landing are resolved by two different mechanisms. The panel
template draws the `x16_in` corridor to `panel_requirements()["input_cell"]`,
but the HOST-INJECTION LANDING comes from the catalog's PortMap, which derives a
block's external input from the FIRST cell's first input port — and
`bus_router._target_input_cell` falls back to `placement.cells[0]`. The two agree
only when the input cell IS cell 0.

**MEASURED:** with another cell at index 0, the landing resolved to THAT cell's
input register at THAT cell's position. Pass 1 was never entered; the chip ran
cleanly to quiescence (`stop_reason == "QueueEmpty"`, **not** a deadlock) and
committed ZERO panel writes, while placement, routing, DRC and the build all
reported success. A block whose ids are chosen for INV-53's jump ordering must
therefore pin the input cell at 0 and derive the rest of the order around it.

Related, from the same pass: **a cell can satisfy the data/state budget and still
have no room for its INPUT register.** The resolver allocates state into
`range(max_data_addr + 1, 31 - instr_count)` and inputs into what is LEFT, so the
failure appears only at build time as `No register space for input 'x'`. Pack
data addresses with NO GAPS (state auto-allocates above `max_data_address`, so a
hole below it is lost) and put the input count in the budget gate.

### 5. "EVERY EDGE DELIVERS" IS NOT ENOUGH — WEIGHT EDGES BY HOW OFTEN THEY FIRE. (fold method)

A fold score that asks only *"does this edge reach its target?"* accepts a RING,
because a ring delivers everything — eventually, at up to `ring_length − 1` hops.
INV-51 clause 1 already says a ring traps its interior; the sibling cost is that
**a ring turns every hop into modular arithmetic**, so two physically ADJACENT
cells can be 11 hops apart and the router will correctly compute 11. Under a
per-sample inner loop those long-haul words saturate the ring and the block runs
to `stop_reason == "EventLimit"` with no output — produced, not wedged.
*(SUPERSEDED 2026-08-30 — this "ring saturation" reading of the `EventLimit`
was DISPROVEN by an execution trace on the same build: the real cause was an
infinite re-probe from two PROGRAM omissions — no `i += 1` on the miss path,
and no hash-table INSERT ever issued. The hop arithmetic above is still true
and the fold-method rule below still stands; the failure attribution does not.
See the CORRECTED block after the gate list. The block is `done`: 15 cells +
panel, every payload class including the scan loop byte-exact on chip.)*

**MEASURED, including the negative result.** Adding a max-hop term to the fold
score (penalise any edge over K hops) is the obvious fix and it is NOT
sufficient: over ~500 annealing restarts across three slot shapes at K = 6 and
K = 7, `LZ4EncoderBlock`'s best fold still needed **11 hops** for some edge. The
long walks are forced by the EDGE GRAPH — 36 distinct cell-pair edges over 14
cells with four 4-way hubs — not by a weak search.

**THE RULE:** when a fold will not shorten, stop re-folding and **shrink the
graph**. Count each cell's in- and out-degree first; a cell written by five
others or writing four is a hub, and hubs are what force long walks. Then:

* **weight the score by FIRING FREQUENCY.** A per-sample inner-loop edge and a
  once-per-frame setup edge are not equally important, and treating them alike is
  what lets a search "solve" the wrong problem;
* **push a dispatch INTO its consumers.** A cell that exists only to fan one
  arriving word out to N consumers has N expensive edges; if each consumer can
  decide for itself whether the word is its own, that collapses to ONE broadcast
  walk. This is INV-46's "prefer more cells doing less" applied to EDGES rather
  than to instructions;
* **check whether two cells can merge** — but MEASURE it: the merge that would
  have removed five of these 36 edges is 41 instructions against 31 words.

**SAY WHICH LAYER.** (1) is a toolchain/authoring contract; the wrap is real and
correct hardware behaviour, what is missing is a guard, and this block now
carries one. (2) is hardware/ISA and permanent. (3) is an authoring contract —
the substrate behaviour is correct, the constant was stale. (4) is toolchain, in
`bus_router._target_input_cell` and the PortMap derivation; it is fixable there
(resolve the named port against every cell) and is a block-authoring contract
until then. (5) is fold METHOD.

**REACH.** (1) measured on one block, but the mechanism is in the shared
`SramPanelDevice` and applies to any block declaring more than one region. (2)
measured twice on one block; the flag rule is in the ISA and applies to every
cell program ever written.

**Gated by:** `verification/tests/test_lz4_encoder.py` —
`test_the_two_panel_regions_never_overlap` (the constructor guard, both
directions), `test_no_branch_reads_the_flags_of_a_MOVE` with
`test_INV4_the_stale_flag_checker_sees_a_planted_defect`,
`test_INV4_a_stale_flag_branch_in_the_token_cell_is_CAUGHT` (an INV-4 negative
that re-introduces the real defect ON CHIP and asserts the gate sees it),
`test_the_input_landing_cell_is_CELL_ZERO` +
`test_the_portmap_resolves_the_external_ports_to_the_right_cells`, and
`test_every_cell_fits_a_32_word_cell` (which now checks the INPUT registers too).

### CORRECTED 2026-08-30 (the block's second pass) — clause 5's DIAGNOSIS of the
### scan-loop failure was wrong, though its method survived

Clause 5's failure story ("the per-byte inner loop saturates the event budget";
elsewhere recorded as "RET's `to_hash` is sized 11 and walks out of the block's
footprint") was **disproven by an execution trace on the same build**:

* `to_hash` at hop 11 landed EXACTLY on HASH — the ring walk was sized right
  and delivered right (the trace shows `hop_cnt=31 action=execute_locally` at
  the HASH cell after eleven forwards);
* the `EventLimit` was not saturation. It was an INFINITE RE-PROBE of position
  0: the same four bytes and the same empty table slot, forever (RET dispatched
  1596 times; MATCH executed ONCE — that one execution was its `setstop`
  delivery). Two PROGRAM OMISSIONS caused it, both invisible to every static
  gate because the tail path never runs them: the no-candidate miss jumped
  `seq.step` with **no `i += 1`** (the model's advance had no on-chip
  counterpart), and **no cell ever issued the hash-table INSERT** — the model's
  `pw(ht_base + h, i + 1)` simply was not wired, so every probe read EMPTY.

**What still stands, verbatim:** the fold-method rule (weight edges by firing
frequency; rare edges can be long), the measured annealing dead end, and the
hub analysis. The second pass's fold applies it — the per-position edges ride
1–6 hop walks and the per-sequence formatter takes the long arcs — and the
suite pins the bounds (`test_the_fold_is_FREQUENCY_WEIGHTED_hot_edges_are_short`).
**The lesson the correction adds:** an `EventLimit` with plausible long hops in
sight invites a COST story, and a cost story cannot be told apart from a
non-termination story without counting EXECUTIONS PER CELL against the model's
expected counts — which the trace gives in one run (`enable_trace()` +
a Counter over `exec_tick` events). Both program omissions are re-introduced as
ON-CHIP INV-4 mutants in the block's suite and proven caught.

**Related:** INV-33 (the overlap contract this extends to the panel), INV-31 and
INV-47 (the panel tier), INV-13 (no unconditional GOTO — the same flag-discipline
family), INV-4 (both gates carry proven negatives).

*(Navigation fix 2026-08-30: an earlier pointer here said the correction block
sits "above the gate list" — it does not; it sits directly BELOW the gate list,
immediately above this line. Nothing else about it changed.)*

## INV-62 — The panel-port corner has exactly TWO reachable client slots; a second client can speak the RAW panel protocol THROUGH the controller if its commits are DEFERRED one transaction; and the panel bridge can drop an ack release on a run boundary

*(Number assigned at landing — authored by a parallel builder.)*

**Found 2026-08-30 finishing `LZ4EncoderBlock` (its second pass), the first
block with TWO on-chip panel clients.** Every claim is measured on a real
placed + routed + built chip; the wedges each name the payload length that
produced them, because the same program passed at other lengths.

### 1. A pinned port-corner controller is reachable from exactly two slots. (geometry — permanent for this chip shape)

The `x1_out` controller sits in the fabric's corner. A word reaches it only if
some cell's face points AT it, and a word transiting any other cell leaves on
that cell's own face (INV-48) — so the client slots are exactly:

* the abutting same-row cell, emitting on a FACE FLIP toward it; and
* the cell DIRECTLY ABOVE it, resting toward it.

And the above-slot is itself **fed only by its west neighbour's flip**: every
walk that could arrive there from elsewhere is forwarded away first (checked
over every face of every other cell of the fold). Consequences that shaped this
block: the hash-table port cell (INS) is pinned at the above-slot, its ONLY
trigger source is the cell west of it (HASH), and nothing else — not the
return cell, not the sequencer — can ever reach it. A design that needs a
trigger into that corner from anywhere else does not have a routing problem;
it has an impossible edge, and must restructure (here: HASH carries the whole
table hand-off, and the return needs no phase — see 4).

### 2. A block cell may speak the RAW panel register protocol through the controller. (protocol idiom — measured)

The above-slot client emits literal `WRITE @panel_hop+1, <panel reg>` /
`JUMP @panel_hop+1, <trigger>` words. They TRANSIT the programmed controller
cell — a transiting word is forwarded on the cell's face, straight out the
port; only a LANDING word executes — and the port serializes them in emission
order. Three register-file facts make the client small:

* the panel's R5 (address) persists until rewritten;
* R3/R4 (the push-read descriptors) are rewritten by EVERY controller read
  with the same build-time constants, so a client that only ever runs after
  at least one controller read never writes them;
* a read followed by a commit at the SAME address needs no second R5 write.

Measured: `LZ4EncoderBlock`'s table client does lookup + insert in 6 port
words and 11 instructions, byte-exact across 12 payload classes. The
controller executes NOTHING for table traffic — which is the point, see 3.

### 3. NEVER leave protocol words in flight when a push-read's return can spawn new port traffic — DEFER the commit. (authoring contract — measured twice)

Two wedges, same shape, different distances:

* routing the table transaction through the controller's own entries
  (`lookup` then `set_addr` then `write`, three jumps from one activation)
  wedged when the second jump's words arrived while the controller was
  stalled MID-PORT-BURST on the lookup's held-ack handshake — n=492 died at
  scan position 3 while n=416 completed, the identical program;
* emitting the commit pair right after the raw read trigger wedged when the
  slot's RETURN (→ dispatch → a HIT → the match engine's first read) reached
  the controller while the commit words were still stalling through the
  held-ack port — n=492 died at position 125, every smaller case passed.

**The rule:** after a client issues a read trigger it must emit NOTHING until
the read's return has been consumed. A commit that must follow a read of the
same address is DEFERRED to the head of the client's NEXT transaction — which
preserves the panel-visible order exactly (`read a_p, write a_p, read a_p+1`
is the model's own sequence), at the cost of the FINAL transaction's commit
never landing (harmless when nothing reads the region after the loop ends —
say so and gate it). The deferred client then has a natural quiescent gap of
a full round trip between its bursts.

### 4. A deterministic read schedule can replace a phase wire. (design idiom)

The block's single push-read return point told MATCH/LITS reads apart by an
explicit phase the history port writes — but the table client CANNOT write a
phase (clause 1: it has no walk to the return cell). The scan's schedule is
rigid — every position is exactly four history reads then one table read,
single-threaded, single-outstanding — so the return cell COUNTS: ph==0
returns decrement a counter seeded 5; the fifth is the slot; dispatching it
reseeds. Self-repairing (reseeded at the identified element), and cheaper
than the wire it replaces. The count seed is gated by an on-chip INV-4 mutant
(seed 4: the fourth history byte dispatches as the slot; caught byte-exact on
a compressible payload — on a mostly-literal payload the degraded all-literal
output EQUALS the model and proves nothing, which is why the mutant names its
payload).

### 5. The in-fabric panel bridge can DROP an ack release on a run boundary. (simulator layer — recover in the harness)

With `chip.register_panel(...)` (the self-pumping API), a
`chip.run(max_events=N)` boundary landing inside a port-capture window can
leave the held ack unreleased forever: the chip drains to QueueEmpty
mid-burst with `any_panel_ack_pending()` True, while the DEVICE already holds
the captured word (its register file shows it). Evidence it is the boundary:
sweeping `max_events` over 256/257/1024/8192/65536 MOVED the wedge (8192
produced 56 bytes before wedging; the others zero). Recovery, lossless
because the capture already reached the device: when the chip is idle with a
panel ack pending, `release_output_ack(<panel out port>)` — the exact
recovery `PanelDriver.step` carries for its own missed-capture timing window.
Layer: the closed simulator binary's bridge; the harness idiom is the fix
available to a block suite.

**SAY WHICH LAYER.** (1) is chip-shape geometry plus INV-48 — permanent for
the corner-pinned panel template. (2) and (3) are protocol/authoring idioms on
permanent substrate behaviour. (4) is a design idiom. (5) is a simulator
bridge defect, recoverable in the harness.

**REACH.** All measured on one block (the first two-client panel block), but
(1) is forced by the port geometry for every panel-backed block, (3)'s rule is
the panel protocol itself, and (5) was reproduced at will by the max_events
sweep and is independent of this block's program.

**Gated by:** `verification/tests/test_lz4_encoder.py` — the
frequency-weighted fold gates, the three ON-CHIP INV-4 mutants
(`test_INV4_ONCHIP_*`), the byte-exact whole-design gates over 12 payload
classes, and the independent-decoder acceptance gate.

**Related:** INV-48/INV-52 (the forwarding and face rules clause 1 follows
from), INV-31/INV-47 (the panel tier), INV-61 (the same block's first-pass
hazards, and the corrected diagnosis), INV-56 (read stop_reason first — this
pass adds "and count executions per cell against the model").

## INV-63 — An EXIT cell's highest-addressed WRITE/JUMP are contested by TWO build passes: a declared backward jump and the external port pair cannot share a cell; RAW literals with build-time-resolved operands are the escape

*(Number assigned at landing — authored by a parallel builder before its number
existed.)*

Found 2026-08-30 finishing `ChaCha20KeystreamBlock`, whose egress must sit ON
the chip's output-port cell (INV-56 addendum) and ALSO relay the pair-3 spill
one hop west. Every claim measured on a real placed + routed + built chip.

**THE MECHANISM.** Two independent build passes each rewrite instructions of a
block's OUTPUT exit cell, selected BY ADDRESS:

* `_apply_internal_feedback` resolves a DECLARED backward internal jump by
  rewriting the cell's **highest-addressed JUMP** (INV-53);
* the output-port sink fixup rewrites the **highest-addressed WRITE and JUMP**
  to the port hand-off when `_output_cell_carries_handoffs(...)` is true
  (the cell sources an `internal_connections` edge or holds an inline
  `WRITE.CFG`) — and otherwise rewrites **EVERY WRITE and JUMP in the cell**.

Three corollaries, each of which was hit:

1. **A declared backward jump and the port pair cannot coexist in one cell.**
   Both passes demand the highest slot; no entry order satisfies them
   simultaneously. (Ordering the port pair last protects it from the feedback
   pass only until a backward edge is declared, and vice versa.)
2. **The FULL-CELL rewrite catches authored literals too.** With no internal
   sources on the cell, the sink fixup rewrote the spill relay's literal
   `WRITE @1, r` / `JUMP @1, e` into `@1, 0` port hand-offs: the relayed words
   landed in the target's R0 and the trigger aimed at entry 0 — the target
   cell executed ZERO times while the relay ran four, with nothing anomalous
   anywhere else.
3. **An exit cell with NO internal sources also keeps the ROUTE's face**, not
   its authored one: `_reassert_internal_forward_faces` restores authored
   faces only on cells that source an `internal_connections` edge. A transit
   or relay duty that depends on the egress cell's resting face silently
   breaks (measured: the spill transit bounced straight back to its sender on
   `exit_face N`).

**THE ESCAPE — the panel family's `RAW_OUTPUT_HOPS`, plus one discipline.**
`RAW_OUTPUT_HOPS = True` opts the block out of the sink fixup AND of the
route-facing of its exit cell, so the cell's authored program and authored
resting face both survive. The block must then author the port pair itself —
read the correct encoding off what the build's own patch resolved BEFORE
opting out (for `x16_out` from its own port cell: `WRITE @1, 0` /
`JUMP @1, 0`, emitted on the port-edge face) — and author the relay's
literals with operands RESOLVED at `build_cell_programs` time from the target
cell's real program (`CellProgramResolver.compute_entry_addresses` + its
declared input registers), never hand-typed: entry addresses are
params-dependent (INV-6/11) and a literal that is not derived rots.

**SAY WHICH LAYER.** Toolchain (`placekyt/engine/build.py` — the sink fixup,
the feedback pass, `_reassert_internal_forward_faces`); permanent until those
passes match on authored intent rather than address. Until then this is a
block-authoring contract, cheap to honour once known and invisible until hit.

**REACH.** Any block whose output exit cell also does internal work — relays,
spill turns, feedback sources — and any block that must place its egress on a
port cell. The panel-backed family (`LZ4DecoderBlock`, `CWKeyerBlock`,
`VaricodeDecoderBlock`, `GolayDecoderBlock`, …) already carries the flag for
its own reasons; this entry records WHY the flag is the right tool for a
non-panel block too, and the full-cell-rewrite failure mode that forces it.

**Gated by:** `verification/tests/test_chacha20_fixed_tap_ring.py` — the
on-chip value gate (whose 32 in-order words transit the authored literals),
the store-count signature (which fails when the relay's literals are
clobbered — the measured `bufB3 == 0` reading), and the second-batch gate.

**Related:** INV-53 (the highest-address resolution rule this collides with),
INV-56 (the deadlock campaign this closed), INV-50/INV-52 (authored faces vs
the router's/build's), INV-6/11 (why the literals must be derived), INV-38
(the report emitted by the gate that proved all of this). **Cross-reference
added 2026-08-30:** INV-64 §2 is the WRITE-side sibling of this collision — a
flip-delivered declared backward WRITE re-patched with the resting-corridor
hop; INV-53 + this entry + INV-64 §2 are one family (three faces of the
build's rewrite-by-address passes) and were landed by different builders —
read all three together.

---

## INV-64 — A latency-tolerant block should be ALL-SERIAL on ONE conveyor cycle; and a FLIPPED backward write must be an AUTHORED literal, because the feedback pass re-patches it with the RESTING-corridor hop

*(Number assigned at landing — authored by a parallel builder while main's
highest entry was INV-63.)*

Found 2026-08-30 finishing `Poly1305MACBlock` (the queue's last block). Every
claim measured on a real placed + routed + built chip.

### 1. THE METHOD RESULT: if a block has no throughput requirement, build it as SERIAL CHAINS on ONE conveyor cycle. The whole INV-58 hazard class vanishes.

Pass 1 built the Poly1305 field multiply as a systolic ring with parallel
fan-fired sweeps, and spent most of its budget discovering and patching the
sweep-staging hazards that INV-58 records: a stage cannot adopt-and-forward
in one entry, sweeps must fire in reverse ring order, a fix-up cell cannot be
ordered between sweeps, a programmed cell stops a broadcast walk. Those are
all REAL — and all consequences of *concurrent* sweeps.

A MAC authenticates one message; nothing about it needs cell-level
parallelism. Rebuilt with **every phase a serial chain** — one cell executes
at a time and hands the baton forward — the hazard class does not have to be
solved because it cannot occur: read/write order IS program order. The 13
serial multiply passes, the normalise rounds, the split rounds, the probe and
the finish are all chains, and the coefficient rides the chain itself (each
cell forwards `c` with the baton), which deletes the coefficient fan
entirely.

The fold that makes control cheap is **ONE CONVEYOR CYCLE**: control row
(east) -> ring serpentine -> closure (bottom tail + column 0 north) -> back
into the control row. Then:

* every sequencer-to-chain-head injection is a plain hop-counted write on
  resting faces (the ring heads sit just downstream of the control row);
* every end-of-chain return rides the closure into small relay cells that
  jump back down into the sequencers — each relay carrying exactly ONE
  backward jump, kept highest-addressed (INV-53 satisfied by construction);
* long control edges that exceed the 31-hop field are split at 2-instruction
  relay interlopers ON the cycle (INV-36), chosen so every segment is <= 30
  (measured: a direct edge that resolved at 32 failed the build; another at
  exactly 31 was kept off the boundary deliberately).

Cost: latency (~25k simulated events for a 1-word message). For a one-shot
authenticator that is nothing. REACH: any block whose value is a final
result rather than a sample stream — MACs, hashes, encoders run offline.
The systolic form is still right when throughput matters.

### 2. THE TOOLCHAIN RULE: a DECLARED backward write that is DELIVERED ON A FLIP gets re-patched to the RESTING-corridor hop. Author it as a literal.

INV-63 recorded that a declared backward JUMP and the exit-cell port pair
contest one instruction slot. This is the WRITE-side sibling, and it bites
ANY cell, not just the exit:

`_apply_internal_feedback` re-patches every connection that runs BACKWARD in
program order by tracing the corridor between the cells — **on resting
faces**. For an edge whose delivery is a hop-1 FLIP (`MOVE [FACE], …` +
`WRITE @1`), that trace is the wrong path by construction. Measured: the
limb post `mulC_k -> lh_k` (west-adjacent, flipped) was re-patched from the
authored @1 to **@21**; at run time the word left on the flip face with 21
hops on it, was forwarded straight back by the neighbour's resting face, and
the pair ping-ponged forever — `stop_reason == "Deadlock"` on the first limb
post, with the whole multiply already bit-exact behind it.

**The rule:** a flip-delivered edge to an ABUTTING cell is authored as a
literal `WRITE @1, <register>` with the register RESOLVED at
`build_cell_programs` time from the target cell's own pinned ports (INV-63's
discipline — never hand-typed), and the edge is deliberately NOT declared in
`internal_connections`. Forward flip edges and backward RESTING-face edges
are unaffected (the resting-corridor trace is exactly right for those — this
block's closure returns depend on it).

**Corollary, same family:** the router resolves a flip-emitted DECLARED edge
only through faces it can SEE — the resting face plus `is_face=True` DATA
words (`router._declared_flip_faces`). A face constant stored as a plain
DataWord is invisible; the router then walks the resting face around the
whole cycle (measured: resolved distance 99, build refused). Every face
constant must be `is_face=True` — which INV-23/INV-61.3 already require for
rotation, so the only way to hit this is to skip that rule.

**SAY WHICH LAYER.** (1) is design method. (2) is a toolchain contract in
`placekyt/engine/build.py::_apply_internal_feedback` (fixable there: skip
re-patching edges whose source instruction is inside a flip window, or match
authored hops for abutting pairs); until then it is a block-authoring
contract, cheap to honour once known.

**REACH.** (1) measured on one 100-cell block, but the argument is the
absence of a hazard class, not a property of Poly1305. (2) is in the shared
build path and applies to every multi-cell block with flipped hop-1
deliveries — the [mulA lh mulC mulB fin] x13 groups here carry 39 of them.

**Gated by:** `verification/tests/test_poly1305_mac.py` — the on-chip RFC
tag (which transits all 39 literal flip writes), the 425-edge walk gate with
its INV-4 negative, `test_flip_deliveries_are_abutting_pairs`, and the
INV-53 audit.

**Related:** INV-58 (the hazard class serial construction removes), INV-63
(the jump-side sibling and the literal-authoring discipline), INV-52 (flips
and the is_face machinery), INV-53 (why the closure relays exist), INV-36
(the hop-31 splits), INV-20 (the serialize-LOCK this block's input uses to
survive saturated drive).

## INV-65 — A full-width CHIP_SCALE block's I/O contract is SAME-PORT-FACING-EDGE at any span, not span-2 colocation

**Measured 2026-08-30** (recorded during the KB audit; the counterexamples are
four shipped, gate-green blocks). An earlier layout convention said a block's
input and output terminals must be COLOCATED (within ~2 cells) for the I/O
corridors to route. That is FALSE as a general statement, and was corrected in
`layout_rules.md` §1 with these counterexamples:

* **ChaCha20KeystreamBlock** (51 cells, 10x7, full-width): terminals NINE cells
  apart on the port row — `seq` at (0,1) taking `x16_in`, `out` ON the
  `x16_out` port cell at (9,0). Routes, builds, runs bit-exact.
* **GRUClassifierBlock** (full-width): terminals three apart.
* **Poly1305MACBlock** (100 cells, 10x10, full-width): same-edge, wide span.
* **FFT64Block** (84 cells, 9x12, NOT full-width): I/O on **opposite edges** —
  legal because a free column remains for the corridor.

The true contract, by layer and reach:

1. **Full-width block (spans the whole port edge):** both terminals must sit on
   the PORT-FACING EDGE, at ANY span along it. The corridor cannot go around a
   full-width block, so it must land directly on the edge — that is the real
   constraint the old "colocate" rule was a shadow of.
2. **Non-full-width block:** terminals may sit on different edges, opposite
   edges included, so long as a free row/column remains for each corridor.
3. **The output cell of a chip-scale fold wants to BE the port cell** where the
   geometry allows it (measured on ChaCha20: a port word bursting THROUGH a
   transited cell waits on that cell's queues — the two-cell wait of INV-56 —
   and moving `out` onto (9,0) was the fix; see INV-56 §port-cell rule).

LAYER: router/corridor geometry — toolchain behavior, but the shipped router's
actual measured behavior, not a guess. REACH: the four blocks above plus every
smaller block that colocates anyway; a fifth shape (full-width with terminals on
a NON-port edge) is untested and should be measured before use.

**Related:** INV-56 (the port-cell rule's deadlock mechanism), INV-40 (fold
shape vs free space), the corrected `layout_rules.md` §1 and checklist.

## INV-66 — What a 2P2S carrier link composes, measured: four per-stream leg shapes work; ONE source cell cannot both feed a far-chip block and emit a tagged transit stream; a RAW-literal egress is TAIL-ONLY

**Measured 2026-08-31** (number assigned at landing — renumber on collision),
during the `examples/secure_link` feasibility de-risk, on the real 4-die board
wiring (both carrier links, `MultiChipSimEngine`, the hosted server's own
`multi_chip_stream_targets` resolution). Gated by
`verification/tests/test_2p2s_fanout_spine.py`.

**What WORKS — all four per-stream leg shapes, word-exact, quiescent:**

1. head-block stream whose TAGGED egress transits the link AND the tail chip
   to the tail port (gain_2p2s stream-A shape);
2. cross-chip ingress transiting the head into a TAIL block (stream-B shape;
   the net ships a STUB route on the head-port cell — the auto-router refuses
   cross-chip nets by design, and the injection hop is composed, not routed);
3. a head-source EXIT net paired with a far-chip LANDING net (the fft128
   port-pair encoding) delivering into a far block;
4. several streams multiplexed over ONE link and ONE tail port, demuxed by
   `out_tag` (`read_port_words_timed`).

A 1→N fan-out is therefore REALIZABLE in the FPGA-mediated form: egress the
produced stream once (shape 1), and re-inject the recovered words as N
independent per-stream legs — the flowgraph is the fan-out. Proven end to end
with a tamper tooth (a flipped channel word surfaces in exactly its output
word).

**What FAILS, silently — two composition limits:**

* **A single source cell cannot carry BOTH a far-block arm and a tagged
  transit arm.** One splitter cell with two arms — one exit-net+landing-net
  pair to a far-chip block, one tagged egress meant to transit to the tail
  port — delivers ONLY the far-block arm; the tagged arm emits ZERO words.
  Build ok, routes ok, every settle `completed`. Measured identically in both
  net-declaration orders; inserting a relay splitter so each arm leaves its
  own cell degrades further (untagged zero words at the tail). The inter-chip
  hop patch owns the exit cell's trailing writes and the tagged arm loses.
* **A `RAW_OUTPUT_HOPS` egress must be the chain TAIL.** Its authored port
  literals are valid only for its own die's port geometry; fed across the
  link into a routed far-chip landing the stream arrives mangled (measured
  with ChaCha20Keystream as head: 36 words for a 32-word burst, mostly zeros,
  one recognizable keystream word). Corollary: every RAW-egress block
  (ChaCha20Keystream, Poly1305MAC, LZ4Encoder, LZ4Decoder) can only sit where
  its port words are READ, never relayed onward — and ChaCha20Keystream,
  whose `out` cell IS its die's output-port cell, additionally monopolizes
  that whole chain's egress (nothing else on its chain can reach the FPGA).

LAYER: build inter-chip hop patch + link relay — toolchain behavior, measured,
not a substrate law. REACH: every multi-chip example with fan-out, and any
placement of a RAW-egress block off the chain tail. The known-limit guard in
`test_2p2s_fanout_spine.py` FAILS the day the dual-role limit is lifted — that
is the signal to move fan-outs on-chip.

**Related:** INV-32 (corridor/broker deadlocks), INV-36 (hop field), INV-42
(`output_words` symmetry), INV-56 (quiescence idiom), INV-63 (RAW literals).

---

## INV-67 — A LOCK-rendezvous chip reports `stop_reason == "Deadlock"` for an arbiter-HELD word MID-GROUP while perfectly healthy; the diagnosis-bearing read is the run AFTER the group completes

*(Number assigned at landing — authored by a parallel FOC-wave builder while
main's highest entry was INV-66.)*

Found 2026-08-31 building `ClarkeTransformBlock` (the N=2 LOCK-by-face join
family: DualFloatToComplex, FeaturePairJoin, XorJoin, TMRVoter, Clarke — and
every future N-arm rendezvous, the CordicRotate N=3 join included). Measured
on the real placed + routed + built two-arm chain.

**THE MEASUREMENT.** Drive the arms one word at a time and read
`chip.run(...)`'s `stop_reason` after EVERY injection (INV-56 rule 1). With
the in-lock-order drive (ia then ib) every run reports `"QueueEmpty"`. With
the out-of-order drive (ib first — exactly what the rendezvous exists to
survive) the sequence is:

    inject ib   -> stop_reason "Deadlock", completed False   (word HELD)
    inject ia#1 -> stop_reason "Deadlock", completed False   (still held)
    inject ia#2 -> stop_reason "QueueEmpty", completed True  (pair emitted)

and the emitted stream is EXACT. The `"Deadlock"` runs are the arbiter doing
precisely its job: a word delivered to the barred face is held by the LOCK,
the sender's handshake stays open, and a run that ends with any held word
reports `Deadlock` — `completed` is `False` and the word count is 0, the
same three signals a genuine wedge shows (INV-56: neither signal a driver
checks tells them apart).

**THE RULE.** For a face-locking block, `stop_reason` is only meaningful
RELATIVE TO THE GROUP BOUNDARY:

* `"Deadlock"` while a group is OPEN (some arms delivered, others not) is the
  healthy hold signature. Do not chase it.
* The run that COMPLETES the group, and every drain after it, must report
  `"QueueEmpty"`. A `"Deadlock"` there is real — it is exactly how the TMR
  voter's re-lock bug and its one-group-in-flight wall present (INV-46 Rule
  3).

So a rendezvous suite should PIN both halves: assert the mid-group hold CAN
report `Deadlock` (so nobody later "fixes" the harness by treating any
Deadlock as fatal), and assert the post-group flush IS `QueueEmpty` (the gate
that catches a genuine wedge). `test_clarke_transform.py::
test_stop_reason_signature_of_a_healthy_rendezvous` is the model.

**COROLLARY (INV-4, mutation-gate teeth measured the same day):** a
substrate mutant that is GEOMETRY-PRESERVING (same cells, ports, faces —
a data-word value change, a template arithmetic edit) MUST place, route and
build; treating its build failure as "rejected, gate passes" makes the gate
vacuous. Measured: the first cut of Clarke's wrong-constant mutant assigned
to a frozen `DataWord` field, every build failed with `cannot assign to
field 'value'` — deterministically, at every anchor — and the mutation gate
read it as an unroutable-rejection pass without ever running the mutant.
(`DataWord` is a frozen dataclass: mutate with `dataclasses.replace` and
rebuild the `data` list.) Reserve the "did not route = rejected" reading for
mutants that genuinely change geometry (extra cells, spent faces).

LAYER: simulator run-report semantics + verification-harness discipline —
permanent. **Related:** INV-56 (read stop_reason first; the corollary that one
case's stop_reason is a sample), INV-46 (the LOCK-rotation family and its
Rule 3 re-lock deadlock), INV-19 (the lock IS the serialization at N=2).

## INV-66 — TWO SRAM-panel clients on one chip: word-level burst exclusivity, lookup-only addr_base, and the backed[0] refresh

**Measured 2026-08-31** while assembling `examples/lz4_stream` — the first
design with two READ-WRITE panel clients (LZ4EncoderBlock, 15 cells +
LZ4DecoderBlock, 8 cells) on one array. Three rules, each with a measured
failure behind it:

1. **Two controllers' panel bursts must be TEMPORALLY EXCLUSIVE — chain the
   clients through the CLIENT, never an on-chip net.** The panel register
   protocol is single-outstanding per WORD (SRAM_PANEL.md §5), and the port
   cell merges inbound faces word by word — two controllers bursting
   concurrently interleave their `R2/R3/R4/R5` writes and trigger JUMPs at the
   merge, so one client's commit lands at the other's address. A direct
   encoder→decoder net would overlap the decoder's writes with the encoder's
   pass-2 scan. The per-sample paced server (the panel contract) makes a
   client-side hand-off exclusive BY CONSTRUCTION: the producer's whole run
   settles inside its injection before the consumer's first byte exists. The
   duplex Varicode transceiver was always implicitly protected the same way
   (two independent per-sample streams); this makes the rule explicit for
   PIPELINED clients.

2. **`SramControllerBlock.addr_base` relocates ONLY the lookup path** — the
   write counter always auto-increments from 0 (and the based variant omits
   `set_addr` entirely). So a READ-WRITE client cannot be moved to a based
   region: MEASURED, a decoder given `addr_base=36864` wrote `[0,len)` and
   read `36864+…` — literals fine, every match byte 0. A RW client keeps
   `addr_base=0` and shares `[0,len)` with the other client SEQUENTIALLY,
   which is sound exactly when the format guarantees append-before-fetch
   (every read is of an address the same client wrote earlier in the same
   batch). **The aliasing gate that certifies this** (INV-61 flavor): decode
   a stream whose content DISAGREES with the other client's leftovers — for
   the encoder's own stream, leftovers and decoded bytes coincide at every
   address, so a leftover-reading decoder passes the plain round trip.

3. **`refresh_panel_params` serves `backed[0]` only, and also rewrites the
   FIRST placed CrossoverBlock's `entry_a`/`hop_a` from `backed[0]`'s
   CONTROLLER-cell entry map whenever any xo→backed[0] connection exists.**
   MEASURED: with the encoder first and an input-relay crossover feeding it,
   the build silently re-aimed the relay's JUMP at the controller cell's
   entry — the stream landed on the ingest cell with the wrong entry and the
   chip ran to QueueEmpty having written nothing. Order the panel-backed
   blocks so backed[0] is the client whose return corridor matches the
   direct-landing formula (route ending one short of the return cell), and
   keep input-relay crossovers un-matchable by that refresh (no xo→backed[0]
   conn, or accept and pre-verify the rewrite).

Placement notes that made the two-client geometry work (all translation-only,
so every internal edge of both proven folds is preserved): the two
controllers' to-panel corridors MERGE same-direction into the port-exit cell
(a routing cell merges inbound faces onto its one exit face); the shared
`x1_in` and `x16_in` corridors FORK at CrossoverBlocks (pushes and injected
streams are per-track uniform, so a re-emitting track preserves them; raw
panel-protocol words are NOT uniform and may only TRANSIT a crossover on its
resting face, never land on a track).

LAYER: panel contract + build toolchain, all measured on the shipped
simulator and build. REACH: any future multi-panel-client design (encode+
decode pairs, MAC+cipher, any table pair with a RW side).

**Related:** INV-61 (aliasing returns a wrong answer of the right length),
INV-62 (the port corner's two client slots), SRAM_PANEL.md §5, the duplex
template in `engine/panel_pnr.py`.

---

## INV-68 — Batch-EVOLVING state (a block counter) is an OWNER register excluded from the reset spec plus end-of-batch writes into every baked COPY, each made where that copy sits at a KNOWN rotation

*(Number to be assigned at landing.)*

**Measured 2026-08-31** building `ChaCha20KeystreamBlock`'s
`counter_mode="increment"` (batch N must emit `block(key, nonce, counter+N)`
— RFC 8439 §2.4's per-block counter). Every claim below was proven on the
real placed + routed + built chip: seven consecutive batches bit-exact, the
0xFFFF→0x10000 carry seam, and three on-chip mutants.

**THE MECHANISM.** `reset_per_batch` restores by REGISTER at every packet
boundary, and a working datapath CLOBBERS its registers during the batch (a
row's slot registers hold post-drain garbage at the boundary; the boot state
exists only as reset VALUES). Two corollaries:

1. **Merely excluding a value's register from the reset is NOT persistence** —
   the register keeps the garbage, not the value. State that must EVOLVE
   across batches needs an OWNER: a StateVar pair in a cell whose program
   never overwrites it, excluded from the reset spec, advanced once per batch
   (32-bit-wide via `ADD`/park/`ADC`/park — INV-45; a carry-less advance is
   bit-perfect until the 16-bit seam, so gate the seam EXPLICITLY with
   `counter0=0xFFFF`).
2. **Every baked COPY of the value must be re-derived by an on-chip write
   before the next batch consumes it, and each write must land where that
   copy is at a KNOWN rotation.** ChaCha's state word 12 has two baked
   copies — the row's slot-0 boot registers and the adder's slot-0 addend
   (a register that ROTATES during the batch). The END of the batch is the
   natural point: the datapath is idle, a rotating register that stepped a
   multiple of its period is back at IDENTITY (the adder fires exactly 4
   times = its 4-slot period), and the writes race nothing — a batch-START
   update would race the host's reset writes and the first publish.

**Two placement sub-lessons the fix needed:**

* **Delivery reachability is about where a walk ENDS, never what it
  transits.** A hop-counted word is transparent to occupied cells, but its
  walk bends to each transit cell's OWN face — so a cell ALL of whose
  neighbours' resting faces forward PAST it (`add3`: bufB3/tap3 east, spad2
  west, bufA3 north) can only be written by an authored FLIP from an abutting
  cell. Survey the neighbours' word budgets before inventing relay cells: the
  one that already owns the needed flip for another duty (`tap3`'s drain-path
  north flip) makes the delivery nearly free. Both counter deliveries are
  authored literals with build-time-resolved operands (INV-63): the targets
  are StateVar/DataWord addresses, not declared input ports.
* **Order the batch-close fan-out so triggers that FLIP a shared conveyor's
  transit cell leave LAST** (INV-52's corollary): `drn.done` emits the
  counter words down the state line and only then `{jump:rel}`, whose handler
  transiently flips `tap0` — a counter word behind that flip would deflect
  into the reorder band, silently.

**MODE ISOLATION is part of the contract.** The default mode's programs must
stay BYTE-IDENTICAL (diff every cell's template/ports/state/data against the
shipped revision — measured NONE here), and the isolation must be mutation-
proven BOTH ways: the increment chip FAILS the fixed-mode second-batch
equality gate, and the fixed chip carries none of the increment machinery.

**REACH.** Any block whose parameters must advance per batch on chip: block/
sequence counters (AEAD nonces, rolling codes), frame indices, seed-evolving
scramblers. **Gated by:** `verification/tests/test_chacha20_counter_increment.py`
(the 7-batch value gate, the carry seam, §2.4.2 from on-chip keystream, and
the three mutants). **Related:** INV-45 (the wide add), INV-52 (face flips
steer transits), INV-63 (derived literals), INV-56 (`stop_reason` read every
batch), INV-33 (the two touched cells close at margin 2 and 1).

## INV-69 (SVPWM-a) — A serialize-LOCK release whose VALUE must equal a ROUTED arm face cannot be an authored constant in another cell; re-point the lock FROM the rendezvous's own reconciled face word, via a backward JUMP into a release entry

*(Number to be assigned at landing — authored by a parallel FOC-wave builder
while main's highest entry was INV-67.)*

Found 2026-08-30 building `SVPWMBlock` (the first N=2 LOCK-by-face rendezvous
with a MULTI-CELL datapath — the shape every future rendezvous-plus-pipeline
block, CordicRotate included, will have). Measured on the real placed +
routed + built two-arm chain.

**THE DEFECT.** The TMRVoter's serialize-LOCK release is a backward
`WRITE.CFG @N, 3` whose written VALUE is an authored `unlock_face` DataWord —
arm A's face, WEST at identity. That value must equal the face the router
ACTUALLY landed arm A on, and the build's face-reconciliation pass
(`_apply_rendezvous_input_faces`) patches face words **in the rendezvous cell
only** — an authored copy in the release cell is NEVER reconciled. The
TMRVoter survives because its suite's anchors happen to land arm A on the
authored face and its probe discards the rest. For the SVPWM's 7-cell chain,
`auto_pnr`'s compact re-pack relocated blocks freely and **69 of 96 built
layouts** landed arm alpha elsewhere: the release then re-pointed the lock at
whatever face the constant named — the BETA corridor, in the decoded case —
and the next beta word barged in ahead of its alpha. The signature is a
STALE-OPERAND packet (`duties(previous_alpha, beta)`, decoded by brute-forcing
the packet against the model), or a wedge when the named face carries nothing.
The chain builds, routes, probes clean on sample 1, and is wrong from
sample 2.

**THE FIX (measured 96/96 after; 27/96 before).** Give the rendezvous a third
entry:

    relock:
        MOVE [LOCK_FACE], R{data:face_alpha}   ; the RECONCILED word
        HALT

and make the release a backward JUMP into it (`MOVE [FACE], face_back;
{jump:unlock}; MOVE [FACE], face_tap` in the abutting cell, the edge declared
in `internal_jumps` so INV-39 holds and the jump is authored LAST so it is the
cell's highest-addressed JUMP per INV-53). The one copy of the arm face the
build DOES reconcile is now the one the release reads. This is also
RACE-FREE by construction: the relock jump arrives on the rendezvous's
INTERNAL forward face, which the arbiter bars until `got_beta`'s final
`LOCK_FACE = face_fwd` has run — the release cannot outrace the arm bar.
(The single-cell N=2 blocks never hit this because they re-lock from their
own face words inline — the same principle, degenerately.)

**LAYER:** block-authoring contract over `_apply_rendezvous_input_faces`'s
documented scope (toolchain, `placekyt/engine/build.py`) — permanent until
that pass reconciles authored face copies block-wide, which nothing today
needs.

**Gated by:** `verification/tests/test_svpwm.py::
test_rotation_has_three_stops_and_release_reads_the_reconciled_face`
(structure) and the layout probe (values, both arrival orders, two
consecutive samples). **Related:** INV-46 (Rule 3, the N+1th stop this
release serves), INV-19 (the serialize-LOCK idiom), INV-53 (backward jumps
resolve by address), INV-63 (why the release also cannot ride the emit cell:
the 3-burst packet needs the full-cell port patch, which an inline release
would break).

---

## INV-70 (SVPWM-b) — Two INDEPENDENT rendezvous arms whose corridors SHARE cells head-of-line DEADLOCK on out-of-order arrival: an arbiter-HELD word's in-flight words block the shared segment, and the completing arm can never deliver

*(Number to be assigned at landing — authored by a parallel FOC-wave builder
while main's highest entry was INV-67.)*

Found 2026-08-31 building `SVPWMBlock`; measured on the real placed + routed +
built two-arm chain. The LOCK-by-face rendezvous is DESIGNED to hold an
early-arriving word (the arbiter bars the out-of-turn face; INV-67's healthy
mid-group Deadlock). What the family docs did not say: the held word is not
free — its in-flight WRITE/DATA/JUMP words occupy the TAIL CELLS of its
delivery corridor while held. If the two arms' corridors share ANY cells
(`auto_pnr`'s compact packs herd both arm blocks into the port corner, where
both nets fork late off one broker), a beta-first sample wedges the chain:

    beta held at the rendezvous -> its words back up the shared segment ->
    the alpha word cannot transit -> the pair NEVER completes -> zero egress

Measured: **10/12** compact-packed layouts wedge on a beta-first sample —
and every one of them is PERFECT under alpha-first drive, so a probe that
only drives the in-lock order certifies nothing about arrival robustness.
Diagnosis needs INV-67's discipline: the held word's own `Deadlock` run is
the healthy signature; the wedge is the POST-group state (the completing
word blocked in transit, `chip.output_available` never true).

**Rules.**
1. A face-locking block's arms must have corridor-DISJOINT deliveries (past
   the unavoidable port cell/first broker) in any design that drives them at
   independent times. Route-preserving placement (`auto_route_all` over
   spread-out authored anchors) achieved 12/12 both-order-clean layouts where
   the compact re-pack achieved 2/12.
2. A rendezvous suite's layout probe must drive BOTH arrival orders across
   two consecutive samples (`test_svpwm.py`'s `_PROBE_ORDERS`); the
   in-lock-order-only probe was measured to pass 96/96 layouts of which
   10/12 wedge out-of-order.
3. This is the held-word generalisation of INV-56's two-waves rule: there the
   two flows travel opposite directions on one conveyor; here one flow is
   PARKED by the arbiter on cells the other must transit. Same circular wait,
   no motion required.

**LAYER:** substrate topology (permanent physics of hold-until-accepted
forwarding) + harness/design discipline. Nothing to fix in the block: the
hold is the rendezvous working as specified.

**Gated by:** `verification/tests/test_svpwm.py` — the both-orders probe in
every chain build, `test_reversed_arrival_order_is_identical`,
`test_random_interleavings_preserve_the_pairs`, and
`test_stop_reason_signature_of_a_healthy_rendezvous` (the INV-67 pin).
**Related:** INV-67 (the stop_reason reading), INV-46 (Rule 4 probing), INV-56
(the moving-waves sibling), INV-24 (port fan-out forks at a broker — the
shared prefix this hazard rides).

---

## INV-71 — A chain of N=2/N=3 LOCK rendezvous blocks is ROUTING-bound, not cell-bound: each arm needs its own relay landing on a DISTINCT face, and a chip-input port fan-out (the port-divert landing) does NOT satisfy an arm

**Measured** while assembling `examples/foc_motor` (Clarke -> CordicRotate(-θ) ->
2×PI -> CordicRotate(+θ) -> SVPWM) on the 10×12 array. The chain is **55 block
cells of 120** — less than half the array — and yet the binding constraint is
routing, not area.

**1. THE PORT-DIVERT LANDING IS NOT AN ARM.** Wiring several nets from
`x16_in` straight into a face-locking block's arms routes and builds clean, and
the build reports DISTINCT `input_landings` (differing `entry` and
`data_addrs`) — the exact signature the per-block suites accept as "distinct
arms". But every landing carries the SAME `hop` (the port cell itself: the
INV-24 port-divert turn programs), so the words all arrive on the SAME face.
The rendezvous LOCK gates that face, and the chain emits **ZERO words** while
every run reports `QueueEmpty` (not `Deadlock` — nothing is stuck; the work
never started, INV-56's "tiny event count" signature). Measured on a
port-fed 2-arm `SVPWMBlock`: `route ok, build ok`, 6/66 cells ever active,
0 duty words in either arrival order.

**The rule:** an arm of a `NEEDS_DISTINCT_INPUT_FACES` block must be driven
through its OWN relay block (a `StreamSplitterBlock` is the shipped idiom, one
per arm) so the router gives it an independent corridor terminating on its own
face. Distinct `entry`/`data_addrs` in `input_landings` is NOT sufficient
evidence of distinct arms — **compare the `hop` too**, and treat equal hops as
one arm.

**2. THE FACE BUDGET IS A CHIP-LEVEL BUDGET, NOT ONLY A BLOCK-LEVEL ONE.**
layout_rules §4b sizes the N+2 face budget inside the fold. Chained, the same
budget applies to the FREE SPACE around each rendezvous: a `CordicRotateBlock`
needs THREE free on-chip faces at its `rdv` leaf AND three corridors that reach
them without sharing cells (INV-70). Two CORDICs plus an SVPWM on one 10×12
array means **three rendezvous competing for corridors**, and that is what runs
out first. Measured over ~2600 placements of the six-block chain (random,
structured, and hill-climbed), the best result was **2 of 13 nets unrouted**;
the failures are always the rendezvous arms, reported as `no bus path from
source to the broker tap` and `no free broker cell abutting the target input`.

**3. THE SUB-CHAINS DO ROUTE.** `2×PI -> CordicRotate(+θ) -> SVPWM` (the FOC
back half, **66 cells**) routes and builds. So does `Clarke -> CordicRotate`
once the CORDIC's `rdv` corner is off the array edge (at the corner, north is
off-chip and the DRC reports `dual_input_same_face`). The whole-chain wall is
the SUM of the arm corridors, not any single edge.

**Corollary for planning:** cost a face-locking chain by ARM COUNT, not cell
count. This chain has 3 + 3 + 2 = 8 rendezvous arms plus 2 complex-rail pairs;
that is the number to fit, and it is the number the 10×12 array runs out of.

**LAYER:** substrate/router geometry (permanent on this array size), plus a
harness/design discipline (relay-per-arm). Not a block defect: all four FOC
blocks are individually chip-proven.

**Related:** INV-46 (Rule 1 silent no-op, Rule 2 face budget, Rule 4 probe),
INV-70 (corridor sharing), INV-24 (port fan-out diverts), INV-56
(`QueueEmpty` + tiny event count = work never started), layout_rules §4b.

---

## INV-72 — A LOCK-rendezvous fix is a FAMILY obligation: an unreconciled release face is invisible until the router disagrees with the author, and a partially-stale rendezvous dump means a BARRED ARM, not an occupied cell

*(Number assigned at landing — main's highest entry was INV-71.)*

**Measured 2026-08-31** on `examples/foc_motor`, fixing the chain's
one-iteration wall. Three separable rules; the first is the defect, the
second and third are the diagnostic method that found it after a plausible
wrong diagnosis.

### 1. INV-69 IS A FAMILY OBLIGATION, NOT A PER-BLOCK FIX

INV-69 was recorded against `SVPWMBlock` and its text explicitly names
`CordicRotateBlock` as "the shape every future rendezvous-plus-pipeline block
will have". CordicRotate was authored in PARALLEL by a different builder and
shipped with the pre-INV-69 construction — the TMRVoter's value-carrying
`WRITE.CFG` whose LOCK_FACE value is an AUTHORED constant in the release
cell. Both blocks passed their own suites; the chain composed them and wedged
from the second iteration.

**The rule:** when a rendezvous-family defect is fixed in one member, AUDIT
EVERY MEMBER in the same change. The family is
`NEEDS_DISTINCT_INPUT_FACES`-declaring blocks; the cheap static check is that
**no such block contains a `WRITE.CFG` carrying a face value**, because the
only face copy the build reconciles is the rendezvous cell's own DataWord
(`_apply_rendezvous_input_faces` patches that cell and nothing else). A
release must therefore be a backward JUMP into a `relock` entry that reads
that word — or, for a SINGLE-CELL rendezvous, an inline re-lock from its own
word (Clarke's degenerate-but-safe form). A release that CLEARS the lock
(value 0, no face) is exempt — PIControllerBlock's is, correctly.

**Why per-block suites cannot see it:** the defect needs the ROUTER to
disagree with the author. A block's own suite probes layouts and keeps one
that works, so the authored face is usually the routed face. Composition is
what moves the arms. Measured here: the FOC placement lands arm x NORTH while
the constant said WEST.

### 2. A PARTIALLY-STALE RENDEZVOUS DUMP MEANS A BARRED ARM

The handed-over diagnosis read the wedged cell's memory, saw iteration 0's
values in the arrival slots, and concluded the slots "never clear between
iterations, so the second sample arrives at an occupied cell and holds". The
full dump refutes it: of three arrival slots, ONE held the stale value and
the other TWO held iteration 1's new words. A rendezvous that accepted two of
three arms is not occupied — it is BARRED on the third.

**The rule:** read EVERY arrival slot before concluding staleness, and treat
a MIXED dump as localising the fault to the arm whose slot did not advance.
Stale-looking state in a rendezvous is far more often the SYMPTOM of a
mis-aimed lock than a missing reset.

**The decisive read is `chip.read_config(cell)`, not cell memory.** LOCK is
bit 14; LOCK_FACE is bits 12-13 (0=S, 1=E, 2=W, 3=N). Across boot /
after-iteration-0 / wedged this chain read `0x7100` / `0x6100` / `0x6100`:
LOCK still set, LOCK_FACE moved north -> west and stuck. A mis-aimed lock and
an unreleased lock are different defects with the SAME `Deadlock` signature,
and only the CONFIG read separates them.

### 3. `reset_per_batch` IS A PACKET-BOUNDARY MECHANISM; CHECK THE DELIVERY PATH

`reset_per_batch` StateVars are resolved by
`build._resolve_batch_reset_writes` into `(x, y, addr, value)` writes on
`ChipBuild.batch_reset_writes`, and applied by `SimServer._apply_batch_reset`
**once per `process_batch` RPC**. A harness that drives the chip directly
(`inject_data_physical` / `chip.run`, which is what every example demo and
most gates do) NEVER executes them.

**The rule:** before proposing `reset_per_batch` for a between-samples
problem, confirm the drive path actually crosses a packet boundary. It cannot
fix anything WITHIN a burst by construction. It is also the wrong tool for an
arrival slot, which is written before it is read on every pass and needs no
clearing; its correct use is loop MEMORY that must cold-start per packet
while persisting within one (INV-68's converse — PI integrators, Costas
phase, matched-filter delay lines).

**LAYER:** block-authoring contract over the build's documented reconciliation
scope (rules 1) + diagnostic method (rules 2, 3) — permanent until
`_apply_rendezvous_input_faces` reconciles authored face copies block-wide.

**Gated by:** `verification/tests/test_cordic_rotate.py::
test_release_is_a_backward_jump_reading_the_reconciled_face` and
`::test_relock_reads_the_same_word_the_build_reconciles` (structure, incl. the
no-`WRITE.CFG` family check), and `test_foc_motor_example.py`'s streaming
gates + `test_mutation_unreconciled_release_face_collapses_the_stream_ON_CHIP`
(the INV-4 proof: re-pointing `relock` at an unreconciled word is
geometry-preserving, still builds, and collapses the chain to exactly one
packet).

**Related:** INV-69 (the defect class this generalises), INV-46 (the
LOCK-rotation family and its Rule 3 N+1th stop), INV-67 (mid-group Deadlock is
healthy; post-group is real), INV-68 (state that must EVOLVE — the converse of
rule 3), INV-70 (the saturated/reversed walls that did NOT move here), INV-56
(read `stop_reason` for every run).

## INV-73 — The two RAILS of one complex output leave the SAME cell, so a hand-route that sends them down one corridor lands them on ONE face and fails the face-locking DRC; the rails must FORK, and one must travel further

*(Number assigned at landing — main's highest entry was INV-72.)*

A complex-valued on-chip output (`yi`/`yq`) is TWO nets that originate at the
SAME source cell. A face-locking consumer (`NEEDS_DISTINCT_INPUT_FACES`:
Clarke, CordicRotate, SVPWM) distinguishes its async input streams ONLY by
arrival face. So the natural hand-route — run both rails down one corridor to
the consumer, because they start together and end together — is exactly the
route that CANNOT work: identical path ⇒ identical final step ⇒ same arrival
face ⇒ `dual_input_same_face` DRC error.

MEASURED on a hand-routed full FOC loop (`foc_motor.full.kyt`), two errors,
both this shape:

```
cordicrotate head=(4,4)
   theta  from West   (via (3,4))
   x      from East   (via (5,4))    <- clarketransform.yi
   y      from East   (via (5,4))    <- clarketransform.yq
svpwm head=(9,7)
   v_alpha from South (via (9,8))    <- cordicrotate_2.yi
   v_beta  from South (via (9,8))    <- cordicrotate_2.yq
```

net17/net18 carry the IDENTICAL waypoint list `[(7,4),(6,4),(5,4),(4,4)]`;
net19/net20 likewise. The DRC is CORRECT and the geometry is genuinely broken —
there is no missing fly line and no false positive. The GUI draws one visible
corridor because the two rails overlap perfectly, which is why the error looks
like it has no cause on screen: **a same-face collision is INVISIBLE as a fly
line precisely because the two nets coincide.**

The shipped working `foc_motor.kyt` solves the same pair by making the rails
FORK late — `yq` continues two cells past `yi`'s turn-in:

```
cordicrotate.yi -> svpwm.v_alpha: [...(2,11),(1,11)]              enters (1,10) from South
cordicrotate.yq -> svpwm.v_beta:  [...(2,11),(1,11),(0,11),(0,10)] enters (1,10) from West
```

So the rule for hand-routing (and for reading such a DRC error):

1. Two nets from the same source block to the same face-locking consumer must
   DIVERGE somewhere and approach on different faces. Share the corridor for
   the long haul if convenient, then fork near the destination.
2. The consumer needs a free neighbour cell on a second face for the forked
   rail to enter from. Check occupancy FIRST: on the measured failure the
   svpwm head (9,7) had E out of bounds (x=10 on a 10-wide array), N and W
   occupied by blocks, leaving only S — one face, so NO route could satisfy
   the rule at that anchor and the block had to move, not the wires.
3. `dual_input_same_face` names the block, the shared face and the nets. Read
   the FACE, then the source of each named net: if the two nets share a source
   block, this is the rails-not-forked shape, not a placement-density problem.

Related: INV-71 (arm count, not cell count, is the binding constraint),
INV-70 (arms whose corridors share cells head-of-line deadlock — forking the
rails also reduces shared segment length), INV-24 (port-divert landings all
arrive on one face for the same reason).
