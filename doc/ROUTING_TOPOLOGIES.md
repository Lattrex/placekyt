# placeKYT Routing Topologies (the router rebuild)

This is the authoritative spec for placeKYT's auto-router. The router's ONLY job is:
given placed blocks and the logical nets between them (and to/from the chip I/O ports),
produce physical routes (waypoint paths + the broker/tap cell programs the build
compiles) that deliver every word correctly with the minimum total route length, under
ONE of three topologies. The topology is **user-selected with a smart default**.

The mental model is deliberately simple. Earlier code grew a per-net BFS with a pile of
collision patches (single-fwd_face guards, through-face overrides, hazard ordering,
"no free broker cell" failures). That approximated a bus but broke when two filaments
shared the array (e.g. the BPSK modem's TX + RX filaments off one input port). This spec
replaces that with the three explicit topologies below.

## Vocabulary

- **Filament**: one signal chain — a linear sequence of blocks in dataflow order
  (e.g. TX = mapper → upsampler → RRC → IQUpconvert; RX = matched-filter → Costas →
  Gardner → slicer). A design may have several filaments that share the chip input
  and/or output port.
- **Tap / broker cell**: a routing cell ON the backbone that delivers a word INTO a
  block (flip face → WRITE+JUMP into the block input cell → restore to the backbone
  direction). A tap cell has an **incoming** backbone direction and an **outgoing**
  backbone direction; it can serve deliveries to blocks on BOTH sides. **Two (or more)
  blocks tapping off one cell is normal and legal**, not a conflict — the cell forwards
  HOP<31 through-traffic on its backbone face and fires a deliver entry per landing
  (HOP==31) word.
- **Backbone**: the shared directional path (bus or ring) that carries every filament's
  words; blocks hang off it as taps.

## The three topologies

### 1. BUS (default when >1 filament shares a port)

Lay down the **minimum-length route from the input port to the output port**. Every
block taps off this single backbone. Rules:

- The backbone is ONE directional path IN → OUT. Words flow one direction along it.
- Every block is attached as a tap off the backbone.
- **Ordering constraint (the ONLY ordering rule): within each filament, that filament's
  blocks must appear along the backbone in signal-flow order.** That is the whole
  constraint. Across filaments there is NO ordering constraint — taps may interleave
  freely (RX-block, TX-block, TX-block, RX-block, …) in whatever interleaving minimises
  the backbone length. (Confirmed by CM 2026-06-29: "the only thing that matters is that
  for a particular filament, THEIR blocks are in order with respect to their particular
  filament.")
- A word destined for a block farther down the backbone simply transits the intervening
  tap cells (HOP<31 → forwarded on the backbone face). It lands (HOP==31) only at its own
  tap. Demux is by the JUMP entry the source addressed + the WRITE dest reg. So multiple
  filaments coexist on one backbone with no interleave hazard: ordering at any fan-in is
  enforced structurally by the per-word hop budget + face routing, never by timing.
- Goal: **minimum backbone length** such that every block taps off it and each filament's
  taps are in order. The backbone prefers the placement spine (serpentine) when one is
  supplied, but the router may thread its own backbone if that is shorter.

### 2. RING (closed bus)

Identical to BUS, but the backbone is a **closed loop**: input port → output port → back
to input port. Blocks tap off the ring. Because the path is a loop, **the per-filament
signal-flow ordering constraint relaxes** — a block may tap anywhere on the ring, since a
word can always continue around to reach the next tap. Goal: minimum ring length with all
blocks tapped.

### 3. BLOCK-TO-BLOCK (no backbone)

No shared bus/ring. Each block's output connects directly to the next block's input,
minimising **total route distance across all filaments**. Abutting blocks (zero route,
output face abuts the next block's input cell) are ideal and preferred. This is the
default for a single short filament. Goal: minimise the sum of per-edge route lengths;
prefer abutment.

## Topology selection (smart default)

Exposed in the Route All / GRC-import dialog as `Auto / Bus / Ring / Block-to-block`.
`Auto` picks:

- **> 1 filament sharing a chip port → Bus.** (The shared backbone is what lets multiple
  filaments coexist; the BPSK modem is this case.)
- **single filament → Block-to-block.** (A lone chain wants tight abutment, no backbone
  overhead.)
- Ring is never auto-selected; it is an explicit user choice (feedback designs / when the
  user wants the ordering constraint relaxed).

## Why the old model failed (the bug this rebuild fixes)

The old per-net BFS routed each net independently and "coalesced" later nets onto earlier
ones via a `bus_dir` single-fwd_face rule. With two filaments off one port it produced a
cell that was BOTH a broker delivering one filament's word in one direction AND a foreign
filament's through-transit in a conflicting direction (the BPSK modem's cell (1,2):
broker for TX mapper→upsampler delivering SOUTH, while the RX input corridor transited it
EAST). One cell, one fwd_face, two conflicting roles → the streams interleaved and the TX
symbol stream was corrupted (auto TX passband corr 0.024 vs the explicit placement's
0.999).

The fix is NOT more collision guards. It is to build ONE backbone first (bus/ring) and
attach every block as an ordered tap off it — so a "tap cell serving two blocks" is the
designed-for normal case (incoming + outgoing backbone direction, deliver entry per
block), and there is never a foreign through-transit fighting a tap, because there is only
ONE backbone and everything rides it.

## Invariants the build + DRC must keep enforcing (unchanged, reused)

- A tap/broker cell: incoming backbone face, per-landing deliver entries (flip → WRITE+JUMP
  → restore to backbone face), HOP<31 through-traffic forwarded on the backbone face.
- Source addresses its tap's deliver entry + hop; the build derives taps from the routed
  backbone (broker_plan) and compiles cell programs (build._apply_brokers).
- >31-hop backbone segments get relay cells (§1.4).
- Single-cell blocks that both receive and drive on one cell keep input-face != output-face.
- Every kept route is a real path; any failure is NAMED, never a silent wrong build.

## Implementation status (2026-06-30)

The BUS topology is wired end to end:

- **Topology selection** — `controller._select_topology()` returns `"bus"` when >1
  filament feeds the chip input port, else `"block"` (single-filament → block-to-block).
  Threaded through `auto_route_all` → `_run_router` → `_bus_route` → `route_all_bus`.
- **`route_all_bus(topology=…)`** — for `"bus"`/`"ring"` it tries the single-backbone v2
  router (`_route_chip_bus_v2`) FIRST, and uses it ONLY if it routes EVERY net AND the
  DRC gate passes them all; otherwise it DISCARDS v2 and falls through to the legacy
  per-net loop UNCHANGED (a partial v2 never displaces the proven path). Default
  `topology="block"` keeps every existing direct caller byte-identical.
- **`_route_chip_bus_v2`** — routes all nets on one shared bus with
  `forbid_broker_transit=True` (a new, default-False option on `_route_chip_bus` /
  `_bus_bfs`): NO foreign net may TRANSIT a broker cell, so a broker is never also a
  conflicting through-transit (the one-cell-two-roles corruption). Plain transit cells
  are still shared (same direction, via `bus_dir`). This makes `crossover_plan` empty
  and `broker_through_face` conflict-free *by construction* for any design it routes.

### Backbone threading (robust) + placement gates (2026-06-30)

`_route_chip_bus_v2`'s backbone threader is now **robust**: it threads ONE contiguous
SIMPLE path input-port → (a free cell abutting each tap, in tap order) → output-port,
keeping the free region connected so a clean backbone is found whenever one EXISTS. Two
guards do this (replacing the old shortest-path-per-segment that WALLED the array): a
**wall-hugging** segment cost (prefer cells adjacent to obstacles / the committed
backbone / the border, so the path clings to walls instead of slicing open space) and a
**connectivity guard** (a segment is accepted only if, after committing it, every
remaining tap AND the output port stay reachable from the new head; otherwise the next
abutting-cell / candidate is tried, and the thread BAILS soundly to the legacy loop if
none keep connectivity). With one cell per travel direction the result keeps
`crossover_plan` empty by construction.

`auto_place(use_bus="always")` now PREFERS the co-designed bus-snake layout
(`with_bus_snake(True)`, `band_margin=0`) when it passes two acceptance gates
(`controller._plan_on_grid` + `controller._plan_ports_reachable`): every block footprint
on-grid AND every chip I/O port still reachable from the free-cell region. Otherwise it
falls back to the proven on-grid MULTI-FILAMENT regions (so tall designs — the 110B front
end — stay on the path the placer's own >1-filament detection already covers).

### Known limitation (BPSK modem auto TX corr — STILL OPEN, now precisely root-caused)

For the full-duplex BPSK modem the auto TX passband corr is still ~0.024 (legacy
fallback), NOT > 0.95. The root cause is a **placement-level / dataflow-topology
obstruction**, not a threading bug:

* The duplex modem is a **Y that reconverges**: ONE input port (`x16_in`) forks into TWO
  filaments (TX mapper→…→IQUpconvert, RX matched-filter→…→slicer) that **reconverge at
  ONE output port** (`x16_out`). A single backbone is a simple path in→out; both
  filaments are fed from its FIRST cell and feed its LAST cell.
* On BOTH placements the placer actually produces — the `with_bus_snake` snake (which
  also **seals the output port**: the upsampler lands at (9,2) and the matched filter at
  (8,0–1), walling (9,0)→(9,1) into a dead 2-cell pocket — so `_plan_ports_reachable`
  rejects it) AND the on-grid MULTI-FILAMENT regions (output port reachable) — **no
  simple-path single backbone exists** that visits all eight taps in per-filament flow
  order and ends at the output port (verified by exhaustive abut-cell backtracking over
  the sensible tap orderings: the path to a center/bottom tap cuts the free region and
  strands a later bottom tap). So v2 soundly returns `None` → the legacy multi-broker
  loop routes all 11 nets, but it must let the RX input net (`net6`, port→MF) **transit
  TX brokers** (mapper's broker (0,1) and the mapper→upsampler broker (1,2)); the RX word
  passing through a TX deliver cell corrupts the TX symbol stream (corr 0.024 = noise,
  far below even the 0.85 envelope).
* This is NOT a reference mismatch: the demo's value-exact `_tx_reference` correlates
  **0.9993** with the acceptance reference `_tx_signal(sps=4)·cos(2π·0.125·n)`, so a clean
  build WOULD score ~0.999.

Reaching corr > 0.95 requires EITHER a **height-aware, clean-serpentine bus-snake placer**
that lays the duplex so a single simple-path backbone threads with zero foreign broker
transit (a placer redesign — out of scope of this router work), OR a **reuse-relaxed
backbone** (one-out-direction-per-cell, allowing the egress to return up an unused column)
plus build / `broker_plan` / `crossover_plan` support for a backbone that revisits cells
(a build-side change — also out of scope, high blast radius). Until then the
explicit-placement duplex (`test_live_duplex_stream_id`, `test_modem_grc_import_duplex_e2e`)
remains the value-exact TX gate, and `test_auto_pnr_tx_passband` keeps its envelope/symbol
gates (it does NOT assert the > 0.95 sample corr — honestly not reachable on the auto path
with the current placer).

## Acceptance (the rebuild is done when)

- BPSK modem (`examples/bpsk_modem/bpsk_modem.grc`), auto-place + Bus route + build:
  auto TX passband corr > 0.95 vs the proper passband reference
  (`_tx_signal(sps=4) × cos(2π·0.125·n)`); RX recovers bits BER 0; all nets routed.
  **(RX BER 0 + all nets routed MET; TX corr NOT met — blocked by the duplex-Y / placer
  obstruction precisely root-caused in the Known limitation above, not by the threader.)**
- The full auto-P&R / router / duplex / coherent-RX regression suite stays green.
- The three topologies each have a focused test (bus multi-filament ordering, ring loop,
  block-to-block abutment minimisation).
