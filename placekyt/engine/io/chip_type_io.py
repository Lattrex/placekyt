"""Load chip-type definitions from YAML (the architecture notes §2.3).

Read-only for v1.0: chip types are fixed hardware descriptions provided by
Lattrex (bundled or dropped into ``~/.placekyt/chips/``), not edited in the IDE.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from model.chip_type import ChipType, PortSpec, Timing
from model.enums import Face, PortDirection

from ._mapping import opt, opt_seq, require, require_mapping
from .errors import SchemaError
from .safe_yaml import load_yaml


def load_chip_type(path: str | Path) -> ChipType:
    """Parse a chip-type ``.yaml`` into a :class:`ChipType`."""
    doc = require_mapping(load_yaml(path), "chip-type file")
    return _chip_type_from_doc(doc, source=str(path))


def chip_type_from_str(text: str, *, source: str = "<string>") -> ChipType:
    from .safe_yaml import load_yaml_str

    doc = require_mapping(load_yaml_str(text, source=source), "chip-type")
    return _chip_type_from_doc(doc, source=source)


def _chip_type_from_doc(doc: Any, *, source: str) -> ChipType:
    ct = require_mapping(require(doc, "chip_type", source), f"{source}.chip_type")
    fabric = require_mapping(require(doc, "fabric", source), f"{source}.fabric")

    timing_node = opt(doc, "timing", {})
    timing = Timing(
        alu_operation_ns=float(opt(timing_node, "alu_operation_ns", 1.0)),
        memory_read_ns=float(opt(timing_node, "memory_read_ns", 1.0)),
        memory_write_ns=float(opt(timing_node, "memory_write_ns", 1.0)),
        instruction_decode_ns=float(opt(timing_node, "instruction_decode_ns", 1.0)),
        handshake_ns=float(opt(timing_node, "handshake_ns", 1.0)),
        hop_delay_ns=float(opt(timing_node, "hop_delay_ns", 1.0)),
    )

    ports = tuple(
        _port_from_node(p, source=f"{source}.ports[{i}]")
        for i, p in enumerate(opt_seq(doc, "ports", source))
    )

    return ChipType(
        name=str(require(ct, "name", f"{source}.chip_type")),
        width=int(require(fabric, "width", f"{source}.fabric")),
        height=int(require(fabric, "height", f"{source}.fabric")),
        memory_words=int(opt(fabric, "memory_words", 32)),
        description=str(opt(ct, "description", "")),
        version=str(opt(ct, "version", "1.0")),
        timing=timing,
        ports=ports,
    )


def _port_from_node(node: Any, *, source: str) -> PortSpec:
    node = require_mapping(node, source)
    cell = require_mapping(require(node, "cell", source), f"{source}.cell")
    direction_raw = str(require(node, "direction", source))
    try:
        direction = PortDirection(direction_raw)
    except ValueError as exc:
        raise SchemaError(
            f"{source}.direction: invalid value {direction_raw!r} "
            "(expected 'input' or 'output')."
        ) from exc
    return PortSpec(
        name=str(require(node, "name", source)),
        direction=direction,
        width=int(require(node, "width", source)),
        cell_x=int(require(cell, "x", f"{source}.cell")),
        cell_y=int(require(cell, "y", f"{source}.cell")),
        face=Face.from_str(str(require(cell, "face", f"{source}.cell"))),
        protocol=str(opt(node, "protocol", "bundled_async_2phase")),
    )
