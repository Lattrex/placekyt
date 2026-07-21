<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Block verification — substrate invariants

Hard-won, model-agnostic rules that apply across block classes. An agent building
or verifying a Kyttar block should read these first. Each is a *constraint* ("always
/ never X"), not a one-block idiosyncrasy. Per-block fixes go in `lessons_log.md`.

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
recovered BITS == per-sample bits, 0-diff, fracs 0.3/0.5/0.7 (proto_gardner_sat.py). NOTE: word
values differ by sub-LSB interpolation amounts that never flip the sign — for a rate/timing block
the SATURATION GATE MUST assert BIT-equality (sign), not word-equality. Also: use a REAL RRC-shaped
2-sps BPSK stimulus — a piecewise-linear synthetic can't be locked in EITHER mode (false "divergence").

**TWO REGISTER-RECLAIM TRICKS (for fitting a serialize-LOCK into a budget-tight landing cell):**
1. **LOCK_FACE need not be written if the feedback face == the CONFIG reset default = SOUTH (00).**
   simkyt config_reg.rs resets LOCK_FACE to Face::South. If the feedback corridor arrives on the
   cell's SOUTH face (place the feedback cell SOUTH, emitting NORTH), the lock tail is just
   `MOVE R0,<nonzero>; MOVE [LOCK],R0` (2 instrs, not 4 — skip the LOCK_FACE writes). CAVEAT: only
   valid for the UN-rotated layout; if auto-orient rotates the block, restore the is_face lock_face
   DataWord + the two LOCK_FACE writes (needs 2 more slots freed).
2. **Merge two DataWords that hold the IDENTICAL value into one** (Gardner: `inc` and `one_q14`
   were both 1<<14 → one word, freeing a slot). Also: LOCK engages with ANY nonzero, so reuse any
   existing nonzero data word for the `MOVE [LOCK],Rn` — no dedicated `one` word needed.

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
and 2). Bounded probe (`proto_cmix_probe.py`):
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

**BRING-UP STATUS + two dead-ends found (ComplexMixer attempt, 2026-07-14 — NOT yet fixed,
block reverted to its committed per-sample GR-verified form; `pipeline_lock` scaffolding
removed):**

1. **The mixer's output cell is genuinely at its register budget, and you CANNOT reclaim by
   "operating on the input registers directly."** The mixer copies its 4 inputs (cosv, sinv,
   xi, xq) into 4 state vars BEFORE the arithmetic *on purpose*: an INPUT register is NOT
   stably re-readable across multiple MULQs in one execution (the async operand latch is
   consumed / not guaranteed to hold for a second read). Rewriting the mixer to `MULQ t,
   R{in:cosv}` etc. built fine and even passed the I channel, but **the Q channel came back
   corrupt (corr ~0.7, `test_complex_mixer.py` Q=FAIL bit-exact)** — a silent DSP regression
   the *saturation* probe (0 output) never would have caught; only the bit-exact GR gate did.
   LESSON: after ANY register-reclaim on a compute cell, re-run the block's bit-exact
   `test_*` gate, not just the pipeline/deadlock probe. The 2 face DataWords the lock needs
   do NOT fit the mixer without a real redesign (e.g. a dedicated unlock relay cell that is
   NOT a feedback source, or splitting the mixer).

2. **The fixed-authored `WRITE.CFG @N` in the exit/output cell gets clobbered by
   `_patch_last_write_handoff`.** iq_upconvert survives because its unlock WRITE.CFG is not
   the highest-address WRITE (the `out` WRITE is last). In the mixer, instruction ordering
   put the WRITE.CFG at/after the output WRITEs, so `_patch_last_write_handoff` (patches
   `max(write_addrs)`) rewrote the WRITE.CFG's dest (4→1) and hop. To use the fixed-authored
   approach you MUST guarantee the block's real output WRITE is the strictly-highest-address
   WRITE (emit yi/yq LAST, WRITE.CFG before them) — same footgun as the CoherentBPSKRx packer
   (`_patch_last_write_handoff patches the highest-addr WRITE — keep the out WRITE LAST`).

