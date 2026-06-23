"""Connection commands: add/remove logical + inter-chip connections (§4.2)."""

from __future__ import annotations

import copy

from model.connection import Connection, InterChipConnection, PanelConnection
from model.project import Project

from .base import Command


class AddConnectionCommand(Command):
    """Add a connection. Undo removes it."""

    def __init__(self, project: Project, connection: Connection):
        self.project = project
        self.connection = connection

    def execute(self) -> None:
        self.project._add_connection(self.connection)
        # Surface an event so the canvas fully re-renders. A re-render draws a FLY
        # LINE only for UNROUTED connections, so adding a ROUTED connection (e.g.
        # re-connecting a physical route for a logical net) makes any stale fly
        # line for that net disappear (#271) — a fly line exists ONLY while a
        # connection has no physical route.
        self.project.event_bus.emit("connection_route_changed",
                                    name=self.connection.name)

    def undo(self) -> None:
        self.project._remove_connection(self.connection.name)
        self.project.event_bus.emit("connection_route_changed",
                                    name=self.connection.name)

    def description(self) -> str:
        return f"Add connection {self.connection.name}"


class SetConnectionRouteCommand(Command):
    """Set (or clear) a connection's waypoint route. Undo restores the prior
    route. Used by auto-route (Phase 3) to materialise a logical net into a drawn
    path, reversibly."""

    def __init__(self, project: Project, name: str, points):
        self.project = project
        self.name = name
        # ``points`` is a list of (x, y) or None to clear back to a logical net.
        self.points = points
        self._prev = None

    def execute(self) -> None:
        from model.connection import RoutePoint

        conn = self.project.connection(self.name)
        if conn is None:
            raise KeyError(f"no connection named {self.name!r}")
        self._prev = conn.route
        conn.route = ([RoutePoint(x, y) for (x, y) in self.points]
                      if self.points else None)
        # Re-render: a now-ROUTED connection drops its fly line; a cleared route
        # (points=None) gains one (#271 — fly line iff unrouted).
        self.project.event_bus.emit("connection_route_changed", name=self.name)

    def undo(self) -> None:
        conn = self.project.connection(self.name)
        if conn is not None:
            conn.route = self._prev
            self.project.event_bus.emit("connection_route_changed",
                                        name=self.name)

    def description(self) -> str:
        return f"Route connection {self.name}"


class RemoveConnectionCommand(Command):
    """Remove a connection by name. Undo restores it."""

    def __init__(self, project: Project, name: str):
        self.project = project
        self.name = name
        self._removed: Connection | None = None

    def execute(self) -> None:
        conn = self.project.connection(self.name)
        if conn is None:
            raise KeyError(f"no connection named {self.name!r}")
        self._removed = copy.deepcopy(conn)
        self.project._remove_connection(self.name)

    def undo(self) -> None:
        if self._removed is not None:
            self.project._add_connection(copy.deepcopy(self._removed))

    def description(self) -> str:
        return f"Remove connection {self.name}"


