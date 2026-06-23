"""CP-SAT bus-sharing auto-router (auto-P&R Phase 3, §7).

The heuristic BFS corridor router (``autoroute.py``) keeps every net on its OWN
free cells (node-disjoint), so a dense design — an I/Q dual path, a fan-out — can
run out of corridors and report "no free corridor" even when a legal layout exists.
This router closes that gap with the design's **time-multiplexed bus** model (§1):
multiple nets may **share** a transit cell as long as they all leave it on the SAME
face (a cell has one ``fwd_face``), each word distinguished by its ``dest`` tag. A
shared corridor carries A's word then B's word, sequentially.

Formulation (OR-Tools CP-SAT — the design's "model as constraints", §7.3):
- Grid graph over FREE cells (block + transit cells are obstacles); directed edges
  between 4-neighbours.
- Per net, boolean edge vars with flow conservation → a simple source→sink path.
- **Single-fwd_face coupling:** for each cell, at most one outgoing direction is
  active across ALL nets; every net leaving that cell uses that one direction. This
  is what makes sharing legal (a bus segment) and forbids two nets crossing a cell
  in different directions.
- Per-net hop budget ≤ 31 (+1 for an output-port target).
- Objective: minimise total distinct cell-faces used (compact bus; sharing is
  free — the whole point).

The solver returns a routed path per net, or proves a net unroutable. It is the
authority on "unroutable" (§7.3); ``autoroute.py``'s BFS remains the fast first
attempt. If OR-Tools is not installed, :func:`route_all_cpsat` raises
``CpSatUnavailable`` and the caller falls back to the heuristic router.
"""

from __future__ import annotations

from model.connection import BlockEndpoint, ChipPortEndpoint
from model.enums import Face

from .autoroute import (AutoRouteReport, AutoRouter, RouteResult, _FACE_STEP,
                        _MAX_HOPS)


class CpSatUnavailable(RuntimeError):
    """Raised when OR-Tools / CP-SAT is not importable."""


def _cp_model():
    try:
        from ortools.sat.python import cp_model
    except ImportError as exc:  # noqa: BLE001
        raise CpSatUnavailable(
            "OR-Tools not installed — `pip install ortools` (the [router] extra) "
            "to enable the CP-SAT bus-sharing router.") from exc
    return cp_model


# Opposite face — a net must not immediately U-turn, and the single-fwd_face rule
# already prevents a cell hosting two opposite outgoing edges.
_NEI = ((1, 0), (-1, 0), (0, 1), (0, -1))


def route_all_cpsat(project, chip_types, port_cell_provider, *,
                    max_time_s: float = 10.0) -> AutoRouteReport:
    """Route every UNROUTED block↔block / port↔block net on a single chip JOINTLY
    with CP-SAT, allowing bus-sharing. Returns an :class:`AutoRouteReport`. Raises
    :class:`CpSatUnavailable` if OR-Tools is missing.

    ``port_cell_provider`` and endpoint resolution are reused from
    :class:`AutoRouter` so geometry matches the heuristic router exactly.
    """
    cp_model = _cp_model()
    helper = AutoRouter(project, chip_types, port_cell_provider)

    # Collect unrouted nets + resolve endpoints (reusing the heuristic helper).
    nets = []          # (name, chip, (sx,sy), sface, (dx,dy), src_is_port, out_port)
    results: list[RouteResult] = []
    chips_seen = set()
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
        (schip, sx, sy, sface), (dchip, dx, dy, _df) = src, dst
        if schip != dchip:
            results.append(RouteResult(conn.name, False,
                                       reason="cross-chip auto-route not supported yet"))
            continue
        src_is_port = isinstance(conn.source, ChipPortEndpoint)
        out_port = (isinstance(conn.target, ChipPortEndpoint)
                    and conn.target.port.endswith("_out"))
        nets.append((conn.name, schip, (sx, sy), sface, (dx, dy),
                     src_is_port, out_port))
        chips_seen.add(schip)

    if not nets:
        return AutoRouteReport(results)

    # This cut solves ONE chip at a time (the common case). Group by chip.
    by_chip: dict[int, list] = {}
    for n in nets:
        by_chip.setdefault(n[1], []).append(n)

    for chip_id, chip_nets in by_chip.items():
        ct = helper._chip_type(chip_id)
        if ct is None:
            for nm, *_ in chip_nets:
                results.append(RouteResult(nm, False, reason="no chip type"))
            continue
        # Obstacles: block + transit cells (and any already-routed connection
        # cells) — nets transit only FREE cells, never an active block cell (§1.2).
        occ = set()
        for blk in project.blocks:
            pl = blk.placement
            if pl is None or pl.chip != chip_id:
                continue
            occ.update((c.x, c.y) for c in pl.cells)
            occ.update((t.x, t.y) for t in pl.transit_cells)
        for conn in project.connections:
            if conn.is_routed and helper._chip_of(conn) == chip_id:
                occ.update((p.x, p.y) for p in conn.route)
        results.extend(_solve_chip(cp_model, ct, occ, chip_nets, max_time_s))

    # Preserve the project's connection order in the report.
    order = {c.name: i for i, c in enumerate(project.connections)}
    results.sort(key=lambda r: order.get(r.name, 1 << 30))
    return AutoRouteReport(results)


