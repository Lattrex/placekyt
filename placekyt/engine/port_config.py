# SPDX-License-Identifier: GPL-3.0-or-later
"""Host-side port configuration for driving a built design (Qt-free).

A built chip needs HOST-side routing info to inject/capture samples through its
I/O ports: which input port feeds the design, the entry address + hop count +
data register to steer injected samples to the first block, and which output
port carries the result. This is NOT part of the chip bitstream — it's how the
host (the simulator, the GNURadio bridge, the .kbs metadata) talks to the ports.

Extracted from the SimController so the CLI build (and any headless consumer)
derives the same config the GUI sim uses. Pure data — takes ``project`` +
``registry`` + ``catalog``, imports no Qt.
"""

from __future__ import annotations

from model.connection import BlockEndpoint, ChipPortEndpoint


def _target_port_reg(catalog, block, port, in_regs):
    """The single input register a named target ``port`` maps to (via the block's
    PortMap), for a float-source net that delivers ONE operand to that rail. Falls
    back to the block's first input reg if the port can't be resolved to a register."""
    try:
        pmap = catalog.port_map(block.type, block.params, library=block.library)
        for p in pmap.ports:
            if p.name == port and p.direction == "in" and p.register is not None:
                return int(p.register)
    except Exception:  # noqa: BLE001
        pass
    return int(in_regs[0]) if in_regs else 0


def _target_port_pair_idx(catalog, block, port, in_regs):
    """POSITIONAL indices of the (I, Q) register pair a complex-source net's
    target ``port`` selects within ``in_regs``, for a block with 2+ complete
    input I/Q pairs (AddCC/SubCC/MultiplyCC: ai aq bi bq). The importer wires a
    complex stream's net to the pair's I half ('ai'/'bi'); its Q sibling is the
    NEXT input register. None if the port can't be resolved (callers keep the
    full-list legacy behaviour)."""
    reg = None
    try:
        pmap = catalog.port_map(block.type, block.params, library=block.library)
        for p in pmap.ports:
            if p.name == port and p.direction == "in" and p.register is not None:
                reg = int(p.register)
                break
    except Exception:  # noqa: BLE001
        pass
    if reg is None:
        return None
    regs = [int(r) for r in in_regs]
    if reg not in regs:
        return None
    i = regs.index(reg)
    return [i, i + 1] if i + 1 < len(regs) else [i]


def _built_landing(build_result, chip_id, conn_name):
    """The build's corridor-accurate ``{cell, entry, hop, data_addrs}`` landing for
    ``conn_name`` on ``chip_id``, or ``None`` if the design isn't built / the net has
    no recorded landing. ``BuildResult.chips`` is keyed by chip id; each ChipBuild's
    ``input_landings`` is keyed by connection name (see build._resolve_input_landings)."""
    cb = getattr(build_result, "chips", {}).get(chip_id) if build_result else None
    if cb is None:
        return None
    return (getattr(cb, "input_landings", {}) or {}).get(conn_name)


def input_port_config(project, registry, catalog, chip_id: int = 0,
                      build_result=None):
    """``(port_name, {entry_addr, hop_count, data_addr})`` for the block fed by
    ``chip_id``'s input port, or ``None``.

    The fed block is the target of an explicit ``x16_in → block`` route on this
    chip, or — when there's no such route — the block whose first cell sits on
    (or nearest to) the input-port cell (the common "block at the port" case).

    ``build_result`` (optional): when the design has been BUILT, the ``ChipBuild``'s
    ``input_landings`` carries the CORRIDOR-ACCURATE injection landing (cell / entry /
    hop / data-addrs) — the hop the routed corridor + broker actually delivers to.
    Prefer it: a Manhattan estimate (``30 - straight-line distance``) is WRONG whenever
    the corridor snakes or a broker delivers into a NON-corner input cell of a
    multi-cell block (e.g. the ComplexMixer's ``phase`` cell reached via a broker one
    hop past the corridor end). Using the manhattan hop there consumes the JUMP a cell
    short, the block never fires, and the whole chain produces NO output. Falls back to
    the manhattan model only when unbuilt.
    """
    chip = project.chip(chip_id)
    type_name = (chip.type_name if chip and chip.type_name
                 else project.chip_type)
    ct = registry.require(type_name).chip_type
    in_port = next((p for p in ct.ports if p.direction.value == "input"), None)
    if in_port is None:
        return None

    # 1. An explicit x16_in → block route on this chip.
    target = None
    target_conn = None
    for conn in project.connections:
        if (isinstance(conn.source, ChipPortEndpoint)
                and conn.source.chip == chip_id
                and conn.source.port == in_port.name
                and isinstance(conn.target, BlockEndpoint)):
            target = project.block(conn.target.block)
            target_conn = conn
            break

    # BUILT corridor-accurate landing (preferred): the build walked this net's physical
    # corridor against the realized faces and recorded the exact cell/entry/hop/data —
    # the single source of truth the server MUST inject at. Use it verbatim when present.
    if build_result is not None and target_conn is not None:
        land = _built_landing(build_result, chip_id, target_conn.name)
        if land is not None:
            das = land.get("data_addrs") or [0]
            return (in_port.name, {
                "entry_addr": int(land["entry"]),
                "hop_count": int(land["hop"]) & 0x1F,
                "data_addr": int(das[0]),
            })
    # 2. Else the block on this chip nearest the input-port cell.
    if target is None:
        best = None
        for blk in project.blocks:
            pl = blk.placement
            if pl is None or pl.chip != chip_id or not pl.cells:
                continue
            c0 = pl.cells[0]
            d = abs(c0.x - in_port.cell_x) + abs(c0.y - in_port.cell_y)
            if best is None or d < best[0]:
                best = (d, blk)
        if best is not None:
            target = best[1]
    if target is None or target.placement is None or not target.placement.cells:
        return None

    cell0 = target.placement.cells[0]
    dist = abs(cell0.x - in_port.cell_x) + abs(cell0.y - in_port.cell_y)
    entry, in_regs = catalog.resolved_io(
        target.type, target.params, library=target.library)
    return (in_port.name, {
        "entry_addr": entry,
        "hop_count": 30 - dist,
        "data_addr": in_regs[0] if in_regs else 0,
    })


