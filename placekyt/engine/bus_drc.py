# SPDX-License-Identifier: GPL-3.0-or-later
"""Bus DRC — face-conflict + deadlock checks over a bus/broker routing.

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
    ``"face_conflict"``, ``"deadlock"`` or ``"port_transit"``; ``reason`` explains
    it; ``nets`` are the connection names involved."""

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

    Checks (a) face conflict, (b) waits-for deadlock, (c) own-block delivery
    cycle (INV-32), (d) used chip-port cell transit. The first two, per the
    design:

    (a) **Face conflict:** if two routed nets both leave a cell in
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

    # (c) OWN-BLOCK delivery cycle (INV-32, the data_link f2c saturation lockup):
    # a net SOURCED at block B whose corridor TRANSITS a cell that DELIVERS a net
    # INTO B closes a wait cycle through B's own internals — B's output words
    # occupy the single-outstanding link its own NEXT input must cross. This
    # shape is deadlock-CERTAIN (same stream, causally chained), unlike a general
    # cell-cycle over independent corridors (the proven-saturated coherent RX
    # carries harmless topological rings of unrelated segments, so a broader
    # through-block collapse would false-positive it — scope deliberately kept
    # to the certain shape; the router additionally hard-forbids it while
    # routing, and the saturated example gates are the empirical check beyond).
    # Needs ``project`` for block ownership; bare route-dict callers skip it.
    if project is not None:
        blk_of_cell: dict[tuple, str] = {}
        for blk in getattr(project, "blocks", ()) or ():
            pl = getattr(blk, "placement", None)
            if pl is None or not getattr(pl, "cells", None):
                continue
            for c in pl.cells:
                blk_of_cell[(c.x, c.y)] = blk.name
            for t in getattr(pl, "transit_cells", ()) or ():
                blk_of_cell[(t.x, t.y)] = blk.name
        deliver_into: dict[str, dict] = {}   # block -> {broker cell: net name}
        src_block: dict[str, str] = {}       # net name -> source block name
        for conn in getattr(project, "connections", ()) or ():
            name = getattr(conn, "name", "")
            tgt = getattr(conn, "target", None)
            pts = routes.get(name)
            if pts and isinstance(tgt, BlockEndpoint):
                last = tuple(pts[-1])
                if blk_of_cell.get(last) != tgt.block:
                    deliver_into.setdefault(tgt.block, {})[last] = name
            src = getattr(conn, "source", None)
            if isinstance(src, BlockEndpoint):
                src_block[name] = src.block
        for name, pts in routes.items():
            b = src_block.get(name)
            if b is None or b not in deliver_into:
                continue
            hits = [(tuple(p), deliver_into[b][tuple(p)])
                    for p in pts[1:] if tuple(p) in deliver_into[b]]
            for cell, in_net in hits:
                violations.append(Violation(
                    cell=cell, kind="deadlock",
                    reason=f"net '{name}' (output of block '{b}') transits the "
                           f"broker cell that DELIVERS net '{in_net}' into "
                           f"'{b}' — the block's own output words block its "
                           "next input delivery (own-block wait cycle, "
                           "INV-32/§5.3)",
                    nets=tuple(sorted({name, in_net}))))

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

    # (d) USED chip-port cell transit (the FLLBandEdge pinch, 2026-08-16): a
    # corridor threading THROUGH a port cell that some net actually uses as its
    # I/O terminus is a SILENT-DEAD build, not a routable cell. A used INPUT
    # port cell's programming delivers the host-injected words toward the block
    # (a wide block ring that pinches the side channels against a corner port
    # made the router wrap a block→block corridor through x16_in — route "ok",
    # build "ok", injections swallowed in 6 sim events); a used OUTPUT port
    # cell's egress faces off-chip, which no in-fabric transit can share (the
    # rotated complex fan-in that snaked through x16_out and lost both
    # operands). Only the port's OWN nets may touch the cell (source nets start
    # there; egress nets end there). UNUSED port cells are plain routing cells
    # and stay legal (the documented column-9 passage) — the routers merely
    # soft-discourage them.
    violations.extend(check_port_transits(project, routes, chip_types))

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


