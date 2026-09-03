<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Block layout — folding, edges, and the bus

How a multi-cell block is **shaped on the array** is not cosmetic — it decides
whether the block can be placed and routed at all on this chip. Read this before
authoring or reshaping any block bigger than one cell.

What is and is not enforced (corrected 2026-08-29 — this preamble used to say
"none of this is enforced", which is only half true):
* a block extending past the fabric **is** caught, loudly, as `unplaced_cell`
  (`engine/drc.py`, repeated in `engine/build.py`);
* I/O colocation is genuinely **not** enforced — `io_colocated` is derived only
  (`engine/portmap.py`) and about half of the shipped blocks are not colocated
  (60 of the 120 catalog-resolvable `done` blocks; the manifest lists 125 done —
  counts re-probed 2026-09-03);
* the ≤8 width cap is not enforced by the placer or the DRC — `layout_caps()`
  has no placer or DRC caller; it is gated only in the per-block test suites of
  the `CHIP_SCALE` classes. See §3.

> **Scope note — this is a CONVENTION, not an architectural rule.** The pressure
> here comes entirely from this chip being **10×12** — very few cells, very few
> routing channels to spare. On the larger chips that come later, the width
> convention (below) stops mattering and would be wrong to enforce. So nothing in
> the codebase flags a violation, *by design*. On **this** chip they are strong
> defaults — but they are defaults, not laws: see §3 for the width convention,
> which is measurably WRONG for blocks that dominate the array.

---

## Why blocks must fold (the Costas cautionary tale)

A block laid out as a long straight line is almost always wrong. The worst case
we hit: an early Costas loop was **7 cells in a row plus a 6-cell full-width
return path** to carry a *one-sample* feedback value back to the front — 13 cells
to delay a single sample. The feedback had to travel the entire width of the
block because the producer (last cell) and consumer (first cell) sat at opposite
ends. (That line is still in the catalog as the legacy `CostasLoopBlock` — a 7×1
line, input at (0,0), outputs at (6,0), no manifest status — do not copy it.)

Folding the same datapath into a compact **~2×4 block** put the producer and
consumer one cell apart, so the feedback returns in ~1 hop instead of 6. Same
program, same DSP, a third of the cells and none of the full-width return route.
(The fold is the shipped `ComplexCostasLoopBlock`, 4×2, manifest `done`.)

**The rule that falls out:** lay a multi-cell block out as a **serpentine fold**
(snake), not a line. Wrap the datapath across rows so that cells which must talk
to each other end up physically adjacent.

**Refined 2026-08-30 — the serpentine is the DEFAULT, not a law; two shipped
classes choose a CYCLE deliberately.** What the Costas tale actually teaches is
"put talking cells adjacent"; a snake is merely the usual way to get that. Two
verified blocks get it with a closed cycle instead:

* **A panel-backed scan loop whose back edge is closed EXTERNALLY.**
  `LZ4EncoderBlock`'s scan is a genuine cycle (issue → read → return →
  dispatch → issue) and the panel's push-read return closes it outside the
  fabric, so a 12-cell scan CYCLE inside its 15-cell 3×7 fold (ingest, INS and
  egress sit off the cycle — INV-51) is the shape that gets the back edge for free.
  The cost a ring adds — every hop becomes modular, so an ADJACENT neighbour
  can be 11 hops the long way round — is paid deliberately: the fold is
  ordered so the per-position hot edges ride 1–6-hop walks and only the
  once-per-sequence formatter edges take the long arcs
  (**frequency-weighted hops**, gated in `test_lz4_encoder.py`).
* **A latency-tolerant block gone ALL-SERIAL on ONE conveyor cycle.**
  `Poly1305MACBlock` (100 cells, 10x10) runs every phase as a serial chain on
  a single conveyor cycle, which makes INV-58's sweep-staging hazard class
  unreachable by construction — see INV-64.

