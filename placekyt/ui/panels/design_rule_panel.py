# SPDX-License-Identifier: GPL-3.0
"""Design-rule findings panel — a BOTTOM dock (tabbed with Output/Waveform/…) that
lists the DRC / build findings and, on click, highlights the offending cell/block/net
on the canvas. Replaces the modal pop-up so violations don't cover the array."""

from __future__ import annotations

import re

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QListWidget, QListWidgetItem)


def _severity(f) -> str:
    return str(getattr(getattr(f, "severity", None), "value",
                       getattr(f, "severity", ""))).upper()


def block_from_message(message: str) -> str | None:
    """Best-effort: pull a block name from a DRC message (e.g.
    ``frequencymodulator[emit]``) so a finding without an (x,y) can still be
    highlighted by its block footprint."""
    m = re.search(r"\b([a-z][a-z0-9_]+)\[", message or "")
    return m.group(1) if m else None


def net_from_message(message: str) -> str | None:
    """Pull a net/connection name (e.g. ``net11``) from an 'unrouted' message so a
    fly-line finding can be highlighted even though it carries no cell coordinate."""
    m = re.search(r"connection ['\"]?([A-Za-z_][\w]*)['\"]?", message or "")
    if m:
        return m.group(1)
    m = re.search(r"\bnet(\d+)\b", message or "")
    return f"net{m.group(1)}" if m else None


class DesignRulePanel(QWidget):
    """Findings list. Emits ``highlight_requested(dict)`` when a row is clicked; the
    payload has chip/x/y (may be None), the raw message, and the parsed block/net."""

    highlight_requested = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        self._header = QLabel("No design-rule check run yet.")
        self._header.setWordWrap(True)
        lay.addWidget(self._header)
        self._list = QListWidget(self)
        self._list.setWordWrap(True)
        self._list.setAlternatingRowColors(True)
        self._list.itemClicked.connect(self._emit_highlight)
        self._list.itemActivated.connect(self._emit_highlight)
        lay.addWidget(self._list, 1)

    def set_findings(self, title: str, findings) -> None:
        findings = list(findings or [])
        self._list.clear()
        errors = [f for f in findings if _severity(f) == "ERROR"]
        if not findings:
            self._header.setText(f"{title}: clean — no violations.")
        else:
            self._header.setText(
                f"{title}: {len(errors)} error(s), "
                f"{len(findings) - len(errors)} other finding(s). "
                "Click a row to highlight it on the canvas.")
        for f in findings:
            it = QListWidgetItem(str(f))
            sev = _severity(f)
            if sev == "ERROR":
                it.setForeground(Qt.red)
            elif sev == "WARNING":
                it.setForeground(Qt.darkYellow)
            msg = str(getattr(f, "message", str(f)))
            it.setData(Qt.UserRole, {
                "chip": getattr(f, "chip", None),
                "x": getattr(f, "x", None),
                "y": getattr(f, "y", None),
                "message": msg,
                "block": block_from_message(msg),
                "net": net_from_message(msg),
            })
            self._list.addItem(it)

    def _emit_highlight(self, item) -> None:
        data = item.data(Qt.UserRole)
        if data:
            self.highlight_requested.emit(data)