def stream_targets(project, registry, catalog, chip_id: int = 0,
                   build_result=None):
    """Resolve EVERY ``x16_in → block`` input net on ``chip_id`` to its injection
    parameters, keyed by the net's ``stream_id``. Returns
    ``{stream_id: {entry_addr, hop_count, data_addrs, in_port, out_tag}}``.

    This is the multi-stream generalization of :func:`input_port_config`: a
    shared input port may feed SEVERAL blocks (the full-duplex modem fans x16_in
    to both the TX mapper and the RX matched filter). Each input net carries a
    ``stream_id`` (set by the GRC importer from the source block's ``stream_id``
    param); the live bridge looks a stream up here so each burst is injected at
    the right block's entry/hop/data-register WITHOUT the GR source knowing any
    placement-dependent value. ``out_tag`` is the matching ``block-chain →
    x16_out`` net's tag (so the sink can demux its own recovered words); None if
    the chain's output isn't tagged.

    A net with no ``stream_id`` is skipped (it uses the single-stream
    :func:`input_port_config` path). ``data_addrs`` is the block's full input
    register list (e.g. ``[xi, xq]`` for a complex block), so the bridge injects
    each operand to the right register.

    ``build_result`` (optional): when given, the per-net injection landing is read
    from the BUILT corridor (``ChipBuild.input_landings`` — the cell/entry/hop the
    routed corridor actually delivers to, resolved against the built faces + broker
    entries) instead of a manhattan straight line. This is REQUIRED for the off-port
    multi-filament auto-P&R layout, where two input corridors share a cell that one
    stream pins to a face diverting the other (the modem's rx corridor pins (1,1)
    EAST, so the tx word must LAND at (1,1)'s broker, not ride straight to the
    mapper). Without it (or for a net the build didn't resolve) the legacy manhattan
    ``30 - dist`` to the block's first cell is used — correct for the proven
    explicit-placement path where each block sits on its straight inject corridor.
    """
    chip = project.chip(chip_id)
    type_name = (chip.type_name if chip and chip.type_name
                 else project.chip_type)
    ct = registry.require(type_name).chip_type
    in_port = next((p for p in ct.ports if p.direction.value == "input"), None)
    if in_port is None:
        return {}

    # The build's per-net injection landing (cell/entry/hop/data_addrs from the routed
    # corridor), keyed by connection name. Absent ⇒ legacy manhattan resolution.
    landings = {}
    if build_result is not None:
        cb = getattr(build_result, "chips", {}).get(chip_id)
        if cb is not None:
            landings = getattr(cb, "input_landings", {}) or {}

    # Map each placed block to the out_tag of its chain's x16_out net, so a
    # stream's recovered words can be demuxed by tag. The chain's LAST block (the
    # one wired to x16_out) carries out_tag; we attribute that tag to the chain's
    # INPUT block by walking forward block→block. For the simple linear chains the
    # modem uses, that's a forward reachability walk from each input block.
    out_tag_of_block = _chain_out_tags(project, chip_id, in_port.name)

    targets: dict = {}
    for conn in project.connections:
        if not (isinstance(conn.source, ChipPortEndpoint)
                and conn.source.chip == chip_id
                and conn.source.port == in_port.name
                and isinstance(conn.target, BlockEndpoint)):
            continue
        sid = getattr(conn, "stream_id", None)
        if not sid:
            continue  # single-stream net — uses input_port_config instead
        blk = project.block(conn.target.block)
        if blk is None or blk.placement is None or not blk.placement.cells:
            continue
        # CROSS-CHIP stream: the input net enters THIS chip's port but its target
        # block lives on a DOWNSTREAM chip (it transits this chip's bus to a far
        # gain). The single-chip hop math (30 - dist to a same-chip cell) is wrong
        # for it — multi_chip_stream_targets resolves the composite cross-chip hop.
        if blk.placement.chip != chip_id:
            continue
        land = landings.get(conn.name)
        entry, in_regs = catalog.resolved_io(
            blk.type, blk.params, library=blk.library)
        if land is not None:
            # Built-corridor landing: the cell/entry/hop the routed corridor actually
            # delivers to (resolved against built faces + broker entries).
            entry_addr = int(land["entry"])
            hop_count = int(land["hop"])
            data_addrs = list(land["data_addrs"]) or [0]
        else:
            cell0 = blk.placement.cells[0]
            dist = abs(cell0.x - in_port.cell_x) + abs(cell0.y - in_port.cell_y)
            entry_addr = int(entry)
            hop_count = 30 - dist
            # A FLOAT source into a complex block delivers ONLY the single rail its
            # net targets (xq stays 0); only a COMPLEX source delivers all input regs.
            if in_regs and len(in_regs) > 1 and getattr(conn, "src_complex", None) is False:
                data_addrs = [_target_port_reg(catalog, blk, conn.target.port, in_regs)]
            else:
                data_addrs = list(in_regs) if in_regs else [0]
        # TWO-COMPLEX-PAIR blocks (AddCC / SubCC / MultiplyCC: 4 input registers
        # = two I/Q pairs): a complex source net targets ONE of the pairs, so this
        # stream must deliver ITS pair only — stream b lands on (bi, bq); handing
        # it the full 4-register list would make the bridge write b's sample into
        # ai/aq and clobber stream a (measured: the join fires on two copies of b
        # and the block egresses nothing meaningful). data_addrs (landing or
        # resolved) is positional with the block's input registers, so slice the
        # pair at the target port's register index.
        if (in_regs is not None and len(in_regs) > 2
                and len(data_addrs) == len(in_regs)
                and getattr(conn, "src_complex", None) is True):
            pair_idx = _target_port_pair_idx(catalog, blk, conn.target.port,
                                             in_regs)
            if pair_idx is not None:
                data_addrs = [data_addrs[i] for i in pair_idx]
        out_tag, term_block = out_tag_of_block.get(blk.name, (None, None))
        # Is the chain's terminal (output-driving) block a COMPLEX-output cell? Then
        # its I and Q rails egress on TWO consecutive tags (out_tag, out_tag+1) — the
        # host reassembles them into an interleaved I/Q stream (mirrors complex input).
        # Two detections, OR-ed: the spec's declared output registers (NCO/mixer
        # style), and the PROJECT nets themselves — the AddCC family declares ONE
        # interface output register (the INV-17 packet emitter) yet the importer
        # synthesizes a real yq→port sibling net on tag out_tag+1, which is the
        # ground truth of what egresses.
        complex_out = False
        if term_block is not None:
            tb = project.block(term_block)
            if tb is not None:
                spec = catalog.get(tb.type, library=tb.library)
                complex_out = bool(spec) and len(spec.output_registers) > 1
            if not complex_out and out_tag is not None:
                complex_out = any(
                    isinstance(c2.source, BlockEndpoint)
                    and c2.source.block == term_block
                    and isinstance(c2.target, ChipPortEndpoint)
                    and getattr(c2, "out_tag", None) == out_tag + 1
                    for c2 in project.connections)
        # A JOIN fan-out stream (one stream_id, SEVERAL port→block arms — the
        # audio-effects echo/tremolo/comb) has one landing PER ARM; the bridge
        # must inject every landing per sample. ``landings`` collects them in
        # trigger-last order: a data-only arm (entry_override → the join's
        # ``sink`` entry) is injected first so every operand is fresh when the
        # TRIGGER arm's JUMP finally fires the combiner. The top-level
        # entry/hop/data_addrs stay = the FIRST landing (single-arm streams —
        # every pre-existing design — are byte-identical to before).
        landing = {"entry_addr": entry_addr, "hop_count": hop_count,
                   "data_addrs": data_addrs,
                   "is_trigger": getattr(conn, "entry_override", None) is None}
        key = str(sid)
        if key in targets:
            arms = targets[key]["landings"]
            arms.append(landing)
            arms.sort(key=lambda a: a["is_trigger"])   # data-only first
            first = arms[0]
            targets[key].update(entry_addr=first["entry_addr"],
                                hop_count=first["hop_count"],
                                data_addrs=first["data_addrs"])
            if targets[key]["out_tag"] is None and out_tag is not None:
                targets[key].update(out_tag=out_tag, complex_out=complex_out)
            continue
        targets[key] = {
            "entry_addr": entry_addr,
            "hop_count": hop_count,
            "data_addrs": data_addrs,
            "in_port": in_port.name,
            "out_tag": out_tag,
            "complex_out": complex_out,
            "landings": [landing],
        }
    # TWO-INPUT-STREAM CHAINS (AddCC / SubCC / MultiplyCC): both ingress streams
    # walk forward to the SAME chain-output net, so both would claim its out_tag
    # — and the duplex demux hands each drained word to the FIRST claiming
    # stream in the RPC's submission order, which is client thread order, i.e.
    # NONDETERMINISTIC. Deterministic contract instead: the FIRST ingress stream
    # in project-connection order (the .grc's first-input wire) OWNS the chain's
    # out_tag; later streams sharing it resolve out_tag=None. A flowgraph names
    # its sink after the block's FIRST input's stream (complex_math's 'sum' /
    # 'diff'/'prod' sources feed each block's first port).
    seen_tags: set = set()
    for cfg in targets.values():
        tag = cfg.get("out_tag")
        if tag is None:
            continue
        if tag in seen_tags:
            cfg["out_tag"] = None
            cfg["complex_out"] = False
        else:
            seen_tags.add(tag)
    return targets