A deliberate cycle still answers to the ring hazards: INV-51 (a closed ring
traps its interior; LZ4's INS/egress sit where the walk can afford them),
INV-55 (a uniformly-faced band is sealed from outside), and INV-56 (two waves
in opposite directions on one conveyor deadlock — ChaCha20Keystream's reorder
band needed both of INV-56's fix shapes). For a latency-SENSITIVE feedback
loop the serpentine with adjacent producer/consumer remains the right default.

---

## The four conventions

### 1. Input and output ports belong on the SAME edge

The router runs a **bus** along one edge of the block and taps it. If the input
port is on the west edge and the output is on the east edge, the bus cannot tap
both without wrapping around the block — it can't, and the route fails.

Put the block's external input port(s) **and** its output port on the **same
edge** (the "bus-facing edge"), within a couple of cells of each other. Then the
bus runs along that edge, taps the input, taps the output, and continues to the
next block.

The engine *observes* whether you did this: `portmap.py` derives
`io_colocated=True` when the input and output ports share the bus-facing edge and
sit within `COLOCATION_SPAN` (2) cells. **It is derived, not required** — nothing
forces it. **Corrected 2026-08-29:** this used to say a non-colocated block "does
not route". That is false — `io_colocated` is True for 60 and **False for 60** of
the 120 catalog-resolvable shipped blocks (re-probed 2026-09-03), and the placer uses it only as a TIE-BREAK
(`engine/autoplace.py`). Colocation shortens the route and makes placement
forgiving; it is a design *target*, not a precondition.

**Corrected 2026-08-30:** the previous sentence here said colocation "IS
mandatory for a `CHIP_SCALE` block". The span-2 `io_colocated` predicate is NOT
what a chip-scale block needs, and the shipped full-width blocks violate it
while working: `ChaCha20KeystreamBlock` (10x7) lands its input `seq` at (0,1)
— one row BELOW the port row, at the west end — and egresses ON the `x16_out`
port cell `out` (9,0): nine columns apart (corrected 2026-09-03; this used to
say "same row"); `Poly1305MACBlock` (10x10) puts `seq_top` and `out`
seven columns apart on its control row; `GRUCellBlock` (10x6) has `fin`/`oout`
three apart. What a **full-width** block genuinely must do is put BOTH
terminals where the chip's port row can reach them — nothing can route around
to a full-width block's far side — i.e. same port-facing edge, any span (§3's
statement of the trade). A chip-scale block that is *not* full width can even
ship I/O on opposite edges: `FFT64Block` (9x12) has its landing at (4,0) and
its exit at (4,11), reachable because the free column keeps the far side
routable.

Concretely: fold the block so its input landing cell and its output cell are both
on the bus-facing edge. For an 8-cell block, a 2×4 vertical fold with input at
(0,0) and output at (1,0) — both on the west edge, adjacent — is the canonical
shape.

### 2. Output egresses from the LAST cell, not cell 0

In a multi-cell **wavefront** block (a long FIR, the chained partial-sum filters),
the input enters cell 0 but the result **exits the last cell** — the partial sum
flows cell 0 → cell 1 → … → cell N-1, and only cell N-1 produces the finished
output. This is the single most common multi-cell trap:

- A harness or driver that derives its drive/drain from `placement.cells[0]`
  (the input landing cell) will inject correctly but **read nothing**, because the
  output never comes out of cell 0.
- The block's output port must be declared on the last cell, and anything that
  drains the block (the verification harness, a bus tap) must target that cell's
  position — not cells[0].

The fact above stands. **Corrected 2026-08-29:** a paragraph here used to call
draining the last cell "the capability to add (see INV-7)". That gap was closed
long ago — `verification/kyttar_verify/dut_runner.py` wires `x16_in -> block ->
x16_out` by PORT NAME and lets the router resolve the output cell, and 44
multi-cell blocks are verified through it, including an 84-cell one. (INV-7 was
corrected on the same date; the last copy of the stale "capability gap" clause
sat in INV-10 and was corrected 2026-09-03.)

### 3. Fold for the SHAPE of the free space, not for a width number

**CORRECTED 2026-08-29.** This section previously said "keep a block ≤ 8 cells
across … a block wider than 8 in either direction leaves no channel and the route
fails — silently," and called it "a hard constraint in practice." That was
**FALSE, and backwards for exactly the designs it mattered most to.** Shipped,
verified blocks that exceed 8 across: `FFT64Block` **9×12** (84 cells),
`FFT128Die1` 9×12, `FFT32Block` 9×10, `GRUCellBlock` **10×6**, and —
with no `CHIP_SCALE` waiver at all — `ComplexToMagBlock` **9×2** and
`CoherentRXBlock` **10×2** (a catalog composite exercised by
`examples/coherent_bpsk_rx/batch_check.py`; it has no manifest row of its own).
`layout_caps()` is read by no placer and no DRC; the ≤8 number was self-imposed
convention.

**The rule that is actually true:**

* **For a SMALL block, ≤8 across is a good default.** It leaves a routing channel
  on each side, which makes I/O placement forgiving. Keep doing it.
* **For a block that is a LARGE fraction of the array, go FULL width and declare
  `CHIP_SCALE`.** A full-width block leaves whole free ROWS — one contiguous
  through-channel. An 8-wide fold of the same block leaves *fragmented perimeter*,
  and a closed-ring block can never enclose a channel at all (a cycle cannot jump
  a gap). **Two more full-width ships, 2026-08-30:** `ChaCha20KeystreamBlock`
  (51 cells, **10x7**) and `Poly1305MACBlock` (100 cells, **10x10** — the
  largest block in the catalog), both bit-exact on the built chip with
  `CHIP_SCALE_ORIENTATIONS` declared.

**This is measured, not argued — see INV-40.** `GRUCellBlock` at the compliant
8×7 left five fragmented free rows and its chain stayed **one net short across
~8200 layouts**; the same 51 cells re-folded to the non-compliant **10×6** left
six full-width free rows and the identical chain routed and built at **102/120**.
Obeying the old ≤8 rule is what *prevented* that design from routing.

**The trade `CHIP_SCALE` demands:** nothing can reach the far side of a
full-width block, so its input and output must be on ONE edge, facing the chip
ports. Declare it via `CHIP_SCALE = True` (`_base.py`), which raises the cap to
the array dimensions.

Still true regardless of width: a 64-tap FIR at ~13 cells cannot be a 13×1 line —
not because 13 > 8, but because I/O at opposite ends of a line is the colocation
problem in §1.

### 4. The base-class auto-snake is a fallback, not a fold

If a block defines no `default_layout`, the base class snakes its cells into a
compact rectangle. That handles *compactness*, but it does **not**:
- guarantee I/O lands on the same edge (convention 1), or
- put the wavefront output cell where a bus can tap it (convention 2).

For any block where feedback returns to the front, or where the bus must tap both
I/O, **author an explicit `default_layout`** (`{cell_id: (dx, dy, face)}`) that
places the cells *and their faces* to satisfy conventions 1–3. Folding a 13×1
line into a 4×4 block with colocated I/O is a deliberate layout design — the
auto-snake will not do it for you.

### 4b. A multi-input FACE-LOCKING block: the rendezvous cell must be a LEAF

If a block declares `NEEDS_DISTINCT_INPUT_FACES` (the LOCK-rotation rendezvous
family — see INV-46), its input cell tells its N producers apart by ARRIVAL FACE.
A cell has four faces, and that cell needs:

    N (one per arm) + 1 (forward into the datapath) + 1 (a release corridor back)

At **N=2** that is 4 and fits. (Corrected 2026-09-03: this used to say "the
shipped N=2 blocks are single-cell" — XorJoin, DualFloatToComplex,
FeaturePairJoin and ClarkeTransform are, and need neither a forward nor a
release; `SVPWMBlock` is N=2 and SEVEN cells, 7×1, with both.) At **N=3** it is
FIVE, and the cell has four. The consequences are layout consequences, so they
belong here:

- **The rendezvous cell must be a LEAF of the fold** — exactly ONE in-block
  neighbour — so three faces stay free for the three arms. A compact 2×2 square
  (the obvious 4-cell fold) gives EVERY cell two in-block neighbours and cannot
  host an N=3 rendezvous: the maze router reports *"no free DISTINCT-face broker
  for a face-locking block's input"* and the chain does not route at all.
- **The rendezvous CELL must be a leaf; the rest of the fold is free.**
  (Corrected 2026-09-03: this used to say "such a block is therefore a CHAIN,
  not a square" — over-stated. `TMRVoterBlock` (N=3, 4×1) is the chain case, but
  `CordicRotateBlock` (N=3: x/y/theta, 20 cells) ships a 5×5 SERPENTINE whose
  rendezvous is a leaf at (0,0) with column 0 holding only the rendezvous.) A
  chain is affordable where it is chosen because the block's inputs land on one
  cell from N different sides rather than tapping a single bus edge, so the
  co-located-I/O convention (§1) does not apply to it; keep any chain ≤8 across
  (§3) — that still binds for a small block.
- **The serialize-LOCK release cannot have a corridor of its own** at N=3; it must
  ride back through the one abutting cell. A dedicated `transit_*` unlock lane
  (§5, the ComplexMixer pattern) needs a face to enter the rendezvous on, and
  there is none — the placer/DRC then reject the layout because an arm loses its
  face. See INV-46 Rule 3 for what that costs in pipeline depth.
- **N ≥ 4 is not placeable** as a single rendezvous. Build a TREE of N=2/N=3
  rendezvous blocks.

### 5. Internal feedback/"transit" cells are FIRST-CLASS block cells

A `default_layout` entry whose `cell_id` starts with `transit_` (e.g.
`transit_fb_0`) is a block-INTERNAL routing/feedback relay: a face-only cell (a
FACE direction, no program) that carries a feedback value back to the front of
the fold. These are **first-class block cells**, not second-class routing cells:

- They live in `Placement.cells` like every program cell (tagged by the
  `transit_*` id — that prefix is how the build/router/DRC recognise them as
  face-only, program-less relays).
- They render with the **owning block's colour/label/identity** (NOT the
  light-blue inter-block route look).
