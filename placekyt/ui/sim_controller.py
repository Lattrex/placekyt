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
# LOGARITHMIC speed ladder. Each tuple is
#   (events_per_tick, tick_ms, flash_steps_per_tick).
# The scale is GEOMETRIC (~3–5x per step), not linear: the slow end needs fine
# resolution to step-watch one transaction at a time, while the fast end must
# jump by big multiples to fast-forward through a long run in seconds (a debug
# tool can't take minutes to reach the interesting point).
#
# In LOCKSTEP (animation on), the chip is paced by how fast the animation drains
# each sample's flash queue — i.e. flash_steps_per_tick, NOT events_per_tick — so
# the high end sets a LARGE flash_steps_per_tick to release the frame gate fast
# (top step effectively drains a whole sample per frame). In the non-animated /
# interactive path, events_per_tick is the chip's batch size per frame.
SPEED_STEPS = [
    (1,      800,  1),      # 0: slowest — one transaction at a time
    (2,      400,  1),      # 1
    (5,      200,  1),      # 2
    (15,     120,  2),      # 3
    (50,      80,  4),      # 4
    (200,     50,  12),     # 5
    (1000,    33,  40),     # 6
    (5000,    33,  200),    # 7
    (25000,   33,  1000),   # 8 (fastest) — fast, still animates the wave
]
# NB: an old top rung (150000 events/frame, whole-sample-per-frame flash) was
# REMOVED — it was so fast the animation could not drain a sample before the next
# was computed, so lockstep wedged and the GUI hung until the batch finished. The
# fastest surviving rung (8) still animates the waveform in step.
DEFAULT_SPEED = 7

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
# Server (GRC batch) mode hosts a BOUNDED burst whose trace must be retained in
# full. The chip-side cap is a HARD stop (not a ring), so it must comfortably
# exceed a whole burst's events (≈64 events/sample) between refreshes — sized for
# a large batch without a mid-burst recording halt.
_SERVER_CHIP_CAP = 5_000_000
# A GRC batch is retained WHOLE (no rolling trim) so the user can see the burst
# start-to-end. But it must still be BOUNDED: without a cap the TraceModel grows
# without limit ACROSS successive Runs (run 1: 1.6k txns, run 2: 327k, run 3:
# 473k, …) and every subsequent refresh — even a 0-event idle tick — re-touches
# that whole model on the GUI thread (~215 ms each), stacking into a multi-second
# freeze. This caps a single batch's retained trace at a generous-but-bounded
# size so one burst stays fully visible without the cross-run blow-up.
_SERVER_BATCH_TRACE_MAX = 400_000
_LIVE_REFRESH_HZ = 8        # cap debug refreshes/sec during streaming

