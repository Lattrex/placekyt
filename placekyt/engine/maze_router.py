"""Maze router — route ANY legal placement with per-net BFS + rip-up-reroute.

The bus/broker router (:mod:`engine.bus_router`) grows ONE shared directed backbone
and taps blocks off it. That model routes a linear serpentine beautifully but CANNOT
route a COMPACT 2-D placement (the CP-SAT packer's Weaver): a dense pack starves the
two things the bus router needs per net — (a) a FREE cell abutting the target input
to host the broker, and (b) a free BFS corridor from the source's exit cell to that
broker along the shared spine. It fails "no free broker cell abutting the target
input" / "no bus path from source to the broker tap" on the Weaver's fan-out nets.

This router routes a compact placement directly, over the free-cell grid:

  * Grid = the chip array. Block body cells + transit cells = OBSTACLES; every other
    cell is routable. A net's OWN source/target cells are always traversable.
  * Per net (routed shortest-Manhattan-span first, then rip-up on failure), A* over
    the free cells from the source's exit cell to a BROKER cell — a free cell abutting
    the target's input cell (the last free waypoint before the target). The route ends
    AT the broker exactly as the build expects (:func:`bus_router.broker_plan` derives
    the broker from the route's final free waypoint abutting the target).
  * NODE-DISJOINT by construction: a routed net's interior + broker cells become
    obstacles for later nets. This makes the single-``fwd_face`` rule (§1.3) trivially
    sound — no plain transit cell is ever shared, so the bus DRC's ``face_conflict``
    can never fire on a routing cell.
  * FAN-IN (two nets into ONE target input cell, e.g. IQUpconvert's xi+xq, or the
    ComplexToFloat re+im) SHARE one broker cell: the second net routes to the SAME
    broker (which :func:`bus_router.broker_plan` groups into one multi-entry tap).
  * RIP-UP-AND-REROUTE: if a net can't route, rip up the routed nets that block it
    (bounded passes, longest-first) and retry. A net that is still unroutable after
    rip-up yields a NAMED :class:`RouteResult(ok=False, ...)` — never a silent partial.

Output shape MATCHES the bus router exactly — an :class:`AutoRouteReport` of per-net
:class:`RouteResult` whose ``points`` are the waypoints the build consumes. Brokers /
crossovers / through-faces are DERIVED by the build from ``conn.route`` (the shared
:func:`bus_router.broker_plan` etc.), so ``build.py`` works unchanged.
"""

from __future__ import annotations

import heapq

from model.connection import BlockEndpoint, ChipPortEndpoint
from model.enums import Face

from .autoroute import AutoRouteReport, AutoRouter, RouteResult, _MAX_HOPS

_NEI = ((1, 0), (-1, 0), (0, 1), (0, -1))
_FACE_STEP = {
    Face.NORTH: (0, -1), Face.SOUTH: (0, 1),
    Face.EAST: (1, 0), Face.WEST: (-1, 0),
}


def _face_step(face):
    """Unit (dx, dy) a block emits toward on ``face`` (screen coords), or None."""
    if face is None:
        return None
    if isinstance(face, Face):
        return _FACE_STEP.get(face)
    # a raw fwd_face code (S=0,E=1,W=2,N=3)
    return {0: (0, 1), 1: (1, 0), 2: (-1, 0), 3: (0, -1)}.get(int(face))


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


def route_all_maze(project, chip_types, port_cell_provider,
                   port_map_provider=None) -> AutoRouteReport:
    """Route every UNROUTED net on each chip with the maze router.

    Signature MIRRORS :func:`bus_router.route_all_bus` so the controller can select
    it interchangeably. ``port_cell_provider`` / ``port_map_provider`` are the same
    callbacks :class:`AutoRouter` takes (reused here for endpoint geometry). Returns
    an :class:`AutoRouteReport`; a net that cannot route (even after rip-up) is a
    NAMED failure, never fabricated or silently dropped.
    """
    helper = AutoRouter(project, chip_types, port_cell_provider, port_map_provider)
    results: list[RouteResult] = []

    # Resolve every net's endpoint geometry up front; group by chip. Sound failures
    # (unplaced block, unknown port, cross-chip) are named immediately.
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
        by_chip.setdefault(schip, []).append(
            _Net(name=conn.name, src=(sx, sy), sface=sface, dst=(dx, dy),
                 dface=dface, src_is_port=isinstance(conn.source, ChipPortEndpoint),
                 dst_is_port=isinstance(conn.target, ChipPortEndpoint), conn=conn))

    for chip_id, nets in by_chip.items():
        ct = helper._chip_type(chip_id)
        if ct is None:
            for n in nets:
                results.append(RouteResult(n.name, False, reason="no chip type"))
            continue
        results.extend(_route_chip_maze(project, ct, chip_id, nets))

    # Preserve the project's connection order in the report.
    order = {c.name: i for i, c in enumerate(project.connections)}
    results.sort(key=lambda r: order.get(r.name, 1 << 30))
    return AutoRouteReport(results)


