"""SRAM / peripheral panels — off-array memory devices on the canvas.

See ``the SRAM panel notes`` for the full architecture. A panel attaches to the cell
fabric like another chip: it has labelled **x16 / x1** input/output ports and is
wired to chip ports (possibly on multiple chips). It holds no program; cells
interact with it purely through the existing ISA WRITE/JUMP fields, which select
a panel register (the register map lives in the sim/engine layer, not here).

This module models the *project instance* of a panel and its ports — the
geometry + configuration the canvas places and the IO layer round-trips. The
runtime behaviour (register protocol, push-read) is in the engine/sim layer.

``position`` is a canvas-relative scene coordinate (scene px at zoom=1), exactly
like :class:`model.chip.ChipInstance`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .enums import Face, PortDirection

# Port bus widths (bits). x16 = one 16-bit word per handshake; x1 = serial.
PORT_WIDTH_X16 = 16
PORT_WIDTH_X1 = 1

# A full 16-bit-addressable array is the default panel size (65 536 words).
DEFAULT_PANEL_WORDS = 1 << 16


@dataclass
class PanelPort:
    """One I/O port on a panel.

    ``direction`` is INPUT (cells → panel: WRITE/JUMP traffic) or OUTPUT (panel →
    cells: push-read WRITE+DATA+JUMP traffic). ``width`` is the bus width in bits
    (16 for x16, 1 for x1) and must match the chip port it connects to (DRC).
    ``face`` is the edge the port sits on, for canvas layout — purely visual.
    """

    name: str
    direction: PortDirection
    width: int = PORT_WIDTH_X16
    face: Face = Face.WEST

    @property
    def is_x16(self) -> bool:
        return self.width == PORT_WIDTH_X16


def _default_ports() -> list[PanelPort]:
    """A panel's default ports: x16 and x1 INPUTs (cells write/trigger the
    panel) on the WEST edge, x16 and x1 OUTPUTs (panel pushes read data back
    into the fabric) on the EAST edge. Inputs and outputs are kept on opposite
    edges so a horizontal mirror swaps them cleanly (chip outputs on the right →
    panel inputs)."""
    return [
        PanelPort("x16_in", PortDirection.INPUT, PORT_WIDTH_X16, Face.WEST),
        PanelPort("x1_in", PortDirection.INPUT, PORT_WIDTH_X1, Face.WEST),
        PanelPort("x16_out", PortDirection.OUTPUT, PORT_WIDTH_X16, Face.EAST),
        PanelPort("x1_out", PortDirection.OUTPUT, PORT_WIDTH_X1, Face.EAST),
    ]


@dataclass
class SramPanel:
    """A placed SRAM panel.

    ``size_words`` is the only configuration: how many 16-bit words the array
    stores (default: a full 16-bit address space, 65 536 words). ``ports`` are
    the panel's I/O ports (default: x16+x1 inputs on the WEST edge, x16+x1
    outputs on the EAST edge). The panel is addressed by cells via the register
    map in ``the SRAM panel notes``; that protocol is implemented in the engine/sim
    layer, not modelled here. ``mirrored`` tracks a horizontal flip (so its
    ports sit on the opposite edges) for tidy wiring against right-edge chip
    ports.
    """

    id: int
    label: str = ""
    position_x: float = 0.0
    position_y: float = 0.0
    size_words: int = DEFAULT_PANEL_WORDS
    ports: list[PanelPort] = field(default_factory=_default_ports)
    mirrored: bool = False     # horizontal flip (ports swap WEST↔EAST edges)

    @property
    def position(self) -> tuple[float, float]:
        return (self.position_x, self.position_y)

    def mirror_h(self) -> None:
        """Flip the panel horizontally: every WEST port moves to EAST and vice
        versa, so inputs/outputs swap sides. ``mirrored`` toggles. NORTH/SOUTH
        ports (if any) are unaffected — same convention as a block H-mirror."""
        for p in self.ports:
            p.face = p.face.mirrored_h
        self.mirrored = not self.mirrored

    @property
    def address_bits(self) -> int:
        """Bits needed to address every word — drives how many address registers
        (R5, R6, …) the panel needs (16 bits each)."""
        return max(1, (self.size_words - 1).bit_length())

    @property
    def address_regs(self) -> int:
        """Number of 16-bit address registers required (R5 onward)."""
        return max(1, (self.address_bits + 15) // 16)

    def port(self, name: str) -> PanelPort | None:
        for p in self.ports:
            if p.name == name:
                return p
        return None