def used_port_cells(project, chip_types) -> dict:
    """``{(x, y): (port_name, [net names using it], {chip ids})}`` for every
    chip-port cell that is an ENDPOINT of some connection (the port actually
    injects or egresses).

    Usage comes from the LOGICAL connections, not the routes: an input net may
    legitimately carry no waypoints (direct port injection renders as a fly line)
    yet its port cell is still live delivery hardware. Cells are (x, y) without a
    chip id (this module's route convention); the CHIP-id set lets a multichip
    caller scope the match (chip 1's corridor over its own (0, 0) must not be
    flagged because chip 0's x16_in at (0, 0) is used)."""
    if project is None or not chip_types:
        return {}
    cell_of: dict[str, tuple] = {}
    for ct in chip_types.values():
        for p in getattr(ct, "ports", ()) or ():
            cell_of[p.name] = (p.cell_x, p.cell_y)
    used: dict[tuple, tuple] = {}
    for conn in getattr(project, "connections", ()) or ():
        for ep in (getattr(conn, "source", None), getattr(conn, "target", None)):
            if isinstance(ep, ChipPortEndpoint) and ep.port in cell_of:
                cell = cell_of[ep.port]
                entry = used.setdefault(cell, (ep.port, [], set()))
                entry[1].append(getattr(conn, "name", ""))
                entry[2].add(getattr(ep, "chip", 0))
    return used


def check_port_transits(project, routes, chip_types) -> list[Violation]:
    """The check-(d) body (see :func:`check_bus`): every occupation of a USED
    chip-port cell by a net that does not OWN the port (its own source/target)
    is a named ``port_transit`` violation — whether mid-corridor (a transit that
    the port programming re-faces into dead space) or terminal (a broker landed
    ON the port cell). Standalone so router escalation paths (maze/heuristic)
    can demote offenders without running the full bus DRC."""
    if project is None:
        return []
    used = used_port_cells(project, chip_types)
    if not used:
        return []
    # Cells of each placed block (a block may sit ON a port cell — the direct
    # port-injection idiom — and its own nets then legitimately start/end there),
    # plus each block's chip id (to scope multichip (x, y) collisions).
    cells_of_block: dict[str, set] = {}
    chip_of_block: dict[str, int] = {}
    for blk in getattr(project, "blocks", ()) or ():
        pl = getattr(blk, "placement", None)
        if pl is None or not getattr(pl, "cells", None):
            continue
        cs = {(c.x, c.y) for c in pl.cells}
        cs |= {(t.x, t.y) for t in getattr(pl, "transit_cells", ()) or ()}
        cells_of_block[blk.name] = cs
        chip_of_block[blk.name] = getattr(pl, "chip", 0)
    # Port cells a net legitimately touches: the cell of a chip port it is wired
    # to, or a cell of its own source/target BLOCK (its route terminal) — and the
    # net's own CHIP id (a corridor on chip 1 never conflicts with chip 0's port).
    owns: dict[str, set] = {}     # net name -> port cells it legitimately touches
    chip_of_net: dict[str, int] = {}
    for conn in getattr(project, "connections", ()) or ():
        name = getattr(conn, "name", "")
        for ep in (getattr(conn, "source", None), getattr(conn, "target", None)):
            if isinstance(ep, ChipPortEndpoint):
                chip_of_net.setdefault(name, getattr(ep, "chip", 0))
                for cell, (pname, _nets, _chips) in used.items():
                    if pname == ep.port:
                        owns.setdefault(name, set()).add(cell)
            elif isinstance(ep, BlockEndpoint):
                if ep.block in chip_of_block:
                    chip_of_net.setdefault(name, chip_of_block[ep.block])
                for cell in cells_of_block.get(ep.block, ()):
                    if cell in used:
                        owns.setdefault(name, set()).add(cell)
    out: list[Violation] = []
    for name, pts in routes.items():
        allowed = owns.get(name, set())
        for p in pts:
            c = tuple(p)
            if c in used and c not in allowed \
                    and chip_of_net.get(name, 0) in used[c][2]:
                pname, port_nets, _chips = used[c]
                others = sorted(set(port_nets) - {name})
                out.append(Violation(
                    cell=c, kind="port_transit",
                    reason=f"net '{name}' rides through chip port cell {c} "
                           f"('{pname}', used by net(s) {others or port_nets}) — "
                           "the port's injection/egress programming and the "
                           "corridor's face programming destroy each other "
                           "(silent dead chip); route around the port or fail "
                           "named",
                    # ONLY the riding net: the port's own nets are innocent (a
                    # demotion pass keyed on ``nets`` must not fail them too).
                    nets=(name,)))
                break                      # one violation per net is enough
    return out


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
        # Only WAYPOINT routes (lists) have cells to check; an ABUTMENT (route ==
        # "abutment", is_routed True) has no corridor — iterating the string raises.
        if isinstance(conn.route, list) and conn.route:
            routes[conn.name] = [(p.x, p.y) for p in conn.route]
    exempt, egress = _bus_exempt_and_egress(project, chip_types, catalog)
    viols = check_bus(project, routes, chip_types, exempt_cells=exempt,
                      egress=egress)
    viols.extend(_check_single_cell_inout(project))
    viols.extend(_check_dual_input_same_face(project, catalog, chip_types))
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
        # WAYPOINT routes only; an ABUTMENT (route == "abutment") has no corridor cells.
        if not (isinstance(conn.route, list) and conn.route):
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


