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