# GUI-side PULL pacing for a GRC batch run (the animation-OFF / full-speed
# path). The server thread does NO per-sample GUI signaling: per-sample queued
# emits arrive faster than Qt can drain + repaint them, so the event loop never
# gets a paint turn and the window freezes (the root cause of the GRC-run
# freeze — two prior fixes died on exactly this). Instead a QTimer OWNED BY THE
# GUI THREAD fires at ~30 Hz while the server is hosted and PULLS whatever the
# chip produced since the last tick. The GUI paces ITSELF — it cannot be
# flooded — and Qt naturally interleaves repaints between ticks.
_SERVER_PULL_MS = 33
# Bound ONE pull-tick's TraceModel ingest: at most this many drained events are
# normalised + appended per refresh; the residual (already pulled off the chip
# into the GUI-thread-only _pending_batch_events buffer) carries to the NEXT
# tick — it is never re-drained, only advanced through. force=True (batch end /
# server stop) ingests everything so the final settled trace is whole.
_PULL_MAX_EVENTS_PER_TICK = 20_000
# Adaptive refresh back-off: after a refresh whose total cost (append + the
# synchronous trace_updated view re-renders) was T seconds, wait at least
# _REFRESH_BACKOFF*T before the next non-forced refresh. On a huge retained
# batch one re-render can take hundreds of ms; the back-off keeps refresh work
# to a bounded fraction (~1/(1+backoff)) of GUI time so painting always gets a
# turn — the window stays responsive even late in a giant burst.
_REFRESH_BACKOFF = 3.0


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
    server_activity = Signal(bool, bool)   # args: (full_capture, force)
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
        # IDENTITY of the project the server currently has hosted. design_version
        # is per-project and starts at 0 for every freshly-loaded design, so it is
        # NOT comparable across projects: loading a DIFFERENT design (e.g. gain
        # after the SSB transceiver) on the same running server can leave
        # cur_ver == _hosted_design_version by coincidence, taking the no-rebuild
        # fast path and injecting the OLD design's stale entry/hop/stream_targets
        # → the new design gets NO output. Tracking id(project) forces a rebuild +
        # target re-resolve whenever the design object itself changes.
        self._hosted_project_id: int | None = None
        from engine.trace_model import TraceModel

        self.trace_model = TraceModel()  # the debug data spine (§debug)

        from engine.breakpoints import BreakpointSet

        # Active breakpoints (DEBUG §3.6) + per-chip scan cursors so each new
        # trace event is checked for a hit exactly once.
        self.breakpoints = BreakpointSet()
        self._bp_scan: dict[int, int] = {}
        # GRC-batch breakpoint scan cursors (breakpoint mode): the server thread's
        # scan of the chip trace (_batch_bp_scan) and the GUI drain's tail cursor
        # (_gui_trace_scan). Both reset at a Run boundary (see _new_run). Only used
        # when breakpoints are armed on a hosted GRC run.
        self._batch_bp_scan: int = 0
        self._gui_trace_scan: int = 0
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
        # True while a GRC server is hosting the chip. A GRC run sends a BOUNDED
        # burst (the whole batch), so the waveform must retain the ENTIRE trace —
        # NOT the rolling window used for an unbounded interactive stream. When
        # set, refresh_debug_from_chip keeps every drained event (no trim) so the
        # user sees ALL samples, same as the GRC waveform window. (Without this,
        # the per-sample refreshes trimmed to the rolling window and only the tail
        # survived — the reported bug.)
        self._server_batch_retain_all = False
        # SINGLE-WRITER TRACE OWNERSHIP: the TraceModel is mutated ONLY on the GUI
        # thread (in refresh_debug_from_chip). The server-thread batch callbacks
        # (_rebuild_if_dirty_threadsafe / _rehost_server_chip_threadsafe) must NOT
        # touch it — they only REQUEST a reset by setting this flag. The next
        # refresh_debug_from_chip (GUI thread) consumes the flag and clears the
        # model there, so the clear and the subsequent append can never interleave
        # across threads. This is the fix for the long-standing intermittent
        # waveform display (server-thread clear() racing the GUI-thread append →
        # partial / TX-only / delayed-then-late traces). A bool assignment is
        # atomic under the GIL, so no lock is needed for this one-way flag.
        self._pending_trace_reset = False
        # PER-RUN TIME REBASE: the chip's sim clock keeps CLIMBING across GRC Runs
        # (it is never reset to 0 between Runs — only the trace model is cleared).
        # Without rebasing, each Run's events land at an ever-larger absolute
        # time_ns, so successive Runs march off to the right of the waveform axis
        # (5M ns, 10M ns, 22M ns …) with huge empty gaps. On a new Run we set this
        # origin to None; the first drained event's timestamp becomes the origin,
        # and every event's time_ns has it subtracted — so each Run's traces start
        # near 0 and both streams overlay on a short window (like GRC's own sink).
        self._trace_time_origin: float | None = None
        # Host-side SRAM panel devices, registered in-fabric with the engine
        # (#193): run() self-pumps them. {panel_id: SramPanelDevice}; the chip
        # output ports feeding registered panels (for ack-pending checks).
        self._panel_devices: dict = {}
        self._panel_out_ports: list = []
        # Live trace window size (events kept in the rolling debug view) — user
        # configurable (Simulation → Live Window Size).
        self._live_trace_max = _LIVE_TRACE_MAX
        # CELL ANIMATION (the "Enable cell animation" toolbar checkbox). OFF by
        # default: the chip runs FLAT OUT and NO per-frame cell-state / face /
        # handshake visuals are emitted (zero GUI overhead) — the fast path. When
        # ON, the run steps in LOCKSTEP with the canvas animation clock so every
        # executing cell, transit, and face change is shown faithfully as it
        # happens (a debug instrument: a stall shows as the fabric ceasing to
        # flow, live). The speed slider only matters (and is only enabled) when
        # this is ON. See set_animate_cells / animate_cells.
        self._animate_cells = False
        # GUI-side PULL timer for a GRC server run (see _SERVER_PULL_MS): started
        # by start_gnuradio_server, stopped by stop_gnuradio_server. It lives on
        # the GUI thread, so each tick is just one more event-loop turn — Qt
        # interleaves repaints between ticks and the GUI can never be handed
        # work faster than it chooses to take it.
        self._server_pull_timer = QTimer(self)
        self._server_pull_timer.setInterval(_SERVER_PULL_MS)
        self._server_pull_timer.timeout.connect(self._server_pull_tick)
        # Drained-but-not-yet-ingested trace events (the pull residual). GUI
        # thread ONLY — refresh_debug_from_chip is its single reader/writer.
        # Dropped on a Run-boundary trace reset (it belongs to the old Run).
        self._pending_batch_events: list = []
        # Wall-clock cost of the last refresh (drives the adaptive back-off).
        self._last_refresh_cost = 0.0

    def animate_cells(self) -> bool:
        """Whether cell-execution/transit animation is enabled (toolbar checkbox)."""
        return self._animate_cells

    def set_animate_cells(self, on: bool) -> None:
        """Enable/disable cell animation (the toolbar checkbox). OFF ⇒ the run is
        full-speed with no visual emission; ON ⇒ lockstep animated run. Changing
        it mid-run takes effect on the next frame/refresh."""
        self._animate_cells = bool(on)
        # LOCKSTEP: when a GRC batch run is hosted, gate the server's per-sample
        # loop on the animation so the chip steps in lockstep with what's shown.
        # OFF ⇒ release any waiter so a mid-run toggle doesn't wedge the burst.
        if self._batch_debug is not None:
            self._batch_debug.set_lockstep(self._lockstep_active())

    def batch_frame_done(self) -> None:
        """Called on the GUI thread (wired from the canvas flash-drained hook)
        when the current sample's fabric animation has finished — release the
        server's per-sample loop to compute the next sample. No-op unless a GRC
        batch is hosted with lockstep active."""
        if self._batch_debug is not None:
            self._batch_debug.frame_done()

    def _server_pull_tick(self) -> None:
        """One GUI-thread pull tick while a GRC server is hosted: pull whatever
        the chip has produced since the last tick and do ONE bounded refresh
        (delta only — see _PULL_MAX_EVENTS_PER_TICK / _REFRESH_BACKOFF inside
        refresh_debug_from_chip). This is the animation-OFF live view: the
        waveform builds progressively while the server thread runs flat out,
        and because the timer fires on the GUI thread, repaints interleave
        naturally between ticks — the window cannot be flooded. An idle tick
        (nothing drained, nothing pending) returns in microseconds."""
        if self._gr_server is None:
            self._server_pull_timer.stop()
            return
        import os as _os
        if _os.environ.get("KYTTAR_PERF_DEBUG") == "1":
            import sys as _sysP, time as _tP
            _now = _tP.monotonic()
            _last = getattr(self, "_dbg_last_tick", None)
            _gap = (None if _last is None else round((_now - _last) * 1000))
            self._dbg_last_tick = _now
            _t0 = _tP.perf_counter()
            self.refresh_debug_from_chip(full_capture=self._server_batch_retain_all)
            _dt = round((_tP.perf_counter() - _t0) * 1000)
            _tm = self.trace_model
            _sysP.stderr.write(
                f"[PERF] pull_tick gap={_gap}ms refresh={_dt}ms "
                f"model_txns={len(getattr(_tm,'transactions',[]))} "
                f"pending={len(getattr(self,'_pending_batch_events',[]))} "
                f"last_cost={round(getattr(self,'_last_refresh_cost',0)*1000)}ms "
                f"animate={self._animate_cells}\n")
            _sysP.stderr.flush()
            return
        self.refresh_debug_from_chip(full_capture=self._server_batch_retain_all)

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
    def batch_paused(self) -> bool:
        """True when a GRC-server BATCH is blocked (a breakpoint hit or a user
        pause), waiting on the debug hooks. This is DISTINCT from ``running`` /
        ``paused`` — a GRC batch runs on the server loop, so ``_running`` is False
        and the pause state lives in ``_batch_debug``, not ``_paused``. The GUI
        Run/F5 handler uses this to Resume a batch stopped at a breakpoint instead
        of trying to start a new interactive run (the reported 'Run does nothing
        after a breakpoint' bug)."""
        bd = self._batch_debug
        return (self._gr_server is not None and bd is not None
                and bool(getattr(bd, "is_paused", False)))

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
            # TOP SPEED = FAST-FORWARD: at the max slider step, drop strict
            # lockstep so the chip is not gated per-sample on the animation (which
            # bottlenecks a long run to minutes). The animation still plays a
            # coarse wave, but the run reaches the end quickly — what you want when
            # fast-forwarding to the interesting point. Below the top, keep true
            # lockstep so a stall stays visible.
            self._batch_debug.set_lockstep(self._lockstep_active())

    def _lockstep_active(self) -> bool:
        """True when the chip should step in strict lockstep with the animation:
        cell animation ON and NOT at the top (fast-forward) speed step."""
        return self._animate_cells and self._speed_index < len(SPEED_STEPS) - 1

    def _batch_debug_delay_for_speed(self) -> float:
        """Per-sample delay (seconds) for the current speed index in a GRC batch
        run. The slow end pauses ~0.3 s/sample (slow-motion, one sample visible);
        the fast end runs with no wait. Derived from the slider's tick interval so
        it tracks the same ladder the interactive animation uses."""
        # Fastest steps → no artificial delay (flat-out); the batch loop still
        # yields the GIL periodically so the GUI-side pull timer keeps painting
        # (see process_batch). Below that, scale the tick interval into a pause.
        if self._speed_index >= 7:
            return 0.0
        tick_ms = SPEED_STEPS[self._speed_index][1]
        return min(0.4, tick_ms / 1000.0)

    def _batch_breakpoint_hit(self, chip, sample_index: int) -> bool:
        """Server-thread breakpoint check for a GRC batch sample. Reuses the same
        BreakpointSet as the interactive run, evaluated against the hosted chip's
        NEW trace events (since the last scan). Returns True if any enabled
        breakpoint fired (the batch loop then pauses). Qt-free — touches engine
        only.

        Scans the chip's own trace via get_trace() with a private scan cursor
        (_batch_bp_scan). The GUI-thread refresh drains the SAME trace but — while
        breakpoints are active — does NOT clear it (see refresh_debug_from_chip),
        so this scan sees every event exactly once. The cursor is clear-resilient:
        if the trace got SHORTER than the cursor (a Run-boundary clear on the
        server thread, or a rare race), the cursor resets to 0 so no event is
        skipped. The earlier version called chip.drain_trace()/trace_events(),
        which don't exist on the hosted chip → it always excepted → breakpoints
        NEVER fired on the GRC path (they only ever worked on the interactive
        standalone run). This wires them up."""
        if not self.breakpoints.breakpoints:
            return False
        chip_obj = self._gr_server._chip if self._gr_server is not None else None
        if chip_obj is None:
            return False
        try:
            sim_chip = getattr(self, "_sim_chip", 0)
            events = list(chip_obj.get_trace())
            start = getattr(self, "_batch_bp_scan", 0)
            if start > len(events):        # trace was cleared under us — restart
                start = 0
            new = events[start:]
            self._batch_bp_scan = len(events)
            hit = self.breakpoints.first_hit(sim_chip, new, self._width)
        except Exception:  # noqa: BLE001 — a faulty scan must never wedge the burst
            return False
        if hit is not None:
            self._bp_hits.append(hit)
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
        project = self.app.project
        # FOOLPROOF: enabling the server BEFORE importing a design must still bind
        # the port. On an empty project (no chip yet) the build fails only because
        # there is nothing to place — NOT a real design error. Host a placeholder
        # chip of the default type so GRC can connect to :58950 right now; the
        # first batch after the user imports triggers _rebuild_if_dirty_threadsafe,
        # which rebuilds the now-routed chip and re-resolves stream_targets. This
        # makes the enable-server-then-import order produce the SAME result as
        # import-then-enable (order independence — the user's requirement).
        empty_project = len(project.chips) == 0
        if not result.ok and not empty_project:
            self.state_changed.emit(f"error: {len(result.errors)} DRC error(s)")
            return None
        if len(project.chips) > 1:
            self.state_changed.emit("error: GNURadio server is single-chip only")
            return None

        # Hosting for a GRC client → bounded batches → retain the WHOLE trace in
        # the waveform (set before engine.load so the chip-side cap is sized for
        # a full burst, not the rolling-stream window).
        self._server_batch_retain_all = True

        chip0 = project.chip(0)
        type_name = (chip0.type_name if chip0 and chip0.type_name
                     else project.chip_type)
        if not type_name:
            # Empty-project placeholder (server enabled before import): no chip
            # exists yet, so host the registry's default type. The real design
            # replaces it on the first post-import batch (dirty-rebuild).
            known = self.app.registry.names()
            type_name = "kyttar_10x12" if "kyttar_10x12" in known else known[0]
        entry = self.app.registry.require(type_name)
        self._width = entry.chip_type.width
        self._multi = False
        self.engine = SimulationEngine(entry.path)
        # A GRC run sends a BOUNDED batch and the waveform must retain it ALL, so
        # in server mode the chip-side trace cap is generous (it must not stop
        # recording mid-burst between refreshes — its hard cap halts recording
        # when full, it is NOT a ring buffer). An interactive stream is unbounded,
        # so it keeps the smaller rolling cap. The TraceModel-side retention is
        # handled by _server_batch_retain_all in refresh_debug_from_chip.
        chip_cap = _SERVER_CHIP_CAP if self._server_batch_retain_all else _LIVE_CHIP_CAP
        self.engine.load(result.words(0), trace=True, max_records=chip_cap)
        # Server now hosts the design at this version; the pre-batch check rebuilds
        # only when the live design_version moves past it (i.e. an edit happened).
        self._hosted_design_version = getattr(self.app.project, "design_version", 0)
        self._hosted_project_id = id(self.app.project)
        self._sim_chip = chip0.id if chip0 else 0
        # Empty-project placeholder: there is no design to resolve injection
        # targets for yet. Skip port-config + stream_targets resolution (they
        # would fail on the absent chip type) — the first post-import batch's
        # dirty-rebuild re-resolves EVERYTHING against the real design.
        cfg = (None if empty_project
               else self._input_port_config(self._sim_chip, build_result=result))
        default_entries: dict[str, int] = {}
        default_hops: dict[str, int] = {}
        if cfg is not None:
            port_name, kw = cfg
            self.engine.configure_input_port(port_name, **kw)
            # Remember the resolved entry so the bridge can default to it when a
            # GRC injects without specifying jump_entry (blocks whose entry != 0,
            # e.g. the coherent-RX phase cell at 17, then work over the bridge).
            if "entry_addr" in kw:
                default_entries[port_name] = int(kw["entry_addr"])
            # And the resolved HOP (31 - distance to the block's landing cell):
            # the bridge MUST inject the batch at this hop, NOT a hardcoded 30
            # (INV-1). A block not 1 hop from the port (any non-edge placement,
            # and what the resync/auto-place produces) otherwise never executes —
            # the WRITE/JUMP is consumed at the wrong cell and output is empty.
            if "hop_count" in kw:
                default_hops[port_name] = int(kw["hop_count"])
        # SHARED-INPUT-PORT DUPLEX: resolve EVERY x16_in→block input net that
        # carries a stream_id to its injection params (entry/hop/data-addrs/out_tag),
        # keyed by stream_id, so two GR sources sharing the input port each reach
        # their own block over the bridge. Single-stream nets (no stream_id) are
        # skipped here and use the default_entries/default_hops path above.
        from engine.port_config import stream_targets as _stream_targets_fn
        stream_targets = ({} if empty_project else _stream_targets_fn(
            self.app.project, self.app.registry, self.app.catalog, self._sim_chip,
            build_result=result))
        # PACKET-BOUNDARY LOOP-MEMORY RESET: resolve the per-batch state resets from
        # the SAME build result (mirrors stream_targets). The build derived them from
        # the placed design's ``reset_per_batch`` StateVars; the SimServer cold-starts
        # each at the top of every process_batch so repeated GRC "Run" presses on this
        # persistently-hosted chip each recover a fresh packet from a cold receiver
        # (Costas/Gardner/matched-filter loops) instead of the previous packet's lock.
        from engine.port_config import batch_reset_writes as _batch_reset_writes_fn
        reset_writes = ([] if empty_project
                        else _batch_reset_writes_fn(result, self._sim_chip))
        # OBSERVABILITY: report what the server resolved at start-up. An EMPTY map
        # is why a duplex run injects at the entry=0/hop=30 single-stream fallback
        # and gets 0 words — it means no x16_in→block input net carried a stream_id
        # (e.g. a hand-edited project, or a design that wasn't imported from a
        # stream-tagged .grc). Listing the input nets' stream_ids makes the cause
        # obvious from the server console instead of a silent flat run.
        import sys as _sys
        from model.connection import ChipPortEndpoint as _CPE, BlockEndpoint as _BE
        _in_sids = [(getattr(c, "stream_id", None), getattr(c.target, "block", None))
                    for c in self.app.project.connections
                    if isinstance(c.source, _CPE) and isinstance(c.target, _BE)]
        _sys.stderr.write(
            f"[placeKYT server] stream_targets resolved: "
            f"{ {k: (v['entry_addr'], v['hop_count'], v['out_tag']) for k, v in stream_targets.items()} } "
            f"| input nets (stream_id, block): {_in_sids}\n")
        _sys.stderr.flush()
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
            # A BATCH-COMPLETE refresh (samples is not None) FORCES past the
            # refresh throttle: it's a semantic burst boundary, not per-sample
            # chatter, and it must always render the settled trace. Without the
            # force, a short 2nd stream (tx after rx in a duplex run) can land
            # inside the ~125ms throttle window and be dropped — leaving the
            # viewer showing only the first stream (the reported "missing output /
            # only rx" intermittent bug). The full_capture flag = same condition.
            batch_complete = samples is not None
            self.server_activity.emit(batch_complete, batch_complete)
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
            # NO per-sample GUI signaling on the free-running path. The burst
            # runs on the SERVER thread; a queued per-sample emit lands on the
            # GUI event queue faster than Qt can drain + repaint, so the event
            # loop never gets a paint turn and the window freezes for the whole
            # batch (the verified root cause — two prior fixes failed on this).
            # Progress is shown instead by the GUI-side pull timer
            # (_server_pull_tick, ~30 Hz on the GUI thread): the GUI paces
            # itself and cannot be flooded.
            #
            # LOCKSTEP is the one exception: the chip BLOCKS in after_sample
            # until the GUI finishes animating this sample (frame_done), so at
            # most ONE queued refresh is ever outstanding — self-paced by
            # construction, not a flood — and the emit is REQUIRED (and forced
            # past the throttle): the forced refresh runs apply_handshakes,
            # whose flash-drained callback fires frame_done and releases the
            # chip for the next sample. full_capture=True (a process_batch is a
            # bounded burst whose whole trace is retained).
            if self._lockstep_active():
                self.server_activity.emit(True, True)
            if paused:
                self.state_changed.emit("paused")

        self._batch_debug = BatchDebugHooks(
            breakpoint_check=_bp_check, on_sample=_on_sample)
        self._batch_debug.set_delay(self._batch_debug_delay_for_speed())
        # Lockstep the chip to the animation iff cell animation is on (see
        # set_animate_cells). main_window wires the canvas flash-drained callback
        # to self.batch_frame_done so each animated sample releases the next.
        self._batch_debug.set_lockstep(self._lockstep_active())

        # on_new_run fires ONCE per GRC "Run" (a fresh client connection). Reset
        # the waveform trace HERE (Run boundary), not per-batch — so the streams
        # within one Run (rx + tx) ACCUMULATE in the viewer instead of the 2nd
        # batch wiping the 1st. Runs on the server thread → just set the flag
        # (consumed on the GUI thread), like the other reset paths.
        def _new_run():
            self._pending_trace_reset = True
            self._last_server_refresh = 0.0
            # New Run → the chip trace is cleared server-side; restart both
            # breakpoint-mode scan cursors so the fresh Run's events aren't skipped.
            self._batch_bp_scan = 0
            self._gui_trace_scan = 0

        self._gr_server = SimServer(
            self.engine.chip, host=host, port=port,
            on_activity=_activity,
            on_reset=self._rehost_server_chip_threadsafe,
            on_before_batch=self._rebuild_if_dirty_threadsafe,
            on_new_run=_new_run,
            default_entries=default_entries,
            default_hops=default_hops,
            stream_targets=stream_targets,
            batch_reset_writes=reset_writes,
            on_grc_params=_grc_params,
            debug_hooks=self._batch_debug)
        bound = self._gr_server.start()
        # Start the GUI-side pull timer: the ONLY per-sample-rate GUI work
        # during a batch run (the server thread emits nothing per sample). Runs
        # for the whole server session; idle ticks are near-free.
        self._pending_batch_events = []
        self._last_refresh_cost = 0.0
        self._server_pull_timer.start()
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
            self._server_pull_timer.stop()
            # One final (unthrottled) refresh so the debug views settle on the
            # last window of activity. final=True drains the ENTIRE pull residual
            # in this ONE call — the pull timer is now stopped, so nothing else
            # will drain it (a plain batch-end force chunks the residual and lets
            # the still-running timer finish it, but here the server is being torn
            # down). full_capture keeps retain-all semantics; clear the flag AFTER
            # so a later interactive run reverts to the rolling window.
            self.refresh_debug_from_chip(
                force=True, final=True,
                full_capture=self._server_batch_retain_all)
            self._server_batch_retain_all = False
            self.state_changed.emit("idle")
            self.server_state.emit(None)

    def refresh_debug_from_chip(self, *, force: bool = False,
                                full_capture: bool = False,
                                final: bool = False) -> None:
        """Push the live chip's current state into the debug views (called when
        the GNURadio server advances the chip).

        ``full_capture`` (set for a one-shot BATCH run): keep the ENTIRE batch
        trace — no rolling-window trim — so the user can see start AND end
        conditions of the bounded burst (essential for tracing startup/end batch
        behaviour). The default (streaming) path keeps the O(window) rolling trace.

        THROTTLED: coalesced to ~`_LIVE_REFRESH_HZ`, PLUS an adaptive back-off —
        after a refresh whose total cost (append + the synchronous view
        re-renders triggered by trace_updated) was T seconds, the next
        non-forced refresh waits at least ``_REFRESH_BACKOFF * T``. That keeps
        refresh work to a bounded fraction of GUI time even when the retained
        batch trace has grown huge (one re-render can take 100s of ms), so the
        event loop always gets paint turns. ``force`` (batch end / stop) does a
        final refresh regardless of the throttle and drains the whole residual.

        BOUNDED PER CALL (batch mode): the chip is drained into a GUI-side
        pending buffer (``_pending_batch_events``), and at most
        ``_PULL_MAX_EVENTS_PER_TICK`` of it is normalised + appended per call;
        the residual carries to the next pull tick (never re-drained). This is
        what makes the ~30 Hz pull timer safe on an arbitrarily large burst."""
        if self.engine is None:
            return
        import time
        now = time.monotonic()
        # Adaptive back-off keeps a single heavy refresh from starving paint. When
        # a pull residual is queued AND the flush is cheap (animation OFF: no canvas
        # re-render, just the trace append), bypass the back-off so the trace
        # finishes building in a few hundred ms, not tens of seconds — the residual
        # is chunk-capped (_PULL_MAX_EVENTS_PER_TICK) so each flush is bounded and
        # paint still interleaves between ticks.
        #
        # BUT with animation ON every refresh also does the expensive canvas work
        # (cell-state overlay + faces + per-word flashes). Successive finite GRC
        # batches keep TOPPING UP the residual faster than those heavy flushes can
        # drain it, so it never empties — if the back-off is bypassed there, the
        # adaptive throttle that exists precisely to keep heavy refreshes from
        # pinning the GUI is defeated and the live view runs "forever", never
        # settling (the reported animation-ON runaway). So: only bypass while
        # animation is OFF; keep the back-off engaged when animation is ON.
        _animating = bool(getattr(self, "_animate_cells", False))
        _draining = bool(getattr(self, "_pending_batch_events", None)) and not _animating
        min_gap = (1.0 / _LIVE_REFRESH_HZ if _draining else
                   max(1.0 / _LIVE_REFRESH_HZ,
                       _REFRESH_BACKOFF * getattr(self, "_last_refresh_cost", 0.0)))
        if not force and (now - getattr(self, "_last_server_refresh", 0.0)
                          < min_gap):
            return
        self._last_server_refresh = now
        chip = getattr(self, "_sim_chip", 0)
        # Gated diagnostic (KYTTAR_TRACE_DEBUG=1) for the "empty on rerun" report:
        # prints the decisive state of every server refresh so a Stop→Run cycle
        # reveals exactly where run-2's trace is dropped (reset consumed but drain
        # empty, chip trace already cleared, time origin, etc.).
        import os as _os
        if _os.environ.get("KYTTAR_TRACE_DEBUG") == "1":
            import sys as _sys
            try:
                _n_chip = len(self.engine.chip.get_trace())
            except Exception:  # noqa: BLE001
                _n_chip = -1
            _sys.stderr.write(
                f"[TRACE_DBG] refresh force={force} full_capture={full_capture} "
                f"pending_reset={self._pending_trace_reset} "
                f"chip_trace_events={_n_chip} "
                f"model_ports={len(self.trace_model.port_streams())} "
                f"time_origin={self._trace_time_origin}\n")
            _sys.stderr.flush()

        # SINGLE-WRITER: consume a pending trace reset HERE, on the GUI thread,
        # before draining/appending. A server-thread batch callback set the flag
        # (it must not clear the TraceModel itself — that races this append and
        # was the long-standing intermittent-display bug: partial / TX-only /
        # delayed traces). Clearing here, in the same method that appends, means
        # the clear and the append cannot interleave across threads. Done before
        # the 0-event early-return so a reset is honoured even if this particular
        # refresh drains nothing new (the model still drops the previous burst).
        if self._pending_trace_reset:
            self._pending_trace_reset = False
            self.trace_model.clear()
            # A Run boundary also obsoletes the pull residual: any drained-but-
            # not-yet-ingested events belong to the PREVIOUS Run and must not
            # leak into the fresh (rebased) trace.
            if getattr(self, "_pending_batch_events", None):
                self._pending_batch_events = []
            # New Run → forget the time origin; the first event drained below
            # (re)establishes it so this Run's trace starts near 0.
            self._trace_time_origin = None
            self.trace_model.set_cursor(self.trace_model.latest_ns())
            self.trace_updated.emit(self.trace_model)

        # DRAIN the chip trace into the GUI-side pending buffer. The chip's
        # max_records is a HARD CAP (it stops recording when full, NOT a ring
        # buffer), so we pull EVERYTHING it recorded and clear it immediately —
        # recording resumes at once, and the drained events now live in
        # _pending_batch_events (GUI thread only), so nothing is lost and
        # nothing is ever re-drained.
        # BREAKPOINT MODE: the server thread scans the SAME chip trace to fire
        # breakpoints (_batch_breakpoint_hit), so we must NOT clear it here (that
        # would drop events before the scan sees them → missed breakpoints). In
        # that mode read only the NEW tail via a GUI-side cursor (_gui_trace_scan)
        # and leave the trace intact; normal runs drain+clear as before (keeps the
        # chip trace small). get_trace() is non-destructive — only clear_trace()
        # empties it — so both threads can read concurrently.
        _bps = getattr(self, "breakpoints", None)
        _bp_mode = (getattr(self, "_gr_server", None) is not None
                    and _bps is not None and bool(_bps.breakpoints))
        try:
            full = list(self.engine.chip.get_trace())
        except Exception:  # noqa: BLE001
            full = []
        if _bp_mode:
            _gstart = getattr(self, "_gui_trace_scan", 0)
            if _gstart > len(full):        # trace cleared (Run boundary) — restart
                _gstart = 0
            drained = full[_gstart:]
            self._gui_trace_scan = len(full)
        else:
            drained = full
        # VOLUME CONTROL — retain ONLY what the live waveform actually plots.
        # Measured on a 256-sample SSB batch (359,168 chip trace events):
        #     exec_tick    146,688 (41%)   never plotted — animation PC-trail only
        #     output_ready 105,984 (30%)   never plotted — animation transit flashes
        #     instr_arrival 60,160 (17%)   never plotted — animation activity only
        #     data_arrival  45,824 (13%)   RETAINED (tag recovery needs it)
        #     port_capture/injection  512  the ONLY events the waveform draws
        # So >87% of the trace is animation-only fabric detail the waveform never
        # draws, yet it was ALL retained + time-rebased + chunk-ingested into the
        # TraceModel — which pegs at its cap and every ~30 Hz pull tick re-renders
        # ~200k+ transactions (~300 ms each). With the adaptive back-off that makes
        # the residual drain slower than the pull timer can keep up, so it never
        # clears and the view churns the same stuck buffer forever (the reported
        # "scrolls forever; GRC gone / Stop / animation toggle don't matter" — it's
        # all GUI-side, nothing to do with the chip or the client).
        #
        # Fix: the ANIMATION-ONLY kinds (exec_tick / output_ready / instr_arrival)
        # must NEVER enter the retained buffer — the overlay reads only the CURRENT
        # refresh slice, never the residual. Split them off at drain UNCONDITIONALLY
        # (a burst drained while animation was ON must not buffer them either). When
        # animation is ON, keep THIS drain's copy in anim_now for the overlay, used
        # for this refresh then discarded. data_arrival is KEPT (port-tag recovery
        # matches a port_capture to its co-located data_arrival's WRITE dest).
        _ANIM_ONLY = ("exec_tick", "output_ready", "instr_arrival")
        anim_now = []
        if drained:
            if getattr(self, "_animate_cells", False):
                anim_now = [ev for ev in drained if ev.get("kind") in _ANIM_ONLY]
            drained = [ev for ev in drained if ev.get("kind") not in _ANIM_ONLY]
        pending = getattr(self, "_pending_batch_events", None)
        if pending is None:
            pending = self._pending_batch_events = []
        # OUTPUT-CAPTURE TAG STAMP (placement-independent): an output port_capture
        # event carries no dest, and recovering its stream tag by matching a
        # co-located data_arrival by cell+time is PLACEMENT-FRAGILE — on some
        # auto-P&R placements the capture and its feeding WRITE don't co-locate, so
        # every capture resolved to tag None and a duplex port's two streams (e.g.
        # AM tx-passband + rx-audio) merged onto ONE None trace. The SERVER already
        # knows the true tag: as it drains the egress port per batch it records
        # (port, sim-time) -> WRITE dest in _capture_tags. Stamp it onto the capture
        # here — BEFORE the time rebase below mutates time_ns — so TraceModel._port_tag
        # reads the dest directly and the port demuxes correctly regardless of layout.
        _srv = getattr(self, "_gr_server", None)
        _cap_tags = getattr(_srv, "_capture_tags", None) if _srv is not None else None
        if drained and _cap_tags:
            for ev in drained:
                if ev.get("kind") == "port_capture" and ev.get("dest") is None:
                    d = _cap_tags.get((ev.get("port_name"),
                                       float(ev.get("time_ns", 0.0))))
                    if d is not None:
                        ev["dest"] = int(d)
        if drained:
            # PER-RUN TIME REBASE (server/batch mode only), applied AT DRAIN
            # TIME so the origin is the Run's true first event even when ingest
            # is spread over many pull ticks: the chip's sim clock climbs across
            # Runs, so subtract this Run's start-time from every event so the
            # Run's traces start near 0 (both streams overlay on a short window
            # like GRC's sink) instead of marching off to ever-larger absolute
            # times. Only for the bounded batch path — an interactive stream
            # keeps real time.
            if self._server_batch_retain_all:
                if self._trace_time_origin is None:
                    self._trace_time_origin = min(
                        float(ev.get("time_ns", 0.0)) for ev in drained)
                _org = self._trace_time_origin
                if _org:
                    for ev in drained:
                        ev["time_ns"] = float(ev.get("time_ns", 0.0)) - _org
            pending.extend(drained)
            # In breakpoint mode we read a tail via _gui_trace_scan and MUST NOT
            # clear (the server scan needs the events). Otherwise clear as before.
            if not _bp_mode:
                self.engine.clear_trace()
                self._trace_scan_reset()
        # BOUNDED INGEST — this call's slice of the pending delta:
        #  * BATCH (full_capture): retain ALL events start-to-end, but ingest at
        #    most _PULL_MAX_EVENTS_PER_TICK per non-forced call (the pull-timer
        #    cadence); the residual advances next tick. force (batch end / stop
        #    settle) ingests everything so the final trace is whole.
        #  * STREAMING: the model keeps only the rolling window anyway, so drop
        #    all but the most-recent window's worth NOW (cheap) instead of
        #    normalising events only to trim them away — cost stays O(window)
        #    for an unbounded stream, exactly as before.
        retain_all = full_capture
        if not retain_all:
            cap = self._live_trace_max
            new_events = pending[-cap:] if len(pending) > cap else pending
            self._pending_batch_events = []
        elif final or len(pending) <= _PULL_MAX_EVENTS_PER_TICK:
            # FINAL teardown settle (server stop): drain EVERYTHING now — the pull
            # timer is about to stop, so nothing else will drain the residual. Also
            # the small-backlog fast path.
            new_events = pending
            self._pending_batch_events = []
        else:
            # BOUNDED even under `force` (batch-end settle). A whole burst's trace
            # (e.g. 400k events) ingested + rendered in ONE synchronous call blocks
            # the GUI thread for ~10s (the reported "animation-off 5-10s freeze at
            # batch end"). Cap the ingest and leave the residual for the still-
            # running pull timer to drain over the next ticks — the render spreads
            # across repaints and the window stays responsive. force still
            # guarantees a render THIS call (falls through the empty-bail below).
            new_events = pending[:_PULL_MAX_EVENTS_PER_TICK]
            self._pending_batch_events = pending[_PULL_MAX_EVENTS_PER_TICK:]
        # NOTHING NEW → DO NOTHING. An empty ingest adds no transactions,
        # changes no cell state, and moves no cursor, so there is nothing to
        # repaint: bail before touching any view (idle pull ticks after a batch
        # finishes cost microseconds). ``force`` (the final settle on Stop)
        # still falls through so the last window is guaranteed to render even
        # if it drained nothing new.
        if not new_events and not force:
            return
        trimmed = new_events

        # CELL ANIMATION (cell-state overlay + live faces + per-word transit
        # flashes) ONLY when enabled. When OFF the GRC run is a flat-out compute
        # pass: the trace/waveform still updates (below), but the canvas fabric
        # visuals — the bulk of the per-refresh GUI cost — are skipped. See
        # set_animate_cells. (getattr keeps the trace path robust if a partial
        # test harness stubs this method without the flag.)
        if getattr(self, "_animate_cells", False):
            # Cell-state overlay + handshakes from THIS batch of new events. The
            # plotted events (new_events) no longer carry the animation-only kinds
            # (exec_tick/output_ready/instr_arrival) — they were split off at drain
            # time so they never bloat the retained buffer — so fold this drain's
            # copy (anim_now, used for this refresh only) back in here for the
            # overlay's executing/active states + per-word transit flashes.
            anim_events = new_events + anim_now if anim_now else new_events
            states = self._states_from_events(anim_events, chip)
            self.cell_states.emit(states)
            # Live output FACE for EVERY cell active this batch — block AND
            # routing/transit/broker cells alike. Without this the canvas arrows stay
            # frozen at the static build-resolved direction during a GRC batch run, so
            # the port-adjacent routing cells never reflect the live forwarding
            # direction (the reported "no face-arrow flipping" / "stuck in one
            # direction" bug). The interactive timer path emits this every frame via
            # _emit_single_chip_frame; the batch path must too.
            active_xy = [(x, y) for (c, x, y) in states if c == chip]
            if active_xy:
                try:
                    faces = self.engine.cell_faces(self._width, cells=active_xy)
                    self.cell_faces.emit(
                        {(chip, x, y): f for (x, y), f in faces.items()})
                except Exception:  # noqa: BLE001 — engine without live-face read
                    pass
            # Per-WORD handshake steps (NOT a single flat all-cells flash). Bucketed
            # by sim-time so each word transiting the fabric — through the input-port
            # routing cells, the block, and the output-port routing cells — flashes
            # ONE AT A TIME (a rolling wave), exactly like the interactive path. The
            # earlier flat form emitted every cell in one instant which, combined with
            # the refresh throttle, collapsed to a single brief glow and read as "no
            # activity" on the transit cells. The flat cells/ports union is kept for
            # backward-compatible callers.
            steps = self._steps_from_events(anim_events, chip)
            self.handshakes.emit({
                "steps": steps,
                "cells": [c for s in steps for c in s["cells"]],
                "ports": [p for s in steps for p in s["ports"]],
            })

        # Append the new events to the rolling TraceModel window, trim, clear the
        # chip trace (resets the hard cap so recording continues).
        tm = self.trace_model
        tm.append_live(chip, trimmed, self._width)
        if not retain_all:
            tm.trim_to(self._live_trace_max)
        else:
            # Retain the WHOLE burst but keep it BOUNDED (see
            # _SERVER_BATCH_TRACE_MAX): a large cap that shows a full batch
            # start-to-end yet stops the TraceModel growing without limit across
            # successive Runs (which made every later refresh — including idle
            # 0-event ticks — re-touch a huge model on the GUI thread and freeze
            # it). A fresh batch also clears the model up front (in
            # _rehost_server_chip_threadsafe / _rebuild_if_dirty_threadsafe), so
            # this cap only guards a single pathologically-long burst.
            tm.trim_to(_SERVER_BATCH_TRACE_MAX)
        tm.set_cursor(tm.latest_ns())
        # (The chip trace was already cleared at drain time above.)
        self.trace_updated.emit(tm)
        self.cell_state_refreshed.emit()
        # Total cost of this refresh, INCLUDING the synchronous view re-renders
        # the emits above triggered — feeds the adaptive back-off so the next
        # non-forced refresh waits proportionally longer (paint gets its turn).
        self._last_refresh_cost = time.monotonic() - now

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

    def _steps_from_events(self, events, chip):
        """Derive PER-WORD handshake steps from a batch of raw trace events,
        bucketed by ``time_ns`` (mirrors ``engine.handshakes``).

        Returns ``[{"cells": [(chip, x, y, face), …], "ports": [(chip, port), …]},
        …]`` in time order — one step per sim-time. The canvas plays the steps
        back one-at-a-time so a word's passage through the fabric lights as a
        rolling wave, and executing block cells flash as they run.

        Every cell-level event contributes so NOTHING is dropped under heavy /
        multiplexed-bus activity (the earlier "busy bus cells don't flash" bug —
        it only kept ``output_ready`` events carrying a ``face``):
          * ``output_ready`` → a word LEFT the cell on its exit ``face`` (the
            directional transit flash + arrow).
          * ``data_arrival`` / ``instr_arrival`` → use its ``exit_face`` (the
            FORWARD direction) so the flash points the way the word is going —
            this catches transit cells that arrival-forward in one instant.
            ``exit_face`` is inherent cell state and is set on every FORWARD; it is
            ABSENT only when the word is consumed here (``action==execute_locally``,
            HOP_CNT==31). In that case there is NO output direction — we mark it
            ``"exec"`` (whole-cell glow, no arrow) rather than fall back to the
            ARRIVAL face, which would point the arrow BACKWARD toward the source
            (the reported "arrow points at where the data came from" bug).
          * ``exec_tick`` → the cell's PC advanced (it EXECUTED). No face; marked
            with the sentinel face ``"exec"`` so the canvas shows a whole-cell
            execute glow (distinct from a directional transit).
        A cell is emitted once per (time, cell, face) so the same cell arriving +
        forwarding one word in one instant doesn't double-light the same edge.
        A port transfer is a ``port_injection`` / ``port_capture``."""
        buckets: dict[float, dict] = {}
        order: list[float] = []
        seen: dict[float, set] = {}
        for ev in events:
            kind = ev.get("kind")
            t = ev.get("time_ns", 0.0)
            cell = port = None
            cid = ev.get("cell_id")
            if kind == "output_ready" and cid is not None and ev.get("face"):
                cell = (chip, cid % self._width, cid // self._width, ev["face"])
            elif kind in ("data_arrival", "instr_arrival") and cid is not None:
                # The word's FORWARD direction. NEVER the arrival face: a word that
                # is consumed here (execute_locally) has no exit_face, and pointing
                # the arrow back at where it came from is wrong — mark it "exec"
                # (whole-cell glow, no directional arrow) instead.
                exitf = ev.get("exit_face")
                face = exitf if exitf else "exec"
                cell = (chip, cid % self._width, cid // self._width, face)
            elif kind == "exec_tick" and cid is not None:
                cell = (chip, cid % self._width, cid // self._width, "exec")
            elif kind in ("port_injection", "port_capture"):
                pn = ev.get("port_name")
                if pn:
                    port = (chip, pn)
            if cell is None and port is None:
                continue
            b = buckets.get(t)
            if b is None:
                b = {"cells": [], "ports": []}
                buckets[t] = b
                order.append(t)
                seen[t] = set()
            if cell is not None and cell not in seen[t]:
                seen[t].add(cell)
                b["cells"].append(cell)
            if port is not None:
                b["ports"].append(port)
        order.sort()
        return [buckets[t] for t in order]

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
        # A GRC batch in flight runs in the server loop, not the interactive
        # timer — trip the hooks so it aborts at the next sample boundary
        # (BatchAborted; also wakes a lockstep/pause waiter). The stop latch is
        # one-shot: the server re-arms it (clear_stop) at the top of the next
        # batch, so Run-again after a Stop works.
        if self._batch_debug is not None:
            self._batch_debug.stop()
        self._timer.stop()
        # Drop any queued pull residual: once the user hits Stop, the waveform must
        # halt at once. Without this the ~30 Hz pull timer keeps grinding through
        # whatever is still buffered in _pending_batch_events (a big batch can leave
        # hundreds of thousands of events queued), so the trace would keep
        # "scrolling forever" long after the chip batch was aborted — the reported
        # "toggling animation / Stop doesn't stop the data" symptom.
        self._pending_batch_events = []
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
        # VISUAL EMISSION (cell-state colours, live face arrows, per-word transit
        # flashes) ONLY when cell animation is enabled. When OFF the run is a
        # flat-out compute pass: deriving/emitting these every frame is the bulk
        # of the GUI overhead, and with nothing to show it is pure waste — skip it
        # entirely (the metrics / injection / output drain below still run so the
        # run completes and the final trace/waveform render).
        if self._animate_cells:
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
        # Full server-burst cap (5M), not the 100k live cap — else a big RX burst
        # overflows the chip trace mid-run and the waveform is truncated.
        _cap = _SERVER_CHIP_CAP if self._server_batch_retain_all else _LIVE_CHIP_CAP
        self.engine.load(result.words(0), trace=True, max_records=_cap)
        cfg = self._input_port_config(getattr(self, "_sim_chip", 0))
        if cfg is not None:
            port_name, kw = cfg
            self.engine.configure_input_port(port_name, **kw)
        # An explicit 'reset' RPC IS a fresh run → reset the trace (consumed on
        # the GUI thread; never clear directly here — that races the append).
        self._pending_trace_reset = True
        self._last_server_refresh = 0.0
        # This rebuild used the current design → record its version so the
        # pre-batch dirty check doesn't redundantly rebuild on the next batch.
        self._hosted_design_version = getattr(self.app.project, "design_version", 0)
        self._hosted_project_id = id(self.app.project)
        # CRITICAL: re-resolve the server's injection targets for THIS design too
        # (not only in the dirty-rebuild path). Opening a new design via this
        # rehost otherwise keeps the previous design's entry/hop/stream_targets →
        # the new design injects at the wrong cell and emits 0 words.
        self._refresh_server_injection_targets(result)
        return self.engine.chip

    def _refresh_server_injection_targets(self, result) -> None:
        """Re-resolve the running server's per-stream injection targets AND the
        single-stream fallback entry/hop from the just-built ``result``, and push
        them into the live SimServer. MUST run on EVERY re-host of a new design —
        both the dirty-rebuild (``_rebuild_if_dirty_threadsafe``) and the reset/
        rehost path (``_rehost_server_chip_threadsafe``). Without it, hosting a
        new design via the rehost path (e.g. opening a .kyt or a GRC 'reset' RPC)
        swaps the chip but keeps the PREVIOUS design's ``_default_entries`` /
        ``_default_hops`` / ``_stream_targets`` — so a stream-less design (gain)
        injects at the old design's entry/hop and emits 0 words (the reported
        'run AM then gain -> gain gets no output, entry=15/hop=20 not 28/30')."""
        if self._gr_server is None:
            return
        try:
            from engine.port_config import (
                stream_targets as _st_fn, batch_reset_writes as _brw_fn)
            _new_targets = dict(_st_fn(
                self.app.project, self.app.registry, self.app.catalog,
                getattr(self, "_sim_chip", 0), build_result=result) or {})
            # TRANSIENT-STATE GUARD: if the re-resolve comes back EMPTY for a
            # design that DOES have stream-tagged x16_in->block nets, the read was
            # transient (mid-edit / not-fully-routed build) — DON'T clobber the
            # server's existing GOOD targets with {} (else every batch falls to
            # the entry=0/hop=30 fallback -> 0 words). A genuinely stream-less
            # design (no such nets) correctly resolves to {} and we accept it.
            from model.connection import (
                ChipPortEndpoint as _CPE, BlockEndpoint as _BE)
            _has_stream_nets = any(
                isinstance(c.source, _CPE) and isinstance(c.target, _BE)
                and getattr(c, "stream_id", None)
                for c in self.app.project.connections)
            if _new_targets or not _has_stream_nets:
                self._gr_server._stream_targets = _new_targets
                self._gr_server._batch_reset_writes = list(_brw_fn(
                    result, getattr(self, "_sim_chip", 0)) or [])
            else:
                import sys as _sysT
                _sysT.stderr.write(
                    "[placeKYT server] re-host saw EMPTY stream_targets for a "
                    "stream-tagged design — keeping current targets, will "
                    "re-resolve on a settled batch\n")
                _sysT.stderr.flush()
            # The single-stream fallback entry/hop (a design with no stream_id,
            # e.g. gain). MUST be refreshed so it injects at THIS design's cell,
            # not the previous design's.
            cfg2 = self._input_port_config(getattr(self, "_sim_chip", 0))
            if cfg2 is not None:
                _pn, _kw = cfg2
                if "entry_addr" in _kw:
                    self._gr_server._default_entries[_pn] = int(_kw["entry_addr"])
                if "hop_count" in _kw:
                    self._gr_server._default_hops[_pn] = int(_kw["hop_count"])
            import sys as _sys2
            _sys2.stderr.write(
                "[placeKYT server] re-resolved injection targets: "
                f"stream_targets={ {k: (v['entry_addr'], v['hop_count'], v['out_tag']) for k, v in self._gr_server._stream_targets.items()} } "
                f"default_entries={dict(self._gr_server._default_entries)} "
                f"default_hops={dict(self._gr_server._default_hops)}\n")
            _sys2.stderr.flush()
        except Exception as _exc:  # noqa: BLE001 — never break the batch
            import sys as _sys2
            _sys2.stderr.write(
                f"[placeKYT server] injection-target re-resolve failed: {_exc}\n")
            _sys2.stderr.flush()

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
        # DESIGN NOT SETTLED: an import / auto-P&R is mid-flight on the GUI thread
        # (self.app.project is being placed+routed incrementally). A batch that
        # arrives now would rebuild the HALF-PLACED project — few cells, no routed
        # stream nets — and re-host that garbage (the reported "reimport a .grc on
        # a running server -> 374-word chip, empty stream_targets -> no output").
        # Keep serving the last GOOD chip; a batch after the design settles
        # (_after_project_loaded clears the flag) rebuilds cleanly.
        if getattr(self.app, "pnr_in_progress", False):
            return None, None
        # Compare the live monotonic design_version to the version the server has
        # hosted. We do NOT use build_dirty here: the GUI's own post-edit
        # cached_build() (inspector/face refresh that fires right after a reroute)
        # CLEARS build_dirty before this GRC Run ever runs, so it would read False
        # and skip — the stale-run / phantom-cells bug. design_version is bumped on
        # every edit and never cleared by a build, so it survives that race.
        cur_ver = getattr(self.app.project, "design_version", 0)
        cur_pid = id(self.app.project)
        if (self._hosted_design_version is not None
                and cur_ver == self._hosted_design_version
                and self._hosted_project_id == cur_pid):
            # Design unchanged — fast path (no rebuild). Do NOT reset the trace
            # here: this hook fires per BATCH, and a duplex Run is two batches
            # (rx + tx) on ONE connection. Resetting per batch made the 2nd batch
            # WIPE the 1st (only one stream ever visible — the reported flicker).
            # The trace reset now happens ONCE per Run in the on_new_run handler
            # (a fresh client connection), so a Run's streams ACCUMULATE.
            return None, None                      # design unchanged — fast path
        import sys, time as _t
        print(f"[placeKYT PERF] design edited since last run (v{self._hosted_design_version}"
              f"→v{cur_ver}) — REBUILDING (this is the SLOW per-run rebuild if it keeps firing)",
              file=sys.stderr, flush=True)
        _t0 = _t.perf_counter()
        result = self.app.build()                  # rebuild from current routes
        print(f"[placeKYT PERF]   app.build() took {(_t.perf_counter()-_t0)*1000:.0f} ms",
              file=sys.stderr, flush=True)
        if not result.ok:
            errs = "; ".join(str(e) for e in result.errors) or "build failed"
            print(f"[placeKYT server] rebuild FAILED (edited design): {errs}",
                  file=sys.stderr, flush=True)
            return None, f"placeKYT build error (edited design): {errs}"
        print(f"[placeKYT server] rebuilt OK — re-hosting {len(result.words(0))} "
              "words for this run", file=sys.stderr, flush=True)
        # Re-host the freshly built bitstream on a clean chip + re-configure the
        # input port. Size the chip trace for a FULL server burst (_SERVER_CHIP_CAP,
        # 5M) — NOT the small _LIVE_CHIP_CAP (100k). A single RX matched-filter
        # burst emits tens of thousands of events; at 100k the chip trace hits its
        # HARD cap mid-burst and STOPS recording, so the waveform showed only a
        # truncated fraction of the output (e.g. 61/120 words — the "fewer captured
        # values" symptom in the WAVE evidence).
        self.engine.reset()
        _cap = _SERVER_CHIP_CAP if self._server_batch_retain_all else _LIVE_CHIP_CAP
        self.engine.load(result.words(0), trace=True, max_records=_cap)
        cfg = self._input_port_config(getattr(self, "_sim_chip", 0))
        if cfg is not None:
            port_name, kw = cfg
            self.engine.configure_input_port(port_name, **kw)
        # Do NOT reset the trace here (this rebuild fires on the FIRST batch of a
        # Run; the on_new_run handler already reset at the Run's connection start).
        # Resetting here too would wipe an earlier batch of the SAME Run.
        self._last_server_refresh = 0.0
        self._hosted_design_version = cur_ver   # remember what we just hosted
        self._hosted_project_id = cur_pid       # …and WHICH design (id) it was
        # RE-RESOLVE the per-stream injection targets + per-batch loop-memory resets
        # from the FRESH build and push them into the running server. WITHOUT this, a
        # server started BEFORE the design was imported/routed captured an EMPTY
        # stream_targets ({}) at start-up; this dirty-rebuild re-hosts the now-routed
        # chip but the server would keep serving the stale/empty map → every batch
        # falls through to the entry=0/hop=30/out_tag=None single-stream fallback and
        # emits 0 words (the "turn the server on, THEN import" flat-run bug). Recomputing
        # here makes the START-SERVER-vs-IMPORT ORDER not matter: whichever happens
        # first, the first post-import batch re-resolves against the current design.
        self._refresh_server_injection_targets(result)
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

    def _input_port_config(self, chip_id: int = 0, build_result=None):
        """(port_name, {entry_addr, hop_count, data_addr}) for the block fed by
        ``chip_id``'s input port, or None. Delegates to the Qt-free helper so the
        GUI sim and the CLI build derive identical port config.

        Passing ``build_result`` makes the helper prefer the BUILT corridor-accurate
        landing (cell/entry/hop the routed corridor+broker actually delivers to) over
        a manhattan straight-line estimate — required so a chip-input net that lands on
        a multi-cell block's non-corner input cell (via a broker one hop past the
        corridor end) injects at the RIGHT cell instead of a cell short (0 output)."""
        from engine.port_config import input_port_config
        return input_port_config(
            self.app.project, self.app.registry, self.app.catalog, chip_id,
            build_result=build_result)

    def _output_target(self):
        """(chip_id, port_name) of the design's final output port, or None."""
        from engine.port_config import output_port_target
        return output_port_target(self.app.project)


def _default_ramp() -> list[int]:
    return [0x1000, 0x2000, 0x3000, 0x4000, 0x5000, 0x6000, 0x4000, 0x2000]
