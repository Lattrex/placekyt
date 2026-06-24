"""Application preferences, persisted with QSettings.

placeKYT had no preferences store; this is the first. It is intentionally tiny
and Qt-only at the persistence boundary (``QSettings`` keyed by the app's
org/name set in ``main.py``), but the VALUES are plain strings/enums so the
controller and tests can branch on them without touching Qt widgets.

The first setting is the GRC parameter-change policy — what placeKYT does when
it detects the connected GNURadio flowgraph's block params differ from the
placed design (see ``engine/grc_sync.py``):

  * ``notify``    — DEFAULT. Show the out-of-sync indicator; the user clicks
                    Resync to re-apply params + re-place + re-route.
  * ``auto``      — Automatically re-apply + re-place + re-route on detection
                    (seamless "just hit Run in GRC"). DRC violations it can't
                    resolve are surfaced.
  * ``reanchor``  — Re-apply params + resize the block IN PLACE keeping its
                    anchor; do NOT auto-reroute. Resulting DRC violations are
                    surfaced for the user to fix.
"""

from __future__ import annotations

# GRC-param-change policy values (stored verbatim in QSettings).
GRC_NOTIFY = "notify"
GRC_AUTO = "auto"
GRC_REANCHOR = "reanchor"
GRC_MODES = (GRC_NOTIFY, GRC_AUTO, GRC_REANCHOR)

# Human labels for the Preferences combo (value -> label).
GRC_MODE_LABELS = {
    GRC_NOTIFY: "Notify only (default)",
    GRC_AUTO: "Auto place & route",
    GRC_REANCHOR: "Re-anchor only",
}

_KEY_GRC_MODE = "grc/param_change_mode"


def _settings():
    """A QSettings bound to the app org/name (set in ``main.py``)."""
    from PySide6.QtCore import QSettings

    return QSettings()


def grc_param_change_mode() -> str:
    """The current GRC-param-change policy (one of ``GRC_MODES``); defaults to
    ``GRC_NOTIFY`` when unset or stored invalid."""
    val = _settings().value(_KEY_GRC_MODE, GRC_NOTIFY)
    val = str(val) if val is not None else GRC_NOTIFY
    return val if val in GRC_MODES else GRC_NOTIFY


def set_grc_param_change_mode(mode: str) -> None:
    """Persist the GRC-param-change policy. Invalid values are coerced to
    ``GRC_NOTIFY`` so a bad write can't wedge the app."""
    if mode not in GRC_MODES:
        mode = GRC_NOTIFY
    s = _settings()
    s.setValue(_KEY_GRC_MODE, mode)
    s.sync()