- They **count in the block's footprint** — `Placement.bounding_box()` and the
  PortMap footprint (bbox / `io_colocated` / auto-place area) include them.
- They transform rigidly with the block (the D4 pivot spans them).
- They follow the same overlap/DRC rules as any block cell.

`is_transit_cell(cell)` (`model/placement.py`) is the single source of truth for
the tag. `Placement.transit_cells` is a read-only filtering VIEW of `cells` for
the router/DRC read-sites; do NOT construct a separate transit list. (The
light-blue `CellKind.TRANSIT` look is reserved for INTER-block connection route
waypoints — the bus spine between blocks — never for cells inside a block.)

---

## How the auto-placer ORIENTS a block (flyline minimisation)

The conventions above shape a block in isolation. When the auto-placer
(`engine/autoplace.py`) lays a flow-ordered pipeline, it ALSO chooses each
block's D4 orientation — and it does so to **minimise routing (flyline) length**,
not merely to point the output along the bus. Two rules drive that choice:

**(a) Orient each block so its INPUT is nearest its driver and its OUTPUT is
nearest its consumer.** For every candidate orientation (identity + the four
primitive transforms, via `PortMap.transformed`) the placer computes where the
block's input and output cells would physically land at its band, then scores the
total Manhattan flyline to its *actual* neighbours: input cell → the upstream
driver's output cell (already placed, so this is known exactly; for the pipeline
lead it is the chip input port), and — when the block feeds a chip OUTPUT port —
output cell → that port. The minimum-flyline orientation wins. The
input-toward-driver term dominates because the driver is placed; an unplaced
downstream BLOCK consumer is handled by a tie-break (the output should face the
bus continuation / travel direction), never a fabricated distance that could
swamp the real term. This replaces the old "output merely faces the travel
direction" heuristic, which could seat a block's input FARTHER from its driver
than its output — backwards, and longer to route.

