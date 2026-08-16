# SPDX-License-Identifier: GPL-3.0-or-later
"""Dockable panels (library, inspector, console, …)."""

from .console_panel import ConsolePanel
from .inspector_panel import InspectorPanel
from .library_panel import LibraryPanel

__all__ = ["LibraryPanel", "InspectorPanel", "ConsolePanel"]