def multi_chip_stream_targets(project, registry, catalog, build_result=None):
    """Resolve input streams across ALL chips for the multi-chip GRC bridge.

    The multi-chip generalization of :func:`stream_targets`: it runs the per-chip
    resolver on every ``project.chip`` and augments each entry with the two things
    the multi-chip live bridge needs beyond the single-chip case:

      * ``chip_id`` — WHICH chip the stream feeds (placement-derived: the block's
        chip). The bridge injects the burst on that chip's head and demuxes its
        recovered words by ``(chip_id, out_tag)``. The GRC source/sink are unchanged
        — chip_id rides here, resolved from placement, exactly like hop/entry.
      * ``routed`` — is the head block reached via a corridor (landing cell != the
        chip's input-port cell) rather than sitting AT the landing? A routed head
        needs the WRITE+JUMP inter-chip/injection path (the routed-input .so); an
        at-landing head uses the raw queue. Derived from the built ``input_landings``.

    Stream-id collisions across chips are namespaced ``"<chip_id>:<stream_id>"`` so
    two chains can both carry e.g. ``rx`` without clobbering each other; the plain
    ``stream_id`` is preserved in the entry for the GR sink's demux. Returns
    ``{key: {..stream_targets fields.., chip_id, routed, stream_id}}``.

    Additive — does NOT touch the single-chip :func:`stream_targets` path.

    Each entry also carries ``out_chip`` — the chain TAIL where this stream's
    recovered words emerge (its head chip's chain, walking the inter-chip wires
    forward until a chip with no outgoing inter-chip link). For a single-chip chain
    ``out_chip == chip_id``.
    """
    # Chain tail per chip: follow inter-chip from_chip -> to_chip forward until no
    # outgoing wire. (Depth <= 2 on the 2P2S board, but this handles any linear chain.)
    _next = {ic.from_chip: ic.to_chip for ic in project.inter_chip_connections}

    def _tail(cid, _guard=None):
        _guard = _guard or set()
        while cid in _next and cid not in _guard:
            _guard.add(cid)
            cid = _next[cid]
        return cid

    merged: dict = {}
    for chip in project.chips:
        cid = chip.id
        ct = registry.require(chip.type_name or project.chip_type).chip_type
        in_port = next((p for p in ct.ports
                        if p.direction.value == "input"), None)
        port_cell = (in_port.cell_x, in_port.cell_y) if in_port else (0, 0)
        landings = {}
        if build_result is not None:
            cb = getattr(build_result, "chips", {}).get(cid)
            if cb is not None:
                landings = getattr(cb, "input_landings", {}) or {}
        per = stream_targets(project, registry, catalog, cid,
                             build_result=build_result)
        for sid, tgt in per.items():
            # Routed iff the built landing cell for this stream's net != port cell.
            routed = False
            for land in landings.values():
                if tuple(land.get("cell", port_cell)) != tuple(port_cell):
                    routed = True
                    break
            entry = dict(tgt)
            entry["chip_id"] = cid
            entry["out_chip"] = _tail(cid)
            entry["routed"] = routed
            entry["stream_id"] = sid
            key = sid if sid not in merged else f"{cid}:{sid}"
            merged[key] = entry

    # --- CROSS-CHIP streams: enter a HEAD port, tap a FAR chip's block ----------
    # A stream whose input net is chip_H.x16_in -> block-on-chip_F (F downstream of
    # H in the chain). It transits chip_H's agnostic bus and lands on chip_F's gain.
    # Composite hop = (chip_F's OWN landing hop, from chip_F.x16_in) - (chip_H's bus
    # crossing, x16_in->x16_out). The word is injected on chip_H's HEAD with that
    # hop; it rides chip_H's bus, crosses the boundary, and lands at chip_F's block.
    from model.connection import BlockEndpoint, ChipPortEndpoint as _CPE

    def _far_landing(far_chip, block):
        """The FAR block's landing as seen from chip_F's OWN x16_in: hop = 30 -
        dist(port -> block cell), entry/data from the block's resolved IO. Computed
        from placement directly (not input_landings, which can carry MULTIPLE nets
        on a chip and pick the wrong one)."""
        fct = registry.require(
            (project.chip(far_chip).type_name if project.chip(far_chip) else None)
            or project.chip_type).chip_type
        fip = next((p for p in fct.ports if p.direction.value == "input"), None)
        if fip is None or block.placement is None or not block.placement.cells:
            return None
        c0 = block.placement.cells[0]
        dist = abs(c0.x - fip.cell_x) + abs(c0.y - fip.cell_y)
        entry, in_regs = catalog.resolved_io(
            block.type, block.params, library=block.library)
        return {"hop": 30 - dist, "entry": int(entry),
                "data_addrs": list(in_regs) if in_regs else [0]}

    def _bus_crossing(head_chip):
        """chip_H's transit-bus width: x16_in cell -> x16_out cell manhattan +1
        (the exit hop). e.g. (0,0)->(9,0) = 9, +1 = 10."""
        hct = registry.require(
            (project.chip(head_chip).type_name if project.chip(head_chip) else None)
            or project.chip_type).chip_type
        ip = next((p for p in hct.ports if p.direction.value == "input"), None)
        op = next((p for p in hct.ports
                   if p.direction.value == "output" and p.name.endswith("_out")), None)
        if ip is None or op is None:
            return 10
        return abs(op.cell_x - ip.cell_x) + abs(op.cell_y - ip.cell_y) + 1

    for conn in project.connections:
        src, tg = conn.source, conn.target
        if not (isinstance(src, _CPE) and src.port.endswith("_in")
                and isinstance(tg, BlockEndpoint)):
            continue
        sid = getattr(conn, "stream_id", None)
        if not sid:
            continue
        blk = project.block(tg.block)
        if blk is None or blk.placement is None:
            continue
        head, far = src.chip, blk.placement.chip
        if head == far:
            continue  # same-chip — already handled above
        own = _far_landing(far, blk)
        if own is None:
            continue
        composite_hop = int(own["hop"]) - _bus_crossing(head)
        # out_tag from the FAR block's own output net (blk -> x16_out on the far
        # chip): the gain writes this tag as its output WRITE dest, and the tag rides
        # across the transparent wire to the chain tail so the host demuxes this
        # stream's words there. (The far block's chain may itself continue, but a
        # simple gain drives x16_out directly.)
        far_tag = None
        for oc in project.connections:
            os_, ot = oc.source, oc.target
            if (isinstance(os_, BlockEndpoint) and os_.block == blk.name
                    and isinstance(ot, _CPE) and str(ot.port).endswith("_out")
                    and not str(ot.port).startswith("x1_")):   # x1_* = panel port
                far_tag = getattr(oc, "out_tag", None)
                break
        e = {
            "entry_addr": int(own["entry"]),
            "hop_count": composite_hop,
            "data_addrs": list(own["data_addrs"]) or [0],
            "in_port": src.port,
            "out_tag": far_tag,
            "complex_out": False,
            "chip_id": head,               # injected on the HEAD chip
            "out_chip": _tail(head),       # emerges at the chain tail
            "routed": True,                # a far tap is always routed
            "stream_id": sid,
        }
        key = sid if sid not in merged else f"{head}:{sid}"
        merged[key] = e
    return merged


