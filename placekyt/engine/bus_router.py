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

from model.connection import ABUTMENT_ROUTE, BlockEndpoint, ChipPortEndpoint
from model.enums import Face

from .autoroute import (AutoRouteReport, AutoRouter, RouteResult, _FACE_STEP,
                        _MAX_HOPS)

# Unit step per fwd_face code (S=0,E=1,W=2,N=3) — screen coords (x-right/y-down).
_FWD_DELTA = {0: (0, 1), 1: (1, 0), 2: (-1, 0), 3: (0, -1)}
_NEI = ((1, 0), (-1, 0), (0, 1), (0, -1))
# model.enums.Face (string-valued) → fwd_face int code.
_FACE_CODE = {"south": 0, "east": 1, "west": 2, "north": 3}


def _cardinal(frm, to):
    """The (dx, dy) unit cardinal from ``frm`` to an adjacent cell ``to``, or None."""
    dx, dy = to[0] - frm[0], to[1] - frm[1]
    return (dx, dy) if (abs(dx) + abs(dy) == 1) else None


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
                  spine_provider=None, port_map_provider=None,
                  topology="block") -> AutoRouteReport:
    """Route every UNROUTED net on one chip over a shared bus with broker taps.

    ``port_cell_provider(block_type, library) -> {port: (cell_id, direction)}`` and
    ``port_map_provider`` are the same callbacks :class:`AutoRouter` takes (reused
    for endpoint geometry). ``spine_provider(chip) -> [(x, y), ...]`` (optional)
    supplies the placement spine (the serpentine snake) as the preferred bus
    backbone; without it the router threads the bus itself.

    ``topology`` selects the routing model (doc/ROUTING_TOPOLOGIES.md):
      * ``"bus"`` / ``"ring"`` — try the single-backbone v2 router
        (:func:`_route_chip_bus_v2`) FIRST: ONE contiguous directed backbone from the
        chip input port to the output port, every block tapping off it as an ordered
        broker. It is used ONLY if it routes EVERY net on the chip AND the DRC gate
        leaves them all ok; otherwise its result is DISCARDED and the legacy per-net
        bus loop runs unchanged (a partial v2 never displaces the proven path).
      * ``"block"`` (DEFAULT) — skip v2; only the legacy per-net loop runs (single-
        filament / block-to-block designs, where forcing a backbone would over-
        constrain). The default keeps every existing caller byte-identical; the
        controller passes the smart-default ``"bus"`` for multi-filament designs.

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
        # SINGLE-BACKBONE v2 first (bus/ring): try the contiguous-backbone router. It
        # is kept ONLY if it routes EVERY net AND the DRC gate passes them all — a
        # partial/failing v2 is DISCARDED so the legacy per-net loop below stays
        # byte-identical on fallback (the proven path is never displaced).
        if topology in ("bus", "ring"):
            v2 = _route_chip_bus_v2(project, ct, chip_id, nets, spine,
                                    port_map_provider=port_map_provider)
            if v2 is not None and len(v2) == len(nets):
                gated = _drc_gate(v2, chip_types)
                bad = [r for r in gated if not r.ok]
                # ACCEPT v2 when EVERY remaining failure is a STRUCTURED
                # ``output-boxed:<block>`` failure (a multi-cell output with no free
                # neighbour — the Costas case) AND the routed subset is DRC-clean. The
                # boxed net is surfaced as a NAMED failure the Part B place<->route loop
                # detects and perturbs (re-fold / spread), so v2 still drives the loop
                # toward a full route rather than silently falling back to the legacy
                # per-net router (which would route the boxed net SHORT, 0-output). A v2
                # with any OTHER failure is discarded (legacy fallback, unchanged).
                if all(r.ok for r in gated):
                    results.extend(gated)
                    continue
                if bad and all("output-boxed:" in str(r.reason) for r in bad):
                    results.extend(gated)
                    continue
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

        # Static block-cell occupancy (block bodies + transit cells), so the ordering
        # can score how CONSTRAINED each net's target is (how many free cells abut its
        # input). The most-constrained target should claim its broker first — otherwise
        # a roomier sibling (the modem's TX mapper, 4 free faces) can grab the ONE free
        # broker cell its tighter neighbour (the RX matched filter, 2 free faces) also
        # needs, leaving that neighbour unroutable (the net6 vs net8 corner fight).
        occ_static = set()
        for blk in project.blocks:
            pl = blk.placement
            if pl is None or pl.chip != chip_id:
                continue
            occ_static.update((c.x, c.y) for c in pl.cells)
            occ_static.update((t.x, t.y)
                              for t in getattr(pl, "transit_cells", []))

        def _free_target_neighbors(n):
            """Count free cells abutting net ``n``'s TARGET input cell (block targets
            only) — a small count means a tightly-packed sink that must route first."""
            _name, s, _sf, d, _df, _sp, dst_is_port, _conn = n
            if dst_is_port or not isinstance(_conn.target, BlockEndpoint):
                return 99
            cnt = 0
            for dx, dy in _NEI:
                c = (d[0] + dx, d[1] + dy)
                if 0 <= c[0] < ct.width and 0 <= c[1] < ct.height \
                        and c not in occ_static and c != s:
                    cnt += 1
            return cnt

        def _key(n, mode):
            _name, s, _sf, d, _df, src_is_port, dst_is_port, _conn = n
            span = -(abs(s[0] - d[0]) + abs(s[1] - d[1]))
            if mode == "hazard":
                # Hazard split DOMINATES the class order so a hazard cell's input net
                # is committed before its output net regardless of egress/block class.
                return (_haz_rank(n), _cls(src_is_port, dst_is_port, "egress"), d,
                        0 if not src_is_port else 1, span)
            if mode == "constrained":
                # Most-constrained TARGET first (fewest free abutting cells), so a tight
                # sink claims its only broker before a roomier sibling can take it.
                return (_cls(src_is_port, dst_is_port, "egress"),
                        _free_target_neighbors(n), d,
                        0 if not src_is_port else 1, span)
            return (_cls(src_is_port, dst_is_port, mode), d,
                    0 if not src_is_port else 1, span)

        modes = ("egress", "blocks", "constrained")
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


def _route_chip_bus_v2(project, ct, chip_id, nets, spine, *, port_map_provider):
    """Single-backbone BUS router (doc/ROUTING_TOPOLOGIES.md).

    The bus model is ONE shared directed backbone from the chip INPUT port to the
    OUTPUT port that every block taps off; the ONLY ordering rule is that within a
    filament its blocks appear in signal-flow order along the backbone. The corruption
    the old per-net router hit was a cell that is BOTH a broker (delivering one
    filament's word) AND a FOREIGN filament's through-transit in a CONFLICTING
    direction (one cell, one ``fwd_face``).

    This router makes that impossible STRUCTURALLY: it routes EVERY net on one shared
    bus (the proven :func:`_route_chip_bus`) but with ``forbid_broker_transit=True``, so
    NO foreign net ever transits a broker cell — plain transit cells are still shared
    (same direction, via ``bus_dir``), only broker cells are private to their own
    delivery. With no foreign broker transit a broker is never a conflicting through-
    transit → ``crossover_plan`` is empty and ``broker_through_face`` never reconciles
    two directions. Nets are tagged by filament (so the ordering keeps a filament's nets
    consecutive and its source-before-target order); a few principled net orderings are
    tried (a broker laid early is a no-transit obstacle for later nets) and the first
    that routes EVERY net is kept.

    ``port_map_provider`` is part of the v2 contract signature (reserved for geometry
    callbacks); the current implementation derives everything from the resolved ``nets``
    + the project. Returns a list of :class:`RouteResult` (one per net, all ``ok``) when
    the whole chip routes WITHOUT any foreign broker transit, or ``None`` (a precondition
    fails, or no ordering routes every net under that constraint — the dense-placement
    case) so the caller DISCARDS it and falls back to the legacy per-net loop.
    """
    import os as _os
    _DBG = _os.environ.get("BUS_V2_DEBUG")

    def _bail(msg):
        if _DBG:
            print("[bus_v2 bail]", msg)
        return None

    W, H = ct.width, ct.height

    def in_bounds(c):
        return 0 <= c[0] < W and 0 <= c[1] < H

    have_in = any(n[5] for n in nets)
    have_out = any(n[6] for n in nets)
    if not (have_in and have_out):
        return _bail("no shared input+output port pair")

    def _port_cell_of(block, port, direction):
        """(x, y) of a block's ``direction`` ("in"/"out") PORT cell via the
        ``port_map_provider``. Falls back to the block's first cell for an input port,
        last cell for an output port (a multi-cell SNAKE block's output is the FAR end of
        the snake, not its input tap — a net leaving there must tap a backbone cell
        abutting it, else the source word lands at the input tap many cells short)."""
        pmap = None
        if port_map_provider is not None:
            try:
                pmap = port_map_provider(block.type, block.library, block.params)
            except TypeError:
                try:
                    pmap = port_map_provider(block.type, block.library)
                except Exception:  # noqa: BLE001
                    pmap = None
            except Exception:  # noqa: BLE001
                pmap = None
        if pmap is not None:
            for p in pmap.ports:
                if p.name == port and p.direction == direction:
                    pc = block.placement.cell(p.cell_id)
                    if pc is not None:
                        return (pc.x, pc.y)
        fallback = block.placement.cells[-1 if direction == "out" else 0]
        return (fallback.x, fallback.y)

    def in_cell_of(block, port):
        return _port_cell_of(block, port, "in")

    def out_cell_of(block, port):
        return _port_cell_of(block, port, "out")

    # Block + transit cells are obstacles for the backbone (a word never transits a
    # live block cell).
    block_cells: set = set()
    for blk in project.blocks:
        pl = blk.placement
        if pl is None or pl.chip != chip_id:
            continue
        block_cells.update((c.x, c.y) for c in pl.cells)
        block_cells.update((t.x, t.y) for t in getattr(pl, "transit_cells", []))

    # Backbone endpoints: input-port and output-port edge cells.
    in_port_cell = out_port_cell = None
    for (_n, s, _sf, d, _df, sp, dp, _c) in nets:
        if sp and in_port_cell is None:
            in_port_cell = s
        if dp and out_port_cell is None:
            out_port_cell = d
    if in_port_cell is None or out_port_cell is None:
        return _bail("missing input or output port cell")

    # Tap cell per target INPUT cell (fan-in nets share a tap). Order them along the
    # backbone by a SERPENTINE sweep of the input cells (so consecutive taps are
    # spatially adjacent → a short threadable backbone), then repair so each filament's
    # taps stay in flow order. Flow order from the connection graph (read-only).
    from .autoplace import AutoPlacer
    _placer = AutoPlacer(project, lambda *a: (0, 0))
    _names = {b.name for b in project.blocks
              if b.placement is not None and b.placement.chip == chip_id
              and b.placement.cells}
    _order, _ = _placer._flow_order(
        [b for b in project.blocks if b.name in _names], _names)
    fpos = {n: i for i, n in enumerate(_order)}
    filaments = _placer._filaments(_order, _names)
    fil_of = {n: fi for fi, mem in enumerate(filaments) for n in mem}

    tap_in: dict = {}                 # in_cell -> {"block","kind","ord","conns"}
    for (name, s, sface, d, dface, sp, dp, conn) in nets:
        if dp or not isinstance(conn.target, BlockEndpoint):
            continue
        blk = project.block(conn.target.block)
        if blk is None or blk.placement is None or not blk.placement.cells:
            continue
        ic = in_cell_of(blk, conn.target.port)
        ent = tap_in.setdefault(ic, {
            "block": blk.name, "kind": "in", "conns": [],
            "ord": (fil_of.get(blk.name, 0), fpos.get(blk.name, 1 << 20), 0)})
        ent["conns"].append(name)

    # OUTPUT TAPS (the CM multi-cell-output mandate, doc/ROUTING_TOPOLOGIES.md). For a
    # block→block / block→port net whose SOURCE block's OUTPUT cell is DIFFERENT from
    # its input cell (a multi-cell snake) the source must tap the backbone at a FREE
    # cell ABUTTING that output cell — otherwise the build (which hops exit→tap @1) lands
    # the source word at the input tap, many cells short (net5/net10/net4/net11 0-output).
    # The forward-ordered backbone (the flow bias below) keeps a producer's output cell
    # UPSTREAM of its consumer, so ``src_tap_cell`` rides from an existing backbone cell
    # abutting the output. A multi-cell output with NO free orthogonal neighbour (the BOXED
    # Costas `rotate` cell, walled by its own snake) CANNOT tap the bus: it is recorded in
    # ``boxed_outputs`` so v2 returns a structured ``output-boxed:<block>`` failure the Part
    # B place<->route loop perturbs (re-fold / spread) — never a dead build.
    boxed_outputs: dict = {}          # block name -> out_cell with no free neighbour
    for (name, s, sface, d, dface, sp, dp, conn) in nets:
        if not isinstance(conn.source, BlockEndpoint):
            continue
        sb = project.block(conn.source.block)
        if sb is None or sb.placement is None or len(sb.placement.cells) < 2:
            continue                  # single-cell source: input tap == output, no extra
        oc = out_cell_of(sb, conn.source.port)
        if oc == in_cell_of(sb, conn.source.port):
            continue                  # output leaves the input cell — input tap suffices
        free_nbrs = [(oc[0] + dx, oc[1] + dy) for dx, dy in _NEI
                     if in_bounds((oc[0] + dx, oc[1] + dy))
                     and (oc[0] + dx, oc[1] + dy) not in block_cells]
        if not free_nbrs:
            boxed_outputs[sb.name] = oc

    # MULTI-CELL EGRESS TERMINALS (the AM/duplex TX IQUpconvert case). A multi-cell block
    # whose OUTPUT cell sits MID-block (``output_cell_id`` — e.g. `upmix`) and drives the
    # chip OUTPUT port directly has NO downstream consumer tap to pull the backbone near its
    # output cell, and its input-tap thread never abuts the buried output cell. So
    # ``src_tap_cell`` finds no adjacent backbone cell and the egress falls back to the
    # INPUT-port cell (a 0-output, over-long route). Handle it like the single-cell egress
    # terminal: thread a DEDICATED egress segment (backbone head → a free cell ABUTTING the
    # output cell → output port) AFTER the main DFS, so the egress rides a SHORT tail slice
    # from a backbone cell abutting the true output cell — not the whole serpentine.
    # Map: output cell -> (block name, source port) for each such terminal.
    mc_egress_out: dict = {}
    for (name, s, sface, d, dface, sp, dp, conn) in nets:
        if not dp or not isinstance(conn.source, BlockEndpoint):
            continue
        sb = project.block(conn.source.block)
        if sb is None or sb.placement is None or len(sb.placement.cells) < 2:
            continue
        if sb.name in boxed_outputs:
            continue
        oc = out_cell_of(sb, conn.source.port)
        if oc == in_cell_of(sb, conn.source.port):
            continue
        mc_egress_out[oc] = (sb.name, conn.source.port)

    # Input cells of blocks whose output drives the chip OUTPUT port (the filament
    # TERMINALS, e.g. the modem's TX IQUpconvert + RX slicer). Both egress to the shared
    # out_port, so they should tap the backbone LATE (near the port) — otherwise a terminal
    # tapped early (the TX IQUpconvert, tapped before the whole RX chain) makes its egress
    # ride the ENTIRE backbone down through RX and back up to the port (a ~26-cell, 29-hop
    # route that never delivers). Ordering terminals last keeps each egress SHORT.
    _term_blocks = set()
    for (_n, s, _sf, d, _df, _sp, dp_, conn) in nets:
        if dp_ and isinstance(conn.source, BlockEndpoint):
            _term_blocks.add(conn.source.block)
    _term_in: set = {ic for ic, meta in tap_in.items()
                     if meta["block"] in _term_blocks}

    def serp_key(cell):
        x, y = cell
        return (y, x if (y % 2 == 0) else (W - 1 - x))

    # INPUT taps thread the backbone (the PROVEN, stable v2 behaviour — unchanged). Output
    # taps are handled POST-HOC (below): a multi-cell source rides from an existing backbone
    # cell ABUTTING its output cell (which the input-tap backbone often already passes), and
    # only when NONE exists is a minimal OUTPUT-TAP EXTENSION threaded, or the source is
    # surfaced as output-boxed. Adding output cells to the main tap set destabilised the
    # greedy thread (it produced disordered backbones that failed FORWARD order for the
    # INPUT taps too); keeping the input-tap thread intact preserves the proven routing.
    taps = sorted(tap_in.items(), key=lambda kv: serp_key(kv[0]))
    # Per-filament flow-order repair (stable): within each filament, its taps must be in
    # ``ord`` (pos) order; cross-filament interleave from the serpentine sweep is kept.
    by_fil: dict = {}
    for i, (cell, meta) in enumerate(taps):
        by_fil.setdefault(meta["ord"][0], []).append(i)
    for _f, idxs in by_fil.items():
        members = sorted((taps[i] for i in idxs), key=lambda kv: kv[1]["ord"][1])
        for slot, member in zip(idxs, members):
            taps[slot] = member

    # Thread ONE contiguous SIMPLE backbone: input port → (a free cell abutting each
    # tap's input cell, in tap order) → output port. Each cell appears once → one travel
    # direction per cell → ``crossover_plan`` empty by construction.
    #
    # ROBUSTNESS (the §ROUTING_TOPOLOGIES bus threading): a naive shortest-path per
    # segment WALLS the array — a straight path across open space cuts the free region
    # in two, stranding a later tap on the far side (the modem's gardner/slicer after the
    # backbone threads to costas). Two guards keep the free region connected so a clean
    # backbone is found whenever one EXISTS:
    #   1. WALL-HUGGING cost — prefer cells adjacent to obstacles / the committed
    #      backbone / the array border (and the spine), so the path clings to walls
    #      rather than slicing open space.
    #   2. CONNECTIVITY guard — a candidate segment is accepted only if, after
    #      committing it, EVERY remaining tap-abutting cell AND the output port stay
    #      reachable from the new head over the still-free cells. A segment that would
    #      disconnect a later goal is rejected; the next candidate / abutting cell is
    #      tried. If none keep connectivity, the whole thread BAILS (→ legacy fallback).
    # When a design's dataflow genuinely has no simple-path backbone (e.g. a duplex Y:
    # two filaments forking at the shared input port and reconverging at the shared
    # output port — no single simple path can visit both filaments in flow order and end
    # at the shared sink), no ordering threads and v2 soundly returns None.
    spine_set = {tuple(p) for p in spine if in_bounds(tuple(p))}

    # CLEAR LANES — fully-free rows / columns (no block cell). The compact placer leaves
    # ``channel_reserve`` free rows BETWEEN block bands and the egress column clear; those
    # are the natural backbone corridors. Threading ALONG a clear lane keeps the free
    # region connected (it never bisects an open row), so prefer lane cells in the path
    # cost. Wall-hugging alone FAILS here: an open lane row has wallness 0 (no adjacent
    # obstacle) → wall-hugging de-prioritises exactly the lanes the serpentine wants, and
    # the path descends a column instead, walling a later tap (the modem's mf→costas
    # descent eating the row gardner needs). Lane preference fixes that.
    _clear_rows = {y for y in range(H)
                   if not any((x, y) in block_cells for x in range(W))}
    _clear_cols = {x for x in range(W)
                   if not any((x, y) in block_cells for y in range(H))}

    def _is_lane(c):
        return c[1] in _clear_rows or c[0] in _clear_cols

    def _wallness(c, occ_set):
        """How wall-adjacent ``c`` is: count of off-grid/occupied orthogonal
        neighbours. Higher = clings to a wall → a path that prefers it keeps the
        interior free region connected (no plane cut)."""
        n = 0
        for dx, dy in _NEI:
            nb = (c[0] + dx, c[1] + dy)
            if not in_bounds(nb) or nb in occ_set:
                n += 1
        return n

    def bfs(src, goal, blocked, wall_occ, avoid=None):
        if src == goal:
            return [src]
        import heapq
        avoid = avoid or set()
        seen = {src}
        pq = [(0, 0, src, [src])]
        tie = 1
        while pq:
            cost, _, cur, path = heapq.heappop(pq)
            for dx, dy in _NEI:
                nxt = (cur[0] + dx, cur[1] + dy)
                if nxt in seen or not in_bounds(nxt):
                    continue
                if nxt != goal and nxt in blocked:
                    continue
                seen.add(nxt)
                np_ = path + [nxt]
                if nxt == goal:
                    return np_
                # Cost: prefer the spine (0), then a CLEAR LANE cell (1) — a fully-free
                # row/col the serpentine rides without bisecting an open row — then
                # wall-hugging (4 - wallness) for the rest. A big penalty steers the
                # connecting segment OUT of ``avoid`` cells (the interior region a LATER
                # tap needs) so it does not wall that tap off — the connectivity-aware
                # path that lets the backtracking thread succeed.
                if nxt in spine_set:
                    step = 0
                elif _is_lane(nxt):
                    step = 1
                else:
                    step = 2 + (4 - _wallness(nxt, wall_occ))
                if nxt in avoid:
                    step += 50
                heapq.heappush(pq, (cost + step, tie, nxt, np_))
                tie += 1
        return None

    def reachable(src, goal, blocked):
        from collections import deque as _dq
        if src == goal:
            return True
        seen = {src}
        q = _dq([src])
        while q:
            cur = q.popleft()
            for dx, dy in _NEI:
                n = (cur[0] + dx, cur[1] + dy)
                if n == goal:
                    return True
                if n in seen or not in_bounds(n) or n in blocked:
                    continue
                seen.add(n)
                q.append(n)
        return False

    def bfs_paths(src, goal, blocked, wall_occ, limit=6, avoid=None):
        """Up to ``limit`` DISTINCT simple paths src→goal over free cells, shortest /
        wall-hugging first. Used by the backtracking tap thread so a tap that strands a
        later goal on its FIRST (greedy) path can be retried on an ALTERNATIVE path
        (e.g. hug the far wall and leave the near row open). The CONNECTIVITY-AWARE
        variant (``avoid`` = the interior cells later taps need) is tried first so the
        connecting segment steers around the region later taps live in; then plain
        variants forbidding one interior cell of an already-yielded path at a time —
        cheap on this tiny array, and enough variety to escape the single-path wall."""
        paths = []
        seen_sig = set()
        if avoid:
            ap = bfs(src, goal, blocked, wall_occ, avoid=avoid)
            if ap is not None:
                paths.append(ap)
                seen_sig.add(tuple(ap))
        first = bfs(src, goal, blocked, wall_occ)
        if first is None:
            return paths
        if tuple(first) not in seen_sig:
            paths.append(first)
            seen_sig.add(tuple(first))
        # Re-search forbidding one interior cell of an existing path at a time, to
        # surface genuinely different routes (a detour around the cell that walls a
        # later goal). Bounded by ``limit`` so the DFS stays fast.
        i = 0
        while len(paths) < limit and i < len(paths):
            base = paths[i]
            i += 1
            for mid in base[1:-1]:
                alt = bfs(src, goal, blocked | {mid}, wall_occ)
                if alt is not None:
                    sig = tuple(alt)
                    if sig not in seen_sig:
                        seen_sig.add(sig)
                        paths.append(alt)
                        if len(paths) >= limit:
                            break
        return paths

    backbone = [in_port_cell]
    occ = set(block_cells)
    occ.discard(in_port_cell)
    tap_abut: dict = {}               # in_cell -> the backbone cell that taps it
    # The egress corridor: the column the OUTPUT port sits on, kept clear of mid-snake
    # backbone so the terminal climb to the port is never walled off (only its FREE cells
    # — block/port cells aren't reservable). Empty unless the port is a clean edge column.
    _EGRESS_COL = {(out_port_cell[0], ry) for ry in range(H)
                   if (out_port_cell[0], ry) not in block_cells
                   and (out_port_cell[0], ry) != in_port_cell}

    def _abut_free(in_cell, occ_now):
        return [(in_cell[0] + dx, in_cell[1] + dy) for dx, dy in _NEI
                if in_bounds((in_cell[0] + dx, in_cell[1] + dy))
                and (in_cell[0] + dx, in_cell[1] + dy) not in block_cells]

    if _DBG:
        print("[bus_v2] tap order:", [(ic, m["block"]) for ic, m in taps])
        print("[bus_v2] in_port", in_port_cell, "out_port", out_port_cell)

    # EXPLICIT-BACKBONE FAST PATH (the bus-snake co-design): when the placement supplies
    # an ORDERED, CONTIGUOUS spine path (the bus-snake placer hands the exact serpentine,
    # doc/ROUTING_TOPOLOGIES.md), ride it VERBATIM rather than re-deriving a backbone by a
    # myopic greedy per-tap thread (which can wall itself in this 2-D maze). The spine is
    # accepted only if it is a clean SIMPLE PATH (orthogonal steps, no repeats, no block
    # cell) from the input port to the output port that ABUTS every tap's input cell — so
    # crossover stays empty by construction (each cell once, one travel direction). On any
    # mismatch we fall through to the robust greedy thread (unchanged).
    def _ordered_spine_backbone():
        seq = [tuple(p) for p in spine]
        path: list = []
        for c in seq:                                  # dedupe consecutive repeats
            if not in_bounds(c):
                return None
            if not path or path[-1] != c:
                path.append(c)
        if len(path) < 2 or path[0] != in_port_cell or path[-1] != out_port_cell:
            return None
        seen = set()
        for i, c in enumerate(path):
            if c in seen:                              # not simple
                return None
            seen.add(c)
            if c in block_cells and c not in (in_port_cell, out_port_cell):
                return None
            if i > 0:
                px, py = path[i - 1]
                if abs(px - c[0]) + abs(py - c[1]) != 1:   # not orthogonally contiguous
                    return None
        idx = {c: i for i, c in enumerate(path)}
        abut: dict = {}
        for in_cell, _m in taps:
            on = [a for a in _abut_free(in_cell, occ) if a in idx]
            if not on:
                return None
            # Tap at the EARLIEST backbone cell abutting this input — the block's input
            # cell may also touch a LATER backbone cell (e.g. the egress rail running past
            # it), and tapping there would put the block's source downstream of its own
            # consumer. The earliest abut is the lane the snake taps it from on the way IN.
            abut[in_cell] = min(on, key=lambda a: idx[a])
        return path, abut

    osb = _ordered_spine_backbone()
    use_explicit = osb is not None
    if use_explicit:
        backbone, tap_abut = list(osb[0]), dict(osb[1])
        if _DBG:
            print(f"[bus_v2] using explicit spine backbone ({len(backbone)} cells)")

    # Per-tap PREFERRED abut cell from the placer's ordered spine (even when the full
    # spine isn't a perfect simple path): the spine cell directly adjacent to each input
    # cell. The greedy thread prefers it so the backbone clings to the intended snake lane
    # (not an arbitrary equal-cost neighbour), making the clean co-designed thread succeed.
    _spine_seq = [tuple(p) for p in spine if in_bounds(tuple(p))]
    _spine_pos = {}
    for _i, _c in enumerate(_spine_seq):
        _spine_pos.setdefault(_c, _i)
    pref_abut: dict = {}
    for _ic, _m in taps:
        _adj = [a for a in _abut_free(_ic, occ) if a in _spine_pos]
        if _adj:
            # The spine cell adjacent to this input that appears EARLIEST on the ordered
            # spine (the lane the snake taps this block from on its forward sweep).
            pref_abut[_ic] = min(_adj, key=lambda a: _spine_pos[a])

    # BACKTRACKING TAP THREAD (the §ROUTING_TOPOLOGIES robustness fix). The greedy
    # one-candidate-per-tap thread BAILS when its committed segment walls a later tap
    # (the modem's costas tap walls gardner): the 1-step connectivity guard correctly
    # REJECTS every costas candidate, but with no backtracking the whole thread dies.
    # Here we DFS over (abut-cell, path) choices per tap and BACKTRACK when a tap can't
    # be threaded — trying alternative PATHS for THIS tap (``bfs_paths`` hugs the far
    # wall, leaving the near row open) and, failing that, alternative choices for the
    # PREVIOUS tap. The connectivity guard is kept (a candidate is accepted only if every
    # later tap + the output port stays reachable), so the kept thread is a clean simple
    # path → ``crossover_plan`` empty by construction. Bounded (few taps, few paths each)
    # so it stays fast. The explicit-spine fast path above skips this entirely.
    thread_taps = [] if use_explicit else taps
    remaining_in = [ic for ic, _m in taps]

    # Single-cell terminal blocks that DRIVE the chip output port (e.g. the modem's
    # slicer at the bottom-right corner). Such a block both RECEIVES its bus input and
    # DRIVES its egress on its ONE cell; if its input tap sits on the OUTPUT-PORT COLUMN
    # (the egress climb) the input and the egress share a face → the §5.3 single-cell
    # deadlock the build rejects. Tapping its input from a DIFFERENT face (off the egress
    # column) keeps input-face != output-face. Map: their input cell -> the egress column.
    _sc_egress_in: dict = {}
    for blk in project.blocks:
        pl = blk.placement
        if pl is None or pl.chip != chip_id or len(pl.cells) != 1:
            continue
        c0 = (pl.cells[0].x, pl.cells[0].y)
        drives_out = any(d == out_port_cell and s == c0
                         for (_n, s, _sf, d, _df, _sp, dp_, _c) in nets if dp_)
        if drives_out:
            _sc_egress_in[c0] = out_port_cell[0]

    def _tap_candidates(ti, head, bb_set, occ_now):
        """Ordered (abut_cell, path) candidates for tap ``ti`` from ``head``. An abut on
        an EXISTING backbone cell needs no new path (reuse); otherwise enumerate paths
        (``bfs_paths``). Preference: reuse first, then the placer's lane abut, then spine,
        then shortest wall-hugging path."""
        in_cell = thread_taps[ti][0]
        cands = _abut_free(in_cell, occ_now)
        pref = pref_abut.get(in_cell)
        wall_occ = set(block_cells) | bb_set
        blocked = occ_now | (bb_set - {head})
        # AVOID region: the abut cells of every LATER tap + the output port. A connecting
        # segment that ploughs through them walls the later tap off (the modem's mf→costas
        # descent eating the row gardner needs); steering around them keeps the free region
        # connected so the backtracking thread completes. The tap's OWN abut cells are
        # never avoided (they are the goal).
        avoid = set()
        for lic in remaining_in[ti + 1:]:
            avoid.update(_abut_free(lic, occ_now))
        avoid.update(_abut_free(out_port_cell, occ_now))
        avoid.add(out_port_cell)
        # Keep the EGRESS column clear of tap-connecting segments so the terminal climb to
        # the output port is never walled (and a single-cell egress terminal keeps its
        # output-face = the egress column, distinct from its side input tap). Excludes the
        # tap's own abut cells (the goal).
        avoid.update(c for c in _EGRESS_COL if c not in cands)
        # A single-cell terminal that egresses (the slicer): deprioritise an input tap on
        # the egress column (it would share the slicer's output face → §5.3 deadlock at
        # build); a side-face tap lets the backbone bend at the block (input from the side,
        # output up the egress column = the §5.3 split). Kept SOFT (a tie-break, not an
        # exclusion) so a tight corner where no side tap reaches still routes.
        egress_col = _sc_egress_in.get(in_cell)
        # FLOW BIAS: among candidate abut cells, prefer the one that PROGRESSES toward the
        # next tap (smaller Manhattan distance to the next tap's input cell). This keeps the
        # backbone visiting blocks in FLOW order (producer before consumer) so a multi-cell
        # source's OUTPUT cell sits UPSTREAM of its consumer on the bus — without it the
        # greedy thread can descend a near wall first and visit the consumer (iqupconvert)
        # BEFORE the producer (rrc), leaving the producer's output stranded downstream
        # (net5 0-output). A SECONDARY key (after reuse/spine/wall-pref) so it never
        # overrides a forced or co-designed route — it only orders otherwise-equal choices.
        next_in = thread_taps[ti + 1][0] if ti + 1 < len(thread_taps) else out_port_cell

        def _to_next(cell):
            return abs(cell[0] - next_in[0]) + abs(cell[1] - next_in[1])
        out = []
        for c in cands:
            sc_pen = 1 if (egress_col is not None and c[0] == egress_col) else 0
            if c in bb_set:
                out.append(((0, sc_pen, 0, 0, 0), c, [c]))   # reuse: no new segment
                continue
            for p in bfs_paths(head, c, blocked, wall_occ,
                               avoid=avoid - {c}):
                prefer = 0 if c == pref else 1
                lane = 0 if c in spine_set else 1
                out.append(((1, sc_pen, prefer, _to_next(c), lane * 100 + len(p)),
                            c, p))
        out.sort(key=lambda t: t[0])
        return out

    def _dfs(ti, backbone, occ, tap_abut):
        """Thread taps ``ti..`` onto ``backbone``; return (backbone, tap_abut) on the
        first complete thread that keeps every later tap + the output port reachable, or
        None (backtrack)."""
        if ti >= len(thread_taps):
            return backbone, tap_abut
        in_cell = thread_taps[ti][0]
        head = backbone[-1]
        bb_set = set(backbone)
        later = remaining_in[ti + 1:]
        if _DBG:
            print(f"[bus_v2] tap#{ti} in={in_cell} head={head} "
                  f"cands={_abut_free(in_cell, occ)}")
        for _rank, c, p in _tap_candidates(ti, head, bb_set, occ):
            new_head = c
            new_occ = occ | set(p[1:])
            blk = new_occ | ((bb_set | set(p[1:])) - {new_head})
            # Connectivity guard: every LATER tap abut + the output port must stay
            # reachable from the new head over the still-free cells.
            ok = True
            for lic in later:
                if not any(reachable(new_head, ac, blk)
                           for ac in _abut_free(lic, new_occ)):
                    ok = False
                    if _DBG:
                        print(f"[bus_v2]   cand {c} strands later tap {lic}")
                    break
            if ok and out_port_cell != new_head \
                    and not reachable(new_head, out_port_cell, blk):
                ok = False
                if _DBG:
                    print(f"[bus_v2]   cand {c} cannot reach out_port {out_port_cell}")
            if not ok:
                continue
            nb = list(backbone)
            for cell in p[1:]:
                nb.append(cell)
            nta = dict(tap_abut)
            nta[in_cell] = c
            res = _dfs(ti + 1, nb, new_occ, nta)
            if res is not None:
                return res
        if _DBG:
            print(f"[bus_v2] backtrack at tap {in_cell} head={head}")
        return None

    # The LAST tap is a single-cell EGRESS terminal (the slicer) when it drives the output
    # port: thread it specially (a coupled input-tap + egress BEND) AFTER the rest, so its
    # bus input and its egress leave on DIFFERENT faces (the §5.3 split). Pull it off the
    # normal DFS; thread taps 0..n-2 first.
    egress_terminal = None
    if thread_taps and thread_taps[-1][0] in _sc_egress_in:
        egress_terminal = thread_taps[-1][0]
        thread_taps = thread_taps[:-1]
        remaining_in = remaining_in[:-1]

    if thread_taps:
        threaded = _dfs(0, backbone, occ, tap_abut)
        if threaded is None:
            return _bail("cannot thread backbone to all taps (backtrack exhausted)")
        backbone, tap_abut = list(threaded[0]), dict(threaded[1])
        occ = set(block_cells)
        occ.discard(in_port_cell)
        occ.update(backbone[1:])

    if egress_terminal is not None:
        # Build the §5.3 bend: head → INPUT tap (a free face-neighbour of the terminal) →
        # OUTPUT cell (a DIFFERENT free face-neighbour) → egress to the output port. The
        # terminal then delivers its bus input on the input-tap face and drives its egress
        # on the output-cell face (distinct faces, no single-link contention). We try every
        # (input, output) ordered pair of the terminal's free face-neighbours and keep the
        # first whose two segments both route. ``tap_abut[egress_terminal]`` = the input
        # tap (sc_out_tap below then finds the output cell as the §5.3 second tap).
        et = egress_terminal
        nbrs = [(et[0] + dx, et[1] + dy) for dx, dy in _NEI
                if in_bounds((et[0] + dx, et[1] + dy))
                and (et[0] + dx, et[1] + dy) not in (set(backbone) | block_cells)]
        head = backbone[-1]
        done = False
        for in_tap in sorted(nbrs, key=lambda c: (0 if c[0] != et[0] else 1)):
            for out_cell in nbrs:
                if out_cell == in_tap:
                    continue
                wall = set(block_cells) | set(backbone)
                seg_in = bfs(head, in_tap, occ | (set(backbone) - {head}), wall)
                if seg_in is None:
                    continue
                occ2 = occ | set(seg_in[1:])
                bb2 = set(backbone) | set(seg_in[1:])
                # out_cell must be reachable from in_tap WITHOUT reusing it, and reach port.
                seg_mid = bfs(in_tap, out_cell, occ2 | (bb2 - {in_tap}), wall | bb2)
                if seg_mid is None:
                    continue
                occ3 = occ2 | set(seg_mid[1:])
                bb3 = bb2 | set(seg_mid[1:])
                seg_out = bfs(out_cell, out_port_cell, occ3 | (bb3 - {out_cell}),
                              wall | bb3)
                if seg_out is None:
                    continue
                for c in seg_in[1:]:
                    backbone.append(c); occ.add(c)
                tap_abut[et] = in_tap
                for c in seg_mid[1:]:
                    backbone.append(c); occ.add(c)
                for c in seg_out[1:]:
                    backbone.append(c); occ.add(c)
                done = True
                break
            if done:
                break
        if not done:
            return _bail(f"cannot thread egress-terminal bend at {et}")

    # MULTI-CELL EGRESS TERMINALS: after the input taps are threaded, append a DEDICATED
    # egress segment per multi-cell terminal (backbone head → a free cell ABUTTING its
    # output cell → output port). This puts an output-tap cell (abutting the buried output
    # cell) NEAR the end of the backbone, so ``src_tap_cell`` rides a SHORT tail slice from
    # it to the port instead of the whole serpentine. Threaded before the final port thread
    # so the last one leaves the head at (or near) the output port. Kept ROBUST: if a
    # terminal's bend can't route it is skipped (``src_tap_cell`` then falls back — never a
    # crash), and multiple terminals chain head→oc_abut→...→port in turn.
    for oc, (_bname, _bport) in mc_egress_out.items():
        head = backbone[-1]
        bbset = set(backbone)
        oc_nbrs = [(oc[0] + dx, oc[1] + dy) for dx, dy in _NEI
                   if in_bounds((oc[0] + dx, oc[1] + dy))
                   and (oc[0] + dx, oc[1] + dy) not in (bbset | block_cells)]
        wall = set(block_cells) | bbset
        placed = False
        for oc_abut in oc_nbrs:
            seg_in = bfs(head, oc_abut, occ | (bbset - {head}), wall)
            if seg_in is None:
                continue
            occ2 = occ | set(seg_in[1:])
            bb2 = bbset | set(seg_in[1:])
            seg_out = bfs(oc_abut, out_port_cell, occ2 | (bb2 - {oc_abut}), wall | bb2)
            if seg_out is None:
                continue
            for c in seg_in[1:]:
                backbone.append(c); occ.add(c)
            for c in seg_out[1:]:
                backbone.append(c); occ.add(c)
            placed = True
            break
        if _DBG and not placed:
            print(f"[bus_v2] multi-cell egress terminal at {oc} could not thread bend "
                  f"head={head} oc_nbrs={oc_nbrs}")

    if out_port_cell != backbone[-1]:
        blocked = occ | (set(backbone) - {backbone[-1]})
        seg = bfs(backbone[-1], out_port_cell, blocked,
                  set(block_cells) | set(backbone))
        if seg is None:
            return _bail("cannot thread backbone to output port")
        for c in seg[1:]:
            backbone.append(c)
            occ.add(c)

    bb_index = {c: i for i, c in enumerate(backbone)}
    # Single-cell bus-fed target INPUT cells (the §5.3 in==out hazard): a single-cell
    # block whose input is delivered by a broker (not a direct port injection at its own
    # cell). It needs a SECOND backbone tap (a bend) so input-face != output-face.
    sc_targets: set = set()
    for blk in project.blocks:
        pl = blk.placement
        if pl is None or pl.chip != chip_id or len(pl.cells) != 1:
            continue
        c0 = (pl.cells[0].x, pl.cells[0].y)
        # bus-fed: some net targets it and is NOT a direct port→own-cell injection
        for (_n, s, _sf, d, _df, sp_, _dp, _c) in nets:
            if d == c0 and not (sp_ and s == d):
                sc_targets.add(c0)
                break

    # Single-cell bus-fed blocks that ALSO drive a block→block output (e.g. the modem's
    # PSK mapper): the §5.3 split needs input-face != output-face. The DFS may have tapped
    # the input on the cell the OUTPUT also wants (the flow-bias forward tap), leaving no
    # downstream cell for the output → input-face == output-face deadlock. When the block
    # abuts an EARLIER backbone cell on a DIFFERENT face, REASSIGN its input tap to that
    # earlier cell so the original (downstream) cell is free to serve the output drive (the
    # §5.3 split below then finds it). Only for single-cell blocks that source a net.
    sc_source_cells: set = set()
    for (_n, _s, _sf, _d, _df, _sp, _dp, c) in nets:
        if not isinstance(c.source, BlockEndpoint):
            continue
        sb = project.block(c.source.block)
        if sb is not None and sb.placement is not None \
                and len(sb.placement.cells) == 1:
            sc_source_cells.add((sb.placement.cells[0].x, sb.placement.cells[0].y))
    for c0 in sc_targets & sc_source_cells:
        in_tap = tap_abut.get(c0)
        if in_tap is None:
            continue
        adj = [(c0[0] + dx, c0[1] + dy) for dx, dy in _NEI
               if (c0[0] + dx, c0[1] + dy) in bb_index]
        if len(adj) < 2:
            continue
        earliest = min(adj, key=lambda a: bb_index[a])
        # If the input is tapped LATER than an available earlier-face cell, move it earlier
        # so a downstream cell remains for the output (input from upstream, output drives
        # downstream — distinct faces).
        if bb_index[in_tap] > bb_index[earliest] \
                and _cardinal(c0, earliest) != _cardinal(c0, in_tap):
            tap_abut[c0] = earliest

    # SINGLE-CELL in==out split (§5.3) via a SECOND backbone tap. A single-cell block
    # both receives and drives on its ONE cell; if both rode the same lane tap they would
    # share a face (deadlock). When the block sits at a backbone BEND it abuts TWO backbone
    # cells on DIFFERENT faces — deliver its INPUT from the earlier one (kept as its tap)
    # and emit its OUTPUT from the later one, so the build sees input-face != output-face.
    # Computed only for single-cell blocks that actually abut a second backbone cell.
    sc_out_tap: dict = {}            # block cell -> the OUTPUT (second) backbone tap
    for c0 in sc_targets:
        in_tap = tap_abut.get(c0)
        if in_tap is None:
            continue
        in_i = bb_index[in_tap]
        # A NEARBY downstream backbone cell on a DIFFERENT face (the inside corner of a
        # bend, Δindex small) — the block's OUTPUT tap. "Nearby" so it stays upstream of
        # the block's consumer; a far cell (the egress rail running past the block) is
        # NOT used (that would put the output downstream of its own consumer).
        seconds = [(c0[0] + dx, c0[1] + dy) for dx, dy in _NEI
                   if (c0[0] + dx, c0[1] + dy) in bb_index
                   and (c0[0] + dx, c0[1] + dy) != in_tap
                   and _cardinal(c0, (c0[0] + dx, c0[1] + dy)) != _cardinal(c0, in_tap)
                   and 1 <= bb_index[(c0[0] + dx, c0[1] + dy)] - in_i <= 2]
        if seconds:
            sc_out_tap[c0] = min(seconds, key=lambda c: bb_index[c])

    def src_tap_cell(conn, i_to_limit=None):
        """The backbone tap cell the SOURCE block of a block→block net rides from. None
        for a head. Preference order:
          1. The block's dedicated OUTPUT-cell tap (the free cell abutting its output
             cell, threaded as an output tap above) — for a MULTI-CELL snake the word
             leaves the FAR output cell, so the build must hop exit→THAT tap @1. Without
             it the source taps its input cell, many cells upstream of the output, and
             the burst lands short (net5/net10/net4/net11 0-output). Used only if it stays
             UPSTREAM of the consumer (``i_to_limit``).
          2. A single-cell source's §5.3 second-face OUTPUT tap (the bend cell).
          3. The block's input-cell tap (a single-cell block sits beside the backbone)."""
        sb = project.block(conn.source.block)
        if sb is None or sb.placement is None:
            return None
        bcells = {(c.x, c.y) for c in sb.placement.cells}
        # 1. dedicated OUTPUT-cell tap (multi-cell snake): a backbone cell ABUTTING the
        #    block's OUTPUT cell for THIS port. Among all backbone cells adjacent to the
        #    output cell, pick the EARLIEST (smallest bb_index) that stays UPSTREAM of the
        #    consumer (``i_to_limit``) — the threaded ``tap_abut[oc]`` is one such cell,
        #    but a path cell of an earlier segment may abut the output cell at a LOWER
        #    index (the source should ride from the earliest one so it stays upstream of
        #    its consumer). A boxed output (no free neighbour) was never tapped → skip.
        oc = out_cell_of(sb, conn.source.port)
        if oc not in bcells or len(bcells) > 1:
            adj = [(oc[0] + dx, oc[1] + dy) for dx, dy in _NEI
                   if (oc[0] + dx, oc[1] + dy) in bb_index]
            # For a multi-cell EGRESS TERMINAL (drives the chip output port directly) the
            # egress should ride from the DEDICATED tail tap threaded ABUTTING its output
            # cell near the port — pick the LATEST adjacent backbone cell ≤ i_to (the port)
            # so the egress is a SHORT tail slice, not the whole serpentine from an early
            # abutting cell. For a block→block source, keep the EARLIEST (stay upstream of
            # the consumer).
            is_egress_terminal = (isinstance(conn.target, ChipPortEndpoint)
                                  and oc in mc_egress_out)
            cands = sorted((bb_index[a] for a in adj),
                           reverse=is_egress_terminal)
            for oi in cands:
                if i_to_limit is None or oi <= i_to_limit:
                    return backbone[oi]
            # A multi-cell egress terminal with NO backbone tap adjacent to its OUTPUT cell
            # (≤ i_to) cannot ride the egress from its true output cell. Falling back to the
            # input-cell tap (branch 3) would start the egress far upstream of the output —
            # the word never reaches the port (0-output). Return None so the caller marks the
            # net a NAMED failure, escalating the whole design to the maze router, which
            # routes the output cell → port directly (short, node-disjoint).
            if is_egress_terminal:
                return None
        primary = None
        for ic, tcell in tap_abut.items():
            if ic in bcells:
                primary = tcell
                break
        # 2. A single-cell source prefers its dedicated OUTPUT tap (the §5.3 second-face
        #    cell) — but ONLY if it stays UPSTREAM of the consumer (``i_to_limit``).
        for bc in bcells:
            if bc in sc_out_tap:
                oi = bb_index[sc_out_tap[bc]]
                if i_to_limit is None or oi <= i_to_limit:
                    return sc_out_tap[bc]
        return primary

    def _is_boxed_src(conn):
        """True when this net's source block has a multi-cell OUTPUT cell with NO free
        orthogonal neighbour to host an output tap (the Costas case). Such a net cannot
        rejoin the bus; v2 emits a STRUCTURED ``output-boxed:<block>`` failure the Part B
        loop perturbs (re-fold / spread), rather than a dead build."""
        return (isinstance(conn.source, BlockEndpoint)
                and conn.source.block in boxed_outputs)

    out: list[RouteResult] = []
    for (name, s, sface, d, dface, sp, dp, conn) in nets:
        if _is_boxed_src(conn):
            out.append(RouteResult(
                name, False,
                reason=f"output-boxed:{conn.source.block} "
                       f"(multi-cell output cell {boxed_outputs[conn.source.block]} "
                       f"has no free neighbour to tap the bus)"))
            continue
        if dp:
            i_to = bb_index.get(d)
            if i_to is None:
                return _bail(f"egress net {name}: output port not on backbone")
            i_from = 0
            src_out_cell = None
            if isinstance(conn.source, BlockEndpoint):
                st = src_tap_cell(conn, i_to)
                if st is None:
                    # Multi-cell egress terminal whose output cell has no reachable tap on
                    # this backbone — NOT the wrong input-port fallback (0-output). A NAMED
                    # failure escalates the whole design to the maze router, which routes the
                    # output cell → port directly and short.
                    out.append(RouteResult(
                        name, False,
                        reason=f"egress net {name}: source block "
                               f"{conn.source.block} output cell not tappable on the bus"))
                    continue
                i_from = bb_index.get(st, 0)
                # The word LEAVES the source block's OUTPUT cell, then hops @1 to the tap
                # (``st``) it rides from. When the output cell is NOT itself the tap (a
                # multi-cell block whose output cell sits OFF the backbone — the tap is a
                # free neighbour), the route MUST begin at the output cell so it starts at
                # the true emit cell: the build derives the exit hop from ``route[0]``, and
                # if ``route[0]`` were the tap (one cell downstream) every waypoint — and the
                # output-port EDGE cell — would be reached one hop early, executing there
                # instead of transiting out the port (0 egress). Prepend the output cell so
                # ``route[0] == exit_cell`` uniformly (when the output cell already sits ON
                # the slice, ``st == oc`` and no prepend is needed — that is the case that
                # always worked).
                sb = project.block(conn.source.block)
                if sb is not None:
                    src_out_cell = out_cell_of(sb, conn.source.port)
            pts = list(backbone[i_from:i_to + 1])
            # ``backbone`` holds (x, y) tuples; keep ``pts`` homogeneous.
            if src_out_cell is not None and (not pts or tuple(pts[0]) != tuple(src_out_cell)):
                pts = [tuple(src_out_cell)] + pts
            # HOP BUDGET (§1.3): a backbone slice whose delivered distance exceeds the
            # 31-hop budget cannot reach the port — mark it a NAMED failure so the design
            # escalates to the maze router (independent, node-disjoint) rather than a build
            # that trips the hop DRC. For a block-sourced egress the word hops exit→tap then
            # rides the slice and exits the port, i.e. ``len(pts)`` physical hops (matches
            # the build's ``hop_overflow`` count).
            if isinstance(conn.source, BlockEndpoint) and len(pts) > _MAX_HOPS:
                out.append(RouteResult(
                    name, False,
                    reason=f"egress net {name}: bus slice is {len(pts)} hops "
                           f"(max {_MAX_HOPS}) — escalate to maze"))
                continue
            out.append(RouteResult(name, True, points=pts))
            continue
        if sp and s == d:
            out.append(RouteResult(name, True, points=[d]))   # vestigial (dropped)
            continue
        if not isinstance(conn.target, BlockEndpoint):
            return _bail(f"net {name}: non-block, non-port target")
        blk = project.block(conn.target.block)
        ic = in_cell_of(blk, conn.target.port)
        tap = tap_abut.get(ic)
        i_to = bb_index.get(tap)
        if i_to is None:
            return _bail(f"net {name}: target tap not on backbone")
        if sp:
            i_from = bb_index.get(in_port_cell, 0)
        else:
            st = src_tap_cell(conn, i_to)
            i_from = bb_index.get(st)
            if i_from is None:
                i_from = bb_index.get(in_port_cell, 0)
        if i_from > i_to:
            if _DBG:
                print(f"[bus_v2] net {name} i_from={i_from} ({src_tap_cell(conn)}) "
                      f"i_to={i_to} (tap {tap}, ic {ic}) src={conn.source} tgt={conn.target}")
            return _bail(f"net {name}: source tap downstream of target on backbone")
        pts = list(backbone[i_from:i_to + 1])
        out.append(RouteResult(name, True, points=pts))
    return out


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