def _solve_chip(cp_model, ct, occ, chip_nets, max_time_s):
    """Solve all nets on one chip jointly. Endpoints (source/sink cells) are
    always usable even if they coincide with a block cell (the path starts AT the
    producer's output cell and ends AT the consumer's input cell)."""
    W, H = ct.width, ct.height

    def _in_bounds(c):
        return 0 <= c[0] < W and 0 <= c[1] < H

    # Reject (soundly) any net whose source or sink cell is OFF-GRID — e.g. a
    # port anchor resolved just outside the array edge. These can't be solved on
    # the grid graph; name them rather than crashing on a missing graph node.
    out_results = []
    solvable = []
    for n in chip_nets:
        (nm, _ch, s, _sf, d, _sp, _op) = n
        if not (_in_bounds(s) and _in_bounds(d)):
            out_results.append(RouteResult(
                nm, False, reason="endpoint cell is off the array grid"))
        else:
            solvable.append(n)
    chip_nets = solvable
    if not chip_nets:
        return out_results

    endpoints = set()
    for (_nm, _ch, s, _sf, d, _sp, _op) in chip_nets:
        endpoints.add(s)
        endpoints.add(d)

    def free(c):
        x, y = c
        return 0 <= x < W and 0 <= y < H and (c in endpoints or c not in occ)

    # Directed edges over free cells.
    cells = [(x, y) for y in range(H) for x in range(W) if free((x, y))]
    cellset = set(cells)
    edges = []          # (u, v, dir_index)
    out_edges = {c: [] for c in cells}
    in_edges = {c: [] for c in cells}
    for c in cells:
        for di, (ddx, ddy) in enumerate(_NEI):
            v = (c[0] + ddx, c[1] + ddy)
            if v in cellset:
                edges.append((c, v, di))
                out_edges[c].append((c, v, di))
                in_edges[v].append((c, v, di))

    m = cp_model.CpModel()
    # x[net][edge] — net uses this directed edge.
    x = {}
    # cellface[cell, di] — SOME net leaves `cell` in direction di (the shared
    # fwd_face). At most one di active per cell (single fwd_face).
    cellface = {}
    for c in cells:
        for di in range(4):
            cellface[(c, di)] = m.NewBoolVar(f"cf_{c}_{di}")
        m.Add(sum(cellface[(c, di)] for di in range(4)) <= 1)

    src_cell = {nm: s for (nm, _ch, s, _sf, d, _sp, _op) in chip_nets}
    dst_cell = {nm: d for (nm, _ch, s, _sf, d, _sp, _op) in chip_nets}

    for (nm, _ch, s, sface, d, src_is_port, out_port) in chip_nets:
        xe = {}
        for e in edges:
            xe[e] = m.NewBoolVar(f"x_{nm}_{e[0]}_{e[1]}")
        x[nm] = xe
        # Flow conservation → a simple source→sink path.
        for c in cells:
            outs = sum(xe[e] for e in out_edges[c])
            ins = sum(xe[e] for e in in_edges[c])
            if c == s and c == d:
                continue
            elif c == s:
                m.Add(outs == 1)
                m.Add(ins == 0)
            elif c == d:
                m.Add(ins == 1)
                m.Add(outs == 0)
            else:
                # transit: in == out, and ≤1 (a simple path visits a cell once)
                m.Add(ins == outs)
                m.Add(outs <= 1)
        # The first edge must leave the source on its emit face — UNLESS the
        # source is a chip input port (which injects AT its own cell, §autoroute).
        if not src_is_port:
            want = _FACE_STEP.get(sface)
            if want is not None:
                wx = (s[0] + want[0], s[1] + want[1])
                if wx in cellset:
                    # force the source's single out-edge to be toward `want`
                    for e in out_edges[s]:
                        if e[1] != wx:
                            m.Add(xe[e] == 0)
                else:
                    # emit face blocked → unroutable for this net
                    pass
        # Hop budget: path length = #edges ≤ 31 (+1 for output-port target).
        budget = _MAX_HOPS - (1 if out_port else 0)
        m.Add(sum(xe[e] for e in edges) <= budget)
        # Couple net edges to the shared cell-face: if this net leaves cell c in
        # direction di, then cellface[c,di] must be set (all nets share one face).
        for (u, v, di) in edges:
            m.Add(cellface[(u, di)] >= xe[(u, v, di)])

    # SOUNDNESS: a plain transit cell forwards EVERYTHING out its one fwd_face, so
    # it cannot DEMUX two streams to different sinks (that needs a programmed
    # broker/Crossover, not emitted by this cut). Therefore two nets may share a
    # cell ONLY IF they have the SAME destination cell (fan-out / common-sink).
    # Nets with DIFFERENT sinks must be node-disjoint on transit cells. Without
    # this the build is geometrically valid but mis-computes (streams collide).
    use = {}   # (net, cell) -> bool: net occupies this cell (as transit or endpoint)
    for (nm, _ch, s, _sf, d, _sp, _op) in chip_nets:
        xe = x[nm]
        for c in cells:
            occ_c = m.NewBoolVar(f"use_{nm}_{c}")
            # a net "uses" a cell if it has an outgoing OR incoming edge there, or
            # it is the net's own source/sink.
            inc = [xe[e] for e in out_edges[c]] + [xe[e] for e in in_edges[c]]
            if c == s or c == d:
                m.Add(occ_c == 1)
            elif inc:
                m.AddMaxEquality(occ_c, inc)
            else:
                m.Add(occ_c == 0)
            use[(nm, c)] = occ_c
    nets_list = [n[0] for n in chip_nets]
    sink_of = {n[0]: n[4] for n in chip_nets}
    src_of = {n[0]: n[2] for n in chip_nets}
    for i in range(len(nets_list)):
        for j in range(i + 1, len(nets_list)):
            a, b = nets_list[i], nets_list[j]
            if sink_of[a] == sink_of[b]:
                continue          # common sink → sharing is sound (fan-out)
            # different sinks → forbid co-occupying any cell that is not an
            # endpoint of BOTH (endpoints never demux a stream onward).
            for c in cells:
                if c in (src_of[a], sink_of[a], src_of[b], sink_of[b]):
                    continue
                m.Add(use[(a, c)] + use[(b, c)] <= 1)

    # Objective: minimise the number of active cell-faces (compact bus). Sharing
    # is free — two nets down one corridor cost the same as one.
    m.Minimize(sum(cellface.values()))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(max_time_s)
    solver.parameters.num_search_workers = 8
    status = solver.Solve(m)

    out: list[RouteResult] = []
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        for nm in src_cell:
            path = _reconstruct(solver, x[nm], src_cell[nm], dst_cell[nm],
                                out_edges)
            if path is None:
                out.append(RouteResult(nm, False,
                                       reason="no path in CP-SAT solution"))
            else:
                out.append(RouteResult(nm, True, points=path))
    else:
        # Infeasible / unknown → name every net (sound failure).
        why = ("no shared-bus routing exists (proven infeasible)"
               if status == cp_model.INFEASIBLE
               else "CP-SAT timed out without a solution")
        for nm in src_cell:
            out.append(RouteResult(nm, False, reason=why))
    return out_results + out


def _reconstruct(solver, xe, src, dst, out_edges):
    """Walk the chosen edges from src to dst into a waypoint list [src,…,dst]."""
    path = [src]
    cur = src
    seen = {src}
    while cur != dst:
        nxt = None
        for e in out_edges.get(cur, []):
            if solver.Value(xe[e]) == 1:
                nxt = e[1]
                break
        if nxt is None or nxt in seen:
            return None
        path.append(nxt)
        seen.add(nxt)
        cur = nxt
        if len(path) > 1024:
            return None
    return path