def batch_reset_writes(build_result, chip_id: int = 0) -> list:
    """The chip's per-batch (packet-boundary) state resets from a BuildResult:
    a list of ``(x, y, addr, value)``.

    Mirrors :func:`stream_targets`' resolve-from-the-build pattern so the host
    (SimController) wires the reset list into the SimServer the same way it wires
    stream_targets. The list is resolved by the build from the placed design's
    ``reset_per_batch`` StateVars (``engine.build._resolve_batch_reset_writes``)
    and lives on ``ChipBuild.batch_reset_writes``. Empty when no block flags any
    reset state, or when the build didn't produce this chip.
    """
    if build_result is None:
        return []
    cb = getattr(build_result, "chips", {}).get(chip_id)
    if cb is None:
        return []
    return list(getattr(cb, "batch_reset_writes", []) or [])


class ShapeChange(ValueError):
    """A requested param value would CHANGE the block's compiled shape (cell
    set, assembly template, entries, or data-word/face layout) — not expressible
    as coefficient WRITEs to the running fabric. The server refuses the write."""


def _prog_structure(progs) -> dict:
    """The shape-identity fingerprint of a block's cell programs: everything
    EXCEPT non-face data-word values. Two program sets with equal structure
    differ only in coefficients — exactly what live WRITEs can express."""
    out = {}
    for cid, p in progs.items():
        out[cid] = (
            getattr(p, "assembly_template", ""),
            tuple(sorted((e.name for e in (getattr(p, "entries", None) or [])))),
            tuple(sorted((pt.name, pt.register)
                         for pt in (getattr(p, "inputs", None) or []))),
            tuple(sorted((dw.name, dw.address, bool(getattr(dw, "is_face", False)),
                          # A FACE word's value is orientation-transformed by the
                          # build — a param that changes one is a shape change.
                          dw.value if getattr(dw, "is_face", False) else None)
                         for dw in (getattr(p, "data", None) or []))),
        )
    return out


