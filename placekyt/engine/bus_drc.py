"""Bus DRC — face-conflict + deadlock checks over a bus/broker routing (P1.2, §1.3/§5.3).

The §1.2 bus model shares cells between streams; a cell has ONE ``fwd_face`` (§1.3), so
two streams may share a cell ONLY if they leave it the SAME way — and a broker mid-flip
is a temporal obstacle to UNRELATED through-traffic it would mis-face during its flip
window. Sharing a corridor can also create a CYCLIC handshake wait (a structural
deadlock, §5.3 — topology, not timing; self-timing does NOT let us skip this). This
module validates a set of routes for both, naming the offending cell so a violation is a
SOUND, explained failure rather than a silent dead build (P3.4).

Used two ways:
  * inside the bus router's ``route_all`` as a legality gate (a violated route is
    demoted to a named failure), and
  * standalone (the placeKYT DRC pass) over an already-routed project.
"""

from __future__ import annotations

from dataclasses import dataclass

from model.connection import BlockEndpoint, ChipPortEndpoint


@dataclass
class Violation:
    """One bus DRC finding. ``cell`` is the offending (x, y); ``kind`` is
    ``"face_conflict"`` or ``"deadlock"``; ``reason`` explains it; ``nets`` are the
    connection names involved."""

    cell: tuple
    kind: str
    reason: str
    nets: tuple

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"[{self.kind}] cell {self.cell}: {self.reason} (nets: {', '.join(self.nets)})"


# Unit step per fwd_face code (S=0,E=1,W=2,N=3).
_FWD_DELTA = {0: (0, 1), 1: (1, 0), 2: (-1, 0), 3: (0, -1)}


def _step_dir(a, b):
    """fwd_face int from adjacent ``a`` toward ``b``, or None."""
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


def check_bus(project, routes, chip_types, *, exempt_cells=None,
              egress=None) -> list[Violation]:
    """Validate ``routes`` (``{conn_name: [(x, y), ...]}``) for bus soundness.

    Two checks, per the design:

    (a) **Face conflict (§1.3, P1.2):** if two routed nets both leave a cell in
        DIFFERENT directions, the cell's single ``fwd_face`` cannot serve both — the
        static-face build would mis-face one stream (the BPSK-dead-build). Counted as
        a "leave" is an interior TRANSIT (toward the next waypoint) AND a chip-output
        PORT EGRESS (the final cell's exit face, supplied in ``egress`` — closing the
        old gap where the slicer→x16_out egress at (9,0) silently overlapped the
        Costas→Gardner transit). A net peeling off at its OWN broker (a non-egress
        final cell) is a delivery, not a forward, so it imposes no face.

        ``exempt_cells`` is the set of cells legitimately serving multiple faces — a
        programmed CROSSOVER (demuxes by JUMP entry, §1.2) or a BROKER (deliver +
        restore). Those are PASSED; only a PLAIN cell with ≥2 distinct forwarding
        directions and NO crossover is a violation (so a deliberate un-crossover'd
        conflict is still NAMED — P3.4).

    (b) **Deadlock (§5.3):** build a directed "waits-for" graph over the corridor —
        an edge ``u -> v`` when some net forwards from ``u`` into ``v`` (``u``'s send
        completes only when ``v`` accepts, single-outstanding, §1.1). A directed CYCLE
        in this graph is a cyclic handshake wait — a structural deadlock (topology, not
        timing). Each cycle is named (its cells).

    Returns the list of violations (empty == sound). ``chip_types`` is accepted for
    symmetry with the other routers (bounds are implicit in the waypoints).
    """
    violations: list[Violation] = []
    exempt = set(exempt_cells or ())
    egress = egress or {}

    # Per-cell outgoing direction(s) for FORWARDING (transit interior + port egress)
    # cells, with the net(s) that impose them. A broker/delivery final cell imposes
    # no face (it delivers, then its restore handles through-traffic).
    out_dir: dict[tuple, dict] = {}   # cell -> {dir_code: [net, ...]}
    edges: dict[tuple, set] = {}      # waits-for graph: cell -> {next cells}
    edge_net: dict[tuple, str] = {}   # (u, v) -> a net that imposes it (for naming)

    for name, pts in routes.items():
        pts = [tuple(p) for p in pts]
        for i in range(len(pts) - 1):
            u, v = pts[i], pts[i + 1]
            d = _step_dir(u, v)
            if d is None:
                continue
            # An interior cell (not this net's final delivery cell) transits the word.
            out_dir.setdefault(u, {}).setdefault(d, []).append(name)
            edges.setdefault(u, set()).add(v)
            edge_net.setdefault((u, v), name)
        # A chip-output PORT EGRESS forwards out of its FINAL cell on the port's face
        # (a real face the build must serve) — count it so the (9,0) egress/transit
        # overlap is no longer a silent gap.
        if name in egress and pts:
            ecell, eface = egress[name]
            ecell = tuple(ecell)
            if eface is not None:
                out_dir.setdefault(ecell, {}).setdefault(int(eface), []).append(name)

    # (a) face conflict: a cell with >1 distinct outgoing direction across nets,
    #     UNLESS it is an exempt (crossover/broker) cell that serves them legally.
    for cell, dirs in out_dir.items():
        if cell in exempt:
            continue
        if len(dirs) > 1:
            nets = tuple(sorted({n for lst in dirs.values() for n in lst}))
            dir_names = {0: "S", 1: "E", 2: "W", 3: "N"}
            ds = "/".join(dir_names[d] for d in sorted(dirs))
            violations.append(Violation(
                cell=cell, kind="face_conflict",
                reason=f"two streams must leave this cell in different directions "
                       f"({ds}) — a cell has one fwd_face (§1.3)",
                nets=nets))

    # (b) deadlock: a directed cycle in the waits-for graph.
    for cycle in _find_cycles(edges):
        nets = tuple(sorted({edge_net.get((cycle[i], cycle[(i + 1) % len(cycle)]), "")
                             for i in range(len(cycle))} - {""}))
        violations.append(Violation(
            cell=cycle[0], kind="deadlock",
            reason="cyclic handshake wait on the corridor (structural deadlock, "
                   f"§5.3): {' -> '.join(str(c) for c in cycle)} -> {cycle[0]}",
            nets=nets))

    return violations


