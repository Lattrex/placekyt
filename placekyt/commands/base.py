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

    def to_trace(self) -> dict | None:
        """A structured, REPLAYABLE record of this operation for the command
        trace (§ console trace / bug-report capture).

        Returns ``{"op": <controller-method>, "args": {...}}`` where ``op`` names
        the public ``AppController`` method that performs this operation and
        ``args`` are its keyword arguments — so a trace can be replayed by calling
        ``getattr(controller, op)(**args)``. Commands override this; the default
        returns ``None`` (the op is recorded as a human-readable description only,
        not auto-replayable — e.g. an internal composite the controller exposes
        under its own higher-level method)."""
        return None


class CompositeCommand(Command):
    """Groups commands into one atomic undo/redo unit (§4.2).

    On partial execute failure, the already-executed children are rolled back
    (undone in reverse) and the exception re-raised so the manager discards the
    composite.
    """

    def __init__(self, description: str, commands: list[Command],
                 trace_op: dict | None = None):
        self._commands = list(commands)
        self._description = description
        # Optional {op, args}: record this composite in the command trace as ONE
        # high-level controller call (e.g. auto_place / auto_route_all) rather
        # than as its decomposed child commands — so the trace replays at the
        # granularity the user acted.
        self._trace_op = trace_op

    def to_trace(self) -> dict | None:
        return self._trace_op

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


class CommandTrace:
    """An append-only log of every command the user performs, for live display
    in the console AND export to a replayable script / .kytrace.

    EVERY GUI interaction is a Command, so recording at the CommandManager is a
    complete, faithful capture of the session: place / move / rotate / face /
    param / connect / route / rename / delete — and undo/redo. Each entry is a
    dict ``{seq, kind, description, op, args}`` where ``kind`` is
    "do"/"undo"/"redo", ``description`` is the human one-liner, and ``op``/``args``
    (when present) are the replayable ``AppController`` call. A trace can be
    exported and replayed on another machine to reproduce a bug EXACTLY.
    """

    def __init__(self) -> None:
        self._events: list[dict] = []
        self._seq = 0
        self._listeners: list = []  # callbacks(event_dict) for live console echo

    def add_listener(self, fn) -> None:
        """Register a callback fired for each recorded event (the console echo)."""
        self._listeners.append(fn)

    def record(self, cmd: "Command", kind: str = "do") -> dict:
        ev = {
            "seq": self._seq,
            "kind": kind,
            "description": cmd.description(),
        }
        try:
            tr = cmd.to_trace()
        except Exception:  # noqa: BLE001 — a bad to_trace must never break an edit
            tr = None
        if tr:
            ev["op"] = tr.get("op")
            ev["args"] = tr.get("args", {})
        elif kind == "do":
            # A 'do' command with NO replayable op is a GAP: the trace cannot be
            # replayed past it. Flag it loudly so a trace is never SILENTLY
            # incomplete (a trace that omits an op is worthless — it can't
            # reproduce the session). Export + --replay surface this.
            ev["replayable"] = False
        self._events.append(ev)
        self._seq += 1
        for fn in list(self._listeners):
            try:
                fn(ev)
            except Exception:  # noqa: BLE001 — a listener must not break the edit
                pass
        return ev

    def gaps(self) -> list[dict]:
        """Recorded 'do' events that are NOT replayable (no op) — the holes that
        make a trace unable to reproduce a session. Empty ⇒ the trace is
        complete and fully replayable."""
        return [e for e in self._events
                if e.get("kind") == "do" and e.get("replayable") is False]

    def record_op(self, op: str, args: dict, description: str) -> dict:
        """Record a HIGH-LEVEL controller operation directly (not via a Command)
        — for replayable controller methods that aren't a single Command, e.g.
        ``import_grc`` (replaces the project), ``auto_place`` / ``auto_route_all``
        (composites whose child moves shouldn't each appear in the trace). Keeps
        the trace at the granularity the USER acted, and replayable as
        ``controller.<op>(**args)``."""
        ev = {"seq": self._seq, "kind": "do",
              "description": description, "op": op, "args": dict(args)}
        self._events.append(ev)
        self._seq += 1
        for fn in list(self._listeners):
            try:
                fn(ev)
            except Exception:  # noqa: BLE001
                pass
        return ev

    def events(self) -> list[dict]:
        return list(self._events)

    def clear(self) -> None:
        self._events.clear()
        self._seq = 0


class CommandManager:
    """Owns undo/redo stacks and drives the model event bus (§4.2, §6).

    Contract:
      * ``execute`` runs the command, pushes it to the undo stack, clears the
        redo stack, marks the project dirty, then flushes the event bus.
      * ``undo`` pops the undo stack, undoes it, pushes to redo, flushes.
      * ``redo`` pops the redo stack, re-executes, pushes to undo, flushes.
      * the undo stack is capped at ``MAX_HISTORY`` (oldest dropped, releasing
        references for GC).
      * every executed/undone/redone command is recorded in ``trace`` (a shared
        :class:`CommandTrace`) for the live console echo + replayable export.
    """

    MAX_HISTORY = 200

    def __init__(self, project: Project, trace: "CommandTrace | None" = None):
        self.project = project
        self._undo: list[Command] = []
        self._redo: list[Command] = []
        # The command trace SURVIVES project swaps (set_project rebuilds the
        # CommandManager but passes the SAME trace), so a full session — import,
        # edits, re-imports — is captured as one continuous log.
        self.trace = trace if trace is not None else CommandTrace()

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
        self.trace.record(cmd, "do")
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
        self.trace.record(cmd, "do")
        self.project.mark_dirty()
        self.project.event_bus.flush()

    def undo(self) -> None:
        if not self._undo:
            return
        cmd = self._undo.pop()
        cmd.undo()
        self._redo.append(cmd)
        self.trace.record(cmd, "undo")
        self.project.mark_dirty()
        self.project.event_bus.flush()

    def redo(self) -> None:
        if not self.can_redo():
            return
        cmd = self._redo.pop()
        cmd.execute()
        self._undo.append(cmd)
        self.trace.record(cmd, "redo")
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