DIR_NAMES = {0: "S", 1: "E", 2: "W", 3: "N"}
_DIR_DELTA = {0: (0, 1), 1: (1, 0), 2: (-1, 0), 3: (0, -1)}


def _needs_distinct_faces(blk, catalog, cache) -> bool:
    """True when ``blk``'s type declares ``NEEDS_DISTINCT_INPUT_FACES``.

    Cached per ``(type, library)`` in the caller-supplied ``cache`` dict; False for
    any block whose spec can't be instantiated (never a false alarm)."""
    if catalog is None:
        return False
    key = (blk.type, blk.library)
    if key not in cache:
        try:
            inst = catalog.instantiate(blk.type, "__drc_probe__",
                                       getattr(blk, "params", None),
                                       library=blk.library)
            cache[key] = bool(getattr(inst, "NEEDS_DISTINCT_INPUT_FACES", False))
        except Exception:  # noqa: BLE001
            cache[key] = False
    return cache[key]


@dataclass
class FaceLockArrivals:
    """Arrival-face geometry of ONE face-locking block, shared by the DRC and the UI.

    ``cell`` is the block's head (input) cell; ``faces`` maps target PORT name →
    arrival face code (S/E/W/N ints); ``nets`` maps that port → the connection NAME
    that delivers it; ``colliding`` is the set of ports whose face is shared with at
    least one sibling port (empty when the layout is legal)."""

    block: str
    cell: tuple
    faces: dict
    nets: dict
    colliding: set

    @property
    def collision_faces(self) -> set:
        """The face codes that carry more than one input."""
        return {self.faces[p] for p in self.colliding}


