"""Project-structure commands: add/remove chips + SRAM panels (§4.2)."""

from __future__ import annotations

import copy

from model.chip import ChipInstance
from model.panel import SramPanel
from model.project import Project

from .base import Command


class AddChipCommand(Command):
    """Add a chip instance to the project. Undo removes it."""

    def __init__(self, project: Project, chip: ChipInstance):
        self.project = project
        self.chip = chip

    def execute(self) -> None:
        self.project._add_chip(self.chip)

    def undo(self) -> None:
        self.project._remove_chip(self.chip.id)

    def description(self) -> str:
        return f"Add chip {self.chip.id}"


class AddPanelCommand(Command):
    """Add an SRAM/peripheral panel to the project. Undo removes it."""

    def __init__(self, project: Project, panel: SramPanel):
        self.project = project
        self.panel = panel

    def execute(self) -> None:
        self.project._add_panel(self.panel)

    def undo(self) -> None:
        self.project._remove_panel(self.panel.id)

    def description(self) -> str:
        return f"Add panel {self.panel.id}"


class RemovePanelCommand(Command):
    """Remove a panel and its panel↔chip links. Undo restores both."""

    def __init__(self, project: Project, panel_id: int):
        self.project = project
        self.panel_id = panel_id
        self._panel: SramPanel | None = None
        self._links: list = []

    def execute(self) -> None:
        self._panel = copy.deepcopy(self.project.panel(self.panel_id))
        self._links = [copy.deepcopy(c)
                       for c in self.project.panel_connections_for(self.panel_id)]
        self.project._remove_panel(self.panel_id)

    def undo(self) -> None:
        if self._panel is not None:
            self.project._add_panel(copy.deepcopy(self._panel))
        for link in self._links:
            self.project._add_panel_connection(copy.deepcopy(link))

    def description(self) -> str:
        return f"Remove panel {self.panel_id}"


class MovePanelCommand(Command):
    """Move a panel to a new scene position. Undo restores the old one."""

    def __init__(self, project: Project, panel_id: int, x: float, y: float):
        self.project = project
        self.panel_id = panel_id
        self.x, self.y = x, y
        self._prev: tuple[float, float] | None = None

    def execute(self) -> None:
        panel = self.project.panel(self.panel_id)
        if panel is None:
            raise KeyError(f"no panel {self.panel_id!r}")
        self._prev = (panel.position_x, panel.position_y)
        panel.position_x, panel.position_y = self.x, self.y
        self.project.event_bus.emit("panel_moved", panel_id=self.panel_id)

    def undo(self) -> None:
        panel = self.project.panel(self.panel_id)
        if panel is not None and self._prev is not None:
            panel.position_x, panel.position_y = self._prev
            self.project.event_bus.emit("panel_moved", panel_id=self.panel_id)

    def description(self) -> str:
        return f"Move panel {self.panel_id}"


class MirrorPanelCommand(Command):
    """Horizontally mirror a panel (ports swap WEST↔EAST edges). Self-inverse,
    so undo just mirrors again."""

    def __init__(self, project: Project, panel_id: int):
        self.project = project
        self.panel_id = panel_id

    def _flip(self) -> None:
        panel = self.project.panel(self.panel_id)
        if panel is None:
            raise KeyError(f"no panel {self.panel_id!r}")
        panel.mirror_h()
        self.project.event_bus.emit("panel_moved", panel_id=self.panel_id)

    def execute(self) -> None:
        self._flip()

    def undo(self) -> None:
        self._flip()

    def description(self) -> str:
        return f"Mirror panel {self.panel_id}"