class _Net:
    """A resolved net to route (endpoint cells + roles + the source connection)."""

    __slots__ = ("name", "src", "sface", "dst", "dface",
                 "src_is_port", "dst_is_port", "conn")

    def __init__(self, name, src, sface, dst, dface, src_is_port, dst_is_port, conn):
        self.name = name
        self.src = src
        self.sface = sface
        self.dst = dst
        self.dface = dface
        self.src_is_port = src_is_port
        self.dst_is_port = dst_is_port
        self.conn = conn

    @property
    def span(self):
        return abs(self.src[0] - self.dst[0]) + abs(self.src[1] - self.dst[1])


def _route_chip_maze(project, ct, chip_id, nets):
    """Route every net on one chip node-disjoint, with fan-in broker sharing and
    bounded rip-up-reroute. Returns a list of :class:`RouteResult` (one per net)."""
    W, H = ct.width, ct.height

    def in_bounds(c):
        return 0 <= c[0] < W and 0 <= c[1] < H

    # Static obstacles: every block body cell + transit cell (a word never transits a
    # live block cell, §1.2). A net's OWN endpoint cells are exempted per-net below.
    block_cells: set = set()
    for blk in project.blocks:
        pl = blk.placement
        if pl is None or pl.chip != chip_id:
            continue
        block_cells.update((c.x, c.y) for c in pl.cells)
        block_cells.update((t.x, t.y) for t in getattr(pl, "transit_cells", []))

    def _adjacent(a, b):
        return abs(a[0] - b[0]) + abs(a[1] - b[1]) == 1

    def is_abutment(n):
        """A block→block net whose source OUTPUT cell directly abuts the target
        INPUT cell needs NO route: the build's ``abutment_pts`` synthesises the @1
        handoff (the source delivers straight into the input). Leaving it UNROUTED
        (empty route) is correct — and it frees the fabric for the nets that DO need
        a corridor. A chip-port source/target is never an abutment here (the port
        injects/egresses via its own face — handled by the routed branches)."""
        return (not n.src_is_port and not n.dst_is_port
                and isinstance(n.conn.source, BlockEndpoint)
                and isinstance(n.conn.target, BlockEndpoint)
                and _adjacent(n.src, n.dst))

    def _free_broker_count(target_in, dface):
        """How many free cells abut ``target_in`` (excluding its own emit face) — a
        MOST-CONSTRAINED-FIRST key: a walled fan-in target (1 candidate) must claim
        its only broker before a roomier sibling grabs the corridor to it."""
        return len(_broker_cells(target_in, dface, set(), in_bounds,
                                 None, block_cells))

    # Routing order: fan-in nets to the SAME target input cell must be CONSECUTIVE
    # (the first creates the broker, the rest reuse it). Group key = target input cell
    # (block targets); ports keep their own singleton group. Then order the GROUPS
    # MOST-CONSTRAINED FIRST: a block target with the fewest free broker cells routes
    # first (it has the least freedom), tie-broken by shortest span. Abutment groups
    # (no broker needed) and port groups are unconstrained → last.
    def group_key(n):
        return n.dst if isinstance(n.conn.target, BlockEndpoint) else ("port", n.name)

    groups: dict = {}
    for n in nets:
        groups.setdefault(group_key(n), []).append(n)

    def group_rank(g):
        n0 = g[0]
        if all(is_abutment(m) for m in g) or n0.dst_is_port:
            return (99, min(m.span for m in g))     # unconstrained → last
        if isinstance(n0.conn.target, BlockEndpoint):
            fb = _free_broker_count(n0.dst, n0.dface)
        else:
            fb = 99
        return (fb, min(m.span for m in g))

    ordered_groups = sorted(groups.values(), key=group_rank)
    for g in ordered_groups:
        g.sort(key=lambda m: m.span)
    ordered = [n for g in ordered_groups for n in g]

    # --- PHASE A: reserve a BROKER cell per block-target group. -----------------
    # Every block→block / port→remote-block net taps its target via a broker (a free
    # cell abutting the target input, off the target's own emit face). A fan-in group
    # (both nets into ONE target cell) shares ONE broker. Reserving brokers BEFORE
    # routing corridors guarantees each target keeps its tap even in a dense pack —
    # the greedy "a sibling's corridor stole my only broker cell" failure is removed.
    # Targets are assigned MOST-CONSTRAINED first (fewest broker candidates), and a
    # broker is chosen to leave the MAX free neighbours for other targets (so tight
    # neighbours don't starve each other). No two groups get the same broker cell.
    broker_of: dict = {}       # target input cell -> reserved broker cell
    reserved: set = set()      # all reserved broker cells
    broker_fail: dict = {}     # target input cell -> reason (no broker)

    block_target_groups = [g for g in ordered_groups
                           if isinstance(g[0].conn.target, BlockEndpoint)
                           and not all(is_abutment(m) for m in g)]
    # order most-constrained first
    block_target_groups.sort(
        key=lambda g: _free_broker_count(g[0].dst, g[0].dface))
    for g in block_target_groups:
        tgt = g[0].dst
        if tgt in broker_of:
            continue
        cands = _broker_cells(tgt, g[0].dface, {c: 0 for c in reserved},
                              in_bounds, None, block_cells)
        if not cands:
            broker_fail[tgt] = "no free broker cell abutting the target input"
            continue

        def _free_after(b):
            # how many free cells remain adjacent to OTHER unassigned targets if we
            # take b — prefer a broker that keeps neighbours' options open (fewest own
            # free-neighbour count = corner cell, least disruptive).
            adj = sum(1 for dx, dy in _NEI
                      if in_bounds((b[0] + dx, b[1] + dy))
                      and (b[0] + dx, b[1] + dy) not in block_cells
                      and (b[0] + dx, b[1] + dy) not in reserved)
            return adj
        broker = min(cands, key=_free_after)
        broker_of[tgt] = broker
        reserved.add(broker)

    # --- PHASE B: route corridors with PathFinder negotiated congestion. --------
    # Each corridor net (block→broker, port→broker, block/port→output-port) is routed
    # by A* over the fabric. Cells may be SHARED by multiple nets ONLY same-direction
    # (§1.3), so a cell used by two nets in DIFFERENT directions is a CONFLICT. We
    # resolve conflicts PathFinder-style: route all, then rip up + reroute the conflicted
    # nets with a rising per-cell history cost until no cell carries two directions (or a
    # bound). Reserved brokers block foreign transit (delivery terminus). This routes any
    # legal placement whenever a directionally-consistent set of corridors exists.
    corridor_nets = []
    abut_names = set()
    for n in nets:
        if is_abutment(n):
            abut_names.add(n.name)
        elif n.src_is_port and n.src == n.dst:
            abut_names.add(n.name)     # direct port injection (vestigial)
        else:
            corridor_nets.append(n)

    # history cost per (cell) — grows each pass a cell is over-used (a direction clash).
    hist: dict = {}
    # base present-congestion inflates with the number of nets currently on a cell.
    result_paths: dict = {}    # name -> path
    result_broker: dict = {}   # name -> broker cell (or None)
    fail_reason: dict = {}

    def _target_broker(n):
        return broker_of.get(n.dst)

    def route_corridor(n, cell_use, cell_used_dir):
        """A* one corridor net honouring reserved brokers + a congestion penalty
        (``cell_use`` = #nets currently on a cell, ``cell_used_dir`` = the set of
        directions currently committed on a cell). Returns (path, broker) or None."""
        goal, broker = (n.dst, None) if n.dst_is_port else \
            (_target_broker(n), _target_broker(n))
        if goal is None:
            return None
        # Foreign reserved brokers (not this net's own goal) are DISCOURAGED for transit
        # (a broker delivers its own net; a foreign word transiting it needs the broker's
        # restore face — the build's ``broker_through_face`` reconciles ONE such
        # direction). Price transit heavily so it's a last resort, never a hard wall
        # (a hard wall made egress/ingress fail when a broker sat on the only corridor).
        foreign = reserved - {broker} if broker is not None else set(reserved)
        return _astar_congestion(
            n.src, n.sface, goal, block_cells, foreign, in_bounds,
            src_is_port=n.src_is_port, hist=hist, cell_use=cell_use,
            cell_used_dir=cell_used_dir)

    # PathFinder iterations. Seed order: PORT nets (ingress / egress) FIRST — they must
    # reach a fixed edge cell through a narrow corridor a broker can easily wall, so they
    # claim their corridor before block→broker nets crowd the port; then shortest-span.
    def _seed_key(n):
        port = 0 if (n.dst_is_port or n.src_is_port) else 1
        return (port, n.span)
    order = sorted(corridor_nets, key=_seed_key)
    MAX_PASSES = 40
    best_score = None
    for _pass in range(MAX_PASSES):
        cell_use: dict = {}
        cell_used_dir: dict = {}
        paths: dict = {}
        brokers_used: dict = {}
        failed_now: dict = {}
        for n in order:
            path = route_corridor(n, cell_use, cell_used_dir)
            if path is None:
                failed_now[n.name] = (broker_fail.get(n.dst)
                                      or "no corridor from source to the tap")
                continue
            dist = max(0, len(path) - 1)
            b = _target_broker(n)
            if not n.dst_is_port and b is not None:
                dist += 1
            elif n.dst_is_port and n.conn.target.port.endswith("_out"):
                dist += 1
            if dist > _MAX_HOPS:
                failed_now[n.name] = (f"route is {dist} hops (max {_MAX_HOPS}); "
                                      "no relay programming available")
                continue
            paths[n.name] = path
            brokers_used[n.name] = b
            for i in range(len(path) - 1):
                c, nxt = path[i], path[i + 1]
                d = _step_face(c, nxt)
                if d is None:
                    continue
                cell_use[c] = cell_use.get(c, 0) + 1
                cell_used_dir.setdefault(c, set()).add(d)
        # Congestion = a cell carrying >1 DISTINCT direction (a face conflict). Grow
        # its history so the next pass reroutes around it.
        overused = [c for c, dirs in cell_used_dir.items() if len(dirs) > 1]
        for c in overused:
            hist[c] = hist.get(c, 0) + 1
        if not overused and not failed_now:
            result_paths, result_broker = paths, brokers_used
            fail_reason = {}
            break
        # keep the best (fewest overused + failed) seen
        score = len(overused) + len(failed_now)
        if best_score is None or score < best_score:
            best_score = score
            result_paths, result_broker = dict(paths), dict(brokers_used)
            fail_reason = dict(failed_now)
        # reorder: FAILED nets first (claim their corridor), then conflicted, then the
        # rest — each class port-first / short-first so the fixed-edge nets lead.
        conflicted = {nm for nm in paths
                      if any(c in overused for c in paths[nm])}
        lead = sorted((n for n in order
                       if n.name in failed_now or n.name in conflicted),
                      key=_seed_key)
        tail = sorted((n for n in order
                       if n.name not in failed_now and n.name not in conflicted),
                      key=_seed_key)
        order = lead + tail

    # Assemble results. Abutment / direct-port nets = OK with no waypoints. Corridor
    # nets = OK with their path, or NAMED failure. A residual over-used cell (a face
    # conflict PathFinder couldn't clear) demotes ITS nets to named failures so the
    # build never sees an unsound route (P3.4 — sound failure, not a dead build).
    final_dir: dict = {}
    for nm, path in result_paths.items():
        for i in range(len(path) - 1):
            d = _step_face(path[i], path[i + 1])
            if d is not None:
                final_dir.setdefault(path[i], set()).add(d)
    conflict_cells = {c for c, dirs in final_dir.items() if len(dirs) > 1}

    out: list[RouteResult] = []
    for n in nets:
        if n.name in abut_names:
            out.append(RouteResult(n.name, True, points=None))
            continue
        path = result_paths.get(n.name)
        if path is None:
            reason = (fail_reason.get(n.name) or broker_fail.get(n.dst)
                      or "unroutable")
            out.append(RouteResult(n.name, False, reason=str(reason)))
            continue
        if any(c in conflict_cells for c in path):
            out.append(RouteResult(
                n.name, False,
                reason="corridor face-conflict could not be resolved (a cell must "
                       "carry two directions, §1.3) after rip-up"))
            continue
        out.append(RouteResult(n.name, True, points=list(path)))
    return out