def live_coeff_writes(project, registry, catalog, chip_id: int = 0,
                      build_result=None) -> dict:
    """Resolve every LIVE-TUNABLE block param on ``chip_id`` to its coefficient
    WRITE plan: ``{block_name: {params, hops, to_writes}}``.

    A param is live-tunable when SOME cell program of the block stores it as a
    SAME-NAMED ``DataWord`` (the GainBlock pattern: ``DataWord("gain", …,
    address=1)``) — single- AND multi-cell blocks (CoherentRX's ``kp``/``ki``
    live in its ``loop_filter`` cell). Re-writing those data words on the
    running fabric retunes the block with no reflash — the sim injects the same
    WRITE-to-(hop, dest) words the hardware path sends over USB, so the two
    backends stay behaviourally identical.

    Map entry:
    - ``params``: ``{param_name: design_value}`` — every tunable param of the
      block, with its built value (seeds the server's change-dedup: an
      advertised value that merely MATCHES the design never writes — a
      gratuitous WRITE is not free, it can misdeliver on broker-routed layouts).
    - ``hops``: ``{cell_id: hop}`` for every cell holding a tunable word. The
      routed injection hop when the build recorded a corridor landing AT that
      cell (port-fed heads), else the manhattan model (``30 - dist``) — valid
      for straight-line/abutted-chain-reachable cells (proven for chained
      single-cell blocks; a broker corridor may divert — the caveat is
      per-design, see the lessons log).
    - ``to_writes(values: dict) -> [(cell_id, dest, word), …]``: re-instantiates
      the block with the design params OVERLAID with ``values`` and diffs the
      compiled data words — the exact fixed-point conversion the block itself
      applies, including DERIVED words (a param that recomputes several data
      words yields several writes). Raises :class:`ShapeChange` when the new
      value alters anything BUT non-face data-word values (different template,
      entries, cell set, data-word layout, or a face word) — the server refuses
      such a write, so a shape-changing value can never corrupt a running chip.
    """
    chip = project.chip(chip_id)
    type_name = (chip.type_name if chip and chip.type_name
                 else project.chip_type)
    ct = registry.require(type_name).chip_type
    in_port = next((p for p in ct.ports if p.direction.value == "input"), None)
    if in_port is None:
        return {}

    # Built corridor landings keyed by TARGET block name (port-fed heads only).
    landings_by_block: dict[str, dict] = {}
    for conn in project.connections:
        if (isinstance(conn.source, ChipPortEndpoint)
                and conn.source.chip == chip_id
                and conn.source.port == in_port.name
                and isinstance(conn.target, BlockEndpoint)):
            land = _built_landing(build_result, chip_id, conn.name)
            if land is not None:
                landings_by_block[conn.target.block] = land

    out: dict[str, dict] = {}
    for blk in project.blocks:
        pl = blk.placement
        if pl is None or pl.chip != chip_id or not pl.cells:
            continue
        params = dict(blk.params or {})
        try:
            inst = catalog.instantiate(blk.type, blk.name, params,
                                       library=blk.library)
            progs = inst.build_cell_programs()
        except Exception:  # noqa: BLE001 — an uninstantiable block is not tunable
            continue
        # Program cell key -> PLACED cell (placement cells carry the program's
        # cell_id — orientation-independent, int or string keys alike).
        placed = {pc.cell_id: pc for pc in pl.cells}
        if set(progs.keys()) - set(placed.keys()):
            continue  # placement/programs disagree — not resolvable here

        tunable: dict[str, object] = {}     # param name -> design value
        cells_needed: set = set()
        for cid, prog in progs.items():
            for dw in (getattr(prog, "data", None) or []):
                pname = dw.name
                if (pname not in params
                        or not isinstance(params[pname], (int, float))
                        or isinstance(params[pname], bool)):
                    continue
                # Auto-packed (address=None) words get their address from the
                # assembler — not resolvable here; face codes are orientation-
                # transformed by the placer. Both are sound omissions.
                if dw.address is None or getattr(dw, "is_face", False):
                    continue
                tunable[pname] = params[pname]
                cells_needed.add(cid)
        if not tunable:
            continue

        hops: dict = {}
        ok = True
        for cid in cells_needed:
            cell = placed[cid]
            land = landings_by_block.get(blk.name)
            if land is not None and tuple(land.get("cell", ())) == (cell.x, cell.y):
                hops[cid] = int(land["hop"]) & 0x1F
                continue
            dist = abs(cell.x - in_port.cell_x) + abs(cell.y - in_port.cell_y)
            if 30 - dist < 0:
                ok = False
                break
            hops[cid] = 30 - dist
        if not ok:
            continue

        base_structure = _prog_structure(progs)
        base_words = {(cid, dw.address): int(dw.value) & 0xFFFF
                      for cid, p in progs.items()
                      for dw in (getattr(p, "data", None) or [])
                      if dw.address is not None
                      and not getattr(dw, "is_face", False)}

        def _to_writes(values, _type=blk.type, _name=blk.name, _lib=blk.library,
                       _params=params, _tunable=dict(tunable),
                       _structure=base_structure, _base=base_words):
            p = dict(_params)
            for k, v in (values or {}).items():
                if k not in _tunable:
                    continue
                # Coerce to the DESIGN param's type (an int param like `n`
                # arrives as a float from a GRC slider).
                p[k] = int(round(float(v))) if isinstance(_tunable[k], int) \
                    else float(v)
            inst2 = catalog.instantiate(_type, _name, p, library=_lib)
            progs2 = inst2.build_cell_programs()
            if _prog_structure(progs2) != _structure:
                raise ShapeChange(
                    f"{_type}.{_name}: value change alters the block's compiled "
                    f"shape — not live-tunable at {values!r}")
            writes = []
            for cid, prog2 in progs2.items():
                for dw2 in (getattr(prog2, "data", None) or []):
                    if dw2.address is None or getattr(dw2, "is_face", False):
                        continue
                    w = int(dw2.value) & 0xFFFF
                    if _base.get((cid, dw2.address)) != w:
                        writes.append((cid, int(dw2.address), w))
            return writes

        out[blk.name] = {"params": {k: (float(v) if not isinstance(v, int)
                                        else int(v)) for k, v in tunable.items()},
                         "hops": hops, "to_writes": _to_writes}
    return out


