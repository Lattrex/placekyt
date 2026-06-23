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
