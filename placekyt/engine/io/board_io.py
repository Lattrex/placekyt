"""Load dev-board configurations from ``.kdb`` YAML (the architecture notes §2.4).

Enforces the ``wire_delay_ns >= 1.0`` rule at load time (§2.4: zero-delay
inter-chip wires break causal ordering in multi-chip simulation).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from model.board import (
    Board,
    BoardChip,
    BoardInterface,
    ChipConnection,
    FpgaConnection,
)

from ._mapping import opt, opt_seq, require, require_mapping
from .errors import SchemaError
from .safe_yaml import load_yaml, load_yaml_str

MIN_WIRE_DELAY_NS = 1.0


def load_board(path: str | Path) -> Board:
    doc = require_mapping(load_yaml(path), "board file")
    return _board_from_doc(doc, source=str(path))


def board_from_str(text: str, *, source: str = "<string>") -> Board:
    doc = require_mapping(load_yaml_str(text, source=source), "board")
    return _board_from_doc(doc, source=source)


def _board_from_doc(doc: Any, *, source: str) -> Board:
    board = require_mapping(require(doc, "board", source), f"{source}.board")

    iface_node = opt(board, "interface", {})
    interface = BoardInterface(
        type=str(opt(iface_node, "type", "ftdi_serial")),
        vid=int(opt(iface_node, "vid", 0x0403)),
        pid=int(opt(iface_node, "pid", 0x6014)),
        baud=int(opt(iface_node, "baud", 3_000_000)),
        protocol=str(opt(iface_node, "protocol", "lattrex_v1")),
    )

    chips = tuple(
        BoardChip(
            id=int(require(require_mapping(c, f"{source}.chips[{i}]"), "id", source)),
            type=str(require(c, "type", source)),
            label=str(opt(c, "label", "")),
        )
        for i, c in enumerate(opt_seq(board, "chips", f"{source}.board"))
    )

    chip_connections = tuple(
        _chip_connection(c, source=f"{source}.chip_connections[{i}]")
        for i, c in enumerate(opt_seq(board, "chip_connections", f"{source}.board"))
    )

    fpga_connections = tuple(
        _fpga_connection(c, source=f"{source}.fpga_connections[{i}]")
        for i, c in enumerate(opt_seq(board, "fpga_connections", f"{source}.board"))
    )

    return Board(
        name=str(require(board, "name", f"{source}.board")),
        manufacturer=str(opt(board, "manufacturer", "Lattrex")),
        version=str(opt(board, "version", "1.0")),
        description=str(opt(board, "description", "")),
        interface=interface,
        bitstream_slots=int(opt(board, "bitstream_slots", 4)),
        fpga_sram_kb=int(opt(board, "fpga_sram_kb", 512)),
        chips=chips,
        chip_connections=chip_connections,
        fpga_connections=fpga_connections,
    )


def _endpoint(node: Any, key: str, source: str) -> tuple[int, str]:
    ep = require_mapping(require(node, key, source), f"{source}.{key}")
    return (
        int(require(ep, "chip", f"{source}.{key}")),
        str(require(ep, "port", f"{source}.{key}")),
    )


def _chip_connection(node: Any, *, source: str) -> ChipConnection:
    node = require_mapping(node, source)
    from_chip, from_port = _endpoint(node, "from", source)
    to_chip, to_port = _endpoint(node, "to", source)
    delay = float(opt(node, "wire_delay", MIN_WIRE_DELAY_NS))
    if delay < MIN_WIRE_DELAY_NS:
        raise SchemaError(
            f"{source}.wire_delay: {delay} ns is below the {MIN_WIRE_DELAY_NS} ns "
            "minimum — zero-delay inter-chip wires break causal ordering in "
            "multi-chip simulation."
        )
    return ChipConnection(
        from_chip=from_chip,
        from_port=from_port,
        to_chip=to_chip,
        to_port=to_port,
        type=str(opt(node, "type", "direct_wire")),
        wire_delay_ns=delay,
    )


def _fpga_connection(node: Any, *, source: str) -> FpgaConnection:
    node = require_mapping(node, source)
    return FpgaConnection(
        name=str(require(node, "name", source)),
        fpga_port=str(require(node, "fpga_port", source)),
        chip=int(require(node, "chip", source)),
        chip_port=str(require(node, "chip_port", source)),
        chip_port_out=(
            str(node["chip_port_out"]) if node.get("chip_port_out") else None
        ),
    )
