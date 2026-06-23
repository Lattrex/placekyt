"""Route-bus analysis: which logical connections traverse each physical cell.

The fabric bus is TIME-MULTIPLEXED (the auto-P&R design notes §1.2): several logical
:class:`~model.connection.Connection` s can share the SAME physical routing
cells (the bus runs through transit lanes, blocks tap it via brokers). So a
routing cell is "shared" when more than one connection's waypoint route covers
it.

This module is the SINGLE source of truth the GUI uses to:
  * highlight the whole physical bus path of the connection(s) through a
    selected I/O cell (route highlight, #266),
  * decide whether deleting a route may remove its routing cells or must keep
    them (smart delete, #267) — a cell may be removed only when NO OTHER
    connection's route covers it,
  * map an in-I/O-cell route segment back to the one connection it belongs to
    (grab-route, #268).

It is pure (no Qt, no project mutation): it reads ``project.connections`` and
returns plain coordinate/name data the canvas/commands render or act on.
"""

from __future__ import annotations

from collections import defaultdict

from model.connection import BlockEndpoint, ChipPortEndpoint


def route_chip_of(project, conn) -> int:
    """The chip a connection's coordinates live on (the source's chip, §2.1)."""
    src = conn.source
    if isinstance(src, ChipPortEndpoint):
        return src.chip
    if isinstance(src, BlockEndpoint):
        blk = project.block(src.block)
        if blk is not None and blk.placement is not None:
            return blk.placement.chip
    return 0


def cell_coverage(project, chip_id: int) -> dict[tuple[int, int], set[str]]:
    """``{(x, y): {connection_name, …}}`` for every routed connection on ``chip_id``.

    Each routed connection contributes ALL of its waypoint cells — including the
    source-output and target-input block cells at the two ends (those ARE the
    block I/O cells the route runs into). A cell shared by >1 name is a
    multiplexed bus cell.
    """
    cov: dict[tuple[int, int], set[str]] = defaultdict(set)
    for conn in project.connections:
        if not conn.is_routed:
            continue
        if route_chip_of(project, conn) != chip_id:
            continue
        for rp in conn.route:
            cov[(rp.x, rp.y)].add(conn.name)
    return dict(cov)


def connections_through_cell(project, chip_id: int, x: int, y: int) -> list[str]:
    """Names of the routed connections whose path covers cell ``(x, y)`` on
    ``chip_id`` (in project order). Empty when no route touches the cell."""
    names: list[str] = []
    for conn in project.connections:
        if not conn.is_routed:
            continue
        if route_chip_of(project, conn) != chip_id:
            continue
        if any(rp.x == x and rp.y == y for rp in conn.route):
            names.append(conn.name)
    return names


def connections_terminating_at_cell(
        project, chip_id: int, x: int, y: int) -> list[str]:
    """Names of routed connections whose route ENDS (source-output or
    target-input endpoint) at cell ``(x, y)`` on ``chip_id``.

    These are the connections an I/O cell selection should highlight first — the
    ones that physically originate from / terminate at this block I/O cell (as
    opposed to merely transiting it)."""
    names: list[str] = []
    for conn in project.connections:
        if not conn.is_routed:
            continue
        if route_chip_of(project, conn) != chip_id:
            continue
        ends = (conn.route[0], conn.route[-1])
        if any(rp.x == x and rp.y == y for rp in ends):
            names.append(conn.name)
    return names


def exclusive_route_cells(project, conn) -> list[tuple[int, int]]:
    """The cells of ``conn``'s route that NO OTHER routed connection covers.

    Used by smart delete (#267): these are the transit cells safe to remove when
    the connection is deleted; cells also covered by another connection are
    multiplexed and must stay. Endpoints (block I/O cells) are NOT included —
    they belong to placed blocks, not the route — only intermediate transit
    cells are returned."""
    if not conn.is_routed:
        return []
    chip_id = route_chip_of(project, conn)
    cov = cell_coverage(project, chip_id)
    # Block cells on this chip — never report a block's own cell as a removable
    # transit cell (the route's endpoints sit on block I/O cells).
    block_cells: set[tuple[int, int]] = set()
    for b in project.blocks:
        pl = b.placement
        if pl is None or pl.chip != chip_id:
            continue
        block_cells.update((c.x, c.y) for c in pl.cells)
        block_cells.update((t.x, t.y) for t in pl.transit_cells)
    out: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for rp in conn.route:
        key = (rp.x, rp.y)
        if key in seen or key in block_cells:
            continue
        seen.add(key)
        if cov.get(key, set()) <= {conn.name}:
            out.append(key)
    return out


def is_bus_shared(project, conn) -> bool:
    """True when ANY cell of ``conn``'s route is also covered by another routed
    connection (a multiplexed bus). Drives the smart-delete branch (#267)."""
    if not conn.is_routed:
        return False
    chip_id = route_chip_of(project, conn)
    cov = cell_coverage(project, chip_id)
    for rp in conn.route:
        if cov.get((rp.x, rp.y), set()) - {conn.name}:
            return True
    return False