def face_lock_arrivals(project, catalog) -> dict:
    """``{block_name: FaceLockArrivals}`` for every placed NEEDS_DISTINCT_INPUT_FACES
    block in ``project``, computed from the ROUTED geometry.

    This is the SINGLE source of truth for "which face does each input of a
    face-locking block arrive on, and do any of them collide". The build DRC
    (:func:`_check_dual_input_same_face`) and the canvas both call it, so the editor
    can show the same constraint the DRC enforces INSTEAD of only reporting it after
    the fact. Qt-free by construction — it reads only the project model.

    Arrival face = the direction from the block's head cell toward the route's last
    corridor waypoint; for an ABUTMENT (no waypoints) it is the direction toward the
    driving block's output cell."""
    if catalog is None:
        return {}

    flag_cache: dict = {}
    out: dict = {}
    for blk in project.blocks:
        pl = blk.placement
        if pl is None or not pl.cells:
            continue
        if not _needs_distinct_faces(blk, catalog, flag_cache):
            continue
        out[blk.name] = FaceLockArrivals(
            block=blk.name, cell=(pl.cells[0].x, pl.cells[0].y),
            faces={}, nets={}, colliding=set())

    if not out:
        return out

    for conn in project.connections:
        if not isinstance(conn.target, BlockEndpoint):
            continue
        rec = out.get(conn.target.block)
        if rec is None:
            continue
        cell = rec.cell
        # Route waypoints may include non-coordinate sentinels (e.g. an ABUTMENT
        # marker); keep only real (x, y) points.
        pts = [(p.x, p.y) for p in (conn.route or []) if hasattr(p, "x")]
        f = None
        if pts:
            last = pts[-1]
            src = pts[-2] if (last == cell and len(pts) >= 2) else last
            f = _step_dir(cell, src)
        else:
            # Abutment (no real waypoints): arrival face is toward the driver's
            # output cell.
            db = project.block(getattr(conn.source, "block", None) or "")
            if db is not None and db.placement and db.placement.cells:
                oc = (db.placement.cells[-1].x, db.placement.cells[-1].y)
                f = _step_dir(cell, oc)
        if f is not None:
            rec.faces[conn.target.port] = f
            rec.nets[conn.target.port] = conn.name

    for rec in out.values():
        if len(rec.faces) < 2:
            continue   # <2 routed inputs — single/logical feed, no lock hazard
        by_face: dict = {}
        for port, f in rec.faces.items():
            by_face.setdefault(f, []).append(port)
        for ports in by_face.values():
            if len(ports) > 1:
                rec.colliding.update(ports)
    return out


def _iq_rail_pair(project, catalog, block, ports):
    """``(source_block, (i_port, q_port))`` when the colliding input ``ports`` of
    ``block`` are fed by the I and Q rails of ONE complex output of ONE source block,
    else ``None``.

    A complex GNU Radio link is ONE port but TWO placeKYT nets — the importer
    SYNTHESISES the Q rail, so the user never drew it and has no reason to expect a
    second wire. Detecting that case lets the message say WHY two nets left the same
    cell. The sibling naming rules live in ``grc_import._iq_sibling`` (the single
    authority: ``yi``/``yq``, ``re``/``im``, and the position-1 tapped forms); they
    are consulted here, never re-derived."""
    try:
        from engine.grc_import import _iq_sibling
    except Exception:  # noqa: BLE001 — importer unavailable ⇒ no enrichment
        return None
    if catalog is None or len(ports) != 2:
        return None
    srcs: dict = {}
    for conn in project.connections:
        if not isinstance(conn.target, BlockEndpoint) \
                or conn.target.block != block or conn.target.port not in ports:
            continue
        if not isinstance(conn.source, BlockEndpoint):
            return None
        srcs[conn.target.port] = (conn.source.block, conn.source.port)
    if len(srcs) != 2:
        return None
    (pa, (sba, spa)), (pb, (sbb, spb)) = sorted(srcs.items())
    if sba != sbb:
        return None                     # two different driver cells — not one pair
    sblk = project.block(sba)
    if sblk is None:
        return None
    params = getattr(sblk, "params", None)
    for i_port, q_port, i_in, q_in in ((spa, spb, pa, pb), (spb, spa, pb, pa)):
        if _iq_sibling(catalog, sblk.type, i_port, want_out=True,
                       params=params) == q_port:
            return sba, (i_in, q_in)
    return None


