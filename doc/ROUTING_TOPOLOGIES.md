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

### Backbone threading (clean simple-path bus-snake) + placement gates (2026-06-30)

The bus-snake placer + v2 threader are now a tight **co-design** that builds ONE clean
SIMPLE-PATH backbone (crossover empty by construction):

* **`AutoPlacer._pack_bus_snake`** lays every block (all filaments) along a 3-corridor
  boustrophedon: blocks pack into the interior columns; **col 0** is the LEFT rail (entry
  + the drop after a left-going band), **col W-2** the RIGHT drop rail (drop after a
  right-going band), **col W-1** the dedicated EGRESS rail (the terminal climb to the
  output-port corner, never a drop → no rail double-use). The number of bands is forced
  ODD so the last band is right-going and egresses up the conflict-free right rail. Row 0
  (the shared port row) stays free as the top lane. The band partition
  (`_bus_snake_band_assign`) is a flow-respecting bin-pack (each filament kept in flow
  order, cross-filament interleave free) that fits the modem in **3 bands** (width budget
  7/8/8). The placer EMITS the exact ordered backbone path as the spine.
* **`_route_chip_bus_v2`** rides that ordered spine VERBATIM (a fast path that validates
  the spine is a simple, contiguous in→out path abutting every tap) — no myopic greedy
  re-derivation. The greedy threader is retained as a robust fallback (wall-hugging cost +
  connectivity guard + a per-tap PREFERRED-abut hint from the placer's lane, + an egress
  corridor reservation). Each tap abuts the EARLIEST backbone cell beside its input (so a
  block's source never lands downstream of its own consumer when the egress rail runs past
  it).
* **`controller._derive_spine(bus_snake=…)`** derives the spine from the SAME bus-snake
  placer when (and only when) the live placement matches the snake plan
  (`_placement_is_bus_snake`); the multi-filament fallback keeps the generic spine.

`auto_place(use_bus="always")` PREFERS the bus-snake layout when it passes three gates:
`_plan_on_grid`, `_plan_ports_reachable`, **and** no single-cell in==out deadlock
(`_bus_snake_sc_deadlock`, see below). Otherwise it falls back to the proven on-grid
MULTI-FILAMENT regions (tall designs — the 110B front end — stay on that path).

### Single-cell in==out split (§5.3) and the remaining BPSK-modem TX-corr limitation

A SINGLE-CELL block both receives its input and drives its output on its ONE cell. On a
straight bus lane both transactions land on the SAME face → the §5.3 single-outstanding
deadlock. The bus router avoids it when the block sits at a backbone **BEND** — abutting
TWO backbone cells (Δindex ≤ 2) on DIFFERENT faces: input is delivered from the earlier,
output emitted from the nearby later cell (`sc_out_tap`, conditioned on staying upstream of
the consumer). Single-cell blocks at the band ENDS (the lane→rail bends) get this for free;
the placer orders single-cell blocks toward band ends (`_order_band_single_cell_last`).

For the full-duplex BPSK modem this fixes **mapper** and **slicer** but **NOT upsampler**,
so the snake is **rejected** by the deadlock gate and the modem falls back to the legacy
multi-filament path (auto TX corr ~0.024). The mechanism is a **geometric over-constraint**,
now exactly characterised:

* The modem's 8 blocks total 23 cells; the array is 10 wide; reserving the three vertical
  corridors leaves width budgets **7 / 8 / 8** → exactly **3 bands** (a 4th band overflows
  to the bottom port row, and an even band count makes the egress rail collide with an
  inter-band drop).
* A 3-band layout has exactly **three bend ends** (band-1 right end, band-2 left end,
  band-3 egress end). There are **three** single-cell blocks (mapper, upsampler, slicer),
  but the dataflow + width constraints force **two of them into one band**: the 4-wide
  blocks (rrc, iqup, costas) with their TX/RX ordering (mapper<upsampler<rrc<iqup,
  MF<costas<gardner<slicer) admit no 3-band partition that puts each single-cell at a
  distinct bend — a band ending in a single-cell tops out at width **7** (4+2+1), so two
  single-cell-ended bands hold ≤14 cells but the post-band-1 remainder is 16. Verified by
  exhaustive partition search: no fitting 3-band assignment gives all three single-cells a
  bend, so `upsampler` always lands mid-lane and deadlocks.
* This is **not** a threading or reference bug: with the deadlock gate disabled the
  bus-snake threads ALL 11 nets on one clean simple-path backbone with `crossover_plan`
  empty — only the build's single-cell DRC (correctly) rejects the upsampler cell. The
  value-exact `_tx_reference` still correlates 0.9993 with the acceptance reference, so a
  bend-placed upsampler WOULD score ~0.999.

Reaching corr > 0.95 requires giving upsampler a backbone bend WITHOUT breaking the 3-band
fit — e.g. an array ≥1 column wider (width budgets 8/8/8 → each single-cell at its own
bend), a block-merge that removes a 1-cell footprint, or a backbone that DIPS one cell at a
mid-lane single-cell block (places the block ON the lane and routes the bus around it,
giving it W/S/E backbone faces — a backbone-construction change, deferred). Until then the
explicit-placement duplex (`test_live_duplex_stream_id`,
`test_modem_grc_import_duplex_e2e`) remains the value-exact TX gate, and
`test_auto_pnr_tx_passband` keeps its envelope/symbol gates (it does NOT assert the > 0.95
sample corr — honestly not reachable on the auto path for THIS array width).

## Acceptance (the rebuild is done when)

- BPSK modem (`examples/bpsk_modem/bpsk_modem.grc`), auto-place + Bus route + build:
  auto TX passband corr > 0.95 vs the proper passband reference
  (`_tx_signal(sps=4) × cos(2π·0.125·n)`); RX recovers bits BER 0; all nets routed.
  **(RX BER 0 + all nets routed MET; TX corr NOT met on THIS 10-wide array — the clean
  simple-path bus-snake threads all 11 nets crossover-empty, but the §5.3 single-cell bend
  budget is one short for the modem's 8 blocks → the deadlock gate falls back to the legacy
  multi-filament path. Precisely characterised in the §single-cell limitation above.)**
- The full auto-P&R / router / duplex / coherent-RX regression suite stays green.
- The three topologies each have a focused test (bus multi-filament ordering, ring loop,
  block-to-block abutment minimisation).
