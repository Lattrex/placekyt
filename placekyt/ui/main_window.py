"""MainWindow — the placeKYT top-level window (the architecture notes §3.1).

QMainWindow with the chip canvas as the central widget and dockable panels for
the block library, inspector, and console. The menu bar carries the
File/Edit/View/Block/Build/Simulation/Hardware/Help menus. Panel contents are
placeholders in this milestone — they are filled in as each panel lands; the
docking skeleton, menus, and status bar are real.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QDockWidget,
    QLabel,
    QMainWindow,
    QWidget,
)

from model.project import Project

from .canvas import ChipCanvas
from .controller import AppController
from .panels import ConsolePanel, InspectorPanel, LibraryPanel

MIN_WIDTH = 1200
MIN_HEIGHT = 800


class MainWindow(QMainWindow):
    """The application main window."""

    def __init__(self, controller: AppController | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("placeKYT")
        self.setMinimumSize(MIN_WIDTH, MIN_HEIGHT)
        self._apply_window_icon()

        # The controller owns the project + commands + catalog. Constructing one
        # eagerly builds the BlockCatalog; callers can inject a prebuilt one.
        self.controller = controller or AppController(parent=self)

        self.canvas = ChipCanvas(self)
        self.setCentralWidget(self.canvas)

        from .sim_controller import SimController

        self.sim = SimController(self.controller, self)

        self._docks: dict[str, QDockWidget] = {}
        self._create_docks()
        self._create_menus()
        self._create_sim_toolbar()
        self._create_status_bar()
        self._wire_signals()

        self._default_state = self.saveState()
        self._refresh_edit_actions()

    # -- docks ----------------------------------------------------------------

    def _create_docks(self) -> None:
        # Block Library (left): categorized, searchable, draggable (§3.4).
        self.library = LibraryPanel(self.controller.catalog)
        self._add_dock("Block Library", self.library, Qt.LeftDockWidgetArea)

        # Program (right): the 32-row memory/assembly table + per-instruction
        # Handoff editor for the selected cell — its OWN dock so it doesn't get
        # squeezed by the Inspector's cell-info + params (§3.3).
        from ui.widgets import CellProgramView

        self.program_view = CellProgramView()
        self.program_view.clear()
        program_dock = self._add_dock(
            "Program", self.program_view, Qt.RightDockWidgetArea)

        # Inspector (right): selection details + params. Drives the external
        # program view above. Stacked above Program in the right area.
        self.inspector = InspectorPanel(
            controller=self.controller, program_view=self.program_view)
        self.inspector.set_project(self.controller.project)
        inspector_dock = self._add_dock(
            "Inspector", self.inspector, Qt.RightDockWidgetArea)
        # Inspector on top, Program below it in the right dock column.
        self.splitDockWidget(inspector_dock, program_dock, Qt.Vertical)
        # When the Program dock is re-shown (un-tabbed, un-floated, raised),
        # re-pull the current canvas selection so it doesn't stay blanked.
        program_dock.visibilityChanged.connect(self._resync_program)

        # Output / Transactions (bottom): the captured output payload (default)
        # and the full ordered, timestamped transaction stream (detail toggle).
        # Folds in the old Output panel (DEBUG_ARCHITECTURE §3.1).
        from .panels.transaction_log_panel import TransactionLogPanel

        self.output_panel = TransactionLogPanel()
        output_dock = self._add_dock(
            "Output", self.output_panel, Qt.BottomDockWidgetArea)

        # Waveform viewer (bottom): GTKWave-style port-stream traces with the
        # shared time cursor (DEBUG_ARCHITECTURE §3.3).
        from .panels.waveform_panel import WaveformPanel

        self.waveform_panel = WaveformPanel()
        waveform_dock = self._add_dock(
            "Waveform", self.waveform_panel, Qt.BottomDockWidgetArea)

        # Breakpoints (bottom): list / add / remove (DEBUG §3.6).
        from .panels.breakpoint_panel import BreakpointPanel

        self.breakpoint_panel = BreakpointPanel(self.sim)
        breakpoint_dock = self._add_dock(
            "Breakpoints", self.breakpoint_panel, Qt.BottomDockWidgetArea)

        # Console (bottom): embedded Python REPL with the API namespace (§3.1).
        self.console = ConsolePanel(self._api_namespace())
        console_dock = self._add_dock("Console", self.console,
                                      Qt.BottomDockWidgetArea)

        # Disassembly (bottom): load a .kbs bitstream → mnemonic listing (#184).
        from .panels.disassembly_panel import DisassemblyPanel

        self.disassembly_panel = DisassemblyPanel()
        disasm_dock = self._add_dock(
            "Disassembly", self.disassembly_panel, Qt.BottomDockWidgetArea)

        # Tab Output + Waveform + Breakpoints + Console + Disassembly together.
        self.tabifyDockWidget(output_dock, waveform_dock)
        self.tabifyDockWidget(waveform_dock, breakpoint_dock)
        self.tabifyDockWidget(breakpoint_dock, console_dock)
        self.tabifyDockWidget(console_dock, disasm_dock)
        output_dock.raise_()

    def _api_namespace(self) -> dict:
        """The objects exposed in the embedded console (§3.1, §9.4 MCP-ready).

        Every console operation is the same API the GUI uses — scripting and
        the GUI share one source of truth (§4.1)."""
        return {
            "controller": self.controller,
            "project": self.controller.project,
            "catalog": self.controller.catalog,
            "registry": self.controller.registry,
            # convenience shims mirroring common menu actions
            "build": self.controller.build,
            "drc": self.controller.run_drc,
            "place": self.controller.place_block,
            "undo": self.controller.undo,
            "redo": self.controller.redo,
        }

    def _add_dock(self, title: str, widget: QWidget,
                  area: Qt.DockWidgetArea) -> QDockWidget:
        dock = QDockWidget(title, self)
        dock.setObjectName(f"dock_{title.replace(' ', '_').lower()}")
        dock.setWidget(widget)
        dock.setAllowedAreas(Qt.AllDockWidgetAreas)
        self.addDockWidget(area, dock)
        self._docks[title] = dock
        return dock

    # -- menus ----------------------------------------------------------------

    def _create_menus(self) -> None:
        mb = self.menuBar()
        # File
        m_file = mb.addMenu("&File")
        m_file.addAction(self._action("New Project", "Ctrl+N", self._new_project))
        m_file.addAction(self._action("Open Project…", "Ctrl+O", self._open_project))
        m_file.addAction(self._action("Import GNURadio Flowgraph…", None,
                                      self._import_grc))
        m_file.addAction(self._action("Save", "Ctrl+S", self._save))
        m_file.addAction(self._action("Save As…", "Ctrl+Shift+S", self._save_as))
        m_file.addSeparator()
        m_file.addAction(self._action("Quit", "Ctrl+Q", self.close))

        # Edit — Undo/Redo bound to the command manager.
        m_edit = mb.addMenu("&Edit")
        self.act_undo = self._action("Undo", "Ctrl+Z", self._undo)
        self.act_redo = self._action("Redo", "Ctrl+Y", self._redo)
        m_edit.addAction(self.act_undo)
        m_edit.addAction(self.act_redo)
        m_edit.addSeparator()
        m_edit.addAction(self._action("Preferences…", None, self._open_preferences))

        # View — panel toggles + reset layout.
        m_view = mb.addMenu("&View")
        for title, dock in self._docks.items():
            m_view.addAction(dock.toggleViewAction())
        m_view.addSeparator()
        # Auto-P&R P2.3: toggle labelled block-port stubs (the named-port markers
        # used to see/wire block I/O on the bus-facing edge).
        self.act_port_stubs = QAction("Show Block Port Stubs", self)
        self.act_port_stubs.setCheckable(True)
        self.act_port_stubs.toggled.connect(self.canvas.set_show_port_stubs)
        m_view.addAction(self.act_port_stubs)
        m_view.addSeparator()
        m_view.addAction(self._action("Reset Layout", None, self.reset_layout))
        m_view.addAction(self._action("Fit to View", "Ctrl+0",
                                      self.canvas.fit_to_view))

        # Block / Chip / Build / Simulation / Hardware.
        mb.addMenu("&Block")
        m_chip = mb.addMenu("&Chip")
        m_chip.addAction(self._action("Add Chip", None, self._add_chip))
        m_chip.addAction(self._action("Connect Chips…", None, self._connect_chips))
        m_chip.addSeparator()
        m_chip.addAction(self._action("Add SRAM Panel", None, self._add_panel))
        m_chip.addAction(self._action("Connect Panel…", None,
                                      self._connect_panel))
        m_build = mb.addMenu("Bu&ild")
        # Auto-P&R (Phase 3): flow-order the blocks (Auto-Place), then materialise
        # every logical net into a real route (Route All), then build.
        m_build.addAction(self._action("Auto-Place Blocks", "Ctrl+Shift+P",
                                       self._auto_place_blocks))
        m_build.addAction(self._action("Route All Nets", "Ctrl+R",
                                       self._route_all_nets))
        m_build.addSeparator()
        m_build.addAction(self._action("Generate Bitstream", "Ctrl+B",
                                       self._generate_bitstream))
        m_build.addAction(self._action("Check Design Rules", None, self._check_drc))
        m_build.addAction(self._action("Export Bitstream…", None,
                                       self._export_bitstream))
        m_build.addAction(self._action("Disassemble Built Bitstream", None,
                                       self._disassemble_built))
        # Simulation actions live on the sim toolbar (created after menus);
        # the menu references them there. Stimulus loading lives here.
        self._sim_menu = mb.addMenu("&Simulation")
        self._sim_menu.addAction(
            self._action("Load Stimulus…", None, self._load_stimulus))
        self._sim_menu.addAction(
            self._action("Clear Stimulus", None, self._clear_stimulus))
        self._sim_menu.addSeparator()
        self.act_gr_server = self._action(
            "Run as GNURadio Server", None, self._toggle_gnuradio_server)
        self.act_gr_server.setCheckable(True)
        self._sim_menu.addAction(self.act_gr_server)
        self._sim_menu.addAction(
            self._action("Live Window Size…", None, self._set_live_window))
        mb.addMenu("&Hardware")

        # Help
        m_help = mb.addMenu("&Help")
        m_help.addAction(self._action("About placeKYT", None, self._about))

    def _action(self, text: str, shortcut: str | None = None, slot=None) -> QAction:
        act = QAction(text, self)
        if shortcut:
            act.setShortcut(shortcut)
        if slot is not None:
            act.triggered.connect(slot)
        return act

    # -- simulation toolbar (§3.2) --------------------------------------------

    def _create_sim_toolbar(self) -> None:
        from PySide6.QtWidgets import QComboBox
        from PySide6.QtWidgets import QLabel as _QLabel
        from PySide6.QtWidgets import QSlider, QToolBar
        from .sim_controller import DEFAULT_SPEED, SPEED_BATCHES

        tb = QToolBar("Simulation", self)
        tb.setObjectName("sim_toolbar")
        self.addToolBar(tb)
        self.act_run = self._action("Run", "F5", self._run_simulation)
        self.act_step = self._action("Step", "F10", self._step_simulation)
        self.act_sim_reset = self._action("Reset Sim", None, self._reset_simulation)
        tb.addAction(self.act_run)
        tb.addAction(self.act_step)
        # Step granularity: one event / instruction / handshake.
        self.step_mode = QComboBox()
        self.step_mode.addItem("Event", "event")
        self.step_mode.addItem("Instruction", "instruction")
        self.step_mode.addItem("Handshake", "handshake")
        self.step_mode.setToolTip("What 'Step' advances by")
        tb.addWidget(self.step_mode)
        tb.addAction(self.act_sim_reset)
        tb.addSeparator()
        tb.addWidget(_QLabel(" Speed "))
        self.speed_slider = QSlider(Qt.Horizontal)
        self.speed_slider.setMinimum(0)
        self.speed_slider.setMaximum(len(SPEED_BATCHES) - 1)
        self.speed_slider.setValue(DEFAULT_SPEED)
        self.speed_slider.setFixedWidth(200)
        self.speed_slider.setTickPosition(QSlider.TicksBelow)
        self.speed_slider.setTickInterval(1)
        self.speed_slider.setPageStep(1)
        self.speed_slider.setToolTip(
            "Simulation speed — slide LEFT for slow-motion (watch individual "
            "transactions fire one at a time), RIGHT for fast.")
        self.speed_slider.valueChanged.connect(self.sim.set_speed_index)
        tb.addWidget(self.speed_slider)
        # The flash playback rate follows the speed so the slow end shows one
        # word at a time.
        self.sim.flash_rate.connect(self.canvas.set_flash_per_tick)
        # Apply the initial speed step (interval + flash rate) up front.
        self.sim.set_speed_index(DEFAULT_SPEED)
        # Mirror the run/step/reset actions into the Simulation menu.
        for act in (self.act_run, self.act_step, self.act_sim_reset):
            self._sim_menu.addAction(act)

        # Timeline scrubber (DEBUG §3.4): a thin full-width strip on its own row
        # under the sim toolbar — the always-visible global replay control. Drag
        # it to move the shared time cursor; all debug views snap to that time.
        from .widgets.timeline_scrubber import TimelineScrubber

        self.addToolBarBreak()
        tl = QToolBar("Timeline", self)
        tl.setObjectName("timeline_toolbar")
        tl.setMovable(False)
        tl.addWidget(_QLabel(" Timeline "))
        self.scrubber = TimelineScrubber()
        # Stretch the scrubber to fill the toolbar width.
        from PySide6.QtWidgets import QSizePolicy
        self.scrubber.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        tl.addWidget(self.scrubber)
        self.addToolBar(tl)

    # -- status bar -----------------------------------------------------------

    def _create_status_bar(self) -> None:
        from PySide6.QtWidgets import QPushButton

        sb = self.statusBar()
        # GRC out-of-sync indicator (§GRC-sync): hidden when in sync; when the
        # connected GNURadio flowgraph's params drift from the design it shows a
        # clickable "out of sync — click to resync" button. Leftmost permanent
        # widget so it's prominent.
        self._grc_sync_btn = QPushButton("GRC: out of sync — click to resync")
        self._grc_sync_btn.setFlat(True)
        self._grc_sync_btn.setStyleSheet(
            "QPushButton { color: white; background: #c0392b; border-radius: 3px;"
            " padding: 1px 6px; }")
        self._grc_sync_btn.setToolTip(
            "The connected GNURadio flowgraph's block parameters differ from this "
            "design. Click to re-apply them and re-place/re-route.")
        self._grc_sync_btn.clicked.connect(self._resync_from_grc)
        self._grc_sync_btn.hide()
        sb.addPermanentWidget(self._grc_sync_btn)

        self._status_canvas = QLabel("Canvas: Edit")
        self._status_hw = QLabel("HW: —")
        self._status_sim = QLabel("Sim: idle")
        for w in (self._status_canvas, self._status_hw, self._status_sim):
            sb.addPermanentWidget(w)
        sb.showMessage("Ready")

    # -- signal wiring --------------------------------------------------------

    def _wire_signals(self) -> None:
        # Canvas interactions → controller commands.
        self.canvas.block_dropped.connect(self._on_block_dropped)
        self.canvas.move_requested.connect(self._on_move_requested)
        self.canvas.selection_changed.connect(self._on_selection_changed)
        self.canvas.delete_requested.connect(self._on_delete_requested)
        self.canvas.set_face_requested.connect(self._on_set_face_requested)
        self.canvas.transform_requested.connect(self._on_transform_requested)
        self.inspector.face_changed.connect(self._on_set_face_requested)
        self.inspector.instr_override_changed.connect(self._on_instr_override)
        self.inspector.params_changed.connect(self._on_params_changed)
        self.inspector.block_renamed.connect(self._on_block_renamed)
        self.canvas.route_completed.connect(self._on_route_completed)
        self.canvas.route_progress.connect(self._on_route_progress)
        self.canvas.delete_connection_requested.connect(self._on_delete_connection)
        self.canvas.delete_inter_chip_requested.connect(self._on_delete_inter_chip)
        self.canvas.block_moved.connect(self._on_block_moved)
        self.canvas.block_moved_to_chip.connect(self._on_block_moved_to_chip)
        self.canvas.panel_moved.connect(self._on_panel_moved)
        self.canvas.panel_delete_requested.connect(self._on_panel_delete)
        self.canvas.panel_mirror_requested.connect(self._on_panel_mirror)
        self.canvas.panel_inspect_requested.connect(self._on_panel_inspect)
        # SRAM panel activity → blink the panel item + refresh open inspectors.
        self.sim.panel_activity.connect(self._on_panel_activity)
        self._sram_inspectors: dict = {}   # panel_id → SramInspectorPanel
        self.canvas.cell_moved.connect(self._on_cell_moved)
        self.canvas.footprint_provider = self._block_footprint
        # Resolve a block port name → its cell_id (auto-P&R P2.3 flylines/stubs).
        self.canvas.port_cell_provider = self._block_port_cells
        # Click-to-wire a logical net between two block-port stubs (P2.3).
        self.canvas.logical_wire_requested.connect(self._on_logical_wire)
        # Any model change → re-render canvas + refresh edit actions.
        self.controller.changed.connect(self._on_model_changed)
        # Simulation → overlay + status.
        self.sim.cell_states.connect(self.canvas.apply_cell_states)
        self.sim.cell_faces.connect(self.canvas.apply_cell_faces)
        self.sim.handshakes.connect(self.canvas.apply_handshakes)
        # Auto-load the running stimulus into the Disassembly panel (#195) +
        # live-highlight each injected word as it enters the chip (#196) +
        # stimulus-line breakpoints (#197).
        self.sim.stimulus_loaded.connect(self._on_stimulus_loaded)
        self.sim.injection_progress.connect(
            self.disassembly_panel.highlight_injected)
        self.disassembly_panel.breakpoint_toggled.connect(
            self._on_disasm_breakpoint_toggled)
        self.sim.injection_breakpoint_hit.connect(
            self._on_injection_breakpoint_hit)
        self.sim.state_changed.connect(self._on_sim_state)
        self.sim.metrics.connect(self._on_sim_metrics)
        self.sim.output.connect(self.output_panel.on_output)
        self.sim.trace_updated.connect(self.output_panel.set_trace_model)
        # Waveform viewer (DEBUG §3.3): rebuild streams on each trace update;
        # left-click on the wave drives the shared cursor.
        self.sim.trace_updated.connect(self.waveform_panel.set_trace_model)
        self.waveform_panel.cursor_requested.connect(self._on_cursor_requested)
        # Let the waveform viewer fetch a register's INITIAL (programmed/reset)
        # value so a register trace shows its value before the first write.
        self.waveform_panel.set_initial_register_fetch(
            self._initial_register_value)
        # Name a dragged port's channels/tags by the logical net that uses them,
        # so the channel picker shows 'xi'/'xq'/… instead of bare tag numbers.
        self.waveform_panel.set_port_tag_namer(self._port_tag_name)
        # Resolve a dragged ROUTE to the data channels flowing through it.
        self.waveform_panel.set_route_channel_provider(self._route_channels)
        # Timeline scrubber (DEBUG §3.4): rebuild span + markers on each trace
        # update; dragging it drives the shared cursor.
        self.sim.trace_updated.connect(self.scrubber.set_from_trace_model)
        self.scrubber.cursor_requested.connect(self._on_cursor_requested)
        # Breakpoints (DEBUG §3.6): canvas right-click / program-pane click add
        # them; the panel is the master list; a hit pauses + parks the cursor.
        self.canvas.breakpoint_requested.connect(self._on_breakpoint_requested)
        self.canvas.block_color_requested.connect(self._on_block_color_requested)
        self.program_view.breakpoint_toggled.connect(
            self._on_program_breakpoint_toggled)
        self.breakpoint_panel.changed.connect(self._on_breakpoints_changed)
        self.sim.breakpoint_hit.connect(self._on_breakpoint_hit)
        # GNURadio bridge server: refresh the debug views as the remote run
        # advances the chip (queued connection → runs on the GUI thread).
        from PySide6.QtCore import Qt as _Qt
        self.sim.server_activity.connect(
            self._on_server_activity, _Qt.QueuedConnection)
        # Per-batch simKYT throughput (samples/sec on this machine) → status bar, so
        # the user can gauge how long a given burst will take (the sim is not
        # real-time). Queued: emitted from the server thread.
        self.sim.server_throughput.connect(
            self._on_server_throughput, _Qt.QueuedConnection)
        # The server rebuilt + re-hosted the chip (the design was edited since the
        # run started) → FULL-render the canvas on the GUI thread so the displayed
        # cells match the freshly-built chip (clears "phantom" routing cells from a
        # route edited since the server started). Queued: emitted on the server thread.
        self.sim.chip_rehosted.connect(
            self._on_model_changed, _Qt.QueuedConnection)
        # GRC↔placeKYT parameter sync (§GRC-sync): a GRC client advertised its
        # flowgraph params (queued from the server thread) → re-diff against the
        # design and update the indicator (auto-resync if the preference says so).
        self.sim.grc_params_received.connect(
            self._on_grc_params_received, _Qt.QueuedConnection)
        # The controller flips the out-of-sync state → update the status-bar
        # indicator.
        self.controller.grc_sync_changed.connect(self._on_grc_sync_changed)
        # Cell Inspector live mode (DEBUG §3.2): after each step/stop or cursor
        # move, refresh the selected cell's PC + live registers in the program
        # view, and move the waveform + scrubber playheads to the shared cursor.
        self.sim.cell_state_refreshed.connect(self._refresh_live_state)
        self.output_panel.cursor_requested.connect(self._on_cursor_requested)

    def _block_footprint(self, block_type, library):
        """Cell offsets for a library block's default placement — its tuned
        ``default_layout`` (e.g. the DFE serpentine), matching the actual cells
        controller.default_cells will create."""
        layout = self.controller.catalog.default_layout(block_type, library=library)
        if layout:
            # Normalize to (0,0) — must match controller.default_cells so the
            # preview lands where the block actually places.
            offs = [(dx, dy) for (dx, dy, _f) in layout.values()]
            min_dx = min(dx for dx, _ in offs)
            min_dy = min(dy for _, dy in offs)
            return [(dx - min_dx, dy - min_dy) for dx, dy in offs]
        return [(0, 0)]

    def _block_port_cells(self, block_type, library, params=None):
        """``{port_name: (cell_id, direction)}`` for a block's external ports —
        from the PortMap (auto-P&R P2.3). Lets the canvas anchor a logical net's
        flyline + a port stub at the right cell, and mark the input/output I/O
        cells. Resolved WITH the block instance's ``params``: a scaling block's
        program size — and therefore which cells are its input vs output —
        depends on its params (a multi-cell FIR has input on cell 0, output on
        its last cell; the param-less default is the 1-tap case where they
        collapse to one cell). Cached per (type, library, params)."""
        cache = getattr(self, "_port_cell_cache", None)
        if cache is None:
            cache = self._port_cell_cache = {}
        # params dict → a hashable, order-independent cache key.
        pkey = tuple(sorted((str(k), str(v)) for k, v in (params or {}).items()))
        key = (block_type, library, pkey)
        if key not in cache:
            out = {}
            try:
                pm = self.controller.catalog.port_map(
                    block_type, params, library=library)
                for p in pm.ports:
                    out[p.name] = (p.cell_id, p.direction)
            except Exception:  # noqa: BLE001 — no PortMap ⇒ canvas falls back
                out = {}
            cache[key] = out
        return cache[key]

    def _on_logical_wire(self, src_block, src_port, dst_block, dst_port) -> None:
        """Create an unrouted logical net from a producer block port to a consumer
        block port (auto-P&R P2.3 click-to-wire). The Phase-3 router materialises
        the physical route later; until then it shows as a fly line."""
        from model.connection import BlockEndpoint

        try:
            self.controller.add_logical_connection(
                BlockEndpoint(block=src_block, port=src_port),
                BlockEndpoint(block=dst_block, port=dst_port),
            )
            self.statusBar().showMessage(
                f"Wired {src_block}.{src_port} → {dst_block}.{dst_port}", 3000)
        except Exception as exc:  # noqa: BLE001
            self.statusBar().showMessage(f"Wire failed: {exc}", 5000)

    def _auto_place_blocks(self) -> None:
        """Flow-order the placed blocks into a 1-D pipeline (auto-P&R §8). One
        undoable step; re-renders. A backward (ring-forcing) edge is reported by
        name in the status bar (sound — nothing hidden)."""
        try:
            plan = self.controller.auto_place()
        except Exception as exc:  # noqa: BLE001
            self._error("Auto-Place", f"Auto-place failed: {exc}")
            return
        self.canvas.render_scene()
        if plan.ok:
            self.statusBar().showMessage(
                f"Placed {len(plan.order)} block(s) in flow order.", 4000)
        else:
            edges = ", ".join(f"{s}→{t}" for s, t in plan.backward_edges)
            self.statusBar().showMessage(
                f"Placed {len(plan.order)} block(s); backward edge(s) need a "
                f"ring: {edges}", 6000)

    def _route_all_nets(self) -> None:
        """Auto-route every unrouted logical net (auto-P&R "Route All"). Applies
        the routes as one undoable step. Success is reported in the status bar;
        unroutable nets (sound failure — named, never fabricated) pop a warning
        dialog listing them so the user knows exactly what to fix."""
        unrouted = [c for c in self.controller.project.connections
                    if not c.is_routed]
        if not unrouted:
            self.statusBar().showMessage("No unrouted nets to route.", 3000)
            return
        try:
            report = self.controller.auto_route_all()
        except Exception as exc:  # noqa: BLE001
            self._error("Route All", f"Auto-route failed: {exc}")
            return
        # Re-render so the routed nets show as solid routes (the command's
        # change signal also triggers this, but render explicitly to be sure the
        # fly lines are replaced immediately).
        self.canvas.render_scene()
        n_ok = len(report.routed)
        if report.ok:
            self.statusBar().showMessage(f"Routed {n_ok} net(s).", 4000)
            return
        self.statusBar().showMessage(
            f"Routed {n_ok} net(s); {len(report.failed)} could not be routed.",
            6000)
        # Only the failure case is important enough for a modal — name the nets.
        from PySide6.QtWidgets import QMessageBox

        lines = "\n".join(f"  • {r.name}: {r.reason}" for r in report.failed)
        box = QMessageBox(self)
        box.setWindowTitle("Route All")
        box.setIcon(QMessageBox.Warning)
        box.setText(f"Routed {n_ok} net(s); "
                    f"{len(report.failed)} could not be routed.")
        box.setDetailedText(lines)
        box.exec()

    def _on_block_dropped(self, block_type, library, chip_id, cx, cy) -> None:
        try:
            self.controller.place_block(block_type, chip_id, cx, cy, library=library)
            self.statusBar().showMessage(
                f"Placed {block_type} at chip {chip_id} ({cx},{cy})", 3000)
        except Exception as exc:  # noqa: BLE001 — surface, never crash on a drop
            self.statusBar().showMessage(f"Could not place {block_type}: {exc}", 5000)

    def _on_move_requested(self, dx, dy) -> None:
        cell = self.canvas.selected_cell()
        if cell is None or not cell.label:
            return
        try:
            self.controller.move_block(cell.label, dx, dy)
        except Exception as exc:  # noqa: BLE001
            self.statusBar().showMessage(f"Move failed: {exc}", 4000)

    def _on_delete_requested(self, block_name) -> None:
        try:
            self.controller.remove_block(block_name)
            self.statusBar().showMessage(f"Deleted {block_name}", 3000)
        except Exception as exc:  # noqa: BLE001
            self.statusBar().showMessage(f"Delete failed: {exc}", 4000)

    def _on_set_face_requested(self, block_name, cell_id, face_value) -> None:
        from model.enums import Face

        try:
            self.controller.set_cell_face(block_name, cell_id, Face.from_str(face_value))
        except Exception as exc:  # noqa: BLE001
            self.statusBar().showMessage(f"Set face failed: {exc}", 4000)

    def _on_transform_requested(self, block_name, kind) -> None:
        names = {"cw": "Rotated", "ccw": "Rotated", "mirror_h": "Mirrored",
                 "mirror_v": "Mirrored"}
        try:
            self.controller.transform_block(block_name, kind)
            self.statusBar().showMessage(
                f"{names.get(kind, 'Transformed')} {block_name} "
                f"(routes cleared — re-route as needed)", 4000)
        except Exception as exc:  # noqa: BLE001
            self.statusBar().showMessage(f"Transform failed: {exc}", 4000)

    def _on_route_completed(self, source, target, points) -> None:
        from model.connection import BlockEndpoint, ChipPortEndpoint

        def _resolve_block_port(block_name, *, want_out):
            """The block's REAL port name on the given side. A block's I/O ports
            carry meaningful names (Costas out=yi_tap, Gardner in=xi, MF in=xi/xq) —
            NOT the generic 'out'/'in'. Drawing a route must reuse those names so it
            (a) RECONNECTS an existing logical net between the same endpoints (frozen
            endpoints compare by value: BlockEndpoint(b,'xi') != BlockEndpoint(b,'in'),
            so a generic name would never match and would spawn a DUPLICATE, leaving
            the original net unrouted with a lingering fly line — the user-seen quirk),
            and (b) matches the imported net. Resolve via the PortMap; fall back to
            'out'/'in' only when no PortMap / no port on that side."""
            direction = "out" if want_out else "in"
            blk = self.controller.project.block(block_name)
            if blk is not None:
                try:
                    pm = self.controller.catalog.port_map(
                        blk.type, library=blk.library)
                    names = [p.name for p in pm.ports if p.direction == direction]
                except Exception:  # noqa: BLE001
                    names = []
                # Prefer an EXISTING connection's port on this side (exact reuse),
                # else the block's first port on that side.
                for c in self.controller.project.connections:
                    ep = c.source if want_out else c.target
                    if isinstance(ep, BlockEndpoint) and ep.block == block_name \
                            and ep.port in names:
                        return ep.port
                if names:
                    return names[0]
            return "out" if want_out else "in"

        def _endpoint(ep, *, want_out):
            """Build an endpoint from a route handle: a block name (str) → its
            REAL block port, or ('port', chip, name) → a chip port."""
            if isinstance(ep, tuple) and ep[0] == "port":
                return ChipPortEndpoint(ep[1], ep[2]), ep[2]
            port = _resolve_block_port(ep, want_out=want_out)
            return BlockEndpoint(ep, port), f"{ep}.{port}"

        source_ep, source_label = _endpoint(source, want_out=True)
        target_ep, target_label = _endpoint(target, want_out=False)
        try:
            self.controller.add_route(source_ep, target_ep, points)
            self.statusBar().showMessage(
                f"Routed {source_label} → {target_label} "
                f"({max(0, len(points) - 1)} hops)", 4000)
        except Exception as exc:  # noqa: BLE001
            self.statusBar().showMessage(f"Route failed: {exc}", 5000)

    def _on_block_moved(self, block_name, dx, dy) -> None:
        try:
            self.controller.move_block(block_name, dx, dy)
        except Exception as exc:  # noqa: BLE001
            self.statusBar().showMessage(f"Move failed: {exc}", 4000)

    def _on_block_moved_to_chip(self, block_name, chip, ax, ay) -> None:
        try:
            self.controller.move_block_to_chip(block_name, chip, ax, ay)
            self.statusBar().showMessage(
                f"Moved {block_name} to chip {chip}", 3000)
        except Exception as exc:  # noqa: BLE001
            self.statusBar().showMessage(f"Cross-chip move failed: {exc}", 4000)

    def _on_cell_moved(self, block_name, cell_id, x, y) -> None:
        try:
            self.controller.move_cell(block_name, cell_id, x, y)
        except Exception as exc:  # noqa: BLE001
            self.statusBar().showMessage(f"Cell move failed: {exc}", 4000)

    def _on_selection_changed(self, sel) -> None:
        # Guard against a SPURIOUS clear: some dock interactions make the scene
        # briefly emit selection_changed(None) while the cell is still selected.
        # If the canvas still has a real selection, keep showing it.
        if sel is None:
            still = self.canvas.current_selection()
            if still is not None:
                sel = still
        self.inspector.show_selection(sel)
        # If a sim has run, immediately show the newly-selected cell's live PC +
        # registers (show_selection reset the program view to the static words).
        self.inspector.update_live_state(self.sim)
        # Show this cell's PC breakpoints in the Program-pane gutter.
        self._refresh_program_breakpoints()
        # Sync the Transaction Log cell filter to the canvas selection (only in
        # detail mode, so it isn't intrusive): a selected cell filters the log to
        # it (and its chip); clicking off the array resets the filter to all.
        if self.output_panel._detail.isChecked():
            if sel is not None and isinstance(sel.get("cell"), tuple):
                cx, cy = sel["cell"]
                self.output_panel.filter_to_cell(sel.get("chip", 0) or 0, cx, cy)
            else:
                self.output_panel.clear_cell_filter()

    def _resync_program(self, visible) -> None:
        """Re-pull the current canvas selection into the Inspector/Program view
        when the Program dock becomes visible (so it isn't left blank after the
        dock was tabbed away / floated / re-docked)."""
        if visible:
            self.inspector.show_selection(self.canvas.current_selection())

    def _on_params_changed(self, block_name, params) -> None:
        try:
            self.controller.edit_params(block_name, params)
            self.statusBar().showMessage(f"{block_name}: parameters updated", 3000)
        except Exception as exc:  # noqa: BLE001
            self.statusBar().showMessage(f"Param edit failed: {exc}", 4000)

    def _on_block_renamed(self, old_name, new_name) -> None:
        try:
            self.controller.rename_block(old_name, new_name)
            self.statusBar().showMessage(
                f"Renamed {old_name} → {new_name}", 3000)
        except Exception as exc:  # noqa: BLE001
            self.statusBar().showMessage(f"Rename failed: {exc}", 4000)

    def _on_instr_override(self, block_name, cell_id, addr, field, value) -> None:
        try:
            self.controller.set_instr_override(
                block_name, cell_id, addr, **{field: value})
            shown = f"@{value}" if field == "hop" and value is not None else value
            self.statusBar().showMessage(
                f"{block_name}[{cell_id}] R{addr} {field}: "
                f"{shown if value is not None else 'auto'}", 3000)
        except Exception as exc:  # noqa: BLE001
            self.statusBar().showMessage(f"Instruction override failed: {exc}", 4000)

    def _on_delete_connection(self, name) -> None:
        # Smart route delete (#267): break ONLY this connection's physical path,
        # keeping the logical link as a fly line. Sole-occupant transit cells
        # vanish with the route; cells shared with another routed connection (a
        # multiplexed bus) stay. Falls back to a plain removal if the connection
        # isn't routed (nothing physical to break).
        try:
            conn = self.controller.project.connection(name)
            if conn is not None and conn.is_routed:
                cmd = self.controller.delete_route(name)
                if cmd.shared:
                    self.statusBar().showMessage(
                        f"Broke route {name} (shared bus kept) — now a fly line",
                        4000)
                else:
                    self.statusBar().showMessage(
                        f"Deleted route {name} "
                        f"({len(cmd.removed_cells)} routing cells removed)", 3000)
            else:
                self.controller.remove_connection(name)
                self.statusBar().showMessage(f"Deleted connection {name}", 3000)
        except Exception as exc:  # noqa: BLE001
            self.statusBar().showMessage(f"Delete route failed: {exc}", 4000)

    def _on_cursor_requested(self, ns) -> None:
        # The shared debug time cursor (DEBUG_ARCHITECTURE §0.2): a Transaction-
        # Log row click OR a waveform left-click moves it. Routing through the
        # SimController pulses the debug views (Cell Inspector PC/registers;
        # waveform playhead) so they re-render at the new time. We also highlight
        # the matching Transaction-Log row so a waveform click drives the log too
        # (the reverse direction of a log row-click moving the cursor).
        self.sim.set_cursor(float(ns))
        self.output_panel.highlight_cursor(float(ns))

    def _initial_register_value(self, chip, x, y, addr):
        """A cell register's INITIAL (programmed/reset) value, from the built
        program memory — or None if unavailable (the waveform viewer then shows
        an 'unknown' state). Mirrors what the Program panel's Value column shows.
        """
        try:
            prog = self.controller.cell_program(chip, x, y)
        except Exception:  # noqa: BLE001 — build may fail; treat as unknown
            return None
        if not prog:
            return None
        mem = prog.get("memory")
        if mem is None or not (0 <= addr < len(mem)):
            return None
        return int(mem[addr]) & 0xFFFF

    def _route_channels(self, connection_name):
        """``[(label, source_dict)]`` of the data channels flowing through a
        route, for the waveform route-drop. A route delivers to its target: a chip
        OUTPUT port → that port's tagged streams; a block input → the data-arrival
        stream(s) at the target block's input cell (one per input register). Lets
        the user grab a route and see a block's input/output without hunting for
        the cell."""
        try:
            from model.connection import ChipPortEndpoint, BlockEndpoint
            from engine.bus_router import _target_input_cell
            proj = self.controller.project
            conn = proj.connection(connection_name)
            if conn is None:
                return []
            tgt = conn.target
            if isinstance(tgt, ChipPortEndpoint):
                # Offer the port's captured tag streams (same as a port drag).
                model = self.sim.trace_model
                out = []
                for tag in model.port_tags(tgt.chip, tgt.port):
                    lbl = (f"{connection_name}: chip{tgt.chip}.{tgt.port} · "
                           f"{self._port_tag_name(tgt.chip, tgt.port, tag) or 'all'}")
                    out.append((lbl, {"type": "port_tag", "chip": tgt.chip,
                                      "port": tgt.port, "tag": tag}))
                return out
            if isinstance(tgt, BlockEndpoint):
                tb = proj.block(tgt.block)
                if tb is None or tb.placement is None or not tb.placement.cells:
                    return []
                ic = _target_input_cell(tb, tgt.port, self.controller.catalog)
                if ic is None:
                    return []
                chip = tb.placement.chip
                _entry, in_regs = self.controller.catalog.resolved_io(
                    tb.type, tb.params, library=tb.library)
                regs = list(in_regs) if in_regs else [0]
                out = []
                for r in regs:
                    lbl = f"{connection_name} → {tgt.block}.{tgt.port} (R{r})"
                    out.append((lbl, {"type": "register", "chip": chip,
                                      "x": ic[0], "y": ic[1], "addr": r}))
                return out
        except Exception:  # noqa: BLE001
            return []
        return []

    def _port_tag_name(self, chip, port, tag):
        """Logical name for one channel/tag of a multiplexed port, for the
        waveform channel picker. Looks at the project's connections touching this
        port and returns the block-port name of the matching net (e.g. 'xi'/'xq'
        for an input port, the producer port for an output). Returns None when it
        can't attribute the tag — the picker then shows the bare tag number."""
        try:
            from model.connection import ChipPortEndpoint, BlockEndpoint
            nets = []
            for c in self.controller.project.connections:
                for ep, other in ((c.source, c.target), (c.target, c.source)):
                    if (isinstance(ep, ChipPortEndpoint) and ep.chip == chip
                            and ep.port == port):
                        if isinstance(other, BlockEndpoint):
                            nets.append(f"{other.block}.{other.port}")
                        else:
                            nets.append(c.name)
            if not nets:
                return None
            # A single net on the port → name it regardless of tag. Multiple nets
            # (a genuinely multiplexed port) → only name confidently when the tag
            # indexes them in order (0,1,…); else leave to the bare tag number.
            if len(nets) == 1:
                return nets[0]
            if isinstance(tag, int) and 0 <= tag < len(nets):
                return nets[tag]
            return None
        except Exception:  # noqa: BLE001
            return None

    # -- breakpoints (DEBUG §3.6) ---------------------------------------------

    def _on_block_color_requested(self, block_name, color) -> None:
        """Set (or reset) a block's canvas colour from the cell context menu."""
        self.controller.set_block_color(block_name, color)

    def _on_breakpoint_requested(self, chip, x, y, kind, value) -> None:
        """A canvas right-click requested a breakpoint — add it via the panel."""
        self.breakpoint_panel.add_breakpoint(chip, x, y, kind, value)
        self.statusBar().showMessage(
            f"Breakpoint added: c{chip}:({x},{y}) "
            f"{'PC==' + str(value) if kind == 'pc' else 'arrival@' + str(value)}",
            3000)

    def _on_program_breakpoint_toggled(self, addr) -> None:
        """A Program-pane row click toggled a PC breakpoint at ``addr`` for the
        currently-selected cell."""
        sel = self.canvas.current_selection()
        cell = sel.get("cell") if sel else None
        if not isinstance(cell, tuple) or not isinstance(cell[0], int):
            return
        chip = sel.get("chip", 0) or 0
        x, y = cell
        from engine.breakpoints import BP_PC
        existing = self.sim.breakpoints.find(chip, x, y, BP_PC, int(addr))
        if existing is not None:
            self.breakpoint_panel.remove_breakpoint(existing)
        else:
            self.breakpoint_panel.add_breakpoint(chip, x, y, BP_PC, int(addr))

    def _on_breakpoints_changed(self) -> None:
        """The breakpoint set changed — refresh the canvas marks, the scrubber
        breakpoint markers, and the Program-pane gutter."""
        self.canvas.apply_breakpoints(self.sim.breakpoints)
        self._refresh_program_breakpoints()
        self._refresh_scrubber_breakpoints()

    def _refresh_program_breakpoints(self) -> None:
        """Tell the Program pane which addresses are breakpointed on the selected
        cell so it can show the gutter markers."""
        sel = self.canvas.current_selection()
        cell = sel.get("cell") if sel else None
        if not isinstance(cell, tuple) or not isinstance(cell[0], int):
            self.program_view.set_breakpoint_addrs(set())
            return
        from engine.breakpoints import BP_PC
        chip = sel.get("chip", 0) or 0
        x, y = cell
        addrs = {bp.value for bp in self.sim.breakpoints.breakpoints
                 if bp.cell == (chip, x, y) and bp.kind == BP_PC}
        self.program_view.set_breakpoint_addrs(addrs)

    def _refresh_scrubber_breakpoints(self) -> None:
        """Re-emit the scrubber markers with breakpoint-hit markers merged in."""
        # set_from_trace_model rebuilds I/O markers; append the hit markers.
        self.scrubber.set_from_trace_model(self.sim.trace_model)
        hits = [(t, "bp") for t in self.sim.breakpoint_hit_times()]
        if hits:
            self.scrubber.set_markers(self.scrubber._markers + hits)

    def _on_breakpoint_hit(self, hit) -> None:
        """A breakpoint fired and paused the run — surface it."""
        self.statusBar().showMessage(
            f"Breakpoint hit: {hit.bp.label()} at {hit.time_ns:.1f} ns", 6000)
        self._refresh_scrubber_breakpoints()
        # Select the hit cell so the Inspector shows its state at the hit.
        self.breakpoint_panel.raise_()

    def _toggle_gnuradio_server(self, checked: bool) -> None:
        """Start/stop hosting the current design's chip for a GNURadio flowgraph
        (the live IPC bridge). When running, GRC streams samples through the
        chip and these debug views update live."""
        if checked:
            # Fixed default port so the bundled live .grc flowgraph
            # (server_port=58950) connects without reconfiguration. Falls back to
            # an OS-assigned port if 58950 is busy.
            try:
                bound = self.sim.start_gnuradio_server(port=58950)
            except OSError:
                bound = self.sim.start_gnuradio_server(port=0)
            if bound is None:
                self.act_gr_server.setChecked(False)
                self.statusBar().showMessage(
                    "GNURadio server failed to start (build errors?)", 5000)
                return
            self.statusBar().showMessage(
                f"GNURadio server listening on 127.0.0.1:{bound} — point a "
                f"placeKYT Sim Client at this port.", 0)
        else:
            self.sim.stop_gnuradio_server()
            self.statusBar().showMessage("GNURadio server stopped", 3000)

    def _on_server_activity(self, full_capture: bool = False) -> None:
        """The GNURadio server advanced the chip — refresh the live debug views
        (canvas overlay, handshakes, transaction log/waveform/scrubber). A one-shot
        BATCH (``full_capture``) keeps the WHOLE burst trace so start AND end
        conditions are visible, instead of the rolling streaming window."""
        self.sim.refresh_debug_from_chip(full_capture=full_capture)

    def _on_server_throughput(self, info) -> None:
        """A GNURadio batch finished — show simKYT's sample throughput on THIS
        machine in the status bar so the user can estimate run times (the sim is
        event-accurate, NOT real-time)."""
        try:
            rate = float(info.get("samples_per_sec", 0.0))
            n = int(info.get("samples", 0))
            secs = float(info.get("seconds", 0.0))
        except Exception:  # noqa: BLE001
            return
        # A friendly real-time-factor hint at a common audio rate.
        rt48 = (48000.0 / rate) if rate > 0 else float("inf")
        self.statusBar().showMessage(
            f"simKYT: {n} samples in {secs*1000:.0f} ms = {rate:,.0f} samples/s "
            f"(≈{rt48:.0f}× slower than 48 kHz real-time)", 15000)

    # -- preferences + GRC parameter sync (§GRC-sync) -------------------------

    def _open_preferences(self) -> None:
        """Open the Preferences dialog (QSettings-persisted)."""
        from .preferences_dialog import PreferencesDialog

        PreferencesDialog(self).exec()

    def _on_grc_sync_changed(self, diffs) -> None:
        """The GRC out-of-sync set changed → show/hide the status-bar indicator."""
        n = len(diffs or {})
        if n:
            self._grc_sync_btn.setText(
                f"GRC: {n} block(s) out of sync — click to resync")
            self._grc_sync_btn.show()
        else:
            self._grc_sync_btn.hide()

    def _watch_grc_source(self, path) -> None:
        """Watch the imported .grc for re-saves so drift is flagged on SAVE (no
        run needed). One watcher, re-pointed on each import."""
        from PySide6.QtCore import QFileSystemWatcher

        w = getattr(self, "_grc_watcher", None)
        if w is None:
            w = self._grc_watcher = QFileSystemWatcher(self)
            w.fileChanged.connect(self._on_grc_file_changed)
        if w.files():
            w.removePaths(w.files())
        if path:
            w.addPath(str(path))

    def _on_grc_file_changed(self, path) -> None:
        """The watched .grc was re-saved → re-diff its params vs the placed design
        and update the out-of-sync indicator (detect-on-save). Many editors
        replace the file on save (atomic write), which drops the watch — re-add
        the path so subsequent saves keep firing."""
        from PySide6.QtCore import QTimer

        w = getattr(self, "_grc_watcher", None)
        # A tiny delay lets an atomic-replace save settle before we read + re-watch.
        def _recheck():
            if w is not None and path and path not in w.files():
                import os
                if os.path.exists(path):
                    w.addPath(path)
            diffs = self.controller.check_grc_file_drift()
            if diffs is not None:
                self._on_grc_sync_changed(diffs)
        QTimer.singleShot(150, _recheck)

    def _on_grc_params_received(self, params_by_block) -> None:
        """A GRC client advertised its flowgraph's block params. Re-diff against
        the design; then branch on the persisted preference: NOTIFY shows the
        indicator (handled by ``_on_grc_sync_changed``), AUTO resyncs seamlessly,
        RE-ANCHOR resizes in place + surfaces DRC."""
        from engine import preferences

        diffs = self.controller.observe_grc_params(params_by_block or {})
        if not diffs:
            return
        mode = preferences.grc_param_change_mode()
        if mode == preferences.GRC_NOTIFY:
            self.statusBar().showMessage(
                f"GRC parameters changed in {len(diffs)} block(s) — "
                "click the indicator to resync.", 6000)
            return
        # AUTO or RE-ANCHOR: act immediately.
        self._resync_from_grc(mode=mode)

    def _resync_from_grc(self, *, mode: str | None = None) -> None:
        """Apply the recorded GRC params to the out-of-sync blocks and re-layout
        (the indicator-click action, and the auto/re-anchor handler). Surfaces
        any resulting DRC violations rather than silently proceeding."""
        try:
            affected, report = self.controller.resync_from_grc(
                mode=mode, chip_types=self.controller.chip_types())
        except Exception as exc:  # noqa: BLE001
            self.statusBar().showMessage(f"GRC resync failed: {exc}", 6000)
            return
        self._on_model_changed()
        if not affected:
            self.statusBar().showMessage("Design already in sync with GRC.", 3000)
            return
        n = len(affected)
        # Route failures (auto/notify) → surface; re-anchor → run DRC + surface.
        unrouted = list(getattr(report, "failed", []) or []) if report else []
        if unrouted:
            names = ", ".join(getattr(r, "name", "?") for r in unrouted)
            self._surface_drc(
                f"Resynced {n} block(s) from GRC, but {len(unrouted)} net(s) "
                f"could not route: {names}")
            return
        if report is None:
            # Re-anchor mode: did not reroute — run DRC to surface any violations
            # the resize introduced.
            drc = self.controller.run_drc()
            if not drc.ok:
                self._surface_drc(
                    f"Resynced {n} block(s) in place; "
                    f"{len(drc.errors)} DRC violation(s) — see Build → Check "
                    "Design Rules.")
                return
        self.statusBar().showMessage(
            f"Resynced {n} block(s) from GRC.", 4000)

    def _surface_drc(self, message: str) -> None:
        """Surface a resync/DRC problem to the user (non-fatal warning)."""
        from PySide6.QtWidgets import QMessageBox

        self.statusBar().showMessage(message, 8000)
        QMessageBox.warning(self, "GRC Resync", message)

    def _set_live_window(self) -> None:
        """Prompt for the live trace-window size (events kept in the rolling
        debug view during GNURadio streaming)."""
        from PySide6.QtWidgets import QInputDialog

        n, ok = QInputDialog.getInt(
            self, "Live Window Size",
            "Events kept in the live debug window (larger = more scrollback, "
            "slower refresh):", self.sim.live_window, 500, 200000, 1000)
        if ok:
            self.sim.set_live_window(n)
            self.statusBar().showMessage(
                f"Live window: {n} events", 3000)

    def _refresh_live_state(self) -> None:
        """Re-pull the selected cell's live PC + registers into the Inspector
        (DEBUG §3.2) and move the waveform + scrubber playheads to the shared
        cursor. Fired on each sim step/stop and on cursor moves."""
        self.inspector.update_live_state(self.sim)
        # With no trace (e.g. just after reset) there is no cursor to show — pass
        # None so the wave/scrubber playheads clear rather than parking at 0.
        ns = (self.sim.trace_model.cursor_ns
              if self.sim.trace_model.transactions else None)
        self.waveform_panel.set_cursor(ns)
        self.scrubber.set_cursor(ns)

    def _on_delete_inter_chip(self, ic) -> None:
        try:
            self.controller.remove_inter_chip(ic)
            self.statusBar().showMessage(
                f"Deleted inter-chip wire {ic.from_chip}.{ic.from_port} → "
                f"{ic.to_chip}.{ic.to_port}", 3000)
        except Exception as exc:  # noqa: BLE001
            self.statusBar().showMessage(f"Delete wire failed: {exc}", 4000)

    def _on_route_progress(self, hops, overflow) -> None:
        if hops == 0 and self.canvas.tool.value == "select":
            self.statusBar().clearMessage()
            return
        warn = "  ⚠ OVERFLOW" if overflow else ""
        self.statusBar().showMessage(f"Routing: {hops}/31 hops{warn}")

    def _apply_window_icon(self) -> None:
        """Set the Lattrex logo as the window icon (#136). Best-effort."""
        from pathlib import Path

        from PySide6.QtGui import QIcon

        icon = (Path(__file__).resolve().parent.parent / "resources" / "icons"
                / "lattrex_logo.png")
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))

    def _on_model_changed(self) -> None:
        self.canvas.render_scene()
        # Re-render recreates the cell items — re-apply the breakpoint marks and
        # sync each block's arrow to its build-resolved output face (#135).
        self.canvas.apply_breakpoints(self.sim.breakpoints)
        self._sync_resolved_faces()
        self._refresh_edit_actions()
        self._update_title()

    def _sync_resolved_faces(self) -> None:
        """Point each cell's arrow at its build-resolved output face (#135).
        Uses the cached build if available; a stale/absent build leaves the
        placement-default arrows (they'll sync on the next build)."""
        try:
            build = self.controller.cached_build()
        except Exception:  # noqa: BLE001
            build = None
        self.canvas.apply_resolved_faces(build)

    def _refresh_edit_actions(self) -> None:
        self.act_undo.setEnabled(self.controller.can_undo())
        self.act_redo.setEnabled(self.controller.can_redo())
        ut = self.controller.commands.undo_text()
        rt = self.controller.commands.redo_text()
        self.act_undo.setText(f"Undo {ut}" if ut else "Undo")
        self.act_redo.setText(f"Redo {rt}" if rt else "Redo")

    def _undo(self) -> None:
        self.controller.undo()

    def _redo(self) -> None:
        self.controller.redo()

    # -- project binding ------------------------------------------------------

    def set_project(self, project: Project, chip_types: dict | None = None) -> None:
        self.controller.set_project(project)
        types = chip_types if chip_types is not None else self.controller.chip_types()
        self.inspector.set_project(project)
        self.canvas.set_project(project, types)
        self.canvas.fit_to_view()
        title = project.metadata.name or "Untitled"
        self.setWindowTitle(f"placeKYT — {title}")
        self._refresh_edit_actions()

    # -- File menu ------------------------------------------------------------

    def _new_project(self) -> None:
        from PySide6.QtWidgets import QInputDialog

        types = self.controller.registry.names()
        if not types:
            self.statusBar().showMessage("No chip types installed.", 5000)
            return
        chip_type, ok = QInputDialog.getItem(
            self, "New Project", "Chip type:", types, 0, False)
        if not ok:
            return
        if not self._confirm_discard():
            return
        self.controller.new_project("Untitled", chip_type)
        self._after_project_loaded()

    def _open_project(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Project", "", "placeKYT projects (*.kyt)")
        if not path:
            return
        try:
            self.controller.open_project(path)
        except Exception as exc:  # noqa: BLE001
            self._error("Open failed", str(exc))
            return
        self._after_project_loaded()

    def _import_grc(self) -> None:
        """Import a GNURadio .grc flowgraph (the GRC-first flow, auto-P&R P4.2).
        A dialog first asks HOW MUCH automation: rough placement only (user routes
        manually) vs full place-and-route, and — for full P&R — the routing
        strategy. Unmapped blocks are NAMED in a warning."""
        from PySide6.QtWidgets import QFileDialog, QMessageBox

        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Import GNURadio Flowgraph", "",
            "GNURadio flowgraphs (*.grc)")
        if not path:
            return
        opts = self._ask_import_options()
        if opts is None:                          # user cancelled the dialog
            return
        try:
            result = self.controller.import_grc(path)
        except Exception as exc:  # noqa: BLE001
            self._error("Import failed", str(exc))
            return
        report = None
        try:
            self.controller.auto_place()
            if opts["route"]:                     # full place-and-route
                report = self.controller.auto_route_all(use_bus=opts["use_bus"])
            else:                                 # rough: place + flow-orient only
                self.controller.auto_orient_for_flow()
        except Exception as exc:  # noqa: BLE001
            self._after_project_loaded()
            self._error("Auto-P&R failed", str(exc))
            return
        self._after_project_loaded()
        # Watch the imported .grc so a re-save in GNU Radio flags drift on SAVE —
        # the user sees "out of sync" and can resync BEFORE running, instead of
        # having to run, resync, then run again.
        self._watch_grc_source(path)
        n_blocks = len(result.block_map)
        if report is not None:
            msg = f"Imported {n_blocks} block(s), routed {len(report.routed)} net(s)."
        else:
            msg = (f"Imported {n_blocks} block(s), placed — draw routes (or "
                   "auto-route), then Build.")
        if result.unknown:
            names = ", ".join(f"{gn} ({gid})" for gn, gid in result.unknown)
            QMessageBox.warning(
                self, "GRC Import",
                f"{msg}\n\nUnmapped blocks (skipped): {names}")
        else:
            self.statusBar().showMessage(msg, 6000)

    def _ask_import_options(self):
        """Dialog: full place-and-route vs rough placement. Returns
        ``{route, use_bus}`` or None if cancelled. Both options auto-place +
        flow-orient the blocks into the SAME compact layout; full P&R also routes
        the nets (bus), while rough placement leaves them unrouted (fly lines) for
        manual routing."""
        from PySide6.QtWidgets import (QButtonGroup, QDialog, QDialogButtonBox,
                                       QLabel, QRadioButton, QVBoxLayout)

        dlg = QDialog(self)
        dlg.setWindowTitle("Import GNURadio Flowgraph")
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel("How should the imported flowgraph be laid out?"))
        grp = QButtonGroup(dlg)
        rb_full = QRadioButton("Full place-and-route — ready to build")
        rb_full.setToolTip("Auto-place + flow-orient + route the nets (bus). "
                           "The general-purpose, ready-to-run layout.")
        rb_rough = QRadioButton("Rough placement — I'll route manually")
        rb_rough.setToolTip("Same compact placement + orientation as full P&R, "
                            "but leave every net unrouted (fly lines) so you can "
                            "draw/pack the routes by hand.")
        rb_full.setChecked(True)
        for rb in (rb_full, rb_rough):
            grp.addButton(rb)
            lay.addWidget(rb)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        if dlg.exec() != QDialog.Accepted:
            return None
        if rb_full.isChecked():
            return {"route": True, "use_bus": "always"}
        return {"route": False, "use_bus": "never"}

    def _save(self) -> None:
        if self.controller.project_path is None:
            self._save_as()
            return
        self._do_save(self.controller.project_path)

    def _save_as(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        path, _ = QFileDialog.getSaveFileName(
            self, "Save Project As", "", "placeKYT projects (*.kyt)")
        if path:
            if not path.endswith(".kyt"):
                path += ".kyt"
            self._do_save(path)

    def _do_save(self, path) -> None:
        try:
            self.controller.save_project(path)
            self.statusBar().showMessage(f"Saved {path}", 3000)
            self._update_title()
        except Exception as exc:  # noqa: BLE001
            self._error("Save failed", str(exc))

    def _after_project_loaded(self) -> None:
        p = self.controller.project
        self.inspector.set_project(p)
        self.canvas.set_project(p, self.controller.chip_types())
        self.canvas.fit_to_view()
        self.console.set_namespace(self._api_namespace())  # rebind to new project
        self._update_title()
        self._refresh_edit_actions()
        self._load_default_stimulus(p)

    def _load_default_stimulus(self, project) -> None:
        """Load the project's ``default_stimulus`` ``.kbs`` (if any) so plain Run
        injects it. Resolved relative to the project file. A missing/invalid file
        just clears the stimulus (Run falls back to the default ramp)."""
        ref = getattr(project.simulation, "default_stimulus", None)
        if not ref:
            self.sim.set_stimulus(None, None)
            return
        from pathlib import Path

        from engine.io.kbs import read_stimulus_kbs

        path = Path(ref)
        if not path.is_absolute() and self.controller.project_path is not None:
            path = self.controller.project_path.parent / ref
        try:
            words = read_stimulus_kbs(path)
        except Exception as exc:  # noqa: BLE001
            self.sim.set_stimulus(None, None)
            self.statusBar().showMessage(
                f"Default stimulus '{ref}' not loaded: {exc}", 4000)
            return
        self.sim.set_stimulus(words, path.name)
        self.statusBar().showMessage(
            f"Stimulus: {path.name} ({len(words)} words)", 3000)

    def _confirm_discard(self) -> bool:
        """If the project is dirty, ask before discarding. Returns True to go on."""
        if not self.controller.project.project_dirty:
            return True
        from PySide6.QtWidgets import QMessageBox

        resp = QMessageBox.question(
            self, "Unsaved changes",
            "Discard unsaved changes to the current project?",
            QMessageBox.Discard | QMessageBox.Cancel)
        return resp == QMessageBox.Discard

    def _update_title(self) -> None:
        p = self.controller.project
        name = p.metadata.name or "Untitled"
        mark = "*" if p.project_dirty else ""
        self.setWindowTitle(f"placeKYT — {name}{mark}")

    # -- Build menu -----------------------------------------------------------

    def _add_chip(self) -> None:
        try:
            chip_id = self.controller.add_chip()
            self.statusBar().showMessage(f"Added chip {chip_id}", 3000)
            self.canvas.fit_to_view()
        except Exception as exc:  # noqa: BLE001
            self.statusBar().showMessage(f"Add chip failed: {exc}", 4000)

    def _connect_chips(self) -> None:
        """Dialog to create a chip-to-chip wire (output port → input port)."""
        from PySide6.QtWidgets import (
            QComboBox,
            QDialog,
            QDialogButtonBox,
            QFormLayout,
        )

        project = self.controller.project
        if len(project.chips) < 2:
            self.statusBar().showMessage(
                "Add a second chip first (Chip → Add Chip).", 4000)
            return

        # Enumerate (chip, port) options by direction.
        outs, ins = [], []
        for chip in project.chips:
            ct = self.controller._chip_type_for_instance(chip)
            if ct is None:
                continue
            # Consistent label: "Chip N" plus the custom label (if any).
            label = f"Chip {chip.id}"
            if chip.label and chip.label != label:
                label = f"{label} ({chip.label})"
            for p in ct.ports:
                entry = (f"{label}.{p.name}", chip.id, p.name)
                (outs if p.direction.value == "output" else ins).append(entry)
        if not outs or not ins:
            self.statusBar().showMessage("No compatible ports to connect.", 4000)
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("Connect Chips")
        form = QFormLayout(dlg)
        src = QComboBox()
        for text, cid, port in outs:
            src.addItem(text, (cid, port))
        dst = QComboBox()
        for text, cid, port in ins:
            dst.addItem(text, (cid, port))
        # Default the target to an input on a DIFFERENT chip than the source,
        # so the obvious one-click choice is a valid cross-chip wire.
        def _retarget():
            sc = src.currentData()[0]
            for i in range(dst.count()):
                if dst.itemData(i)[0] != sc:
                    dst.setCurrentIndex(i)
                    return
        src.currentIndexChanged.connect(_retarget)
        _retarget()
        form.addRow("From (output):", src)
        form.addRow("To (input):", dst)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        if dlg.exec() != QDialog.Accepted:
            return
        (fc, fp), (tc, tp) = src.currentData(), dst.currentData()
        try:
            self.controller.add_inter_chip(fc, fp, tc, tp)
            self.statusBar().showMessage(
                f"Connected {fc}.{fp} → {tc}.{tp}", 3000)
        except Exception as exc:  # noqa: BLE001
            self.statusBar().showMessage(f"Connect failed: {exc}", 4000)

    def _on_panel_moved(self, panel_id, x, y) -> None:
        try:
            self.controller.move_panel(panel_id, x, y)
        except Exception as exc:  # noqa: BLE001
            self.statusBar().showMessage(f"Move panel failed: {exc}", 4000)

    def _on_panel_delete(self, panel_id) -> None:
        try:
            self.controller.remove_panel(panel_id)
            self.statusBar().showMessage(f"Deleted panel {panel_id}", 3000)
        except Exception as exc:  # noqa: BLE001
            self.statusBar().showMessage(f"Delete panel failed: {exc}", 4000)

    def _on_panel_mirror(self, panel_id) -> None:
        try:
            self.controller.mirror_panel(panel_id)
            self.statusBar().showMessage(f"Mirrored panel {panel_id}", 3000)
        except Exception as exc:  # noqa: BLE001
            self.statusBar().showMessage(f"Mirror panel failed: {exc}", 4000)


    def _on_panel_inspect(self, panel_id) -> None:
        """Open (or raise) the SRAM contents inspector for a panel."""
        from ui.panels.sram_inspector_panel import SramInspectorPanel

        win = self._sram_inspectors.get(panel_id)
        if win is None:
            panel = self.controller.project.panel(panel_id)
            if panel is None:
                return
            win = SramInspectorPanel(
                panel_id, panel.label, panel.size_words,
                provider=lambda pid=panel_id: self._panel_snapshot(pid))
            self._sram_inspectors[panel_id] = win
            win.destroyed.connect(
                lambda *_a, pid=panel_id: self._sram_inspectors.pop(pid, None))
        win.show()
        win.raise_()
        win.refresh()

    def _panel_snapshot(self, panel_id):
        """``(mem, activity)`` for a panel's inspector — from the live device.
        Activity is consumed by the inspector itself, so here we only hand over
        the current contents (blink is driven by panel_activity)."""
        dev = self.sim.panel_device(panel_id)
        if dev is None:
            return ({}, [])
        return (dict(dev.mem), [])

    def _on_panel_activity(self, acts) -> None:
        """SRAM panel read/write activity this batch: blink the panel box in the
        main view and push the touched addresses into any open inspector."""
        for pid, activity in acts.items():
            self.canvas.flash_panel(pid, activity)
            win = self._sram_inspectors.get(pid)
            if win is not None:
                dev = self.sim.panel_device(pid)
                if dev is not None:
                    win.view.set_contents(dict(dev.mem))
                win.view.add_activity(activity)

    def _add_panel(self) -> None:
        try:
            pid = self.controller.add_panel()
            self.statusBar().showMessage(f"Added SRAM panel {pid}", 3000)
            self.canvas.fit_to_view()
        except Exception as exc:  # noqa: BLE001
            self.statusBar().showMessage(f"Add panel failed: {exc}", 4000)

    def _connect_panel(self) -> None:
        """Dialog to wire a panel port to a chip port (the SRAM panel notes)."""
        from PySide6.QtWidgets import (
            QComboBox,
            QDialog,
            QDialogButtonBox,
            QFormLayout,
        )

        project = self.controller.project
        if not project.panels:
            self.statusBar().showMessage(
                "Add an SRAM panel first (Chip → Add SRAM Panel).", 4000)
            return
        # Panel port options: "(panel.port [dir])".
        panel_opts = []
        for panel in project.panels:
            for p in panel.ports:
                lbl = f"{panel.label or f'Panel {panel.id}'}.{p.name} " \
                      f"({p.direction.value})"
                panel_opts.append((lbl, panel.id, p.name))
        # Chip port options.
        chip_opts = []
        for chip in project.chips:
            ct = self.controller._chip_type_for_instance(chip)
            if ct is None:
                continue
            label = f"Chip {chip.id}"
            for p in ct.ports:
                chip_opts.append(
                    (f"{label}.{p.name} ({p.direction.value})", chip.id, p.name))
        if not panel_opts or not chip_opts:
            self.statusBar().showMessage("No ports available to connect.", 4000)
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("Connect Panel")
        form = QFormLayout(dlg)
        pcb = QComboBox()
        for text, pid, port in panel_opts:
            pcb.addItem(text, (pid, port))
        ccb = QComboBox()
        for text, cid, port in chip_opts:
            ccb.addItem(text, (cid, port))
        form.addRow("Panel port:", pcb)
        form.addRow("Chip port:", ccb)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        if dlg.exec() != QDialog.Accepted:
            return
        (pid, pport), (cid, cport) = pcb.currentData(), ccb.currentData()
        try:
            self.controller.connect_panel(pid, pport, cid, cport)
            self.statusBar().showMessage(
                f"Connected panel {pid}.{pport} → chip {cid}.{cport}", 3000)
        except Exception as exc:  # noqa: BLE001
            self.statusBar().showMessage(f"Connect panel failed: {exc}", 4000)

    def _check_drc(self) -> None:
        result = self.controller.run_drc()
        self._show_findings("Design Rule Check", result.findings,
                            ok_msg="DRC clean." if result.ok else None)

    def _generate_bitstream(self) -> None:
        result = self.controller.build()
        self._last_build = result if result.ok else None
        if result.ok:
            self._sync_resolved_faces()  # arrows now reflect resolved faces
        findings = result.errors + result.warnings
        if result.ok:
            cells = ", ".join(str(result.chips[c].cell_count)
                              for c in sorted(result.chips))
            self.statusBar().showMessage(
                f"Build OK — cells used: [{cells}]", 4000)
        self._show_findings(
            "Build", findings,
            ok_msg=f"Build succeeded ({len(result.warnings)} warning(s))."
            if result.ok else None)

    def _disassemble_built(self) -> None:
        """Build the open project and show its bitstream in the Disassembly dock
        (#184)."""
        result = getattr(self, "_last_build", None)
        if result is None or not getattr(result, "ok", False):
            result = self.controller.build()
            if not result.ok:
                self._show_findings("Build", result.errors)
                return
            self._last_build = result
        chips = [result.words(c) for c in sorted(result.chips)]
        name = self.controller.project.metadata.name or "built bitstream"
        self.disassembly_panel.show_words(
            chips[0] if chips else [], source=name, chips=chips or [[]])
        dock = self._docks.get("Disassembly")
        if dock is not None:
            dock.show()
            dock.raise_()
        self.statusBar().showMessage("Disassembled built bitstream.", 3000)

    def _export_bitstream(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        result = getattr(self, "_last_build", None)
        if result is None:
            result = self.controller.build()
            if not result.ok:
                self._show_findings("Build", result.errors)
                return
            self._last_build = result
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Bitstream", "", "Bitstream (*.kbs)")
        if not path:
            return
        if not path.endswith(".kbs"):
            path += ".kbs"
        try:
            self.controller.export_bitstream(result, path)
            self.statusBar().showMessage(f"Exported {path}", 3000)
        except Exception as exc:  # noqa: BLE001
            self._error("Export failed", str(exc))

    def _show_findings(self, title, findings, ok_msg=None) -> None:
        from PySide6.QtWidgets import QMessageBox

        if not findings:
            QMessageBox.information(self, title, ok_msg or f"{title}: clean.")
            return
        text = "\n".join(str(f) for f in findings[:50])
        if len(findings) > 50:
            text += f"\n… and {len(findings) - 50} more"
        box = QMessageBox(self)
        box.setWindowTitle(title)
        errors = [f for f in findings if f.severity.value == "ERROR"]
        box.setIcon(QMessageBox.Critical if errors else QMessageBox.Warning)
        box.setText(f"{len(errors)} error(s), "
                    f"{len(findings) - len(errors)} other finding(s).")
        box.setDetailedText(text)
        box.exec()

    def _error(self, title, message) -> None:
        from PySide6.QtWidgets import QMessageBox

        QMessageBox.critical(self, title, message)

    # -- Simulation menu ------------------------------------------------------

    def _load_stimulus(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        from engine.io.kbs import read_stimulus_kbs

        path, _ = QFileDialog.getOpenFileName(
            self, "Load Stimulus", "",
            "Stimulus bitstream (*.kbs);;All files (*)")
        if not path:
            return
        try:
            words = read_stimulus_kbs(path)
        except Exception as exc:  # noqa: BLE001
            self.statusBar().showMessage(f"Stimulus load failed: {exc}", 5000)
            return
        from pathlib import Path

        name = Path(path).name
        self.sim.set_stimulus(words, name)
        self.statusBar().showMessage(
            f"Loaded stimulus {name}: {len(words)} bitstream words", 4000)

    def _clear_stimulus(self) -> None:
        self.sim.set_stimulus(None, None)
        self.statusBar().showMessage("Stimulus cleared (using default ramp)", 3000)

    def _run_simulation(self) -> None:
        # F5 / Run-Pause toggle: start, or pause/resume a running sim (§3.2).
        if self.sim.running:
            self.sim.toggle_pause()
            return
        self._status_canvas.setText("Canvas: Simulation")
        if not self.sim.start():
            self._status_canvas.setText("Canvas: Edit")
            return
        self.output_panel.set_inputs(self.sim.input_samples)

    def _step_simulation(self) -> None:
        # Step by the selected granularity. Start (paused) first if not running.
        if not self.sim.running:
            if not self.sim.start():
                return
            self.output_panel.set_inputs(self.sim.input_samples)
            self.sim.pause()
        self.sim.step(self.step_mode.currentData())

    def _reset_simulation(self) -> None:
        self.sim.reset()
        self.canvas.clear_sim_states()
        self._status_canvas.setText("Canvas: Edit")

    def _on_stimulus_loaded(self, words, name) -> None:
        """Auto-load the run's stimulus bitstream into the Disassembly panel
        (#195) so the user sees what is being injected without a manual Load."""
        self.disassembly_panel.show_words(list(words), source=str(name))
        # Re-apply any stimulus-line breakpoints the user set (#197).
        self.disassembly_panel.set_breakpoints(self.sim.injection_breakpoints())

    def _on_disasm_breakpoint_toggled(self, line, on) -> None:
        """A Disassembly line breakpoint was toggled — sync it to the sim (#197).
        Toggling via the panel and via the sim must agree, so only flip the sim
        if its state differs from the requested ``on``."""
        has = line in self.sim.injection_breakpoints()
        if has != on:
            self.sim.toggle_injection_breakpoint(line)
        self.statusBar().showMessage(
            f"Breakpoint {'set on' if on else 'cleared from'} stimulus word "
            f"{line}.", 2500)

    def _on_injection_breakpoint_hit(self, line) -> None:
        """The run paused at a stimulus-line breakpoint (#197)."""
        dock = self._docks.get("Disassembly")
        if dock is not None:
            dock.show()
            dock.raise_()
        self.statusBar().showMessage(
            f"Paused at stimulus word {line} (just injected). Run to continue.",
            5000)

    def _on_sim_state(self, state: str) -> None:
        self._status_sim.setText(f"Sim: {state}")
        self.act_run.setText("Pause" if state == "running" else "Run")
        if state in ("done", "idle") or state.startswith("error"):
            self._status_canvas.setText("Canvas: Edit")

    def _on_sim_metrics(self, m: dict) -> None:
        self._status_sim.setText(
            f"Sim: {self.sim.total_events} events, "
            f"{m.get('time_ns', 0.0):.0f} ns")

    # -- actions --------------------------------------------------------------

    def reset_layout(self) -> None:
        self.restoreState(self._default_state)

    def _about(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        QMessageBox.about(
            self, "About placeKYT",
            "placeKYT — IDE for the Kyttar asynchronous mcell array\n"
            "Lattrex · lattrex.com",
        )
