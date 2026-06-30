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

### Known limitation (BPSK modem auto TX corr)

The co-designed `with_bus_snake` placement (lay ALL filaments along ONE serpentine so
the placement IS the backbone) currently (a) OVERFLOWS the 10×12 grid for the modem
(the single serpentine of the tall RX folds needs ~13 rows — fixable only with a
height-aware compact packer, which lives in the placer) and (b) when forced to fit
(`band_margin=0`) produces a scattered, oddly-oriented layout the bus cannot thread.
The on-grid MULTI-FILAMENT placement (two stacked filament regions) packs the modem
densely enough that NO net ordering routes every net WITHOUT some foreign broker transit
(exhaustively checked: best is 9/11 under the no-foreign-transit constraint). So for the
modem v2 returns `None` and the router falls back to the legacy loop, leaving auto TX
passband corr at the pre-existing ~0.024. Reaching corr > 0.95 (the acceptance below)
requires a **height-aware, clean-serpentine bus-snake placer** so the single backbone
threads trivially with zero foreign broker transit — a placer change, out of scope of
this router work. The explicit-placement duplex remains the value-exact TX gate.

## Acceptance (the rebuild is done when)

- BPSK modem (`examples/bpsk_modem/bpsk_modem.grc`), auto-place + Bus route + build:
  auto TX passband corr > 0.95 vs the proper passband reference
  (`_tx_signal(sps=4) × cos(2π·0.125·n)`); RX recovers bits BER 0; all nets routed.
  **(TX corr NOT yet met — see Known limitation above.)**
- The full auto-P&R / router / duplex / coherent-RX regression suite stays green.
- The three topologies each have a focused test (bus multi-filament ordering, ring loop,
  block-to-block abutment minimisation).
