"""Breakpoints — Qt-free condition model (DEBUG_the architecture notes §3.6).

The engine has no native breakpoints; they are implemented by checking trace
events after each step batch. This module is the pure-data layer: a
:class:`Breakpoint` (what to watch) and a :class:`BreakpointSet` (the active
set + the "did any fire in these new events?" check). The SimController owns a
BreakpointSet and pauses the run when :meth:`BreakpointSet.first_hit` returns a
hit; the UI lists/sets/removes them.

Two core types (register-value is a later pass):
  * **PC** — fires when cell ``(chip, x, y)`` executes ``pc == value`` (an
    ``exec_tick`` whose ``pc`` matches).
  * **FACE** — fires when data/instruction arrives at cell ``(chip, x, y)`` on
    face ``value`` (a ``data_arrival``/``instr_arrival`` whose ``face`` matches).
"""

from __future__ import annotations

from dataclasses import dataclass, field

BP_PC = "pc"
BP_FACE = "face"
BP_TYPES = (BP_PC, BP_FACE)

# Trace event kinds each breakpoint type inspects.
_EXEC_KIND = "exec_tick"
_ARRIVAL_KINDS = ("data_arrival", "instr_arrival")


@dataclass
class Breakpoint:
    """One breakpoint condition on a cell.

    ``kind`` is :data:`BP_PC` or :data:`BP_FACE`. ``value`` is the PC number
    (PC) or the face string ``"N"/"S"/"E"/"W"`` (FACE). ``enabled`` gates it
    without removing it."""

    chip: int
    x: int
    y: int
    kind: str
    value: object          # int PC, or "N"/"S"/"E"/"W" face
    enabled: bool = True

    @property
    def cell(self) -> tuple[int, int, int]:
        return (self.chip, self.x, self.y)

    def label(self) -> str:
        where = f"c{self.chip}:({self.x},{self.y})"
        if self.kind == BP_PC:
            return f"{where} PC=={self.value}"
        return f"{where} arrival@{self.value}"

    def matches(self, ev: dict, width: int) -> bool:
        """Does trace event ``ev`` (with its chip already known to be this
        breakpoint's chip) trigger this breakpoint? ``width`` maps cell_id →
        (x, y)."""
        if not self.enabled:
            return False
        cid = ev.get("cell_id")
        if cid is None or (cid % width, cid // width) != (self.x, self.y):
            return False
        kind = ev.get("kind")
        if self.kind == BP_PC:
            return kind == _EXEC_KIND and ev.get("pc") == self.value
        if self.kind == BP_FACE:
            return kind in _ARRIVAL_KINDS and ev.get("face") == self.value
        return False


@dataclass
class BreakpointHit:
    """A fired breakpoint + the trace event/time that fired it."""

    bp: Breakpoint
    time_ns: float
    event: dict


@dataclass
class BreakpointSet:
    """The active breakpoints + the per-chip event scan.

    ``first_hit`` is the run-loop check: given the new trace events for a chip
    (those produced by the just-run batch), return the FIRST event that fires an
    enabled breakpoint, or None. The caller advances its scan cursor past the
    checked events so each event is checked once."""

    breakpoints: list[Breakpoint] = field(default_factory=list)

    def add(self, bp: Breakpoint) -> Breakpoint:
        # De-dupe an identical condition (same cell/kind/value) — just re-enable.
        for existing in self.breakpoints:
            if (existing.cell == bp.cell and existing.kind == bp.kind
                    and existing.value == bp.value):
                existing.enabled = True
                return existing
        self.breakpoints.append(bp)
        return bp

    def remove(self, bp: Breakpoint) -> None:
        if bp in self.breakpoints:
            self.breakpoints.remove(bp)

    def toggle(self, bp: Breakpoint, enabled: bool | None = None) -> None:
        bp.enabled = (not bp.enabled) if enabled is None else enabled

    def clear(self) -> None:
        self.breakpoints.clear()

    def find(self, chip: int, x: int, y: int, kind: str,
             value: object) -> Breakpoint | None:
        for bp in self.breakpoints:
            if (bp.cell == (chip, x, y) and bp.kind == kind
                    and bp.value == value):
                return bp
        return None

    def has_any(self, chip: int, x: int, y: int) -> bool:
        """Any (enabled or not) breakpoint on this cell — for the canvas mark."""
        return any(bp.cell == (chip, x, y) for bp in self.breakpoints)

    def first_hit(self, chip: int, events: list, width: int) -> BreakpointHit | None:
        """First breakpoint fired by ``events`` (a chip's new trace events), or
        None. ``events`` are this chip's events; each is checked against the
        breakpoints scoped to ``chip``."""
        chip_bps = [bp for bp in self.breakpoints
                    if bp.enabled and bp.chip == chip]
        if not chip_bps:
            return None
        for ev in events:
            for bp in chip_bps:
                if bp.matches(ev, width):
                    t = ev.get("time_ns", ev.get("time", 0.0))
                    return BreakpointHit(bp=bp, time_ns=float(t or 0.0), event=ev)
        return None
