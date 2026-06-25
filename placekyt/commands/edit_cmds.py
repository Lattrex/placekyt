"""Edit commands: face direction and block parameters (§4.2)."""

from __future__ import annotations

import copy

from model.enums import Face
from model.placement import CellId, InstrOverride
from model.project import Project

from .base import Command


class SetCellFaceCommand(Command):
    """Change a placed cell's output face. Undo restores the prior face."""

    def __init__(self, project: Project, block_name: str, cell_id: CellId,
                 face: Face):
        self.project = project
        self.block_name = block_name
        self.cell_id = cell_id
        self.face = face
        self._prev: Face | None = None

    def execute(self) -> None:
        block = self.project.block(self.block_name)
        if block is None or block.placement is None:
            raise KeyError(f"block {self.block_name!r} not placed")
        cell = block.placement.cell(self.cell_id)
        if cell is None:
            raise KeyError(f"block {self.block_name!r} has no cell {self.cell_id!r}")
        self._prev = cell.face
        self.project._set_cell_face(self.block_name, self.cell_id, self.face)

    def undo(self) -> None:
        if self._prev is not None:
            self.project._set_cell_face(self.block_name, self.cell_id, self._prev)

    def description(self) -> str:
        return f"Set {self.block_name}[{self.cell_id}] face → {self.face.value}"

    def to_trace(self) -> dict:
        # face recorded as its string value; the controller's set_cell_face
        # accepts either a Face or its string (replay-friendly).
        return {"op": "set_cell_face",
                "args": {"block_name": self.block_name, "cell_id": self.cell_id,
                         "face": self.face.value}}


class EditParamsCommand(Command):
    """Replace a block's parameter dict. Undo restores the prior params.

    (The generator-resynchronisation lifecycle in §4.2 layers on top of this
    once user-authored computed blocks land; for v1.0's static + Python blocks
    this is a straightforward value swap.)"""

    def __init__(self, project: Project, block_name: str, params: dict):
        self.project = project
        self.block_name = block_name
        self.params = dict(params)
        self._prev: dict | None = None

    def execute(self) -> None:
        block = self.project.block(self.block_name)
        if block is None:
            raise KeyError(f"no block named {self.block_name!r}")
        self._prev = copy.deepcopy(block.params)
        self.project._set_block_params(self.block_name, self.params)

    def undo(self) -> None:
        if self._prev is not None:
            self.project._set_block_params(self.block_name, self._prev)

    def description(self) -> str:
        return f"Edit params of {self.block_name}"

    def to_trace(self) -> dict:
        return {"op": "edit_params",
                "args": {"block_name": self.block_name, "params": self.params}}


class ResyncFromGrcCommand(Command):
    """Re-apply GRC-side params to the placed design, then re-layout (§GRC-sync).

    Used when placeKYT detects the connected GNURadio flowgraph's block params
    drifted from the placed design (see ``engine/grc_sync.py``). It is ONE
    undoable unit because a param change may RESIZE a block (FIR 7→40 taps), and
    a resize must re-place + re-route — which rewrites many blocks' placements
    and routes. Capturing the whole ``blocks`` + ``connections`` state lets undo
    restore the design exactly, regardless of how far the re-layout reached.

    ``apply`` is the param-application + re-layout work, run inside ``execute``
    AFTER the snapshot is taken. The controller supplies it: it applies the
    merged params to each affected block and then either re-places+re-routes
    (notify/auto) or resizes-in-place keeping the anchor (re-anchor). Returning a
    value (e.g. a route report) is fine — it is stashed on ``result`` for the
    caller to surface DRC.
    """

    def __init__(self, project: Project, block_names: list, apply, *,
                 description: str = "Resync from GRC"):
        self.project = project
        self.block_names = list(block_names)
        self._apply = apply
        self._description = description
        self._snapshot = None
        self.result = None

    def execute(self) -> None:
        # Full snapshot of the structural state a re-layout can touch (params,
        # placement, routes). Deep-copied so the live mutation can't alias it.
        self._snapshot = (
            copy.deepcopy(self.project.blocks),
            copy.deepcopy(self.project.connections),
        )
        self.result = self._apply()

    def undo(self) -> None:
        if self._snapshot is None:
            return
        blocks, conns = self._snapshot
        # Restore by replacing list CONTENTS (keep the list objects the model and
        # views hold references to), then announce the change.
        self.project.blocks[:] = copy.deepcopy(blocks)
        self.project.connections[:] = copy.deepcopy(conns)
        self.project.build_dirty = True
        self.project.event_bus.emit("project_resynced", source="grc-undo")

    def description(self) -> str:
        return self._description


class RenameBlockCommand(Command):
    """Rename a block instance. Undo restores the previous name.

    Rewrites the block's ``name`` and every connection endpoint that referenced
    it (see :meth:`Project._rename_block`). Validates that the new name is
    non-empty and unique."""

    def __init__(self, project: Project, old_name: str, new_name: str):
        self.project = project
        self.old_name = old_name
        self.new_name = (new_name or "").strip()

    def execute(self) -> None:
        if not self.new_name:
            raise ValueError("block name cannot be empty")
        if self.new_name == self.old_name:
            return
        if self.project.block(self.old_name) is None:
            raise KeyError(f"no block named {self.old_name!r}")
        if self.project.block(self.new_name) is not None:
            raise ValueError(f"a block named {self.new_name!r} already exists")
        self.project._rename_block(self.old_name, self.new_name)

    def undo(self) -> None:
        if self.new_name and self.new_name != self.old_name:
            self.project._rename_block(self.new_name, self.old_name)

    def description(self) -> str:
        return f"Rename {self.old_name} → {self.new_name}"

    def to_trace(self) -> dict:
        return {"op": "rename_block",
                "args": {"old_name": self.old_name, "new_name": self.new_name}}


class SetInstrOverrideCommand(Command):
    """Set/clear a per-instruction handoff override on a block cell (§3.3).

    The hop count and dest/entry of a WRITE/JUMP belong to the instruction, not
    the route, so the override is stored on the block's placement keyed by
    ``(cell_id, addr)``. Undoable: restores the prior override (or its absence).
    """

    def __init__(self, project: Project, block_name: str, cell_id: CellId,
                 addr: int, override: InstrOverride | None):
        self.project = project
        self.block_name = block_name
        self.cell_id = cell_id
        self.addr = addr
        self.override = override
        self._prev: InstrOverride | None = None
        self._had_prev = False

    def execute(self) -> None:
        block = self.project.block(self.block_name)
        if block is None or block.placement is None:
            raise KeyError(f"block {self.block_name!r} not placed")
        prev = block.placement.override(self.cell_id, self.addr)
        self._had_prev = prev is not None
        self._prev = copy.deepcopy(prev)
        self.project._set_instr_override(
            self.block_name, self.cell_id, self.addr, self.override)

    def undo(self) -> None:
        self.project._set_instr_override(
            self.block_name, self.cell_id, self.addr,
            self._prev if self._had_prev else None)

    def description(self) -> str:
        return f"Set instruction override {self.block_name}[{self.cell_id}]@{self.addr}"