def _free_faces(project, cell, block, chip_types, chip_id):
    """``(usable, blocked)`` faces of ``cell``: ``usable`` is the list of face codes a
    corridor could still arrive on; ``blocked`` is ``{face_code: reason}``.

    A face is unusable when the neighbouring cell is OUT OF BOUNDS, is one of the
    block's OWN cells, or is occupied by ANOTHER block — in each case no corridor can
    ever deliver there, so no reroute at this anchor can fix a face collision."""
    ct = None
    if chip_types:
        chip = project.chip(chip_id) if hasattr(project, "chip") else None
        name = getattr(chip, "type_name", None) or getattr(
            project, "chip_type", None)
        ct = chip_types.get(name)
    occ: dict = {}
    for b in project.blocks:
        if b.placement is None:
            continue
        if getattr(b.placement, "chip", 0) != chip_id:
            continue
        for c in b.placement.cells:
            occ[(c.x, c.y)] = b.name

    usable: list = []
    blocked: dict = {}
    for f, (dx, dy) in _DIR_DELTA.items():
        n = (cell[0] + dx, cell[1] + dy)
        if ct is not None and not ct.in_bounds(n[0], n[1]):
            blocked[f] = "off-array"
            continue
        owner = occ.get(n)
        if owner == block:
            blocked[f] = "own cell"
        elif owner is not None:
            blocked[f] = f"occupied by '{owner}'"
        else:
            usable.append(f)
    return usable, blocked


def _check_dual_input_same_face(project, catalog, chip_types=None) -> list[Violation]:
    """DRC the DISTINCT-INPUT-FACE requirement of a face-locking rendezvous block.

    A block declaring ``NEEDS_DISTINCT_INPUT_FACES`` (the DualFloatToComplex LOCK
    rendezvous) distinguishes its two INDEPENDENT async input streams ONLY by their
    arrival FACE — it LOCKs to one face at a time. If the placer/router lands its two
    input nets on the SAME face of its cell, the face lock CANNOT tell the streams apart
    and the rendezvous stalls (it may "happen to run" only for a rigged in-order feed).
    So this is a HARD ERROR, the mirror of the single-cell in!=out DRC: read the two input
    nets' arrival faces from the routed geometry and ERROR (NAMED) when they coincide.

    The message is ACTIONABLE, not just diagnostic. Beyond naming the shared face it
    reports, computed from the design:

      * the two NET names and the source ports feeding them, and — when the pair is
        the I/Q rails of one complex output — that the two rails LEAVE THE SAME CELL
        and must FORK (a complex GNU Radio link imports as two nets, only one of which
        the user drew, so an identical route for both looks like ONE finished wire);
      * whether a REROUTE can still fix it at this anchor, or the block must MOVE:
        the target cell's four neighbours are classified (off-array / the block's own
        cell / occupied by another block), and when fewer faces remain usable than
        there are inputs to separate, the message says so and lists why.

    Requires ``catalog`` to read the block's flag (via instantiate); a no-op without it or
    for any block that does not declare the flag. ``chip_types`` (optional) enables the
    OFF-ARRAY neighbour classification; without it an out-of-bounds face is simply
    counted as free (the pre-existing, less specific behaviour)."""
    arrivals = face_lock_arrivals(project, catalog)
    if not arrivals:
        return []

    out: list[Violation] = []
    for name, rec in arrivals.items():
        if not rec.colliding:
            continue
        ports = sorted(rec.colliding)
        same = "/".join(sorted(DIR_NAMES.get(f, "?") for f in rec.collision_faces))
        nets = [rec.nets.get(p, p) for p in ports]
        blk = project.block(name)
        chip_id = getattr(getattr(blk, "placement", None), "chip", 0) or 0

        msg = [f"face-locking block '{name}' (NEEDS_DISTINCT_INPUT_FACES) has inputs "
               f"{', '.join(ports)} (nets {', '.join(nets)}) arriving on the SAME face "
               f"({same}) — its LOCK rendezvous distinguishes the async streams ONLY by "
               "arrival face, so same-face inputs cannot be paired."]

        pair = _iq_rail_pair(project, catalog, name, ports)
        if pair is not None:
            sblk, (i_in, q_in) = pair
            msg.append(
                f"These are the I/Q rails of ONE complex output of '{sblk}': a complex "
                f"link is TWO nets, so both rails LEAVE THE SAME CELL and an identical "
                f"route for each draws as one line. They must FORK — share the corridor "
                f"for the long haul, then diverge near '{name}' so {i_in} and {q_in} "
                "enter on different faces.")

        usable, blocked = _free_faces(project, rec.cell, name, chip_types, chip_id)
        need = len(rec.faces)
        if len(usable) < need:
            why = ", ".join(f"{DIR_NAMES[f]} {blocked[f]}" for f in sorted(blocked))
            msg.append(
                f"NO reroute can fix this at cell {rec.cell}: it needs {need} usable "
                f"faces but has {len(usable)} "
                f"({', '.join(DIR_NAMES[f] for f in sorted(usable)) or 'none'}) — "
                f"{why}. Re-anchor '{name}' where {need} faces are free.")
        else:
            free = ", ".join(DIR_NAMES[f] for f in sorted(usable))
            msg.append(
                f"Reroutable in place: cell {rec.cell} has {len(usable)} usable faces "
                f"({free}); bring one input in on a different one.")

        out.append(Violation(cell=rec.cell, kind="dual_input_same_face",
                             reason=" ".join(msg), nets=tuple(ports)))
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