3. The `_apply_internal_feedback` route (declare a backward `mixer→phase` edge) is the WRONG
   mechanism for a config-only unlock with no data feedback: its data-WRITE patch collides
   with the output register (phase.xi = reg 0 = the yi output reg), corrupting yi, and the
   config patch it does apply is then re-defaulted anyway. iq_upconvert deliberately does NOT
   declare the backward edge — it fixed-authors the hop and relies on `trig __terminate__` +
   correct WRITE ordering. Follow iq_upconvert EXACTLY (no backward edge; fixed `@N`; face_
   internal/face_tap names; output WRITE strictly last) — but only after solving (1).

**BRING-UP PROGRESS (2026-07-14, commits d82b359..b536b42) — the SOLVED path:** the
approach that WORKS is a DEDICATED internal `unlock` cell (not inline in the mixer, which
has no register room — see (1)). Build-engine support added: `_apply_internal_feedback`
handles a CONFIG-ONLY backward edge whose src port is `"unlock"` (patch the WRITE.CFG hop
via `_patch_config_write`, which matches on the config bit alone to recover a router-
clobbered dest). Layout MUST keep the EXACT proven 2-column datapath (moving the cos column
breaks relay→mixer — relay's EAST forward collides, seen via `get_trace` as relay re-firing
+ flooding the shifted column). Place `unlock` in a FREE cell NORTH of the mixer so the
mixer's yi/yq egress EAST stays clear and its trig fires unlock NORTH; unlock's WRITE.CFG
goes to phase. CRITICAL separate-concern bug found+fixed: phase's default_layout FACE is its
DATAPATH emission (south→sin_fold) — it is INDEPENDENT of LOCK_FACE (the arbiter gate,
a `lock_face` CONFIG DataWord). Setting phase's face to the corridor direction sends its
fan-out into the corridor and stalls the whole block (phase re-fires forever). Status:
**N=1 completes + emits**; STILL OPEN: N≥2 stalls at the output-port column + N=1 emits 1
word value 0 (the auto-placer offsets the relative default_layout, so unlock↔phase adjacency
+ the WRITE.CFG hop don't match the hand-designed corridor; the output WRITE value/count is
also wrong). Next: make the corridor placement-invariant (or author it so the placer keeps
the adjacency) and fix the 2-word yi/yq output routing. Sim `get_trace()` (dict list with
`cell_id` linear index, `kind`, `data`) + bounded `run(max_events=)` are the diagnostic
tools — NOT blind face/hop guesses (a whole class of "@1 vs @2" dead-ends were a probe
decode bug: hop field is bits[9:5]).

