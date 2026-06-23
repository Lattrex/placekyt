"""BreakpointPanel — list / add / remove breakpoints (DEBUG_ARCHITECTURE §3.6).

A dock listing the active breakpoints with enable checkboxes + remove buttons,
and a small form to add one (cell chip/x/y, type PC/Face, value). Breakpoints
can also be set by right-clicking a canvas cell or clicking a Program-pane row;
this panel is the always-available explicit path and the master list.

Drives a :class:`engine.breakpoints.BreakpointSet` owned by the SimController.
Qt lives here; the BreakpointSet is Qt-free.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from engine.breakpoints import BP_FACE, BP_PC, Breakpoint


class BreakpointPanel(QWidget):
    """The breakpoint list + add form, over the SimController's BreakpointSet."""

    # Emitted when the breakpoint set changes (added/removed/toggled) so the
    # canvas + scrubber can refresh their marks.
    changed = Signal()
    # The user wants to jump the cursor to a fired breakpoint's row (double-click).
    goto_requested = Signal(float)

    def __init__(self, sim, parent=None):
        super().__init__(parent)
        self._sim = sim  # SimController (owns .breakpoints)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)

        # Add form: chip / x / y / type / value / Add.
        form = QHBoxLayout()
        form.addWidget(QLabel("Chip"))
        self._chip = QSpinBox()
        self._chip.setRange(0, 7)
        form.addWidget(self._chip)
        form.addWidget(QLabel("X"))
        self._x = QSpinBox()
        self._x.setRange(0, 31)
        form.addWidget(self._x)
        form.addWidget(QLabel("Y"))
        self._y = QSpinBox()
        self._y.setRange(0, 31)
        form.addWidget(self._y)
        self._type = QComboBox()
        self._type.addItem("PC ==", BP_PC)
        self._type.addItem("Arrival @face", BP_FACE)
        self._type.currentIndexChanged.connect(self._on_type_changed)
        form.addWidget(self._type)
        # Value: a spinbox (PC) or a face combo (Face) — swapped by type.
        self._pc_value = QSpinBox()
        self._pc_value.setRange(0, 63)
        form.addWidget(self._pc_value)
        self._face_value = QComboBox()
        self._face_value.addItems(["N", "S", "E", "W"])
        self._face_value.setVisible(False)
        form.addWidget(self._face_value)
        add = QPushButton("Add")
        add.clicked.connect(self._on_add)
        form.addWidget(add)
        form.addStretch()
        outer.addLayout(form)

        # Breakpoint list.
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["On", "Breakpoint", "", ""])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.itemChanged.connect(self._on_item_changed)
        outer.addWidget(self._table, 1)

        self._empty = QLabel("No breakpoints. Add one above, right-click a cell, "
                             "or click a row in the Program pane.")
        self._empty.setStyleSheet("color: #888;")
        self._empty.setWordWrap(True)
        outer.addWidget(self._empty)

        self.refresh()

    # -- form -----------------------------------------------------------------

    def _on_type_changed(self) -> None:
        is_pc = self._type.currentData() == BP_PC
        self._pc_value.setVisible(is_pc)
        self._face_value.setVisible(not is_pc)

    def _on_add(self) -> None:
        kind = self._type.currentData()
        value = (self._pc_value.value() if kind == BP_PC
                 else self._face_value.currentText())
        self.add_breakpoint(self._chip.value(), self._x.value(),
                            self._y.value(), kind, value)

    # -- API (also used by the canvas / program-pane entry paths) -------------

    def add_breakpoint(self, chip: int, x: int, y: int, kind: str,
                       value) -> None:
        self._sim.breakpoints.add(
            Breakpoint(chip=chip, x=x, y=y, kind=kind, value=value))
        self.refresh()
        self.changed.emit()

    def remove_breakpoint(self, bp) -> None:
        self._sim.breakpoints.remove(bp)
        self.refresh()
        self.changed.emit()

    # -- list -----------------------------------------------------------------

    def refresh(self) -> None:
        bps = self._sim.breakpoints.breakpoints
        self._empty.setVisible(not bps)
        # Block itemChanged while we rebuild rows (setCheckState would fire it).
        self._table.blockSignals(True)
        self._table.setRowCount(len(bps))
        for r, bp in enumerate(bps):
            # Enable checkbox (column 0).
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            chk.setCheckState(Qt.Checked if bp.enabled else Qt.Unchecked)
            self._table.setItem(r, 0, chk)
            self._table.setItem(r, 1, QTableWidgetItem(bp.label()))
            # Remove button (column 2).
            rm = QPushButton("Remove")
            rm.clicked.connect(lambda _c=False, b=bp: self.remove_breakpoint(b))
            self._table.setCellWidget(r, 2, rm)
        self._table.blockSignals(False)

    def _on_item_changed(self, item) -> None:
        if item.column() != 0:
            return
        bps = self._sim.breakpoints.breakpoints
        r = item.row()
        if 0 <= r < len(bps):
            bps[r].enabled = item.checkState() == Qt.Checked
            self.changed.emit()