# --------------------------------------------------------------------------- #
# Stray-emission DRC (P3.4): a WRITE/JUMP that lands on an EMPTY/unowned cell.
#
# Catches the class of bug where a block's emission fires into dead space — e.g. a
# dual-face output cell whose `out` FACE flip didn't follow the drawn route, so the
# word shoots into empty cells and stray-EXECUTES on the universal program ("phantom
# routes" that light red and forward to nothing). Reads the BUILT memory + resolved
# faces, models the per-cell FACE flips (MOVE [FACE], Rk), and follows each emission
# through the transit forwarding to its terminal cell; flags any that end UNOWNED.
# --------------------------------------------------------------------------- #

def _decode_ops(cells: dict) -> dict:
    """Disassemble each cell's memory and return, per (x, y), the address-ordered
    list of relevant ops: ``("face", src)`` for ``MOVE [FACE], R{src}``,
    ``("write", hop)`` / ``("jump", hop)`` for emissions. Uses the simkyt
    disassembler so the decode matches the simulator exactly (the raw word layout
    is NOT re-derived here). If simkyt is unavailable the result is empty (the check
    no-ops rather than guessing)."""
    import re
    try:
        from simkyt import Program
    except Exception:  # noqa: BLE001
        return {}
    face_re = re.compile(r"Move\s*\{\s*dest:\s*33\s*,\s*src:\s*(\d+)")
    write_re = re.compile(r"Write\s*\{[^}]*hop_cnt:\s*(\d+)")
    jump_re = re.compile(r"Jump\s*\{[^}]*hop_cnt:\s*(\d+)")
    out: dict = {}
    for (x, y), info in cells.items():
        mem = info.get("memory") or []
        if not any(w for w in mem):
            continue
        try:
            text = Program.from_words("c", [w & 0xFFFF for w in mem]).disassemble()
        except Exception:  # noqa: BLE001
            continue
        seq = []
        for line in text.splitlines():
            m = face_re.search(line)
            if m:
                seq.append(("face", int(m.group(1))))
                continue
            m = write_re.search(line)
            if m:
                seq.append(("write", int(m.group(1))))
                continue
            m = jump_re.search(line)
            if m:
                seq.append(("jump", int(m.group(1))))
        if seq:
            out[(x, y)] = seq
    return out


