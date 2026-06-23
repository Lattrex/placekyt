"""DisassemblyPanel — view a bitstream as a mnemonic listing (#184).

A dock that loads a ``.kbs`` bitstream (a chip program or an input/golden
stimulus) and shows the disassembly via ``engine.disasm.disassemble_bitstream``.
The word after a WRITE is shown as its DATA payload (``DW``), not mis-decoded —
so a stimulus reads as the WRITE+DATA+JUMP bursts it really is.

Qt lives here; the disassembler itself is Qt-free.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtCore import Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


class DisassemblyPanel(QWidget):
    """Load a ``.kbs`` and show its disassembled mnemonic listing."""

    # (line, on) — a stimulus-line breakpoint was toggled (double-click a line):
    # the run pauses when that word injects (#197).
    breakpoint_toggled = Signal(int, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._words: list[int] = []          # currently loaded chip's words
        self._chips: list[list[int]] = []     # per-chip word lists
        self._source = ""
        self._breakpoints: set[int] = set()   # stimulus-line breakpoints (#197)
        self._hl_line: int | None = None      # current injection highlight line

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)

        bar = QHBoxLayout()
        self._load_btn = QPushButton("Load .kbs…")
        self._load_btn.clicked.connect(self._on_load)
        bar.addWidget(self._load_btn)
        bar.addWidget(QLabel("Chip:"))
        self._chip_spin = QSpinBox()
        self._chip_spin.setMinimum(0)
        self._chip_spin.setMaximum(0)
        self._chip_spin.valueChanged.connect(self._render)
        bar.addWidget(self._chip_spin)
        self._flat = QCheckBox("Flat (no WRITE→DATA)")
        self._flat.setToolTip(
            "Decode every word independently instead of tracking WRITE→DATA "
            "payloads (useful for a flat program image).")
        self._flat.stateChanged.connect(self._render)
        bar.addWidget(self._flat)
        self._clear_bp = QPushButton("Clear Breakpoints")
        self._clear_bp.setToolTip("Remove all stimulus-line breakpoints.")
        self._clear_bp.clicked.connect(self._on_clear_breakpoints)
        bar.addWidget(self._clear_bp)
        bar.addStretch(1)
        outer.addLayout(bar)

        self._title = QLabel(
            "No bitstream loaded. (Double-click a line to set a breakpoint —"
            " the run pauses when that word is injected.)")
        outer.addWidget(self._title)

        self._view = QPlainTextEdit()
        self._view.setReadOnly(True)
        self._view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self._view.setFont(QFont("monospace"))
        self._view.mouseDoubleClickEvent = self._on_view_double_click
        outer.addWidget(self._view)

    # -- public API -----------------------------------------------------------

    def highlight_injected(self, count: int) -> None:
        """Highlight the most-recently-INJECTED stimulus word (#196): ``count``
        words have entered the input port, so line ``count - 1`` is the latest.
        The highlight marches down the listing as data enters the chip."""
        idx = count - 1
        if 0 <= idx < self._view.blockCount():
            self._hl_line = idx
            from PySide6.QtGui import QTextCursor
            self._view.setTextCursor(QTextCursor(
                self._view.document().findBlockByNumber(idx)))
            self._view.ensureCursorVisible()
        self._apply_marks()

    def clear_highlight(self) -> None:
        self._hl_line = None
        self._apply_marks()

    def set_breakpoints(self, lines) -> None:
        """Sync the breakpoint markers to a set of line indices (from the sim)."""
        self._breakpoints = set(int(i) for i in lines)
        self._apply_marks()

    def _apply_marks(self) -> None:
        """Render the injection HIGHLIGHT (green) + BREAKPOINT lines (red) as
        full-width extra selections."""
        from PySide6.QtGui import QColor, QTextCursor, QTextFormat
        from PySide6.QtWidgets import QTextEdit
        sels = []
        doc = self._view.document()
        n = self._view.blockCount()

        def _line(idx, color):
            cur = QTextCursor(doc.findBlockByNumber(idx))
            cur.select(QTextCursor.LineUnderCursor)
            sel = QTextEdit.ExtraSelection()
            sel.cursor = cur
            sel.format.setBackground(color)
            sel.format.setProperty(QTextFormat.FullWidthSelection, True)
            return sel

        for bp in self._breakpoints:
            if 0 <= bp < n:
                sels.append(_line(bp, QColor(120, 40, 40)))   # red = breakpoint
        if self._hl_line is not None and 0 <= self._hl_line < n:
            sels.append(_line(self._hl_line, QColor(70, 110, 60)))  # green = run
        self._view.setExtraSelections(sels)

    # -- breakpoint interaction (#197) ----------------------------------------

    def _line_at(self, pos) -> int:
        return self._view.cursorForPosition(pos).blockNumber()

    def _on_view_double_click(self, event) -> None:
        if self._chips:
            line = self._line_at(event.position().toPoint())
            on = line not in self._breakpoints
            if on:
                self._breakpoints.add(line)
            else:
                self._breakpoints.discard(line)
            self._apply_marks()
            self.breakpoint_toggled.emit(line, on)
        QPlainTextEdit.mouseDoubleClickEvent(self._view, event)

    def _on_clear_breakpoints(self) -> None:
        for line in sorted(self._breakpoints):
            self.breakpoint_toggled.emit(line, False)
        self._breakpoints.clear()
        self._apply_marks()

    def show_words(self, words: list[int], *, source: str = "(bitstream)",
                   chips: list[list[int]] | None = None) -> None:
        """Display a bitstream directly (e.g. the built program of the open
        project). ``chips`` optionally provides a per-chip word list."""
        self._chips = chips if chips is not None else [list(words)]
        self._source = source
        self._chip_spin.blockSignals(True)
        self._chip_spin.setMaximum(max(0, len(self._chips) - 1))
        self._chip_spin.setValue(0)
        self._chip_spin.blockSignals(False)
        self._render()

    # -- internals ------------------------------------------------------------

    def _on_load(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load bitstream", "", "Bitstream (*.kbs);;All files (*)")
        if not path:
            return
        from engine.io.kbs import read_kbs
        try:
            kbs = read_kbs(path)
        except Exception as exc:  # noqa: BLE001
            self._title.setText(f"Load failed: {exc}")
            self._view.setPlainText("")
            return
        from pathlib import Path
        self._source = Path(path).name
        self._chips = [list(c.words) for c in kbs.chips] or [[]]
        # A stimulus/golden .kbs is a WRITE+DATA+JUMP stream → keep stateful.
        kind = (kbs.metadata or {}).get("kind")
        self._title.setProperty("kind", kind)
        self._chip_spin.blockSignals(True)
        self._chip_spin.setMaximum(max(0, len(self._chips) - 1))
        self._chip_spin.setValue(0)
        self._chip_spin.blockSignals(False)
        self._render()

    def _render(self) -> None:
        from engine.disasm import disassemble_bitstream
        if not self._chips:
            self._title.setText("No bitstream loaded.")
            self._view.setPlainText("")
            return
        idx = min(self._chip_spin.value(), len(self._chips) - 1)
        words = self._chips[idx]
        self._title.setText(
            f"{self._source} — chip {idx}, {len(words)} words")
        self._view.setPlainText(
            disassemble_bitstream(words, stateful=not self._flat.isChecked()))
        self._hl_line = None     # stale run highlight; breakpoints persist
        self._apply_marks()
