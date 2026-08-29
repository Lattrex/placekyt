# SPDX-License-Identifier: GPL-3.0-or-later
"""SRAM-panel synthesis + template place-and-route (INV-31 chains).

A panel-backed block (Varicode encoder, CW keyer — anything whose class returns
:meth:`~gr_kyttar.placement.blocks._base.KyttarBlock.panel_requirements`) cannot be
placed by the generic sweep: its embedded SRAM-controller cell must sit AT the
panel's ``x1_out`` chip-port cell, the panel's push-read return must be routed from
``x1_in`` back to the block's consumer cell, and the chip-input and chip-output
corridors CROSS (the four ports sit at the four corners, the data path visits
NW → SE → SW → NE), so a :class:`CrossoverBlock` is required at the crossing.

This module provides the two halves:

* :func:`synthesize_panel` — give an imported project the panel + panel_connections
  + the ``x1_in`` return net a panel-backed block needs (called by the GRC
  importer, so ``import_grc`` emits a COMPLETE project).
* :func:`apply_panel_template` — the template P&R: pin the controller at the panel
  port, place the crossover + the DSP chain, draw the corridor routes (the PROVEN
  ``engine/sram_demo.py`` corridor class), rewrite the port nets to run through the
  crossover, and derive the placement-dependent parameters (crossover track
  hops/dests/entries, the controller's push-read descriptors, the emit cell's
  downstream WRITE/JUMP target). ``AppController.auto_pnr`` delegates to this when
  the project contains a panel-backed block; the remaining block→block nets are
  routed by the normal ``auto_route_all``.

Every failure path raises :class:`~engine.errors.PlacementError` with a SPECIFIC
reason — a panel design that cannot be placed says exactly why.
"""
from __future__ import annotations

from dataclasses import dataclass


def _wr(hop: int, dest: int) -> int:
    return (0x6 << 12) | ((hop & 0x1F) << 5) | (dest & 0x1F)


def _jp(hop: int, entry: int) -> int:
    return (0x7 << 12) | ((hop & 0x1F) << 5) | (entry & 0x1F)


def panel_backed_blocks(project, catalog) -> list[tuple[object, dict]]:
    """``[(project Block, requirements dict)]`` for every placed block whose class
    declares SRAM-panel backing. Instantiation errors are treated as 'no panel'
    (the block will fail later at build with its own error)."""
    out = []
    for b in project.blocks:
        try:
            inst = catalog.instantiate(b.type, "__panel_probe__", b.params,
                                       library=b.library)
            req = inst.panel_requirements()
        except Exception:  # noqa: BLE001 — no requirements probe → not panel-backed
            req = None
        if req:
            out.append((b, req))
    return out


def synthesize_panel(project, catalog) -> list[str]:
    """Add the SRAM panel + panel_connections + the ``x1_in`` return net for each
    panel-backed block missing them. Returns a list of human-readable actions
    taken (empty = nothing needed). Idempotent."""
    from model.connection import (BlockEndpoint, ChipPortEndpoint, Connection,
                                  PanelConnection)
    from model.panel import SramPanel

    actions: list[str] = []
    backed = panel_backed_blocks(project, catalog)
    if not backed:
        return actions
    if len(backed) > 2:
        from engine.errors import PlacementError
        raise PlacementError(
            "more than two SRAM-panel-backed blocks in one design ("
            + ", ".join(b.name for b, _ in backed)
            + ") — the chip has ONE x1_out/x1_in port pair; at most a TX/RX "
              "pair SHARING one panel is supported")
    if len(backed) == 2:
        # SHARED PANEL (the duplex transceiver): both clients' tables live in
        # ONE panel; their address regions MUST be disjoint (the client with an
        # ``addr_base`` param offsets its table — e.g. the Varicode encoder at
        # base 1024, clear of the decoder's 1..955 reverse map).
        from engine.errors import PlacementError
        imgs = [(b, {int(a): int(w) & 0xFFFF
                     for a, w in (r.get("image") or {}).items()})
                for b, r in backed]
        overlap = set(imgs[0][1]) & set(imgs[1][1])
        if overlap:
            raise PlacementError(
                f"shared SRAM panel: {imgs[0][0].name} and {imgs[1][0].name} "
                f"table addresses OVERLAP at {sorted(overlap)[:5]}… — offset "
                f"one client's table (its addr_base param) so the regions are "
                f"disjoint")
        merged = dict(imgs[0][1])
        merged.update(imgs[1][1])
        label = " + ".join(str(r.get("label") or b.name) for b, r in backed)
        auto_inc = any(r.get("auto_inc_read") for _b, r in backed)
    else:
        blk0, req0 = backed[0]
        merged = {int(a): int(w) & 0xFFFF
                  for a, w in (req0.get("image") or {}).items()}
        label = str(req0.get("label") or "SRAM panel")
        auto_inc = bool(req0.get("auto_inc_read"))
    blk, req = backed[0]

    if not project.panels:
        panel = SramPanel(id=0, label=label,
                          position_x=240.0, position_y=840.0)
        panel.mirror_h()
        panel.image = dict(merged)
        project.panels.append(panel)
        actions.append(f"panel '{panel.label}' added (image {len(panel.image)} words)")
    else:
        panel = project.panels[0]
        if merged and not panel.image:
            panel.image = dict(merged)
            actions.append(f"panel image set ({len(panel.image)} words)")
    if auto_inc and not panel.auto_inc_read:
        panel.auto_inc_read = True
        actions.append("panel read auto-increment enabled")
    # The panel image is the blocks' DESIGN state (e.g. the CW keyer's
    # message-dependent run records): if the requirements now declare a
    # DIFFERENT image than the panel holds, refresh it (a re-import with a new
    # message must not keep the stale schedule).
    if merged and panel.image != merged:
        panel.image = dict(merged)
        actions.append(f"panel image refreshed ({len(merged)} words)")

    if not project.panel_connections:
        project.panel_connections.extend([
            PanelConnection(0, "x1_in", 0, "x1_out"),   # chip x1_out -> panel input
            PanelConnection(0, "x1_out", 0, "x1_in"),   # panel output -> chip x1_in
        ])
        actions.append("panel_connections x1_out/x1_in wired")

    for rb, rreq in backed:
        rp = str(rreq.get("return_port") or "word")
        have_ret = any(
            isinstance(c.source, ChipPortEndpoint) and c.source.port == "x1_in"
            and isinstance(c.target, BlockEndpoint)
            and c.target.block == rb.name
            for c in project.connections)
        if not have_ret:
            project.connections.append(Connection(
                f"{rb.name}_panel_return",
                ChipPortEndpoint(0, "x1_in"),
                BlockEndpoint(rb.name, rp),
                route=None))
            actions.append(f"return net x1_in -> {rb.name}.{rp} added")
    return actions


@dataclass
class _Geometry:
    """The template corridor geometry, derived from the chip type's port cells."""
    w: int
    h: int
    in_cell: tuple[int, int]      # x16_in
    out_cell: tuple[int, int]     # x16_out
    x1_out_cell: tuple[int, int]  # controller sits here
    x1_in_cell: tuple[int, int]   # return corridor starts here
    xo: tuple[int, int]           # the crossover cell
    in_route: list[tuple[int, int]]
    xo_to_ctl: list[tuple[int, int]]
    ret_route: list[tuple[int, int]]
    xo_to_out: list[tuple[int, int]]
    emit: tuple[int, int]         # the panel-return consumer cell
    box: tuple[int, int, int, int]  # free DSP region (x0, y0, x1, y1) inclusive


