"""Auto-route — materialise logical nets into drawn waypoint routes (Phase 3).

This is the first cut of the auto-P&R router (AUTO_PNR_DESIGN §7). It takes the
UNROUTED logical nets (fly lines from the Phase-2 capture front-end) and produces
concrete waypoint routes — the same drawn paths a user could have laid by hand,
which the existing ``build.py`` then consumes (placeKYT builds STRICTLY from drawn
routes; the router fabricates a *path the user could have drawn*, never a hidden
route — the build-from-design invariant holds).

Scope of THIS cut — a sound spine/corridor router for placed blocks:
- A net's source is the producer block's OUTPUT cell (emitting on its face); its
  sink is the consumer block's INPUT (landing) cell. Both come from the PortMap.
- The route is a shortest grid path over FREE cells (BFS) from the cell just
  outside the source's emit face to the cell just outside the sink's arrival face,
  within the 31-hop budget. Block cells, transit cells, and other routes are
  obstacles.
- A net that cannot be routed (no free corridor / over budget / unplaced block /
  cross-chip) is reported by NAME — never silently dropped or fabricated. This is
  the "sound failure" requirement (§ P3.4): the router only claims success when it
  has a real path.

NOT yet in this cut (later P3 increments): the CP-SAT shared-bus/broker/tag/hop
co-optimisation, relays for >31-hop rings/multi-chip, and auto-orient. The BFS
corridor is the A*/greedy fast path the design calls for; the solver becomes the
authority on "unroutable" for the hard shared-bus cases.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Optional

from model.connection import AUTO_ROUTE, BlockEndpoint, ChipPortEndpoint
from model.enums import Face

# The primitive transforms an orientation search may apply (identity = no change).
_ORIENTS = (None, "cw", "ccw", "mirror_h", "mirror_v")


def suggest_flow_orientation(port_map, desired_out_face: Face):
    """Return the primitive transform (``cw``/``ccw``/``mirror_h``/``mirror_v`` or
    ``None``) that brings the block's primary OUTPUT port to face
    ``desired_out_face`` — the auto-orient core (AUTO_PNR_DESIGN §8 / flow-ordered
    §4.3: a block's output should face the downstream consumer).

    Searches the identity + 4 primitive orientations (covers the common single-
    transform cases) using ``PortMap.transformed`` and returns the first whose
    output port already faces ``desired_out_face``; ``None`` (identity) if the
    block has no output port or already faces the right way. Prefers identity, so
    a block that's already oriented is never needlessly transformed."""
    outs = port_map.outputs()
    if not outs:
        return None
    if any(p.face == desired_out_face for p in port_map.outputs()):
        return None  # already correct — don't transform
    for kind in _ORIENTS[1:]:
        try:
            t = port_map.transformed(kind)
        except Exception:  # noqa: BLE001
            continue
        if any(p.face == desired_out_face for p in t.outputs()):
            return kind
    return None


def _cardinal(src, dst):
    """The dominant cardinal Face from cell ``src`` to cell ``dst`` (the larger of
    the x/y deltas), or None if the cells coincide."""
    sx, sy = src
    dx, dy = dst
    ex, ey = dx - sx, dy - sy
    if ex == 0 and ey == 0:
        return None
    if abs(ex) >= abs(ey):
        return Face.EAST if ex > 0 else Face.WEST
    return Face.SOUTH if ey > 0 else Face.NORTH


# Unit step for each face (screen coords, x-right / y-down).
_FACE_STEP = {
    Face.NORTH: (0, -1),
    Face.SOUTH: (0, 1),
    Face.EAST: (1, 0),
    Face.WEST: (-1, 0),
}
_MAX_HOPS = 31


@dataclass
class RouteResult:
    """One net's routing outcome."""

    name: str
    ok: bool
    points: list[tuple[int, int]] | None = None   # waypoint path (if ok)
    reason: str | None = None                     # why it failed (if not ok)
    # Relay cells (x, y) inserted along ``points`` for a >31-hop route (§1.4): each
    # is a point where the word RE-LAUNCHES with a fresh 31-hop budget (the universal
    # routing-cell ``relay`` entry). Empty for the common ≤31-hop route.
    relays: list[tuple[int, int]] | None = None


@dataclass
class AutoRouteReport:
    """The whole-design auto-route outcome: per-net results + a tidy summary."""

    results: list[RouteResult]

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)

    @property
    def routed(self) -> list[RouteResult]:
        return [r for r in self.results if r.ok]

    @property
    def failed(self) -> list[RouteResult]:
        return [r for r in self.results if not r.ok]


