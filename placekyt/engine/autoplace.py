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
        # Who drives whom, and which chip ports each block touches — so orientation
        # can put the INPUT cell nearest its driver and the OUTPUT cell nearest its
        # consumer (flyline minimisation, §8 / §4.3).
        self._driver_of, self._in_port_of, self._out_port_of = \
            self._neighbour_maps(names)

        positions: dict = {}
        orientations: dict = {}
        # Where each placed block's OUTPUT cell physically landed, so the NEXT block
        # in flow order can score its input flyline against an already-placed driver.
        out_pos: dict = {}
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
                kind = self._orient_for(blk, going_right, n, out_pos, x, band_top)
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
                x = (self._width - 1) if not going_right else 0
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

        return PlacePlan(positions=positions, order=order,
                         orientations=orientations, spine=spine,
                         backward_edges=backward)

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

    def with_feedback(self, provider):
        """Inject a ``feedback(block_type, library[, params]) -> bool`` provider
        that reports whether a block has INTERNAL feedback/forwarding (hardcoded
        per-cell faces). The flyline orienter leaves such blocks as-authored — a D4
        transform would rotate their PortMap but not their direction-specific
        program, breaking the loop. Returns self (chainable)."""
        self._feedback_provider = provider
        return self