class DeleteRouteCommand(Command):
    """Smart-delete a block-to-block route (#267).

    Deletes the connection's PHYSICAL route while respecting the time-multiplexed
    bus (the auto-P&R design notes §1.2):

      * **Sole occupant** — no OTHER routed connection covers any of this route's
        transit cells: the route is removed and those routing cells disappear with
        it (they were rendered from the route; nothing else references them). The
        connection is reduced to an UNROUTED logical net (a fly line), so the
        block-to-block link is preserved and can be re-routed.
      * **Shared / multiplexed bus** — at least one transit cell is also covered by
        another connection's route: those cells STAY (the co-tenant connection
        still routes through them); only THIS connection's route is broken and it
        becomes a fly line.

    In both branches the connection itself is kept (reduced to a fly line), which
    is the consistent "the route is gone, the logical wire remains" behaviour the
    user asked for. Undo restores the prior route exactly.

    ``shared`` (set after :meth:`execute`) reports which branch was taken;
    ``removed_cells`` lists the transit cells that disappear (sole-occupant
    branch) so callers/tests can verify the physical effect.
    """

    def __init__(self, project: Project, name: str):
        self.project = project
        self.name = name
        self._prev = None
        self.shared: bool | None = None
        self.removed_cells: list[tuple[int, int]] = []

    def execute(self) -> None:
        from engine.route_analysis import exclusive_route_cells, is_bus_shared

        conn = self.project.connection(self.name)
        if conn is None:
            raise KeyError(f"no connection named {self.name!r}")
        self._prev = conn.route
        # Compute the physical effect BEFORE clearing the route.
        self.shared = is_bus_shared(self.project, conn)
        # Cells that vanish: only this route's EXCLUSIVE transit cells (a shared
        # bus keeps every cell another connection still covers).
        self.removed_cells = (
            [] if self.shared else exclusive_route_cells(self.project, conn))
        # Break the physical route → an unrouted logical net (a fly line). Shared
        # bus cells remain rendered because the co-tenant connections still cover
        # them; sole-occupant transit cells disappear with this route.
        conn.route = None
        self.project.mark_dirty()
        # Surface a bus event so the views (canvas) re-render — the manager flush
        # delivers nothing if no event was emitted (a route is a field mutation,
        # not an add/remove).
        self.project.event_bus.emit("connection_route_changed", name=self.name)

    def undo(self) -> None:
        conn = self.project.connection(self.name)
        if conn is not None:
            conn.route = self._prev
            self.project.mark_dirty()
            self.project.event_bus.emit("connection_route_changed",
                                        name=self.name)

    def description(self) -> str:
        return f"Delete route {self.name}"


class AddInterChipCommand(Command):
    """Add a chip-to-chip connection (board-level wire). Undo removes it."""

    def __init__(self, project: Project, ic: InterChipConnection):
        self.project = project
        self.ic = ic

    def execute(self) -> None:
        self.project._add_inter_chip(self.ic)

    def undo(self) -> None:
        self.project._remove_inter_chip(self.ic)

    def description(self) -> str:
        return (f"Add inter-chip {self.ic.from_chip}.{self.ic.from_port} → "
                f"{self.ic.to_chip}.{self.ic.to_port}")


class RemoveInterChipCommand(Command):
    """Remove a chip-to-chip connection. Undo restores it."""

    def __init__(self, project: Project, ic: InterChipConnection):
        self.project = project
        self.ic = ic

    def execute(self) -> None:
        self.project._remove_inter_chip(self.ic)

    def undo(self) -> None:
        self.project._add_inter_chip(self.ic)

    def description(self) -> str:
        return (f"Remove inter-chip {self.ic.from_chip}.{self.ic.from_port} → "
                f"{self.ic.to_chip}.{self.ic.to_port}")


class AddPanelConnectionCommand(Command):
    """Add a panel↔chip link (the SRAM panel notes). Undo removes it."""

    def __init__(self, project: Project, pc: PanelConnection):
        self.project = project
        self.pc = pc

    def execute(self) -> None:
        self.project._add_panel_connection(self.pc)

    def undo(self) -> None:
        self.project._remove_panel_connection(self.pc)

    def description(self) -> str:
        return (f"Connect panel {self.pc.panel}.{self.pc.panel_port} → "
                f"chip {self.pc.chip}.{self.pc.chip_port}")


class RemovePanelConnectionCommand(Command):
    """Remove a panel↔chip link. Undo restores it."""

    def __init__(self, project: Project, pc: PanelConnection):
        self.project = project
        self.pc = pc

    def execute(self) -> None:
        self.project._remove_panel_connection(self.pc)

    def undo(self) -> None:
        self.project._add_panel_connection(self.pc)

    def description(self) -> str:
        return (f"Disconnect panel {self.pc.panel}.{self.pc.panel_port} → "
                f"chip {self.pc.chip}.{self.pc.chip_port}")