def _chip_bus_crossing(project, registry, chip_id: int) -> int:
    """chip ``chip_id``'s transit-bus width for a word riding THROUGH it to the
    next chip in its chain: x16_in cell -> x16_out cell manhattan + 1 (the exit
    hop). Mirrors ``multi_chip_stream_targets``' ``_bus_crossing``."""
    ct = registry.require(
        (project.chip(chip_id).type_name if project.chip(chip_id) else None)
        or project.chip_type).chip_type
    ip = next((p for p in ct.ports if p.direction.value == "input"), None)
    op = next((p for p in ct.ports
               if p.direction.value == "output" and p.name.endswith("_out")), None)
    if ip is None or op is None:
        return 10
    return abs(op.cell_x - ip.cell_x) + abs(op.cell_y - ip.cell_y) + 1


def multi_chip_live_coeff_writes(project, registry, catalog,
                                 build_result=None) -> dict:
    """The multi-chip generalization of :func:`live_coeff_writes` (the 2P2S
    board's chains): every tunable block on EVERY chip, with the injection
    re-based to its CHAIN HEAD — a WRITE enters the head chip's input port and
    self-routes across the inter-chip wires exactly like a stream word (the
    same composite-hop arithmetic ``multi_chip_stream_targets`` uses:
    far-chip local hop minus each transit chip's bus-crossing width).

    Adds ``chip_id`` (the HEAD chip to inject on) to each entry."""
    # incoming inter-chip wire map: to_chip -> from_chip (each chain is a line).
    incoming = {ic.to_chip: ic.from_chip
                for ic in getattr(project, "inter_chip_connections", []) or []}
    out: dict[str, dict] = {}
    for chip in project.chips:
        local = live_coeff_writes(project, registry, catalog, chip.id,
                                  build_result=build_result)
        if not local:
            continue
        # Walk back to the chain head, accumulating each TRANSIT chip's
        # crossing width (for a 2-chip chain: just the head's).
        head, crossing = chip.id, 0
        seen = set()
        while head in incoming and head not in seen:
            seen.add(head)
            head = incoming[head]
            crossing += _chip_bus_crossing(project, registry, head)
        for bname, spec in local.items():
            hops = {cid: h - crossing for cid, h in spec["hops"].items()}
            if any(h < 0 for h in hops.values()):
                continue  # deeper than the hop field can express — sound omission
            out[bname] = {**spec, "hops": hops, "chip_id": head}
    return out


