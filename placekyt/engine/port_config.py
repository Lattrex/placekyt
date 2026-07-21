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
        land = landings.get(conn.name)
        if land is not None:
            # Built-corridor landing: the cell/entry/hop the routed corridor actually
            # delivers to (resolved against built faces + broker entries).
            entry_addr = int(land["entry"])
            hop_count = int(land["hop"])
            data_addrs = list(land["data_addrs"]) or [0]
        else:
            cell0 = blk.placement.cells[0]
            dist = abs(cell0.x - in_port.cell_x) + abs(cell0.y - in_port.cell_y)
            entry, in_regs = catalog.resolved_io(
                blk.type, blk.params, library=blk.library)
            entry_addr = int(entry)
            hop_count = 30 - dist
            # A FLOAT source into a complex block delivers ONLY the single rail its
            # net targets (xq stays 0); only a COMPLEX source delivers all input regs.
            if in_regs and len(in_regs) > 1 and getattr(conn, "src_complex", None) is False:
                data_addrs = [_target_port_reg(catalog, blk, conn.target.port, in_regs)]
            else:
                data_addrs = list(in_regs) if in_regs else [0]
        out_tag, term_block = out_tag_of_block.get(blk.name, (None, None))
        # Is the chain's terminal (output-driving) block a COMPLEX-output cell? Then
        # its I and Q rails egress on TWO consecutive tags (out_tag, out_tag+1) — the
        # host reassembles them into an interleaved I/Q stream (mirrors complex input).
        complex_out = False
        if term_block is not None:
            tb = project.block(term_block)
            if tb is not None:
                spec = catalog.get(tb.type, library=tb.library)
                complex_out = bool(spec) and len(spec.output_registers) > 1
        targets[str(sid)] = {
            "entry_addr": entry_addr,
            "hop_count": hop_count,
            "data_addrs": data_addrs,
            "in_port": in_port.name,
            "out_tag": out_tag,
            "complex_out": complex_out,
        }
    return targets


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


def _chain_out_tags(project, chip_id, in_port_name):
    """``{input-block name: (out_tag, terminal_block_name)}`` — for each block fed
    directly by the input port, the ``out_tag`` of the ``…→x16_out`` net its forward
    chain ends at, plus the NAME of the block driving that output net (so the caller
    can tell if the chain's egress is complex — two rails on two consecutive tags).

    Walks the block→block forward graph from each input block to the block whose
    output targets a chip output port, and reads that net's ``out_tag``. Linear
    chains only (the modem's case); a fan-out stops at the first output net found.
    """
    # block name -> list of downstream block names (block→block nets)
    fwd: dict[str, list[str]] = {}
    # block name -> out_tag if it feeds an output port
    out_net_tag: dict[str, int] = {}
    for conn in project.connections:
        s, t = conn.source, conn.target
        if isinstance(s, BlockEndpoint) and isinstance(t, BlockEndpoint):
            fwd.setdefault(s.block, []).append(t.block)
        elif (isinstance(s, BlockEndpoint)
              and isinstance(t, ChipPortEndpoint)
              and str(t.port).endswith("_out")):
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
            frontier.extend(fwd.get(b, []))
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
                and conn.target.port.endswith("_out")):
            candidates.append((conn.target.chip, conn.target.port))
    if not candidates:
        return None
    return max(candidates, key=lambda c: c[0])
