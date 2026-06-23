"""OutputPanel — the values captured at the design's output port (§3.7).

Shows, per sample, the index, the captured output value (hex / signed / Q15),
and — when a stimulus is loaded — the corresponding input value, so the user can
SEE what the chip(s) produced and check it against what went in. Populated from
``SimController.output`` (``{"chip", "port", "samples"}``).
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


def _fmt(word: int, fmt: str) -> str:
    word &= 0xFFFF
    if fmt == "Hex":
        return f"0x{word:04X}"
    if fmt == "Signed":
        return str(word - 0x10000 if word >= 0x8000 else word)
    if fmt == "Q15":
        signed = word - 0x10000 if word >= 0x8000 else word
        return f"{signed / 32768.0:+.5f}"
    return f"0x{word:04X}"


class OutputPanel(QWidget):
    """Read-only table of captured output-port samples (+ inputs when known)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._samples: list[int] = []
        self._inputs: list[int] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)

        hdr = QHBoxLayout()
        self._title = QLabel("Output: (run a simulation)")
        hdr.addWidget(self._title)
        hdr.addStretch()
        hdr.addWidget(QLabel("Format:"))
        self.fmt = QComboBox()
        self.fmt.addItems(["Hex", "Signed", "Q15"])
        self.fmt.currentTextChanged.connect(self._refresh)
        hdr.addWidget(self.fmt)
        outer.addLayout(hdr)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["#", "Input", "Output"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.Stretch)
        h.setSectionResizeMode(2, QHeaderView.Stretch)
        outer.addWidget(self.table)

    def set_inputs(self, inputs: list[int]) -> None:
        """Record the injected stimulus so outputs can be shown beside inputs."""
        self._inputs = list(inputs)

    def on_output(self, payload) -> None:
        """Slot for ``SimController.output``: ``{"chip","port","samples"}``."""
        if not isinstance(payload, dict):
            return
        self._samples = list(payload.get("samples") or [])
        port = payload.get("port")
        chip = payload.get("chip")
        if port is None:
            self._title.setText("Output: (run a simulation)")
        else:
            where = f"chip{chip}.{port}" if chip is not None else str(port)
            self._title.setText(
                f"Output @ {where} — {len(self._samples)} sample(s)")
        self._refresh()

    def _refresh(self) -> None:
        fmt = self.fmt.currentText()
        self.table.setRowCount(len(self._samples))
        for i, v in enumerate(self._samples):
            self.table.setItem(i, 0, QTableWidgetItem(str(i)))
            in_txt = _fmt(self._inputs[i], fmt) if i < len(self._inputs) else ""
            self.table.setItem(i, 1, QTableWidgetItem(in_txt))
            self.table.setItem(i, 2, QTableWidgetItem(_fmt(v, fmt)))
