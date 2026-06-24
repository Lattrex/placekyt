"""Auto-place — flow-ordered SERPENTINE multi-row packing of blocks (auto-P&R §8).

The design's placer (AUTO_PNR_DESIGN §8): because there is no fan-in placement
sensitivity (Q2 — ordering is enforced inside blocks), the placer orders blocks by
**signal flow along a directional bus**. With the logical netlist this is a
**topological sort of the inter-block graph**, then a footprint-aware **serpentine
pack**: lay blocks left-to-right in the first row-band; when the next block would
overflow the array width, **wrap to the next band and reverse direction** so the bus
naturally snakes (a boustrophedon). Each band reserves the tallest block's height
plus a routing margin. This fits wide pipelines (e.g. the 18-cell coherent RX) onto
a 10-wide array — the 1-row pack could not — and the snake is exactly the path the
bus/broker router (§1.2) then threads, with blocks tapping off it.

Output (`PlacePlan`):
- ``positions``  : block_name -> (chip, x, y) anchor (min corner) proposals.
- ``order``      : flow (topological) order.
- ``orientations``: block_name -> D4 transform kind (or None) so the block's
  bus-facing edge meets the band it sits on (auto-orient, §8) — applied by the
  caller before the translate.
- ``spine``      : the ordered list of bus-cell (x, y) waypoints the snake threads
  alongside the blocks (one band-row per band, joined by the wrap columns) — the
  bus/broker router (Stage 3) constructs the actual bus on these cells.
- ``backward_edges``: (src, dst) ring-forcing edges (a later stage feeds an
  earlier one) — NAMED, not silently mis-ordered (the design promotes such a
  netlist to a ring; the placer still lays a best-effort order).

Geometry only — it does not route (Route All does). The caller repositions +
re-orients the placed blocks undoably.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from model.connection import BlockEndpoint, ChipPortEndpoint


@dataclass
class PlacePlan:
    """A flow-ordered serpentine placement proposal."""

    positions: dict          # block_name -> (chip, x, y)
    order: list              # block names in flow (topological) order
    orientations: dict = field(default_factory=dict)  # block_name -> kind|None
    spine: list = field(default_factory=list)         # [(x, y), ...] bus waypoints
    backward_edges: list = field(default_factory=list)  # (src, dst) ring-forcing

    @property
    def ok(self) -> bool:
        return not self.backward_edges


class AutoPlacer:
    """Flow-orders a project's placed blocks into a serpentine multi-row pipeline.

    ``footprint_provider(block_type, library) -> (w, h)`` gives a block's cell
    footprint (max_dx, max_dy from the PortMap), so the packer can space blocks and
    reserve band height; injected to avoid a hard catalog dependency. ``width`` /
    ``height`` are the array bounds (the serpentine wraps within them). ``anchor``
    (x, y) is where the first band starts — the caller passes the chip INPUT port's
    cell so the lead block's landing cell lands ON the port (builds ≠ computes).
    """

    def __init__(self, project, footprint_provider, *, row: int = 0, gap: int = 1,
                 anchor=None, width: int = 10, height: int = 12,
                 band_margin: int = 1):
        self._project = project
        self._fp = footprint_provider
        if anchor is not None:
            self._start_x, self._row = int(anchor[0]), int(anchor[1])
        else:
            self._start_x, self._row = 0, row
        self._gap = gap              # free columns between blocks (corridor room)
        self._width = int(width)
        self._height = int(height)
        self._band_margin = int(band_margin)  # extra rows below a band for the bus
        self._takes_params: dict = {}

    # -- provider adapter -----------------------------------------------------
    def _provider(self, fn, blk):
        """Call a ``(block_type, library[, params])`` provider for a placed block,
        passing the block's PARAMS when the provider accepts a third argument. A
        block whose FOOTPRINT scales with params (e.g. an N-tap FIR: cells =
        ceil(taps/5)) is mis-sized if resolved from the bare type — the default
        construction is single-cell, so the packer reserves ONE cell for a block
        that builds many, and the extra cells spill over / off-array. Back-compat:
        older 2-arg providers are called as before."""
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

    # -- footprint helper -----------------------------------------------------
    def _wh(self, blk):
        """Block cell extent (w, h) = footprint+1 (footprint is max offset)."""
        try:
            fw, fh = self._provider(self._fp, blk)
        except Exception:  # noqa: BLE001
            fw, fh = 0, 0
        return int(fw) + 1, int(fh) + 1

    def _oriented_wh(self, blk, kind):
        """Block cell extent (w, h) AFTER applying orientation ``kind`` — the extent
        of the cells the caller will ACTUALLY place. The serpentine pack must reserve
        space for the ORIENTED block, not the as-authored one: a 90° rotate (cw/ccw)
        swaps w/h, so a block planned as wide-and-short but placed tall-and-narrow
        (e.g. Gardner ccw) would otherwise desync the positions from the spine and
        strand the next block on a far band (the net4 gardner→slicer gap). Mirrors
        don't change the extent. Returns the un-oriented extent when ``kind`` is None.
        """
        w, h = self._wh(blk)
        if kind in ("cw", "ccw"):
            return h, w
        return w, h

    # -- flow ordering (unchanged topological sort) ---------------------------
    def _flow_order(self, blocks, names):
        succ: dict[str, set] = {b.name: set() for b in blocks}
        indeg: dict[str, int] = {b.name: 0 for b in blocks}
        edges = []
        for conn in self._project.connections:
            s, t = conn.source, conn.target
            if isinstance(s, BlockEndpoint) and isinstance(t, BlockEndpoint) \
                    and s.block in names and t.block in names \
                    and t.block not in succ[s.block]:
                succ[s.block].add(t.block)
                indeg[t.block] += 1
                edges.append((s.block, t.block))

        input_fed = set()
        for conn in self._project.connections:
            if isinstance(conn.source, ChipPortEndpoint) \
                    and isinstance(conn.target, BlockEndpoint) \
                    and conn.target.block in names:
                input_fed.add(conn.target.block)
        cur_x = {b.name: b.placement.cells[0].x for b in blocks}

        def rank(n):
            return (0 if n in input_fed else 1, cur_x.get(n, 0), n)

        ready = sorted((n for n in names if indeg[n] == 0), key=rank)
        order: list = []
        indeg_work = dict(indeg)
        while ready:
            n = ready.pop(0)
            order.append(n)
            for m in sorted(succ[n], key=rank):
                indeg_work[m] -= 1
                if indeg_work[m] == 0:
                    ready.append(m)
            ready.sort(key=rank)
        leftover = [n for n in names if n not in order]
        if leftover:
            order.extend(sorted(leftover, key=rank))
        backward = [(s, t) for (s, t) in edges
                    if order.index(s) >= order.index(t)] if order else []
        return order, backward

    def _lead_input_fed(self, order, names):
        """First block in flow order fed by a chip INPUT port (the pipeline lead),
        or None. It anchors its input cell on the port and is not reoriented."""
        fed = set()
        for conn in self._project.connections:
            s, t = conn.source, conn.target
            if isinstance(s, ChipPortEndpoint) and isinstance(t, BlockEndpoint) \
                    and t.block in names:
                fed.add(t.block)
        for n in order:
            if n in fed:
                return n
        return None

    # -- the serpentine pack --------------------------------------------------
    def plan(self, chip: int = 0) -> PlacePlan:
        """Compute a flow-ordered serpentine placement for the blocks on ``chip``.

        Blocks are laid in flow order along horizontal bands; each band fills to
        the array width then wraps DOWN to the next band, reversing direction so
        the bus snakes. Returns anchor positions, the flow order, per-block
        orientation hints, the bus-spine waypoints, and any backward edges.
        """
        blocks = [b for b in self._project.blocks
                  if b.placement is not None and b.placement.chip == chip
                  and b.placement.cells]
        names = {b.name for b in blocks}
        order, backward = self._flow_order(blocks, names)
        blk_of = {b.name: b for b in blocks}
        # The lead input-fed block (first in flow order fed by a chip input port):
        # its input cell anchors on the port and it is NOT reoriented.
        self._lead_block = self._lead_input_fed(order, names)

        positions: dict = {}
        orientations: dict = {}
        spine: list = []

        x = self._start_x
        band_top = self._row
        going_right = True
        band_h = 0  # tallest block extent in the current band

        for n in order:
            blk = blk_of[n]
            # Decide the orientation FIRST so the pack reserves space for the cells
            # actually placed. Don't reorient the LEAD input-fed block: its input
            # cell must sit on the port at the array's edge and its body must extend
            # INTO the array. Orienting it toward the bus (WEST/etc.) would push
            # cells off-grid.
            if n == self._lead_block:
                kind = None
            else:
                kind = self._orient_for(blk, going_right)
            orientations[n] = kind
            # Use the ORIENTED extent (a cw/ccw rotate swaps w/h) so positions match
            # the cells the caller places — keeping the spine and the next block
            # adjacent to this block's output (no far-band stranding).
            w, h = self._oriented_wh(blk, kind)
            # Would this block overflow the band horizontally? If so, wrap to the
            # next band and reverse direction (serpentine). The lead block never
            # wraps (it must land on the anchor/input port).
            if going_right:
                overflow = (x + w) > self._width and positions
            else:
                overflow = (x - w + 1) < 0 and positions
            if overflow:
                band_top = band_top + band_h + self._band_margin
                going_right = not going_right
                band_h = 0
                x = (self._width - 1) if not going_right else 0
                # The travel direction flipped after we chose the orientation; re-
                # derive it for the new direction (and the oriented extent with it)
                # so the block faces the bus the right way on the new band.
                if n != self._lead_block:
                    kind = self._orient_for(blk, going_right)
                    orientations[n] = kind
                    w, h = self._oriented_wh(blk, kind)
                # mark the wrap column on the spine (the snake turns here)
                spine.append((max(0, min(self._width - 1, x)), band_top))

            # Place the block's anchor (min corner). When travelling LEFT the
            # block still occupies [x-w+1 .. x]; anchor at the min corner.
            ax = x if going_right else max(0, x - w + 1)
            positions[n] = (chip, ax, band_top)
            band_h = max(band_h, h)

            # Bus waypoint: the cell just outside the block on the travel side,
            # at the band row, so the spine threads block-to-block.
            bus_y = band_top
            if going_right:
                spine.append((min(self._width - 1, ax + w), bus_y))
                x = ax + w + 1 + self._gap
            else:
                spine.append((max(0, ax - 1), bus_y))
                x = ax - 1 - self._gap

        return PlacePlan(positions=positions, order=order,
                         orientations=orientations, spine=spine,
                         backward_edges=backward)

    # -- orientation (auto-orient toward the bus, §8) -------------------------
    def _orient_for(self, blk, going_right):
        """Suggest a D4 transform so the block's OUTPUT faces the travel direction
        along the band (EAST when going right, WEST when going left), i.e. toward
        the next block / the bus. Returns a transform kind ('cw'/'ccw'/'mirror_h'/
        'mirror_v') or None (already aligned). Uses the PortMap if available; falls
        back to None (no reorientation) so a block without port geometry is left
        as authored.
        """
        from model.enums import Face
        pm = getattr(self, "_port_map_provider", None)
        if pm is None:
            return None
        try:
            port_map = self._provider(pm, blk)
        except Exception:  # noqa: BLE001
            return None
        from engine.autoroute import suggest_flow_orientation
        want = Face.EAST if going_right else Face.WEST
        return suggest_flow_orientation(port_map, want)

    def with_port_maps(self, provider):
        """Inject a ``port_map(block_type, library) -> PortMap`` provider so the
        packer can auto-orient blocks toward the bus. Returns self (chainable)."""
        self._port_map_provider = provider
        return self
