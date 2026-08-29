<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Block layout — folding, edges, and the bus

How a multi-cell block is **shaped on the array** is not cosmetic — it decides
whether the block can be placed and routed at all on this chip. None of what
follows is enforced by a DRC or a warning: the build will happily accept a block
that violates these conventions and then **silently fail to route** (no output,
no error pointing at the cause). An agent that does not know these rules will burn
a long time rediscovering them. Read this before authoring or reshaping any
block bigger than one cell.

> **Scope note — this is a CONVENTION, not an architectural rule.** The pressure
> here comes entirely from this chip being **10×12** — very few cells, very few
> routing channels to spare. On the larger chips that come later, the width
> convention (below) stops mattering and would be wrong to enforce. So nothing in
> the codebase flags a violation, *by design*. But on **this** chip, breaking
> these conventions means the block doesn't work. Treat them as hard for now.

---

## Why blocks must fold (the Costas cautionary tale)

A block laid out as a long straight line is almost always wrong. The worst case
we hit: an early Costas loop was **7 cells in a row plus a 6-cell full-width
return path** to carry a *one-sample* feedback value back to the front — 13 cells
to delay a single sample. The feedback had to travel the entire width of the
block because the producer (last cell) and consumer (first cell) sat at opposite
ends.

Folding the same datapath into a compact **~2×4 block** put the producer and
consumer one cell apart, so the feedback returns in ~1 hop instead of 6. Same
program, same DSP, a third of the cells and none of the full-width return route.

**The rule that falls out:** lay a multi-cell block out as a **serpentine fold**
(snake), not a line. Wrap the datapath across rows so that cells which must talk
to each other end up physically adjacent.

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
forces it. But a block that is **not** I/O-colocated will not tap a single bus,
which on this chip means it does not route. So treat `io_colocated=True` as a
design *target* you achieve by where you place your ports, not a flag you set.

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

If you are extending the verification harness for a wavefront block, this is the
capability to add: drive cell 0, drain the **last** cell. (See INV-7 — this is the
known FIR limit.)

### 3. Keep a block ≤ 8 cells across (this chip only)

The array is 10 wide. A bus needs one routing channel of cells on each side of a
block to get traffic past it. 8 (block) + 1 (channel) + 1 (channel) = 10 = the
whole width. A block **wider than 8 cells in either direction leaves no channel**
for the bus to pass, and the route fails — silently, again.

So fold to keep both dimensions ≤ 8. A 64-tap FIR at 5 taps/cell is ~13 cells —
that **cannot** be a 13×1 line (too wide *and* I/O at opposite ends). It must fold
to something like 4×4 or 5×3 with I/O colocated on one edge.

This is the convention that evaporates on bigger chips. On a 10×12 it is a hard
constraint in practice.

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
family — see INV-45), its input cell tells its N producers apart by ARRIVAL FACE.
A cell has four faces, and that cell needs:

    N (one per arm) + 1 (forward into the datapath) + 1 (a release corridor back)

At **N=2** that is 4 and fits — and the shipped N=2 blocks are single-cell, so
they need neither a forward nor a release. At **N=3** it is FIVE, and the cell has
four. The consequences are layout consequences, so they belong here:

- **The rendezvous cell must be a LEAF of the fold** — exactly ONE in-block
  neighbour — so three faces stay free for the three arms. A compact 2×2 square
  (the obvious 4-cell fold) gives EVERY cell two in-block neighbours and cannot
  host an N=3 rendezvous: the maze router reports *"no free DISTINCT-face broker
  for a face-locking block's input"* and the chain does not route at all.
- **Such a block is therefore a CHAIN, not a square** — a longitudinal strip,
  which is normally exactly what §1–§3 warn against. It is FORCED here, and it is
  affordable because the block's inputs land on one cell from N different sides
  rather than tapping a single bus edge, so the co-located-I/O convention (§1)
  does not apply to it. Keep the chain ≤8 across (§3) — that still binds.
- **The serialize-LOCK release cannot have a corridor of its own** at N=3; it must
  ride back through the one abutting cell. A dedicated `transit_*` unlock lane
  (§5, the ComplexMixer pattern) needs a face to enter the rendezvous on, and
  there is none — the placer/DRC then reject the layout because an arm loses its
  face. See INV-45 Rule 3 for what that costs in pipeline depth.
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
two orientations tie on flyline, the placer prefers the one whose input and
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

**The route-pass orient must respect the placer's applied orientation.** Route
All (`auto_route_all`) runs a SECOND, older orient pass before routing
(`AutoRouter.orient_for_flow`, "output faces the dominant consumer"). That pass
scores a block's output face — but it must score against the block's *current*
orientation, NOT the as-authored catalog PortMap. The flyline placer may have
already rotated the block (e.g. a vertical-column FIR turned `ccw` so its input
lands nearest its driver); the bare PortMap still reads as the un-rotated layout,
so scoring it re-recommends the SAME rotation, which then composes on top
(`ccw∘ccw` = 180°) and flips the block back — input FARTHEST from its driver
(the bug). `orient_for_flow` therefore composes `placement.orientation` onto the
PortMap before scoring, so an already-correctly-oriented block is left untouched
(suggestion `None`). Regression: `tests/test_fir_orient_input_near_driver.py`
asserts the input-near-driver invariant survives the full place+route flow.

---

## Checklist for a multi-cell block on this chip

- [ ] Both dimensions of the footprint ≤ 8 cells.
- [ ] External input and output ports on the **same** edge, within ~2 cells →
      `portmap` reports `io_colocated=True`.
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
   oscillator feeding N mixers is expensive fan-out the router must broker. **REPLICATE the
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
  self-carriers; 4 blocks, routes 5/5.
- **SSB Weaver** — down-mix = 1 ComplexMixer (both rails); each up-mix = 1 lean IQUpconvert
  (one rail: `xi`→cos, `xq`→−sin, so the combine is an ADD). 77 cells, no NCO, no fan-out.
  (SSB is *inherently* 4 mixers + 2 filters/half — "big" is the algorithm, not the fabric.)

**Block-choice cheat sheet for a carrier multiply:**
- Need BOTH cos & sin rails from one signal → `kyttar_complex_mixer` (11 cells, 2 out ports).
- Need ONE rail → `kyttar_iq_upconvert` (6 cells): `xi`→`sig·cos`, `xq`→`−sig·sin`. Prefer
  this where you can — it's ~half the cells, which is what makes dense chains (SSB) fit.
- Never import a graph with a standalone `kyttar_nco` feeding mixers and expect it to run.