def _chain_out_tags(project, chip_id, in_port_name):
    """``{input-block name: (out_tag, terminal_block_name)}`` — for each block fed
    directly by the input port, the ``out_tag`` of the ``…→x16_out`` net its forward
    chain ends at, plus the NAME of the block driving that output net (so the caller
    can tell if the chain's egress is complex — two rails on two consecutive tags).

    Walks the block→block forward graph from each input block to the block whose
    output targets a chip output port, and reads that net's ``out_tag``. Linear
    chains only (the modem's case); a fan-out stops at the first output net found.
    """
    # block name -> list of (downstream block, edge tag) (block→block nets).
    # An edge-level out_tag annotates the chain's WIRE tag when the chain ends
    # at a SHARED relay (the duplex TX crossover carries dest_b for the TX
    # stream and dest_c for the RX stream — the terminal's own net tag is the
    # TX one, so the RX chain's edge into it carries its tag explicitly).
    fwd: dict[str, list[tuple]] = {}
    # block name -> out_tag if it feeds an output port
    out_net_tag: dict[str, int] = {}
    for conn in project.connections:
        s, t = conn.source, conn.target
        if isinstance(s, BlockEndpoint) and isinstance(t, BlockEndpoint):
            fwd.setdefault(s.block, []).append(
                (t.block, getattr(conn, "out_tag", None)))
        elif (isinstance(s, BlockEndpoint)
              and isinstance(t, ChipPortEndpoint)
              and str(t.port).endswith("_out")
              and not str(t.port).startswith("x1_")):
            # x1_* is the SRAM-PANEL port pair, not a stream egress: the duplex
            # template's face-setting rxctl→x1_out net would otherwise mark the
            # RX ctl's block as this chain's terminal with out_tag None — the
            # RX stream then demuxed on tag None and recovered NOTHING over the
            # GR client loop (while the headless port-reading gates still
            # passed). Only x16-class ports terminate a stream chain here.
            #
            # A COMPLEX-output block feeds the port on TWO nets (yi→tag N, yq→tag N+1);
            # keep the LOWER (I-rail) tag as the chain's base so the complex-egress
            # collector reads I on out_tag and Q on out_tag+1 (not the reverse). For a
            # single real output there's just one net and this is a no-op.
            t_new = getattr(conn, "out_tag", None)
            t_old = out_net_tag.get(s.block, None)
            if t_old is None:
                out_net_tag[s.block] = t_new
            elif t_new is not None:
                out_net_tag[s.block] = min(t_old, t_new)

    result: dict[str, tuple] = {}
    for conn in project.connections:
        if not (isinstance(conn.source, ChipPortEndpoint)
                and conn.source.chip == chip_id
                and conn.source.port == in_port_name
                and isinstance(conn.target, BlockEndpoint)):
            continue
        # BFS forward to the first block that feeds an output port.
        seen = set()
        frontier = [conn.target.block]
        tag = None
        term = None
        while frontier:
            b = frontier.pop(0)
            if b in seen:
                continue
            seen.add(b)
            if b in out_net_tag:
                tag = out_net_tag[b]
                term = b
                break
            nxt = fwd.get(b, [])
            # An edge annotated with an explicit tag IS the chain's wire tag
            # (the shared-relay case) — take it and stop.
            edge = next(((tb, et) for tb, et in nxt if et is not None), None)
            if edge is not None:
                tag, term = edge[1], edge[0]
                break
            frontier.extend(tb for tb, _et in nxt)
        result[conn.target.block] = (tag, term)
    return result


