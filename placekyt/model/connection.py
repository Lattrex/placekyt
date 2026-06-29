"""Logical and physical connections between blocks and chip ports.

Mirrors the ``connections:`` and ``inter_chip_connections:`` sections of the
``.kyt`` schema (the architecture notes §2.1).

A :class:`Connection` endpoint is a tagged union: either a **block port**
(``{block: agc, port: out}``) or a **chip port** (``{chip_port: {chip: 0,
port: x16_in}}``). Either kind is valid on both the ``from:`` and ``to:`` side
— a ``from: chip_port`` is external data entering a chip (ADC → x16_in); a
``to: chip_port`` is data leaving a chip (equalizer → x16_out).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Union

from .enums import IQFormat, Modulation


@dataclass(frozen=True)
class BlockEndpoint:
    """An endpoint on a named block's port (``{block: <name>, port: <port>}``)."""

    block: str
    port: str


@dataclass(frozen=True)
class ChipPortEndpoint:
    """An endpoint on a chip's I/O port (``{chip_port: {chip: N, port: P}}``)."""

    chip: int
    port: str


# A connection endpoint is one of the two kinds above.
Endpoint = Union[BlockEndpoint, ChipPortEndpoint]


@dataclass(frozen=True)
class RoutePoint:
    """One ``(x, y)`` waypoint on a physical route path."""

    x: int
    y: int

    @property
    def pos(self) -> tuple[int, int]:
        return (self.x, self.y)


# Route coordinates are implicitly on the source block's chip (§2.1). A route
# is either an explicit waypoint list, the sentinel ``"auto"`` (Phase 2 auto-
# route; treated as a fly line in Phase 1), or ``None`` (unrouted fly line).
AUTO_ROUTE = "auto"


# An inter-block edge is realized on the fabric as a WRITE (data handoff), a JUMP
# (trigger), or BOTH (a triggered data handoff) — the auto-P&R design notes §4. This is
# the LOGICAL ``kind`` of a connection (a ``LogicalNet`` in the design doc); the
# auto-router uses it to decide what the source cell emits and how the channel
# (WRITE dest / JUMP entry) is assigned at the destination broker.
#   - ``data``         : WRITE only — a producer hands a value to a consumer's
#                        input register; the consumer is triggered some other way
#                        (e.g. its own clocked entry, or a separate trigger net).
#   - ``trigger``      : JUMP only — fire the destination's entry, no data moves.
#   - ``data+trigger`` : WRITE + JUMP sharing one corridor (the common case) — the
#                        value lands AND the destination executes. This is how
#                        every realized inter-block handoff works today, so it is
#                        the default (preserves existing build behavior).
NET_DATA = "data"
NET_TRIGGER = "trigger"
NET_DATA_TRIGGER = "data+trigger"
_NET_KINDS = (NET_DATA, NET_TRIGGER, NET_DATA_TRIGGER)


@dataclass
class Connection:
    """A logical connection, optionally with a physical route.

    ``route`` is one of:
      * ``None`` — unrouted (rendered as a dashed fly line),
      * the string :data:`AUTO_ROUTE` — request auto-routing (Phase 2),
      * a ``list[RoutePoint]`` — explicit path including source, transit, and
        destination cells (§4.1 ``project.route(..., path=...)``).

    The optional ``modulation`` / ``code_rate`` / ``iq_format`` metadata enables
    Constellation View, BER auto-derive, and throughput conversion (§2.1). They
    are typically set on connections targeting a chip output port.
    """

    name: str
    source: Endpoint
    target: Endpoint
    route: Union[None, str, list[RoutePoint]] = None
    modulation: Modulation | None = None
    code_rate: float | None = None
    iq_format: IQFormat | None = None
    # Output destination-address TAG (§ shared-port duplex). When a connection
    # targets a chip OUTPUT port, this sets the dest field of the source block's
    # exit WRITE so multiple chains sharing one output port stay distinguishable
    # on the wire (the captured OutWord.tag). Default None ⇒ dest 0 (untagged).
    out_tag: int | None = None
    # Per-stream identifier (§ shared-port duplex). When several input nets fan
    # off ONE chip input port (the full-duplex modem: x16_in → TX mapper AND
    # x16_in → RX matched filter), each carries a ``stream_id`` (e.g. "tx"/"rx",
    # set by the GRC importer from the source block's stream_id param). The live
    # bridge resolves this id to the net's block entry/hop/data-registers
    # (engine.port_config.stream_targets) so each GR source's burst injects at the
    # right block WITHOUT the source knowing any placement-dependent value.
    # Default None ⇒ single-stream net (uses input_port_config).
    stream_id: str | None = None
    # The LOGICAL kind of this edge — ``data`` (WRITE), ``trigger`` (JUMP), or
    # ``data+trigger`` (both; the auto-P&R design notes §4). Default ``data+trigger``
    # matches how every realized inter-block handoff works today (WRITE + JUMP),
    # so existing projects are unchanged. The auto-router reads this to decide
    # what the source emits and how the channel is assigned at the destination.
    kind: str = NET_DATA_TRIGGER

    def __post_init__(self) -> None:
        if self.kind not in _NET_KINDS:
            raise ValueError(
                f"Connection.kind must be one of {_NET_KINDS}, got {self.kind!r}")

    @property
    def is_routed(self) -> bool:
        """True only when an explicit, non-empty waypoint route is present."""
        return isinstance(self.route, list) and len(self.route) > 0

    @property
    def is_auto(self) -> bool:
        return self.route == AUTO_ROUTE

    @property
    def emits_write(self) -> bool:
        """True if the source cell emits a WRITE for this edge (data moves)."""
        return self.kind in (NET_DATA, NET_DATA_TRIGGER)

    @property
    def emits_jump(self) -> bool:
        """True if the source cell emits a JUMP for this edge (trigger fires)."""
        return self.kind in (NET_TRIGGER, NET_DATA_TRIGGER)


@dataclass(frozen=True)
class InterChipConnection:
    """A chip-to-chip link used by the project (§2.1 ``inter_chip_connections``).

    Must be a subset of the board's ``chip_connections`` (DRC
    ``inter_chip_not_wired``). Inter-chip connections have no coordinate route —
    the wire is defined by the board.
    """

    from_chip: int
    from_port: str
    to_chip: int
    to_port: str


@dataclass(frozen=True)
class PanelConnection:
    """A panel-to-chip link: one SRAM/peripheral panel port wired to a chip port
    (see ``the SRAM panel notes``). Like an inter-chip wire, it has no coordinate route.

    A panel may wire to multiple chips, so a panel can own several of these. The
    port-type widths (x16/x1) must match across the link (DRC
    ``panel_port_mismatch``).
    """

    panel: int          # SramPanel.id
    panel_port: str
    chip: int           # ChipInstance.id
    chip_port: str
