"""Bus/broker auto-router — the §1.2 active-control-fabric model (auto-P&R P3.1).

This is the central router the design (`the auto-P&R design notes` §1.2) calls for and that
the BFS corridor router (`autoroute.py`) and the CP-SAT router (`cpsat_router.py`)
do NOT implement: a **directed bus** of routing cells that snakes input→output along
the placement spine, with **blocks abutting the bus** and a programmed **BROKER**
cell (a flip→relay→restore cell, the proven `SplitterBlock` pattern) wherever a
net's source/target taps the bus.

Why this is the win over the prior routers (§11.2/§11.3):
- The corridor router keeps every net on DISJOINT cells, so a densely-packed chain
  (the 18-cell coherent RX) runs out of free corridors — net4/5/6 fail "no free
  corridor". The bus model lets nets SHARE the spine (sequential, tagged), so they
  coexist.
- The CP-SAT router shares a cell ONLY when both nets fan out to a COMMON sink (a
  plain transit cell can't demux). The bus model adds programmed brokers, so
  DIFFERENT-sink streams legally share the spine: each peels off at its OWN broker
  (selected by the JUMP entry it carries), and farther-bound words transit nearer
  brokers untouched (HOP_CNT<31 there → the broker forwards on its bus face).

What this router PRODUCES (consumed by ``build.py``):
- A ``RouteResult`` per net whose ``points`` is the waypoint path FROM the source's
  exit cell, ALONG the shared bus, to the **broker cell** that taps into the target
  (a free cell abutting the target's input cell). The route ends AT the broker, not
  inside the target block — nothing transits the block's own cells (§1.2).
- The brokers themselves are DERIVED at build time from the routed project (the
  build-from-design invariant: the broker is the route's final free waypoint abutting
  a target). :func:`broker_plan` is the shared derivation both this router and
  ``build._apply_brokers`` use, so the source's WRITE-dest / JUMP-entry / hop and the
  broker's program agree exactly.

Sound failure (§P3.4): a net that genuinely can't tap the bus (unplaced block,
cross-chip, no free broker cell, over budget, or a DRC violation) yields a
``RouteResult(ok=False, reason=...)`` — NAMED, never fabricated.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from model.connection import BlockEndpoint, ChipPortEndpoint
from model.enums import Face

from .autoroute import (AutoRouteReport, AutoRouter, RouteResult, _FACE_STEP,
                        _MAX_HOPS)

# Unit step per fwd_face code (S=0,E=1,W=2,N=3) — screen coords (x-right/y-down).
_FWD_DELTA = {0: (0, 1), 1: (1, 0), 2: (-1, 0), 3: (0, -1)}
_NEI = ((1, 0), (-1, 0), (0, 1), (0, -1))
# model.enums.Face (string-valued) → fwd_face int code.
_FACE_CODE = {"south": 0, "east": 1, "west": 2, "north": 3}


def _face_code(face):
    """Normalize a face (model.enums.Face / cell_map.Face IntEnum / int) → int code
    (S=0,E=1,W=2,N=3), or None."""
    if face is None:
        return None
    val = getattr(face, "value", face)
    if isinstance(val, str):
        return _FACE_CODE.get(val)
    try:
        return int(val) & 0x3
    except (TypeError, ValueError):
        return None

# The broker convention (shared by the router and the build hook, so the source's
# WRITE/JUMP and the broker's relay program agree):
#   * the burst value the source WRITEs lands in the broker's R0 (the SplitterBlock
#     ``WRITE @N, 0`` convention — the relay then re-emits R0 into the block);
#   * the broker's deliver entry is resolved from its assembled program (the build
#     hook resolves the same template → the same entry address).
BROKER_BURST_REG = 0


@dataclass
class BrokerDelivery:
    """One delivery a broker performs: relay WRITE @1, ``in_reg`` + JUMP @1,
    ``in_entry`` into ``in_cell`` after flipping to ``deliver_face``. ``conn`` names
    the connection whose source addresses this delivery (so the build points that
    source at the right broker entry). ``src_cell`` is the source's exit cell —
    used to COALESCE deliveries that share one source AND one target cell into a
    single multi-operand complex-sample delivery (the input-port complex-sample
    contract: N WRITEs then ONE trigger), instead of N independent WRITE+JUMP
    deliveries that would fire the target N times with stale operands."""

    conn: str
    in_cell: tuple
    in_reg: int
    in_entry: int
    deliver_face: int
    src_cell: tuple = None


@dataclass
class BrokerTap:
    """One block-attach point on the bus: a broker cell delivering into a block.

    ``cell`` is the broker's (x, y) on the bus. ``deliveries`` is the list of
    per-net deliveries this broker performs — usually one, but a FAN-IN (two streams
    into one input cell, e.g. the Costas phase cell's xi + xq) gives the broker TWO
    deliveries, one entry each (§1.2). ``bus_face`` is the through-bus direction it
    restores to (a transiting HOP<31 word continues that way).
    """

    cell: tuple
    deliveries: list
    bus_face: int


def route_all_bus(project, chip_types, port_cell_provider,
                  spine_provider=None, port_map_provider=None) -> AutoRouteReport:
    """Route every UNROUTED net on one chip over a shared bus with broker taps.

    ``port_cell_provider(block_type, library) -> {port: (cell_id, direction)}`` and
    ``port_map_provider`` are the same callbacks :class:`AutoRouter` takes (reused
    for endpoint geometry). ``spine_provider(chip) -> [(x, y), ...]`` (optional)
    supplies the placement spine (the serpentine snake) as the preferred bus
    backbone; without it the router threads the bus itself.

    Returns an :class:`AutoRouteReport`. Brokers are NOT returned here — they are
    derived from the resulting routes by :func:`broker_plan` (the build reads the
    same routed project), so the router's only output is the waypoint paths.
    """
    helper = AutoRouter(project, chip_types, port_cell_provider, port_map_provider)
    results: list[RouteResult] = []

    # Group unrouted nets by chip; resolve endpoints up front (sound failures named).
    by_chip: dict[int, list] = {}
    for conn in project.connections:
        if conn.is_routed:
            continue
        src = helper._endpoint_cell(conn.source, role="src")
        dst = helper._endpoint_cell(conn.target, role="dst")
        if src is None:
            results.append(RouteResult(conn.name, False,
                                       reason="source block unplaced or port unknown"))
            continue
        if dst is None:
            results.append(RouteResult(conn.name, False,
                                       reason="target block unplaced or port unknown"))
            continue
        (schip, sx, sy, sface), (dchip, dx, dy, dface) = src, dst
        if schip != dchip:
            results.append(RouteResult(conn.name, False,
                                       reason="cross-chip auto-route not supported yet"))
            continue
        src_is_port = isinstance(conn.source, ChipPortEndpoint)
        dst_is_port = isinstance(conn.target, ChipPortEndpoint)
        by_chip.setdefault(schip, []).append(
            (conn.name, (sx, sy), sface, (dx, dy), dface,
             src_is_port, dst_is_port, conn))

    for chip_id, nets in by_chip.items():
        ct = helper._chip_type(chip_id)
        if ct is None:
            for n in nets:
                results.append(RouteResult(n[0], False, reason="no chip type"))
            continue
        spine = list(spine_provider(chip_id)) if spine_provider else []
        # Route ORDER matters under single-fwd_face contention (the bus is a directed
        # backbone; later nets should COALESCE onto it, not fight it). No single order
        # is optimal for every layout (the design's CP-SAT does a joint solve), so we
        # try a few principled orderings and KEEP the one that routes the most nets —
        # a robust, sound heuristic (every kept route is a real path; failures named).
        # Common to all: group by TARGET cell so a fan-in's nets are consecutive (the
        # first creates the broker, the rest REUSE it; block→block before port→block).
        # ``mode`` picks the class precedence:
        #   "egress"  — output-egress nets first (claim the long through-corridor),
        #               then input nets, then block→block. Best for a pipeline whose
        #               output must cross a bottleneck (the coherent chain's net6).
        #   "blocks"  — block→block first (establish the bus + brokers), then port
        #               nets tap/exit respecting them. Best when egress would
        #               otherwise grab a broker cell the wrong way (dense fan-outs).
        def _cls(src_is_port, dst_is_port, mode):
            if mode == "egress":
                return 0 if dst_is_port else (1 if src_is_port else 2)
            return 1 if (src_is_port or dst_is_port) else 0

        # Single-cell bus-fed blocks (the §5.3 deadlock hazard the user flagged): a
        # block with ONE cell that both RECEIVES its input (a broker WRITE+JUMP) and
        # DRIVES its output (WRITE+JUMP) on its ONE cell. If the input arrives on the
        # SAME face the output drives, both contend on ONE single-outstanding link →
        # deadlock. ``_route_chip_bus`` makes each such block SAFE adaptively: whichever
        # of its two nets routes FIRST commits its face; the SECOND is steered OFF it
        # (the input broker avoids a committed output face; the output's first hop
        # avoids a committed input arrival face). So the NET ORDER decides which net
        # leads — and no single order suits every layout (a corner block needs its
        # INPUT first so its OUTPUT can detour; a mid-bus block is fine with the natural
        # egress-first order). We therefore try a "hazard" ordering (hazard INPUT nets
        # first, hazard OUTPUT nets last) IN ADDITION to the egress/blocks orderings and
        # keep the first that routes every net. The DRC re-verifies input != output
        # face. ``sc_cells`` = the single-cell bus-fed target cells on this chip.
        sc_cells = _single_cell_bus_fed_targets(project, chip_id, nets)

        def _haz_rank(n):
            """0 = a hazard cell's INPUT net (lead), 2 = its OUTPUT net (trail),
            1 = every other net — used only by the "hazard" ordering mode."""
            _name, s, _sf, d, _df, _sp, _dp, _conn = n
            if d in sc_cells and isinstance(_conn.target, BlockEndpoint):
                return 0
            if s in sc_cells and isinstance(_conn.source, BlockEndpoint):
                return 2
            return 1

        def _key(n, mode):
            _name, s, _sf, d, _df, src_is_port, dst_is_port, _conn = n
            span = -(abs(s[0] - d[0]) + abs(s[1] - d[1]))
            if mode == "hazard":
                # Hazard split DOMINATES the class order so a hazard cell's input net
                # is committed before its output net regardless of egress/block class.
                return (_haz_rank(n), _cls(src_is_port, dst_is_port, "egress"), d,
                        0 if not src_is_port else 1, span)
            return (_cls(src_is_port, dst_is_port, mode), d,
                    0 if not src_is_port else 1, span)

        modes = ("egress", "blocks")
        if sc_cells:
            modes = modes + ("hazard",)
        best = None
        for mode in modes:
            ordered = sorted(nets, key=lambda n: _key(n, mode))
            res = _route_chip_bus(project, ct, chip_id, ordered, spine,
                                  sc_cells=sc_cells)
            nok = sum(1 for r in res if r.ok)
            if best is None or nok > best[0]:
                best = (nok, res)
            if nok == len(nets):
                break
        # FALLBACK (§P3.4 sound failure, not a dead build): if NO safe ordering routes
        # every net — a single-cell hazard block in a geometry too tight to split its
        # input/output faces (e.g. a walled corner sink) — re-route with the hazard
        # guard DISABLED so the nets still route (best-effort, as the pre-safety router
        # did). The build's bus DRC then ERRORS on the residual input-face==output-face
        # cell (NAMED), blocking the unsafe build rather than failing to route at all.
        # Only used when the safe attempt strictly improves on nothing — a safe route is
        # always preferred when one exists.
        if sc_cells and best[0] < len(nets):
            for mode in ("egress", "blocks"):
                ordered = sorted(nets, key=lambda n: _key(n, mode))
                res = _route_chip_bus(project, ct, chip_id, ordered, spine,
                                      sc_cells=None)
                nok = sum(1 for r in res if r.ok)
                if nok > best[0]:
                    best = (nok, res)
                if nok == len(nets):
                    break
        chip_results = _drc_gate(best[1], chip_types)
        results.extend(chip_results)

    # Preserve the project's connection order in the report.
    order = {c.name: i for i, c in enumerate(project.connections)}
    results.sort(key=lambda r: order.get(r.name, 1 << 30))
    return AutoRouteReport(results)


def _drc_gate(results, chip_types):
    """Run the bus DRC (:mod:`engine.bus_drc`) over the SUCCESSFULLY-routed nets and
    DEMOTE any net implicated in a face-conflict / deadlock to a NAMED failure (P3.4:
    a violation is a sound, explained failure, never a silent dead build). Returns the
    results with offenders re-marked ``ok=False`` carrying the DRC reason."""
    from .bus_drc import check_bus

    routed = {r.name: r.points for r in results if r.ok and r.points}
    if not routed:
        return results
    viols = check_bus(None, routed, chip_types)
    if not viols:
        return results
    reason_for: dict = {}
    for v in viols:
        for n in v.nets:
            reason_for.setdefault(n, str(v))
    out = []
    for r in results:
        if r.ok and r.name in reason_for:
            out.append(RouteResult(r.name, False, reason=reason_for[r.name]))
        else:
            out.append(r)
    return out


def _route_chip_bus(project, ct, chip_id, nets, spine, *, sc_cells=None):
    """Construct the bus on one chip and route each net source→bus→broker.

    Strategy (constructive, matching §7.3's backbone-first heuristic):
      1. Obstacles = block cells + transit cells (a word never transits a live block
         cell, §1.2) + already-routed connection cells.
      2. The shared bus is a growing set of free cells. For each net (flow order):
         a. find the BROKER cell — a free cell abutting the target's input cell (or,
            for an output-port target, the port edge cell itself);
         b. BFS a path from the source's exit cell to that broker over free cells,
            PREFERRING cells already on the bus (so nets coalesce onto one spine) and
            the placement spine, then add the path to the bus.
      3. Two nets sharing a bus cell is sound here (unlike the plain-transit CP-SAT
         router): each peels off at its OWN broker by JUMP entry, and a farther word
         transits a nearer broker because its HOP_CNT<31 there.
    """
    W, H = ct.width, ct.height

    def in_bounds(c):
        return 0 <= c[0] < W and 0 <= c[1] < H

    # Block + transit + routed obstacles. Endpoint cells (a net's own source/target
    # cell) are always usable even if they are block cells.
    occ = set()
    for blk in project.blocks:
        pl = blk.placement
        if pl is None or pl.chip != chip_id:
            continue
        occ.update((c.x, c.y) for c in pl.cells)
        occ.update((t.x, t.y) for t in getattr(pl, "transit_cells", []))
    for conn in project.connections:
        if conn.is_routed and _conn_chip(project, conn) == chip_id:
            occ.update((p.x, p.y) for p in conn.route)

    spine_set = {tuple(p) for p in spine if in_bounds(tuple(p))}
    bus: set = set()                  # cells already carrying the bus (preferred)
    # The committed OUTGOING direction of each bus cell. A cell has ONE fwd_face
    # (§1.3), so a net may only RE-USE a bus cell if it leaves it in the SAME
    # direction; otherwise that cell is an obstacle for this net (it must route
    # disjointly). This is what keeps a shared bus segment SOUND — both streams
    # leave a shared cell the same way; they peel off at their own brokers.
    bus_dir: dict = {}
    brokers: set = set()              # cells programmed as a broker (their fwd is bus)
    out: list[RouteResult] = []

    # Single-cell bus-fed hazard cells (§5.3): one cell both RECEIVES (broker) and
    # DRIVES (output). To guarantee input-face != output-face WITHOUT forcing a net
    # order (which can wall a corner block), we let the natural route order decide which
    # of the two nets routes FIRST, commit its face, then steer the SECOND off it:
    #   * if the OUTPUT net routes first  -> record ``hazard_out_face[cell]``; the input
    #     broker then avoids that face (it taps a DIFFERENT neighbour),
    #   * if the INPUT  net routes first  -> record ``hazard_in_face[cell]`` (the broker
    #     arrival face); the output's first hop then avoids it.
    # Whichever is second adapts; both directions are tried, so a layout that admits a
    # safe split is found, and one that doesn't yields a NAMED failure (the DRC also
    # re-verifies the built faces). ``sc_cells`` = the hazard cells.
    sc_cells = set(sc_cells or ())
    hazard_in_face: dict = {}         # hazard (x, y) -> committed input ARRIVAL face
    hazard_out_face: dict = {}        # hazard (x, y) -> committed OUTPUT face

    # The bus is grown LAZILY: each net's path commits its cells' outgoing directions
    # (``bus_dir``), and a later net may RE-USE a committed cell only by leaving it the
    # SAME way (sound sharing, §1.3) — else that cell is an obstacle and the net routes
    # disjointly there. The placement spine merely BIASES the per-net BFS (a cost
    # preference, ``spine_set``) toward the snake, so nets coalesce onto it without a
    # rigid pre-committed backbone that would wall off transverse port nets.

    for (name, s, sface, d, dface, src_is_port, dst_is_port, conn) in nets:
        if not (in_bounds(s) and in_bounds(d)):
            out.append(RouteResult(
                name, False, reason="endpoint cell is off the array grid"))
            continue

        # Where the route ends + whether it terminates in a broker:
        #   * chip-OUTPUT-port target → the egress cell IS the port edge cell (no
        #     broker; the source WRITE/JUMP exits via the port face), route ends AT
        #     the port cell, like the corridor router.
        #   * chip-INPUT-port SOURCE → the port injects the burst directly (it is the
        #     bus origin, a unique stream), so route STRAIGHT to the target's input
        #     cell, no broker — exactly how every existing port→block build works.
        #   * block→block → a programmed BROKER taps off the bus into the target
        #     (the §1.2 case that lets different-sink streams share the spine).
        forbid_broker = None
        if dst_is_port:
            goal = d
            goal_is_block = False
            goal_is_broker = False
        elif src_is_port and s == d:
            # The chip input port injects DIRECTLY into the block (the landing cell IS
            # the port cell) — no route/broker needed, as every existing port→block
            # build does. (A port whose target is a DIFFERENT cell taps via a broker,
            # below, like any other stream into that cell.)
            goal = d
            goal_is_block = False
            goal_is_broker = False
        else:
            # block→block OR port→(remote block cell) → tap the bus through a BROKER
            # abutting the target's input cell (§1.2). A FAN-IN (a second net into the
            # SAME input cell, e.g. the Costas phase cell's xi + xq) REUSES the broker
            # already serving that cell: the broker grows one deliver entry per net
            # (§1.2: two streams to one cell ⇒ two entries). ``broker_plan`` groups by
            # broker cell, so router and build agree.
            # INPUT net into a single-cell hazard cell whose OUTPUT face is ALREADY
            # committed: forbid the broker from sitting on that output face, so the
            # input feed and the output drive use DIFFERENT links (§5.3).
            if d in sc_cells and isinstance(conn.target, BlockEndpoint):
                forbid_broker = hazard_out_face.get(d)
            reuse = _broker_abutting(d, dface, brokers, s, forbid_broker)
            goal = reuse if reuse is not None else \
                _free_neighbor(d, dface, occ, bus, spine_set, in_bounds, s,
                               forbid_broker)
            goal_is_block = True
            goal_is_broker = True
            if goal is None:
                out.append(RouteResult(
                    name, False,
                    reason="no free broker cell abutting the target input"))
                continue

        # OUTPUT net of a single-cell hazard cell: forbid its first hop from leaving on
        # the face the INPUT arrives on (recorded when the input net routed first), so
        # input-face != output-face. ``forbid_first`` is that face code, or None.
        forbid_first = None
        if s in sc_cells and isinstance(conn.source, BlockEndpoint):
            forbid_first = hazard_in_face.get(s)

        path = _bus_bfs(s, sface, goal, occ, bus, spine_set, in_bounds,
                        src_is_port, bus_dir=bus_dir, brokers=brokers,
                        forbid_first=forbid_first)
        if path is None and goal_is_broker:
            # The chosen broker is walled (its only approaches are committed the wrong
            # way). Try the OTHER free neighbours of the target as broker taps before
            # giving up — a packed fan-in may need a different abutment face.
            for alt in _free_neighbors_all(d, dface, occ, in_bounds, s,
                                           forbid_broker):
                if alt == goal:
                    continue
                path = _bus_bfs(s, sface, alt, occ, bus, spine_set, in_bounds,
                                src_is_port, bus_dir=bus_dir, brokers=brokers,
                                forbid_first=forbid_first)
                if path is not None:
                    goal = alt
                    break
        if path is None:
            out.append(RouteResult(
                name, False, reason="no bus path from source to the broker tap"))
            continue

        # Hop budget: source→broker distance (+1 to deliver into the block at the
        # broker, since the broker re-emits @1; +1 for an output-port egress).
        distance = max(0, len(path) - 1)
        if goal_is_block:
            distance += 1          # broker relays one more hop into the block
        elif dst_is_port and conn.target.port.endswith("_out"):
            distance += 1          # word must transit the edge cell to exit
        # >31-hop route (§1.4): instead of failing, insert RELAY cells along the
        # path so each segment is ≤31 hops. A relay is a routing cell where the word
        # lands at HOP==31 and the universal ``relay`` entry re-launches it with a
        # fresh budget onward. We place a relay every (_MAX_HOPS - 1) waypoints so
        # the source→relay, relay→relay, and relay→broker segments each fit. The
        # final +1 (broker deliver or port egress) is absorbed in the last segment.
        relays: list[tuple] = []
        if distance > _MAX_HOPS:
            seg = _MAX_HOPS - 1            # leave headroom for the deliver/egress +1
            # path index of each relay: every `seg` hops from the source exit, but
            # never the source (idx 0) or the final broker/target (last idx).
            idx = seg
            while idx < len(path) - 1:
                relays.append(path[idx])
                idx += seg
            if not relays:                 # pathological: couldn't place one — fail
                out.append(RouteResult(
                    name, False,
                    reason=f"bus route is {distance} hops (max {_MAX_HOPS}) and no "
                           "relay cell could be placed on the path"))
                continue

        # Commit: this net's cells join the shared bus so later nets coalesce, and
        # record each cell's committed outgoing direction (so a later net may share a
        # cell only if it leaves the same way — single fwd_face soundness). The final
        # cell is this net's BROKER (for block targets): later nets transiting it must
        # leave on ITS bus direction (recorded when this broker forwards onward) — but
        # since the broker is THIS net's endpoint, its own outgoing dir is the bus
        # direction the NEXT spine cell would take; we leave it unconstrained here and
        # let a transiting net set it (the broker's restore face matches that).
        for i in range(len(path) - 1):
            c = path[i]
            dcode = _step_face(path[i], path[i + 1])
            bus.add(c)
            if dcode is not None and c not in bus_dir:
                bus_dir[c] = dcode
        bus.add(path[-1])
        if goal_is_broker:
            brokers.add(path[-1])
            # The broker forwards transiting (HOP<31) words on its BUS face = the
            # direction of travel INTO it. A later net transiting this broker must
            # continue that way (matches the broker's restore face). Record it so the
            # directional-share check enforces it.
            if len(path) >= 2:
                bd = _step_face(path[-2], path[-1])
                if bd is not None:
                    bus_dir[path[-1]] = bd
        if relays:
            # Relay PLACEMENT is computed (§1.4), but the BUILD does not yet program
            # the relay re-launch (storing relays on the connection + patching each
            # relay's onward hop is the remaining build-side piece). Rather than emit
            # a route the build would MIS-program (a silent wrong build — forbidden),
            # fail this net loudly and NAME it, carrying the computed relay cells so a
            # future build pass can consume them. Sound failure, not a dead build.
            out.append(RouteResult(
                name, False, points=path, relays=relays,
                reason=f"bus route is {distance} hops (>{_MAX_HOPS}); "
                       f"{len(relays)} relay cell(s) placed at {relays}, but relay "
                       "programming is not yet emitted by the build"))
            continue
        # Record this hazard cell's committed face so the OTHER net (routed later) is
        # steered off it. INPUT net -> the input ARRIVAL face (cell -> broker dir);
        # OUTPUT net -> the OUTPUT face (cell -> first waypoint dir).
        if d in sc_cells and goal_is_broker \
                and isinstance(conn.target, BlockEndpoint):
            arr = _step_face(d, path[-1])
            if arr is not None:
                hazard_in_face.setdefault(d, arr)
        if s in sc_cells and isinstance(conn.source, BlockEndpoint) and len(path) >= 2:
            of = _step_face(path[0], path[1])
            if of is not None:
                hazard_out_face.setdefault(s, of)

        out.append(RouteResult(name, True, points=path))

    return out


def _broker_forbidden(in_face, forbid_out):
    """The set of (dx, dy) neighbour deltas a broker may NOT occupy: the target's own
    emit (``in_face``) face (§7.4) PLUS — for a single-cell hazard cell whose OUTPUT
    face is already committed — that OUTPUT face (``forbid_out``, a face code), so the
    input broker never shares the single-outstanding link the output drives (§5.3)."""
    forbid = set()
    code = _face_code(in_face)
    if code is not None and code in _FWD_DELTA:
        forbid.add(_FWD_DELTA[code])
    if forbid_out is not None and int(forbid_out) in _FWD_DELTA:
        forbid.add(_FWD_DELTA[int(forbid_out)])
    return forbid


def _free_neighbor(cell, in_face, occ, bus, spine_set, in_bounds, src,
                   forbid_out=None):
    """A free cell abutting ``cell`` (the target input) to host the broker.

    A delivery may arrive on any face EXCEPT the target's own emit (``in_face``) face
    (§7.4) and — for a single-cell hazard cell — its committed OUTPUT face
    (``forbid_out``). Prefers a cell already on the bus / spine (coalesce), then any
    free neighbour; never the source cell itself. When ``forbid_out`` is set (the
    hazard case) it instead prefers a QUIET free neighbour OFF the bus/spine and the
    calmest corner, so the input feed never competes with through-traffic on the
    hazard cell's single link.
    """
    forbid = _broker_forbidden(in_face, forbid_out)
    cands = []
    for code, (dx, dy) in _FWD_DELTA.items():
        if (dx, dy) in forbid:
            continue
        n = (cell[0] + dx, cell[1] + dy)
        if not in_bounds(n) or n in occ or n == src or n == cell:
            continue
        if forbid_out is not None:
            base = 2 if n in bus else (1 if n in spine_set else 0)
            adj = sum(1 for ddx, ddy in _NEI
                      if (n[0] + ddx, n[1] + ddy) in bus
                      or (n[0] + ddx, n[1] + ddy) in spine_set)
            rank = (base, adj)
        else:
            rank = (0 if n in bus else (1 if n in spine_set else 2), 0)
        cands.append((rank, n))
    if not cands:
        return None
    cands.sort()
    return cands[0][1]


def _free_neighbors_all(cell, in_face, occ, in_bounds, src, forbid_out=None):
    """All free neighbours of ``cell`` that may host a broker (any face except the
    target's own emit face, and a single-cell hazard cell's committed output face), in
    no particular order — the fallback set when the preferred broker tap is walled."""
    forbid = _broker_forbidden(in_face, forbid_out)
    res = []
    for c, (dx, dy) in _FWD_DELTA.items():
        if (dx, dy) in forbid:
            continue
        n = (cell[0] + dx, cell[1] + dy)
        if in_bounds(n) and n not in occ and n != src and n != cell:
            res.append(n)
    return res


def _broker_abutting(cell, in_face, brokers, src, forbid_out=None):
    """An EXISTING broker cell abutting the target input ``cell`` (a FAN-IN reuse:
    a second net into the same input cell rides the broker already there, which then
    grows a deliver entry per net). Returns that broker cell or None. Excludes the
    target's own emit face, a single-cell hazard cell's committed output face, and the
    source cell."""
    forbid = _broker_forbidden(in_face, forbid_out)
    for c, (dx, dy) in _FWD_DELTA.items():
        if (dx, dy) in forbid:
            continue
        n = (cell[0] + dx, cell[1] + dy)
        if n in brokers and n != src and n != cell:
            return n
    return None


def _bus_bfs(src, sface, goal, occ, bus, spine_set, in_bounds, src_is_port,
             *, bus_dir=None, brokers=None, forbid_first=None):
    """Shortest free-cell path src→goal, PREFERRING bus then spine cells, and only
    SHARING a bus cell when leaving it in its already-committed direction.

    ``forbid_first`` (a face code, or None) forbids the FIRST hop from leaving ``src``
    on that face — used for a single-cell hazard block's OUTPUT net so it never drives
    the same link the input arrives on (the §5.3 deadlock guard; input != output face).

    A block source emits on ``sface`` so the first step leaves on that face; a chip
    input port injects AT its own cell so BFS starts there. Cells already on the bus
    or spine are preferred (Dijkstra with cost 0 for bus, 1 for spine, higher for
    free) so nets coalesce onto a single shared backbone — what makes the densely-
    packed chain routable where disjoint corridors fail.

    SOUNDNESS (the single-fwd_face rule, §1.3): a bus cell already carrying traffic
    has ONE committed outgoing direction (``bus_dir[c]``). This net may LEAVE that
    cell only in that same direction (so both streams exit the shared cell the same
    way and demux at their own brokers); any other exit from a committed cell is
    forbidden, forcing this net onto a disjoint cell there. A foreign broker may be
    transited only by continuing its bus direction (it forwards HOP<31 words that
    way). Without this the shared segment would build but mis-compute (a turn at a
    shared cell mis-faces the other stream — the net-conflict the DRC also names).
    """
    import heapq

    bus_dir = bus_dir or {}
    brokers = brokers or set()

    if src == goal:
        return [src]

    def free(c):
        return in_bounds(c) and (c == goal or c == src or c not in occ)

    def can_leave(c, nxt):
        """May this net leave committed bus cell ``c`` toward ``nxt``? Only if ``c``
        has no committed direction yet, or its committed direction == c→nxt."""
        dc = bus_dir.get(c)
        if dc is None:
            return True
        return _step_face(c, nxt) == dc

    if src_is_port:
        starts = [src]
    else:
        # A block output emits on ``sface``, so PREFER leaving on that face; but a
        # mid-block / densely-packed output (e.g. the Costas rotate, whose emit-face
        # neighbour is another of its own cells) may have that neighbour blocked. In
        # that case the bus picks the burst up at ANY free neighbour — the build then
        # faces the exit cell toward whichever first waypoint we chose. Without this,
        # a packed block's output can never reach the bus (the net4/5/6 failure).
        # The forbidden first-step neighbour (single-cell hazard output guard): the cell
        # the OUTPUT may NOT leave toward (it is where the INPUT arrives from).
        forbid_cell = None
        if forbid_first is not None and int(forbid_first) in _FWD_DELTA:
            fdx, fdy = _FWD_DELTA[int(forbid_first)]
            forbid_cell = (src[0] + fdx, src[1] + fdy)
        step = _FACE_STEP.get(sface)
        emit = (src[0] + step[0], src[1] + step[1]) if step else None
        starts = []
        if emit is not None and emit != forbid_cell \
                and (free(emit) or emit == goal):
            starts.append(emit)
        for dx, dy in _NEI:
            n = (src[0] + dx, src[1] + dy)
            if n == forbid_cell:
                continue                  # never leave on the input's arrival face
            if n not in starts and (free(n) or n == goal):
                starts.append(n)
        if not starts:
            return None

    def cost(c):
        if c == goal:
            return 0
        return 0 if c in bus else (1 if c in spine_set else 4)

    # Dijkstra from ALL candidate starts; reconstruct start..goal then prepend src.
    pq = []
    dist = {}
    prev = {}
    for start in starts:
        if cost(start) < dist.get(start, 1 << 30):
            dist[start] = cost(start)
            prev[start] = None
            pq.append((cost(start), start))
    import heapq as _hq
    _hq.heapify(pq)
    while pq:
        dcur, cur = heapq.heappop(pq)
        if cur == goal:
            break
        if dcur > dist.get(cur, 1 << 30):
            continue
        for dx, dy in _NEI:
            nxt = (cur[0] + dx, cur[1] + dy)
            if nxt == src or not free(nxt):
                continue
            if not can_leave(cur, nxt):
                continue                  # would mis-face a shared bus cell (§1.3)
            # A foreign broker (not this net's own goal) may be TRANSITED but not
            # landed on; transiting it is already constrained by its bus_dir above.
            if nxt in brokers and nxt != goal:
                # allow only if we then continue on its bus direction (handled by
                # can_leave(nxt, ...) on the next expansion); permit entry here.
                pass
            nd = dcur + cost(nxt) + 1     # +1 per hop to bound length
            if nd < dist.get(nxt, 1 << 30):
                dist[nxt] = nd
                prev[nxt] = cur
                heapq.heappush(pq, (nd, nxt))
    if goal not in prev:
        return None
    chain = []
    node = goal
    while node is not None:
        chain.append(node)
        node = prev[node]
    chain.reverse()
    return [src] + chain if chain[0] != src else chain


def _single_cell_bus_fed_targets(project, chip_id, nets) -> set:
    """The (x, y) cells of SINGLE-CELL blocks targeted by a BUS-FED input net.

    A bus-fed single-cell block has exactly one cell that receives its input through a
    BROKER (block→block, or a chip input port whose target cell is NOT the port cell
    itself), rather than a direct chip-input-port injection at its own cell. That one
    cell both RECEIVES (broker WRITE+JUMP) and DRIVES (WRITE+JUMP) its output; if the
    input arrives on the SAME face the output drives, both contend on one single-
    outstanding link → deadlock (§5.3). These cells get the input-face != output-face
    guarantee and are re-verified by the DRC.

    A single-cell block fed DIRECTLY by a chip input port (the port injects at its own
    cell — the lead-block contract seats it on the port) is NOT included: there is no
    broker, so no shared-face hazard. ``nets`` is the resolved per-chip net list
    ``(name, s, sface, d, dface, src_is_port, dst_is_port, conn)``."""
    single: set = set()
    for blk in project.blocks:
        pl = blk.placement
        if pl is None or pl.chip != chip_id or len(pl.cells) != 1:
            continue
        single.add((pl.cells[0].x, pl.cells[0].y))
    if not single:
        return set()
    bus_fed: set = set()
    for (_name, s, _sf, d, _df, src_is_port, _dst_is_port, _conn) in nets:
        if d not in single:
            continue
        if src_is_port and s == d:        # direct port→own-cell injection (no broker)
            continue
        bus_fed.add(d)
    return bus_fed


def _conn_chip(project, conn):
    for ep in (conn.source, conn.target):
        if isinstance(ep, BlockEndpoint):
            blk = project.block(ep.block)
            if blk is not None and blk.placement is not None:
                return blk.placement.chip
        if isinstance(ep, ChipPortEndpoint):
            return ep.chip
    return None


# --------------------------------------------------------------------------- #
# Broker derivation — shared by the build hook (build-from-design invariant).
# --------------------------------------------------------------------------- #

def broker_plan(project, chip_id, chip_type, catalog):
    """Derive the BROKER taps for one chip from the ROUTED project (no side channel).

    A broker is the final free waypoint of a routed block→block connection that abuts
    the target block's input cell — i.e. a routing cell that is NOT inside any block.
    For each such connection this returns a :class:`BrokerTap` describing the cell to
    program: flip toward the target input, relay WRITE @1 + JUMP @1 into it, restore
    to the bus (forward) face.

    Returns ``{(x, y): BrokerTap}``. The build's ``_apply_brokers`` programs each;
    the build's source-exit patch addresses the broker (WRITE dest 0 == burst reg,
    JUMP entry == broker deliver entry) at hop = route distance.

    This is the SAME geometry the router used (a route ending at a free neighbour of
    the target), so router and build agree without passing state — the route in the
    project IS the contract (build-from-design).
    """
    block_cells: dict[tuple, str] = {}
    # A block's feedback TRANSIT cells carry an internal feedback word on their
    # AUTHORED face. When a broker lands on one of these (the only free tap for a
    # block whose output cell shares its emit face with its feedback, e.g. the
    # Gardner loop_filter: `out` + `period_fb` both leave on one face into the
    # feedback transit lane), the broker must RESTORE to that authored face so the
    # transiting feedback word (HOP<31) continues down the lane untouched — NOT to
    # the route's travel direction (which would divert the feedback into the
    # delivery target). Map each transit cell → its authored fwd_face code.
    transit_face: dict[tuple, int] = {}
    for blk in project.blocks:
        pl = blk.placement
        if pl is None or pl.chip != chip_id:
            continue
        for c in pl.cells:
            block_cells[(c.x, c.y)] = blk.name
        for t in getattr(pl, "transit_cells", []):
            fc = _face_code(getattr(t, "face", None))
            if fc is not None:
                transit_face[(t.x, t.y)] = fc

    # A block's resolved input geometry for a SPECIFIC port — entry addr + the
    # landing register for THAT port (so a fan-in's two streams land in their own
    # regs: e.g. the Costas phase cell's xi→R0 and xq→R1, not both into R0).
    def target_io(block, port):
        entry, in_regs = catalog.resolved_io(block.type, block.params,
                                             library=block.library)
        reg = in_regs[0] if in_regs else 0
        try:
            pmap = catalog.port_map(block.type, block.params, library=block.library)
            for p in pmap.ports:
                if p.name == port and p.direction == "in" and p.register is not None:
                    reg = p.register
                    break
        except Exception:  # noqa: BLE001
            pass
        return entry, reg

    taps: dict[tuple, BrokerTap] = {}
    for conn in project.connections:
        if not conn.is_routed:
            continue
        if _conn_chip(project, conn) != chip_id:
            continue
        if not isinstance(conn.target, BlockEndpoint):
            continue
        # PHYSICAL path: a route drawn ENDING ON the target input cell is stripped to
        # the abutting broker (the always-brokered block→block contract). The
        # auto-router's stop-one-short routes are unchanged.
        pts = _phys_pts(project, conn, catalog)
        if not pts:
            continue
        last = pts[-1]
        # The broker is the final (physical) waypoint — a free routing cell abutting
        # the target. After _phys_pts strips a trailing on-the-cell waypoint, the
        # broker is always a free cell; a route that still ends INSIDE another block
        # (overshoot through a different block) genuinely has no broker.
        if last in block_cells:
            continue
        tgt = project.block(conn.target.block)
        if tgt is None or tgt.placement is None or not tgt.placement.cells:
            continue
        # The target's input cell (where the broker delivers).
        in_cell = _target_input_cell(tgt, conn.target.port, catalog)
        if in_cell is None:
            continue
        # The broker must abut the input cell (the route ended adjacent to it).
        df = _step_face(last, in_cell)
        if df is None:
            continue
        entry, in_reg = target_io(tgt, conn.target.port)
        # The source's exit cell is the route's first waypoint when the source is a
        # placed block (the route starts AT the block's output cell). Used to detect
        # a COMPLEX-SAMPLE fan-in: two nets from the SAME source cell into the SAME
        # target cell must be relayed as one multi-WRITE + single-JUMP burst.
        src_cell = pts[0] if isinstance(conn.source, BlockEndpoint) else None
        delivery = BrokerDelivery(conn=conn.name, in_cell=in_cell, in_reg=in_reg,
                                  in_entry=entry, deliver_face=df, src_cell=src_cell)
        if last in taps:
            # FAN-IN: a second net taps the SAME broker cell (e.g. xq joining xi at
            # the Costas phase cell) — append a delivery (one more broker entry).
            taps[last].deliveries.append(delivery)
        else:
            # The bus (restore) face: normally the route's travel direction into the
            # broker. But a broker on a block's FEEDBACK transit cell must restore to
            # that cell's AUTHORED face so the transiting feedback word continues down
            # the feedback lane (not diverted to the delivery target).
            bus_face = transit_face.get(last, _bus_forward_face(pts))
            taps[last] = BrokerTap(cell=last, deliveries=[delivery],
                                   bus_face=bus_face)
    return taps


@dataclass
class CrossoverTrack:
    """One stream a CROSSOVER cell relays: ``conn`` lands here (HOP==31 via its own
    JUMP entry) and is re-emitted out ``exit_face`` to continue its route. ``head``
    is the number of hops from the net's SOURCE exit cell to this crossover cell (the
    source is re-pointed to land here at that hop). The crossover then re-emits the
    net's ORIGINAL downstream delivery (dest/entry) with the REMAINING hop budget —
    the build reads those from the source's already-patched exit WRITE/JUMP, so router
    and build agree without a side channel (the §1.4 universal routing-cell relay)."""

    conn: str
    exit_face: int
    head: int


@dataclass
class CrossoverTap:
    """One CROSSOVER cell: a plain routing cell two (or more) nets must leave in
    DIFFERENT directions (the single-``fwd_face`` conflict, §1.3). Instead of one
    static face (which silently corrupts one stream), the cell becomes a programmed
    DEMUX (the proven :class:`CrossoverBlock` primitive): each net lands via its own
    JUMP entry (the per-stream tag, §1.1), sets its own exit FACE, and re-emits
    onward (§1.4 #3 relay). ``tracks`` is one :class:`CrossoverTrack` per crossing
    net."""

    cell: tuple
    tracks: list


def _net_exit_face(conn, pts, i, project, chip_id, chip_type, catalog, block_cells):
    """The face net ``conn`` leaves its route cell ``pts[i]`` on — the FORWARDING
    direction a single ``fwd_face`` would have to serve there. For an INTERIOR cell
    that is toward the next waypoint. For the FINAL cell: a chip-OUTPUT-port target
    egresses on the port's face (a real face the build must serve); any other final
    cell (a block delivery / broker) is the net's TERMINUS — the broker's restore
    handles through-traffic, so it imposes NO forwarding face (returns ``None``)."""
    if i + 1 < len(pts):
        return _step_face(pts[i], pts[i + 1])
    # Final cell. Only a chip-output-port egress imposes a forwarding face here.
    if isinstance(conn.target, ChipPortEndpoint):
        for p in chip_type.ports:
            if p.name == conn.target.port:
                return _face_code(getattr(p, "face", None))
    return None


def crossover_plan(project, chip_id, chip_type, catalog):
    """Derive CROSSOVER cells from the ROUTED project (the §1.2 time-multiplexed bus,
    sibling of :func:`broker_plan`).

    A crossover is a PLAIN routing cell (not a broker, not inside a block) that two or
    more routed nets must leave in DIFFERENT directions — the single-``fwd_face``
    conflict that the static-face build silently corrupts (one net's word dies on the
    other's face). Each such cell is promoted to a programmed demux: every crossing
    net lands via its own JUMP entry and is re-emitted on its own face (§1.3/§1.4).

    A broker cell is EXCLUDED: it already serves two faces legitimately (deliver +
    restore) and forwards through-traffic on its restore face — no crossover needed.
    A net's OWN broker/delivery terminus imposes no forwarding face (see
    :func:`_net_exit_face`), so a deliver+transit overlap at a broker is NOT a
    conflict (the broker handles it) — only PLAIN cells with ≥2 distinct forwarding
    faces are crossovers.

    Returns ``{(x, y): CrossoverTap}``. The build (:func:`build._apply_crossovers`)
    programs each cell with the :class:`CrossoverBlock` template and re-points each
    crossing net's source to land at the crossover."""
    block_cells: set = set()
    for blk in project.blocks:
        pl = blk.placement
        if pl is None or pl.chip != chip_id:
            continue
        block_cells.update((c.x, c.y) for c in pl.cells)

    # Which routed connections live on this chip (with their waypoints).
    conns = []
    for conn in project.connections:
        if not conn.is_routed or _conn_chip(project, conn) != chip_id:
            continue
        # PHYSICAL path (same stripping as broker_plan): a block→block route drawn onto
        # the target input cell stops at the abutting broker, so crossover head hops
        # and forwarding faces agree with the broker source-exit hops.
        pts = _phys_pts(project, conn, catalog)
        if pts:
            conns.append((conn, pts))

    # The set of broker cells (a net's final free waypoint into a block) — excluded
    # from crossover promotion (brokers self-resolve via their restore face).
    brokers = set(broker_plan(project, chip_id, chip_type, catalog).keys())

    # Per-cell: {exit_face: [(conn, pts, i), ...]} across every net's FORWARDING use
    # of that cell (transit interior, or port-egress final cell).
    cell_uses: dict[tuple, dict] = {}
    for conn, pts in conns:
        for i, c in enumerate(pts):
            if c in block_cells or c in brokers:
                continue
            face = _net_exit_face(conn, pts, i, project, chip_id, chip_type,
                                  catalog, block_cells)
            if face is None:
                continue
            cell_uses.setdefault(c, {}).setdefault(face, []).append((conn, pts, i))

    taps: dict[tuple, CrossoverTap] = {}
    for cell, byface in cell_uses.items():
        if len(byface) < 2:
            continue  # one direction (or none) — a plain transit/turn, not a crossover
        tracks = []
        seen = set()
        for face, uses in byface.items():
            for (conn, pts, i) in uses:
                if conn.name in seen:
                    continue          # one track per net (a net uses the cell once)
                seen.add(conn.name)
                head = i               # hops from source exit cell to this cell
                tracks.append(CrossoverTrack(conn=conn.name, exit_face=face,
                                             head=head))
        taps[cell] = CrossoverTap(cell=cell, tracks=tracks)
    return taps


def _target_input_cell(block, port, catalog):
    """(x, y) of a block's input PORT cell (PortMap port → placed cell; falls back
    to the block's first/landing cell)."""
    try:
        pmap = catalog.port_map(block.type, block.params, library=block.library)
    except Exception:  # noqa: BLE001
        pmap = None
    cell_id = None
    if pmap is not None:
        for p in pmap.ports:
            if p.name == port and p.direction == "in":
                cell_id = p.cell_id
                break
    if cell_id is not None:
        pc = block.placement.cell(cell_id)
        if pc is not None:
            return (pc.x, pc.y)
    lc = block.placement.cells[0]
    return (lc.x, lc.y)


def _source_output_cell(block, port, catalog):
    """(x, y) of a block's OUTPUT port cell (PortMap out-port → placed cell; falls
    back to the block's last cell). The mirror of :func:`_target_input_cell` —
    used to detect a DIRECT ABUTMENT (the source's output cell sits adjacent to
    the target's input cell) when the user made the connection without drawing a
    route."""
    try:
        pmap = catalog.port_map(block.type, block.params, library=block.library)
    except Exception:  # noqa: BLE001
        pmap = None
    cell_id = None
    if pmap is not None:
        for p in pmap.ports:
            if p.name == port and p.direction == "out":
                cell_id = p.cell_id
                break
    if cell_id is not None:
        pc = block.placement.cell(cell_id)
        if pc is not None:
            return (pc.x, pc.y)
    lc = block.placement.cells[-1]
    return (lc.x, lc.y)


def _step_face(a, b):
    """fwd_face int (S=0,E=1,W=2,N=3) from adjacent ``a`` toward ``b``, or None."""
    ax, ay = a
    bx, by = b
    if bx == ax + 1 and by == ay:
        return 1
    if bx == ax - 1 and by == ay:
        return 2
    if by == ay + 1 and bx == ax:
        return 0
    if by == ay - 1 and bx == ax:
        return 3
    return None


def _bus_forward_face(pts):
    """The bus's forward face at the final (broker) cell — the direction of travel
    INTO it (so a transiting word continues that way). For a single-cell route
    defaults SOUTH (0)."""
    if len(pts) < 2:
        return 0
    return _step_face(pts[-2], pts[-1]) or 0


def abutment_pts(project, conn, catalog, ports):
    """The synthesised 2-cell physical path for a DIRECT-ABUTMENT connection that
    has NO drawn route (``conn.route == []``).

    The user connected a source block's output to an adjacent target (another
    block's input, or a chip output port) WITHOUT drawing waypoints — the blocks
    physically touch, so there are zero cells between them. Returns
    ``[source_output_cell, target_input_cell]`` when those cells are orthogonally
    adjacent (a valid @1 handoff), else ``None`` (not an abutment — stays
    unrouted). ``ports`` is ``{name: (cell_x, cell_y, face_code)}`` from the chip
    type, so a block→output-port abutment is supported too.

    This is what makes a packed, fully-abutted layout build + run without needing
    a filler routing cell between every pair of blocks."""
    if conn.route:                              # has a drawn route → not this path
        return None
    if not isinstance(conn.source, BlockEndpoint):
        return None
    src = project.block(conn.source.block)
    if src is None or src.placement is None or not src.placement.cells:
        return None
    out_cell = _source_output_cell(src, conn.source.port, catalog)
    in_cell = None
    if isinstance(conn.target, BlockEndpoint):
        tb = project.block(conn.target.block)
        if tb is not None and tb.placement is not None and tb.placement.cells:
            in_cell = _target_input_cell(tb, conn.target.port, catalog)
    elif isinstance(conn.target, ChipPortEndpoint):
        p = ports.get(conn.target.port)
        if p is not None:
            in_cell = (p[0], p[1])
    if in_cell is None or _step_face(out_cell, in_cell) is None:
        return None
    return [out_cell, in_cell]


def _phys_pts(project, conn, catalog):
    """The PHYSICAL waypoint path the build realises for ``conn`` (its broker/face/hop
    geometry), derived from the stored drawn ``conn.route`` WITHOUT mutating it.

    block→block delivery is ALWAYS brokered: the broker is the route's last FREE cell
    abutting the target's input cell; it relays the burst @1 into the input. The user
    draws the route ENDING AT the destination cell (the final hop tells the broker
    which face/cell to deliver to). So when the LAST drawn waypoint IS the target
    block's own input cell, the PHYSICAL route stops ONE waypoint short — at the
    abutting broker — and that trailing input-cell waypoint is stripped here. The
    auto-router's stop-one-short routes (last waypoint already the abutting broker)
    are returned unchanged, so both forms yield the SAME broker + source hop.

    A DIRECT ABUTMENT (the source block's own output cell sits adjacent to the target
    input cell — route ``[src_cell, in_cell]``) is NOT stripped: there is no FREE cell
    between them to host a broker, so the source delivers @1 straight into the input
    (the legacy abutment contract). Stripping is applied ONLY when the cell that would
    become the broker (the second-to-last waypoint) is a FREE routing cell.

    Returns ``[(x, y), ...]`` — the route from the source exit cell to the broker
    (block→block), or the unmodified path (chip-port / panel targets — never stripped,
    a port egress legitimately ends on its edge cell; direct abutment — keep the cell)."""
    pts = [(p.x, p.y) for p in conn.route]
    if len(pts) < 2 or not isinstance(conn.target, BlockEndpoint):
        return pts
    tgt = project.block(conn.target.block)
    if tgt is None or tgt.placement is None or not tgt.placement.cells:
        return pts
    in_cell = _target_input_cell(tgt, conn.target.port, catalog)
    if in_cell is None:
        return pts
    # The route ends ON the target's own input cell → the broker would be the cell
    # BEFORE it (must abut the input). Strip the trailing input-cell waypoint ONLY if
    # that prior cell is a FREE routing cell (can host a broker). If it sits inside any
    # placed block (the source's own output cell — direct abutment), DON'T strip: the
    # source delivers @1 directly into the input, the legacy adjacent-block contract.
    if pts[-1] == in_cell and _step_face(pts[-2], in_cell) is not None:
        chip_id = _conn_chip(project, conn)
        block_cells = set()
        for blk in project.blocks:
            pl = blk.placement
            if pl is None or (chip_id is not None and pl.chip != chip_id):
                continue
            block_cells.update((c.x, c.y) for c in pl.cells)
        if pts[-2] not in block_cells:
            return pts[:-1]
    return pts