**(b) Prefer the fold aspect that CO-LOCATES I/O on the bus-facing edge.** When
two orientations tie on flyline, the placer's sort key is
`(flyline, travel_rank, colo_rank, ident_rank)` (`autoplace.py`): it FIRST prefers
an output facing the travel direction, THEN the one whose input and
output sit on the same bus-facing edge (`PortMap.io_colocated` — convention 1's
cheap 1-D tap), then identity (never transform a block needlessly). So a block
that can fold either W×H or H×W is oriented to keep both ports tappable from one
bus edge, shortening the route — exactly the 4-wide-vs-5-wide choice that decides
whether a wavefront's I/O end up on the same side.

**Exception — internal-feedback blocks are left as-authored.** A block that
declares INTERNAL connections/jumps (a Costas/Gardner-style loop, the complex
matched filter) hardcodes per-cell FACES in its assembly — a dual-face emit or a
feedback return that rests at a specific direction the build's feedback tracer
follows. A D4 transform rotates the PortMap geometry but NOT that
direction-specific program, so reorienting such a block silently breaks its loop
(the receiver recovers nothing). The placer detects internal feedback and keeps
those blocks at identity; author their `default_layout` to fold I/O on the bus
edge directly (convention 1). Feed-forward wavefronts (e.g. FIR) declare no
internal connections — their forwarding faces come from `default_layout` and DO
transform correctly — so they remain freely orientable.

