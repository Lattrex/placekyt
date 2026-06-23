"""Resource path resolution (the architecture notes §6).

All resource access goes through :func:`resource_path` so it works both in
development and in a frozen PyInstaller bundle. Lives at the top level so
``model/``, ``engine/``, and ``ui/`` can all import it.

Direct use of ``Path(__file__).parent`` for resources is forbidden — it breaks
in PyInstaller onefile mode, where bundled data lives under ``sys._MEIPASS``.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _base_dir() -> Path:
    """The root to resolve resources against.

    In a frozen PyInstaller bundle that is ``sys._MEIPASS``; in development it
    is this file's directory (the package root, flat layout — §6).
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent


def resource_path(relative: str) -> Path:
    """Resolve ``relative`` (e.g. ``"resources/icons/gain.svg"``) to an absolute
    path valid in both dev and frozen modes."""
    return _base_dir() / relative
