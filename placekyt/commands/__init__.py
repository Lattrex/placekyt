"""Command pattern + undo/redo for placeKYT (the architecture notes §4.2).

Every state-mutating operation is a :class:`Command` executed through the
:class:`CommandManager`, which owns the undo/redo stacks, flushes the model
event bus after each operation (§6 flush contract), and sets the project dirty
flags. ``commands/`` depends only on ``model/`` (§6).
"""

from .base import Command, CommandManager, CompositeCommand
from .connection_cmds import (
    AddConnectionCommand,
    AddInterChipCommand,
    AddPanelConnectionCommand,
    DeleteRouteCommand,
    RemoveConnectionCommand,
    RemoveInterChipCommand,
    RemovePanelConnectionCommand,
    SetConnectionRouteCommand,
)
from .edit_cmds import (
    EditParamsCommand,
    RenameBlockCommand,
    SetCellFaceCommand,
    SetInstrOverrideCommand,
)
from .placement_cmds import (
    MoveBlockCommand,
    MoveBlockToChipCommand,
    OrientBlockCommand,
    PlaceBlockCommand,
    PlaceCellCommand,
    PlaceTransitCommand,
    RemoveBlockCommand,
    TransformBlockCommand,
)
from .project_cmds import (
    AddChipCommand,
    AddPanelCommand,
    MirrorPanelCommand,
    MovePanelCommand,
    RemovePanelCommand,
)

__all__ = [
    "Command",
    "CommandManager",
    "CompositeCommand",
    "PlaceCellCommand",
    "PlaceBlockCommand",
    "PlaceTransitCommand",
    "MoveBlockCommand",
    "MoveBlockToChipCommand",
    "RemoveBlockCommand",
    "TransformBlockCommand",
    "OrientBlockCommand",
    "SetCellFaceCommand",
    "EditParamsCommand",
    "RenameBlockCommand",
    "SetInstrOverrideCommand",
    "AddConnectionCommand",
    "RemoveConnectionCommand",
    "DeleteRouteCommand",
    "SetConnectionRouteCommand",
    "AddInterChipCommand",
    "RemoveInterChipCommand",
    "AddChipCommand",
    "AddPanelCommand",
    "RemovePanelCommand",
    "MovePanelCommand",
    "MirrorPanelCommand",
    "AddPanelConnectionCommand",
    "RemovePanelConnectionCommand",
]