def _find_cycles(edges: dict[tuple, set]) -> list[list]:
    """Return one representative simple cycle per strongly-connected back-edge in the
    directed graph ``edges`` (cell -> {cells}). DFS with a recursion stack; on a
    back-edge, extract the cycle. At most one cycle reported per starting back-edge —
    enough to NAME a deadlock soundly without enumerating them all."""
    cycles: list[list] = []
    color: dict[tuple, int] = {}    # 0=unseen,1=on-stack,2=done
    stack: list = []
    seen_cycle_keys: set = set()

    def dfs(u):
        color[u] = 1
        stack.append(u)
        for v in edges.get(u, ()):  # noqa: SIM118
            c = color.get(v, 0)
            if c == 0:
                dfs(v)
            elif c == 1:
                # back-edge u->v: cycle = stack[idx(v):] + [u]
                if v in stack:
                    idx = stack.index(v)
                    cyc = stack[idx:]
                    key = frozenset(cyc)
                    if key not in seen_cycle_keys:
                        seen_cycle_keys.add(key)
                        cycles.append(list(cyc))
        stack.pop()
        color[u] = 2

    for node in list(edges.keys()):
        if color.get(node, 0) == 0:
            dfs(node)
    return cycles


def check_project_bus(project, chip_types, catalog=None) -> list[Violation]:
    """Convenience: run :func:`check_bus` over a project's ROUTED connections (per
    chip). Exempts the cells that legitimately serve multiple faces — programmed
    CROSSOVERS (:func:`bus_router.crossover_plan`) and BROKERS
    (:func:`bus_router.broker_plan`) — and supplies chip-output-port EGRESS faces so
    an un-crossover'd transit/egress overlap is NAMED, not silently passed. Returns
    all violations across chips. ``catalog`` (optional) enables the crossover/broker
    derivation; without it the bare-route checks still run."""
    routes: dict = {}
    for conn in project.connections:
        if conn.is_routed:
            routes[conn.name] = [(p.x, p.y) for p in conn.route]
    exempt, egress = _bus_exempt_and_egress(project, chip_types, catalog)
    viols = check_bus(project, routes, chip_types, exempt_cells=exempt,
                      egress=egress)
    viols.extend(_check_single_cell_inout(project))
    return viols