def check_stray_emissions(cells: dict, owned: set, width: int, height: int
                          ) -> list[Violation]:
    """Flag any WRITE/JUMP whose forwarding path TERMINATES on a cell not in
    ``owned`` (a block cell, transit cell, or route waypoint).

    ``cells`` maps (x, y) -> {"memory": [w0..w31], "face": <face>} from the build.
    ``owned`` is the set of (x, y) that legitimately carry traffic. The walk:
      * decode the cell's program in address order, tracking the current FACE
        (init = the cell's resting face; updated by ``MOVE [FACE], Rk`` whose value
        is the const at memory[k]);
      * for each WRITE(0x6xxx)/JUMP(0x7xxx), distance = 31 - hop; step ``distance``
        cells, the FIRST hop on the emitter's current FACE, each subsequent hop on
        the TRANSIT cell's own resting face (universal-program forwarding);
      * the terminal cell is where the word executes — if it is not ``owned`` (and
        on-grid), that is a stray emission.
    """
    viols: list[Violation] = []
    # Decode each cell's program via the simkyt disassembler (authoritative — the
    # raw bit layout is not re-derived here). We read, in address order:
    #   MOVE [FACE], Rk  -> "Move { dest: 33, src: k }"  (33 = the CONFIG FACE addr)
    #   WRITE @h         -> "Write { ..., hop_cnt: h, ... }"
    #   JUMP  @h         -> "Jump { hop_cnt: h, ... }"
    # tracking the runtime FACE (init = the cell's resting face; a face MOVE sets it
    # to the const at memory[k]) and the emission distance (31 - hop_cnt).
    ops = _decode_ops(cells)
    for (x, y), info in cells.items():
        mem = info.get("memory") or []
        if not any(w for w in mem):
            continue  # empty cell, nothing emits
        cur_face = _face_code_of(info.get("face"))
        for kind, arg in ops.get((x, y), []):
            if kind == "face":          # MOVE [FACE], R{arg}
                v = mem[arg] & 0xFFFF if 0 <= arg < len(mem) else None
                if v in (0, 1, 2, 3):
                    cur_face = v
                continue
            # kind in ("write","jump"); arg = hop_cnt
            if cur_face is None:
                continue
            dist = 31 - arg
            if dist <= 0:
                continue  # @31 = execute locally, no emission
            # First hop on the emitter's current face; then follow transit faces.
            # The word executes on the dist-th cell. A word that enters an UNOWNED
            # cell along the way is already astray: either it dies there (no
            # forwarding face) or the universal program ferries it to another dead
            # cell. Flag the FIRST unowned cell the path enters. Steps that leave the
            # grid are a separate (port/edge) case and are not flagged here.
            dx, dy = _FWD_DELTA[cur_face]
            cx, cy = x + dx, y + dy
            stray_at = None
            for hop in range(dist):
                if not (0 <= cx < width and 0 <= cy < height):
                    break  # off-grid — port/edge egress, not a dead-cell stray
                if (cx, cy) not in owned:
                    stray_at = (cx, cy)
                    break
                if hop == dist - 1:
                    break  # reached the (owned) terminal cleanly
                tf = _face_code_of((cells.get((cx, cy)) or {}).get("face"))
                if tf is None:
                    break  # owned but unforwardable here — not this check's concern
                tdx, tdy = _FWD_DELTA[tf]
                cx, cy = cx + tdx, cy + tdy
            if stray_at is not None:
                viols.append(Violation(
                    cell=stray_at, kind="stray_emission",
                    reason=(f"a WRITE/JUMP from cell ({x},{y}) reaches EMPTY/unowned "
                            f"cell {stray_at} — it will stray-execute on the universal "
                            f"forwarding program (data into dead space). The emitting "
                            f"cell's output FACE does not follow a route to an owned "
                            f"cell."),
                    nets=()))
    # De-dup by terminal cell (many emissions can converge on one dead cell).
    seen = set()
    uniq = []
    for v in viols:
        if v.cell in seen:
            continue
        seen.add(v.cell)
        uniq.append(v)
    return uniq


def owned_cells(project, chip_id: int = 0) -> set:
    """The set of (x, y) on ``chip_id`` that legitimately carry traffic: every block
    cell, every block transit cell, and every routed-connection waypoint. The
    complement (programmed-but-unowned) is what :func:`check_stray_emissions` flags."""
    owned: set = set()
    for b in project.blocks:
        pl = getattr(b, "placement", None)
        if pl is None or getattr(pl, "chip", 0) != chip_id:
            continue
        for c in pl.cells:
            owned.add((c.x, c.y))
        for t in (getattr(pl, "transit_cells", None) or []):
            owned.add((t.x, t.y))
    for conn in project.connections:
        for p in (conn.route or []):
            owned.add((p.x, p.y))
    return owned