def _broker_cells(target_in, in_face, cell_dir, in_bounds, src, block_cells):
    """Free cells abutting the target input cell that may host a broker.

    A broker may sit on ANY orthogonal neighbour of the target input cell EXCEPT the
    target's own emit face (§7.4 — a delivery must not arrive on the face the block
    drives its own output). It must NOT be a block cell, must not already carry a
    routing corridor (``cell_dir`` — a broker is a terminus, not a shared transit
    cell), and never the source cell itself."""
    forbid = _face_step(in_face)
    forbid_cell = ((target_in[0] + forbid[0], target_in[1] + forbid[1])
                   if forbid else None)
    cells = []
    for dx, dy in _NEI:
        c = (target_in[0] + dx, target_in[1] + dy)
        if c == forbid_cell:
            continue
        if not in_bounds(c) or c in block_cells or c in cell_dir or c == src:
            continue
        cells.append(c)
    return cells


def _astar_congestion(src, sface, goal, block_cells, blocked_brokers, in_bounds, *,
                      src_is_port, hist, cell_use, cell_used_dir):
    """A* src -> goal (inclusive) with PathFinder negotiated-congestion costs.

    The fabric is shared: a cell may carry multiple corridors, but two corridors that
    leave it in DIFFERENT directions is a face conflict (§1.3). Rather than forbid
    sharing outright (which starves a dense pack), we PRICE it: entering a cell costs

        1  (unit distance)
        + hist[cell]                       (accumulated congestion history)
        + present-penalty when this net would leave the cell in a direction that
          CLASHES with a direction already committed there this pass

    Iterating (route all, bump ``hist`` on over-used cells, reroute) pushes clashing
    corridors apart until each cell carries at most one direction (or the bound is hit,
    surfaced as a named failure). Reusing a cell SAME-direction is free of the clash
    penalty, so sound sharing is encouraged and coalesces corridors.

    ``blocked_brokers`` are reserved broker cells FOREIGN to this net (delivery termini);
    transiting one is heavily PRICED (not forbidden — a hard wall made simple egress /
    ingress nets fail when a broker sat on their only corridor; the build's
    ``broker_through_face`` reconciles a single foreign transit direction). Block cells
    are obstacles; the src/target endpoint + goal are always traversable. Returns
    ``[src, ...waypoints..., goal]`` or None."""
    if src == goal:
        return [src]
    CLASH = 40                             # present-congestion penalty for a face clash
    FOREIGN_BROKER = 30                    # transit-a-foreign-broker penalty (last resort)

    def passable(c):
        if not in_bounds(c):
            return False
        if c == goal or c == src:
            return True
        if c in block_cells:
            return False
        return True

    def enter_cost(frm, to):
        """Cost to leave ``frm`` toward ``to`` (charged on ``frm`` — the cell whose
        fwd_face this step commits) PLUS a penalty for entering ``to`` if it is a
        foreign broker (discourage but allow)."""
        cost = 1 + hist.get(frm, 0)
        d = _step_face(frm, to)
        dirs = cell_used_dir.get(frm)
        if dirs and d is not None and d not in dirs:
            cost += CLASH                  # a different direction than already there
        if to in blocked_brokers and to != goal:
            cost += FOREIGN_BROKER
        return cost

    starts = []
    if src_is_port:
        for dx, dy in _NEI:
            c = (src[0] + dx, src[1] + dy)
            if passable(c):
                starts.append(c)
    else:
        step = _face_step(sface)
        emit = (src[0] + step[0], src[1] + step[1]) if step else None
        if emit is not None and passable(emit):
            starts.append(emit)
        for dx, dy in _NEI:
            c = (src[0] + dx, src[1] + dy)
            if c not in starts and passable(c):
                starts.append(c)
    if not starts:
        return None

    def h(c):
        return abs(c[0] - goal[0]) + abs(c[1] - goal[1])

    dist: dict = {}
    prev: dict = {}
    pq: list = []
    tie = 0
    for st in starts:
        if st == src:
            continue
        g0 = enter_cost(src, st)
        if g0 < dist.get(st, 1 << 30):
            dist[st] = g0
            prev[st] = src
            heapq.heappush(pq, (g0 + h(st), tie, st))
            tie += 1
    while pq:
        _f, _t, cur = heapq.heappop(pq)
        if cur == goal:
            break
        dcur = dist[cur]
        if cur == goal:
            continue
        for dx, dy in _NEI:
            nxt = (cur[0] + dx, cur[1] + dy)
            if nxt == src or not passable(nxt):
                continue
            nd = dcur + enter_cost(cur, nxt)
            if nd < dist.get(nxt, 1 << 30):
                dist[nxt] = nd
                prev[nxt] = cur
                heapq.heappush(pq, (nd + h(nxt), tie, nxt))
                tie += 1
    if goal not in prev:
        return None
    chain = [goal]
    node = goal
    while node != src:
        node = prev[node]
        chain.append(node)
    chain.reverse()
    return chain
