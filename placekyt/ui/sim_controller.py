"""SimController — drives a GUI simulation over the engine SimulationEngine.

Builds the project, loads the bitstream (with tracing), configures the input
port from the placed design, then runs animated via a QTimer: each tick steps a
batch of events and re-derives the per-cell overlay from the trace (§3.2). Emits
``cell_states`` for the canvas and ``state_changed`` for the status bar.

Single-chip (§4.3 v1.0). Multi-chip round-based playback is a later milestone.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QTimer, Signal

from engine.simulator import SimulationEngine

# Animation cadence. Each tick advances the sim by ``batch`` engine events; the
# tick interval and the per-tick flash-playback rate are part of the same speed
# step so the SLOW end is genuinely slow-motion (few events, long interval, one
# word lit per tick) and the FAST end keeps pace (big batch, short interval,
# flash catch-up). Each entry: (events_per_tick, tick_ms, flash_steps_per_tick).
# flash_steps_per_tick == 0 → adaptive catch-up (len//8); a positive value caps
# playback to that many per-word steps per tick so individual words are visible.
SPEED_STEPS = [
    (1,    800, 1),    # 0: slowest — ~1 transaction visible at a time
    (1,    400, 1),    # 1
    (1,    200, 1),    # 2
    (2,    132, 1),    # 3
    (5,    100, 1),    # 4
    (20,    66, 2),    # 5
    (100,   40, 4),    # 6
    (500,   33, 0),    # 7: adaptive catch-up from here up
    (2000,  33, 0),    # 8 (default)
    (8000,  33, 0),    # 9: fastest
]
DEFAULT_SPEED = 8  # → 2000 events/tick (matches the prior default)

# Back-compat: some callers/tests reference the events-per-tick ladder.
SPEED_BATCHES = [s[0] for s in SPEED_STEPS]
_TICK_MS = SPEED_STEPS[DEFAULT_SPEED][1]

# Live GNURadio-server mode. The TraceModel keeps a rolling window of the most
# recent _LIVE_TRACE_MAX events (the chip trace is DRAINED + cleared each refresh
# — its max_records is a hard cap that stops recording when full, NOT a ring
# buffer, so we clear it to keep fresh events flowing). _LIVE_CHIP_CAP bounds the
# chip's between-refresh buffer so a burst can't blow memory if a refresh is
# slow. Refresh is throttled to _LIVE_REFRESH_HZ.
_LIVE_TRACE_MAX = 20000     # default rolling window kept in the TraceModel (GUI)
_LIVE_CHIP_CAP = 100_000    # chip-side cap between refreshes (drained each tick)
_LIVE_REFRESH_HZ = 8        # cap debug refreshes/sec during streaming


class SimController(QObject):
    """Owns a running simulation and emits overlay/status updates."""

    # object (not dict): PySide6 can't marshal a dict with tuple keys through a
    # typed dict signal; pass it as an opaque Python object.
    cell_states = Signal(object)   # {(x, y): state} for the single chip
    # {(chip, x, y): "south"|"east"|"west"|"north"} — LIVE output face per cell
    # this frame. A cell can change its FACE at runtime (MOVE [FACE], e.g. the
    # crossover relay), so the canvas arrow follows the live config.
    cell_faces = Signal(object)
    state_changed = Signal(str)    # "running"/"paused"/"idle"/"done"/"error: …"
    metrics = Signal(object)       # {"events": N, "time_ns": float}
    # {"chip": id, "port": name, "samples": [uint16, …]} — captured output port
    # data after each run batch (the values exiting the design).
    output = Signal(object)
    # (words, name) — the exact BITSTREAM injected at the input port for THIS run
    # (loaded .kbs, or a ramp wrapped into bursts). Emitted when a run starts so
    # the Disassembly panel can auto-load what is actually being run.
    stimulus_loaded = Signal(object, object)
    # int — cumulative count of stimulus words injected at the input port so far.
    # Drives the Disassembly panel's live line highlight (the just-injected word
    # = line count-1) as data enters the chip.
    injection_progress = Signal(int)
    # int — a stimulus-line breakpoint fired: the run paused after this many
    # words injected (the just-injected word index = arg - 1). Drives the
    # Disassembly panel's "stopped here" marker.
    injection_breakpoint_hit = Signal(int)
    # [(chip, x, y, face), …] — NEW data transfers this batch (handshake-flash).
    handshakes = Signal(object)
    # Per-tick per-word flash playback rate (steps released per decay tick), set
    # by the speed slider: 1 = slow-motion (one word at a time), 0 = adaptive
    # catch-up. Drives the canvas flash playback so the SLOW end shows individual
    # transactions firing one-by-one.
    flash_rate = Signal(int)
    # the TraceModel was rebuilt from the latest trace (debug views refresh).
    trace_updated = Signal(object)  # the TraceModel
    # the live cell state changed (a step/stop happened): debug views holding a
    # selected cell should re-pull cell_live_state(). Carries nothing — it's a
    # "refresh now" pulse so the Inspector reads the freshest PC + registers.
    cell_state_refreshed = Signal()
    # A breakpoint fired and paused the run — carries the BreakpointHit (the UI
    # shows which, parks the cursor at the hit, marks the scrubber).
    breakpoint_hit = Signal(object)
    # The GNURadio bridge server advanced the chip (a remote run_until_output) —
    # the debug views should refresh from the live chip. Emitted from the server
    # thread; receivers run on the GUI thread via Qt's queued connection.
    server_activity = Signal(bool)   # arg: full_capture (True for a one-shot batch)
    # Per-batch simKYT throughput on THIS machine: {"samples": N, "seconds": s,
    # "samples_per_sec": r}. Surfaced in the status bar so the user can estimate how
    # long a given burst will take (simKYT is an event-accurate async-ASIC sim, NOT
    # a real-time DSP source). Emitted from the server thread; GUI-thread receivers.
    server_throughput = Signal(object)
    # The GNURadio server started/stopped: carries the bound port (or 0/None).
    server_state = Signal(object)
    # A GRC client advertised its flowgraph's block params (the GRC↔placeKYT
    # sync wire field / op). Payload: {placeKYT block name: params}. Emitted from
    # the server thread → queued to the GUI thread, where the controller re-diffs
    # against the placed design and flips the out-of-sync indicator.
    grc_params_received = Signal(object)

    # Emitted (queued to the GUI thread) when the server REBUILT + re-hosted the
    # chip because the design was edited since the last run (build_dirty). The GUI
    # does a FULL canvas render_scene() so the displayed cells match the just-built
    # chip — otherwise routing cells from a PRIOR route can linger as "phantom"
    # items while the new (correct) bitstream runs underneath.
    chip_rehosted = Signal()
    # SRAM panel activity this batch: {panel_id: [(addr, "w"|"r"), …]}. Drives
    # the panel blink (main view) + the inspector refresh.
    panel_activity = Signal(object)

    def __init__(self, app_controller, parent=None):
        super().__init__(parent)
        self.app = app_controller
        self.engine: SimulationEngine | None = None
        self._width = 10
        self._timer = QTimer(self)
        self._timer.setInterval(_TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._running = False
        self._paused = False
        self._batch, _tick, self._flash_per_tick = SPEED_STEPS[DEFAULT_SPEED]
        self._events = 0
        # Optional user-loaded stimulus BITSTREAM: a list of raw 16-bit
        # WRITE/DATA/JUMP words injected into the input port verbatim. When set,
        # it feeds the input port instead of the default ramp (§stimulus).
        self._stimulus: list[int] | None = None
        self._stimulus_name: str | None = None
        self._multi = False  # multi-chip (MultiChipSimEngine) vs single-chip
        self._sim_chip = 0
        self._captured: list[int] = []  # accumulated output-port samples
        self._input_samples: list[int] = []  # samples injected this run
        # The project.design_version (monotonic, bumped on every edit, NEVER cleared
        # by a build) that the SERVER currently has hosted. The pre-batch check
        # compares this to the live design_version to decide whether to rebuild —
        # NOT build_dirty, which the GUI's own post-edit cached_build() clears before
        # the GRC Run ever sees it (that was the stale-run / phantom-cells bug).
        self._hosted_design_version: int | None = None
        from engine.trace_model import TraceModel

        self.trace_model = TraceModel()  # the debug data spine (§debug)

        from engine.breakpoints import BreakpointSet

        # Active breakpoints (DEBUG §3.6) + per-chip scan cursors so each new
        # trace event is checked for a hit exactly once.
        self.breakpoints = BreakpointSet()
        self._bp_scan: dict[int, int] = {}
        # Hits accumulated this run (for the scrubber's red markers).
        self._bp_hits: list = []
        # Stimulus-line breakpoints (#197): word indices into the injected
        # bitstream. The run PAUSES once that many words have entered the input
        # port (the word's disassembly line just injected). Set from the
        # Disassembly panel; survive across runs until cleared.
        self._inject_breakpoints: set[int] = set()
        self._last_inject_count = 0
        # GNURadio bridge server (placeKYT hosts the chip; GRC streams to it).
        self._gr_server = None
        # Debug hooks for a GRC batch run (breakpoints/speed/step honored in the
        # server-side per-sample loop); created on server start, None otherwise.
        self._batch_debug = None
        # Current speed-slider index (also drives the batch playback delay).
        self._speed_index = DEFAULT_SPEED
        # Host-side SRAM panel devices, registered in-fabric with the engine
        # (#193): run() self-pumps them. {panel_id: SramPanelDevice}; the chip
        # output ports feeding registered panels (for ack-pending checks).
        self._panel_devices: dict = {}
        self._panel_out_ports: list = []
        # Live trace window size (events kept in the rolling debug view) — user
        # configurable (Simulation → Live Window Size).
        self._live_trace_max = _LIVE_TRACE_MAX

    def set_stimulus(self, stimulus, name: str | None = None) -> None:
        """Use a stimulus BITSTREAM (list of raw 16-bit words) for the next run,
        or ``None`` to clear (falls back to the default ramp)."""
        self._stimulus = list(stimulus) if stimulus is not None else None
        self._stimulus_name = name

    @property
    def stimulus_name(self) -> str | None:
        return self._stimulus_name

    @property
    def input_samples(self) -> list[int]:
        """The samples injected at the input port for the current run."""
        return list(self._input_samples)

    @property
    def running(self) -> bool:
        return self._running

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def total_events(self) -> int:
        return self._events

    def set_speed_index(self, index: int) -> None:
        """Apply a slider speed step: events-per-tick + tick interval + per-word
        flash playback rate. The slow end runs few events with a long interval
        and lights ONE word per tick (slow-motion); the fast end runs big batches
        with flash catch-up."""
        index = max(0, min(len(SPEED_STEPS) - 1, index))
        self._speed_index = index
        self._batch, tick_ms, self._flash_per_tick = SPEED_STEPS[index]
        self._timer.setInterval(tick_ms)
        self.flash_rate.emit(self._flash_per_tick)
        # Same slider paces a GRC batch run (the per-sample server loop): map the
        # speed step to a per-sample delay so slow = slow-motion, fast = no wait.
        if self._batch_debug is not None:
            self._batch_debug.set_delay(self._batch_debug_delay_for_speed())

    def _batch_debug_delay_for_speed(self) -> float:
        """Per-sample delay (seconds) for the current speed index in a GRC batch
        run. The slow end pauses ~0.3 s/sample (slow-motion, one sample visible);
        the fast end runs with no wait. Derived from the slider's tick interval so
        it tracks the same ladder the interactive animation uses."""
        # Fastest few steps → no artificial delay (flat-out). Below that, scale
        # from the tick interval (ms) into a per-sample pause.
        if self._speed_index >= 7:
            return 0.0
        tick_ms = SPEED_STEPS[self._speed_index][1]
        return min(0.4, tick_ms / 1000.0)

    def _batch_breakpoint_hit(self, chip, sample_index: int) -> bool:
        """Server-thread breakpoint check for a GRC batch sample. Reuses the same
        BreakpointSet as the interactive run, evaluated against the hosted chip's
        recent trace events. Returns True if any enabled breakpoint fired on this
        sample (the batch loop then pauses). Qt-free — touches engine only."""
        if not self.breakpoints.breakpoints:
            return False
        chip_obj = self._gr_server._chip if self._gr_server is not None else None
        if chip_obj is None:
            return False
        try:
            sim_chip = getattr(chip_obj, "id", 0) or 0
            events = chip_obj.drain_trace() if hasattr(chip_obj, "drain_trace") \
                else chip_obj.trace_events()
            hit = self.breakpoints.first_hit(sim_chip, list(events), self._width)
        except Exception:
            return False
        if hit is not None:
            self.breakpoint_hit.emit(hit)
            return True
        return False

    # -- lifecycle ------------------------------------------------------------

    def start(self, stimulus: list[int] | None = None) -> bool:
        """Build, load, configure the input port, and begin animated stepping.

        Returns False (and emits an error state) if the build has DRC errors.
        ``stimulus`` defaults to a short ramp so the demo shows activity without
        a stimulus file.
        """
        result = self.app.build()
        if not result.ok:
            self.state_changed.emit(f"error: {len(result.errors)} DRC error(s)")
            return False

        project = self.app.project
        chip0 = project.chip(0)
        type_name = (chip0.type_name if chip0 and chip0.type_name
                     else project.chip_type)
        entry = self.app.registry.require(type_name)
        self._width = entry.chip_type.width

        # Auto-select: 1 chip → fast single-chip path; 2+ chips → round-based
        # MultiChipSimulation with inter-chip relay (§4.3).
        self._multi = len(project.chips) > 1
        if self._multi:
            if not self._start_multi(result, stimulus):
                return False
        else:
            self.engine = SimulationEngine(entry.path)
            self.engine.load(result.words(0), trace=True)
            self._sim_chip = chip0.id if chip0 else 0
            # Stimulus is a BITSTREAM of raw WRITE+DATA+JUMP words, injected into
            # the input port verbatim (§stimulus). A loaded .kbs stimulus IS the
            # words; otherwise a value list / default ramp is wrapped into bursts
            # using the design's input-port config so it runs the same path.
            in_cfg = self._input_port_config(self._sim_chip)
            words = self._stimulus_words(in_cfg, override=stimulus)
            self._sim_in_port = in_cfg[0] if in_cfg else "x16_in"
            if words:
                self.engine.inject_words(words, port=self._sim_in_port)
            # Tell the Disassembly panel what's actually being injected this run.
            self.stimulus_loaded.emit(
                list(words), self._stimulus_name or "(run stimulus)")

        self._setup_panels()

        self._running = True
        self._paused = False
        self._events = 0
        self._captured = []
        self._bp_scan = {}   # fresh breakpoint scan for this run
        self._bp_hits = []
        self._last_inject_count = 0       # fresh stimulus-line bp scan (#197)
        self._inject_paused_this_frame = False
        self.output.emit({"chip": None, "port": None, "samples": []})  # clear
        self.state_changed.emit("running")
        self._timer.start()
        return True

    @property
    def gr_server_running(self) -> bool:
        return self._gr_server is not None

    @property
    def live_window(self) -> int:
        return self._live_trace_max

    def set_live_window(self, n: int) -> None:
        """Set the live trace window (events kept in the rolling debug view).
        Trims immediately if shrinking."""
        self._live_trace_max = max(100, int(n))
        self.trace_model.trim_to(self._live_trace_max)

    def start_gnuradio_server(self, *, host: str = "127.0.0.1",
                              port: int = 0) -> int | None:
        """Build the project and host its chip over a socket so a GNURadio
        flowgraph can stream samples through it LIVE (DEBUG bridge). placeKYT's
        debug views refresh as the remote run advances. Single-chip only for now.
        Returns the bound port, or None on failure."""
        if self._gr_server is not None:
            return self._gr_server.bound_port

        result = self.app.build()
        if not result.ok:
            self.state_changed.emit(f"error: {len(result.errors)} DRC error(s)")
            return None
        project = self.app.project
        if len(project.chips) > 1:
            self.state_changed.emit("error: GNURadio server is single-chip only")
            return None

        chip0 = project.chip(0)
        type_name = (chip0.type_name if chip0 and chip0.type_name
                     else project.chip_type)
        entry = self.app.registry.require(type_name)
        self._width = entry.chip_type.width
        self._multi = False
        self.engine = SimulationEngine(entry.path)
        # Live streaming can run indefinitely — cap the chip's trace to a ring
        # buffer of the last N events so memory + refresh cost stay O(window),
        # not O(total). Without this, a long stream grows the trace without
        # bound and the GUI lags / stalls on Stop.
        self.engine.load(result.words(0), trace=True,
                         max_records=_LIVE_CHIP_CAP)
        # Server now hosts the design at this version; the pre-batch check rebuilds
        # only when the live design_version moves past it (i.e. an edit happened).
        self._hosted_design_version = getattr(self.app.project, "design_version", 0)
        self._sim_chip = chip0.id if chip0 else 0
        cfg = self._input_port_config(self._sim_chip)
        default_entries: dict[str, int] = {}
        if cfg is not None:
            port_name, kw = cfg
            self.engine.configure_input_port(port_name, **kw)
            # Remember the resolved entry so the bridge can default to it when a
            # GRC injects without specifying jump_entry (blocks whose entry != 0,
            # e.g. the coherent-RX phase cell at 17, then work over the bridge).
            if "entry_addr" in kw:
                default_entries[port_name] = int(kw["entry_addr"])
        self._bp_scan = {}
        self._bp_hits = []
        self._last_server_refresh = 0.0  # for refresh throttling

        from engine.sim_bridge import SimServer

        # on_activity runs on the server thread → emit a Qt signal (queued to the
        # GUI thread) so the debug-view refresh happens safely on the GUI side.
        # on_reset (client requested a fresh run) rehosts a clean chip — Qt-free
        # engine ops only, so it's safe to run on the server thread.
        def _activity(samples=None, seconds=None, samples_per_sec=None):
            # Refresh the debug views. A reported `samples` count means a one-shot
            # BATCH finished → full_capture so the whole bounded burst is traceable
            # (start to end). Streaming activity (no samples) keeps the rolling
            # window. Also surface the throughput metric when present.
            self.server_activity.emit(samples is not None)
            if samples_per_sec is not None:
                self.server_throughput.emit({
                    "samples": samples, "seconds": seconds,
                    "samples_per_sec": samples_per_sec})

        # on_grc_params runs on the server thread → just emit a queued Qt signal
        # so the controller re-diffs on the GUI thread (Qt-free server contract).
        def _grc_params(params_by_block):
            self.grc_params_received.emit(params_by_block)

        # Debug hooks make breakpoints / speed / step first-class DURING a GRC
        # batch run (the burst runs server-side, not in the interactive loop).
        # breakpoint_check + on_sample run on the SERVER thread → keep them
        # Qt-free / queued. We seed the speed delay from the current slider.
        from engine.batch_debug import BatchDebugHooks

        def _bp_check(chip, sample_index):
            return self._batch_breakpoint_hit(chip, sample_index)

        def _on_sample(sample_index, paused):
            # Queued to the GUI thread: refresh the debug views per sample, and
            # surface a pause so the toolbar state reflects a breakpoint stop.
            self.server_activity.emit(False)
            if paused:
                self.state_changed.emit("paused")

        self._batch_debug = BatchDebugHooks(
            breakpoint_check=_bp_check, on_sample=_on_sample)
        self._batch_debug.set_delay(self._batch_debug_delay_for_speed())

        self._gr_server = SimServer(
            self.engine.chip, host=host, port=port,
            on_activity=_activity,
            on_reset=self._rehost_server_chip_threadsafe,
            on_before_batch=self._rebuild_if_dirty_threadsafe,
            default_entries=default_entries,
            on_grc_params=_grc_params,
            debug_hooks=self._batch_debug)
        bound = self._gr_server.start()
        self.state_changed.emit(f"gnuradio-server :{bound}")
        self.server_state.emit(bound)
        return bound

    def stop_gnuradio_server(self) -> None:
        if self._gr_server is not None:
            # Abort any in-flight batch (e.g. paused at a breakpoint) so the
            # server thread unblocks before we tear it down.
            if self._batch_debug is not None:
                self._batch_debug.stop()
            self._gr_server.stop()
            self._gr_server = None
            self._batch_debug = None
            # One final (unthrottled) refresh so the debug views settle on the
            # last window of activity.
            self.refresh_debug_from_chip(force=True)
            self.state_changed.emit("idle")
            self.server_state.emit(None)

    def refresh_debug_from_chip(self, *, force: bool = False,
                                full_capture: bool = False) -> None:
        """Push the live chip's current state into the debug views (called when
        the GNURadio server advances the chip).

        ``full_capture`` (set for a one-shot BATCH run): keep the ENTIRE batch
        trace — no rolling-window trim — so the user can see start AND end
        conditions of the bounded burst (essential for tracing startup/end batch
        behaviour). The default (streaming) path keeps the O(window) rolling trace.

        THROTTLED: the server fires this constantly under streaming; we coalesce
        to ~`_LIVE_REFRESH_HZ` so the GUI isn't swamped. The chip's trace is a
        bounded ring buffer (`_LIVE_TRACE_MAX`), so the rebuilt TraceModel /
        transaction log / waveform show only the most-recent window — cost stays
        O(window) no matter how long the stream runs. ``force`` (on stop) does a
        final refresh regardless of the throttle."""
        if self.engine is None:
            return
        import time
        now = time.monotonic()
        if not force and (now - getattr(self, "_last_server_refresh", 0.0)
                          < 1.0 / _LIVE_REFRESH_HZ):
            return
        self._last_server_refresh = now
        chip = getattr(self, "_sim_chip", 0)

        # INCREMENTAL window: the chip's max_records is a HARD CAP (it stops
        # recording when full, NOT a ring buffer), so we drain it each refresh —
        # ingest the new events into the TraceModel (which keeps the rolling
        # window), then CLEAR the chip trace so it resumes recording fresh
        # events. Without this, the trace freezes at the first _LIVE_TRACE_MAX
        # events and the views look stuck after the initial burst.
        try:
            new_events = list(self.engine.chip.get_trace())
        except Exception:  # noqa: BLE001
            new_events = []
        # At high sample rates a single refresh can drain tens of thousands of
        # events (≈64 per sample). Normalising all of them only to trim most away
        # is the dominant cost — keep just the most-recent window's worth of RAW
        # events before the expensive normalise/append. (The handshake/cell-state
        # overlay still scans the full batch, but that's cheap dict work.)
        # BATCH (full_capture): keep ALL events so the whole bounded burst — start
        # to end — is traceable. STREAMING: keep only the most-recent window's worth
        # before the expensive normalise/append (cost stays O(window) for an
        # unbounded stream).
        if full_capture:
            trimmed = new_events
        else:
            cap = self._live_trace_max
            trimmed = new_events[-cap:] if len(new_events) > cap else new_events

        # Cell-state overlay + handshakes from THIS batch of new events.
        states = self._states_from_events(new_events, chip)
        self.cell_states.emit(states)
        cells, ports = [], []
        for ev in new_events:
            k = ev.get("kind")
            cid = ev.get("cell_id")
            if k == "output_ready" and cid is not None and ev.get("face"):
                cells.append((chip, cid % self._width, cid // self._width,
                              ev["face"]))
            elif k in ("port_injection", "port_capture") and ev.get("port_name"):
                ports.append((chip, ev["port_name"]))
        self.handshakes.emit({"cells": cells, "ports": ports})

        # Append the new events to the rolling TraceModel window, trim, clear the
        # chip trace (resets the hard cap so recording continues).
        tm = self.trace_model
        tm.append_live(chip, trimmed, self._width)
        if not full_capture:
            tm.trim_to(self._live_trace_max)
        tm.set_cursor(tm.latest_ns())
        self.engine.clear_trace()
        self._trace_scan_reset()
        self.trace_updated.emit(tm)
        self.cell_state_refreshed.emit()

    def _states_from_events(self, events, chip):
        """Derive the cell-state overlay (executing/active) from a batch of trace
        events (live mode — we don't keep the full chip trace)."""
        from engine.simulator import CELL_ACTIVE, CELL_EXECUTING
        exec_cells, active_cells = set(), set()
        for ev in events:
            cid = ev.get("cell_id")
            if cid is None:
                continue
            if ev.get("kind") == "exec_tick":
                exec_cells.add(cid)
            elif ev.get("kind") in ("instr_arrival", "data_arrival",
                                    "output_ready"):
                active_cells.add(cid)
        out = {}
        for cid in active_cells | exec_cells:
            out[(chip, cid % self._width, cid // self._width)] = (
                CELL_EXECUTING if cid in exec_cells else CELL_ACTIVE)
        return out

    def _trace_scan_reset(self) -> None:
        """Reset the engine's handshake trace cursor (we cleared the chip trace,
        so the old index would be stale)."""
        if self.engine is not None and hasattr(self.engine, "_trace_cursor"):
            self.engine._trace_cursor = 0

    def _start_multi(self, result, stimulus) -> bool:
        """Set up a MultiChipSimEngine for a multi-chip project: load each chip's
        bitstream, wire the inter-chip connections, configure each chip's input
        port, and inject stimulus at the FIRST chip's input port."""
        from engine.simulator import MultiChipSimEngine

        project = self.app.project
        # Per-chip ChipType paths.
        paths: dict[int, str] = {}
        for chip in project.chips:
            tn = chip.type_name or project.chip_type
            paths[chip.id] = str(self.app.registry.require(tn).path)
        self.engine = MultiChipSimEngine(paths)
        # Inter-chip wires.
        for ic in project.inter_chip_connections:
            self.engine.connect(ic.from_chip, ic.from_port, ic.to_chip, ic.to_port)
        # Load + trace + configure each chip's input port.
        first_chip = project.chips[0].id
        first_port = None
        for chip in project.chips:
            self.engine.load(chip.id, result.words(chip.id), trace=True)
            cfg = self._input_port_config(chip.id)
            if cfg is not None:
                port, kw = cfg
                self.engine.configure_input_port(chip.id, port, **kw)
                if chip.id == first_chip:
                    first_port = port
        # Inject stimulus at the first chip's input port. Multi-chip injection
        # has no raw-word path in simkyt yet (single input port; downstream
        # chips daisy-chain), so a multi-chip run still uses the value-list path:
        # a loaded bitstream stimulus is NOT applied here. TODO: add a
        # MultiChipSimulation raw-word inject to unify on the bitstream path.
        if first_port is not None:
            values = stimulus if stimulus is not None else _default_ramp()
            self._input_samples = list(values)
            self.engine.inject(first_chip, first_port, self._input_samples)
        return True

    def _stimulus_words(self, port_cfg, *, override=None) -> list[int]:
        """The BITSTREAM words to inject this run (§stimulus).

        Precedence:
          1. A loaded ``.kbs`` stimulus → its raw words, injected verbatim (the
             words already encode WRITE/DATA/JUMP with hop + dest/entry, so a
             writes-then-reads stream needs no port config).
          2. An explicit value-list ``override`` → wrapped into WRITE+DATA+JUMP
             bursts via the design's port config.
          3. Nothing → the default ramp, likewise wrapped.

        Returns ``[]`` if there is no stimulus and no resolvable port config.
        """
        from engine.port_config import values_to_bitstream

        # 1. A loaded bitstream stimulus is a plain list of raw words.
        if self._stimulus:
            self._input_samples = list(self._stimulus)
            return list(self._stimulus)
        # 2/3. A value list (override) or the default ramp → wrap into bursts.
        values = override if override is not None else _default_ramp()
        self._input_samples = list(values)
        if port_cfg is None:
            return []
        _port, kw = port_cfg
        return values_to_bitstream(values, kw)

    def stop(self) -> None:
        self._timer.stop()
        self._running = False
        self._paused = False
        self.state_changed.emit("idle")

    def pause(self) -> None:
        # During a GRC batch run the burst runs in the server loop, not the
        # interactive timer — pause the hooks so it blocks at the next sample.
        if self._batch_debug is not None:
            self._batch_debug.pause()
            self.state_changed.emit("paused")
            return
        if self._running and not self._paused:
            self._timer.stop()
            self._paused = True
            self._rebuild_trace()  # let the debug views catch up while paused
            self.state_changed.emit("paused")

    def resume(self) -> None:
        if self._batch_debug is not None:
            self._batch_debug.resume()
            self.state_changed.emit("running")
            return
        if self._running and self._paused:
            self._paused = False
            self.state_changed.emit("running")
            self._timer.start()

    def toggle_pause(self) -> None:
        self.resume() if self._paused else self.pause()

    def step(self, mode: str = "event") -> None:
        """Single-step the simulation while paused/stopped.

        ``mode``:
          * ``"event"``       — advance exactly one engine event.
          * ``"instruction"`` — advance until the next instruction executes
                                 (a new ``exec_tick`` in the trace).
          * ``"handshake"``   — advance until the next data transfer (a new
                                 ``output_ready``).
        Multi-chip falls back to a bounded batch step (round-based; per-event
        granularity isn't meaningful across the inter-chip relay)."""
        # During a GRC batch run a "step" advances exactly one SAMPLE through the
        # server loop (per-event stepping isn't meaningful across the RPC).
        if self._batch_debug is not None:
            self._batch_debug.step()
            return
        if self.engine is None:
            return
        if self._multi or mode == "event":
            saved = self._batch
            if mode == "event" and not self._multi:
                self._batch = 1
            try:
                self._run_batch()
            finally:
                self._batch = saved
            self._rebuild_trace()  # single-step → debug views update immediately
            return
        self._step_until(mode)

    def _step_until(self, mode: str) -> None:
        """Single-chip: run small increments until a new ``exec_tick`` (mode
        ``instruction``) or ``output_ready`` (mode ``handshake``) appears, then
        refresh the overlay once."""
        target = "exec_tick" if mode == "instruction" else "output_ready"
        try:
            before = len(self.engine.chip.get_trace())
        except Exception:  # noqa: BLE001
            before = 0
        # Bounded so a stuck/idle sim can't spin forever.
        for _ in range(2000):
            info = self.engine.chip.run(max_events=1)
            if isinstance(info, dict):
                self._events += int(info.get("events_processed", 0))
                if info.get("stop_reason") == "QueueEmpty":
                    self._running = False
                    self.state_changed.emit("done")
                    break
            try:
                events = self.engine.chip.get_trace()
            except Exception:  # noqa: BLE001
                break
            if any(e.get("kind") == target for e in events[before:]):
                break
        self._emit_single_chip_frame()
        self._rebuild_trace()  # single-step → debug views update immediately

    def _emit_single_chip_frame(self) -> None:
        chip = getattr(self, "_sim_chip", 0)
        local = self.engine.cell_states(self._width)
        states = {(chip, x, y): s for (x, y), s in local.items()}
        self.cell_states.emit(states)
        # Live output faces for the cells active this frame (the crossover and
        # any MOVE [FACE] cell re-point at runtime → the arrow should follow).
        faces = self.engine.cell_faces(self._width, cells=list(local.keys()))
        self.cell_faces.emit({(chip, x, y): f for (x, y), f in faces.items()})
        hs = self.engine.handshakes(self._width)
        # Per-word steps (#194): each step is the cells+ports that transacted at
        # one sim-time. The canvas plays them back one-at-a-time (rolling wave)
        # rather than flashing the whole batch at once. Keep the flat cells/ports
        # union for backward compatibility.
        steps = [
            {
                "cells": [(chip, x, y, f) for (x, y, f) in s.get("cells", [])],
                "ports": [(chip, p) for p in s.get("ports", [])],
            }
            for s in hs.get("steps", [])
        ]
        self.handshakes.emit({
            "steps": steps,
            "cells": [(chip, x, y, f) for (x, y, f) in hs["cells"]],
            "ports": [(chip, p) for p in hs["ports"]],
        })
        self.metrics.emit({"events": self._events,
                           "time_ns": getattr(self.engine.chip,
                                              "simulation_time", 0.0)})
        # Live line highlight (#196) + stimulus-line breakpoints (#197): how many
        # stimulus words have been injected this frame.
        inj = self.engine.input_injection_count(
            getattr(self, "_sim_in_port", "x16_in"))
        self.injection_progress.emit(inj)
        self._inject_paused_this_frame = self._check_injection_breakpoint(inj)
        self._drain_output()
        # NOTE: the TraceModel is NOT rebuilt here. Rebuilding the full cumulative
        # trace (and re-rendering the Transaction Log) every animation frame
        # starves the flash-decay/paint timers (transit-cell flashes never get a
        # paint) and stacks up table relayouts (multi-second hangs). The trace is
        # rebuilt only when the run pauses/finishes or single-steps — see
        # _rebuild_trace() callers.

    def _rebuild_trace(self) -> None:
        """Rebuild the TraceModel from the current trace and notify debug views.

        The engine trace is cumulative, so we rebuild from scratch each frame
        (cheap for bounded traces; §debug §5)."""
        if self.engine is None:
            return
        tm = self.trace_model
        tm.clear()
        try:
            if self._multi:
                for cid, w in self.engine._widths.items():
                    tm.ingest(cid, self.engine._sim.get_trace(f"chip{cid}"), w)
            else:
                chip = getattr(self, "_sim_chip", 0)
                tm.ingest(chip, self.engine.chip.get_trace(), self._width)
        except Exception:  # noqa: BLE001 — trace not available
            return
        # The trace just advanced (a step/stop) — park the cursor at the live
        # edge so the Cell Inspector reads the freshest PC + registers, then
        # pulse the debug views to re-pull.
        tm.set_cursor(tm.latest_ns())
        self.trace_updated.emit(tm)
        self.cell_state_refreshed.emit()

    def set_cursor(self, ns: float) -> None:
        """Move the shared time cursor (e.g. a Transaction-Log row click) and
        pulse the debug views to re-render at that time."""
        self.trace_model.set_cursor(ns)
        self.cell_state_refreshed.emit()

    # -- live cell state (DEBUG §3.2 Cell Inspector live mode) -----------------

    def has_run(self) -> bool:
        """True when there is trace data to show live state from (so the
        Inspector shows live PC/registers rather than the static program).
        False after a reset clears the trace."""
        return bool(self.trace_model.transactions)

    def cell_live_state(self, chip: int, x: int, y: int) -> dict:
        """The selected cell's PC + register values at the current cursor.

        Returns ``{"pc": int|None, "registers": {addr: uint16}, "live": bool}``.
        ``pc`` is the most-recent exec_tick PC at/<= the cursor. Registers come
        from the engine's LIVE RAM for the single-chip cursor-at-latest case
        (truthful, includes self-computed values); otherwise from TraceModel
        reconstruction (external writes only). ``live`` flags which source was
        used (the Inspector shows a hint)."""
        tm = self.trace_model
        pc = tm.cell_pc_at(chip, x, y)
        # Live RAM read only makes sense when the cursor is at the latest state
        # (not scrubbed back in time) and we have a single-chip engine.
        at_latest = tm.cursor_ns >= tm.latest_ns()
        regs: dict[int, int] = {}
        live = False
        if at_latest and not self._multi and self.engine is not None \
                and chip == getattr(self, "_sim_chip", 0):
            regs = self.engine.read_cell_registers(x, y)
            live = bool(regs)
        if not regs:
            regs = tm.cell_registers_at(chip, x, y)
        return {"pc": pc, "registers": regs, "live": live}

    def reset(self) -> None:
        self.stop()
        self._events = 0
        self._captured = []
        # Breakpoints themselves persist across runs; only the per-run scan
        # cursors + recorded hits are cleared.
        self._bp_scan = {}
        self._bp_hits = []
        # Drop the host-side panel devices — start() rebuilds + re-registers them.
        self._panel_devices = {}
        self._panel_out_ports = []
        if self._gr_server is not None:
            # GNURadio server is hosting the chip: reset = rebuild + reload +
            # reconfigure a FRESH chip and re-point the live server at it (the
            # old chip carried run state; the server must serve a clean one).
            self._rehost_server_chip()
        elif self.engine is not None and hasattr(self.engine, "reset"):
            # Single-chip engine resets in place; the multi-chip engine is
            # rebuilt fresh on the next start() (no in-place reset needed).
            self.engine.reset()
        self.engine = None if (self._multi and self._gr_server is None) \
            else self.engine
        self.cell_states.emit({})  # clears the overlay
        self.output.emit({"chip": None, "port": None, "samples": []})
        # Drop the debug trace + live overlay so the Inspector reverts to the
        # static program (DEBUG §3.2).
        self.trace_model.clear()
        self.trace_updated.emit(self.trace_model)
        self.cell_state_refreshed.emit()

    def _rehost_server_chip_threadsafe(self):
        """Rebuild a fresh, port-configured chip and return it (no Qt signals —
        safe to call from the server thread for the 'reset' RPC). Returns the new
        chip, or None if the build failed / no engine.

        Also CLEARS the live trace window: the fresh chip restarts simulation
        time near 0, so leaving the old high-timestamp events in the TraceModel
        would make the new (low-timestamp) events sort before them and get
        trimmed away — the views would look frozen on the previous run (the
        Run/Stop/Run bug). Clearing makes the next run start from a clean
        window, exactly like Reset Sim."""
        result = self.app.build()
        if not result.ok or self.engine is None:
            return None
        self.engine.reset()                       # blank chip
        self.engine.load(result.words(0), trace=True,
                         max_records=_LIVE_CHIP_CAP)
        cfg = self._input_port_config(getattr(self, "_sim_chip", 0))
        if cfg is not None:
            port_name, kw = cfg
            self.engine.configure_input_port(port_name, **kw)
        # Clear the GUI-side rolling window + refresh throttle so the new run's
        # fresh events aren't sorted behind / trimmed by the previous run's.
        self.trace_model.clear()
        self._last_server_refresh = 0.0
        # This rebuild used the current design → record its version so the
        # pre-batch dirty check doesn't redundantly rebuild on the next batch.
        self._hosted_design_version = getattr(self.app.project, "design_version", 0)
        return self.engine.chip

    def _rebuild_if_dirty_threadsafe(self):
        """Called by the SimServer at the TOP of each process_batch (server
        thread). If the design was edited since the last build (the project's
        ``build_dirty`` flag — set by any placement/route/connection command),
        rebuild the hosted chip from the CURRENT project and return it so the
        batch runs the design as it stands NOW. Returns ``(chip_or_None,
        error_or_None)``:

          * not dirty            -> (None, None): keep the current chip (fast path).
          * dirty + build ok     -> (fresh_chip, None): re-host the rebuilt chip.
          * dirty + build fails  -> (None, "<DRC errors>"): ABORT the batch with
            the error rather than silently running a STALE chip (the bug where a
            deleted route still 'ran' because the server held the old build).

        Qt-free (no signals) so it is safe to run on the server thread, like
        :meth:`_rehost_server_chip_threadsafe`."""
        if self.engine is None:
            return None, None
        # Compare the live monotonic design_version to the version the server has
        # hosted. We do NOT use build_dirty here: the GUI's own post-edit
        # cached_build() (inspector/face refresh that fires right after a reroute)
        # CLEARS build_dirty before this GRC Run ever runs, so it would read False
        # and skip — the stale-run / phantom-cells bug. design_version is bumped on
        # every edit and never cleared by a build, so it survives that race.
        cur_ver = getattr(self.app.project, "design_version", 0)
        if self._hosted_design_version is not None and cur_ver == self._hosted_design_version:
            return None, None                      # design unchanged — fast path
        import sys
        print(f"[placeKYT server] design edited since last run (v{self._hosted_design_version}"
              f"→v{cur_ver}) — REBUILDING from the current design before this batch",
              file=sys.stderr, flush=True)
        result = self.app.build()                  # rebuild from current routes
        if not result.ok:
            errs = "; ".join(str(e) for e in result.errors) or "build failed"
            print(f"[placeKYT server] rebuild FAILED (edited design): {errs}",
                  file=sys.stderr, flush=True)
            return None, f"placeKYT build error (edited design): {errs}"
        print(f"[placeKYT server] rebuilt OK — re-hosting {len(result.words(0))} "
              "words for this run", file=sys.stderr, flush=True)
        # Re-host the freshly built bitstream on a clean chip + re-configure the
        # input port, exactly like the reset path. Clear the trace window so the
        # new run's low-timestamp events aren't sorted behind the previous run's.
        self.engine.reset()
        self.engine.load(result.words(0), trace=True, max_records=_LIVE_CHIP_CAP)
        cfg = self._input_port_config(getattr(self, "_sim_chip", 0))
        if cfg is not None:
            port_name, kw = cfg
            self.engine.configure_input_port(port_name, **kw)
        self.trace_model.clear()
        self._last_server_refresh = 0.0
        self._hosted_design_version = cur_ver   # remember what we just hosted
        # Tell the GUI (queued) to FULL-render the canvas so the displayed cells
        # match this freshly-built chip — clears any routing cells left over from a
        # route the user edited since the server started (the "phantom blue boxes").
        self.chip_rehosted.emit()
        return self.engine.chip, None

    def _rehost_server_chip(self) -> None:
        """Rebuild a fresh chip and re-point the running GNURadio server at it.
        Used by reset() (GUI thread) so a second flowgraph run starts clean."""
        new_chip = self._rehost_server_chip_threadsafe()
        if new_chip is not None and self._gr_server is not None:
            self._gr_server.set_chip(new_chip)

    # -- stepping -------------------------------------------------------------

    def _tick(self) -> None:
        if self.engine is None:
            self.stop()
            return
        self._run_batch()

    def _has_active_breakpoints(self) -> bool:
        return any(bp.enabled for bp in self.breakpoints.breakpoints)

    def _effective_batch(self) -> int:
        """Events to run this batch. With active breakpoints we run ONE event at
        a time so the run can stop AT the hit — otherwise a large batch (e.g.
        2000 at default speed) runs the whole sim past the breakpoint before the
        scan ever sees it (the 'breaks late' bug). No breakpoints → full speed.

        (#193) The earlier per-held-word batch cap for panels is GONE: the panel
        is now an in-fabric handshake node and `run()` SELF-PUMPS it inside the
        engine, so a held word is serviced WITHIN the batch — there is no
        read-before-commit even at full batch size."""
        if self._has_active_breakpoints():
            return 1
        return self._batch

    def _setup_panels(self) -> None:
        """Build a host-side SramPanelDevice for each SRAM panel and REGISTER it
        with the engine as an IN-FABRIC handshake node (#193). Each panel INPUT
        wires to a chip OUTPUT port (the panel reads triggers/data there); each
        panel OUTPUT wires to a chip INPUT port (the panel pushes read results
        there). `chip.register_panel(out_port, in_port, dev)` marks out_port
        HELD-ACK and makes `run()` SELF-PUMP the panel — the host no longer pumps
        a PanelDriver between batches; it only drains activity for the visuals."""
        self._panel_devices = {}
        self._panel_out_ports = []   # chip output ports feeding registered panels
        project = self.app.project
        if project is None or not project.panels or self._multi:
            return  # multi-chip panel pumping not wired yet
        chip = self.engine.chip if self.engine else None
        if chip is None:
            return
        from engine.sram_panel import SramPanelDevice
        from model.enums import PortDirection
        for panel in project.panels:
            dev = SramPanelDevice(size_words=panel.size_words,
                                  addr_regs=panel.address_regs)
            self._panel_devices[panel.id] = dev
            # Resolve the chip output port (panel-input side) and chip input
            # port (panel-output side) from the panel connections.
            out_port = in_port = None
            for pc in project.panel_connections_for(panel.id):
                pport = panel.port(pc.panel_port)
                if pport is None:
                    continue
                if pport.direction == PortDirection.INPUT:
                    out_port = pc.chip_port     # chip OUTPUT feeds panel input
                else:
                    in_port = pc.chip_port      # chip INPUT receives panel output
            if out_port is None:
                continue  # nothing to read from → panel is inert
            # Register the panel in-fabric: run() self-pumps it (drains out_port,
            # applies WRITEs/JUMP-triggers to dev, injects push-reads into in_port,
            # releases the held ack). register_panel marks out_port held-ack.
            try:
                chip.register_panel(out_port, in_port or out_port, dev)
                self._panel_out_ports.append(out_port)
            except Exception:  # noqa: BLE001 — older simkyt w/o register_panel
                # Fall back to held-ack so a host pump (if any) still works.
                try:
                    chip.set_port_handshake(out_port, True)
                except Exception:  # noqa: BLE001
                    pass

    def _pump_panels(self) -> int:
        """The engine now SELF-PUMPS registered panels inside `run()` (#193); the
        host only drains panel ACTIVITY here for the blink + inspector visuals.
        Returns 0 (no host-injected work — the engine does the injection)."""
        if not self._panel_devices:
            return 0
        acts = {pid: dev.take_activity()
                for pid, dev in self._panel_devices.items()}
        acts = {pid: a for pid, a in acts.items() if a}
        if acts:
            self.panel_activity.emit(acts)
        return 0

    def panel_device(self, panel_id: int):
        """The live SramPanelDevice for a panel (for the inspector), or None."""
        return self._panel_devices.get(panel_id)

    def _run_batch(self) -> None:
        if self._multi:
            self._run_batch_multi()
            return
        info = self.engine.chip.run(max_events=self._effective_batch())
        pushed = self._pump_panels()   # drain panel traffic + inject push-reads
        if isinstance(info, dict):
            self._events += int(info.get("events_processed", 0))
        self._emit_single_chip_frame()
        # A stimulus-line breakpoint (#197) paused inside the frame emit — stop.
        if getattr(self, "_inject_paused_this_frame", False):
            return
        # Breakpoint check (DEBUG §3.6): if a watched condition fired in the new
        # trace events, pause the run at the hit.
        if self._check_breakpoints(getattr(self, "_sim_chip", 0),
                                   self.engine.chip.get_trace(), self._width):
            return
        # simkyt run() returns a dict; QueueEmpty means nothing left to do —
        # BUT keep running if (a) the panel just injected push-reads (those
        # bursts must still transit out), or (b) a held-ack panel port still has
        # a cell stalled awaiting the panel's release (the controller is paused
        # mid-handshake, not finished). Otherwise the no-FIFO backpressure would
        # look like a finished run.
        if (isinstance(info, dict) and info.get("stop_reason") == "QueueEmpty"
                and not pushed and not self._panel_acks_pending()):
            self._timer.stop()
            self._running = False
            self._rebuild_trace()  # populate the debug views now the run is done
            self.state_changed.emit("done")

    def _panel_acks_pending(self) -> bool:
        """True if any registered panel's chip output port has a held ack
        outstanding (a cell stalled mid-handshake awaiting the panel). With the
        in-fabric panel (#193) run() self-pumps, but a held word can still be
        pending at the boundary between batches; the run loop must not call the
        run done until it clears."""
        chip = self.engine.chip if self.engine else None
        if chip is None:
            return False
        for out_port in getattr(self, "_panel_out_ports", []):
            try:
                if chip.port_ack_pending(out_port):
                    return True
            except Exception:  # noqa: BLE001 — older chip without the API
                pass
        return False

    def _check_breakpoints(self, chip: int, events: list, width: int) -> bool:
        """Scan the chip's NEW trace events (since the last scan) for a fired
        breakpoint. On a hit: pause, rebuild the trace, park the cursor at the
        hit time, record it for the scrubber, and emit ``breakpoint_hit``.
        Returns True if the run was paused by a hit."""
        if not self.breakpoints.breakpoints:
            self._bp_scan[chip] = len(events)
            return False
        start = self._bp_scan.get(chip, 0)
        new = events[start:]
        self._bp_scan[chip] = len(events)
        hit = self.breakpoints.first_hit(chip, new, width)
        if hit is None:
            return False
        # Pause the run AT the hit. Rebuild so the debug views see up-to-here,
        # then park the cursor at the hit time.
        self._timer.stop()
        self._paused = True
        self._rebuild_trace()
        self.trace_model.set_cursor(hit.time_ns)
        self._bp_hits.append(hit)
        self.cell_state_refreshed.emit()
        self.breakpoint_hit.emit(hit)
        self.state_changed.emit("paused")
        return True

    def breakpoint_hit_times(self) -> list[float]:
        """Times of breakpoints that fired this run (for scrubber markers)."""
        return [h.time_ns for h in self._bp_hits]

    # -- stimulus-line breakpoints (#197) -------------------------------------

    def toggle_injection_breakpoint(self, line: int) -> bool:
        """Toggle a breakpoint on stimulus word ``line`` (its disassembly line).
        The run pauses when that word is injected. Returns the new state (True =
        breakpoint set)."""
        if line in self._inject_breakpoints:
            self._inject_breakpoints.discard(line)
            return False
        self._inject_breakpoints.add(line)
        return True

    def injection_breakpoints(self) -> set[int]:
        return set(self._inject_breakpoints)

    def clear_injection_breakpoints(self) -> None:
        self._inject_breakpoints.clear()

    def _check_injection_breakpoint(self, count: int) -> bool:
        """If injecting word(s) up to ``count`` crossed a stimulus-line
        breakpoint, pause the run AT the first such word. Returns True if paused.
        ``count`` is the cumulative injected-word count this frame; a breakpoint
        on line ``i`` fires when word ``i`` injects (i.e. count reaches i+1)."""
        if not self._inject_breakpoints or count <= self._last_inject_count:
            self._last_inject_count = count
            return False
        # Lines that became "injected" since the last frame: [last, count).
        fired = sorted(b for b in self._inject_breakpoints
                       if self._last_inject_count <= b < count)
        self._last_inject_count = count
        if not fired:
            return False
        line = fired[0]
        self._timer.stop()
        self._paused = True
        self._rebuild_trace()
        self.injection_progress.emit(line + 1)      # mark the stopped line
        self.injection_breakpoint_hit.emit(line)
        self.state_changed.emit("paused")
        return True

    def _run_batch_multi(self) -> None:
        # One batch = a bounded number of inter-chip rounds. cell_states already
        # comes back keyed by (chip_id, x, y). With active breakpoints, run a
        # single event per chip per round so we can stop AT the hit.
        rounds = 1 if self._has_active_breakpoints() else 4
        info = self.engine.run(self._effective_batch(), rounds=rounds)
        if isinstance(info, dict):
            self._events += int(info.get("total_events", 0))
        self.cell_states.emit(self.engine.cell_states())
        self.handshakes.emit(self.engine.handshakes())  # {"cells":…, "ports":…}
        self.metrics.emit({"events": self._events, "time_ns": 0.0})
        self._drain_output()
        # Breakpoint check per chip (DEBUG §3.6).
        if self.breakpoints.breakpoints:
            for cid in self.engine._chip_ids:
                width = self.engine._widths.get(cid, 10)
                try:
                    evs = self.engine._sim.get_trace(f"chip{cid}")
                except Exception:  # noqa: BLE001
                    continue
                if self._check_breakpoints(cid, evs, width):
                    return
        # Trace rebuilt on stop/done only (see _emit_single_chip_frame note).
        # Done when all chips are idle (completed) and no events advanced.
        if isinstance(info, dict) and info.get("completed") \
                and int(info.get("total_events", 0)) == 0:
            self._timer.stop()
            self._running = False
            self._rebuild_trace()
            self.state_changed.emit("done")

    def _drain_output(self) -> None:
        """Drain whatever new samples reached the output port this batch and emit
        the accumulated list (``capture`` consumes the buffer, so accumulate)."""
        tgt = self._output_target()
        if self.engine is None or tgt is None:
            return
        chip_id, port = tgt
        try:
            new = (self.engine.capture(chip_id, port) if self._multi
                   else self.engine.capture(port))
        except Exception:  # noqa: BLE001
            return
        if new:
            self._captured.extend(new)
            self.output.emit({"chip": chip_id, "port": port,
                              "samples": list(self._captured)})

    # -- helpers --------------------------------------------------------------

    def _input_port_config(self, chip_id: int = 0):
        """(port_name, {entry_addr, hop_count, data_addr}) for the block fed by
        ``chip_id``'s input port, or None. Delegates to the Qt-free helper so the
        GUI sim and the CLI build derive identical port config."""
        from engine.port_config import input_port_config
        return input_port_config(
            self.app.project, self.app.registry, self.app.catalog, chip_id)

    def _output_target(self):
        """(chip_id, port_name) of the design's final output port, or None."""
        from engine.port_config import output_port_target
        return output_port_target(self.app.project)


def _default_ramp() -> list[int]:
    return [0x1000, 0x2000, 0x3000, 0x4000, 0x5000, 0x6000, 0x4000, 0x2000]