_OP_WRITE = 0x6
_OP_JUMP = 0x7


def values_to_bitstream(values, port_cfg) -> list[int]:
    """Wrap a plain value list into a self-contained bitstream of WRITE+DATA+JUMP
    bursts, using a design's input-port config (``{entry_addr, hop_count,
    data_addr}`` from :func:`input_port_config`).

    Each value ``v`` becomes one burst delivered to the block at the input port::

        WRITE  hop=hop_count, dest=data_addr     ; steer the data word
        <v>                                      ; the data word itself
        JUMP   hop=hop_count, entry=entry_addr   ; trigger the block

    ``hop_count`` is the raw 5-bit hop FIELD (``31 - hops``) the port used to
    inject — the same value :func:`input_port_config` returns and the legacy
    ``set_port_target_hop_count`` consumed. This is the bridge that lets a
    design with a value-list / ramp stimulus run through the single bitstream
    injection path (the words ARE the bursts)."""
    hop = port_cfg["hop_count"] & 0x1F
    dest = port_cfg["data_addr"] & 0x1F
    entry = port_cfg["entry_addr"] & 0x1F
    write = (_OP_WRITE << 12) | (hop << 5) | dest
    jump = (_OP_JUMP << 12) | (hop << 5) | entry
    words: list[int] = []
    for v in values:
        words += [write, int(v) & 0xFFFF, jump]
    return words


def output_port_target(project):
    """``(chip_id, port_name)`` of the design's final output port, or ``None``.

    The output is a chip OUTPUT port that a block routes to. With multiple chips,
    prefer the LAST chip's output (the end of the signal chain).
    """
    candidates = []
    for conn in project.connections:
        if (isinstance(conn.source, BlockEndpoint)
                and isinstance(conn.target, ChipPortEndpoint)
                and conn.target.port.endswith("_out")
                and not conn.target.port.startswith("x1_")):  # x1_* = panel port
            candidates.append((conn.target.chip, conn.target.port))
    if not candidates:
        return None
    return max(candidates, key=lambda c: c[0])
