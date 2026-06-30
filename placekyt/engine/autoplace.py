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
        # Strategy hint: force MULTI-FILAMENT placement (no block on the port, one
        # region per filament) even for a single filament — set by the caller when the
        # route mode is BUS (``use_bus``), which also requires the port stay a free
        # tap. >1 filament always triggers multi regardless of this flag.
        self._multi_filament = False
        # Strategy hint: COMPACT pack — lay ALL blocks (every filament) tightly in global
        # flow order, off the port, filling from the input-port corner and abutting where
        # possible, leaving ``channel_reserve`` free rows between block rows so the router
        # has routing channels. Topology-AGNOSTIC (doc/ROUTING_TOPOLOGIES.md): the router
        # (block-to-block / bus v2 / ring) realizes the chosen topology over this layout,
        # and the place<->route loop (engine/pnr.py) bumps ``channel_reserve`` / perturbs
        # on a routing failure. Replaces the old serpentine "bus-snake" pre-bake (deleted).
        self._compact = False
        # Free rows the compact pack leaves below each block row (routing channels). The
        # P&R loop raises this to open channels when routing congests; 1 is the tight
        # default (the modem's 8 blocks fit on a 10x12 with room to route at margin 1).
        self._channel_reserve = 1

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

    # -- filament detection (strategy-aware placement) ------------------------
    def _port_fed_blocks(self, names):
        """The set of blocks fed DIRECTLY by a chip INPUT port — each is the head of
        a distinct FILAMENT (a maximal forward chain from the port)."""
        fed = set()
        for conn in self._project.connections:
            s, t = conn.source, conn.target
            if isinstance(s, ChipPortEndpoint) and isinstance(t, BlockEndpoint) \
                    and t.block in names:
                fed.add(t.block)
        return fed

    def _filaments(self, order, names):
        """Partition the flow ``order`` into FILAMENTS — one per distinct chip-input
        head, each being that head and the maximal forward chain reachable from it.

        A "filament" is a maximal forward chain (the design's term). Two filaments
        fed from the SAME shared input port (the full-duplex modem's x16_in feeding
        BOTH the TX mapper and the RX matched filter) are SEPARATE filaments — they
        diverge at the port and only re-converge at the output port (not a block), so
        the block graph splits cleanly into one component per head.

        Returns ``[[block, ...], ...]`` in flow order, one list per filament head (in
        the order the heads appear in ``order``). Blocks reachable from no input head
        (rare: an unconnected island) form a trailing catch-all filament so nothing is
        dropped. The partition is by forward reachability: a block belongs to the head
        whose forward cone first reaches it (heads taken in flow order), so a chain
        that forks is assigned greedily but every block lands in exactly one filament.
        """
        succ: dict[str, set] = {n: set() for n in names}
        for conn in self._project.connections:
            s, t = conn.source, conn.target
            if isinstance(s, BlockEndpoint) and isinstance(t, BlockEndpoint) \
                    and s.block in names and t.block in names:
                succ[s.block].add(t.block)
        heads = [n for n in order if n in self._port_fed_blocks(names)]
        assigned: dict[str, int] = {}
        filaments: list[list] = []
        for fi, head in enumerate(heads):
            # Forward-reachable cone from this head, claiming any not-yet-assigned
            # block (a block already claimed by an earlier head's cone stays there).
            stack = [head]
            members: list = []
            while stack:
                n = stack.pop()
                if n in assigned:
                    continue
                assigned[n] = fi
                members.append(n)
                stack.extend(succ.get(n, ()))
            # Keep members in flow order for a coherent left-to-right run.
            members.sort(key=order.index)
            filaments.append(members)
        # Anything unreachable from any input head (islands) -> a trailing filament.
        leftover = [n for n in order if n not in assigned]
        if leftover:
            filaments.append(leftover)
        return [f for f in filaments if f]

    # -- neighbour graph (for flyline-minimising orientation) -----------------
    def _neighbour_maps(self, names):
        """Build, per block, its inter-block DRIVER and the chip ports it touches —
        the data the flyline-minimising orienter needs to know which way the input
        and output should point.

        Returns ``(driver_of, in_port_of, out_port_of)``:
        - ``driver_of[block]`` = the upstream BLOCK feeding this block's input (the
          first such driver; that block's OUTPUT cell is where this block's INPUT
          should sit nearest). None if fed only by a chip port / unconnected.
        - ``in_port_of[block]`` = (cell_x, cell_y) of the chip INPUT port feeding
          this block, if any (the lead block's driver is the port).
        - ``out_port_of[block]`` = (cell_x, cell_y) of the chip OUTPUT port this
          block feeds, if any (this block's OUTPUT should sit nearest it).
        """
        driver_of: dict = {}
        in_port_of: dict = {}
        out_port_of: dict = {}
        for conn in self._project.connections:
            s, t = conn.source, conn.target
            if isinstance(t, BlockEndpoint) and t.block in names \
                    and isinstance(s, BlockEndpoint) and s.block in names:
                driver_of.setdefault(t.block, s.block)
            if isinstance(t, BlockEndpoint) and t.block in names \
                    and isinstance(s, ChipPortEndpoint):
                pc = self._chip_port_cell(s)
                if pc is not None:
                    in_port_of.setdefault(t.block, pc)
            if isinstance(s, BlockEndpoint) and s.block in names \
                    and isinstance(t, ChipPortEndpoint):
                pc = self._chip_port_cell(t)
                if pc is not None:
                    out_port_of.setdefault(s.block, pc)
        return driver_of, in_port_of, out_port_of

    def _chip_port_cell(self, ep: ChipPortEndpoint):
        """(cell_x, cell_y) of a chip port, via an injected resolver if present.

        The placer has no hard chip-type dependency; the controller injects
        ``with_chip_ports(resolver)`` so the orienter can score flyline to the
        actual chip I/O port cells. Returns None when no resolver is available."""
        resolver = getattr(self, "_chip_port_resolver", None)
        if resolver is None:
            return None
        try:
            return resolver(ep.chip, ep.port)
        except Exception:  # noqa: BLE001
            return None

    # -- the serpentine pack --------------------------------------------------
    def plan(self, chip: int = 0) -> PlacePlan:
        """Compute a flow-ordered serpentine placement for the blocks on ``chip``.

        Blocks are laid in flow order along horizontal bands; each band fills to
        the array width then wraps DOWN to the next band, reversing direction so
        the bus snakes. Returns anchor positions, the flow order, per-block
        orientation hints, the bus-spine waypoints, and any backward edges.

        STRATEGY-AWARE (the project-owner's context rule): the placer chooses
        between two strategies based on how many FILAMENTS feed the chip input port
        and the route mode (``multi_filament`` flag set by the caller from the bus
        mode):

        * **Single filament + block-to-block routing** — the proven coherent-RX
          path: the lead input-fed block ANCHORS its input cell ON the port (lowest
          latency; the port injects at its own cell). One serpentine run.
        * **Multiple filaments from a shared port, OR bus routing** — the modem
          path: NO block may sit on the port (it must stay a FREE BUS TAP so the bus
          can reach each filament's input). Each filament is packed as its OWN
          coherent serpentine run in its own band-region (stacked vertically), so
          the filaments are visually separable, and every filament head sits OFF the
          port with a clear bus corridor from the port to it.
        """
        blocks = [b for b in self._project.blocks
                  if b.placement is not None and b.placement.chip == chip
                  and b.placement.cells]
        names = {b.name for b in blocks}
        order, backward = self._flow_order(blocks, names)
        self._blk_of = {b.name: b for b in blocks}
        self._flow_order_index = {n: i for i, n in enumerate(order)}
        # Who drives whom, and which chip ports each block touches — so orientation
        # can put the INPUT cell nearest its driver and the OUTPUT cell nearest its
        # consumer (flyline minimisation, §8 / §4.3).
        self._driver_of, self._in_port_of, self._out_port_of = \
            self._neighbour_maps(names)
        # The chip OUTPUT-port cells terminal blocks egress to MUST stay free — a block
        # body covering one leaves the bus no port cell to exit through (the §1.2 "no
        # bus path to the broker" egress failure).
        self._reserved_out = {c for c in self._out_port_of.values() if c is not None}
        # A chip OUTPUT-port cell at the array's RIGHTMOST column must stay free so the
        # egress can reach it. Reserve, PER ROW, the columns a block may not extend onto:
        # only the rows that actually hold an output port at the last column get their
        # last column withheld (``_east_bound_row[row] = port_col``). Other rows keep the
        # full width, so the proven single-filament serpentine (whose mid-block bands DO
        # use the last column, e.g. the coherent-RX Costas at column 9 row 3) is
        # unaffected — only a block on the port's OWN row is kept off the port cell's
        # column. This fixes the folded-terminal-covers-the-port-cell egress failure
        # without perturbing layouts that never touch the port row's last column.
        self._east_bound_row: dict = {}
        for c in self._reserved_out:
            if c[0] == self._width - 1:
                self._east_bound_row[c[1]] = min(
                    self._east_bound_row.get(c[1], self._width), c[0])

        # Strategy selection. Multi-filament when >1 distinct chain feeds the port,
        # OR when the caller forces bus mode (``multi_filament``): both demand the
        # port stay a free bus tap (no lead-on-port).
        filaments = self._filaments(order, names)
        multi = self._multi_filament or len(filaments) > 1

        positions: dict = {}
        orientations: dict = {}
        # Where each placed block's OUTPUT cell physically landed, so the NEXT block
        # in flow order can score its input flyline against an already-placed driver.
        out_pos: dict = {}
        spine: list = []

        # Whether the per-block packer is running inside the MULTI-filament layout
        # (controls the egress-column reservation: in multi mode EVERY multi-cell block
        # — folds included — keeps the egress corridor clear; in single mode a feedback
        # fold legitimately uses the last column). Set per-run below.
        self._in_multi_pack = False
        if not multi:
            # SINGLE-FILAMENT, block-to-block: the lead input-fed block anchors its
            # input cell ON the port (preferred — lowest latency). One serpentine run.
            # (Byte-identical to the proven coherent-RX path — unchanged.)
            self._lead_block = self._lead_input_fed(order, names)
            self._pack_run(order, positions, orientations, out_pos, spine, chip,
                           band_top=self._row)
        else:
            # MULTI-FILAMENT / bus: COMPACT pack — lay EVERY block, all filaments, tightly
            # in global flow order, OFF the port (the port stays a free bus tap), filling
            # from the input-port corner and abutting where possible, leaving
            # ``channel_reserve`` free rows between block rows so the router has routing
            # channels. Topology-AGNOSTIC: the router (block-to-block / bus v2 / ring)
            # realizes the chosen topology over this layout, and the place<->route loop
            # (engine/pnr.py) bumps channel_reserve / perturbs on a routing failure. The
            # per-filament flow order is preserved by the global topological ``order``.
            self._lead_block = None          # nothing anchors on the port
            self._in_multi_pack = True       # keep the egress corridor clear for folds
            self._pack_compact(order, positions, orientations, out_pos, spine, chip)

        return PlacePlan(positions=positions, order=order,
                         orientations=orientations, spine=spine,
                         backward_edges=backward)

    # -- compact pack (topology-agnostic; the iterative-P&R placer) ------------
    def _pack_compact(self, order, positions, orientations, out_pos, spine, chip):
        """Pack ALL blocks tightly in global flow ``order``, OFF the port, leaving
        ``self._channel_reserve`` free rows between block rows as routing channels.

        Topology-agnostic (doc/ROUTING_TOPOLOGIES.md): fill row-by-row from the input-
        port corner; place each block at the current row cursor (abutting the previous
        block in the row), wrap to the next row + the channel reserve when a block would
        overflow the array width. The chip input/output ports sit on the top row (row 0
        of this array); we keep the WHOLE top row free as the shared ingress/egress lane
        and start the first block row at ``self._row + 1``, so both ports stay reachable
        and the router has the port row to weave the bus in/out. Orientation comes from
        the flyline-minimising orienter; a folded block gets the wall inset so its bus-
        facing edge keeps a free neighbour for its broker tap. The ``spine`` is a loose
        dataflow-ordered hint (the router's threader realises the real backbone).

        The place<->route loop perturbs by raising ``self._channel_reserve`` (open more
        channels) or, per-block, nudging a single-cell block to a bend — all monotone in
        looseness, so a looser pack always exists (worst case = sparse, always routable).
        """
        blk_of = self._blk_of
        reserve = max(0, int(self._channel_reserve))
        left = max(0, self._start_x)        # first block column (off the port column)
        # Start one row below the port row so the whole port row stays a free I/O lane.
        row_top = self._row + 1
        x = left
        row_h = 0

        for n in order:
            blk = blk_of[n]
            kind = self._orient_for(blk, True, n, out_pos, x, row_top)
            w, h = self._oriented_wh(blk, kind)
            # Wrap to a new row + channel reserve when this block would overflow width.
            if positions and (x + w) > self._width:
                row_top = row_top + row_h + reserve
                x = left
                row_h = 0
                # Re-orient for the fresh row position (the orienter scores against the
                # driver's now-known output cell).
                kind = self._orient_for(blk, True, n, out_pos, x, row_top)
                w, h = self._oriented_wh(blk, kind)
            ax, aty = x, row_top
            # Keep a folded block's bus-facing edge off the array wall (free neighbour
            # for its broker tap) — reuse the proven inset; no-op for unwalled placements.
            if self._driver_of.get(n) is not None:
                ax, aty = self._wall_inset(blk, kind, ax, aty, w, h)
            orientations[n] = kind
            positions[n] = (chip, ax, aty)
            row_h = max(row_h, (aty - row_top) + h)
            # Record the block's OUTPUT cell so the next block scores its flyline + the
            # router knows where to tap from.
            op = self._io_offsets(blk, kind)
            if op is not None and op[1] is not None:
                out_pos[n] = (ax + op[1][0], aty + op[1][1])
            # Loose dataflow spine hint: the free cell just east of the block on its row.
            sx = min(self._width - 1, ax + w)
            spine.append((sx, row_top))
            x = ax + w + self._gap          # next block abuts after a small gap

    # -- multi-filament packing (shared-port, off-port heads) -----------------
    def _pack_filaments(self, filaments, positions, orientations, out_pos, spine,
                        chip):
        """Lay several filaments that share ONE chip input port, keeping the port a
        FREE bus tap (no block on it). Because the port is one cell with one fwd_face
        (§1.3) it fans out in a SINGLE direction, so every filament's input cell must
        be reachable along that one shared corridor — the heads cannot each claim an
        independent direction. We therefore:

          1. seat all filament HEADS side-by-side on the port's bus ROW (``self._row``)
             going east, starting one column off the port (the port stays free at the
             array edge). The NARROWEST head goes nearest the port so a wider head's
             body never walls the lane to a farther head (the proven fan-out: the bus
             leaves the port, runs east just below/along the head row, and each head
             taps it at its own broker);
          2. pack each filament's TAIL (the blocks after its head) as its OWN coherent
             serpentine run in its OWN band-region below the head row, so the filaments
             stay visually separable. The head→tail link is a normal block→block net.

        Heads are ordered narrowest-first; ties keep flow order. Each tail region is
        stacked under the previous one. Mutates the shared accumulators."""
        blk_of = self._blk_of

        def head_w(members):
            kind = self._orient_for(blk_of[members[0]], True, members[0], out_pos,
                                    self._start_x, self._row)
            return self._oriented_wh(blk_of[members[0]], kind)[0]

        ordered = sorted(filaments, key=lambda m: (head_w(m), self._order_index(m)))

        # The chip OUTPUT-port cells the terminal blocks egress to MUST stay free (a
        # block covering one leaves the bus no port cell to exit through — the egress
        # then can't route). Collect them so head packing never seats a head on one.
        out_cells = {c for c in self._out_port_of.values() if c is not None}
        # If an output port sits ON the head row (e.g. x16_out at (9,0) on row 0), cap
        # the head row's east edge just WEST of it so no head ever covers it.
        head_row_cap = self._width
        for oc in out_cells:
            if oc[1] == self._row:
                head_row_cap = min(head_row_cap, oc[0])

        # 1) Heads on the port row, left to right, off the port (>= start_x + 1).
        # A SINGLE-CELL head seated on the port row between the port (or a previous head)
        # on the WEST and the next head abutting on the EAST is BOXED into the corner: its
        # only open faces are the off-grid north edge of the port row and the one south
        # cell, and the flow-orienter commits that south cell as the head's EMIT face — so
        # the bus router's INPUT broker is forced onto the EAST cell, which the neighbour's
        # corridor also uses, tangling the two host-injected input streams (the modem's TX
        # mapper vs RX matched filter). DROP A FREE ROW below such a boxed single-cell head
        # (seat it at ``self._row + 1`` rather than the port row) so it gains free
        # neighbours on its WEST/EAST/SOUTH faces and the router can give it its OWN broker
        # whose fwd_face points INTO it (a clean ride-straight delivery, no foreign-broker
        # divert). A multi-cell head's body already exposes free broker neighbours, so it
        # stays on the row. The dropped head is still reached by the bus turning down.
        hx = self._start_x + 1
        head_band_h = 0
        for members in ordered:
            head = members[0]
            blk = blk_of[head]
            kind = self._orient_for(blk, True, head, out_pos, hx, self._row)
            w, h = self._oriented_wh(blk, kind)
            if hx + w > head_row_cap:        # would reach the output-port column
                # No room left on the head row before the output-port column. Fall
                # back to seating this head at the START of its own tail region (still
                # off the port, reachable by the bus turning down) rather than dropping
                # it — every block must be placed.
                positions[head] = None       # marker: place with the tail run below
                continue
            # A boxed single-cell head drops one row so it isn't walled into a shared
            # broker; it still sits in the head band (its row+1 just adds to head_band_h).
            boxed_single = (w * h) == 1 and len(ordered) > 1
            row = self._row + 1 if boxed_single else self._row
            orientations[head] = kind
            positions[head] = (chip, hx, row)
            op = self._io_offsets(blk, kind)
            if op is not None and op[1] is not None:
                out_pos[head] = (hx + op[1][0], row + op[1][1])
            head_band_h = max(head_band_h, (row - self._row) + h)
            spine.append((min(self._width - 1, hx + w), row))
            # Heads ABUT on the row (gap 0): the bus fans from the port and threads the
            # free cells around/below them, tapping each at its own broker — a gap would
            # only widen the head row and push a wide head past the output-port column.
            hx += w

        # 2) Tails: each filament's remaining blocks as a serpentine in its own region
        # below the head row. Regions stack so the filaments are separable. A head that
        # could NOT fit on the head row (marked None) is packed at the FRONT of its tail
        # run so it still gets a position.
        band_top = self._row + head_band_h + self._band_margin
        for members in ordered:
            head = members[0]
            tail = list(members[1:])
            if positions.get(head) is None:
                positions.pop(head, None)                 # drop the marker
                tail = [head] + tail                      # pack the head with its tail
            if not tail:
                continue
            spine.append((self._start_x, band_top))      # region-break waypoint
            band_top = self._pack_run(tail, positions, orientations, out_pos,
                                      spine, chip, band_top=band_top)

    def _east_bound_for(self, row) -> int:
        """The rightmost column a block may extend onto for a band at ``row`` — the
        array width, except a row holding an OUTPUT port at the last column withholds
        that column (a clear egress to the port). Other rows are unconstrained, so the
        proven serpentines that use the last column on non-port rows are unaffected."""
        return self._east_bound_row.get(row, self._width)

    def _east_bound_col(self) -> int:
        """The rightmost column a feed-forward block may extend onto on ANY band — one
        west of a chip OUTPUT port at the array's last column, so the egress column
        stays a clear vertical corridor (no forward-route vs egress fwd_face contention).
        Falls back to the full width when no output port holds the last column."""
        cols = [c[0] for c in self._reserved_out if c[0] == self._width - 1]
        return min([self._width] + cols)

    def _order_index(self, members):
        """A stable sort key for a filament: the flow-order index of its head."""
        try:
            return self._flow_order_index.get(members[0], 0)
        except AttributeError:
            return 0

    def _bus_snake_band_assign(self, order, widths, budget_fn, W):
        """Partition the flow ``order`` into BANDS (each a flow-ordered list of block
        names) for the bus-snake, minimising the band count subject to each band's WIDTH
        BUDGET (``budget_fn(k, nbands)``) and an ODD final band count (so the last band is
        right-going and egresses up the conflict-free right rail).

        The §ROUTING_TOPOLOGIES rule is only that each FILAMENT's blocks stay in flow
        order along the backbone; the cross-filament interleave is free. So this is a
        bin-pack of the flow order where consecutive bands are filled greedily best-fit
        but the GLOBAL order may be any topological order — we keep the supplied ``order``
        (already a valid topological order) and simply cut it into width-bounded bands,
        then, if that needs an even count or overflows, retry over a few principled
        re-orderings. Falls back to the plain greedy cut (any band count) if no odd-count
        packing fits — the caller's on-grid gate then decides."""
        names = list(order)

        def _greedy_cut(seq, nbands_hint):
            """Cut ``seq`` into bands honouring per-band budgets; nbands_hint sizes the
            budget lookups. Returns the band list (length may differ from the hint)."""
            bands_out: list = [[]]
            cur_w = 0
            for n in seq:
                w = widths[n]
                k = len(bands_out) - 1
                bud = budget_fn(k, max(nbands_hint, len(bands_out)))
                if cur_w + w > bud and bands_out[-1]:
                    bands_out.append([])
                    cur_w = 0
                bands_out[-1].append(n)
                cur_w += widths[n]
            return bands_out

        # Try band counts 1,3,5,… (odd) — for each, cut greedily with that count's budgets
        # and accept the FIRST whose cut actually fits in that many bands AND is odd.
        total = sum(widths[n] for n in names)
        max_bands = len(names)
        for nb in range(1, max_bands + 2, 2):
            cut = _greedy_cut(names, nb)
            if len(cut) == nb and all(
                    sum(widths[n] for n in b) <= budget_fn(k, nb)
                    for k, b in enumerate(cut)):
                return cut
        # No odd-count exact fit with the plain order — reorder to a flow-respecting
        # interleave that bin-packs tighter (each filament kept in order, blocks pulled
        # forward to fill a band). Best-fit-decreasing within the topological frontier.
        order_idx = {n: i for i, n in enumerate(names)}
        # Predecessor: the block that must precede each (its driver in the same filament).
        from model.connection import BlockEndpoint
        pred = {n: None for n in names}
        for conn in self._project.connections:
            s, t = conn.source, conn.target
            if isinstance(s, BlockEndpoint) and isinstance(t, BlockEndpoint) \
                    and s.block in order_idx and t.block in order_idx:
                # keep only the LAST driver in flow order (the immediate predecessor)
                if pred[t.block] is None or order_idx[s.block] > order_idx[pred[t.block]]:
                    pred[t.block] = s.block
        for nb in range(1, max_bands + 2, 2):
            placed: set = set()
            bands_out = []
            for k in range(nb):
                bud = budget_fn(k, nb)
                band: list = []
                cur = 0
                progressed = True
                while progressed:
                    progressed = False
                    # ready = blocks whose predecessor is already placed, in flow order
                    ready = [n for n in names if n not in placed
                             and (pred[n] is None or pred[n] in placed)]
                    # best-fit: the widest ready block that still fits the band
                    ready.sort(key=lambda n: (-widths[n], order_idx[n]))
                    for n in ready:
                        if cur + widths[n] <= bud:
                            band.append(n)
                            placed.add(n)
                            cur += widths[n]
                            progressed = True
                            break
                bands_out.append(band)
            if len(placed) == len(names) and all(b for b in bands_out):
                # Order within each band: keep each FILAMENT's blocks in flow order (the
                # only ordering rule) but otherwise push SINGLE-CELL blocks toward the
                # band END (the backbone BEND), where a single-cell block abuts TWO
                # backbone cells (lane + rail) on different faces — the §5.3 in==out
                # split the bus router needs to avoid a deadlock. The cross-filament
                # interleave is free, so a single-cell block may sit after a different
                # filament's block.
                for b in bands_out:
                    b[:] = self._order_band_single_cell_last(b, order_idx)
                return bands_out
        # Last resort: plain greedy cut (any count) — the on-grid gate decides.
        return _greedy_cut(names, 3)

    def _filament_of(self, names_set):
        """Map each block name → its filament index (forward-reachability partition)."""
        # Reuse the placer's filament partition over the current order.
        order = [n for n in self._flow_order_index] if hasattr(self, "_flow_order_index") \
            else list(names_set)
        fils = self._filaments(order, set(names_set))
        return {n: i for i, mem in enumerate(fils) for n in mem}

    def _order_band_single_cell_last(self, members, order_idx):
        """Order a band's members keeping each FILAMENT's blocks in flow order, but
        emitting MULTI-cell blocks before SINGLE-cell ones where the filament order
        allows — so single-cell blocks drift to the band end (the backbone bend, the
        §5.3 in==out split). A stable topological-ish merge: repeatedly pick the
        flow-earliest READY block (all same-filament predecessors already emitted),
        preferring a MULTI-cell candidate over a single-cell one."""
        fil_of = self._filament_of(set(members))
        remaining = sorted(members, key=lambda n: order_idx[n])
        emitted: list = []
        emitted_set: set = set()

        def ready(n):
            fi = fil_of.get(n)
            return all(order_idx[m] >= order_idx[n] or m in emitted_set
                       for m in remaining if fil_of.get(m) == fi)

        def is_single(n):
            return self._oriented_wh(self._blk_of[n], None) == (1, 1)

        while remaining:
            cands = [n for n in remaining if ready(n)]
            if not cands:
                cands = remaining[:]                  # safety: avoid stall
            # prefer multi-cell, then flow-earliest
            cands.sort(key=lambda n: (1 if is_single(n) else 0, order_idx[n]))
            pick = cands[0]
            emitted.append(pick)
            emitted_set.add(pick)
            remaining.remove(pick)
        return emitted

    def _pack_bus_snake(self, order, positions, orientations, out_pos, spine,
                        chip):
        """Pack the BUS-SNAKE layout — the co-designed placement the single-backbone
        v2 router threads (doc/ROUTING_TOPOLOGIES.md bus topology). It lays EVERY block
        (all filaments) along ONE clean serpentine in global flow ``order``, so the v2
        threader visits each block's input cell as an in-order tap and weaves one
        contiguous backbone input-port → taps → output-port.

        The geometry is deliberately SIMPLE and threadable (the earlier generic
        ``_pack_run`` scattered blocks — flyline reorientation + per-band overflow — so
        a tap's input cell could end boxed with a single free neighbour, and the tap
        order zig-zagged left↔right, walling later taps; the v2 thread then bailed).
        This packer instead guarantees:

        * **Every block's input cell rides a horizontal BUS LANE.** All these blocks
          author their input port at footprint offset (0, 0) — the anchor / min corner —
          so anchoring a block one row BELOW a free "bus lane" row puts its input cell
          directly under a free lane cell (always ≥1 free bus-facing neighbour for the
          broker tap, never boxed).
        * **Row 0 is the first bus lane AND the shared port lane.** Both chip ports sit
          on row 0 (x16_in@(0,0), x16_out@(9,0)); keeping row 0 free lets the backbone
          ingress at the input port, ride the lanes through the snake, and egress along
          the clear row back to the output port (ports stay reachable — the gate passes).
        * **A clean BOUSTROPHEDON tap order.** Blocks fill a band left-to-right; the next
          band reverses, and a fresh bus lane separates the bands. Anchors therefore
          march monotonically along the snake, so the v2 serpentine tap sort matches flow
          order and the backbone never doubles back to strand a later tap.

        Folds (internal feedback — MF/Costas/Gardner/IQUpconvert) keep their authored
        orientation (a D4 transform would rotate the PortMap but not their hand-authored
        per-cell faces). Reorientable blocks stay identity (their input is already the
        top-left anchor). Mutates the shared accumulators; geometry only."""
        blk_of = self._blk_of
        W, H = self._width, self._height
        # Keep ALL chip-port cells free (a block body covering one seals the port —
        # the reachability gate then rejects the plan). Reserve the columns of any port
        # so a block never lands on one (the modem's array has ports at the four
        # corners; x16_in/out on row 0, x1_in/out on row H-1).
        port_cells = set()
        resolver = getattr(self, "_chip_port_resolver", None)
        if resolver is not None:
            for conn in self._project.connections:
                for ep in (conn.source, conn.target):
                    pc = self._chip_port_cell(ep) \
                        if isinstance(ep, ChipPortEndpoint) else None
                    if pc is not None:
                        port_cells.add(pc)
        # Reserve a clear lane on the row(s) holding ports so the backbone can ride
        # alongside both the input and the output port. Row 0 (the port row) is the
        # first bus lane; the bottom row holding the unused x1 ports stays clear too.
        port_rows = {pc[1] for pc in port_cells}
        bottom_free = (H - 1) if (H - 1) in port_rows else None
        usable_bottom = (bottom_free - 1) if bottom_free is not None else (H - 1)

        # THREE clear vertical corridors keep the backbone a clean SIMPLE PATH (one travel
        # direction per cell ⇒ crossover empty), with both ports on the top row:
        #   - col 0          : LEFT rail — the entry (port → first lane) AND the drop after
        #                      a LEFT-going band.
        #   - col W-2 (=8)   : RIGHT drop rail — the drop after a RIGHT-going band.
        #   - col W-1 (=9)   : EGRESS rail — reserved SOLELY for the terminal climb to the
        #                      output-port corner; never a drop, so it can't collide.
        # Blocks pack into the interior columns 1 .. W-3, leaving cols 0, W-2, W-1 clear as
        # vertical corridors. Three disjoint corridors (entry/left-drop on col 0, right-drop
        # on col W-2, egress on col W-1) let a 3-band boustrophedon enter top-left, snake
        # the bands, and egress top-right with NO rail double-use (the §ROUTING_TOPOLOGIES
        # egress / simple-path rule). Row 0 (the port row) stays a free top lane.
        left_margin = 1
        egress_col = W - 1            # reserved egress climb to the output port
        # A RIGHT-going band drops down col W-2 to the next lane, so it must leave col W-2
        # FREE → its blocks stop at col W-3. A LEFT-going band drops down col 0 (the left
        # rail, already free) → its blocks may use up to col W-2. So the right edge a band
        # may occupy depends on its travel direction. (The egress rail col W-1 is always
        # reserved.) Three disjoint vertical corridors — entry/left-drop col 0, right-drop
        # col W-2, egress col W-1 — keep the backbone a SIMPLE PATH (crossover empty).
        # Width budget for the k-th band: a RIGHT-going band (even k: 0,2,…) reserves the
        # right drop rail (col W-2) → interior width W-3; a LEFT-going band (odd k) drops
        # the left rail → width W-2; the LAST band needs NO drop (it egresses) → width W-2
        # whatever its direction. The number of bands is forced ODD so the last band is
        # RIGHT-going and egresses up the conflict-free right rail (col W-1). Bodies abut
        # within a band (gap 0).
        kinds = {n: None for n in order}              # all identity (input = top-left)
        widths = {n: self._oriented_wh(blk_of[n], None)[0] for n in order}
        heights = {n: self._oriented_wh(blk_of[n], None)[1] for n in order}

        def _band_budget(k, nbands):
            if k == nbands - 1:
                return W - 2                          # last band: no drop, egress only
            return (W - 3) if (k % 2 == 0) else (W - 2)

        assign = self._bus_snake_band_assign(order, widths, _band_budget, W)
        # assign: list of bands, each a list of block names in flow order. Build geometry.
        bands: list = []             # [(bus_row, going_right)]
        bus_row = self._row + 1       # first bus lane is row 1; bodies row 2 (row 0 free)
        for k, members in enumerate(assign):
            going_right = (k % 2 == 0)
            band_h = max((heights[n] for n in members), default=1)
            body_row = bus_row + 1
            # Lay members left→right (right-going) or right→left (left-going), abutting.
            if going_right:
                cx = left_margin
                for n in members:
                    w = widths[n]
                    orientations[n] = kinds[n]
                    positions[n] = (chip, cx, body_row)
                    op = self._io_offsets(blk_of[n], None)
                    if op is not None and op[1] is not None:
                        out_pos[n] = (cx + op[1][0], body_row + op[1][1])
                    cx += w
            else:
                # Right-to-left: place from the right edge (col W-2) leftward, but keep
                # flow order so the bus tapping left→... still sees flow order. The
                # backbone rides this band right→left, tapping members in flow order, so
                # the FIRST flow member must sit at the RIGHT.
                cx = W - 2
                for n in members:
                    w = widths[n]
                    orientations[n] = kinds[n]
                    ax = cx - w + 1
                    ax = max(left_margin, ax)
                    positions[n] = (chip, ax, body_row)
                    op = self._io_offsets(blk_of[n], None)
                    if op is not None and op[1] is not None:
                        out_pos[n] = (ax + op[1][0], body_row + op[1][1])
                    cx = ax - 1
            bands.append((bus_row, going_right))
            bus_row = body_row + band_h               # free lane row below this band

        # Emit the spine as ONE ORDERED, CONTIGUOUS SIMPLE-PATH backbone the v2 threader
        # rides verbatim — it abuts every block's input cell in flow order and ends at the
        # output port, with NO cell reused (so crossover_plan is empty by construction):
        #   in-port → down LEFT rail (col 0) to band-0 lane
        #   → BOUSTROPHEDON: ride each band's bus-lane row (right / left / …); between
        #     bands drop the RIGHT rail (col W-2) after a right-going band, the LEFT rail
        #     (col 0) after a left-going band
        #   → from the last band's end, step into the EGRESS rail (col W-1) and climb it to
        #     the top row, landing on the OUTPUT-port corner.
        top = self._row
        path: list = []

        def _ride(a, b):
            (x0, y0), (x1, y1) = a, b
            if x0 == x1:
                step = 1 if y1 >= y0 else -1
                cells = [(x0, y) for y in range(y0, y1 + step, step)]
            else:
                step = 1 if x1 >= x0 else -1
                cells = [(x, y0) for x in range(x0, x1 + step, step)]
            for c in cells:
                if not path or path[-1] != c:
                    path.append(c)

        drop_right = W - 2                           # right-going bands drop down this col
        path.append((0, top))
        _ride((0, top), (0, bands[0][0]))           # entry: down the left rail
        for bi, (br, gr) in enumerate(bands):
            last = (bi + 1 == len(bands))
            if gr:
                # Ride right. Non-last right band ends at its drop rail (W-2); the LAST
                # right band rides to W-2 then steps onto the egress rail (W-1).
                _ride((0, br), (drop_right, br))
                end_col = drop_right
            else:
                # Left band: a left-going band is always entered by the previous right
                # band's right-rail drop at col W-2, so ride from there to the left rail.
                _ride((drop_right, br), (0, br))
                end_col = 0
            if not last:
                nbr = bands[bi + 1][0]
                _ride((end_col, br), (end_col, nbr))   # drop the rail the band ended on
        # Egress: from the last band's end, climb to the output-port corner. A right-ending
        # band steps onto the egress rail (W-1) and climbs it. A left-ending band climbs the
        # left rail to the top row and rides right to the output port.
        last_br, last_gr = bands[-1]
        if last_gr:
            _ride((drop_right, last_br), (egress_col, last_br))
            _ride((egress_col, last_br), (egress_col, top))
        else:
            _ride((0, last_br), (0, top))
            _ride((0, top), (egress_col, top))
        for c in path:
            spine.append(c)
        self._bus_snake_backbone = list(path)

        # SINGLE-CELL BEND CHECK (§5.3 in==out). A single-cell block both receives and
        # drives on its ONE cell; the bus router avoids the same-face deadlock ONLY when
        # the block abuts a backbone BEND (two backbone cells, Δindex ≤ 2, on DIFFERENT
        # faces — input from the earlier, output from the later). Record any single-cell
        # block that does NOT — the caller's gate then REJECTS this snake (it would build
        # a deadlocked chip) and falls back to the proven multi-filament layout, so a
        # design the snake can't deadlock-safely place is never silently mis-built.
        bb_idx = {c: i for i, c in enumerate(path)}

        def _has_bend(cell):
            faces = [(d, bb_idx[(cell[0] + d[0], cell[1] + d[1])])
                     for d in ((1, 0), (-1, 0), (0, 1), (0, -1))
                     if (cell[0] + d[0], cell[1] + d[1]) in bb_idx]
            return any(f1 != f2 and 1 <= (i2 - i1) <= 2
                       for f1, i1 in faces for f2, i2 in faces)

        self._bus_snake_sc_deadlock = False
        for n in order:
            if self._oriented_wh(blk_of[n], None) != (1, 1):
                continue
            _c, bx, by = positions[n]
            # A single-cell block FED DIRECTLY by a chip input port at its own cell has no
            # broker (no in==out hazard); but in bus-snake mode no block sits on a port, so
            # every single-cell block here is bus-fed → needs a bend.
            if not _has_bend((bx, by)):
                self._bus_snake_sc_deadlock = True
                break

    def _pack_run(self, run_order, positions, orientations, out_pos, spine, chip,
                  *, band_top):
        """Pack ONE serpentine run (a single filament/tail, or the whole flow order in
        single-filament mode) starting at band ``band_top``. Lays the run's blocks
        left-to-right, wrapping DOWN to the next band at the array width. Mutates the
        shared ``positions``/``orientations``/``out_pos``/``spine`` accumulators (so a
        later filament's blocks treat earlier ones as placed) and returns the band row
        just BELOW this run (where the next filament region begins). The per-block
        logic is identical for both strategies — only the lead-on-port exemption
        (``self._lead_block``) and the starting band differ."""
        blk_of = self._blk_of
        x = self._start_x
        going_right = True
        band_h = 0  # tallest block extent in the current band

        for n in run_order:
            blk = blk_of[n]
            # Decide the orientation FIRST so the pack reserves space for the cells
            # actually placed. Don't reorient the LEAD input-fed block: its input
            # cell must sit on the port at the array's edge and its body must extend
            # INTO the array. Orienting it toward the bus (WEST/etc.) would push
            # cells off-grid.
            if n == self._lead_block:
                kind = None
            else:
                kind = self._orient_for(blk, going_right, n, out_pos, x, band_top)
            orientations[n] = kind
            # Use the ORIENTED extent (a cw/ccw rotate swaps w/h) so positions match
            # the cells the caller places — keeping the spine and the next block
            # adjacent to this block's output (no far-band stranding).
            w, h = self._oriented_wh(blk, kind)
            # Would this block overflow the band horizontally? If so, wrap to the
            # next band and reverse direction (serpentine). The lead block never
            # wraps (it must land on the anchor/input port). The east boundary keeps a
            # chip OUTPUT port at the array's last column a clear EGRESS: PER ROW the
            # band on the port's own row is held one column short of it (so a block body
            # never covers the port cell and walls every egress), EXCEPT a NON-FEEDBACK
            # multi-cell block keeps the WHOLE egress column clear on every band: its
            # output/egress route would otherwise wind through that column and CONTEND
            # with another stream on the single bus fwd_face — the "routes but doesn't
            # deliver" folded-RRC→IQUpconvert failure, and the cross-filament net1
            # contention in the modem. A FEEDBACK fold (Costas/Gardner) is exempt: it
            # has no forward-route-vs-egress contention and legitimately uses the last
            # column (the coherent-RX Costas at column 9 row 3), so it keeps the per-row
            # bound. Terminal blocks are NOT exempt — keeping them west of the egress
            # column lets their output route cleanly UP that clear column to the port.
            # In MULTI-filament mode the egress column must stay clear for EVERY
            # multi-cell block (even a feedback fold like IQUpconvert): two filaments'
            # egresses share that corridor and a fold body on it strands the other
            # filament's internal handoff (the modem net1 cross-filament contention).
            # In SINGLE-filament mode a feedback fold is exempt (it legitimately uses
            # the last column — the coherent-RX Costas — with no competing egress).
            col_reserve = (n != self._lead_block and (w * h) > 1
                           and (self._in_multi_pack
                                or not self._has_internal_feedback(blk)))
            east = self._east_bound_col() if col_reserve \
                else self._east_bound_for(band_top)
            if going_right:
                overflow = (x + w) > east and positions
            else:
                overflow = (x - w + 1) < 0 and positions
            # A FEEDBACK FOLD (Costas/Gardner/IQUpconvert) is non-reorientable and
            # authored WEST-in / EAST-out: its input cell sits on the WEST footprint
            # edge (the broker taps there) and its output continues EAST toward the
            # next stage / the output port. On a LEFT-going band the fold's egress
            # would have to double back ACROSS its own input broker (a single-
            # outstanding cell the fold's arbiter LOCK backpressures) — sharing it
            # deadlocks the burst (the IQUpconvert collapse). Force such a fold onto a
            # fresh RIGHT-going band so input (west) and egress (east) have clearance
            # on opposite sides and the output flows toward its consumer. No-op when
            # already going right (the flagship Costas/Gardner land right-going).
            # Only a BUS-FED fold (one with an upstream BLOCK driver) is managed: a
            # standalone / lead fold (no inter-block driver — e.g. the single-block
            # CoherentRX smoke test, or the input-fed lead) keeps its natural anchor,
            # since there is no source-bus to double back across.
            fold_managed = (n != self._lead_block and self._is_west_in_fold(blk)
                            and self._driver_of.get(n) is not None)
            if fold_managed and not going_right and positions:
                overflow = True
            if overflow:
                band_top = band_top + band_h + self._band_margin
                going_right = not going_right
                band_h = 0
                # A leftward band starts at its east boundary minus one so it never seats
                # a block on a reserved output-port cell (or, for a feed-forward block
                # keeping the egress column clear, on that column).
                bnd = self._east_bound_col() if col_reserve \
                    else self._east_bound_for(band_top)
                x = (bnd - 1) if not going_right else 0
                # The travel direction flipped after we chose the orientation; re-
                # derive it for the new direction (and the oriented extent with it)
                # so the block faces the bus the right way on the new band.
                if n != self._lead_block:
                    kind = self._orient_for(blk, going_right, n, out_pos, x,
                                            band_top)
                    orientations[n] = kind
                    w, h = self._oriented_wh(blk, kind)
                # mark the wrap column on the spine (the snake turns here)
                spine.append((max(0, min(self._width - 1, x)), band_top))

            # Place the block's anchor (min corner). When travelling LEFT the
            # block still occupies [x-w+1 .. x]; anchor at the min corner.
            ax = x if going_right else max(0, x - w + 1)
            # FEEDBACK-FOLD output alignment (§4.3 / §5.3): a west-in/east-out fold's
            # INPUT route must stay SHORT (not sweep across the array — a long input
            # bus walls the egress) and its OUTPUT must reach its consumer WITHOUT the
            # egress doubling back over the input broker. Both hold when the fold's
            # OUTPUT cell sits NEAR its consumer's column: the input then taps the bus
            # just to the fold's west and the egress continues straight east. Slide the
            # (right-going) fold so its output cell aligns under its consumer (the
            # downstream block's input, or the chip output port), clamped to keep the
            # input edge off the wall (ax>=1) and the whole footprint on-grid.
            if going_right and fold_managed:
                tgt = self._fold_consumer_x(n)
                if tgt is not None:
                    io = self._io_offsets(blk, kind)
                    out_dx = io[1][0] if (io and io[1]) else (w - 1)
                    # Anchor so the OUTPUT cell lands one column WEST of the consumer
                    # (the egress then continues @1 into it). The hi clamp keeps the
                    # fold's east edge off the array's last column so a chip OUTPUT
                    # port cell there stays FREE (the fold must not cover it — that
                    # leaves the corridor/bus router no port cell to egress to); the
                    # lo clamp keeps the west input edge off the wall (ax>=1).
                    want = tgt - 1 - out_dx
                    hi = max(1, self._width - 1 - w)   # east edge at most W-2
                    ax = max(1, min(want, hi))
                    x = ax                        # keep the band cursor consistent
            # Wall inset (§4.3): keep the block's BUS-FACING edge off the array
            # boundary so its I/O cell retains a free bus-facing NEIGHBOUR for the
            # broker tap. A FOLDED non-reorientable block (Costas/Gardner/IQ-
            # upconvert: a hand-authored serpentine whose faces don't transform, so
            # the orienter leaves it as-authored) seats its input cell on the
            # footprint edge that abuts the bus; when a left-going band-wrap anchors
            # that edge ON the array wall (ax==0), the input cell has NO free
            # neighbour and the router can find no broker tap (the §1.2 "no bus path
            # to the broker" failure). Nudge the anchor one cell inward so the bus
            # edge faces open fabric. Harmless for unwalled placements (no-op there).
            # The LEAD input-fed block is EXEMPT: its input cell is deliberately
            # seated ON the chip input port at the array edge (the controller re-
            # anchors it post-plan), so insetting it off the port would break ingress.
            # Only a BUS-FED fold needs the input-broker neighbour; a standalone fold
            # (no inter-block driver) has no bus input to keep off the wall.
            if n != self._lead_block and self._driver_of.get(n) is not None:
                ax, band_top = self._wall_inset(blk, kind, ax, band_top, w, h)
            positions[n] = (chip, ax, band_top)
            band_h = max(band_h, h)
            # Record where this block's OUTPUT cell lands (anchor + the post-orient
            # output offset) so the next block scores its input flyline against it.
            op = self._io_offsets(blk, kind)
            if op is not None and op[1] is not None:
                out_pos[n] = (ax + op[1][0], band_top + op[1][1])

            # Bus waypoint: the cell just outside the block on the travel side,
            # at the band row, so the spine threads block-to-block.
            bus_y = band_top
            if going_right:
                spine.append((min(self._width - 1, ax + w), bus_y))
                x = ax + w + 1 + self._gap
            else:
                spine.append((max(0, ax - 1), bus_y))
                x = ax - 1 - self._gap

        # The next filament region begins one band below this run's last band
        # (its tallest block + the routing margin), so the regions don't overlap.
        return band_top + band_h + self._band_margin

    # -- orientation (flyline-minimising auto-orient, §8 / §4.3) --------------
    def _orient_for(self, blk, going_right, name, out_pos, x, band_top):
        """Choose the D4 orientation that MINIMISES this block's total flyline to
        its actual neighbours — the input cell nearest its driver, the output cell
        nearest its consumer (AUTO_PNR_DESIGN §8, flow-ordered §4.3).

        For each candidate orientation (identity + the 4 primitive transforms) we
        compute where the block's INPUT and OUTPUT cells would physically land at
        this band/x, then score:

        - ``flyline_in``  = Manhattan(input cell → driver's output cell), where the
          driver is the already-placed upstream block (its real output position is
          in ``out_pos``) or, for a port-fed block, the chip INPUT port cell. This
          is the dominant, fully-KNOWN term (the upstream neighbour is placed).
        - ``flyline_out`` = Manhattan(output cell → consumer's input) — but ONLY
          when the consumer is EXACT (this block feeds a chip OUTPUT port). The
          downstream BLOCK consumer is not yet placed, so rather than fabricate a
          distance that could swamp the real input term, an unplaced consumer is
          handled by the travel-direction TIE-BREAK below (documented
          approximation: the output should face the bus continuation).

        Ties prefer, in order: an OUTPUT facing the travel direction (the bus
        continuation toward the next stage, §8) when the consumer is not exact;
        then an orientation whose input+output are **co-located on the bus-facing
        edge** (``io_colocated`` — the cheap 1-D bus tap, §4.3 / INV-8); then
        identity (stability — never transform a block needlessly). Returns the
        transform kind ('cw'/'ccw'/'mirror_h'/'mirror_v') or None (identity).
        Falls back to None when no PortMap is available.
        """
        pm_provider = getattr(self, "_port_map_provider", None)
        if pm_provider is None:
            return None
        try:
            base = self._provider(pm_provider, blk)
        except Exception:  # noqa: BLE001
            return None
        if base is None:
            return None

        # Where the driver's output sits (KNOWN): the placed upstream block, else
        # the chip input port. None ⇒ no input flyline term (unconnected head).
        drv = self._driver_of.get(name)
        driver_out = out_pos.get(drv) if drv is not None else None
        if driver_out is None:
            driver_out = self._in_port_of.get(name)

        # Where this block's consumer input is. Exact if it feeds a chip OUTPUT
        # port; otherwise ESTIMATE it as the next anchor in the travel direction.
        consumer_in = self._out_port_of.get(name)

        # A block with INTERNAL feedback/forwarding (a Costas/Gardner-style loop,
        # the complex matched filter) hardcodes per-cell FACES in its assembly — a
        # dual-face emit / feedback return rests at a SPECIFIC direction the build's
        # feedback tracer follows. A D4 transform rotates the PortMap faces but NOT
        # that hand-authored direction-specific program, so reorienting such a block
        # silently breaks its loop (the RX recovers nothing). Restrict its search to
        # IDENTITY — its layout was authored to fold I/O on its bus edge already, so
        # we never need to rotate it (verified: rotating Gardner cw breaks RX BER).
        if self._has_internal_feedback(blk):
            return None
        # The bus continues in the travel direction, so absent an exact consumer
        # the OUTPUT should face that way (EAST going right, WEST going left).
        from model.enums import Face
        travel = Face.EAST if going_right else Face.WEST
        candidates = (None, "cw", "ccw", "mirror_h", "mirror_v")
        best = None  # ((flyline, travel_rank, colo_rank, ident_rank), kind)
        for kind in candidates:
            try:
                pm = base if kind is None else base.transformed(kind)
            except Exception:  # noqa: BLE001
                continue
            io = self._io_offsets_from(pm)
            if io is None:
                continue
            in_off, out_off = io
            w, h = self._oriented_wh(blk, kind)
            ax = x if going_right else max(0, x - w + 1)
            # PRIMARY: the flyline to KNOWN neighbours only — the placed driver and
            # (if this block feeds a chip output port) the exact consumer port. The
            # input-near-driver term dominates because the upstream block IS placed;
            # an unplaced downstream consumer is handled by a tie-break (below), not
            # a fabricated distance that could swamp the real input term.
            flyline = 0
            if in_off is not None and driver_out is not None:
                icx, icy = ax + in_off[0], band_top + in_off[1]
                flyline += abs(icx - driver_out[0]) + abs(icy - driver_out[1])
            out_face = None
            if out_off is not None:
                ocx, ocy = ax + out_off[0], band_top + out_off[1]
                outs = pm.outputs()
                out_face = outs[0].face if outs else None
                if consumer_in is not None:
                    flyline += abs(ocx - consumer_in[0]) + abs(ocy - consumer_in[1])
            # Tie-breaks (0 sorts first = preferred):
            #  - travel_rank: when the consumer is NOT exact, prefer an output that
            #    faces the bus continuation (the §8 flow-orient intent, now a tie-
            #    break rather than a distance term so it can't override the input).
            #  - colo_rank: prefer the fold aspect that co-locates I/O on the bus
            #    edge (the cheap 1-D tap, §4.3 / INV-8).
            #  - ident_rank: prefer identity — never transform a block needlessly.
            travel_rank = (0 if (consumer_in is not None or out_face == travel)
                           else 1)
            colo_rank = 0 if pm.io_colocated else 1
            ident_rank = 0 if kind is None else 1
            key = (flyline, travel_rank, colo_rank, ident_rank)
            if best is None or key < best[0]:
                best = (key, kind)
        return best[1] if best is not None else None

    def _fold_consumer_x(self, name):
        """The x-column a feedback fold's OUTPUT should align under: its consumer's
        input column. The consumer is a chip OUTPUT port (exact, ``_out_port_of``) or
        the downstream block — but a downstream block is not yet placed, so for that
        case fall back to the chip output port the pipeline ultimately egresses to (a
        terminal fold), else None (no alignment hint; the natural pack position
        stands)."""
        cx = self._out_port_of.get(name)
        if cx is not None:
            return cx[0]
        return None

    def _is_west_in_fold(self, blk) -> bool:
        """True if ``blk`` is a non-reorientable FEEDBACK FOLD whose input edge faces
        WEST (its broker taps the west side, its output continues east) — the
        Costas/Gardner/IQUpconvert family. Such a fold must sit on a RIGHT-going band
        so its egress never doubles back across its input broker (§5.3 deadlock).
        Resolved from the feedback flag + the (identity) PortMap bus edge; False if
        either is unavailable."""
        if not self._has_internal_feedback(blk):
            return False
        pm = self._oriented_port_map(blk, None)   # identity — folds aren't reoriented
        from model.enums import Face
        return getattr(pm, "bus_facing_edge", None) == Face.WEST if pm else False

    def _has_internal_feedback(self, blk) -> bool:
        """True if ``blk`` declares INTERNAL connections/jumps — a feedback loop or
        cross-cell forwarding whose assembly hardcodes per-cell faces (so a D4
        transform would rotate the PortMap geometry but not the program → break the
        loop). Such blocks are left as-authored by the flyline orienter.

        A multi-cell FEED-FORWARD wavefront (e.g. FIR) declares NO internal
        connections — its forwarding faces come from its ``default_layout`` and DO
        transform correctly — so it is NOT flagged and remains freely orientable.

        Uses the injected ``feedback_provider(block_type, library[, params]) ->
        bool`` when present; without it (a bare unit-test placer) NO block is
        flagged, so the orienter is free — those callers don't build feedback DUTs.
        """
        provider = getattr(self, "_feedback_provider", None)
        if provider is None:
            return False
        try:
            return bool(self._provider(provider, blk))
        except Exception:  # noqa: BLE001
            return False

    # -- I/O offset helpers ---------------------------------------------------
    def _io_offsets(self, blk, kind):
        """The (input_offset, output_offset) of ``blk`` AFTER orientation ``kind``,
        each ``(dx, dy)`` from the block's min corner — or None if no PortMap. Used
        to record where a placed block's output cell landed."""
        pm_provider = getattr(self, "_port_map_provider", None)
        if pm_provider is None:
            return None
        try:
            base = self._provider(pm_provider, blk)
            pm = base if kind is None else base.transformed(kind)
        except Exception:  # noqa: BLE001
            return None
        return self._io_offsets_from(pm)

    def _oriented_port_map(self, blk, kind):
        """The block's :class:`PortMap` AFTER orientation ``kind`` (or None)."""
        pm_provider = getattr(self, "_port_map_provider", None)
        if pm_provider is None:
            return None
        try:
            base = self._provider(pm_provider, blk)
            return base if kind is None else base.transformed(kind)
        except Exception:  # noqa: BLE001
            return None

    def _wall_inset(self, blk, kind, ax, band_top, w, h):
        """Nudge a FOLDED block's anchor so its BUS-FACING edge does not lie on the
        array wall — returns the (possibly shifted) ``(ax, band_top)``.

        A folded, non-reorientable block (one with internal feedback/forwarding,
        whose hand-authored faces the orienter leaves as identity — Costas, Gardner,
        IQUpconvert) seats its I/O cell on the footprint edge that taps the bus
        (``bus_facing_edge``). A serpentine band-wrap can anchor that edge ON the
        array boundary (e.g. a left-going wrap puts a WEST-edge fold at ax==0), which
        strips the I/O cell of its only free bus-facing neighbour, so the router
        finds no broker tap (§1.2). One cell of inset away from that wall restores a
        free neighbour. Applied ONLY to folded blocks (a freely-orientable block's
        edge is already chosen by the flyline orienter) and ONLY when room remains
        inside the array; otherwise the anchor is unchanged (the router then reports
        a sound failure rather than the placer fabricating an off-grid position).
        """
        if not self._has_internal_feedback(blk):
            return ax, band_top                  # orientable — orienter owns its edge
        pm = self._oriented_port_map(blk, kind)
        edge = getattr(pm, "bus_facing_edge", None) if pm is not None else None
        if edge is None:
            return ax, band_top
        from model.enums import Face
        # Inset away from the wall the bus-facing edge would sit on. ``w``/``h`` are
        # the ORIENTED cell extents, so ax+w-1 / band_top+h-1 are the far columns.
        if edge == Face.WEST and ax <= 0 and (w + 1) <= self._width:
            ax = 1
        elif edge == Face.EAST and (ax + w - 1) >= self._width - 1 \
                and (w + 1) <= self._width:
            ax = self._width - 1 - w
        elif edge == Face.NORTH and band_top <= 0 and (h + 1) <= self._height:
            band_top = 1
        elif edge == Face.SOUTH and (band_top + h - 1) >= self._height - 1 \
                and (h + 1) <= self._height:
            band_top = self._height - 1 - h
        return ax, band_top

    @staticmethod
    def _io_offsets_from(port_map):
        """(input_offset, output_offset) from a PortMap, each ``(dx, dy)`` or None.
        Picks the FIRST input port and FIRST output port (a single bus tap each)."""
        if port_map is None:
            return None
        ins = port_map.inputs()
        outs = port_map.outputs()
        in_off = (ins[0].dx, ins[0].dy) if ins else None
        out_off = (outs[0].dx, outs[0].dy) if outs else None
        return (in_off, out_off)

    def with_port_maps(self, provider):
        """Inject a ``port_map(block_type, library) -> PortMap`` provider so the
        packer can auto-orient blocks to minimise flyline. Returns self
        (chainable)."""
        self._port_map_provider = provider
        return self

    def with_chip_ports(self, resolver):
        """Inject a ``resolver(chip, port_name) -> (cell_x, cell_y)`` so the
        flyline-minimising orienter can score against the actual chip I/O port
        cells (input port for the lead block's driver, output port for a terminal
        block's consumer). Returns self (chainable)."""
        self._chip_port_resolver = resolver
        return self

    def with_multi_filament(self, on: bool = True):
        """Force the MULTI-FILAMENT strategy (no block anchored on the port; one
        coherent serpentine region per filament). The caller sets this when routing
        is BUS-mode (``use_bus``), since the bus needs the port to stay a free tap.
        A design that already has >1 filament feeding the port is multi-filament
        regardless of this flag. Returns self (chainable)."""
        self._multi_filament = bool(on)
        return self

    def with_compact(self, on: bool = True):
        """Force the COMPACT pack: lay EVERY block (all filaments) tightly in global flow
        order, off the port, leaving routing channels (``channel_reserve`` free rows).
        Topology-agnostic — the router realises the chosen topology over this layout and
        the place<->route loop perturbs on failure. Returns self (chainable)."""
        self._compact = bool(on)
        return self

    def with_channel_reserve(self, rows: int):
        """Set the number of free rows the compact pack leaves between block rows (the
        routing-channel reserve). The place<->route loop raises this to open channels
        when routing congests. Returns self (chainable)."""
        self._channel_reserve = max(0, int(rows))
        return self

    def with_feedback(self, provider):
        """Inject a ``feedback(block_type, library[, params]) -> bool`` provider
        that reports whether a block has INTERNAL feedback/forwarding (hardcoded
        per-cell faces). The flyline orienter leaves such blocks as-authored — a D4
        transform would rotate their PortMap but not their direction-specific
        program, breaking the loop. Returns self (chainable)."""
        self._feedback_provider = provider
        return self