class AutoRouter:
    """Routes a project's unrouted logical nets over free fabric cells.

    ``port_cell_provider(block_type, library) -> {port: (cell_id, direction)}`` —
    the PortMap accessor (same callback the canvas uses); injected so the router
    has no hard catalog dependency. ``port_map_provider(block_type, library) ->
    PortMap`` (optional) enables auto-orient (P3.2); without it ``orient_for_flow``
    is a no-op.
    """

    def __init__(self, project, chip_types: dict, port_cell_provider,
                 port_map_provider=None):
        self._project = project
        self._chip_types = chip_types
        self._ports = port_cell_provider
        self._port_maps = port_map_provider
        self._takes_params: dict = {}

    # -- provider adapter -----------------------------------------------------

    def _provider(self, fn, blk):
        """Call a ``(block_type, library[, params])`` port provider for a placed
        block, passing the block's PARAMS when the provider accepts a third
        argument. A multi-cell block whose footprint/output cell scales with its
        params (e.g. an N-tap FIR) is mis-resolved if the PortMap is built from
        the bare type (INV-6/egress): the default construction is single-cell, so
        the output port lands on cell 0 instead of the real last cell. Back-compat:
        older providers take only ``(block_type, library)`` — call them as before.
        """
        if fn is None:
            return None
        takes = self._takes_params.get(fn)
        if takes is None:
            takes = self._accepts_three(fn)
            self._takes_params[fn] = takes
        if takes:
            return fn(blk.type, blk.library, getattr(blk, "params", None))
        return fn(blk.type, blk.library)

    @staticmethod
    def _accepts_three(fn) -> bool:
        """True if ``fn`` can be called with three positional args."""
        import inspect
        try:
            params = inspect.signature(fn).parameters.values()
        except (ValueError, TypeError):
            return False
        positional = 0
        for p in params:
            if p.kind is p.VAR_POSITIONAL:
                return True
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD):
                positional += 1
        return positional >= 3

    # -- public ---------------------------------------------------------------

    def orient_for_flow(self) -> dict:
        """Suggest a flow-ordered orientation per placed block (auto-orient,
        P3.2): a block's OUTPUT should face the dominant downstream consumer so
        the corridor leaves toward the next stage. Returns ``{block_name:
        transform_kind}`` for the blocks that would benefit (omits blocks already
        oriented / single-orientation / no PortMap). Pure suggestion — the caller
        applies the transforms (undoably) before routing.

        The desired output face is the majority direction from each producer block
        to its consumers (the net's source→target vector), snapped to a cardinal.
        Requires ``port_map_provider``; returns ``{}`` without it."""
        if self._port_maps is None:
            return {}
        # Tally, per source block, the cardinal direction to each consumer.
        from collections import Counter
        want: dict[str, Counter] = {}
        for conn in self._project.connections:
            src, tgt = conn.source, conn.target
            if not isinstance(src, BlockEndpoint):
                continue
            sb = self._project.block(src.block)
            if sb is None or sb.placement is None or not sb.placement.cells:
                continue
            tcell = self._consumer_anchor(tgt)
            scell = self._block_out_anchor(sb)
            if tcell is None or scell is None:
                continue
            face = _cardinal(scell, tcell)
            if face is not None:
                want.setdefault(src.block, Counter())[face] += 1
        out: dict[str, str] = {}
        for block_name, counter in want.items():
            blk = self._project.block(block_name)
            if blk is None:
                continue
            # Do NOT re-orient a block whose INPUT cell is anchored on a chip
            # input port: the serpentine placer deliberately seats the lead
            # input-fed block's input cell ON the port (it is the pipeline start)
            # and does not reorient it. A flow-orient pass here would flip the
            # block (e.g. Costas mirror_h), sliding the input cell OFF the port —
            # breaking I/Q ingress and the input flyline. The placer's anchor wins.
            if self._input_on_chip_port(block_name):
                continue
            try:
                pm = self._provider(self._port_maps, blk)
            except Exception:  # noqa: BLE001
                continue
            desired = counter.most_common(1)[0][0]
            kind = suggest_flow_orientation(pm, desired)
            if kind is not None:
                out[block_name] = kind
        return out

    def route_all(self) -> AutoRouteReport:
        """Route every UNROUTED connection between two placed blocks on one chip.

        Routes are computed against a SHARED obstacle map updated as each net is
        placed, so two nets never claim the same corridor cell (this cut keeps
        nets on disjoint cells — the shared-bus multiplexing comes in a later
        increment). Returns a report; the caller applies the routes via a command
        so the operation is undoable.
        """
        results: list[RouteResult] = []
        # Per-chip obstacle set: block cells + transit cells + already-routed
        # waypoints + any explicitly-routed connection cells.
        occupied: dict[int, set] = {}
        for blk in self._project.blocks:
            pl = blk.placement
            if pl is None:
                continue
            occ = occupied.setdefault(pl.chip, set())
            occ.update((c.x, c.y) for c in pl.cells)
            occ.update((t.x, t.y) for t in pl.transit_cells)
        for conn in self._project.connections:
            if conn.is_routed:
                chip = self._chip_of(conn)
                if chip is not None:
                    occupied.setdefault(chip, set()).update(
                        (p.x, p.y) for p in conn.route)

        for conn in self._project.connections:
            if conn.is_routed:
                continue
            r = self._route_one(conn, occupied)
            results.append(r)
            if r.ok and r.points:
                chip = self._chip_of(conn)
                if chip is not None:
                    # Reserve this net's cells so later nets avoid them.
                    occupied.setdefault(chip, set()).update(r.points)
        return AutoRouteReport(results)

    # -- per-net --------------------------------------------------------------

    def _route_one(self, conn, occupied) -> RouteResult:
        name = conn.name
        src = self._endpoint_cell(conn.source, role="src")
        dst = self._endpoint_cell(conn.target, role="dst")
        if src is None:
            return RouteResult(name, False,
                               reason="source block unplaced or port unknown")
        if dst is None:
            return RouteResult(name, False,
                               reason="target block unplaced or port unknown")
        (schip, sx, sy, sface) = src
        (dchip, dx, dy, dface) = dst
        if schip != dchip:
            return RouteResult(name, False,
                               reason="cross-chip auto-route not supported yet")
        ct = self._chip_type(schip)
        if ct is None:
            return RouteResult(name, False, reason="no chip type")
        occ = occupied.get(schip, set())

        # A block OUTPUT emits on its face, so the corridor starts at the
        # face-neighbour. A chip INPUT port injects AT its own cell, so the
        # corridor starts at the port cell itself (no off-array first step).
        src_is_port = isinstance(conn.source, ChipPortEndpoint)
        path = self._bfs(ct, occ, (sx, sy), sface, (dx, dy),
                         start_at_cell=src_is_port)
        if path is None:
            return RouteResult(name, False,
                               reason="no free corridor between the ports")
        # Hop budget: distance = len(path)-1; an output-port target adds +1 (the
        # word must transit off the array). Block→block targets don't.
        distance = max(0, len(path) - 1)
        if isinstance(conn.target, ChipPortEndpoint) and \
                conn.target.port.endswith("_out"):
            distance += 1
        if distance > _MAX_HOPS:
            return RouteResult(name, False,
                               reason=f"route is {distance} hops (max {_MAX_HOPS})")
        return RouteResult(name, True, points=path)

    # -- geometry -------------------------------------------------------------

    def _endpoint_cell(self, ep, *, role: str):
        """(chip, x, y, face) of a connection endpoint's communicating cell.

        For a block endpoint: the PortMap port cell + that cell's face. For a chip
        port: the port's edge cell + its face. ``role`` is informational."""
        if isinstance(ep, ChipPortEndpoint):
            ct = self._chip_type(ep.chip)
            if ct is None:
                return None
            port = ct.port(ep.port)
            if port is None:
                return None
            return (ep.chip, port.cell_x, port.cell_y,
                    Face.from_str(port.face.value))
        if isinstance(ep, BlockEndpoint):
            blk = self._project.block(ep.block)
            if blk is None or blk.placement is None or not blk.placement.cells:
                return None
            try:
                pmap = self._provider(self._ports, blk) or {}
            except Exception:  # noqa: BLE001
                pmap = {}
            entry = pmap.get(ep.port)
            cell = None
            if entry is not None:
                cell = blk.placement.cell(entry[0])
            if cell is None:
                # Fall back to first (input) / last (output) placed cell.
                cell = (blk.placement.cells[0] if role == "dst"
                        else blk.placement.cells[-1])
            return (blk.placement.chip, cell.x, cell.y, cell.face)
        return None

    def _bfs(self, ct, occ, src, sface, dst, *, start_at_cell: bool = False):
        """Shortest free-cell path from the cell just outside ``src``'s emit face
        to ``dst``, returning the FULL waypoint list ``[src, …, dst]`` (inclusive)
        or None if no free corridor exists.

        The path starts at ``src`` (the producer's output cell) and ends at
        ``dst`` (the consumer's input cell). Intermediate cells must be free
        (not in ``occ``) and on-grid. The first step leaves ``src`` on its emit
        ``sface`` so the corridor abuts the producer correctly (§7.4)."""
        sx, sy = src
        dx, dy = dst
        if src == dst:
            return [src]

        def free(p):
            x, y = p
            return ct.in_bounds(x, y) and (p == dst or p not in occ)

        if start_at_cell:
            # A chip input port injects at its own cell — BFS starts there.
            first = src
        else:
            # A block output emits on its face: the first corridor cell is the
            # neighbour in the emit-face direction.
            step = _FACE_STEP.get(sface, (1, 0))
            first = (sx + step[0], sy + step[1])
            if not free(first) and first != dst:
                # The producer's emit face is blocked — can't even leave the block.
                return None
        prev = {first: src}
        q = deque([first])
        found = first == dst
        while q and not found:
            cur = q.popleft()
            for ddx, ddy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nxt = (cur[0] + ddx, cur[1] + ddy)
                if nxt in prev or nxt == src:
                    continue
                if not free(nxt):
                    continue
                prev[nxt] = cur
                if nxt == dst:
                    found = True
                    break
                q.append(nxt)
        if dst not in prev:
            return None
        # Reconstruct dst → … → first → src, then reverse.
        path = [dst]
        node = dst
        while node != src:
            node = prev[node]
            path.append(node)
        path.reverse()
        return path

    # -- helpers --------------------------------------------------------------

    def _block_out_anchor(self, blk):
        """(x, y) of a block's OUTPUT cell (PortMap output port → placed cell;
        falls back to the last placed cell)."""
        try:
            pmap = self._provider(self._ports, blk) or {}
        except Exception:  # noqa: BLE001
            pmap = {}
        for _name, (cell_id, direction) in pmap.items():
            if direction == "out":
                c = blk.placement.cell(cell_id)
                if c is not None:
                    return (c.x, c.y)
        last = blk.placement.cells[-1]
        return (last.x, last.y)

    def _input_on_chip_port(self, block_name) -> bool:
        """True if ``block_name``'s INPUT cell currently sits on a chip input
        port — i.e. the serpentine placer anchored it as the pipeline start. Such
        a block must NOT be reoriented (that would slide its input off the port).
        Checks every connection from a chip input port into this block."""
        blk = self._project.block(block_name)
        if blk is None or blk.placement is None or not blk.placement.cells:
            return False
        try:
            pmap = self._provider(self._ports, blk) or {}
        except Exception:  # noqa: BLE001
            pmap = {}
        in_cells = {
            (c.x, c.y)
            for name, (cell_id, direction) in pmap.items()
            if direction == "in"
            for c in (blk.placement.cell(cell_id),) if c is not None}
        if not in_cells:
            in_cells = {(blk.placement.cells[0].x, blk.placement.cells[0].y)}
        for conn in self._project.connections:
            s, t = conn.source, conn.target
            if isinstance(s, ChipPortEndpoint) and isinstance(t, BlockEndpoint) \
                    and t.block == block_name:
                ct = self._chip_type(s.chip)
                port = ct.port(s.port) if ct else None
                if port is not None and (port.cell_x, port.cell_y) in in_cells:
                    return True
        return False

    def _consumer_anchor(self, ep):
        """(x, y) of a connection target's cell — a block's input cell or a chip
        port's cell — for flow-direction tallying."""
        if isinstance(ep, ChipPortEndpoint):
            ct = self._chip_type(ep.chip)
            port = ct.port(ep.port) if ct else None
            return (port.cell_x, port.cell_y) if port else None
        if isinstance(ep, BlockEndpoint):
            blk = self._project.block(ep.block)
            if blk is None or blk.placement is None or not blk.placement.cells:
                return None
            try:
                pmap = self._provider(self._ports, blk) or {}
            except Exception:  # noqa: BLE001
                pmap = {}
            entry = pmap.get(ep.port)
            cell = (blk.placement.cell(entry[0]) if entry is not None else None)
            cell = cell or blk.placement.cells[0]
            return (cell.x, cell.y)
        return None

    def _chip_of(self, conn) -> Optional[int]:
        for ep in (conn.source, conn.target):
            if isinstance(ep, BlockEndpoint):
                blk = self._project.block(ep.block)
                if blk is not None and blk.placement is not None:
                    return blk.placement.chip
            if isinstance(ep, ChipPortEndpoint):
                return ep.chip
        return None

    def _chip_type(self, chip_id: int):
        chip = self._project.chip(chip_id)
        if chip is None:
            return None
        name = getattr(chip, "type_name", None) or self._project.chip_type
        return self._chip_types.get(name)
