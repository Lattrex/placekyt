"""WaveformPanel — dock around the WaveformView (DEBUG_ARCHITECTURE §3.3).

Wraps the custom-painted :class:`WaveformView` with:
  * a **per-stream radix** strip (Analog / Hex / Dec / Bin) — one selector per
    plotted ``(chip, port)`` stream,
  * a **measurement readout**: the Δt between the main + measurement cursors,
    input→output latency, and the focused stream's amplitude.

Reads a :class:`engine.trace_model.TraceModel` (via ``set_trace_model``) and
shares the global time cursor with the other debug views (emits
``cursor_requested`` when the user left-clicks the wave; follows the shared
cursor via ``set_cursor``).
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ui.widgets.waveform_view import WaveformRuler, WaveformView


class WaveformPanel(QWidget):
    """Waveform viewer + radix controls + measurement readout."""

    # Re-emits WaveformView.cursor_requested (the shared time cursor).
    cursor_requested = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._model = None
        self._initial_reg_fetch = None  # (chip,x,y,addr) -> initial value | None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # File menu: save / load the signal list (persist a debug view to YAML).
        from PySide6.QtWidgets import QMenuBar

        menubar = QMenuBar()
        file_menu = menubar.addMenu("File")
        file_menu.addAction("Save Signals…", self._save_signals)
        file_menu.addAction("Load Signals…", self._load_signals)
        # X-axis: real time (ns) vs sample index (0,1,2,… — lines up with GRC).
        axis_menu = menubar.addMenu("X-Axis")
        self._act_time = axis_menu.addAction("Time (ns)")
        self._act_index = axis_menu.addAction("Sample index")
        for a in (self._act_time, self._act_index):
            a.setCheckable(True)
        self._act_time.setChecked(True)
        self._act_time.triggered.connect(lambda: self._set_x_axis("time"))
        self._act_index.triggered.connect(lambda: self._set_x_axis("index"))
        outer.setMenuBar(menubar)

        body = QVBoxLayout()
        body.setContentsMargins(4, 4, 4, 4)
        body.setSpacing(4)
        outer.addLayout(body, 1)

        # The wave itself, in a scroll area (more signals than fit → scroll). The
        # view requests its full content height (set_streams updates its minimum
        # height) so the scroll area scrolls instead of squashing the rows.
        self.view = WaveformView()
        self.view.cursor_requested.connect(self.cursor_requested)
        self.view.radix_changed.connect(lambda *_: self._refresh_readout())
        # The measurement (middle-click) cursor also drives the Δt/freq readout.
        self.view.measurement_changed.connect(self._refresh_readout)
        self.view.streams_changed.connect(self._refresh_readout)
        # A register dragged from the Program pane → add it as a (hex) trace.
        self.view.register_dropped.connect(
            lambda chip, x, y, addr: self.add_register_trace(chip, x, y, addr))
        # A chip PORT dragged from the canvas → pick a channel/tag, add that
        # demuxed stream (a port is a time-multiplexed bus — view one at a time).
        self.view.port_dropped.connect(self._on_port_dropped)
        # Optional: a project lookup giving human stream names per port tag.
        self._port_tag_namer = None
        # Right-click "Add channel" submenu: list the demuxed port channels the
        # trace model has captured, so the user can add one WITHOUT dragging.
        self.view.set_channel_provider(self._available_channels)
        # A ROUTE dragged from the canvas → plot the channels flowing through it.
        self.view.route_dropped.connect(self._on_route_dropped)
        # Optional: a project resolver giving a route's data channels.
        self._route_channel_provider = None
        # Fixed time-axis header ABOVE the scroll area so the ruler is always
        # visible while the traces scroll vertically. It mirrors the view's
        # window and is wide enough to align ticks over the plot region.
        self._ruler = WaveformRuler(self.view)
        body.addWidget(self._ruler)
        self.view.cursor_requested.connect(lambda *_: self._ruler.update())

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setWidget(self.view)
        body.addWidget(self._scroll, 1)

        # Measurement readout.
        self._readout = QLabel("")
        self._readout.setWordWrap(True)
        body.addWidget(self._readout)

        self._hint = QLabel(
            "Drag a chip PORT or a register here to add a trace (a port pops a "
            "channel picker) · Left-click: cursor · Middle-click: measure (Δt) · "
            "Wheel: zoom · Shift+wheel: pan · Ctrl+wheel: amplitude · "
            "Drag row edge: height · Click gutter: select · Drag gutter: move/"
            "stack · Right-click: radix/colour/delete · z: zoom region · f: fit")
        self._hint.setStyleSheet("color: #888;")
        body.addWidget(self._hint)
        self._refresh_readout()

    def _set_x_axis(self, mode: str) -> None:
        self._act_time.setChecked(mode == "time")
        self._act_index.setChecked(mode == "index")
        self.view.set_x_axis_mode(mode)
        self._ruler.update()
        self._refresh_readout()

    # -- model ----------------------------------------------------------------

    def set_trace_model(self, model) -> None:
        # A brand-new model (fresh run / project) → re-apply the default per-tag
        # split once for it (the flag below makes the split a one-shot per model
        # so a user-removed channel isn't forced back on every live refresh).
        if model is not self._model:
            self._default_split_done = False
        self._model = model
        if model is None:
            self.view.set_streams({})
            self.set_cursor(None)
            return
        # A MULTIPLEXED port (several tagged channels, e.g. x16_in carrying xi+xq)
        # would otherwise overlay all its channels on ONE trace, which is unreadable.
        # Split such a port into one DEMUXED trace per tag for the default view;
        # single-channel ports stay as a single plain-port trace.
        by_tag = model.port_streams_by_tag()
        tags_per_port: dict[tuple[int, str], list] = {}
        for (chip, port, tag) in by_tag:
            tags_per_port.setdefault((chip, port), []).append(tag)
        plain = {}
        multiplexed = set()
        for (chip, port), samples in model.port_streams().items():
            tags = tags_per_port.get((chip, port), [])
            if len(tags) > 1:
                multiplexed.add((chip, port))
            else:
                plain[(chip, port)] = samples
        # Plain ports → single traces (replaces prior plain-port traces each call).
        self.view.set_streams(plain)
        # Multiplexed ports → one persistent demuxed trace per tag (idempotent;
        # update_port_tag_samples below keeps them live). Only auto-added once —
        # if the user removed one, it isn't forced back.
        shown = {(s["source"].get("chip"), s["source"].get("port"),
                  s["source"].get("tag"))
                 for s in self.view._streams
                 if s.get("source", {}).get("type") == "port_tag"}
        if not getattr(self, "_default_split_done", False):
            for (chip, port) in sorted(multiplexed):
                for tag in sorted(tags_per_port[(chip, port)],
                                  key=lambda d: (d is None, d)):
                    if (chip, port, tag) not in shown:
                        self.add_port_trace(chip, port, tag)
            self._default_split_done = True
        # Refresh register + demuxed-port traces from the model (keep live).
        self.view.update_register_samples(model.register_stream)
        self.view.update_port_tag_samples(self._port_tag_stream)
        self.set_cursor(model.cursor_ns)

    def set_initial_register_fetch(self, fetch) -> None:
        """Provide ``fetch(chip,x,y,addr) -> initial value | None`` so register
        traces can show their programmed/reset value before the first write."""
        self._initial_reg_fetch = fetch

    def _initial_for(self, chip, x, y, addr):
        if self._initial_reg_fetch is None:
            return None
        try:
            return self._initial_reg_fetch(chip, x, y, addr)
        except Exception:  # noqa: BLE001
            return None

    def add_register_trace(self, chip, x, y, addr, label=None) -> None:
        """Add a register value-over-time trace (Program-pane drag target)."""
        samples = (self._model.register_stream(chip, x, y, addr)
                   if self._model is not None else [])
        self.view.add_register_stream(
            chip, x, y, addr, samples, label=label,
            initial=self._initial_for(chip, x, y, addr))

    # -- port drag → channel/tag picker → demuxed trace -----------------------

    def set_port_tag_namer(self, namer) -> None:
        """Provide ``namer(chip, port, tag) -> str | None`` so the channel picker
        can label a port's streams with their logical net names (e.g. 'xi'/'xq')
        instead of the bare dest tag. Optional — without it tags show as numbers."""
        self._port_tag_namer = namer

    def _tag_label(self, chip, port, tag) -> str:
        name = None
        if self._port_tag_namer is not None:
            try:
                name = self._port_tag_namer(chip, port, tag)
            except Exception:  # noqa: BLE001
                name = None
        if tag is None:
            return name or "all words"
        return f"{name} (tag {tag})" if name else f"tag {tag}"

    def _port_tag_stream(self, chip, port, tag):
        if self._model is None:
            return []
        return self._model.port_streams_by_tag().get((chip, port, tag), [])

    def _on_port_dropped(self, chip, port) -> None:
        """A port was dragged onto the viewer. A port is a TIME-MULTIPLEXED bus:
        present the streams it carries (by tag) and add the one the user picks —
        viewing all interleaved words at once is not useful."""
        from PySide6.QtWidgets import QInputDialog, QMessageBox

        tags = self._model.port_tags(chip, port) if self._model is not None else []
        if not tags:
            QMessageBox.information(
                self, "Add port",
                f"chip{chip}.{port} has no captured samples yet — run the sim "
                "first, then drag the port in to view a stream.")
            return
        if len(tags) == 1:
            self.add_port_trace(chip, port, tags[0])
            return
        labels = [self._tag_label(chip, port, t) for t in tags]
        choice, ok = QInputDialog.getItem(
            self, "Select channel",
            f"chip{chip}.{port} is a multiplexed bus — pick a stream to view:",
            labels, 0, False)
        if not ok:
            return
        self.add_port_trace(chip, port, tags[labels.index(choice)])

    def add_port_trace(self, chip, port, tag, label=None) -> None:
        """Add a demuxed port-stream trace for one channel/tag."""
        samples = self._port_tag_stream(chip, port, tag)
        self.view.add_port_stream(
            chip, port, tag, samples,
            label=label or f"chip{chip}.{port} · {self._tag_label(chip, port, tag)}")

    def set_route_channel_provider(self, provider) -> None:
        """Set ``provider(connection_name) -> [(label, source_dict)]`` giving the
        data channels flowing through a route (the panel can't see the project, so
        MainWindow supplies this). ``source_dict`` is a waveform source descriptor
        (``{"type":"port_tag",chip,port,tag}`` or
        ``{"type":"register",chip,x,y,addr}``)."""
        self._route_channel_provider = provider

    def _add_source(self, src: dict, label=None) -> None:
        """Add a trace for a waveform source descriptor (port_tag or register)."""
        t = src.get("type")
        if t == "port_tag":
            self.add_port_trace(src["chip"], src["port"], src.get("tag"), label)
        elif t == "register":
            self.add_register_trace(src["chip"], src["x"], src["y"], src["addr"],
                                    label=label)

    def _on_route_dropped(self, connection_name) -> None:
        """A route was dragged onto the viewer → plot the channel(s) flowing
        through it. One channel auto-adds; several pop a picker (a route can carry
        multiple streams, e.g. an I/Q complex sample)."""
        from PySide6.QtWidgets import QInputDialog, QMessageBox

        if self._route_channel_provider is None:
            return
        try:
            chans = self._route_channel_provider(connection_name) or []
        except Exception:  # noqa: BLE001
            chans = []
        if not chans:
            QMessageBox.information(
                self, "Add route",
                f"No captured data on route '{connection_name}' yet — run the "
                "sim, then drag the route in.")
            return
        if len(chans) == 1:
            label, src = chans[0]
            self._add_source(src, label)
            return
        labels = [c[0] for c in chans]
        choice, ok = QInputDialog.getItem(
            self, "Select channel",
            f"Route '{connection_name}' carries multiple streams — pick one:",
            labels, 0, False)
        if ok:
            self._add_source(chans[labels.index(choice)][1], choice)

    def _available_channels(self):
        """``[(menu_label, on_add)]`` of every demuxed port channel the trace
        model has captured (drives the right-click 'Add channel' submenu). Each
        on_add adds that demuxed stream. Already-shown channels are skipped."""
        if self._model is None:
            return []
        shown = {(s["source"].get("chip"), s["source"].get("port"),
                  s["source"].get("tag"))
                 for s in self.view._streams
                 if s.get("source", {}).get("type") == "port_tag"}
        items = []
        for (chip, port, tag) in sorted(
                self._model.port_streams_by_tag().keys(),
                key=lambda k: (k[0], k[1], (k[2] is None, k[2]))):
            if (chip, port, tag) in shown:
                continue
            label = f"chip{chip}.{port} · {self._tag_label(chip, port, tag)}"
            items.append((label,
                          lambda c=chip, p=port, t=tag: self.add_port_trace(c, p, t)))
        return items

    # -- signal-list save / load (YAML) ---------------------------------------

    def _resolve_source(self, src: dict):
        """Current samples for a saved source descriptor (port or register)."""
        if self._model is None:
            return []
        if src.get("type") == "port":
            return self._model.port_streams().get(
                (src["chip"], src["port"]), [])
        if src.get("type") == "port_tag":
            return self._port_tag_stream(src["chip"], src["port"], src.get("tag"))
        if src.get("type") == "register":
            return self._model.register_stream(
                src["chip"], src["x"], src["y"], src["addr"])
        return []

    def _save_signals(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        from engine.io.waveform_io import save_signal_list

        path, _ = QFileDialog.getSaveFileName(
            self, "Save Signal List", "", "Signal list (*.wsig.yaml *.yaml)")
        if not path:
            return
        if not path.endswith((".yaml", ".yml")):
            path += ".wsig.yaml"
        save_signal_list(self.view.to_signal_list(), path)

    def _load_signals(self) -> None:
        from PySide6.QtWidgets import QFileDialog, QMessageBox

        from engine.io.waveform_io import load_signal_list

        path, _ = QFileDialog.getOpenFileName(
            self, "Load Signal List", "", "Signal list (*.wsig.yaml *.yaml)")
        if not path:
            return
        try:
            signals = load_signal_list(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Load Signal List", str(exc))
            return
        self.view.from_signal_list(signals, self._resolve_source)

    def set_cursor(self, ns) -> None:
        """Follow the shared time cursor — move the wave's main cursor and update
        the gutter values + measurement readout."""
        self.view.set_main_cursor(ns)
        self._refresh_readout()

    # -- measurement readout --------------------------------------------------

    def _refresh_readout(self) -> None:
        parts = []
        dt = self.view.measurement_dt
        if dt is not None:
            freq = f" → {1e9 / dt:.1f} Hz" if dt > 0 else ""
            parts.append(f"Δt = {dt:.1f} ns{freq}")
        lat = self.view.latency_ns()
        if lat is not None:
            parts.append(f"latency = {lat:.1f} ns")
        # Amplitude of the first stream (the most common single-stream case).
        if self.view.stream_count():
            amp = self.view.amplitude_of(0)
            if amp is not None:
                parts.append(f"{self.view.stream_labels()[0]} "
                             f"amp = [{amp[0]}, {amp[1]}]")
        self._readout.setText("   ".join(parts) if parts
                              else "Place cursors (left/middle-click) to measure.")