def _route_chip_bus(project, ct, chip_id, nets, spine, *, sc_cells=None,
                    forbid_broker_transit=False):
    """Construct the bus on one chip and route each net source→bus→broker.

    ``forbid_broker_transit`` (the BUS / v2 topology) forbids ANY net from TRANSITING
    a foreign broker cell — the exact one-cell-two-roles corruption (a broker that also
    forwards a different filament's word in a conflicting direction) the bus topology
    eliminates by construction. Default False keeps the legacy behaviour byte-identical.

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
        if (conn.is_routed and not conn.is_abutment
                and _conn_chip(project, conn) == chip_id):
            # ABUTMENT nets have no corridor cells — they occupy nothing.
            occ.update((p.x, p.y) for p in conn.route)

    # Chip PORT edge cells. A port cell is a DEDICATED I/O terminus: a net owning it
    # (its src or goal) uses it, but no OTHER net may TRANSIT it — the build faces a
    # port-egress cell toward the port exit, clobbering any transit face routed through
    # it (the rotated complex fan-in that snaked THROUGH x16_out and lost both operands).
    port_cells = {(p.cell_x, p.cell_y) for p in getattr(ct, "ports", [])}

    spine_set = {tuple(p) for p in spine if in_bounds(tuple(p))}
    bus: set = set()                  # cells already carrying the bus (preferred)
    # The committed OUTGOING direction of each bus cell. A cell has ONE fwd_face
    # (§1.3), so a net may only RE-USE a bus cell if it leaves it in the SAME
    # direction; otherwise that cell is an obstacle for this net (it must route
    # disjointly). This is what keeps a shared bus segment SOUND — both streams
    # leave a shared cell the same way; they peel off at their own brokers.
    bus_dir: dict = {}
    brokers: set = set()              # cells programmed as a broker (their fwd is bus)
    # Each broker delivers into ONE target input cell (the cell it abuts + flips toward).
    # A broker may only be REUSED for a net whose target is the SAME cell (a true FAN-IN,
    # e.g. the Costas phase cell's xi + xq). Two nets to DIFFERENT target cells must NOT
    # share a broker: a single cell has ONE fwd_face (§1.3), so it cannot flip toward two
    # different neighbours — the modem's TX-mapper and RX-matched-filter input nets both
    # abut one free cell and would otherwise coalesce onto it, colliding (net8 0-corr).
    broker_target: dict = {}          # broker (x, y) -> target input cell it serves
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
            reuse = _broker_abutting(d, dface, brokers, s, forbid_broker,
                                     broker_target)
            # A PORT→block INPUT net (the host injects the burst) should NOT broker off
            # a cell already carrying ANOTHER input stream's corridor: the host word
            # would have to ride that shared corridor and DIVERT at the foreign broker,
            # landing one operand at the wrong cell (the modem's TX-mapper net riding the
            # RX corridor to a shared broker → corrupted symbols). Prefer a FRESH broker
            # cell (off the existing bus) so each input stream gets its own clean tap.
            avoid_bus = src_is_port
            goal = reuse if reuse is not None else \
                _free_neighbor(d, dface, occ, bus, spine_set, in_bounds, s,
                               forbid_broker, broker_target, avoid_bus=avoid_bus)
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

        # Reserve every FOREIGN chip port cell (a port cell that is not THIS net's own
        # source or goal): no net may thread its corridor through another net's I/O
        # terminus (the port-egress face would clobber the transit face).
        forbid_transit = {pc for pc in port_cells if pc != s and pc != goal}

        path = _bus_bfs(s, sface, goal, occ, bus, spine_set, in_bounds,
                        src_is_port, bus_dir=bus_dir, brokers=brokers,
                        forbid_first=forbid_first,
                        forbid_broker_transit=forbid_broker_transit,
                        forbid_transit=forbid_transit)
        if path is None and goal_is_broker:
            # The chosen broker is walled (its only approaches are committed the wrong
            # way). Try the OTHER free neighbours of the target as broker taps before
            # giving up — a packed fan-in may need a different abutment face.
            for alt in _free_neighbors_all(d, dface, occ, in_bounds, s,
                                           forbid_broker, broker_target):
                if alt == goal:
                    continue
                alt_forbid = {pc for pc in port_cells if pc != s and pc != alt}
                path = _bus_bfs(s, sface, alt, occ, bus, spine_set, in_bounds,
                                src_is_port, bus_dir=bus_dir, brokers=brokers,
                                forbid_first=forbid_first,
                                forbid_broker_transit=forbid_broker_transit,
                                forbid_transit=alt_forbid)
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
            broker_target[path[-1]] = d   # the target input cell this broker serves
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
                   forbid_out=None, broker_target=None, avoid_bus=False):
    """A free cell abutting ``cell`` (the target input) to host the broker.

    A delivery may arrive on any face EXCEPT the target's own emit (``in_face``) face
    (§7.4) and — for a single-cell hazard cell — its committed OUTPUT face
    (``forbid_out``). Prefers a cell already on the bus / spine (coalesce), then any
    free neighbour; never the source cell itself. When ``forbid_out`` is set (the
    hazard case) it instead prefers a QUIET free neighbour OFF the bus/spine and the
    calmest corner, so the input feed never competes with through-traffic on the
    hazard cell's single link.

    ``avoid_bus`` (a PORT→block input net) INVERTS the coalesce preference: a fresh
    cell OFF the existing bus is preferred so two host-injected input streams don't
    share one broker corridor (which would force one to divert at the other's broker,
    landing the operand at the wrong cell — the modem's TX-mapper-on-RX-corridor bug).

    A cell already serving as a broker for a DIFFERENT target (per ``broker_target``)
    is excluded: it has ONE fwd_face flipping toward its own target and cannot also
    deliver into this cell (the two distinct-sink streams would collide on it)."""
    forbid = _broker_forbidden(in_face, forbid_out)
    cands = []
    for code, (dx, dy) in _FWD_DELTA.items():
        if (dx, dy) in forbid:
            continue
        n = (cell[0] + dx, cell[1] + dy)
        if not in_bounds(n) or n in occ or n == src or n == cell:
            continue
        if broker_target is not None and broker_target.get(n) not in (None, cell):
            continue  # already a broker delivering to a different target
        if avoid_bus:
            # PORT input net: PREFER a fresh cell off the bus/spine (0), then spine (1),
            # then a busy bus cell last (2) — the opposite of the coalescing default.
            rank = (2 if n in bus else (1 if n in spine_set else 0), 0)
        elif forbid_out is not None:
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


def _free_neighbors_all(cell, in_face, occ, in_bounds, src, forbid_out=None,
                        broker_target=None):
    """All free neighbours of ``cell`` that may host a broker (any face except the
    target's own emit face, and a single-cell hazard cell's committed output face), in
    no particular order — the fallback set when the preferred broker tap is walled. A
    cell already brokering a DIFFERENT target (``broker_target``) is excluded (it has one
    fwd_face toward its own target; two distinct sinks cannot share it)."""
    forbid = _broker_forbidden(in_face, forbid_out)
    res = []
    for c, (dx, dy) in _FWD_DELTA.items():
        if (dx, dy) in forbid:
            continue
        n = (cell[0] + dx, cell[1] + dy)
        if in_bounds(n) and n not in occ and n != src and n != cell:
            if broker_target is not None and broker_target.get(n) not in (None, cell):
                continue
            res.append(n)
    return res


def _broker_abutting(cell, in_face, brokers, src, forbid_out=None,
                     broker_target=None):
    """An EXISTING broker cell abutting the target input ``cell`` (a FAN-IN reuse:
    a second net into the same input cell rides the broker already there, which then
    grows a deliver entry per net). Returns that broker cell or None. Excludes the
    target's own emit face, a single-cell hazard cell's committed output face, and the
    source cell.

    A broker may be reused ONLY when it already serves the SAME target input ``cell`` (a
    true fan-in — two streams into one cell, e.g. the Costas phase cell's xi + xq). A
    broker abutting ``cell`` but DELIVERING to a different neighbour cell (``broker_target``
    says so) must NOT be reused: a single broker cell has ONE fwd_face (§1.3) and cannot
    flip toward two different targets, so two distinct sinks sharing it would collide (the
    modem's TX-mapper vs RX-matched-filter input nets, which both abut one free cell)."""
    forbid = _broker_forbidden(in_face, forbid_out)
    for c, (dx, dy) in _FWD_DELTA.items():
        if (dx, dy) in forbid:
            continue
        n = (cell[0] + dx, cell[1] + dy)
        if n in brokers and n != src and n != cell:
            if broker_target is not None and broker_target.get(n) not in (None, cell):
                continue  # this broker delivers elsewhere — not a same-cell fan-in
            return n
    return None


def _bus_bfs(src, sface, goal, occ, bus, spine_set, in_bounds, src_is_port,
             *, bus_dir=None, brokers=None, forbid_first=None,
             forbid_broker_transit=False, forbid_transit=None):
    """Shortest free-cell path src→goal, PREFERRING bus then spine cells, and only
    SHARING a bus cell when leaving it in its already-committed direction.

    ``forbid_first`` (a face code, or None) forbids the FIRST hop from leaving ``src``
    on that face — used for a single-cell hazard block's OUTPUT net so it never drives
    the same link the input arrives on (the §5.3 deadlock guard; input != output face).

    ``forbid_transit`` (a set of cells, or None) forbids the path from TRANSITING those
    cells — they may still be this net's own ``src``/``goal``, but no other net's
    corridor may thread THROUGH them. Used to reserve the chip OUTPUT-port edge cell: a
    net that merely rides through it would be re-faced toward the port exit when the
    egress net faces that cell (the port exit face clobbers the transit face), diverting
    the transiting stream into dead space (the rotated complex fan-in that snaked through
    x16_out and lost both operands).

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
    forbid_transit = forbid_transit or set()

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
        base = 0 if c in bus else (1 if c in spine_set else 4)
        # A FOREIGN port cell (another net's I/O terminus) is heavily penalised as a
        # TRANSIT cell — the build faces a port-egress cell toward the port exit, which
        # clobbers a transit face routed through it (the rotated complex fan-in that
        # snaked THROUGH x16_out and lost both operands). The penalty is soft, not a
        # hard wall: a net with NO alternative (e.g. a column-9 egress that must pass the
        # x16_out cell to reach x1_out) still routes, but any net with a detour takes it.
        if c in forbid_transit:
            base += 1000
        return base

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
                if forbid_broker_transit:
                    # BUS (v2) mode: NO foreign net may transit a broker — that is the
                    # exact one-cell-two-roles (deliver + conflicting through-transit)
                    # corruption the bus topology forbids by construction. Route around.
                    continue
                # allow only if we then continue on its bus direction (handled by
                # can_leave(nxt, ...) on the next expansion); permit entry here.
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
        # A chip-INPUT-port net into a COMPLEX block (>1 input reg, e.g. the RX matched
        # filter's xi+xq) must deliver ALL operands: the host injects N operands then ONE
        # trigger (the complex-sample contract), so the broker relays N WRITEs + 1 JUMP.
        # A single delivery would relay only the FIRST operand (the duplex RX "MF gets xi
        # but never xq" data-loss). Expand into one delivery per input reg, coalesced by
        # ``_broker_program`` (same in_cell) into the multi-operand group. A common
        # ``src_cell`` sentinel keys them as one group even though the source is the port.
        port_complex_regs = None
        if isinstance(conn.source, ChipPortEndpoint) and conn.src_complex is not False:
            # Only a COMPLEX source injects the full xi+xq packet (N WRITEs + 1 JUMP).
            # A FLOAT source (AM up-converter's ``xi`` rail) injects ONE operand — keep
            # the single delivery so the broker relays exactly what the host writes.
            _e2, _regs = catalog.resolved_io(tgt.type, tgt.params,
                                             library=tgt.library)
            if _regs and len(_regs) > 1:
                port_complex_regs = list(_regs)
        if port_complex_regs is not None:
            grp_key = ("port_complex", in_cell)
            for _r in port_complex_regs:
                d = BrokerDelivery(conn=conn.name, in_cell=in_cell, in_reg=_r,
                                   in_entry=entry, deliver_face=df, src_cell=grp_key)
                if last in taps:
                    taps[last].deliveries.append(d)
                else:
                    bus_face = transit_face.get(last, _bus_forward_face(pts))
                    taps[last] = BrokerTap(cell=last, deliveries=[d],
                                           bus_face=bus_face)
            continue
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


def broker_through_face(project, chip_id, chip_type, catalog):
    """``{(x, y): face}`` — for each BROKER cell that a FOREIGN routed net merely
    TRANSITS (passes through as a non-terminal waypoint, or egresses through), the
    forwarding face that foreign through-traffic needs.

    A broker delivers its OWN net(s) by landing them (HOP==31) + flipping per entry,
    then restores its cell ``fwd_face`` for transiting (HOP<31) words. When a DIFFERENT
    net's route passes THROUGH the broker cell (the auto-router packed two corridors
    onto it — e.g. the modem's MF→Costas net4 transiting the Upsampler→RRC broker at
    (2,3), or the Slicer→x16_out egress net7 transiting the Costas→Gardner broker at
    (6,9)), that foreign word is forwarded on the broker's static ``fwd_face`` — so the
    restore face MUST equal the foreign net's forwarding direction, NOT the broker's
    own into-cell travel. Otherwise the foreign stream is silently mis-faced and dies
    (the same single-``fwd_face`` corruption :func:`crossover_plan` resolves for plain
    cells; a broker can carry ONE extra through-direction by choosing its restore face,
    which is the common case here — two distinct foreign directions would need a full
    crossover and are reported by the bus DRC).

    Returns only the cells that HAVE a foreign-transit face (a broker with no through-
    traffic is absent ⇒ keep its own restore). The build's :func:`_apply_brokers`
    overrides ``bus_face`` with this value so the broker forwards the foreign stream
    correctly."""
    taps = broker_plan(project, chip_id, chip_type, catalog)
    if not taps:
        return {}
    brokers = set(taps.keys())
    # Each broker's OWN nets (delivered here) — excluded from "foreign".
    own: dict = {}
    for cell, tap in taps.items():
        own[cell] = {d.conn for d in tap.deliveries}

    block_cells: set = set()
    for blk in project.blocks:
        pl = blk.placement
        if pl is None or pl.chip != chip_id:
            continue
        block_cells.update((c.x, c.y) for c in pl.cells)

    through: dict = {}
    for conn in project.connections:
        if not conn.is_routed or _conn_chip(project, conn) != chip_id:
            continue
        pts = _phys_pts(project, conn, catalog)
        for i, c in enumerate(pts):
            if c not in brokers:
                continue
            if conn.name in own.get(c, set()):
                continue  # this broker's OWN delivery terminates here — not transit
            face = _net_exit_face(conn, pts, i, project, chip_id, chip_type,
                                  catalog, block_cells)
            if face is None:
                continue
            through.setdefault(c, face)  # first foreign use wins (one extra direction)
    return through


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
    a filler routing cell between every pair of blocks.

    Called both for a NO-route net (legacy coincidental-adjacency path) and for an
    explicit ``ABUTMENT_ROUTE`` net (an intended, router-declared abutment from the
    logical netlist) — both synthesise the same 2-cell handoff."""
    if conn.route and conn.route != ABUTMENT_ROUTE:  # a drawn waypoint route → not this
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
    # ABUTMENT sentinel: no waypoint list to read — synthesise the [src_out, tgt_in]
    # @1 handoff from the placements (abutment is always block→block; the source's
    # output cell abuts the target's input cell). Centralised here so every _phys_pts
    # caller (build faces, hops, brokers) gets the corridor-free path.
    if conn.route == ABUTMENT_ROUTE:
        return abutment_pts(project, conn, catalog, {})
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
