"""Preferences dialog — the app's first persisted settings (QSettings).

Currently exposes one setting: the GRC parameter-change policy (what placeKYT
does when it detects the connected GNURadio flowgraph's block params drifted from
the placed design — see ``engine/grc_sync.py`` and ``engine/preferences.py``).
Reachable from Edit → Preferences. Persists via QSettings on Apply/OK.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QVBoxLayout,
)

from engine import preferences


class PreferencesDialog(QDialog):
    """Edit + persist application preferences."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Preferences")
        self.setModal(True)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)

        self._grc_mode = QComboBox()
        for value in preferences.GRC_MODES:
            self._grc_mode.addItem(preferences.GRC_MODE_LABELS[value], value)
        current = preferences.grc_param_change_mode()
        idx = self._grc_mode.findData(current)
        if idx >= 0:
            self._grc_mode.setCurrentIndex(idx)
        self._grc_mode.setToolTip(
            "What placeKYT does when the connected GNURadio flowgraph's block "
            "parameters differ from the placed design.\n"
            "• Notify only: show an indicator; you click Resync.\n"
            "• Auto place & route: re-apply params + re-place + re-route "
            "automatically.\n"
            "• Re-anchor only: re-apply params + resize in place; surface any "
            "DRC violations.")
        form.addRow("On GRC parameter change:", self._grc_mode)

        hint = QLabel(
            "Resync re-applies the GRC parameters to the affected blocks. A "
            "parameter change can resize a block (e.g. a FIR going 7→40 taps), "
            "so a resync may re-place and re-route.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray;")
        layout.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept(self) -> None:
        preferences.set_grc_param_change_mode(self._grc_mode.currentData())
        self.accept()
