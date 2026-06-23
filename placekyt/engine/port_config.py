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


def input_port_config(project, registry, catalog, chip_id: int = 0):
    """``(port_name, {entry_addr, hop_count, data_addr})`` for the block fed by
    ``chip_id``'s input port, or ``None``.

    The fed block is the target of an explicit ``x16_in → block`` route on this
    chip, or — when there's no such route — the block whose first cell sits on
    (or nearest to) the input-port cell (the common "block at the port" case).
    ``hop_count = 30 - distance(port_cell, block_anchor)``; entry / input
    register come from the block's resolved v2 layout.
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
    for conn in project.connections:
        if (isinstance(conn.source, ChipPortEndpoint)
                and conn.source.chip == chip_id
                and conn.source.port == in_port.name
                and isinstance(conn.target, BlockEndpoint)):
            target = project.block(conn.target.block)
            break
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
