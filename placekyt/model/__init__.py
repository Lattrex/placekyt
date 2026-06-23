"""placeKYT data model — pure-Python project state, no Qt / simkyt deps.

This package is the single source of truth for project state (the architecture notes
§4.1, §6). The ``engine/``, ``commands/``, and ``ui/`` layers build on it; it
builds on nothing but the standard library.
"""

from __future__ import annotations

from .block import Block
from .board import (
    Board,
    BoardChip,
    BoardInterface,
    ChipConnection,
    FpgaConnection,
)
from .chip import ChipInstance
from .chip_type import ChipType, PortSpec, Timing
from .connection import (
    AUTO_ROUTE,
    BlockEndpoint,
    ChipPortEndpoint,
    Connection,
    Endpoint,
    InterChipConnection,
    PanelConnection,
    RoutePoint,
)
from .panel import PanelPort, SramPanel
from .enums import (
    KNOWN_CODE_RATES,
    Face,
    IQFormat,
    Modulation,
    PortDirection,
)
from .events import Event, EventBus
from .placement import Placement, PlacedCell, TransitCell
from .project import (
    BoardRef,
    FaceOverride,
    FpgaModelRef,
    Project,
    ProjectMetadata,
    SimulationConfig,
)

__all__ = [
    # enums
    "Face",
    "PortDirection",
    "Modulation",
    "IQFormat",
    "KNOWN_CODE_RATES",
    # events
    "EventBus",
    "Event",
    # placement
    "PlacedCell",
    "TransitCell",
    "Placement",
    # chip type
    "ChipType",
    "PortSpec",
    "Timing",
    # board
    "Board",
    "BoardChip",
    "BoardInterface",
    "ChipConnection",
    "FpgaConnection",
    # chip instance
    "ChipInstance",
    # block
    "Block",
    # connection
    "Connection",
    "BlockEndpoint",
    "ChipPortEndpoint",
    "Endpoint",
    "RoutePoint",
    "InterChipConnection",
    "PanelConnection",
    "AUTO_ROUTE",
    # panel
    "SramPanel",
    "PanelPort",
    # project
    "Project",
    "ProjectMetadata",
    "BoardRef",
    "SimulationConfig",
    "FpgaModelRef",
    "FaceOverride",
]
