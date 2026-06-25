"""Inspector panel — context-sensitive details for the selection (§3.3).

Shows the selected cell's coordinates, face, kind, owning block + params, AND —
for a built cell — its 32-register memory and disassembled program
(CellProgramView). Editable memory/params is a later step.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from model.enums import Face
from model.project import Project

from ui.widgets import CellProgramView


class InspectorPanel(QWidget):
    """Shows details for the currently selected cell / block."""

    # (block_name, cell_id, face_value) — user changed a block-cell face.
    face_changed = Signal(str, object, str)
    # (block_name, cell_id, addr, field, value) — user changed a per-instruction
    # handoff override in the cell program view. ``field`` ∈ hop/dest/entry;
    # ``value`` is an int (set) or None (reset to the route-derived auto value).
    instr_override_changed = Signal(str, object, int, str, object)
    # (block_name, params_dict) — user edited a block's parameters.
    params_changed = Signal(str, dict)
    # (old_name, new_name) — user renamed a block instance.
    block_renamed = Signal(str, str)

    def __init__(self, controller=None, parent=None, program_view=None):
        super().__init__(parent)
        self.setMinimumWidth(250)
        self._project: Project | None = None
        self._controller = controller  # for cell_program() lookups
        self._sel: dict | None = None
        self._face_combo: QComboBox | None = None
        self._rows: list = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        self._form_host = QWidget()
        self._form = QFormLayout(self._form_host)
        self._title = QLabel("No selection")
        self._form.addRow(self._title)
        outer.addWidget(self._form_host)

        # The program/handoff view may be EXTERNAL (its own dock) so it doesn't
        # compete with the cell-info + params for vertical space. When external,
        # we drive it but don't embed it.
        self._external_program = program_view is not None
        self._program = program_view or CellProgramView()
        self._program.instr_changed.connect(self._on_instr_changed)
        # (block_name, cell_id) the program view currently shows — needed to
        # route its per-instruction edits back to the right block placement.
        self._prog_owner: tuple | None = None
        if not self._external_program:
            self._program.setVisible(False)
            outer.addWidget(self._program, 1)
        outer.addStretch(0)

    def set_controller(self, controller) -> None:
        self._controller = controller

    def update_live_state(self, sim) -> None:
        """Push the selected cell's live PC + registers into the program view
        (DEBUG §3.2 Cell Inspector live mode). Called after each sim step/stop
        and on cursor moves. Clears the overlay when nothing's running or the
        selection isn't a real cell."""
        sel = self._sel
        cell = sel.get("cell") if sel else None
        if (sim is None or not sim.has_run()
                or not isinstance(cell, tuple)
                or not isinstance(cell[0], int)):
            self._program.clear_live_state()
            return
        cx, cy = cell
        chip = sel.get("chip", 0) or 0
        state = sim.cell_live_state(chip, cx, cy)
        self._program.set_live_state(state["pc"], state["registers"])

    def _hide_program(self) -> None:
        """Hide/blank the program view. When external (a dock) it stays visible
        but shows an empty state; when embedded it is hidden outright."""
        if self._external_program:
            self._program.clear()
        else:
            self._program.setVisible(False)

    def set_project(self, project: Project) -> None:
        self._project = project
        self.show_selection(None)

    def show_selection(self, sel: dict | None) -> None:
        """Render a selection descriptor from the canvas (or clear it)."""
        self._clear_rows()
        self._face_combo = None
        self._sel = sel
        self._hide_program()
        if sel is None:
            self._title.setText("No selection")
            return

        # A selected connection (route line) → endpoints. (Hop/dest/entry are
        # per-instruction properties edited in the cell program view, not here —
        # the route is passive, §3.3.)
        conn_name = sel.get("connection")
        if conn_name is not None:
            self._show_connection(conn_name)
            return

        cx, cy = sel.get("cell", ("?", "?"))
        kind = sel.get("kind", "?")
        block_name = sel.get("block")
        face = sel.get("face")
        route = sel.get("route")

        self._title.setText(f"Cell ({cx}, {cy})")
        prog_kind = None
        if route:
            # A routing (transit) cell — shown as such with its output face.
            self._add_row("Kind", "routing cell")
            self._add_row("Route", route)
            if face:
                self._add_row("Output face", face)
        else:
            # A programmed routing cell (bus BROKER / CROSSOVER) — no owning block,
            # but it carries the fabric's relay/demux control logic. Label it so it
            # doesn't read as a bare cell, and force the program view below.
            prog_kind = None
            if self._controller is not None and isinstance(cx, int):
                try:
                    _p = self._controller.cell_program(sel.get("chip", 0), cx, cy)
                    prog_kind = _p.get("kind") if _p else None
                except Exception:  # noqa: BLE001
                    prog_kind = None
            self._add_row("Kind", "bus broker (routing control)"
                          if prog_kind == "broker" else kind)
            if face:
                if block_name:
                    self._add_face_combo(face)  # editable for block cells
                else:
                    self._add_row("Face", face)
            if block_name and self._project is not None:
                block = self._project.block(block_name)
                # Instance NAME is editable (rename); block TYPE is read-only.
                self._add_name_editor(block_name)
                if block is not None:
                    self._add_row("Type", block.type)
                    self._add_verification_row(block)
                    if block.params:
                        self._add_params_editor(block)

        # Memory + assembly view — only for cells the MODEL actually programs (a
        # block cell or a routing/transit cell on a real route). A bare EMPTY
        # cell must read as unprogrammed even though the build auto-fills a
        # forwarding face on every downstream cell (else a deleted-route / blank
        # cell wrongly shows a "routing cell" program).
        if block_name or route or prog_kind == "broker":
            self._show_program(cx, cy, sel.get("chip", 0))
        else:
            self._add_row("Program", "(empty cell)")
            self._hide_program()

    def _show_connection(self, conn_name) -> None:
        self._title.setText(f"Connection: {conn_name}")
        conn = self._project.connection(conn_name) if self._project else None
        if conn is None:
            return
        self._add_row("Source", _ep_label(conn.source))
        self._add_row("Target", _ep_label(conn.target))
        self._add_row(
            "Note",
            "Hop count and destination are set per WRITE/JUMP in the source "
            "cell's program — select that cell to edit them.")

    def _show_program(self, cx, cy, chip) -> None:
        self._prog_owner = None
        if self._controller is None or not isinstance(cx, int):
            return
        try:
            prog = self._controller.cell_program(chip, cx, cy)
        except Exception:  # noqa: BLE001 — build may fail; just skip the view
            prog = None
        if prog is None:
            self._add_row("Program", "(build to inspect)")
            self._hide_program()
            return
        routing = prog.get("routing_only")
        # A FABRIC routing cell = no owning block but a forwarding face: a plain
        # transit cell, OR a programmed broker/crossover/§1.4 universal transit
        # cell. These get the "Routing cell" banner even though they have a program.
        fabric_routing = prog.get("block") is None and prog.get("face") is not None
        if not routing:
            self._add_row("Entry addr", str(prog["entry"]))
        self._program.set_program(
            prog["entry"], prog["memory"], prog["disasm"],
            face=prog.get("face"), has_program=not routing,
            routing_only=bool(routing), classes=prog.get("classes"),
            fabric_routing=fabric_routing)
        # Tag the program view with this cell so a register dragged to the
        # waveform viewer carries the right (chip, x, y).
        self._program.set_cell(chip, cx, cy)
        # Per-instruction handoff editor — only for block cells.
        block_name = prog.get("block")
        cell_id = prog.get("cell_id")
        if block_name is not None and not routing:
            self._prog_owner = (block_name, cell_id)
            self._program.set_instructions(prog.get("instructions") or [])
        else:
            self._program.set_instructions([])
        if not self._external_program:
            self._program.setVisible(True)

    def _on_instr_changed(self, addr, field, value) -> None:
        """Relay a cell-view per-instruction edit to the controller (deferred).

        Deferred via a 0-timer for the same reason as the face combo: the edit
        triggers a rebuild → re-render → reselect → rebuild of this panel, which
        would delete the emitting widget mid-signal (use-after-free).
        """
        if self._prog_owner is None:
            return
        from PySide6.QtCore import QTimer

        block_name, cell_id = self._prog_owner
        QTimer.singleShot(0, lambda: self.instr_override_changed.emit(
            block_name, cell_id, int(addr), str(field), value))

    # -- helpers --------------------------------------------------------------

    def _add_row(self, label: str, value: str) -> None:
        w = QLabel(str(value))
        w.setWordWrap(True)
        self._form.addRow(f"{label}:", w)
        self._rows.append(w)

    def _add_verification_row(self, block) -> None:
        """A 'Verification' row flagging an unverified / proof-of-concept block
        (🧪) with an explanatory tooltip. Verified blocks show nothing (the
        default — no clutter)."""
        from engine.catalog import (VERIFY_VERIFIED, verify_badge, verify_note)

        spec = (self._controller.catalog.get(block.type, block.library)
                if self._controller else None)
        state = getattr(spec, "verification", VERIFY_VERIFIED) if spec else VERIFY_VERIFIED
        if state == VERIFY_VERIFIED:
            return
        note = verify_note(state)
        w = QLabel(f"{verify_badge(state)} {state}".strip())
        w.setWordWrap(True)
        w.setToolTip(note)
        self._form.addRow("Verification:", w)
        self._rows.append(w)

    def _add_name_editor(self, block_name: str) -> None:
        """Editable instance-NAME field (distinct from the read-only block type).
        Committing (Enter / focus-out) renames the instance + all its routes."""
        from PySide6.QtWidgets import QLineEdit

        edit = QLineEdit(block_name)
        edit.setToolTip("Instance name — rename this block (updates its routes). "
                        "Distinct from the block Type below.")
        edit.editingFinished.connect(lambda e=edit: self._on_name_edited(e))
        self._form.addRow("Name:", edit)
        self._rows.append(edit)
        self._name_edit = edit

    def _on_name_edited(self, edit) -> None:
        sel = self._sel
        old = sel.get("block") if sel else None
        if not old or self._project is None:
            return
        new = edit.text().strip()
        if not new or new == old:
            edit.setText(old)              # no-op / empty → revert
            return
        if self._project.block(new) is not None:
            edit.setText(old)             # duplicate → revert (handler also guards)
            edit.setToolTip("Name already in use — reverted")
            return
        # Defer (same use-after-free reason as params/face): the rename rebuilds
        # this panel, deleting the emitting line edit mid-signal.
        from PySide6.QtCore import QTimer

        QTimer.singleShot(0, lambda: self.block_renamed.emit(old, new))

    def _add_face_combo(self, current: str) -> None:
        combo = QComboBox()
        combo.addItems([f.value for f in Face])
        idx = combo.findText(current)
        if idx >= 0:
            combo.setCurrentIndex(idx)
        combo.currentTextChanged.connect(self._on_face_combo)
        self._form.addRow("Face:", combo)
        self._rows.append(combo)
        self._face_combo = combo

    def _add_params_editor(self, block) -> None:
        """Parameter fields for the selected block (§3.3).

        Only DATA-mapped params (those whose value lands in a cell data word) are
        editable — committing one (Enter / focus-out) emits ``params_changed``
        and the build re-resolves, updating the green data words. Topology /
        informational params (e.g. the DFE's ``forward_taps``, which would re-tile
        the block) are shown READ-ONLY: changing them needs structural support
        that's out of scope for now.
        """
        from PySide6.QtWidgets import QLineEdit

        spec = (self._controller.catalog.get(block.type, block.library)
                if self._controller else None)
        type_of = {p.name: p.type_name for p in (spec.params if spec else ())}
        editable = set()
        if self._controller is not None:
            try:
                editable = self._controller.catalog.editable_params(
                    block.type, block.params, library=block.library)
            except Exception:  # noqa: BLE001 — fall back to all read-only
                editable = set()
        self._param_edits = {}
        for key, val in block.params.items():
            if key in editable:
                edit = QLineEdit(_param_to_text(val))
                edit.setToolTip(f"{key} ({type_of.get(key) or 'value'}) — "
                                "maps to a cell data value")
                tname = type_of.get(key)
                edit.editingFinished.connect(
                    lambda k=key, e=edit, t=tname: self._on_param_edited(k, e, t))
                self._form.addRow(f"{key}:", edit)
                self._rows.append(edit)
                self._param_edits[key] = edit
            else:
                # Informational / topology param — read-only.
                lbl = QLabel(_param_to_text(val))
                lbl.setWordWrap(True)
                lbl.setToolTip(f"{key} is a structural/topology parameter — "
                               "changing it would re-tile the block (not editable "
                               "yet); informational only.")
                lbl.setEnabled(False)
                self._form.addRow(f"{key}:", lbl)
                self._rows.append(lbl)

    def _on_param_edited(self, key: str, edit, type_name) -> None:
        sel = self._sel
        block_name = sel.get("block") if sel else None
        if not block_name or self._project is None:
            return
        block = self._project.block(block_name)
        if block is None:
            return
        try:
            value = _parse_param(edit.text(), type_name, block.params.get(key))
        except (ValueError, TypeError):
            # Invalid input — revert the field to the current model value.
            edit.setText(_param_to_text(block.params.get(key)))
            if self._controller is not None:
                edit.setToolTip("Invalid value — reverted")
            return
        if value == block.params.get(key):
            return  # no change
        new_params = dict(block.params)
        new_params[key] = value
        # Defer (same reason as the face combo): committing rebuilds the panel,
        # which would delete this line edit mid-signal.
        from PySide6.QtCore import QTimer

        QTimer.singleShot(
            0, lambda: self.params_changed.emit(block_name, new_params))

    def _on_face_combo(self, value: str) -> None:
        sel = self._sel
        if not sel or value == sel.get("face"):
            return
        # Defer to the next event-loop turn — emitting synchronously runs the
        # command → model change → canvas re-render → reselect → show_selection()
        # → _clear_rows(), deleting this combo mid-signal (use-after-free crash).
        from PySide6.QtCore import QTimer

        if sel.get("block") and not sel.get("route"):
            block, cell_id = sel["block"], sel.get("cell_id")
            QTimer.singleShot(
                0, lambda: self.face_changed.emit(block, cell_id, value))

    def _clear_rows(self) -> None:
        # Remove all rows except the title row (row 0).
        while self._form.rowCount() > 1:
            self._form.removeRow(1)
        self._rows.clear()


