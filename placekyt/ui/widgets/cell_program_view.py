"""CellProgramView — a built cell's 32-register program in ONE table (§3.3).

A single register-indexed table: **Addr | Value | Instruction**. Each row is one
memory word (R0–R31): the Value column shows it in the selected format
(Hex / Unsigned / Signed / Q15 float, §7.4) and the Instruction column shows its
disassembly. The entry-address row is highlighted; mnemonics carry ISA tooltips
(§2.7); the Instruction column auto-stretches as the panel widens.

For a routing cell (whose program is its FACE config, not main memory) a header
line states the routing FACE so it doesn't look unprogrammed.

Populated from ``controller.cell_program(chip, x, y)``.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtGui import QColor

# ISA mnemonic → short description (from the architecture notes §2.7 quick reference).
# MIME type for dragging a register from the program view → waveform viewer.
# Payload: b"chip,x,y,addr".
REGISTER_MIME = "application/x-placekyt-register"

ISA_HELP = {
    "Halt": "Stop execution",
    "Move": "Copy register (or CONFIG with brackets)",
    "Add": "R0 = Ra + Rb",
    "Sub": "R0 = Ra - Rb",
    "Adc": "R0 = Ra + Rb + Carry",
    "And": "R0 = Ra & Rb (sets Z flag)",
    "Or": "R0 = Ra | Rb",
    "Xor": "R0 = Ra ^ Rb",
    "Not": "R0 = ~Ra",
    "Shl": "R0 = Rn << imm",
    "Shr": "R0 = Rn >> imm",
    "Mul": "R0 = Ra * Rb (MulQ = Q15: (Ra*Rb)>>15)",
    "Mac": "R0 += Ra * Rb (MacQ = Q15)",
    "Msu": "R0 -= Ra * Rb (MsuQ = Q15)",
    "Cmp": "Set flags from Ra - Rb (R0 unchanged)",
    "Load": "R0 = mem[mem[Rn] & 0x1F] (indirect)",
    "Write": "Send R0 to cell at hop distance",
    "Jump": "Trigger cell at hop distance",
    "Branch": "Conditional branch on a flag",
    "Sbc": "R0 = Ra - Rb - ~Carry",
    "Rol": "R0 = Rn rotated left by imm",
    "Ror": "R0 = Rn rotated right by imm",
}

_ENTRY_BG = QColor(60, 90, 120)   # entry-address row highlight
_ENTRY_FG = QColor(235, 235, 235)
_DATA_BG = QColor(30, 70, 40)     # data-word row highlight (green)
_STATE_BG = QColor(60, 55, 30)    # state-register row highlight (amber)
_ROLE_FG = QColor(225, 225, 225)
_PC_OUTLINE = QColor(255, 215, 60)  # current-PC row outline (bright yellow)
_LIVE_FG = QColor(120, 230, 255)    # live (changed) register value text (cyan)


def _format_value(word: int, fmt: str) -> str:
    word &= 0xFFFF
    if fmt == "Hex":
        return f"0x{word:04X}"
    if fmt == "Unsigned":
        return str(word)
    if fmt == "Signed":
        return str(word - 0x10000 if word >= 0x8000 else word)
    if fmt == "Q15":
        signed = word - 0x10000 if word >= 0x8000 else word
        return f"{signed / 32768.0:+.5f}"
    return f"0x{word:04X}"


class CellProgramView(QWidget):
    """Single combined memory+assembly table for one built cell.

    Below the table, a **handoff editor** lets the user override the hop count
    (in ``@N`` hops-away form) and the destination/entry address of each WRITE
    and JUMP instruction in the cell — these are properties of the instruction,
    not of the route (§3.3). Values default to the route-derived auto-fill; the
    user may override per-instruction. The ``instr_changed`` signal carries
    ``(addr, field, value)`` where ``field`` is ``"hop"``/``"dest"``/``"entry"``
    and ``value`` is an int (set) or ``None`` (reset to auto).
    """

    instr_changed = Signal(int, str, object)
    # The user clicked the Reg gutter of a row → toggle a PC breakpoint at that
    # address for the selected cell (DEBUG §3.6, the IDE "click the line" gesture).
    breakpoint_toggled = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._words: list[int] = [0] * 32
        self._disasm: list = []
        self._entry = 0
        self._editors: list = []
        self._classes: dict = {}
        self._bp_addrs: set = set()  # addresses with a PC breakpoint (gutter dot)
        # The cell this view currently shows (chip, x, y) — used to tag a
        # register dragged out to the waveform viewer. None when no cell.
        self._cell: tuple[int, int, int] | None = None
        # Live-overlay state (DEBUG §3.2). When ``_live`` is True the Value column
        # shows ``_live_regs`` (the cell's registers at the cursor) and the row at
        # ``_pc`` is marked as the current program counter.
        self._live = False
        self._live_regs: dict[int, int] = {}
        self._live_changed: set[int] = set()  # regs that changed since last step
        self._pc: int | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)

        hdr = QHBoxLayout()
        hdr.addWidget(QLabel("Program"))
        hdr.addStretch()
        hdr.addWidget(QLabel("Value:"))
        self.fmt = QComboBox()
        self.fmt.addItems(["Hex", "Unsigned", "Signed", "Q15"])
        self.fmt.currentTextChanged.connect(self._refresh)
        hdr.addWidget(self.fmt)
        layout.addLayout(hdr)

        self._face_label = QLabel("")
        self._face_label.setVisible(False)
        layout.addWidget(self._face_label)

        self.table = QTableWidget(32, 3)
        self.table.setHorizontalHeaderLabels(["Reg", "Value", "Instruction"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)  # read-only (yet)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)   # Reg
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)   # Value
        h.setSectionResizeMode(2, QHeaderView.Stretch)            # Instruction grows
        # Clicking the Reg gutter toggles a PC breakpoint at that address.
        self.table.cellClicked.connect(self._on_cell_clicked)
        # Drag a register row OUT to the waveform viewer (plot its value over
        # time). The table is the drag source; the waveform accepts the drop.
        self.table.setDragEnabled(True)
        self.table.setDragDropMode(QTableWidget.DragOnly)
        self.table.startDrag = self._start_register_drag
        for r in range(32):
            self.table.setItem(r, 0, QTableWidgetItem(f"R{r}"))
            self.table.setItem(r, 1, QTableWidgetItem(""))
            self.table.setItem(r, 2, QTableWidgetItem(""))
        layout.addWidget(self.table)

        # Handoff editor — one row per WRITE/JUMP, populated by set_instructions.
        self._handoff = QFrame()
        self._handoff.setVisible(False)
        hv = QVBoxLayout(self._handoff)
        hv.setContentsMargins(0, 4, 0, 0)
        self._handoff_title = QLabel("Handoff (where this cell sends its result)")
        self._handoff_title.setWordWrap(True)
        hv.addWidget(self._handoff_title)
        self._handoff_grid = QGridLayout()
        self._handoff_grid.setHorizontalSpacing(6)
        self._handoff_grid.setVerticalSpacing(2)
        hv.addLayout(self._handoff_grid)
        layout.addWidget(self._handoff)

    # -- population -----------------------------------------------------------

    def set_program(self, entry: int, words: list[int], disasm: list,
                    face: str | None = None, has_program: bool = True,
                    routing_only: bool = False, classes: dict | None = None,
                    fabric_routing: bool = False) -> None:
        self._entry = entry
        self._words = list(words)
        # ``has_program`` is False for routing-only cells (no real entry/program)
        # — don't highlight an "entry" row for those (R0 would look special). A
        # block cell that ALSO forwards (face set, has_program True) keeps its
        # entry highlight; ``routing_only`` is the true distinction, not ``face``.
        self._has_program = has_program and not routing_only
        # {addr: {"role", "name"}} — data/state/instruction classification.
        self._classes = classes or {}
        # Index disasm by address for the combined table.
        self._disasm = {a: m for a, _w, m in disasm}
        # A FABRIC ROUTING cell — a face-only transit cell OR a programmed routing
        # cell (broker/crossover, or a §1.4 UNIVERSAL transit cell carrying the
        # transmit/relay program) — gets the "Routing cell" banner. It is part of
        # the control fabric, not an owning block, even when it now has a program.
        if face and (routing_only or fabric_routing):
            self._face_label.setText(f"Routing cell — FACE = {face}")
            self._face_label.setVisible(True)
        elif face:
            # A block cell that also forwards its output in a fixed direction.
            self._face_label.setText(f"Output FACE = {face}")
            self._face_label.setVisible(True)
        else:
            self._face_label.setVisible(False)
        self._refresh()

    def clear(self) -> None:
        """Blank the view (no cell selected) — used when the view is a dock that
        stays visible but should show nothing."""
        self._words = [0] * 32
        self._disasm = {}
        self._classes = {}
        self._has_program = False
        self._live = False
        self._live_regs = {}
        self._live_changed = set()
        self._pc = None
        self._bp_addrs = set()
        self._face_label.setText("No cell selected")
        self._face_label.setVisible(True)
        self.set_instructions([])
        self._refresh()

    def set_live_state(self, pc: int | None, registers: dict[int, int]) -> None:
        """Enter live mode (DEBUG §3.2): show ``registers`` (the cell's values at
        the cursor) in the Value column and mark row ``pc`` as the current PC.

        Registers that changed since the previous live update are flagged so the
        Value column can render them in the live/changed colour — the "watch the
        registers update as you step" cue."""
        prev = self._live_regs if self._live else {}
        self._live_changed = {a for a, v in registers.items()
                              if prev.get(a) != v} if prev else set()
        self._live = True
        self._live_regs = dict(registers)
        self._pc = pc
        self._refresh()

    def set_cell(self, chip: int, x: int, y: int) -> None:
        """Record which cell this view shows (for register drag-out)."""
        self._cell = (chip, x, y)

    def _start_register_drag(self, _supported_actions) -> None:
        """Begin a drag carrying the selected register (addr) so it can be
        dropped on the waveform viewer to plot its value over time."""
        from PySide6.QtCore import QMimeData
        from PySide6.QtGui import QDrag

        if self._cell is None:
            return
        rows = self.table.selectionModel().selectedRows()
        addr = rows[0].row() if rows else self.table.currentRow()
        if addr < 0:
            return
        chip, x, y = self._cell
        mime = QMimeData()
        mime.setData(REGISTER_MIME,
                     f"{chip},{x},{y},{addr}".encode("utf-8"))
        mime.setText(f"c{chip}:({x},{y}).R{addr}")
        drag = QDrag(self.table)
        drag.setMimeData(mime)
        drag.exec(Qt.CopyAction)

    def set_breakpoint_addrs(self, addrs) -> None:
        """Set which addresses have a PC breakpoint (gutter dot) and repaint."""
        new = set(addrs)
        if new != self._bp_addrs:
            self._bp_addrs = new
            self._refresh()

    def _on_cell_clicked(self, row: int, col: int) -> None:
        # Click on the Reg gutter (col 0) toggles a PC breakpoint at that addr.
        if col == 0:
            self.breakpoint_toggled.emit(int(row))

    def clear_live_state(self) -> None:
        """Leave live mode — the Value column reverts to the static program
        words and the PC marker is removed."""
        if not self._live and self._pc is None:
            return
        self._live = False
        self._live_regs = {}
        self._live_changed = set()
        self._pc = None
        self._refresh()

    def set_instructions(self, instructions: list) -> None:
        """Populate the handoff editor from ``controller.cell_program(...)``.

        ``instructions`` is the per-WRITE/JUMP metadata list (see
        ``AppController._cell_instructions``). An empty list hides the editor.
        """
        # Clear existing editor rows.
        while self._handoff_grid.count():
            item = self._handoff_grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._editors = []
        if not instructions:
            self._handoff.setVisible(False)
            return

        # Header row.
        for col, text in enumerate(("Instr", "Hops away", "Target")):
            lbl = QLabel(text)
            self._handoff_grid.addWidget(lbl, 0, col)
        for i, instr in enumerate(instructions, start=1):
            self._add_handoff_row(i, instr)
        self._handoff.setVisible(True)

    def _add_handoff_row(self, row: int, instr: dict) -> None:
        addr = instr["addr"]
        kind = instr["kind"]
        # Label: e.g. "R2 WRITE" so it ties to the table row above.
        self._handoff_grid.addWidget(QLabel(f"R{addr} {kind}"), row, 0)

        # Hop spinbox (in @N hops-away form). Override when != auto.
        hop = QSpinBox()
        hop.setRange(0, 31)
        hop.setPrefix("@")
        hop.setValue(int(instr["hop"]))
        hop.setToolTip("Hops away (@N) — how many cells the handoff travels")
        self._handoff_grid.addWidget(hop, row, 1)
        hop.valueChanged.connect(
            lambda v, a=addr: self.instr_changed.emit(a, "hop", int(v)))

        # Target field: dest reg (WRITE) or entry addr (JUMP). Use a combo when
        # the downstream interface offers options, else a plain spinbox.
        field_name = "dest" if kind == "WRITE" else "entry"
        field_kind = instr.get("field_kind", "reg")
        options = instr.get("field_options") or []
        cur = int(instr["field"])
        target_box = QWidget()
        tb_layout = QHBoxLayout(target_box)
        tb_layout.setContentsMargins(0, 0, 0, 0)
        tb_layout.setSpacing(2)

        # WRITE may target a CONFIG address (C0–C31) as well as a data register
        # (R0–R31) — a small R/C selector picks the space; the spinbox picks the
        # address. JUMP can only target a register (an entry address), so it has
        # no R/C selector.
        space_combo = None
        if field_kind == "reg_or_config":
            space_combo = QComboBox()
            space_combo.addItem("R", "reg")     # data register
            space_combo.addItem("C", "config")  # CONFIG address
            space_combo.setCurrentIndex(1 if instr.get("field_config") else 0)
            space_combo.setToolTip("R = data register, C = CONFIG address")
            tb_layout.addWidget(space_combo)

        spin = QSpinBox()
        spin.setRange(0, 31)
        spin.setValue(cur)
        spin.setToolTip(
            "Destination address (R = register, C = CONFIG)" if kind == "WRITE"
            else "Entry address to JUMP into")
        tb_layout.addWidget(spin)

        # Optional dropdown of downstream-interface choices — shown as a hint
        # combo that, when changed, drives the spinbox. Only when >1 option.
        opt_combo = None
        if len(options) > 1:
            opt_combo = QComboBox()
            opt_combo.addItem("—", None)
            for opt in options:
                opt_combo.addItem(f"R{opt}" if kind == "WRITE" else f"addr {opt}",
                                  opt)
            opt_combo.setToolTip(
                "Downstream input registers" if kind == "WRITE"
                else "Downstream entry points")
            tb_layout.addWidget(opt_combo)

        def emit_target():
            val = int(spin.value())
            self.instr_changed.emit(addr, field_name, val)
            if space_combo is not None:
                self.instr_changed.emit(
                    addr, "dest_config",
                    space_combo.currentData() == "config")

        spin.valueChanged.connect(lambda _v: emit_target())
        if space_combo is not None:
            space_combo.currentIndexChanged.connect(lambda _i: emit_target())
        if opt_combo is not None:
            opt_combo.currentIndexChanged.connect(
                lambda _i, c=opt_combo, s=spin:
                    s.setValue(int(c.currentData())) if c.currentData() is not None
                    else None)
        self._handoff_grid.addWidget(target_box, row, 2)

        # Reset-to-auto button, enabled only when an override is active.
        overridden = (instr.get("hop_override") is not None
                      or instr.get("field_override") is not None
                      or instr.get("config_override") is not None)
        reset = QToolButton()
        reset.setText("auto")
        reset.setToolTip("Clear overrides — use the route-derived auto value")
        reset.setEnabled(overridden)
        reset.clicked.connect(
            lambda _c=False, a=addr, f=field_name, w=(kind == "WRITE"):
                self._reset_row(a, f, w))
        self._handoff_grid.addWidget(reset, row, 3)
        self._editors.append((addr, hop, target_box, reset))

    def _reset_row(self, addr: int, field_name: str, is_write: bool) -> None:
        # Emit None for hop + target to clear the override; also clear the
        # WRITE config flag so the dest space reverts to the auto value.
        self.instr_changed.emit(addr, "hop", None)
        self.instr_changed.emit(addr, field_name, None)
        if is_write:
            self.instr_changed.emit(addr, "dest_config", None)

    def _refresh(self) -> None:
        from PySide6.QtGui import QBrush

        fmt = self.fmt.currentText()
        clear = QBrush()  # default (theme) background/foreground
        for r in range(32):
            # In live mode the Value column shows the cell's register value at
            # the cursor (from the engine/trace), not the static program word.
            word = self._live_regs.get(r, self._words[r]) if self._live \
                else self._words[r]
            self.table.item(r, 1).setText(_format_value(word, fmt))
            # The Reg column carries a ● breakpoint dot + the ▶ PC marker (live).
            reg_item = self.table.item(r, 0)
            bp = "●" if r in self._bp_addrs else ""
            pc = "▶" if (self._live and r == self._pc) else ""
            reg_item.setText(f"{bp}{pc}R{r}")
            role_info = self._classes.get(r) or {}
            role = role_info.get("role")
            name = role_info.get("name")
            instr = self.table.item(r, 2)
            # DATA/STATE words are NOT instructions — show their role + name,
            # not a (meaningless) disassembly of the coefficient bits.
            if role in ("data", "state", "input", "output"):
                label = role if not name else f"{role} ({name})"
                instr.setText(label)
                instr.setToolTip(
                    "Data word — a value in memory, not an executable "
                    "instruction. Edit via the block's parameters."
                    if role == "data" else f"{role} register"
                    if role in ("state", "input", "output") else "")
                mnem = ""
            else:
                mnem = self._disasm.get(r, "")
                instr.setText(mnem)
                head = mnem.split(" ", 1)[0].split("{", 1)[0].strip()
                instr.setToolTip(
                    f"{head}: {ISA_HELP[head]}" if head in ISA_HELP else "")
            entry_hl = self._has_program and r == self._entry
            is_pc = self._live and r == self._pc
            if role == "data":
                bg, fg = _DATA_BG, _ROLE_FG
            elif role in ("state", "input", "output"):
                bg, fg = _STATE_BG, _ROLE_FG
            elif entry_hl:
                bg, fg = _ENTRY_BG, _ENTRY_FG
            else:
                bg = fg = None
            # The current-PC row is rendered with a bright outline-coloured text
            # on a dim fill, distinct from the entry/data/state fills underneath.
            if is_pc:
                bg = QColor(70, 60, 20)  # dim amber backing for the bright text
                fg = _PC_OUTLINE
            for col in range(3):
                cell = self.table.item(r, col)
                if bg is not None:
                    cell.setBackground(bg)
                    cell.setForeground(fg)
                else:
                    # Reset to the theme default (NOT transparent, which renders
                    # text in an unreadable colour on some themes).
                    cell.setBackground(clear)
                    cell.setForeground(clear)
            # A register that changed since the last step gets a cyan Value text
            # (overrides the row fg) — the "watch registers update" cue. Skip on
            # the PC row so its bright outline colour wins.
            if self._live and r in self._live_changed and not is_pc:
                self.table.item(r, 1).setForeground(_LIVE_FG)

    # -- helpers (tests) ------------------------------------------------------

    def value_text(self, reg: int) -> str:
        return self.table.item(reg, 1).text()

    def instruction_text(self, reg: int) -> str:
        return self.table.item(reg, 2).text()

    def row_count(self) -> int:
        return self.table.rowCount()