**The route-pass orient must agree with the placer.** Route All
(`auto_route_all`) runs an orient pass before routing
(`AutoRouter.orient_for_flow`). **Updated 2026-09-03:** that pass is now the SAME
full-flyline optimiser as the placer (`autoroute.py` documents that it MUST agree
with the placer's own flyline-minimising orient), not the older
"output faces the dominant consumer" heuristic this paragraph used to describe;
it also skips any block whose input cell is anchored on a chip input port. The
historical bug is still the reason the two must agree: a pass that scores the
as-authored catalog PortMap instead of the block's *current* orientation
re-recommends a rotation the flyline placer already applied (e.g. a
vertical-column FIR turned `ccw` so its input lands nearest its driver), which
composes on top (`ccw∘ccw` = 180°) and flips the block back — input FARTHEST
from its driver. `orient_for_flow` composes `placement.orientation` onto the
PortMap before scoring, so an already-correctly-oriented block is left untouched
(suggestion `None`). Regression:
`placekyt/tests/test_fir_orient_input_near_driver.py`
asserts the input-near-driver invariant survives the full place+route flow.

---

## Checklist for a multi-cell block on this chip

- [ ] Footprint chosen for the SHAPE of the free space it leaves (§3): ≤8 across
      for a small block; FULL width + `CHIP_SCALE` for a block that dominates the
      array. NOT a bare width check — see INV-40.
- [ ] I/O ports on the **same** edge where practical (`io_colocated=True`).
      A preference, not a precondition — about half of the shipped blocks
      (60 of 120 probed) are NOT colocated and route fine; the placer uses it
      only as a tie-break. For a
      FULL-WIDTH `CHIP_SCALE` block the same-PORT-FACING-EDGE half is mandatory
      (no reachable far side), but the 2-cell colocation span is not —
      ChaCha20Keystream's terminals sit nine columns apart, its landing one row
      below the port row (`seq` (0,1) vs `out` (9,0)), and a
      non-full-width chip-scale block (FFT64, 9x12) even ships I/O on opposite
      edges (corrected 2026-08-30; see §1).
- [ ] Wavefront output port declared on the **last** cell; anything draining the
      block targets that cell, not cells[0].
- [ ] Feedback producers/consumers folded adjacent (no full-width return path).
- [ ] An explicit `default_layout` if the auto-snake can't satisfy the above.
- [ ] Verified: the block actually **routes and produces output** in a real
      place+route+build+sim — not just that it builds. A block that builds but
      doesn't route looks identical to a working one until you run it.

---

## Oscillators & fan-out — translate GNU Radio graphs, don't transliterate them

**This chip is CLOCKLESS.** Every cell executes only when a neighbour sends it a JUMP;
there is no internal clock and no self-incrementing counter. Three consequences that trip
up every analog demo (AM, SSB, anything with a local oscillator):

1. **There are NO free-running sources.** An NCO here is NOT a source — its own contract is
   "one input TRIGGER → one output sample". It has input registers so something can JUMP it
   per sample. A GNU Radio `analog.sig_source`/NCO drawn as a source is host-clock-pulled;
   when that graph is imported, the NCO ends up with **no input connection → nothing triggers
   it → it never emits → the whole chain is dead**. placeKYT places it faithfully (no input
   port ⇒ nothing to route). The FLOWGRAPH is wrong for the fabric, not the tool.

   **Fix: FUSE the oscillator into its consumer.** Use a mixer-with-built-in-oscillator
   (`kyttar_complex_mixer` → yi=sig·cos, yq=sig·sin as two ports; or `kyttar_iq_upconvert` →
   `xi·cos − xq·sin`, one real rail per input). The arriving sample is BOTH the trigger AND
   the data, and the mixer runs its own carrier. No standalone NCO.

2. **A cell's output goes to exactly ONE consumer** (writing to two clobbers). So a SHARED
   oscillator feeding N mixers is fan-out that costs a splitter/broker (N-arm fan-out is
   brokered by `StreamSplitterBlock` — it works, it is just not free). **REPLICATE the
   cheap phase-accumulator per consumer instead of sharing it.** Cells are plentiful (120);
   wires/fan-out are the scarce resource. Two mixers at the same freq, each started at phase
   0 from sample 0, stay coherent — no shared carrier needed.

3. **Match the FUNCTION to the fabric, not GR's literal block.** (Same lesson as
   QuadratureDemod: `atan2`-then-difference IS a MAC-only discriminator.) A `sig_source →
   multiply` pair is a host-clock idiom; on-chip it's one fused oscillator-mixer.

