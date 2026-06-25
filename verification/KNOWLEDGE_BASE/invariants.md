<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Block verification — substrate invariants

Hard-won, model-agnostic rules that apply across block classes. An agent building
or verifying a Kyttar block should read these first. Each is a *constraint* ("always
/ never X"), not a one-block idiosyncrasy. Per-block fixes go in `lessons_log.md`.

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
~7 taps that exceeds the budget (the block's own `<=12 → 1 cell` heuristic is
too optimistic). Above the single-cell ceiling the block becomes a multi-cell
**wavefront** whose output egresses from the *last* cell — which a harness that
derives its hop/drive from `placement.cells[0]` (the input landing cell) does
not yet handle.

**Fix / status:** verify scaling blocks across their *proven* parameter range and
record the ceiling as an explicit known limit (executable guard tests that flip
when fixed) rather than claiming the block is fully done. Multi-cell egress
(driving the last cell, not the first) is a harness capability still to be built.

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

## INV-9 — On THIS 10×12 chip, keep a block ≤ 8 cells across (convention)

**Symptom:** a wide block (a long FIR as a near-straight line) builds but won't
route past itself; an adjacent block can't be reached by the bus.

**Root cause:** the array is 10 wide and the bus needs one channel of cells on
EACH side of a block to pass traffic. 8 + 1 + 1 = 10 = full width. A block wider
than 8 in either dimension leaves no channel → routes fail silently.

**Fix:** fold to keep both footprint dimensions ≤ 8 (a 64-tap FIR ≈ 13 cells must
be ~4×4, never 13×1).

**This is a CHIP-SIZE CONVENTION, not an architectural rule, and is deliberately
NOT enforced by any DRC or warning** — on larger future chips it stops mattering
and enforcement would have to be ripped out. Nothing flags a violation; it just
won't work here. Honor it on this chip.

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

**Fix / guideline (not a hard DRC — a layout constraint). NO PADDING (CM, this
session).** Choose the most COMPACT fold (tallest column `H ≤ FOLD_HEIGHT` ⇒ fewest
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
26-tap... no — a 125-tap/26-cell dc_blocker hit exactly this: `_fold_geometry`
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
twice == `-c*Ra` (`MSUQ` is `R0 -= (Ra*Rb)>>15`, arch_spec v0.11 §4.12 — MAC opcode
MODE=11). Each `(Ra * c/2)>>15` product is in range, so there is NO intermediate
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

