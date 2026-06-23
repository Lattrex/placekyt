"""Dev board configuration (``.kdb``), the architecture notes §2.4.

A board describes the physical hardware: which chips are present, how they are
wired to each other, and how they connect to the FPGA. The project's
``inter_chip_connections`` must be a subset of the board's ``chip_connections``
(validated by the ``inter_chip_not_wired`` DRC check).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class BoardChip:
    """A chip socket on the board: an id, a chip type, and a silkscreen label."""

    id: int
    type: str
    label: str = ""


@dataclass(frozen=True)
class ChipConnection:
    """A physical chip-to-chip wire on the board.

    ``wire_delay_ns`` must be >= 1.0 (a board config error otherwise — §2.4:
    zero-delay inter-chip wires break causal ordering in multi-chip simulation).
    Validation of that bound happens in the engine/IO layer at load time; the
    model just carries the value.
    """

    from_chip: int
    from_port: str
    to_chip: int
    to_port: str
    type: str = "direct_wire"
    wire_delay_ns: float = 1.0


@dataclass(frozen=True)
class FpgaConnection:
    """An FPGA-to-chip link (ADC in, DAC out, or bidirectional SRAM).

    ``chip_port_out`` is present only for bidirectional connections (e.g. SRAM
    read/write); unidirectional links (ADC input, DAC output) leave it ``None``.
    """

    name: str
    fpga_port: str
    chip: int
    chip_port: str
    chip_port_out: str | None = None


@dataclass(frozen=True)
class BoardInterface:
    """Programming interface descriptor (FTDI serial)."""

    type: str = "ftdi_serial"
    vid: int = 0x0403
    pid: int = 0x6014
    baud: int = 3_000_000
    protocol: str = "lattrex_v1"


@dataclass(frozen=True)
class Board:
    """A dev board: chips, inter-chip wiring, and FPGA interface."""

    name: str
    manufacturer: str = "Lattrex"
    version: str = "1.0"
    description: str = ""
    interface: BoardInterface = field(default_factory=BoardInterface)
    bitstream_slots: int = 4
    fpga_sram_kb: int = 512
    chips: tuple[BoardChip, ...] = ()
    chip_connections: tuple[ChipConnection, ...] = ()
    fpga_connections: tuple[FpgaConnection, ...] = ()

    def has_chip_connection(
        self, from_chip: int, from_port: str, to_chip: int, to_port: str
    ) -> bool:
        """True if the board physically wires this exact chip-to-chip link.

        Used by the ``inter_chip_not_wired`` DRC check: a project inter-chip
        connection is only legal if the board declares the matching wire.
        """
        return any(
            c.from_chip == from_chip
            and c.from_port == from_port
            and c.to_chip == to_chip
            and c.to_port == to_port
            for c in self.chip_connections
        )
