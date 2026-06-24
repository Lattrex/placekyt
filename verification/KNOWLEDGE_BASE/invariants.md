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

## INV-13 — Saturate a Q15 accumulator ONCE at the end, never per-step

**Symptom:** a MAC-chain block (FIR/IIR/mixer) either (a) explodes in cell count —
a 20-tap FIR balloons from ~4 cells to ~10 — or (b) under overload emits a clean
rescaled signal instead of the flat-topped ±full-scale rails it should show, so a
real overdrive is invisible.

**Root cause:** the cell ALU has NO auto-saturating mode — `MACQ`/`ADD` WRAP
(modulo 2^16, sign-extended) and set the V (signed-overflow) flag. The tempting
fix is to clamp R0 after *every* accumulation step (`BR.NV +2 ; SHR R0,#15 ;
SUB satneg,R0` per tap). That is WRONG twice over: (1) it costs ~3 extra
instructions PER TAP, collapsing the per-cell tap density (for FIR it dropped
TAPS_PER_CELL 5→2 and the single-cell ceiling 7→3, ~2.5× the cells); and (2) it
ALTERS THE MATH — clamping intermediate partial sums re-normalises legitimate
mid-sum excursions that would otherwise wrap and return, masking genuine overload.
A correctly-overdriven filter then looks fine.

**Fix:** clamp the accumulator EXACTLY ONCE — on the FINAL accumulation, just
before the output WRITE. The whole filter (single cell, or the entire cross-cell
wavefront) is ONE logical accumulator; let every intermediate MACQ tap and every
cross-cell partial WRAP, and apply the 3-instruction clamp only to the last op
(the final MACQ in a single cell, or the cross-cell ADD on the last multi-cell
cell). The V flag of that last op decides the clamp:
`true sum > +FS ⇒ wrapped N=1 ⇒ +0x7FFF`; `< −FS ⇒ wrapped N=0 ⇒ −0x8000`; via
`0x8000 − (R0>>15)` from one shared `satneg=0x8000` word. The priming MULQ is
NEVER clamped — MULQ sets V from the RAW 32-bit product (which almost always
exceeds i16), so clamping there saturates spuriously.

**Corner case (accept it):** with a single 16-bit accumulator, only the FINAL
result is guaranteed saturated. An intermediate sum can wrap and the final op can
land back in range, so a vastly-over-unity float sum does NOT always pin at a
rail — it pins ONLY when the final accumulation step itself overflows. This is the
standard single-accumulator fixed-point tradeoff. Consequence for verification:
the bit-exact reference must model WRAPPING intermediates + a single final clamp
(not per-step), and an overload/rail test must use stimulus whose FINAL op
overflows (steady large input, not a transient) or it will not exercise the clamp;
the wrap-mutation must likewise overflow the final op so wrap ≠ end-only-clamp.

**Applies to:** FIR, IIR, complex mixer, correlators — any Q15 MAC chain. See
[[layout_rules]] for how the resulting per-cell tap density sets the fold.