**Worked examples (all in `examples/`):**
- **FM** — the model to emulate: VCO (input-driven phase accumulator) + discriminator (MAC,
  no oscillator). No shared oscillator anywhere → tiny (12 cells), routes trivially.
- **AM (DSB)** — `audio → iq_upconvert@fc → iq_upconvert@fc → LowPass → Gain`. Each mixer
  self-carriers; 4 blocks, 33 cells, routes 6/6 (figures re-read from the shipped .kyt
  2026-09-03).
- **SSB Weaver** — down-mix = 1 ComplexMixer (both rails); each up-mix = 1 lean IQUpconvert
  (one rail: `xi`→cos, `xq`→−sin, so the combine is an ADD). 7 blocks, 67 cells, routes
  13/13, no NCO, no fan-out.
  (SSB is *inherently* 4 mixers + 2 filters/half — "big" is the algorithm, not the fabric.)

**Block-choice cheat sheet for a carrier multiply:**
- Need BOTH cos & sin rails from one signal → `kyttar_complex_mixer` (11 cells, 2 out ports).
- Need ONE rail → `kyttar_iq_upconvert` (6 cells): `xi`→`sig·cos`, `xq`→`−sig·sin`. Prefer
  this where you can — it's ~half the cells, which is what makes dense chains (SSB) fit.
- Never import a graph with a standalone `kyttar_nco` feeding mixers and expect it to run.