def _check_single_cell_inout(project) -> list[Violation]:
    """DRC the SINGLE-CELL bus-fed deadlock hazard (§5.3, the user-flagged risk).

    A block with exactly ONE cell that both RECEIVES its input (a broker/route delivers
    into it) AND DRIVES its output must NOT have its input arrive on the SAME face its
    output drives — that puts both on one single-outstanding link, a deadlock waiting to
    happen (it "happens to run today"). For each such cell we read the routed geometry:
      * input ARRIVAL face  = cell -> the input net's final waypoint (the broker), and
      * output DRIVE  face  = cell -> the output net's first waypoint,
    and ERROR (NAMED) when they coincide. A single-cell block fed DIRECTLY by a chip
    input port at its own cell (no broker) is exempt — there is no shared-face hazard.
    This is the authoritative gate (P3.4): the router PREFERS a safe split, but if a
    geometry admits none, the unsafe route is built only over THIS named failure."""
    from model.connection import BlockEndpoint, ChipPortEndpoint

    # Single-cell blocks: cell -> block name.
    one_cell: dict = {}
    for blk in project.blocks:
        pl = blk.placement
        if pl is None or len(pl.cells) != 1:
            continue
        one_cell[(pl.cells[0].x, pl.cells[0].y)] = blk.name

    if not one_cell:
        return []

    # Per single-cell block: the input net's broker (final waypoint) + whether it is a
    # direct port injection, and the output net's first waypoint.
    in_arrival: dict = {}   # cell -> arrival face code (from a brokered input net)
    in_is_direct_port: set = set()
    out_drive: dict = {}    # cell -> output drive face code
    for conn in project.connections:
        if not conn.is_routed or not conn.route:
            continue
        pts = [(p.x, p.y) for p in conn.route]
        # Input net into a single-cell block: target is that block.
        if isinstance(conn.target, BlockEndpoint):
            blk = project.block(conn.target.block)
            if blk is not None and len(blk.placement.cells) == 1:
                cell = (blk.placement.cells[0].x, blk.placement.cells[0].y)
                last = pts[-1]
                if last == cell and isinstance(conn.source, ChipPortEndpoint):
                    in_is_direct_port.add(cell)   # port injects at the cell itself
                else:
                    f = _step_dir(cell, last)     # arrives from the broker direction
                    if f is not None:
                        in_arrival.setdefault(cell, f)
        # Output net from a single-cell block: source is that block.
        if isinstance(conn.source, BlockEndpoint):
            blk = project.block(conn.source.block)
            if blk is not None and len(blk.placement.cells) == 1:
                cell = (blk.placement.cells[0].x, blk.placement.cells[0].y)
                # first waypoint != the cell itself gives the drive face.
                nxt = pts[1] if (len(pts) > 1 and pts[0] == cell) else \
                    (pts[0] if pts[0] != cell else None)
                if nxt is not None:
                    f = _step_dir(cell, nxt)
                    if f is not None:
                        out_drive.setdefault(cell, f)

    out: list[Violation] = []
    dir_names = {0: "S", 1: "E", 2: "W", 3: "N"}
    for cell, name in one_cell.items():
        if cell in in_is_direct_port:
            continue
        inf = in_arrival.get(cell)
        of = out_drive.get(cell)
        if inf is None or of is None:
            continue
        if inf == of:
            out.append(Violation(
                cell=cell, kind="single_cell_inout",
                reason=f"single-cell block '{name}' is bus-fed and its input arrives "
                       f"on the same face ({dir_names[inf]}) its output drives — input "
                       "and output contend on one single-outstanding link (§5.3 "
                       "deadlock hazard). Place/route so input-face != output-face.",
                nets=(name,)))
    return out


def _bus_exempt_and_egress(project, chip_types, catalog):
    """Derive (exempt_cells, egress) for a routed project: exempt = crossover ∪ broker
    cells (serve multiple faces legally); egress = ``{net: (final_cell, port_face)}``
    for each block→chip-output-port net (so its egress face counts as a forward)."""
    from model.connection import ChipPortEndpoint

    exempt: set = set()
    egress: dict = {}
    if catalog is not None:
        try:
            from .bus_router import broker_plan, crossover_plan
            chip_ids = [c.id for c in project.chips] or [0]
            for cid in chip_ids:
                ct = _chip_type_for(project, cid, chip_types)
                if ct is None:
                    continue
                exempt |= set(broker_plan(project, cid, ct, catalog).keys())
                exempt |= set(crossover_plan(project, cid, ct, catalog).keys())
        except Exception:  # noqa: BLE001 — bare-route checks still apply
            pass
    # Port-egress faces per net.
    port_face = {}
    for c in project.chips or []:
        ct = chip_types.get(c.type_name) if c.type_name else None
        if ct is None and project.chip_type:
            ct = chip_types.get(project.chip_type)
        if ct is not None:
            for p in ct.ports:
                port_face[p.name] = _face_code_of(getattr(p, "face", None))
    if not port_face and project.chip_type and chip_types.get(project.chip_type):
        for p in chip_types[project.chip_type].ports:
            port_face[p.name] = _face_code_of(getattr(p, "face", None))
    for conn in project.connections:
        if conn.is_routed and isinstance(conn.target, ChipPortEndpoint) \
                and conn.target.port.endswith("_out") and conn.route:
            f = port_face.get(conn.target.port)
            if f is not None:
                egress[conn.name] = ((conn.route[-1].x, conn.route[-1].y), f)
    return exempt, egress


def _chip_type_for(project, chip_id, chip_types):
    c = project.chip(chip_id)
    name = (c.type_name if c and c.type_name else project.chip_type)
    return chip_types.get(name) if name else None


_FACE_VAL = {"south": 0, "east": 1, "west": 2, "north": 3}


def _face_code_of(face):
    if face is None:
        return None
    val = getattr(face, "value", face)
    if isinstance(val, str):
        return _FACE_VAL.get(val)
    try:
        return int(val) & 0x3
    except (TypeError, ValueError):
        return None
