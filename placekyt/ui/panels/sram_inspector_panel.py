"""SramInspectorPanel — a window around :class:`SramInspectorView`.

Opened by double-clicking an SRAM panel on the canvas. Shows the panel's 256×256
word map (live), a hover readout (address + value), and a Fit button. It pulls
its data from a provider callback so it stays decoupled from the sim/device:
``refresh()`` asks the host for ``(mem_snapshot, activity)`` and feeds the view,
then animates the activity blink decay on a timer.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ui.widgets.sram_inspector import SramInspectorView


class SramInspectorPanel(QWidget):
    """A live SRAM-contents inspector for one panel."""

    def __init__(self, panel_id: int, label: str = "",
                 size_words: int = 1 << 16, *, provider=None, parent=None):
        super().__init__(parent)
        self.panel_id = panel_id
        # provider() -> (mem: dict[addr,value], activity: list[(addr, "w"|"r")])
        self._provider = provider
        self.setWindowTitle(f"SRAM Inspector — {label or f'Panel {panel_id}'}")
        self.setWindowFlag(Qt.Window, True)
        self.resize(560, 600)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)

        self.view = SramInspectorView(size_words)
        self.view.hover_addr.connect(self._on_hover)
        outer.addWidget(self.view, 1)

        bar = QHBoxLayout()
        self._readout = QLabel("Hover a cell to read its address/value.")
        self._readout.setStyleSheet("color: #aab;")
        bar.addWidget(self._readout, 1)
        fit = QPushButton("Fit")
        fit.clicked.connect(self.view.fit)
        bar.addWidget(fit)
        legend = QLabel("● write   ● read")
        legend.setStyleSheet("color: #8c8; ")  # quick legend tint
        bar.addWidget(legend)
        outer.addLayout(bar)

        # Decay the activity blink smoothly while the window is open.
        self._timer = QTimer(self)
        self._timer.setInterval(60)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    # -- data -----------------------------------------------------------------

    def set_provider(self, provider) -> None:
        self._provider = provider

    def refresh(self) -> None:
        """Pull the latest contents + activity from the provider and feed the
        view. Called by the host after each sim step / debug refresh."""
        if self._provider is None:
            return
        mem, activity = self._provider()
        self.view.set_contents(mem)
        if activity:
            self.view.add_activity(activity)

    def _tick(self) -> None:
        self.view.decay()

    def _on_hover(self, addr: int, value: int) -> None:
        self._readout.setText(f"addr 0x{addr:04X} ({addr})   =   "
                              f"0x{value:04X} ({value})")

    def showEvent(self, ev) -> None:  # noqa: N802
        super().showEvent(ev)
        self.view.fit()
        self.refresh()
