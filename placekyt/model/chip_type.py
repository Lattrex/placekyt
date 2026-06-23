"""Chip type definition: fabric geometry, timing, and I/O ports.

Mirrors the chip-type ``.yaml`` (the architecture notes §2.3), e.g.
``kyttar_10x12.yaml`` / ``KYT16A120.yaml``. A chip type is a *registry entry*
shared by all chip instances of that type in a project; instances reference it
by name.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .enums import Face, PortDirection


@dataclass(frozen=True)
class PortSpec:
    """A chip I/O port (the architecture notes §2.3 ``ports:``).

    ``face`` means "direction data arrives FROM" on input ports and "direction
    data exits TO" on output ports. ``cell`` is the edge cell the port attaches
    to.
    """

    name: str
    direction: PortDirection
    width: int
    cell_x: int
    cell_y: int
    face: Face
    protocol: str = "bundled_async_2phase"


@dataclass(frozen=True)
class Timing:
    """Per-stage matched-delay timings (ns) from the chip type's ``timing:``."""

    alu_operation_ns: float = 1.0
    memory_read_ns: float = 1.0
    memory_write_ns: float = 1.0
    instruction_decode_ns: float = 1.0
    handshake_ns: float = 1.0
    hop_delay_ns: float = 1.0


@dataclass(frozen=True)
class ChipType:
    """A Kyttar chip type: fabric size, memory depth, timing, and ports.

    Frozen because a chip type is a fixed hardware description, not editable
    project state.
    """

    name: str
    width: int
    height: int
    memory_words: int = 32
    description: str = ""
    version: str = "1.0"
    timing: Timing = field(default_factory=Timing)
    ports: tuple[PortSpec, ...] = ()

    @property
    def cell_count(self) -> int:
        return self.width * self.height

    def cell_id(self, x: int, y: int) -> int:
        """Flat cell id for ``(x, y)`` per the simkyt convention.

        ``cell_id = y * width + x`` (matches ``read_cell_memory`` addressing in
        the simkyt API, the architecture notes §4.3).
        """
        return y * self.width + x

    def in_bounds(self, x: int, y: int) -> bool:
        return 0 <= x < self.width and 0 <= y < self.height

    def port(self, name: str) -> PortSpec | None:
        for p in self.ports:
            if p.name == name:
                return p
        return None