**RESOLVED (2026-07-14, commits 331e993 / 10969a9 / 9aaacda) — ComplexMixer is now fully
pipeline-saturation-capable, bit-exact.** Supersedes the "NOT yet fixed" status and the
dedicated-`unlock`-cell path above:
- Dead-end (1) is DISPROVEN for MULQ: `MULQ A,B` writes R0 and does NOT clobber its operands
  (PROGRAMMING_GUIDE). The earlier Q-corruption came from operating on INPUT regs (consumed by
  the arbiter latch), NOT from re-reading a state var. So the mixer's `t` scratch was
  UNNECESSARY: `MULQ xi2,c` directly frees 4 instrs + the `t` state reg — enough budget to
  fold the unlock INLINE (no separate cell; CM required self-contained). RE-RUN the bit-exact
  gate after any such reclaim (dead-end 1's real lesson stands).
- The unlock is folded into the mixer with a DUAL-FACE flip EXACTLY like iq_upconvert's upmix:
  emit yi/yq on `face_tap` (routed output, set by `_apply_rotate_tap_face`), flip to a
  DISTINCTLY-NAMED `unlock_face` (NORTH — name it NOT `face_internal` so `_apply_rotate_tap_face`
  leaves it to `_apply_orientation_face_words` to rotate), do `WRITE.CFG @2,4` down a 1-cell
  `transit_unlock` corridor to phase. `output_cell_id()=="mixer"`; the config-only backward edge
  IS declared as `("mixer","unlock","phase","xi")` and resolved by `_apply_internal_feedback`'s
  `_src_port=="unlock"` branch (dead-end 3's data-collision does NOT occur because that branch
  patches the WRITE.CFG by config-bit alone, never a data reg).
- THE FINAL BUG (the N≥2 "post-burst deadlock"): the terminal `{jump:trig}` (self-terminating
  via `__terminate__`) STILL EMITS a JUMP word on the CURRENT face, which was left at
  `unlock_face`=NORTH by the WRITE.CFG → the trig fired into the unlock corridor → transit →
  phase → transited through → sin_fold → ... a self-sustaining datapath loop that deadlocked
  once the real samples drained. FIX: `MOVE [FACE], face_tap` AFTER the WRITE.CFG so the trailing
  trig goes to the drained output port. **NEW INVARIANT: a `__terminate__` trig is not a no-op —
  it emits a JUMP word on the current FACE; a dual-FACE cell MUST restore its output face before
  any trailing emission.**
- PROVEN: `run_block_dut_pipelined` (saturated) == `run_block_dut_complex` (per-sample GR ref)
  BIT-EXACT over 8 samples, both placements. Gate: `test_pipeline_saturation.py` COMPLEX_2IN2OUT
  (22 pass). DEFAULT stays `pipeline_lock=False` (flipping to True regressed the GRC importer's
  input-reg/net-resolution + the compact SSB Weaver footprint); the lock is OPT-IN, gate passes
  `params={pipeline_lock:True}`.

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
- KNOWN-OPEN (2026-07-21): the unlock corridor is NOT yet placement-invariant in an AUTO-ROUTED
  CHAIN — FM releases the lock hand-placed, but auto-placed in the fsk4 TX chain `transit_unlock`
  never fires (WRITE.CFG never reaches phase → stall). Next: make emit→transit_unlock→phase
  adjacency survive auto-placement (or author it placement-invariant). This blocks the fsk4 modem TX.

---

## INV-21 — SATURATED "pipelined" drive means the SLOWEST BLOCK is saturated, NOT the input port; a raw-word input stream cannot be demuxed by decoding word bits

Two distinct facts about the pipelined/full-speed path (sim_bridge `process_batch`
`pipelined:true` → `queue_words_physical` whole burst + one continuous `run()`),
both surfaced getting the coherent BPSK RX full-speed GRC demo live.

**A. The coherent RX IS pipelined and the array is ~95% BUSY — it is THROUGHPUT-bound
by DSP instruction count, ~675 instr per recovered symbol.** ⚠️ TWICE-CORRECTED. Two
earlier drafts were WRONG: (1) "Gardner is the saturated bottleneck" and (2) "88% idle /
latency-bound". Both came from measuring ONE cell's utilisation (~12%) and mistaking it
for the array. Measuring a whole STEADY-STATE OUTPUT INTERVAL settles it. Facts for the
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
  here — the work itself is the limit. See [[project_coherent_rx_cannot_pipeline]].
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
COMMERCIAL REALITY (CM's framing): as a SINGLE serial DSP engine this is SLOW — a
commodity FPGA/RFSoC/C66x demod core does tens–hundreds of MSa/s complex, i.e. ~100–
1000× faster per chain; even at 20× shrink one chain trails badly. Parallelism does NOT
rescue a chain too slow for the target signal (CM: "a million too-slow chains still
can't demod a fast signal"). So Kyttar is viable ONLY for NARROWBAND many-channel work
(the skimmer: hundreds of kBaud–low-MBaud channels — PSK31/RTTY/AFSK/voice/low-rate PSK-
FSK), NOT wideband single-channel. THE LEVER for one-chain speed is SHORTENING THE
CRITICAL PATH (243 serial instr/symbol), not adding chains: cut the serial spine (the 2
PI loops + MF accumulate) and spread MF taps across more cells so real parallelism rises
from 2.78× toward the ~10× the tap count allows. throughput_bench.py + /tmp/critpath.py
measure it. See [[project_coherent_rx_cannot_pipeline]].


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

**Applies to:** any full-duplex / shared-input-port design (the BPSK modem, the QPSK
modem, the upcoming 4FSK modem) — a TX and an RX chain multiplexed on one `x16_in`. Prove
it end-to-end on the HOSTED chip (load `.kyt` → build → `stream_targets` → `SimServer` →
drive both `stream_id`s), NOT with a synthetic single-block harness — the shared-port fork
only exercises when both streams are present. See [[layout_rules]] duplex pattern.
