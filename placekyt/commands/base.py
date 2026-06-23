"""Command base classes and the CommandManager (the architecture notes §4.2, §6)."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from model.project import Project

logger = logging.getLogger("placekyt.commands")


class Command(ABC):
    """A reversible, state-mutating operation."""

    @abstractmethod
    def execute(self) -> None: ...

    @abstractmethod
    def undo(self) -> None: ...

    @abstractmethod
    def description(self) -> str: ...

    def is_redoable(self) -> bool:
        """Whether this command can be redone (§4.2 — EditParamsCommand may
        return False while a generator result is in-flight). Default True."""
        return True


class CompositeCommand(Command):
    """Groups commands into one atomic undo/redo unit (§4.2).

    On partial execute failure, the already-executed children are rolled back
    (undone in reverse) and the exception re-raised so the manager discards the
    composite.
    """

    def __init__(self, description: str, commands: list[Command]):
        self._commands = list(commands)
        self._description = description

    def execute(self) -> None:
        executed: list[Command] = []
        try:
            for cmd in self._commands:
                cmd.execute()
                executed.append(cmd)
        except Exception:
            for cmd in reversed(executed):
                cmd.undo()
            raise

    def undo(self) -> None:
        for cmd in reversed(self._commands):
            cmd.undo()

    def description(self) -> str:
        return self._description

    @property
    def commands(self) -> list[Command]:
        return list(self._commands)


class CommandManager:
    """Owns undo/redo stacks and drives the model event bus (§4.2, §6).

    Contract:
      * ``execute`` runs the command, pushes it to the undo stack, clears the
        redo stack, marks the project dirty, then flushes the event bus.
      * ``undo`` pops the undo stack, undoes it, pushes to redo, flushes.
      * ``redo`` pops the redo stack, re-executes, pushes to undo, flushes.
      * the undo stack is capped at ``MAX_HISTORY`` (oldest dropped, releasing
        references for GC).
    """

    MAX_HISTORY = 200

    def __init__(self, project: Project):
        self.project = project
        self._undo: list[Command] = []
        self._redo: list[Command] = []

    # -- queries --------------------------------------------------------------

    def can_undo(self) -> bool:
        return bool(self._undo)

    def can_redo(self) -> bool:
        return bool(self._redo) and self._redo[-1].is_redoable()

    def undo_text(self) -> str | None:
        return self._undo[-1].description() if self._undo else None

    def redo_text(self) -> str | None:
        return self._redo[-1].description() if self._redo else None

    @property
    def undo_depth(self) -> int:
        return len(self._undo)

    # -- operations -----------------------------------------------------------

    def execute(self, cmd: Command) -> None:
        """Execute ``cmd``; on success push to undo and clear redo. A failing
        command is NOT pushed and the event queue is flushed so any partial
        events (e.g. a composite that rolled back) are delivered consistently."""
        try:
            cmd.execute()
        except Exception:
            # The command (or composite) already rolled back its own effects.
            # Clear any half-emitted events so callbacks don't see them, then
            # surface a resync signal and re-raise.
            self.project.event_bus.clear()
            self.project.event_bus.emit("command_failed",
                                        description=cmd.description())
            self.project.event_bus.flush()
            raise
        self._undo.append(cmd)
        self._redo.clear()
        self._trim()
        self.project.mark_dirty()
        self.project.event_bus.flush()

    def add_executed(self, cmd: Command) -> None:
        """Register a command the CALLER has ALREADY executed (in pieces), pushing
        it to the undo stack WITHOUT re-running it. Used when later steps of a
        composite must observe earlier steps' effects as they go — e.g. auto-place
        orients then translates per block, reading post-orient positions. Undo/redo
        behave normally afterward (the composite undoes/redoes as a unit)."""
        self._undo.append(cmd)
        self._redo.clear()
        self._trim()
        self.project.mark_dirty()
        self.project.event_bus.flush()

    def undo(self) -> None:
        if not self._undo:
            return
        cmd = self._undo.pop()
        cmd.undo()
        self._redo.append(cmd)
        self.project.mark_dirty()
        self.project.event_bus.flush()

    def redo(self) -> None:
        if not self.can_redo():
            return
        cmd = self._redo.pop()
        cmd.execute()
        self._undo.append(cmd)
        self.project.mark_dirty()
        self.project.event_bus.flush()

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()

    def _trim(self) -> None:
        # Drop oldest commands beyond MAX_HISTORY (releases refs for GC).
        excess = len(self._undo) - self.MAX_HISTORY
        if excess > 0:
            del self._undo[:excess]