def _derive_geometry(ct) -> _Geometry:
    """The proven sram_demo corridor class, derived from the chip type's ports.

    Requires the standard corner topology (x16_in NW, x16_out NE, x1_out SE,
    x1_in SW — the kyttar_10x12 layout); anything else raises a NAMED error
    rather than emitting a wrong template."""
    from engine.errors import PlacementError

    ports = {p.name: (p.cell_x, p.cell_y) for p in ct.ports}
    for need in ("x16_in", "x16_out", "x1_in", "x1_out"):
        if need not in ports:
            raise PlacementError(
                f"panel template: chip type {ct.name!r} has no {need} port")
    w, h = ct.width, ct.height
    pin, pout = ports["x16_in"], ports["x16_out"]
    p1o, p1i = ports["x1_out"], ports["x1_in"]
    if not (pin == (0, 0) and pout == (w - 1, 0)
            and p1o == (w - 1, h - 1) and p1i == (0, h - 1)):
        raise PlacementError(
            "panel template: unsupported port topology "
            f"(x16_in={pin}, x16_out={pout}, x1_out={p1o}, x1_in={p1i}); the "
            "template needs the corner layout x16_in NW / x16_out NE / "
            "x1_out SE / x1_in SW")

    xo = (w - 2, h // 2)
    # ROUTE CONVENTION (matches the auto-router / what the GUI draws for the
    # modem examples): every corridor route STARTS ON its source cell (when the
    # source is a block) and ENDS ON its target cell, so the drawn polyline
    # visually connects INTO the blocks (user-reported: the old sram_demo-style
    # routes stopped one cell short and looked disconnected). The build's
    # ``_phys_pts`` strips a trailing on-target waypoint back to the abutting
    # cell, so the REALIZED faces/hops are identical to the old form.
    # Input corridor: x16_in -> EAST along row 0 -> SOUTH down col w-2 -> ON the
    # crossover cell.
    in_route = ([(x, 0) for x in range(0, w - 1)]
                + [(w - 2, y) for y in range(1, xo[1] + 1)])
    # Crossover -> controller: from the crossover, SOUTH down col w-2, ending ON
    # the controller cell at the x1_out port.
    xo_to_ctl = ([xo] + [(w - 2, y) for y in range(xo[1] + 1, h)]
                 + [(w - 1, h - 1)])
    # Return: x1_in -> NORTH up col 0, ending ON the consumer (emit) cell.
    emit = (0, 1)
    ret_route = [(0, y) for y in range(h - 1, emit[1] - 1, -1)]
    # Crossover -> x16_out: EAST one cell, then NORTH up col w-1 INCLUDING the
    # port cell (0-row) — the final waypoint takes the port's exit face, so the
    # route must END ON the port cell (as sram_demo's XO_TO_OUT does); ending one
    # cell short faces the last corridor cell out the wrong edge.
    xo_to_out = ([xo, (w - 1, xo[1])]
                 + [(w - 1, y) for y in range(xo[1] - 1, -1, -1)])
    box = (1, 1, w - 3, xo[1] - 1)
    return _Geometry(w=w, h=h, in_cell=pin, out_cell=pout,
                     x1_out_cell=p1o, x1_in_cell=p1i, xo=xo,
                     in_route=in_route, xo_to_ctl=xo_to_ctl,
                     ret_route=ret_route, xo_to_out=xo_to_out,
                     emit=emit, box=box)


def _resolve_cell(catalog, block, cell_id):
    """(resolved memory-image resolver outputs) for one cell of a project block:
    ``(entry_addresses, {classified name: addr}, first_entry_name)``."""
    from gr_kyttar.placement.resolver import CellProgramResolver
    inst = catalog.instantiate(block.type, "__panel_resolve__", block.params,
                               library=block.library)
    cp = inst.build_cell_programs()[cell_id]
    r = CellProgramResolver()
    entries = r.compute_entry_addresses(cp)
    cls = r.classify_addresses(cp)
    named = {}
    for addr, info in cls.items():
        nm = info.get("name")
        if nm is not None and nm not in named:
            named[nm] = addr
    first = cp.entries[0].name if getattr(cp, "entries", None) else None
    return entries, named, first


def _chain_after(project, start_block: str) -> list[str]:
    """Block names downstream of ``start_block`` (following block→block nets) in
    dataflow order, ending at the block wired to the chip output port."""
    from model.connection import BlockEndpoint

    succ: dict[str, list[str]] = {}
    for c in project.connections:
        if isinstance(c.source, BlockEndpoint) and isinstance(c.target, BlockEndpoint):
            succ.setdefault(c.source.block, []).append(c.target.block)
    chain, cur, seen = [], start_block, {start_block}
    while True:
        nxts = [n for n in succ.get(cur, []) if n not in seen]
        if not nxts:
            break
        cur = nxts[0]
        seen.add(cur)
        chain.append(cur)
    return chain


def apply_panel_template(project, catalog, ct, *, chip: int = 0, _only=None):
    """Template-place + corridor-route a panel-backed design (see module doc).

    Returns ``(corridor RouteResults, notes)``; block→block DSP nets are left for
    the caller's ``auto_route_all``. Raises ``PlacementError`` (with a specific
    reason) when the design does not fit the template."""
    from engine.autoroute import RouteResult
    from engine.errors import PlacementError
    from model.block import Block
    from model.connection import (BlockEndpoint, ChipPortEndpoint, Connection,
                                  RoutePoint)
    from model.enums import Face
    from model.placement import Placement, PlacedCell

    notes: list[str] = []
    backed = panel_backed_blocks(project, catalog)
    if not backed:
        raise PlacementError("panel template invoked on a design with no "
                             "panel-backed block")
    # DUPLEX (shared panel): a TX-head client + an RX-tail client on one chip.
    if _only is None and len(backed) == 2:
        return _apply_duplex_panel_template(project, catalog, ct, backed,
                                            chip=chip)
    blk, req = next(((b, r) for b, r in backed
                     if _only is None or b.name == _only), backed[0])
    # SELF-CONTAINED SHAPE: a panel-backed block that supplies its OWN complete
    # layout for every cell and speaks to the panel from inside itself (its emit
    # cell drives the controller directly; its egress is its own output port).
    # The role-named templates below place only 2-4 named cells, which silently
    # DROPS the rest of a larger block — see _apply_self_contained_template.
    if req.get("self_contained"):
        return _apply_self_contained_template(project, catalog, ct, blk, req,
                                              chip=chip)
    # RX-TAIL SHAPE: a panel-backed block whose stream INPUT lands on a
    # different cell than its panel controller (input_cell declared) consumes
    # the END of a chain (bits in -> panel lookup -> chars out) — the mirrored
    # corridor set. Dispatch to the RX template.
    if req.get("input_cell") is not None \
            and req.get("input_cell") != req.get("controller_cell", 0):
        return _apply_rx_template(project, catalog, ct, blk, req, chip=chip)
    geo = _derive_geometry(ct)
    lib = blk.library

    def _rp(pts):
        return [RoutePoint(x, y) for (x, y) in pts]

    # ---- 1. Pin the panel-backed block: controller AT x1_out, consumer at emit.
    ctl_cell = req.get("controller_cell", 0)
    ret_cell = req.get("return_cell", 1)
    blk.placement = Placement(chip, [
        PlacedCell(ctl_cell, geo.x1_out_cell[0], geo.x1_out_cell[1], Face.SOUTH),
        PlacedCell(ret_cell, geo.emit[0], geo.emit[1], Face.EAST),
    ])

    # ---- 2. Place the downstream DSP chain in the free box, dataflow order.
    chain = _chain_after(project, blk.name)
    x0, y0, x1, y1 = geo.box
    cx, cy = x0, y0
    band_h = 1
    placements: dict[str, list] = {}
    for name in chain:
        b = project.block(name)
        if b is None:
            continue
        layout = catalog.default_layout(b.type, b.params, library=b.library) \
            or {0: (0, 0, "east")}
        bw = max(dx for dx, _dy, _f in layout.values()) + 1
        bh = max(dy for _dx, dy, _f in layout.values()) + 1
        if cx + bw - 1 > x1:
            # Wrap to the next row band, leaving ONE free routing row between
            # bands — a full-width band (e.g. the 7-cell envelope) otherwise
            # walls off every southward path from the band above (the boxed-net
            # failure the sweep's channel_reserve solves for generic designs).
            cx, cy = x0, cy + band_h + 1
            band_h = 1
        band_h = max(band_h, bh)
        if cy + bh - 1 > y1:
            raise PlacementError(
                f"panel template: DSP chain does not fit the free region "
                f"cols {x0}..{x1} rows {y0}..{y1} (placing {name} "
                f"{bw}x{bh} at row {cy})")
        cells = [PlacedCell(cid, cx + dx, cy + dy, Face.from_str(f))
                 for cid, (dx, dy, f) in layout.items()]
        b.placement = Placement(chip, cells)
        placements[name] = cells
        cx += bw
    notes.append(f"chain placed: {', '.join(chain) or '(none)'}")

    # ---- 3. Resolve the controller + consumer + crossover parameters. The
    # streaming injection targets the controller cell's DEFAULT (first) entry —
    # 'lookup' for the Varicode key lookup, 'fetch' for the CW record fetch.
    ctl_entries, ctl_named, ctl_first = _resolve_cell(catalog, blk, ctl_cell)
    if ctl_first is None or ctl_first not in ctl_entries:
        raise PlacementError(
            f"panel template: {blk.name}'s controller cell has no resolvable "
            f"default entry (entries: {sorted(ctl_entries)})")
    # The controller cell's input REGISTER (the stream key lands here), from the
    # block's resolved landing IO — port-name agnostic.
    _e_unused, _ctl_in_regs = catalog.resolved_io(blk.type, blk.params,
                                                  library=blk.library)
    if not _ctl_in_regs:
        raise PlacementError(
            f"panel template: {blk.name}'s controller cell has no resolvable "
            "input register (the stream key has nowhere to land)")
    _ctl_in_reg = int(_ctl_in_regs[0])
    emit_entries, emit_named, _ = _resolve_cell(catalog, blk, ret_cell)
    ret_port = str(req.get("return_port") or "word")
    if ret_port not in emit_named:
        raise PlacementError(
            f"panel template: {blk.name} cell {ret_cell} has no register named "
            f"{ret_port!r} (have {sorted(emit_named)})")

    # Push-read descriptors: deliver each looked-up word to the consumer cell's
    # return register + kick its (single/default) entry, hop-counted along the
    # return corridor (route cells + 1 to land ON the consumer — HOP_CNT is
    # consumed at 31; same arithmetic as engine/sram_demo.py).
    # ret_route includes the x1_in port cell AND ends ON the consumer cell:
    # the push transits every route cell and lands on the last -> len(route).
    ret_hop = 31 - len(geo.ret_route)
    if ret_hop < 0:
        raise PlacementError(
            f"panel template: return corridor {len(geo.ret_route)}+1 hops "
            "exceeds the 31-hop budget")
    rwd = _wr(ret_hop, emit_named[ret_port])
    rjd = _jp(ret_hop, min(emit_entries.values()))
    blk.params["read_wr_desc"] = rwd
    blk.params["read_jp_desc"] = rjd

    # The consumer (emit) cell's downstream WRITE/JUMP target: the FIRST chain
    # block's landing entry + input register, at abutment (@1). Only set for
    # blocks that take these as params (the Varicode/CW emit_hop convention).
    if chain:
        nxt = project.block(chain[0])
        entry, in_regs = catalog.resolved_io(nxt.type, nxt.params,
                                             library=nxt.library)
        if "emit_dest" in blk.params or "emit_entry" in blk.params \
                or "emit_hop" in blk.params:
            blk.params["emit_hop"] = 1
            blk.params["emit_dest"] = (in_regs[0] if in_regs else 0)
            blk.params["emit_entry"] = entry
    notes.append(f"descriptors rwd=0x{rwd:04X} rjd=0x{rjd:04X} (hop {ret_hop})")

    # ---- 4. The crossover: input corridor (N->S) x egress corridor (W->E).
    xo_name = f"{blk.name}_xover"
    if project.block(xo_name) is None:
        xo_params = {
            # xo_to_ctl includes the crossover (source) and ends ON the
            # controller: transits = len - 1.
            "face_a": "south", "hop_a": len(geo.xo_to_ctl) - 1,
            # Track A data register: the controller cell's INPUT register,
            # resolved GENERICALLY (the SramController names it 'data', the CW
            # record fetch 'char' — a name-keyed lookup silently fell back to
            # R0/the accumulator and the relayed key vanished).
            "dest_a": _ctl_in_reg,
            "entry_a": ctl_entries[ctl_first],
            # xo_to_out includes the crossover and ends ON the port cell:
            # transits + the port exit = len.
            "face_b": "east", "hop_b": len(geo.xo_to_out),
            "dest_b": 0, "entry_b": 0,
            # Track C (control-only relay): the block's declared COMPLETION
            # entry — the CW player's per-record done-kick relays through here
            # back to the fetch cell's 'next' entry (the flow control that makes
            # record sequencing self-paced). Blocks without one leave it inert.
            "face_c": "south", "hop_c": len(geo.xo_to_ctl) - 1,
            "entry_c": ctl_entries.get(
                str(req.get("completion_entry") or ""),
                ctl_entries.get("read", 0)),
        }
        project.blocks.append(Block(
            xo_name, "CrossoverBlock", library=lib, params=xo_params,
            placement=Placement(chip, [PlacedCell(0, geo.xo[0], geo.xo[1],
                                                  Face.SOUTH)])))
    notes.append(f"crossover at {geo.xo}")

    # ---- 5. Rewrite the port nets through the crossover + draw corridor routes.
    #   x16_in -> blk          becomes  x16_in ->(in_route) xover ->(xo_to_ctl) blk
    #   last  -> x16_out       becomes  last ->(drawn) xover ->(xo_to_out) x16_out
    #   x1_in -> blk.ret_port  gets the drawn return route
    results: list[RouteResult] = []

    def _drop(pred):
        dropped = [c for c in project.connections if pred(c)]
        project.connections[:] = [c for c in project.connections if not pred(c)]
        return dropped

    dropped_in = _drop(
        lambda c: isinstance(c.source, ChipPortEndpoint)
        and c.source.port == "x16_in"
        and isinstance(c.target, BlockEndpoint) and c.target.block == blk.name)
    # Carry the replaced input net's stream identity onto the corridor net so the
    # GRC live bridge still resolves this stream's injection (stream_targets).
    in_sid = next((c.stream_id for c in dropped_in
                   if getattr(c, "stream_id", None)), None)
    project.connections.append(Connection(
        "in_to_xo", ChipPortEndpoint(chip, "x16_in"),
        BlockEndpoint(xo_name, "in"), route=_rp(geo.in_route),
        stream_id=in_sid))
    project.connections.append(Connection(
        "xo_to_ctl", BlockEndpoint(xo_name, "out"),
        BlockEndpoint(blk.name, "in"), route=_rp(geo.xo_to_ctl)))
    results += [RouteResult("in_to_xo", True, points=list(geo.in_route)),
                RouteResult("xo_to_ctl", True, points=list(geo.xo_to_ctl))]

    # Return corridor (the synthesized net, or a fresh one).
    ret_conn = next(
        (c for c in project.connections
         if isinstance(c.source, ChipPortEndpoint) and c.source.port == "x1_in"
         and isinstance(c.target, BlockEndpoint) and c.target.block == blk.name),
        None)
    if ret_conn is None:
        ret_conn = Connection(f"{blk.name}_panel_return",
                              ChipPortEndpoint(chip, "x1_in"),
                              BlockEndpoint(blk.name, ret_port), route=None)
        project.connections.append(ret_conn)
    ret_conn.route = _rp(geo.ret_route)
    results.append(RouteResult(ret_conn.name, True, points=list(geo.ret_route)))

    # Egress: the LAST chain block's output cell -> down its column -> row xo.y
    # east -> crossover; crossover track_b -> x16_out.
    # The egress source: the LAST chain block — or, for a CHAINLESS design (the
    # CW keyer: panel block straight to the output port), the panel block itself
    # (its output leaves the return/consumer cell, e.g. the run player).
    last = chain[-1] if chain else blk.name
    if last is not None:
        b = project.block(last)
        if chain:
            pm = catalog.port_map(b.type, b.params, library=b.library)
            out_ports = [p for p in pm.ports if p.direction == "out"]
            out_cid = out_ports[0].cell_id if out_ports else None
            pos = {c.cell_id: (c.x, c.y) for c in b.placement.cells}
            # PortMap cell ids may be strings (multi-cell blocks) — match loosely.
            ox, oy = pos.get(out_cid, list(pos.values())[-1])
        else:
            ox, oy = geo.emit          # the panel block's consumer cell
        if ox >= geo.xo[0]:
            raise PlacementError(
                f"panel template: {last}'s output cell at ({ox},{oy}) is not "
                f"west of the crossover col {geo.xo[0]}")
        # Router invariant: route[0] IS the source's exit cell, and a block→block
        # route ends ON the target cell (the build strips it to the abutting
        # hop) — the exit-hop patch derives the WRITE/JUMP hop from this length.
        if ox == 0:
            # Column 0 below the source is the RETURN corridor: egress EAST
            # along the source's row to the crossover's column - 1, then SOUTH
            # to the crossover row, then onto the crossover (the chainless/CW
            # shape — rows 1..xo.y-1 of that column are free).
            mid = geo.xo[0] - 1
            egress = ([(ox, oy)]
                      + [(x, oy) for x in range(ox + 1, mid + 1)]
                      + [(mid, y) for y in range(oy + 1, geo.xo[1] + 1)]
                      + [(geo.xo[0], geo.xo[1])])
        else:
            egress = ([(ox, oy)]
                      + [(ox, y) for y in range(oy + 1, geo.xo[1])]
                      + [(x, geo.xo[1]) for x in range(ox, geo.xo[0] + 1)])
        # Drop any straight last->x16_out net; ride the crossover instead. Keep
        # its out_tag: the sink demuxes by the exit WRITE's dest field, which for
        # a crossover egress is track_b's dest_b.
        dropped_out = _drop(
            lambda c: isinstance(c.source, BlockEndpoint)
            and c.source.block == last
            and isinstance(c.target, ChipPortEndpoint)
            and c.target.port == "x16_out")
        out_tag = next((c.out_tag for c in dropped_out
                        if getattr(c, "out_tag", None) is not None), None)
        if out_tag is not None:
            xo_blk = project.block(xo_name)
            xo_blk.params["dest_b"] = int(out_tag)
        # The egress must enter the crossover on TRACK_B (relay east to the
        # output port) — the block's DEFAULT entry is track_a (relay to the
        # controller), which would turn every output sample into a panel lookup
        # (a runaway read loop). entry_override selects the track per-net.
        xo_entries, xo_named, _ = _resolve_cell(catalog, project.block(xo_name), 0)
        # CHAINLESS RAW-hop source (the CW keyer): the panel block authors its
        # own per-sample WRITE/JUMP + completion kick, so set its placement-
        # derived emit params — hop from its output cell to the crossover along
        # the drawn egress (route[0] is the source, so hops = len-1), dest = the
        # crossover's relay register, per-sample entry = track_b (relay to the
        # output port), completion entry = track_c (relay to the fetch 'next').
        if not chain and "emit_hop" in blk.params:
            blk.params["emit_hop"] = len(egress) - 1
            blk.params["emit_dest"] = int(xo_named.get("relay", 0))
            blk.params["emit_entry"] = int(xo_entries["track_b"])
            if "done_entry" in blk.params and "track_c" in xo_entries:
                blk.params["done_entry"] = int(xo_entries["track_c"])
        project.connections.append(Connection(
            "egress_to_xo", BlockEndpoint(last, "out"),
            BlockEndpoint(xo_name, "in"), route=_rp(egress),
            entry_override=int(xo_entries["track_b"])))
        project.connections.append(Connection(
            "xo_to_out", BlockEndpoint(xo_name, "out"),
            ChipPortEndpoint(chip, "x16_out"), route=_rp(geo.xo_to_out),
            out_tag=out_tag))
        results += [RouteResult("egress_to_xo", True, points=list(egress)),
                    RouteResult("xo_to_out", True, points=list(geo.xo_to_out))]

    return results, notes


def _apply_duplex_panel_template(project, catalog, ct, backed, *, chip: int = 0):
    """SHARED-panel duplex template: a TX-head panel client (the Varicode
    encoder feeding a modulator chain) + an RX-tail panel client (the Varicode
    decoder consuming a demodulator chain) on ONE chip, both talking to ONE
    panel through the single x1 port pair.

    Layout (kyttar_10x12): the TX half is the proven TX template with ONE
    adjustment — the ctl moves OFF the x1_out port cell to (8,11) so (9,11)
    stays a PLAIN ROUTING cell both clients' panel words traverse (emit (0,1),
    chain in the box, crossover at (8,6)); the RX half threads AROUND it:

      x16_in row-0/col-8 corridor (SHARED) → transits the TX xo → RX tap (8,8)
      → slicer (7,8) → diffdec (6,8) → tail xo (8,9) → accumulate (9,9)
      → RX ctl (9,10, panel_hop 2 through the (9,11) routing cell) → PANEL;
      x1_in → RX emit (0,10) [the TX pushes transit it];
      RX emit → col-0 north → ret xo (0,6) → row-6 east → TX xo track_c
      (a DATA track, dest_c = the RX out_tag) → x16_out.

    Every relay cell others TRANSIT gets ``restore_face`` (the broker
    self-restore) so a relay never leaves the transit face flipped. The two
    clients' panel reads are safe to interleave because EVERY read writes its
    own R3/R4 descriptors (the SramController read protocol). PER-SAMPLE PACED
    ONLY (the standard panel contract; the server enforces it)."""
    from engine.autoroute import RouteResult
    from engine.errors import PlacementError
    from model.connection import BlockEndpoint, Connection, RoutePoint
    from model.enums import Face

    rx = next(((b, r) for b, r in backed
               if r.get("input_cell") is not None
               and r.get("input_cell") != r.get("controller_cell", 0)), None)
    tx = next(((b, r) for b, r in backed if rx is None or b is not rx[0]), None)
    if rx is None or tx is None or tx[1].get("input_cell") not in (
            None, tx[1].get("controller_cell", 0)):
        raise PlacementError(
            "duplex panel template: need exactly ONE TX-head client and ONE "
            "RX-tail client (input_cell-declaring); got "
            + ", ".join(b.name for b, _ in backed))

    # RX out_tag (captured before the TX half's rewrites touch anything).
    from model.connection import ChipPortEndpoint
    rx_out_tag = next(
        (c.out_tag for c in project.connections
         if isinstance(c.source, BlockEndpoint) and c.source.block == rx[0].name
         and isinstance(c.target, ChipPortEndpoint)
         and c.target.port == "x16_out"
         and getattr(c, "out_tag", None) is not None), None)

    # ---- TX half: the proven TX template, verbatim.
    results, notes = apply_panel_template(project, catalog, ct, chip=chip,
                                          _only=tx[0].name)
    notes.append("duplex: TX half applied")

    # ---- Free the x1_out PORT CELL (user-reported GUI defect: with the TX ctl
    # sitting ON (9,11), the RX ctl's panel words at (9,10) had "no way to get
    # to the SRAM port without going through one of the blocks"). Move the TX
    # ctl one cell WEST onto the corridor end (8,11), facing EAST into the now
    # PLAIN ROUTING cell (9,11) — both clients' panel words then reach the
    # port through routing fabric only (TX @2 from the west, RX @2 from the
    # north; a routing cell merges two inbound faces onto its one exit face).
    geo = _derive_geometry(ct)
    _w, _h = geo.w, geo.h
    tx_blk = tx[0]
    _ctl_id = tx[1].get("controller_cell", 0)
    for _pc in tx_blk.placement.cells:
        if _pc.cell_id == _ctl_id and (_pc.x, _pc.y) == geo.x1_out_cell:
            _pc.x, _pc.y = _w - 2, _h - 1
            _pc.face = Face.EAST
    tx_blk.params["panel_hop"] = 2       # transit (9,11), then the port exit
    _c_x2c = next((c for c in project.connections if c.name == "xo_to_ctl"),
                  None)
    if _c_x2c is not None and isinstance(_c_x2c.route, list) \
            and _c_x2c.route and (_c_x2c.route[-1].x, _c_x2c.route[-1].y) \
            == geo.x1_out_cell:
        _c_x2c.route = _c_x2c.route[:-1]         # ends ON the ctl at (8,11)
    # Face the freed port cell toward the x1_out exit: a route ending ON the
    # port cell sets its exit face (the xo_to_out convention). The conn's
    # SOURCE must be a RAW_OUTPUT_HOPS block (the build then never patches its
    # words for this net — it only realizes the corridor faces); the TX client
    # need not be RAW (VaricodeEncoder isn't — sourcing this net there patched
    # its emit cell and killed the TX outright), but the RX-tail client always
    # is, and its ctl at (9,10) genuinely drives this corridor: its panel
    # words head SOUTH through (9,11) out the port.
    _p2p = "rxctl_to_panel"
    _rx_port = next(
        (c.source.port for c in project.connections
         if isinstance(c.source, BlockEndpoint) and c.source.block == rx[0].name
         and isinstance(c.target, ChipPortEndpoint)), "out")
    if not any(c.name == _p2p for c in project.connections):
        project.connections.append(Connection(
            _p2p, source=BlockEndpoint(rx[0].name, _rx_port),
            target=ChipPortEndpoint(chip=chip, port="x1_out"),
            route=[RoutePoint(_w - 1, _h - 2), RoutePoint(_w - 1, _h - 1)]))
    results = list(results) + [RouteResult(
        _p2p, True, points=[(_w - 1, _h - 2), (_w - 1, _h - 1)])]
    notes.append("duplex: TX ctl moved off the x1_out port cell -> (8,11); "
                 "(9,11) is a plain routing cell shared by both clients")

    # The TX crossover gains the transit-face restore (RX input words TRANSIT
    # this cell heading south) — and, when its track_c is FREE (no completion
    # relay — the Varicode TX), the RX-egress DATA track. A keyer TX keeps its
    # completion on track_c; the kicker-form RX egress crosses elsewhere.
    tx_xo = project.block(f"{tx[0].name}_xover")
    if tx_xo is None:
        raise PlacementError("duplex panel template: TX crossover missing")
    if rx[1].get("kicker_cell") is None:
        tx_xo.params["face_c"] = "east"
        tx_xo.params["hop_c"] = int(tx_xo.params.get("hop_b", 8))
        tx_xo.params["entry_c"] = 0
        tx_xo.params["dest_c"] = int(rx_out_tag) if rx_out_tag is not None \
            else 0
    tx_xo.params["restore_face"] = "south"
    # The dest_c/restore additions CHANGE the crossover program, shifting its
    # entry addresses — re-resolve and update every net that JUMPs into it
    # (the TX egress's track_b entry_override was resolved pre-mutation).
    _xo_e2, _, _ = _resolve_cell(catalog, tx_xo, 0)
    for c in project.connections:
        if (isinstance(c.target, BlockEndpoint)
                and c.target.block == tx_xo.name
                and getattr(c, "entry_override", None) is not None):
            c.entry_override = int(_xo_e2["track_b"])

    # ---- RX half.
    r2, n2 = _apply_duplex_rx_half(project, catalog, ct, rx[0], rx[1],
                                   tx_xo_name=tx_xo.name, chip=chip,
                                   rx_out_tag=rx_out_tag)
    return list(results) + list(r2), list(notes) + list(n2)


def _ret_broker_descs(conn_name, emit_pos, emit_reg, emit_entry, ret_hop):
    """Push-read descriptors for an OFF-CORRIDOR RX emit cell served by a
    return-fork BROKER (the routing cell west of ``emit_pos`` on the x1_in
    corridor): the panel's read result WRITEs the broker's burst reg and JUMPs
    its deliver entry; the broker relays into the emit cell.

    The deliver entry is resolved by assembling the SAME single-delivery
    broker program the build's ``_apply_brokers`` will emit for the return
    conn (``engine.build._broker_program`` — shared by construction, so the
    addresses agree; the entry address depends only on the program STRUCTURE,
    not the face values). Returns ``(read_wr_desc, read_jp_desc, emit_entry)``
    — the caller stores ``emit_entry`` as the return conn's
    ``entry_override`` so broker_plan builds the identical delivery."""
    from engine.build import _broker_program
    from engine.bus_router import BROKER_BURST_REG, BrokerDelivery

    deliveries = [BrokerDelivery(
        conn=conn_name, in_cell=tuple(emit_pos), in_reg=int(emit_reg),
        in_entry=int(emit_entry), deliver_face=1)]      # deliver EAST
    entry_by_conn, _mem, burst_by_conn = _broker_program(
        deliveries, 3)                                   # bus face NORTH
    burst = int(burst_by_conn.get(conn_name, BROKER_BURST_REG + 1))
    return (_wr(ret_hop, burst),
            _jp(ret_hop, int(entry_by_conn[conn_name])),
            int(emit_entry))


def _apply_duplex_rx_half(project, catalog, ct, blk, req, *, tx_xo_name,
                          chip: int = 0, rx_out_tag=None):
    """The RX-tail half of the duplex layout (see _apply_duplex_panel_template).

    Dispatches on the RX block's shape: the 3-cell Varicode form (input cell
    IS the ctl kicker; egress via the TX crossover's data track_c) or the
    4-cell KICKER form (the streaming CW decoder: detect → classify → ctl;
    the TX crossover's track_c is the keyer's COMPLETION relay, so the RX
    egress crosses on its own relay pair instead)."""
    if req.get("kicker_cell") is not None:
        return _apply_duplex_rx_half_kicker(project, catalog, ct, blk, req,
                                            tx_xo_name=tx_xo_name, chip=chip,
                                            rx_out_tag=rx_out_tag)
    from engine.autoroute import RouteResult
    from engine.errors import PlacementError
    from model.block import Block
    from model.connection import (BlockEndpoint, ChipPortEndpoint, Connection,
                                  RoutePoint)
    from model.enums import Face
    from model.placement import Placement, PlacedCell

    notes: list[str] = ["duplex: RX half"]
    geo = _derive_geometry(ct)
    w, h = geo.w, geo.h
    lib = blk.library
    ctl_cell = req.get("controller_cell", 0)
    in_cell_id = req.get("input_cell")
    ret_cell = req.get("return_cell", 1)
    ret_port = str(req.get("return_port") or "word")

    def _rp(pts):
        return [RoutePoint(x, y) for (x, y) in pts]

    # NO DSP cell sits ON a corridor and NO CrossoverBlock relays remain in
    # this half (user-reported "routing through the blocks"): the RX taps are
    # standard build BROKERS (plain routing cells the corridor words transit
    # at HOP<31), the emit hangs OFF the return corridor behind a fork broker
    # at (0,10), and the chars ride col 1 + row 6 straight onto the TX
    # crossover's data track_c — the old retxo fork cell is gone entirely.
    ctl_pos = (w - 1, h - 2)     # (9,10): panel words transit (9,11) (routing)
    acc_pos = (w - 1, h - 3)     # (9,9), faces SOUTH into the RX ctl
    emit_pos = (1, h - 2)        # (1,10): OFF the return corridor, faces NORTH
    tap_pos = (w - 2, h - 4)     # (8,8): BROKER on the TX feed corridor
    tailxo_pos = (w - 2, h - 3)  # (8,9): BROKER delivering into the acc
    pos_by_id = {ctl_cell: (ctl_pos, Face.SOUTH),
                 in_cell_id: (acc_pos, Face.SOUTH),
                 ret_cell: (emit_pos, Face.NORTH)}
    blk.placement = Placement(chip, [
        PlacedCell(cid, pos_by_id[cid][0][0], pos_by_id[cid][0][1],
                   pos_by_id[cid][1])
        for cid in sorted(pos_by_id)])

    # Upstream RX chain: single-cell blocks flowing WEST from (w-3, h-4).
    chain = _chain_before(project, blk.name)
    if not chain:
        raise PlacementError(
            f"duplex panel template: {blk.name} has no upstream RX chain")
    cx = w - 3
    for name in chain:
        b = project.block(name)
        layout = catalog.default_layout(b.type, b.params, library=b.library) \
            or {0: (0, 0, "west")}
        if len(layout) != 1:
            raise PlacementError(
                f"duplex panel template: RX chain block {name} is multi-cell "
                "(only single-cell RX stages fit the row-8 band)")
        if cx < 1:
            raise PlacementError(
                "duplex panel template: RX chain too long for the row band")
        b.placement = Placement(chip, [PlacedCell(0, cx, h - 4, Face.WEST)])
        cx -= 1
    head_pos = (w - 3, h - 4)
    tail = chain[-1]
    tail_pos = (cx + 1, h - 4)
    notes.append(f"RX chain placed westward from {head_pos}: {', '.join(chain)}")

    # ---- Derived RX decoder params (read-via-ctl + descriptors + emit relay).
    ctl_entries, ctl_named, _ = _resolve_cell(catalog, blk, ctl_cell)
    if "lookup" not in ctl_entries or "data" not in ctl_named:
        raise PlacementError(
            f"duplex panel template: {blk.name}'s controller cell lacks "
            "'lookup'/'data'")
    emit_entries, emit_named, _ = _resolve_cell(catalog, blk, ret_cell)
    if ret_port not in emit_named:
        raise PlacementError(
            f"duplex panel template: {blk.name} cell {ret_cell} has no "
            f"register named {ret_port!r}")
    ret_route = [geo.x1_in_cell, (0, h - 2)]
    ret_hop = 31 - len(ret_route)        # read results LAND AT the fork broker
    blk.params["panel_hop"] = 2          # transits the (9,11) routing cell
    blk.params["read_addr_hop"] = 1
    blk.params["read_dest"] = int(ctl_named["data"])
    blk.params["read_entry"] = int(ctl_entries["lookup"])

    txxo_entries, txxo_named, _ = _resolve_cell(
        catalog, project.block(tx_xo_name), 0)

    # RX emit: relay the char DIRECTLY onto the TX crossover's data track_c —
    # up col 1 (free cells) and east along row 6 (sharing the TX egress
    # corridor's same-direction tail), landing ON the crossover relay.
    emit_route = ([emit_pos]
                  + [(1, y) for y in range(emit_pos[1] - 1, geo.xo[1] - 1, -1)]
                  + [(x, geo.xo[1]) for x in range(2, w - 2)] + [geo.xo])
    blk.params["emit_hop"] = len(emit_route) - 1
    blk.params["out_dest"] = int(txxo_named.get("relay", 20))
    blk.params["emit_jump_entry"] = int(txxo_entries["track_c"])
    # emit_jump_entry ADDS an instruction to the emit program — its entry
    # address and register map SHIFT. Re-resolve and compute the push-read
    # descriptors against the FINAL program: they target the FORK BROKER at
    # (0,10) (burst reg + its deliver entry — assembled with the build's own
    # _broker_program so the addresses agree by construction), which relays
    # the read result into the off-corridor emit cell.
    emit_entries, emit_named, _ = _resolve_cell(catalog, blk, ret_cell)
    wr_desc, jp_desc, ret_entry = _ret_broker_descs(
        f"{blk.name}_panel_return", emit_pos, int(emit_named[ret_port]),
        int(min(emit_entries.values())), ret_hop)
    blk.params["read_wr_desc"] = wr_desc
    blk.params["read_jp_desc"] = jp_desc
    notes.append(
        f"RX brokers: tap {tap_pos}, tail {tailxo_pos}, return fork "
        f"{(0, h - 2)}; emit {emit_pos}, emit_hop {len(emit_route) - 1}")

    # ---- Net rewrites + routes.
    results: list[RouteResult] = []

    def _drop(pred):
        dropped = [c for c in project.connections if pred(c)]
        project.connections[:] = [c for c in project.connections
                                  if not pred(c)]
        return dropped

    # RX input: x16_in -> chain head, route ending at the FREE tap cell one
    # short of the head (the standard broker convention — the build creates
    # the tap broker; TX-corridor words transit it at HOP<31).
    dropped_in = _drop(
        lambda c: isinstance(c.source, ChipPortEndpoint)
        and c.source.port == "x16_in"
        and isinstance(c.target, BlockEndpoint)
        and c.target.block == chain[0])
    in_sid = next((c.stream_id for c in dropped_in
                   if getattr(c, "stream_id", None)), None)
    in_port_name = next((c.target.port for c in dropped_in), "in")
    tap_route = ([(x, 0) for x in range(0, w - 1)]
                 + [(w - 2, y) for y in range(1, tap_pos[1] + 1)])
    project.connections.append(Connection(
        "rx_in_to_tap", ChipPortEndpoint(chip, "x16_in"),
        BlockEndpoint(chain[0], in_port_name), route=_rp(tap_route),
        stream_id=in_sid))
    results.append(RouteResult("rx_in_to_tap", True, points=list(tap_route)))

    # RX tail -> decoder input cell, route ending at the FREE tail-broker
    # cell abutting the acc (the build patches the tail's exit + programs
    # the broker; the TX ctl corridor transits the broker at HOP<31).
    tail_conn = next(
        (c for c in project.connections
         if isinstance(c.source, BlockEndpoint) and c.source.block == tail
         and isinstance(c.target, BlockEndpoint)
         and c.target.block == blk.name), None)
    if tail_conn is None:
        raise PlacementError(
            f"duplex panel template: no net from {tail} to {blk.name}")
    tail_route = ([tail_pos, (tail_pos[0], h - 3)]
                  + [(x, h - 3) for x in range(tail_pos[0] + 1, w - 2)]
                  + [tailxo_pos])
    tail_conn.route = _rp(tail_route)
    results.append(RouteResult(tail_conn.name, True, points=list(tail_route)))

    # RX return: x1_in -> the FORK BROKER at (0,10) -> emit (1,10). The route
    # ends AT the broker cell; the broker delivers EAST into the off-corridor
    # emit while the TX pushes transit it northward at HOP<31.
    # ``entry_override`` carries the emit cell's entry into the build's broker
    # delivery (broker_plan would otherwise use the BLOCK's default entry —
    # a different cell's address).
    ret_conn = next(
        (c for c in project.connections
         if isinstance(c.source, ChipPortEndpoint) and c.source.port == "x1_in"
         and isinstance(c.target, BlockEndpoint)
         and c.target.block == blk.name), None)
    if ret_conn is None:
        ret_conn = Connection(f"{blk.name}_panel_return",
                              ChipPortEndpoint(chip, "x1_in"),
                              BlockEndpoint(blk.name, ret_port), route=None)
        project.connections.append(ret_conn)
    ret_conn.route = _rp(ret_route)
    ret_conn.entry_override = ret_entry
    results.append(RouteResult(ret_conn.name, True, points=list(ret_route)))

    # RX egress: decoder emit -> col 1 -> row 6 -> the TX crossover's data
    # track_c -> x16_out. The original decoder->x16_out net is dropped (its
    # out_tag already lives in the TX crossover's dest_c).
    # out_tag annotates the RX chain's WIRE tag (dest_c) so stream_targets'
    # chain walk attributes the right demux tag — the walk otherwise ends at
    # the SHARED TX crossover and reads dest_b (the TX tag) for both streams.
    _drop(lambda c: isinstance(c.source, BlockEndpoint)
          and c.source.block == blk.name
          and isinstance(c.target, ChipPortEndpoint)
          and c.target.port == "x16_out")
    project.connections.append(Connection(
        "rx_egress", BlockEndpoint(blk.name, "out"),
        BlockEndpoint(tx_xo_name, "in"), route=_rp(emit_route),
        out_tag=(int(rx_out_tag) if rx_out_tag is not None else None)))
    results.append(RouteResult("rx_egress", True, points=list(emit_route)))
    return results, notes


def _apply_duplex_rx_half_kicker(project, catalog, ct, blk, req, *,
                                 tx_xo_name, chip: int = 0, rx_out_tag=None):
    """The KICKER-form duplex RX half (the streaming CW decoder beside the CW
    keyer TX). Geometry (10x12; the TX box is EMPTY — the keyer is chainless —
    so row 2 is free for the RX egress):

      x16_in corridor (shared row-0/col-8) → RX tap (8,7) → chain westward on
      row 7 → tail → (col, row 8) → tail xo (8,8) → detect (9,8) → classify
      (9,9) → RX ctl (9,10, panel_hop 2 via the (9,11) routing cell)
      → PANEL; x1_in → emit (0,10);
      emit → col-0 north → ret xo (0,2) → row-2 east → col xo (8,2) → east →
      col-9 north (the TX out corridor) → x16_out with the RX tag.
    """
    from engine.autoroute import RouteResult
    from engine.errors import PlacementError
    from model.block import Block
    from model.connection import (ABUTMENT_ROUTE, BlockEndpoint,
                                  ChipPortEndpoint, Connection, RoutePoint)
    from model.enums import Face
    from model.placement import Placement, PlacedCell

    notes: list[str] = ["duplex: RX half (kicker form)"]
    geo = _derive_geometry(ct)
    w, h = geo.w, geo.h
    lib = blk.library
    ctl_cell = req.get("controller_cell", 0)
    kick_cell = req.get("kicker_cell")
    in_cell_id = req.get("input_cell")
    ret_cell = req.get("return_cell", 1)
    ret_port = str(req.get("return_port") or "word")

    def _rp(pts):
        return [RoutePoint(x, y) for (x, y) in pts]

    # NO DSP cell on a corridor, taps as BROKERS, no retxo — see the
    # non-kicker form's note. ONE CrossoverBlock remains (colxo): the RX char
    # egress genuinely CROSSES the TX feed corridor at (8,2), and a crossing
    # is the one thing only a crossover cell can do (one fwd_face per cell).
    ctl_pos = (w - 1, h - 2)     # (9,10): panel words transit (9,11) (routing)
    kick_pos = (w - 1, h - 3)    # (9,9) faces SOUTH into the RX ctl
    in_pos = (w - 1, h - 4)      # (9,8) faces SOUTH into the kicker
    emit_pos = (1, h - 2)        # (1,10): OFF the return corridor, faces NORTH
    tap_pos = (w - 2, h - 5)     # (8,7): BROKER on the TX feed corridor
    tailxo_pos = (w - 2, h - 4)  # (8,8): BROKER delivering into detect
    colxo_pos = (w - 2, 2)       # (8,2): RX egress crosses the TX feed
    pos_by_id = {ctl_cell: (ctl_pos, Face.SOUTH),
                 kick_cell: (kick_pos, Face.SOUTH),
                 in_cell_id: (in_pos, Face.SOUTH),
                 ret_cell: (emit_pos, Face.NORTH)}
    blk.placement = Placement(chip, [
        PlacedCell(cid, pos_by_id[cid][0][0], pos_by_id[cid][0][1],
                   pos_by_id[cid][1])
        for cid in sorted(pos_by_id)])

    chain = _chain_before(project, blk.name)
    if not chain:
        raise PlacementError(
            f"duplex panel template: {blk.name} has no upstream RX chain")
    cx = w - 3
    for name in chain:
        b = project.block(name)
        layout = catalog.default_layout(b.type, b.params, library=b.library) \
            or {0: (0, 0, "west")}
        if len(layout) != 1 or cx < 1:
            raise PlacementError(
                f"duplex panel template: RX chain block {name} does not fit "
                "the row band (single-cell stages only)")
        b.placement = Placement(chip, [PlacedCell(0, cx, h - 5, Face.WEST)])
        cx -= 1
    tail = chain[-1]
    tail_pos = (cx + 1, h - 5)
    notes.append(f"RX chain placed westward from ({w - 3},{h - 5}): "
                 f"{', '.join(chain)}")

    # ---- Derived decoder params.
    ctl_entries, ctl_named, _ = _resolve_cell(catalog, blk, ctl_cell)
    if "lookup" not in ctl_entries or "data" not in ctl_named:
        raise PlacementError(
            f"duplex panel template: {blk.name}'s controller cell lacks "
            "'lookup'/'data'")
    k_entries, k_named, k_first = _resolve_cell(catalog, blk, kick_cell)
    ret_route = [geo.x1_in_cell, (0, h - 2)]
    ret_hop = 31 - len(ret_route)        # read results LAND AT the fork broker
    blk.params["panel_hop"] = 2          # transits the (9,11) routing cell
    blk.params["read_addr_hop"] = 1
    blk.params["read_dest"] = int(ctl_named["data"])
    blk.params["read_entry"] = int(ctl_entries["lookup"])
    blk.params["run_dest"] = int(k_named.get("run", 0))
    blk.params["run_entry"] = int(k_entries[k_first])

    # ---- The ONE remaining relay cell: the egress crossing.
    def _mk_xo(name, pos, face, params):
        if project.block(name) is None:
            project.blocks.append(Block(
                name, "CrossoverBlock", library=lib, params=params,
                placement=Placement(chip, [PlacedCell(0, pos[0], pos[1],
                                                      face)])))
        return project.block(name)

    # colxo: the RX chars cross the TX feed at (8,2) and exit east/north.
    col_exit = [(w - 1, 2), (w - 1, 1), (w - 1, 0)]
    colxo = _mk_xo(f"{blk.name}_colxo", colxo_pos, Face.SOUTH, {
        "face_a": "east", "hop_a": len(col_exit) + 1,
        "dest_a": int(rx_out_tag) if rx_out_tag is not None else 0,
        "entry_a": 0,
        "face_b": "south", "hop_b": 1, "dest_b": 0, "entry_b": 0,
        "face_c": "south", "hop_c": 1, "entry_c": 0,
        "restore_face": "south"})
    colxo_entries, colxo_named, _ = _resolve_cell(catalog, colxo, 0)

    # RX emit: chars ride col 1 north (free cells) then row 2 east, LANDING
    # ON the colxo relay which retags and exits them north to x16_out.
    emit_route = ([emit_pos]
                  + [(1, y) for y in range(emit_pos[1] - 1, 1, -1)]
                  + [(x, 2) for x in range(2, w - 2)] + [colxo_pos])
    blk.params["emit_hop"] = len(emit_route) - 1
    blk.params["out_dest"] = int(colxo_named.get("relay", 20))
    blk.params["emit_jump_entry"] = int(colxo_entries["track_a"])
    # emit_jump_entry grows the emit program — re-resolve for the descriptors,
    # which target the FORK BROKER at (0,10) (see _ret_broker_descs).
    emit_entries, emit_named, _ = _resolve_cell(catalog, blk, ret_cell)
    if ret_port not in emit_named:
        raise PlacementError(
            f"duplex panel template: {blk.name} cell {ret_cell} has no "
            f"register named {ret_port!r}")
    wr_desc, jp_desc, ret_entry = _ret_broker_descs(
        f"{blk.name}_panel_return", emit_pos, int(emit_named[ret_port]),
        int(min(emit_entries.values())), ret_hop)
    blk.params["read_wr_desc"] = wr_desc
    blk.params["read_jp_desc"] = jp_desc
    notes.append(
        f"RX brokers: tap {tap_pos}, tail {tailxo_pos}, return fork "
        f"{(0, h - 2)}; colxo {colxo_pos}; emit {emit_pos}, "
        f"emit_hop {len(emit_route) - 1}")

    # ---- Net rewrites + routes.
    results: list[RouteResult] = []

    def _drop(pred):
        dropped = [c for c in project.connections if pred(c)]
        project.connections[:] = [c for c in project.connections
                                  if not pred(c)]
        return dropped

    # RX input: x16_in -> chain head via the FREE tap-broker cell (the build
    # creates the broker; the TX-corridor words transit it at HOP<31).
    dropped_in = _drop(
        lambda c: isinstance(c.source, ChipPortEndpoint)
        and c.source.port == "x16_in"
        and isinstance(c.target, BlockEndpoint)
        and c.target.block == chain[0])
    in_sid = next((c.stream_id for c in dropped_in
                   if getattr(c, "stream_id", None)), None)
    in_port_name = next((c.target.port for c in dropped_in), "in")
    tap_route = ([(x, 0) for x in range(0, w - 1)]
                 + [(w - 2, y) for y in range(1, tap_pos[1] + 1)])
    project.connections.append(Connection(
        "rx_in_to_tap", ChipPortEndpoint(chip, "x16_in"),
        BlockEndpoint(chain[0], in_port_name), route=_rp(tap_route),
        stream_id=in_sid))
    results.append(RouteResult("rx_in_to_tap", True, points=list(tap_route)))

    # RX tail -> detect cell via the FREE tail-broker cell.
    tail_conn = next(
        (c for c in project.connections
         if isinstance(c.source, BlockEndpoint) and c.source.block == tail
         and isinstance(c.target, BlockEndpoint)
         and c.target.block == blk.name), None)
    if tail_conn is None:
        raise PlacementError(
            f"duplex panel template: no net from {tail} to {blk.name}")
    tail_route = ([tail_pos, (tail_pos[0], h - 4)]
                  + [(x, h - 4) for x in range(tail_pos[0] + 1, w - 2)]
                  + [tailxo_pos])
    tail_conn.route = _rp(tail_route)
    results.append(RouteResult(tail_conn.name, True, points=list(tail_route)))

    # RX return: x1_in -> the fork broker at (0,10) -> emit (1,10); the
    # emit's entry rides entry_override into the build's broker delivery.
    ret_conn = next(
        (c for c in project.connections
         if isinstance(c.source, ChipPortEndpoint) and c.source.port == "x1_in"
         and isinstance(c.target, BlockEndpoint)
         and c.target.block == blk.name), None)
    if ret_conn is None:
        ret_conn = Connection(f"{blk.name}_panel_return",
                              ChipPortEndpoint(chip, "x1_in"),
                              BlockEndpoint(blk.name, ret_port), route=None)
        project.connections.append(ret_conn)
    ret_conn.route = _rp(ret_route)
    ret_conn.entry_override = ret_entry
    results.append(RouteResult(ret_conn.name, True, points=list(ret_route)))

    _drop(lambda c: isinstance(c.source, BlockEndpoint)
          and c.source.block == blk.name
          and isinstance(c.target, ChipPortEndpoint)
          and c.target.port == "x16_out")
    project.connections.append(Connection(
        "rx_egress", BlockEndpoint(blk.name, "out"),
        BlockEndpoint(colxo.name, "in"), route=_rp(emit_route),
        out_tag=(int(rx_out_tag) if rx_out_tag is not None else None)))
    project.connections.append(Connection(
        "rx_egress_out", BlockEndpoint(colxo.name, "out"),
        ChipPortEndpoint(chip, "x16_out"), route=_rp([colxo_pos] + col_exit),
        out_tag=(int(rx_out_tag) if rx_out_tag is not None else None)))
    results += [RouteResult("rx_egress", True, points=list(emit_route)),
                RouteResult("rx_egress_out", True,
                            points=[colxo_pos] + col_exit)]
    return results, notes


def _chain_before(project, end_block: str) -> list[str]:
    """Block names UPSTREAM of ``end_block`` (walking block→block nets backward
    from it), returned in dataflow order (the x16_in-fed head first)."""
    from model.connection import BlockEndpoint

    pred: dict[str, list[str]] = {}
    for c in project.connections:
        if isinstance(c.source, BlockEndpoint) and isinstance(c.target,
                                                              BlockEndpoint):
            pred.setdefault(c.target.block, []).append(c.source.block)
    chain, cur, seen = [], end_block, {end_block}
    while True:
        prevs = [n for n in pred.get(cur, []) if n not in seen]
        if not prevs:
            break
        cur = prevs[0]
        seen.add(cur)
        chain.append(cur)
    chain.reverse()
    return chain


def _apply_rx_template(project, catalog, ct, blk, req, *, chip: int = 0):
    """RX-tail panel template: an UPSTREAM DSP chain feeds the panel-backed
    block, whose looked-up chars egress to x16_out (the Varicode-decoder shape).

    Corridor set (kyttar_10x12 corner topology; one crossover where the egress
    row crosses the input descent)::

        x16_in(0,0) → row0 east → XO(2,1) → track_a south col2 → chain (row 7,
        cols 2..) → tail → (row7 east, col7 south, row11) → input_cell(8,11)
        → [abut] controller(9,11) → PANEL;  x1_in(0,11) → emit(0,10);
        emit → col0 north → (0,1) → row1 east → XO track_b → (9,1) → x16_out(9,0)

    Derived params: the block's read words retarget the companion controller's
    ``data`` register + ``lookup`` entry (per-read R3/R4 descriptors — the
    shared-panel-safe protocol), the push-read descriptors encode the 2-cell
    return corridor, and the emit cell's raw WRITE(+JUMP) targets the
    crossover's relay/track_b."""
    from engine.autoroute import RouteResult
    from engine.errors import PlacementError
    from model.block import Block
    from model.connection import (BlockEndpoint, ChipPortEndpoint, Connection,
                                  RoutePoint)
    from model.enums import Face
    from model.placement import Placement, PlacedCell

    notes: list[str] = ["rx-tail template"]
    geo = _derive_geometry(ct)
    w, h = geo.w, geo.h
    lib = blk.library
    ctl_cell = req.get("controller_cell", 0)
    in_cell_id = req.get("input_cell")
    ret_cell = req.get("return_cell", 1)
    ret_port = str(req.get("return_port") or "word")

    def _rp(pts):
        return [RoutePoint(x, y) for (x, y) in pts]

    # ---- 1. Pin the panel block: controller AT x1_out, the ctl KICKER cell
    # abutting it from the west, the input cell next over (== the kicker when
    # the block has no separate one — the Varicode shape), and the consumer
    # (emit) one above the x1_in return port.
    kick_cell = req.get("kicker_cell", in_cell_id)
    ctl_pos = geo.x1_out_cell                    # (w-1, h-1)
    acc_pos = (w - 2, h - 1)                     # abuts the controller, faces E
    in_pos = acc_pos if kick_cell == in_cell_id else (w - 3, h - 1)
    emit_pos = (0, h - 2)                        # above x1_in, faces N
    # ORDER BY CELL ID: the build assigns build_cell_programs()[i] to
    # placement.cells[i] BY INDEX — an id-keyed order (ctl first) lands every
    # program on the wrong cell (the emit program at the accumulate position).
    pos_by_id = {ctl_cell: (ctl_pos, Face.SOUTH),
                 kick_cell: (acc_pos, Face.EAST),
                 in_cell_id: (in_pos, Face.EAST),
                 ret_cell: (emit_pos, Face.NORTH)}
    blk.placement = Placement(chip, [
        PlacedCell(cid, pos_by_id[cid][0][0], pos_by_id[cid][0][1],
                   pos_by_id[cid][1])
        for cid in sorted(pos_by_id)])

    # ---- 2. Place the UPSTREAM chain along row band h-5 (row 7 on 10x12),
    # flowing east from col 2 (below the input descent column).
    chain = _chain_before(project, blk.name)
    if not chain:
        raise PlacementError(
            f"rx panel template: {blk.name} declares input_cell (the RX-tail "
            "shape) but has no upstream chain from x16_in")
    band_y = h - 5
    cx = 2
    first_pos = None
    for name in chain:
        b = project.block(name)
        if b is None:
            continue
        layout = catalog.default_layout(b.type, b.params, library=b.library) \
            or {0: (0, 0, "east")}
        bw = max(dx for dx, _dy, _f in layout.values()) + 1
        bh = max(dy for _dx, dy, _f in layout.values()) + 1
        if cx + bw - 1 > w - 3 or band_y + bh - 1 > h - 2:
            raise PlacementError(
                f"rx panel template: upstream chain does not fit the row-"
                f"{band_y} band (placing {name} {bw}x{bh} at col {cx})")
        b.placement = Placement(chip, [
            PlacedCell(cid, cx + dx, band_y + dy, Face.from_str(f))
            for cid, (dx, dy, f) in layout.items()])
        if first_pos is None:
            first_pos = (cx, band_y)
        cx += bw
    tail = chain[-1]
    tail_blk = project.block(tail)
    tail_pos = (tail_blk.placement.cells[-1].x, tail_blk.placement.cells[-1].y)
    notes.append(f"upstream chain placed: {', '.join(chain)}")

    # ---- 3. Resolve the panel block's derived read/emit/descriptor params.
    ctl_entries, ctl_named, _ = _resolve_cell(catalog, blk, ctl_cell)
    if "lookup" not in ctl_entries or "data" not in ctl_named:
        raise PlacementError(
            f"rx panel template: {blk.name}'s controller cell lacks a "
            f"'lookup' entry / 'data' register (entries {sorted(ctl_entries)}, "
            f"named {sorted(ctl_named)})")
    emit_entries, emit_named, _ = _resolve_cell(catalog, blk, ret_cell)
    if ret_port not in emit_named:
        raise PlacementError(
            f"rx panel template: {blk.name} cell {ret_cell} has no register "
            f"named {ret_port!r} (have {sorted(emit_named)})")
    ret_route = [geo.x1_in_cell, emit_pos]       # (0,h-1) -> (0,h-2), ON emit
    ret_hop = 31 - len(ret_route)
    blk.params["panel_hop"] = 1
    blk.params["read_addr_hop"] = 1
    blk.params["read_dest"] = int(ctl_named["data"])
    blk.params["read_entry"] = int(ctl_entries["lookup"])
    blk.params["read_wr_desc"] = _wr(ret_hop, emit_named[ret_port])
    blk.params["read_jp_desc"] = _jp(ret_hop, min(emit_entries.values()))
    if kick_cell != in_cell_id:
        # Separate kicker (the CW classify cell): the input cell's run handoff
        # targets it @1 — derive its register/entry.
        k_entries, k_named, k_first = _resolve_cell(catalog, blk, kick_cell)
        blk.params["run_dest"] = int(k_named.get("run", 0))
        blk.params["run_entry"] = int(k_entries[k_first])
    notes.append(
        f"read via ctl.lookup (dest {ctl_named['data']}, entry "
        f"{ctl_entries['lookup']}); descriptors "
        f"rwd=0x{blk.params['read_wr_desc']:04X} "
        f"rjd=0x{blk.params['read_jp_desc']:04X} (hop {ret_hop})")

    # ---- 4. The crossover: input descent (col 2) x egress row (row 1).
    xo = (2, 1)
    in_entry, in_regs = catalog.resolved_io(blk.type, blk.params,
                                            library=blk.library)
    f_blk = project.block(chain[0])
    f_entry, f_regs = catalog.resolved_io(f_blk.type, f_blk.params,
                                          library=f_blk.library)
    egress = ([emit_pos] + [(0, y) for y in range(emit_pos[1] - 1, 0, -1)]
              + [(1, 1), xo])                   # emit -> col0 north -> row1 -> ON xo
    xo_to_out = ([xo] + [(x, 1) for x in range(3, w)] + [(w - 1, 0)])
    xo_name = f"{blk.name}_xover"
    if project.block(xo_name) is None:
        project.blocks.append(Block(
            xo_name, "CrossoverBlock", library=lib, params={
                # track_a: input relay SOUTH down col 2 into the chain head.
                "face_a": "south", "hop_a": first_pos[1] - xo[1],
                "dest_a": int(f_regs[0]) if f_regs else 0,
                "entry_a": int(f_entry),
                # track_b: egress relay EAST along row 1, out through the port.
                "face_b": "east", "hop_b": len(xo_to_out),
                "dest_b": 0, "entry_b": 0,
                # track_c: inert (no completion kick in the RX shape).
                "face_c": "south", "hop_c": 1, "entry_c": 0,
            },
            placement=Placement(chip, [PlacedCell(0, xo[0], xo[1],
                                                  Face.SOUTH)])))
    xo_entries, xo_named, _ = _resolve_cell(catalog, project.block(xo_name), 0)
    # The emit cell's raw egress words: land ON the crossover, deposit into its
    # relay register, and JUMP track_b (a WRITE-only egress would never fire
    # the relay).
    blk.params["emit_hop"] = len(egress) - 1
    blk.params["out_dest"] = int(xo_named.get("relay", 0))
    blk.params["emit_jump_entry"] = int(xo_entries["track_b"])
    # The added emit JUMP shifts the emit cell's entries/registers — recompute
    # the push-read descriptors against the FINAL program (see the duplex fn).
    emit_entries, emit_named, _ = _resolve_cell(catalog, blk, ret_cell)
    blk.params["read_wr_desc"] = _wr(ret_hop, emit_named[ret_port])
    blk.params["read_jp_desc"] = _jp(ret_hop, min(emit_entries.values()))
    notes.append(f"crossover at {xo}; emit_hop {len(egress) - 1}")

    # ---- 5. Net rewrites + corridor routes.
    results: list[RouteResult] = []

    def _drop(pred):
        dropped = [c for c in project.connections if pred(c)]
        project.connections[:] = [c for c in project.connections
                                  if not pred(c)]
        return dropped

    in_route = [(x, 0) for x in range(0, 3)] + [xo]      # (0,0)..(2,0), ON xo
    xo_to_chain = [xo] + [(2, y) for y in range(2, band_y + 1)]  # ON chain head
    dropped_in = _drop(
        lambda c: isinstance(c.source, ChipPortEndpoint)
        and c.source.port == "x16_in"
        and isinstance(c.target, BlockEndpoint) and c.target.block == chain[0])
    in_sid = next((c.stream_id for c in dropped_in
                   if getattr(c, "stream_id", None)), None)
    project.connections.append(Connection(
        "in_to_xo", ChipPortEndpoint(chip, "x16_in"),
        BlockEndpoint(xo_name, "in"), route=_rp(in_route), stream_id=in_sid))
    project.connections.append(Connection(
        "xo_to_chain", BlockEndpoint(xo_name, "out"),
        BlockEndpoint(chain[0], "in"), route=_rp(xo_to_chain)))
    results += [RouteResult("in_to_xo", True, points=list(in_route)),
                RouteResult("xo_to_chain", True, points=list(xo_to_chain))]

    # Tail -> panel block (the bit stream into the accumulator): east along the
    # band row to col w-3, south to row h-1, then ON the input cell.
    tail_route = ([tail_pos]
                  + [(x, tail_pos[1]) for x in range(tail_pos[0] + 1, w - 2)]
                  + [(w - 3, y) for y in range(tail_pos[1] + 1, h)]
                  + ([acc_pos] if in_pos == acc_pos else []))
    tail_conn = next(
        (c for c in project.connections
         if isinstance(c.source, BlockEndpoint) and c.source.block == tail
         and isinstance(c.target, BlockEndpoint)
         and c.target.block == blk.name), None)
    if tail_conn is None:
        raise PlacementError(
            f"rx panel template: no net from {tail} to {blk.name}")
    tail_conn.route = _rp(tail_route)
    results.append(RouteResult(tail_conn.name, True, points=list(tail_route)))

    # Return corridor.
    ret_conn = next(
        (c for c in project.connections
         if isinstance(c.source, ChipPortEndpoint) and c.source.port == "x1_in"
         and isinstance(c.target, BlockEndpoint)
         and c.target.block == blk.name), None)
    if ret_conn is None:
        ret_conn = Connection(f"{blk.name}_panel_return",
                              ChipPortEndpoint(chip, "x1_in"),
                              BlockEndpoint(blk.name, ret_port), route=None)
        project.connections.append(ret_conn)
    ret_conn.route = _rp(ret_route)
    results.append(RouteResult(ret_conn.name, True, points=list(ret_route)))

    # Egress: panel block -> crossover(track_b) -> x16_out.
    dropped_out = _drop(
        lambda c: isinstance(c.source, BlockEndpoint)
        and c.source.block == blk.name
        and isinstance(c.target, ChipPortEndpoint)
        and c.target.port == "x16_out")
    out_tag = next((c.out_tag for c in dropped_out
                    if getattr(c, "out_tag", None) is not None), None)
    if out_tag is not None:
        project.block(xo_name).params["dest_b"] = int(out_tag)
    project.connections.append(Connection(
        "egress_to_xo", BlockEndpoint(blk.name, "out"),
        BlockEndpoint(xo_name, "in"), route=_rp(egress),
        entry_override=int(xo_entries["track_b"])))
    project.connections.append(Connection(
        "xo_to_out", BlockEndpoint(xo_name, "out"),
        ChipPortEndpoint(chip, "x16_out"), route=_rp(xo_to_out),
        out_tag=out_tag))
    results += [RouteResult("egress_to_xo", True, points=list(egress)),
                RouteResult("xo_to_out", True, points=list(xo_to_out))]
    return results, notes


def _apply_self_contained_template(project, catalog, ct, blk, req, *,
                                   chip: int = 0):
    """SELF-CONTAINED panel template: place EVERY cell of a panel-backed block
    from the block's own ``default_layout``, with the embedded controller pinned
    on the ``x1_out`` port cell.

    Why this shape exists
    ---------------------
    The TX and RX templates above place only the cells NAMED AS ROLES in
    ``panel_requirements()`` — 2 for the TX shape, 3-4 for the RX shape. That is
    fine for a block whose every cell IS a role (the 3-cell Varicode decoder), but
    for a larger block it is a silent-dead build, in two ways at once:

    * the un-named cells get **no position** and are simply absent from the
      ``Placement`` — nothing raises, and no DRC check compares ``cell_count``
      against ``len(placement.cells)``; and
    * the build binds ``build_cell_programs()`` to ``placement.cells`` **by
      index**, so a 3-cell placement of a 7-cell block also lands programs 1 and 2
      on the controller's and the consumer's positions — the wrong programs on the
      wrong cells.

    So this template asks the block for its whole layout instead of inventing one,
    and the block owns the hard part: a layout whose internal edges actually
    deliver. The rule there is easy to get wrong — a word leaves on its SOURCE
    cell's face, but every cell it then arrives at forwards it on **that cell's
    own** face, so each cell has exactly ONE outgoing walk and all of its targets
    must lie along it. (A straight-line model, where the word keeps the sender's
    direction, is false; a layout built on it places, builds and DRCs clean and
    then HANGS.) Cells that must serve several directions do it the way
    ``LMSEqualizerBlock`` and ``MMTimingRecoveryBlock`` do — an in-program face
    flip, ``DataWord(is_face=True)`` plus ``MOVE [FACE], …``. See INV-48. This
    function only translates the layout onto the fabric and draws the three
    corridors around it; it does not check the walk.

    Geometry (kyttar_10x12 corner topology). The layout is translated so the
    controller lands on ``x1_out``, and the row it belongs to becomes the block's
    band along the chip's bottom edge::

        x16_in(0,0) → row 0 east → col (input cell x) south → INPUT CELL
        x1_in(0,11) → row 11 east → RETURN CELL      (the push-read corridor)
        EGRESS CELL → its column north → row 0 east → x16_out(9,0)

    The EGRESS cell is a free cell on the emit cell's own outgoing walk: the emit
    cell has one resting face, already committed toward the controller, so its
    ``out`` WRITE sets off the same way and has to land somewhere that is not the
    controller. The block leaves that cell blank in its layout; this template
    finds it and starts the output corridor there. A block whose layout leaves no
    such cell is rejected with a named error rather than built into a word that
    gets deflected into the SRAM port.
    """
    from engine.autoroute import RouteResult
    from engine.errors import PlacementError
    from model.connection import (BlockEndpoint, ChipPortEndpoint, Connection,
                                  RoutePoint)
    from model.enums import Face
    from model.placement import Placement, PlacedCell

    notes: list[str] = ["self-contained panel template"]
    geo = _derive_geometry(ct)
    w, h = geo.w, geo.h
    ctl_cell = req.get("controller_cell", 0)
    in_cell_id = req.get("input_cell")
    ret_cell = req.get("return_cell", 1)
    ret_port = str(req.get("return_port") or "word")

    def _rp(pts):
        return [RoutePoint(x, y) for (x, y) in pts]

    # ---- 1. Translate the block's OWN layout so the controller lands on x1_out.
    layout = catalog.default_layout(blk.type, blk.params, library=blk.library)
    if not layout:
        raise PlacementError(
            f"self-contained panel template: {blk.name} declares "
            "self_contained but has no default_layout to place")
    for need, what in ((ctl_cell, "controller_cell"), (in_cell_id, "input_cell"),
                       (ret_cell, "return_cell")):
        if need not in layout:
            raise PlacementError(
                f"self-contained panel template: {blk.name}'s {what} {need!r} is "
                f"not in its default_layout (cells {sorted(layout)})")
    cxl, cyl, _cf = layout[ctl_cell]
    ox = geo.x1_out_cell[0] - cxl
    oy = geo.x1_out_cell[1] - cyl
    pos = {cid: (dx + ox, dy + oy) for cid, (dx, dy, _f) in layout.items()}
    for cid, (px, py) in pos.items():
        if not (0 <= px < w and 0 <= py < h):
            raise PlacementError(
                f"self-contained panel template: {blk.name} cell {cid} lands at "
                f"({px},{py}), outside the {w}x{h} fabric — its default_layout "
                f"does not fit with the controller pinned at {geo.x1_out_cell}")
    # ORDER BY CELL ID: the build binds cell_programs()[i] to placement.cells[i]
    # BY INDEX, so the placed list must ascend by cell id (see the module note on
    # the RX template) or every program lands on the wrong cell.
    blk.placement = Placement(chip, [
        PlacedCell(cid, pos[cid][0], pos[cid][1],
                   Face.from_str(layout[cid][2]))
        for cid in sorted(pos)])
    notes.append("cells placed: "
                 + ", ".join(f"{cid}@{pos[cid]}" for cid in sorted(pos)))

    # ---- 2. The EGRESS cell: the first FREE cell on the return/emit cell's own
    # outgoing walk, before the controller. The emit cell's `out` WRITE sets off
    # on that same face, so this is where the output corridor must begin. NOTE
    # the walk is followed here as a straight line, which is only right while the
    # cells along it rest on one face; the general rule is that each transit cell
    # forwards on ITS OWN face (INV-48).
    occupied = {p: cid for cid, p in pos.items()}
    ret_pos = pos[ret_cell]
    _fd = {"east": (1, 0), "west": (-1, 0), "north": (0, -1), "south": (0, 1)}
    rdx, rdy = _fd[str(layout[ret_cell][2])]
    ctl_hop = None
    egress_cell = None
    emit_hop = None
    for k in range(1, 32):
        px, py = ret_pos[0] + rdx * k, ret_pos[1] + rdy * k
        if not (0 <= px < w and 0 <= py < h):
            break
        if occupied.get((px, py)) == ctl_cell:
            ctl_hop = k
            break
        if (px, py) not in occupied and egress_cell is None:
            egress_cell, emit_hop = (px, py), k
    if ctl_hop is None:
        raise PlacementError(
            f"self-contained panel template: {blk.name}'s return cell "
            f"{ret_cell} at {ret_pos} does not reach the controller along its "
            f"{layout[ret_cell][2]} face — the panel hand-offs cannot be issued")
    if egress_cell is None:
        raise PlacementError(
            f"self-contained panel template: {blk.name}'s return cell "
            f"{ret_cell} at {ret_pos} has NO free cell on its "
            f"{layout[ret_cell][2]} face before the controller (hop {ctl_hop}) — "
            "its output WRITE would transit the controller and be deflected into "
            "the SRAM port. Leave one cell of the layout blank on that walk.")
    notes.append(f"egress cell {egress_cell} (emit @{emit_hop}); "
                 f"controller @{ctl_hop}")

    # ---- 3. Derived panel params. The controller sits ON the port cell, so its
    # own WRITE/JUMP exit directly (@1); the block's emit cell reaches it at the
    # hop measured above.
    ctl_entries, ctl_named, _ = _resolve_cell(catalog, blk, ctl_cell)
    if "data" not in ctl_named:
        raise PlacementError(
            f"self-contained panel template: {blk.name}'s controller cell "
            f"{ctl_cell} has no 'data' register (have {sorted(ctl_named)})")
    emit_entries, emit_named, _ = _resolve_cell(catalog, blk, ret_cell)
    if ret_port not in emit_named:
        raise PlacementError(
            f"self-contained panel template: {blk.name} cell {ret_cell} has no "
            f"register named {ret_port!r} (have {sorted(emit_named)})")
    # Return corridor: x1_in, east along its row, landing ON the return cell.
    ret_route = ([geo.x1_in_cell]
                 + [(x, geo.x1_in_cell[1])
                    for x in range(geo.x1_in_cell[0] + 1, ret_pos[0] + 1)])
    if ret_route[-1] != ret_pos:
        raise PlacementError(
            f"self-contained panel template: {blk.name}'s return cell "
            f"{ret_pos} is not on the x1_in row {geo.x1_in_cell[1]} — the "
            "push-read corridor cannot reach it")
    ret_hop = 31 - len(ret_route)
    if ret_hop < 0:
        raise PlacementError(
            f"self-contained panel template: return corridor {len(ret_route)} "
            "hops exceeds the 31-hop budget")
    # Which entry the push-read result kicks. A block may name one
    # (``return_entry``) — the fetched word often re-enters a loop MID-BODY, and
    # the return cell's lowest-addressed entry is then the wrong door. Default to
    # that lowest entry, which is what the single-entry consumers want.
    ret_entry_name = req.get("return_entry")
    if ret_entry_name is not None:
        if ret_entry_name not in emit_entries:
            raise PlacementError(
                f"self-contained panel template: {blk.name} cell {ret_cell} has "
                f"no entry {ret_entry_name!r} (have {sorted(emit_entries)})")
        ret_entry = int(emit_entries[ret_entry_name])
    else:
        ret_entry = int(min(emit_entries.values()))
    blk.params["panel_hop"] = 1            # the controller IS the port cell
    blk.params["read_wr_desc"] = _wr(ret_hop, emit_named[ret_port])
    blk.params["read_jp_desc"] = _jp(ret_hop, ret_entry)
    notes.append(
        f"descriptors rwd=0x{blk.params['read_wr_desc']:04X} "
        f"rjd=0x{blk.params['read_jp_desc']:04X} (hop {ret_hop}, entry "
        f"{ret_entry_name or 'default'}@{ret_entry})")

    # ---- 4. Corridors + net rewrites.
    results: list[RouteResult] = []

    def _drop(pred):
        dropped = [c for c in project.connections if pred(c)]
        project.connections[:] = [c for c in project.connections if not pred(c)]
        return dropped

    # INPUT: x16_in -> row 0 east -> down the input cell's column -> ON the cell.
    in_pos = pos[in_cell_id]
    in_route = ([(x, geo.in_cell[1]) for x in range(geo.in_cell[0], in_pos[0] + 1)]
                + [(in_pos[0], y) for y in range(geo.in_cell[1] + 1, in_pos[1] + 1)])
    blocked = [p for p in in_route[:-1] if p in occupied]
    if blocked:
        raise PlacementError(
            f"self-contained panel template: the input corridor to {blk.name} "
            f"cell {in_cell_id} at {in_pos} is blocked by its own cells at "
            f"{blocked} — the input cell must be reachable down its column")
    dropped_in = _drop(
        lambda c: isinstance(c.source, ChipPortEndpoint)
        and c.source.port == "x16_in"
        and isinstance(c.target, BlockEndpoint) and c.target.block == blk.name)
    in_sid = next((c.stream_id for c in dropped_in
                   if getattr(c, "stream_id", None)), None)
    in_port_name = next((c.target.port for c in dropped_in), "in")
    project.connections.append(Connection(
        "in_to_block", ChipPortEndpoint(chip, "x16_in"),
        BlockEndpoint(blk.name, in_port_name), route=_rp(in_route),
        stream_id=in_sid))
    results.append(RouteResult("in_to_block", True, points=list(in_route)))

    # RETURN: the synthesized x1_in net gets the drawn corridor.
    ret_conn = next(
        (c for c in project.connections
         if isinstance(c.source, ChipPortEndpoint) and c.source.port == "x1_in"
         and isinstance(c.target, BlockEndpoint)
         and c.target.block == blk.name), None)
    if ret_conn is None:
        ret_conn = Connection(f"{blk.name}_panel_return",
                              ChipPortEndpoint(chip, "x1_in"),
                              BlockEndpoint(blk.name, ret_port), route=None)
        project.connections.append(ret_conn)
    ret_conn.route = _rp(ret_route)
    results.append(RouteResult(ret_conn.name, True, points=list(ret_route)))

    # EGRESS: the emit cell -> the free egress cell -> up its column -> row 0
    # east -> ON the x16_out port cell (the final waypoint takes the port's exit
    # face, the xo_to_out convention).
    # The route STARTS on the egress cell, not on the emit cell: the hop between
    # them is not a drawn corridor but the emit cell's own single-face WRITE
    # (measured as ``emit_hop`` above), and it TRANSITS the block's own cells,
    # which a waypoint list may not do. Starting here also keeps the polyline
    # contiguous — a route that skipped from the emit cell to the egress cell
    # would read as a fly line (``route_gap``).
    eg_route = ([egress_cell]
                + [(egress_cell[0], y)
                   for y in range(egress_cell[1] - 1, geo.out_cell[1] - 1, -1)]
                + [(x, geo.out_cell[1])
                   for x in range(egress_cell[0] + 1, geo.out_cell[0] + 1)])
    blocked = [p for p in eg_route[:-1] if p in occupied]
    if blocked:
        raise PlacementError(
            f"self-contained panel template: the egress corridor from "
            f"{blk.name} is blocked by its own cells at {blocked}")
    dropped_out = _drop(
        lambda c: isinstance(c.source, BlockEndpoint)
        and c.source.block == blk.name
        and isinstance(c.target, ChipPortEndpoint)
        and c.target.port == "x16_out")
    out_tag = next((c.out_tag for c in dropped_out
                    if getattr(c, "out_tag", None) is not None), None)
    out_port_name = next((c.source.port for c in dropped_out), "out")
    project.connections.append(Connection(
        "block_to_out", BlockEndpoint(blk.name, out_port_name),
        ChipPortEndpoint(chip, "x16_out"), route=_rp(eg_route),
        out_tag=out_tag))
    results.append(RouteResult("block_to_out", True, points=list(eg_route)))
    # The emit cell AUTHORS its own `out` WRITE/JUMP (RAW_OUTPUT_HOPS): the same
    # cell issues the panel protocol, so the build's exit patch must not touch
    # it. Give it the measured hop to the egress cell; the word then rides the
    # drawn corridor to the port. The dest carries the net's output TAG so
    # several chains sharing one port stay distinguishable on the wire.
    if "emit_hop" in blk.params:
        blk.params["emit_hop"] = int(emit_hop)
        blk.params["out_dest"] = int(out_tag) if out_tag is not None else 0
        blk.params["emit_entry"] = 0
        notes.append(f"emit authored: hop {emit_hop} -> {egress_cell}, "
                     f"dest {blk.params['out_dest']}")
    notes.append(f"corridors: in {len(in_route)}, return {len(ret_route)}, "
                 f"egress {len(eg_route)}")
    return results, notes


# --------------------------------------------------------------------------- #
# Build-time refresh of placement-derived panel parameters
# --------------------------------------------------------------------------- #

def _route_cells(conn):
    return [(p.x, p.y) for p in conn.route] if isinstance(conn.route, list) else []


def refresh_panel_params(project, catalog, *, chip: int = 0) -> list[str]:
    """Re-derive every placement-dependent panel parameter from the CURRENT
    routes, at build time — so a user-moved / hand-rerouted panel design builds
    with CORRECT descriptors instead of silently keeping stale ones.

    The panel chain's parameters are functions of the routed geometry: the
    push-read descriptors encode the return-corridor length + the consumer
    cell's register/entry; the crossover's track hops encode the corridor
    lengths; the RAW keyer's emit/done targets encode the egress length + the
    crossover's entries. The auto-P&R template sets them once — but the GUI
    lets the user move blocks and redraw routes, after which those baked values
    are WRONG. This refresh recomputes each parameter FROM ITS ROUTE whenever
    that route exists (a missing/unrouted corridor leaves the parameter alone —
    the unrouted-net DRC names the real problem). Called by BuildEngine.build,
    so 'the build derives it from the routes' holds for panel params exactly as
    it does for faces. Returns human-readable notes of what changed."""
    from model.connection import BlockEndpoint, ChipPortEndpoint

    notes: list[str] = []
    backed = panel_backed_blocks(project, catalog)
    if not backed:
        return notes
    blk, req = backed[0]
    if blk.placement is None or not blk.placement.cells:
        return notes
    cells = {c.cell_id: (c.x, c.y) for c in blk.placement.cells}
    ctl_cell_id = req.get("controller_cell", 0)
    ret_cell_id = req.get("return_cell", 1)
    ctl_pos = cells.get(ctl_cell_id)
    ret_pos = cells.get(ret_cell_id)
    ret_port = str(req.get("return_port") or "word")

    def _set(params, key, val, owner):
        if key in params and params[key] != val:
            notes.append(f"{owner}.{key}: {params[key]} -> {val}")
            params[key] = val

    # --- push-read descriptors from the x1_in return corridor -----------------
    ret_conn = next(
        (c for c in project.connections
         if isinstance(c.source, ChipPortEndpoint) and c.source.port == "x1_in"
         and isinstance(c.target, BlockEndpoint) and c.target.block == blk.name),
        None)
    if ret_conn is not None and _route_cells(ret_conn) and ret_pos is not None:
        pts = _route_cells(ret_conn)
        # Transits: every route cell (incl. the x1_in port cell) plus the
        # landing on the consumer when the route stops one short of it.
        transits = len(pts) if pts[-1] == ret_pos else len(pts) + 1
        hopf = 31 - transits
        if hopf >= 0:
            entries, named, _ = _resolve_cell(catalog, blk, ret_cell_id)
            if ret_port in named and entries:
                # Honour a block-declared ``return_entry`` — the fetched word may
                # re-enter a loop MID-BODY, in which case the return cell's
                # lowest-addressed entry is the WRONG door and re-deriving it here
                # would silently undo what the template chose (the LZ4 match copy:
                # landing on ``fetch`` instead of ``emit_mat`` re-issues the read
                # and spins). Blocks that name no entry keep the historical min.
                _re = req.get("return_entry")
                _entry = (int(entries[_re]) if _re in entries
                          else int(min(entries.values())))
                _set(blk.params, "read_wr_desc", _wr(hopf, named[ret_port]),
                     blk.name)
                _set(blk.params, "read_jp_desc", _jp(hopf, _entry), blk.name)

    # --- crossover track params from its corridors ----------------------------
    xo_blk = next((b for b in project.blocks
                   if b.type == "CrossoverBlock" and b.placement is not None
                   and b.placement.cells), None)
    if xo_blk is not None:
        xo_pos = (xo_blk.placement.cells[0].x, xo_blk.placement.cells[0].y)
        ctl_entries, ctl_named, ctl_first = _resolve_cell(catalog, blk,
                                                          ctl_cell_id)
        _e, _regs = catalog.resolved_io(blk.type, blk.params,
                                        library=blk.library)

        def _transits_from(pts, src_pos, tgt_pos):
            if pts and src_pos is not None and pts[0] == src_pos:
                pts = pts[1:]
            if not pts:
                return None
            return len(pts) if (tgt_pos is not None and pts[-1] == tgt_pos) \
                else len(pts) + 1

        # track_a/c: crossover -> controller.
        c_ctl = next((c for c in project.connections
                      if isinstance(c.source, BlockEndpoint)
                      and c.source.block == xo_blk.name
                      and isinstance(c.target, BlockEndpoint)
                      and c.target.block == blk.name), None)
        if c_ctl is not None and _route_cells(c_ctl) and ctl_pos is not None:
            hops = _transits_from(_route_cells(c_ctl), xo_pos, ctl_pos)
            if hops is not None:
                _set(xo_blk.params, "hop_a", hops, xo_blk.name)
                # hop_c mirrors hop_a ONLY for the completion-relay track (the
                # CW keyer). A DATA track_c (dest_c set — the duplex RX egress
                # to the port) keeps its own hop; re-deriving it from the ctl
                # corridor landed the RX chars one row short of the exit.
                if xo_blk.params.get("dest_c") is None:
                    _set(xo_blk.params, "hop_c", hops, xo_blk.name)
                if _regs:
                    _set(xo_blk.params, "dest_a", int(_regs[0]), xo_blk.name)
                if ctl_first in ctl_entries:
                    _set(xo_blk.params, "entry_a", int(ctl_entries[ctl_first]),
                         xo_blk.name)
                comp = str(req.get("completion_entry") or "")
                if comp in ctl_entries:
                    _set(xo_blk.params, "entry_c", int(ctl_entries[comp]),
                         xo_blk.name)
        # track_b: crossover -> x16_out (exit through the port cell = +1).
        c_out = next((c for c in project.connections
                      if isinstance(c.source, BlockEndpoint)
                      and c.source.block == xo_blk.name
                      and isinstance(c.target, ChipPortEndpoint)
                      and c.target.port == "x16_out"), None)
        if c_out is not None and _route_cells(c_out):
            pts = _route_cells(c_out)
            if pts and pts[0] == xo_pos:
                pts = pts[1:]
            if pts:
                # pts ends ON the port cell; exit costs one more hop.
                _set(xo_blk.params, "hop_b", len(pts) + 1, xo_blk.name)

        # --- RAW emit/done targets (the CW keyer) from the egress corridor ----
        gb = None
        try:
            gb = catalog.instantiate(blk.type, "__raw_probe__", blk.params,
                                     library=blk.library)
        except Exception:  # noqa: BLE001
            gb = None
        if (gb is not None and getattr(gb, "RAW_OUTPUT_HOPS", False)
                and "emit_hop" in blk.params):
            c_eg = next((c for c in project.connections
                         if isinstance(c.source, BlockEndpoint)
                         and c.source.block == blk.name
                         and isinstance(c.target, BlockEndpoint)
                         and c.target.block == xo_blk.name), None)
            if c_eg is not None and _route_cells(c_eg) and ret_pos is not None:
                hops = _transits_from(_route_cells(c_eg), ret_pos, xo_pos)
                if hops is not None:
                    xo_entries, xo_named, _ = _resolve_cell(catalog, xo_blk, 0)
                    _set(blk.params, "emit_hop", hops, blk.name)
                    if "emit_dest" in blk.params:
                        # The keyer convention: emit_dest/emit_entry name the
                        # crossover relay + track_b JUMP.
                        _set(blk.params, "emit_dest",
                             int(xo_named.get("relay", 0)), blk.name)
                        if "track_b" in xo_entries:
                            _set(blk.params, "emit_entry",
                                 int(xo_entries["track_b"]), blk.name)
                    if "out_dest" in blk.params:
                        # The RX-decoder convention: out_dest is the relay reg,
                        # emit_jump_entry the track_b JUMP (emit_entry keeps its
                        # push-read meaning — never clobber it here).
                        _set(blk.params, "out_dest",
                             int(xo_named.get("relay", 0)), blk.name)
                        if ("emit_jump_entry" in blk.params
                                and "track_b" in xo_entries):
                            _set(blk.params, "emit_jump_entry",
                                 int(xo_entries["track_b"]), blk.name)
                    if "done_entry" in blk.params and "track_c" in xo_entries:
                        _set(blk.params, "done_entry",
                             int(xo_entries["track_c"]), blk.name)
    return notes
