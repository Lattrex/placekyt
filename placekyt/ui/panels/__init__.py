"""Dockable panels (library, inspector, console, …) — the architecture notes §3.3–3.8."""

from .console_panel import ConsolePanel
from .inspector_panel import InspectorPanel
from .library_panel import LibraryPanel

__all__ = ["LibraryPanel", "InspectorPanel", "ConsolePanel"]