def _param_to_text(val) -> str:
    """Render a parameter value for a line edit."""
    if isinstance(val, (list, tuple)):
        return ", ".join(_param_to_text(v) for v in val)
    if isinstance(val, float):
        # Trim trailing zeros but keep it a float-looking string.
        return repr(val)
    return str(val)


def _parse_param(text: str, type_name, current):
    """Parse line-edit text into a parameter value, guided by the declared type
    (falling back to the current value's Python type). Raises on bad input."""
    text = text.strip()
    is_list = (type_name in ("List", "list")
               or isinstance(current, (list, tuple))
               or ("," in text))
    if is_list:
        parts = [p.strip() for p in text.split(",") if p.strip() != ""]
        return [_parse_scalar(p) for p in parts]
    if type_name == "int" or isinstance(current, bool) is False and \
            isinstance(current, int) and not isinstance(current, bool):
        return int(text)
    if type_name == "float" or isinstance(current, float):
        return float(text)
    return _parse_scalar(text)


def _parse_scalar(text: str):
    """Parse a single scalar: int if it looks like one, else float, else str."""
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def _ep_label(ep) -> str:
    """Human label for a connection endpoint (block.port or chip port)."""
    from model.connection import BlockEndpoint, ChipPortEndpoint

    if isinstance(ep, BlockEndpoint):
        return f"{ep.block}.{ep.port}"
    if isinstance(ep, ChipPortEndpoint):
        return f"chip{ep.chip}.{ep.port}"
    return str(ep)
