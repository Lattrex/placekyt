"""TransactionLogPanel — the ordered, timestamped transaction stream (§debug 3.1).

The debug "what happened, in order, when" view, folding in the old Output panel:

* **Payload mode** (detail toggle OFF): the input↔output sample table — the
  simple "what came out" view (the previous Output panel).
* **Transaction mode** (detail toggle ON): the full trace — every WRITE/JUMP
  instruction word, DATA payload, hop count, dest register, and face, in time
  order. The deep "what the chip actually did" view.

Filters by chip / cell / kind. Clicking a transaction row moves the shared time
cursor (``cursor_requested``) so every other debug view follows. Reads a
:class:`engine.trace_model.TraceModel`.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .output_panel import OutputPanel, _fmt

# Trace kinds shown in the kind filter (None = all).
_KINDS = ["(all)", "port_injection", "instr_arrival", "data_arrival",
          "exec_tick", "output_ready", "port_capture"]


class TransactionLogPanel(QWidget):
    """Ordered transaction table + payload view, over a TraceModel."""

    # time_ns to move the shared cursor to (a row was clicked).
    cursor_requested = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._model = None  # engine.trace_model.TraceModel
        self._rows: list = []  # Transaction per table row (for cursor mapping)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)

        # Header: title + detail toggle + (transaction-mode) filters + radix.
        hdr = QHBoxLayout()
        self._title = QLabel("Output: (run a simulation)")
        hdr.addWidget(self._title)
        hdr.addStretch()
        self._detail = QCheckBox("Full transactions")
        self._detail.setToolTip(
            "Off: input/output payload table. On: the full WRITE+DATA+JUMP "
            "transaction stream (timestamped, ordered).")
        self._detail.toggled.connect(self._on_detail_toggled)
        hdr.addWidget(self._detail)
        self._chip_filter = QComboBox()
        self._cell_filter = QComboBox()
        self._kind_filter = QComboBox()
        self._kind_filter.addItems(_KINDS)
        for f in (self._chip_filter, self._cell_filter, self._kind_filter):
            f.currentIndexChanged.connect(self._refresh_transactions)
        self._filter_label = QLabel("Chip:")
        self._cell_label = QLabel("Cell:")
        hdr.addWidget(self._filter_label)
        hdr.addWidget(self._chip_filter)
        hdr.addWidget(self._cell_label)
        hdr.addWidget(self._cell_filter)
        hdr.addWidget(QLabel("Kind:"))
        hdr.addWidget(self._kind_filter)
        hdr.addWidget(QLabel("Value:"))
        self.fmt = QComboBox()
        self.fmt.addItems(["Hex", "Signed", "Q15"])
        self.fmt.currentTextChanged.connect(self._refresh)
        hdr.addWidget(self.fmt)
        outer.addLayout(hdr)

        # Stacked: [0] payload (OutputPanel), [1] transaction table.
        self._stack = QStackedWidget()
        self._payload = OutputPanel()
        self._stack.addWidget(self._payload)

        self._txn_table = QTableWidget(0, 5)
        self._txn_table.setHorizontalHeaderLabels(
            ["time (ns)", "chip", "cell", "kind", "detail"])
        self._txn_table.verticalHeader().setVisible(False)
        self._txn_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._txn_table.setSelectionBehavior(QTableWidget.SelectRows)
        # Fixed/Interactive column widths — NOT ResizeToContents. ResizeToContents
        # re-measures every row on each insert, which is ~O(n²) and was the main
        # on-screen hang when toggling detail/filters/radix on a large trace.
        h = self._txn_table.horizontalHeader()
        for c in range(4):
            h.setSectionResizeMode(c, QHeaderView.Interactive)
        h.setSectionResizeMode(4, QHeaderView.Stretch)
        for c, w in ((0, 90), (1, 44), (2, 64), (3, 96)):
            self._txn_table.setColumnWidth(c, w)
        self._txn_table.cellClicked.connect(self._on_row_clicked)
        self._stack.addWidget(self._txn_table)
        outer.addWidget(self._stack)

        self._set_transaction_mode(False)

    # -- payload passthrough (the old Output panel API) -----------------------

    # The payload table internals, exposed for callers/tests that read the
    # captured/injected samples directly.
    @property
    def _samples(self):
        return self._payload._samples

    @property
    def _inputs(self):
        return self._payload._inputs

    @property
    def table(self):
        return self._payload.table

    def set_inputs(self, inputs) -> None:
        self._payload.set_inputs(inputs)

    def on_output(self, payload) -> None:
        self._payload.on_output(payload)
        if isinstance(payload, dict) and payload.get("port") is not None:
            chip, port = payload.get("chip"), payload.get("port")
            where = f"chip{chip}.{port}" if chip is not None else str(port)
            self._title.setText(f"Output @ {where}")
        else:
            self._title.setText("Output: (run a simulation)")

    # -- transaction model ----------------------------------------------------

    def set_trace_model(self, model) -> None:
        self._model = model
        txns = model.transactions if model else []
        # Chip filter.
        self._repopulate(self._chip_filter,
                         [(f"chip {c}", c) for c in sorted({t.chip for t in txns})])
        # Cell filter — every (chip, x, y) that appears (excluding port-only).
        cells = sorted({(t.chip, t.cx, t.cy) for t in txns if t.cx >= 0})
        self._repopulate(self._cell_filter,
                         [(f"c{c}:({x},{y})", (c, x, y)) for (c, x, y) in cells])
        if self._detail.isChecked():
            self._refresh_transactions()
            # set_trace_model is called on each step/stop with the cursor parked
            # at the live edge — keep the newest transactions in view so the user
            # sees what just happened while single-stepping. (A row-click scrub
            # moves only the cursor, not the model, so it won't auto-scroll.)
            self._scroll_to_latest()

    @staticmethod
    def _repopulate(combo, items) -> None:
        """Refill a filter combo keeping the current selection if still present.
        Matches userData manually (Qt's findData mishandles tuples)."""
        cur = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("(all)", None)
        keep = 0
        for n, (text, data) in enumerate(items, start=1):
            combo.addItem(text, data)
            if data == cur:
                keep = n
        combo.setCurrentIndex(keep)
        combo.blockSignals(False)

    def filter_to_cell(self, chip: int, x: int, y: int) -> None:
        """Filter the transaction view to one cell (e.g. canvas cell clicked) —
        also pins the chip filter to that cell's chip. Switches to transaction
        mode so the effect is visible."""
        self._detail.setChecked(True)
        self._select_data(self._chip_filter, chip)
        self._select_data(self._cell_filter, (chip, x, y))

    def clear_cell_filter(self) -> None:
        """Reset the cell + chip filters to '(all)' (e.g. selection cleared)."""
        if self._cell_filter.currentIndex() != 0 \
                or self._chip_filter.currentIndex() != 0:
            self._cell_filter.setCurrentIndex(0)
            self._chip_filter.setCurrentIndex(0)

    @staticmethod
    def _select_data(combo, data) -> None:
        """Select the combo item whose userData == ``data`` (manual match —
        Qt's findData mishandles tuples)."""
        for i in range(combo.count()):
            if combo.itemData(i) == data:
                combo.setCurrentIndex(i)
                return

    def _on_detail_toggled(self, on: bool) -> None:
        self._set_transaction_mode(on)
        self._refresh()

    def _set_transaction_mode(self, on: bool) -> None:
        self._stack.setCurrentIndex(1 if on else 0)
        for w in (self._chip_filter, self._cell_filter, self._kind_filter,
                  self._filter_label, self._cell_label):
            w.setVisible(on)

    def _refresh(self) -> None:
        if self._detail.isChecked():
            self._refresh_transactions()
        else:
            self._payload.fmt.setCurrentText(self.fmt.currentText())

    def _refresh_transactions(self) -> None:
        fmt = self.fmt.currentText()
        chip_f = self._chip_filter.currentData()
        cell_f = self._cell_filter.currentData()
        kind_f = self._kind_filter.currentText()
        kind_f = None if kind_f == "(all)" else kind_f
        txns = [t for t in (self._model.transactions if self._model else [])
                if (chip_f is None or t.chip == chip_f)
                and (cell_f is None or (t.chip, t.cx, t.cy) == cell_f)
                and (kind_f is None or t.kind == kind_f)]
        self._rows = txns
        # Disable repaints during the bulk fill so each setItem doesn't trigger a
        # viewport relayout/paint — this is the difference between a snappy and a
        # multi-second refresh on a large trace.
        self._txn_table.setUpdatesEnabled(False)
        try:
            self._txn_table.setRowCount(len(txns))
            for r, t in enumerate(txns):
                self._txn_table.setItem(r, 0, QTableWidgetItem(f"{t.time_ns:.1f}"))
                self._txn_table.setItem(r, 1, QTableWidgetItem(str(t.chip)))
                self._txn_table.setItem(
                    r, 2, QTableWidgetItem(f"({t.cx},{t.cy})"))
                self._txn_table.setItem(r, 3, QTableWidgetItem(t.kind))
                self._txn_table.setItem(r, 4, QTableWidgetItem(_txn_detail(t, fmt)))
        finally:
            self._txn_table.setUpdatesEnabled(True)

    def highlight_cursor(self, ns: float) -> None:
        """Select + scroll to the transaction row at time ``ns`` (the row whose
        time is the nearest at/<= the cursor). Driven by an EXTERNAL cursor move
        (e.g. a waveform click) so the log follows the shared cursor — the
        reverse of clicking a row to move the cursor. No-op unless in detail mode
        with rows shown."""
        if not self._detail.isChecked() or not self._rows:
            return
        # Nearest row with time_ns <= ns (rows are time-sorted).
        target = -1
        for i, t in enumerate(self._rows):
            if t.time_ns <= ns:
                target = i
            else:
                break
        if target < 0:
            target = 0
        # Avoid re-emitting cursor_requested: just move the selection, don't
        # trigger _on_row_clicked.
        self._txn_table.blockSignals(True)
        self._txn_table.selectRow(target)
        self._txn_table.blockSignals(False)
        item = self._txn_table.item(target, 0)
        if item is not None:
            self._txn_table.scrollToItem(item, QAbstractItemView.PositionAtCenter)

    def _scroll_to_latest(self) -> None:
        """Scroll the transaction table so its last (newest) row is visible —
        the live single-step follow behaviour. Deferred to the next event-loop
        turn: scrolling synchronously runs before the table has laid out the
        newly-added rows, so it lands one row short of the true bottom."""
        from PySide6.QtCore import QTimer

        def do_scroll():
            self._txn_table.scrollToBottom()
        QTimer.singleShot(0, do_scroll)

    def _on_row_clicked(self, row: int, _col: int) -> None:
        if 0 <= row < len(self._rows):
            self.cursor_requested.emit(self._rows[row].time_ns)


def _txn_detail(t, fmt: str) -> str:
    """Human-readable detail. Instruction words are DECODED to their mnemonic
    (e.g. 'Write {…}') alongside the raw hex; data is shown as just its value."""
    from engine.trace_model import KIND_DATA, KIND_PORT_IN, KIND_PORT_OUT, decode_word

    parts = []
    if t.face:
        parts.append(f"face={t.face}")
    if t.port:
        parts.append(f"port={t.port}")
    if t.pc is not None:
        parts.append(f"pc={t.pc}")
    # A WORD on a non-data event is an INSTRUCTION — decode it; the instruction
    # word itself is ALWAYS hex (a raw opcode, not a data value, so it ignores
    # the radix). (output_ready with is_data=True carries a data word.)
    if t.word is not None:
        is_data_word = t.detail.get("is_data") is True
        if t.kind not in (KIND_DATA, KIND_PORT_IN, KIND_PORT_OUT) \
                and not is_data_word:
            mnem = decode_word(t.word)
            hexw = f"0x{t.word & 0xFFFF:04X}"
            parts.append(f"{mnem} [{hexw}]" if mnem else hexw)
        else:
            parts.append(f"data={_fmt(t.word, fmt)}")
    # DATA payload (and the port-event data) — just the value.
    if t.data is not None:
        parts.append(f"data={_fmt(t.data, fmt)}")
    if t.dest is not None:
        parts.append(f"→R{t.dest}")
    if t.hop_cnt is not None:
        parts.append(f"hop={t.hop_cnt}")
    for k in ("action", "exit_face"):
        if k in t.detail:
            parts.append(f"{k}={t.detail[k]}")
    return "  ".join(parts)
