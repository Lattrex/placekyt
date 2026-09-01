# SPDX-License-Identifier: GPL-3.0-or-later
"""GUI-side pull-timer refresh for a GRC batch run (the freeze fix).

The verified root cause of the "window frozen for the whole batch" bug: the
burst runs on the SERVER thread, and each sample emitted a queued Qt signal to
the GUI (``server_activity``); queued slot invocations arrive faster than Qt
can drain + repaint, so the event loop never gets a paint turn — the window is
dead until the batch ends. The fix inverts control: the server thread does NO
per-sample GUI signaling; a QTimer OWNED BY THE GUI THREAD pulls the delta at
~30 Hz with a BOUNDED per-tick ingest, so the GUI paces itself and can never
be flooded.

Offscreen tests CANNOT observe paint/responsiveness (all three failed prior
fixes were green here while the real GUI was dead) — so these tests lock in
the STRUCTURAL properties the design is correct-by-construction on:

  * ZERO per-sample ``server_activity`` emissions on the free-running path
    (O(few) per batch, not O(nsamp));
  * LOCKSTEP (animation on, below top speed) still emits per sample — that
    path is self-paced by construction (the chip blocks on frame_done, so at
    most one queued refresh is ever outstanding);
  * the capped incremental ingest converges to the SAME final TraceModel as a
    single whole-batch append, advancing the residual (never re-draining);
  * a Run-boundary reset drops the stale residual;
  * the pull timer runs exactly while the server is hosted;
  * SimController.stop() aborts an in-flight batch (BatchAborted) and the next
    batch re-arms (clear_stop) so Stop → Run-again works.
"""
import math
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from engine.batch_debug import BatchDebugHooks
from engine.catalog import BlockCatalog
from engine.sim_bridge import BatchAborted
from engine.trace_model import TraceModel
from ui import sim_controller as sc


# ---------------------------------------------------------------------------
# Harness (same pattern as test_batch_trace_retention): the minimal slice of
# SimController state refresh_debug_from_chip reads, so the bounded-ingest
# behaviour is exercised directly with a synthetic drainable chip.
# ---------------------------------------------------------------------------
def _fake_event(t_ns):
    # A PLOTTED event kind (data_arrival), NOT exec_tick: the live GRC drain path
    # drops exec_tick events when cell animation is OFF (they feed only the
    # animation overlay / debug PC-trail, never the waveform) — using a plotted
    # kind here exercises the ingest/chunking machinery with events that actually
    # reach the TraceModel, faithful to what a real batch delivers to the viewer.
    return {"kind": "data_arrival", "cell_id": 0, "time_ns": float(t_ns),
            "dest": 1, "data": 0}


class _Chip:
    def __init__(self, events):
        self._events = list(events)

    def get_trace(self):
        return list(self._events)

    def clear_trace(self):
        self._events = []


class _Engine:
    def __init__(self, events):
        self.chip = _Chip(events)

    def clear_trace(self):
        self.chip.clear_trace()


class _Sig:
    def __init__(self):
        self.count = 0

    def emit(self, *a, **k):
        self.count += 1


class _Harness:
    refresh_debug_from_chip = sc.SimController.refresh_debug_from_chip
    _states_from_events = sc.SimController._states_from_events
    _steps_from_events = sc.SimController._steps_from_events
    # Bottleneck busy-time accumulator (exec_ticks are dropped from the model, so
    # the drain folds them into this incrementally).
    _accumulate_busy = sc.SimController._accumulate_busy
    cell_busy_report = sc.SimController.cell_busy_report
    _busy_from_chip_trace = sc.SimController._busy_from_chip_trace

    def __init__(self, events, *, retain_all=True):
        self.engine = _Engine(events)
        self.trace_model = TraceModel()
        self._width = 10
        self._sim_chip = 0
        self._live_trace_max = 50
        self._server_batch_retain_all = retain_all
        self._last_server_refresh = 0.0
        self._pending_trace_reset = False
        self._trace_time_origin = None
        self._animate_cells = False
        self._busy_ns = {}
        self._busy_gaps = {}
        self._busy_last_tick = {}
        self._busy_first_tick = None
        self._busy_last_any = None
        for name in ("cell_states", "cell_faces", "handshakes", "trace_updated",
                     "cell_state_refreshed"):
            setattr(self, name, _Sig())

    def _trace_scan_reset(self):
        pass

    def unthrottled_refresh(self, **kw):
        """One refresh with the rate throttle + adaptive back-off bypassed (the
        tests drive ticks explicitly; wall-clock pacing is not under test)."""
        self._last_server_refresh = 0.0
        self._last_refresh_cost = 0.0
        self.refresh_debug_from_chip(**kw)


N = 500
CAP = 120     # small per-tick cap to force multiple ticks over N events


def test_capped_ingest_converges_to_whole_batch_reference(monkeypatch):
    """Successive non-forced pull ticks ingest at most CAP events each, never
    re-drain, never shrink the model, terminate in ceil(N/CAP) ticks, and end
    with EXACTLY the trace a single whole-batch append produces."""
    monkeypatch.setattr(sc, "_PULL_MAX_EVENTS_PER_TICK", CAP)
    h = _Harness([_fake_event(i) for i in range(N)])

    sizes = []
    for tick in range(50):                      # hard guard: no runaway
        h.unthrottled_refresh(full_capture=True)
        sizes.append(len(h.trace_model.transactions))
        # The chip was drained ONCE (first tick) and stays empty — the residual
        # is advanced GUI-side, never re-drained from the chip.
        assert h.engine.chip.get_trace() == []
        if not h._pending_batch_events:
            break
    ticks = len(sizes)
    assert ticks == math.ceil(N / CAP), \
        f"expected ceil({N}/{CAP}) bounded ticks, took {ticks}"
    # Monotonic build, bounded per tick.
    assert sizes == sorted(sizes)
    assert sizes[0] == CAP, "first tick must ingest exactly the cap"
    assert all(b - a <= CAP for a, b in zip(sizes, sizes[1:]))

    # Reference: the same events appended in ONE whole-batch refresh (final=True
    # drains the entire residual at once — the server-stop teardown settle).
    ref = _Harness([_fake_event(i) for i in range(N)])
    ref.unthrottled_refresh(force=True, final=True, full_capture=True)
    got = [t.time_ns for t in h.trace_model.transactions]
    want = [t.time_ns for t in ref.trace_model.transactions]
    assert len(h.trace_model.transactions) == N
    assert got == want, "incremental ingest must equal the whole-batch trace"


def test_force_batch_end_chunks_residual(monkeypatch):
    """force=True WITHOUT final (a batch-end settle) must CHUNK the residual, not
    drain it all in one blocking call — a 400k-event batch ingested at once froze
    the GUI ~10s. It ingests one more cap and leaves the rest for the pull timer."""
    monkeypatch.setattr(sc, "_PULL_MAX_EVENTS_PER_TICK", CAP)
    h = _Harness([_fake_event(i) for i in range(N)])
    h.unthrottled_refresh(full_capture=True)            # one capped tick
    assert len(h.trace_model.transactions) == CAP
    assert len(h._pending_batch_events) == N - CAP
    # force (batch end) ingests only ONE more cap, not the whole residual.
    h.unthrottled_refresh(force=True, full_capture=True)
    assert len(h.trace_model.transactions) == 2 * CAP
    assert len(h._pending_batch_events) == N - 2 * CAP


def test_final_flush_ingests_entire_residual(monkeypatch):
    """final=True (server-stop teardown, timer already stopped) drains the WHOLE
    residual in one call — the settled trace is complete no matter how much was
    pending, because nothing else will drain it."""
    monkeypatch.setattr(sc, "_PULL_MAX_EVENTS_PER_TICK", CAP)
    h = _Harness([_fake_event(i) for i in range(N)])
    h.unthrottled_refresh(full_capture=True)            # one capped tick
    assert len(h.trace_model.transactions) == CAP
    assert len(h._pending_batch_events) == N - CAP
    h.unthrottled_refresh(force=True, final=True, full_capture=True)
    assert len(h.trace_model.transactions) == N
    assert h._pending_batch_events == []


def test_run_boundary_reset_drops_stale_residual(monkeypatch):
    """A pending Run-boundary trace reset must drop the pull residual too — the
    leftover events belong to the PREVIOUS Run and must not leak into (or time-
    anchor) the fresh Run's trace."""
    monkeypatch.setattr(sc, "_PULL_MAX_EVENTS_PER_TICK", CAP)
    h = _Harness([_fake_event(i) for i in range(N)])
    h.unthrottled_refresh(full_capture=True)            # leaves a residual
    assert h._pending_batch_events
    h._pending_trace_reset = True                       # new Run (server thread)
    h.unthrottled_refresh(full_capture=True)            # consumed on GUI side
    assert h._pending_batch_events == []
    assert len(h.trace_model.transactions) == 0
    assert h._trace_time_origin is None


def test_animation_on_keeps_backoff_while_draining(monkeypatch):
    """With animation ON, a queued residual must NOT bypass the adaptive back-off.

    Reproduces the reported animation-ON runaway ("the trace is forever, it keeps
    processing the batch over and over"): with animation ON every refresh also does
    the expensive canvas work, and successive finite GRC batches keep topping up the
    residual faster than those heavy flushes drain it — so it never empties. If the
    back-off is bypassed just because a residual exists, the throttle that keeps
    heavy refreshes from pinning the GUI is defeated and the live view never settles.
    A residual + animation OFF DOES bypass (cheap flushes should drain fast)."""
    import time
    monkeypatch.setattr(sc, "_PULL_MAX_EVENTS_PER_TICK", CAP)

    def _one_throttled_refresh(h):
        # A residual is queued; a heavy last refresh cost + a very recent refresh
        # means the adaptive back-off, IF engaged, blocks this (non-forced) call.
        h._pending_batch_events = [_fake_event(i) for i in range(CAP)]
        h._last_refresh_cost = 10.0                 # 10s cost -> 30s back-off gap
        # 1s since the last refresh: clears the base 1/_LIVE_REFRESH_HZ gap (~33ms,
        # so the animation-OFF bypass fires) but NOT the 30s adaptive back-off (so
        # the animation-ON path, which keeps the back-off, is still throttled).
        h._last_server_refresh = time.monotonic() - 1.0
        before = len(h.trace_model.transactions)
        h.refresh_debug_from_chip(full_capture=True)
        return len(h.trace_model.transactions) - before

    # Animation ON: back-off engaged -> the call is throttled, ingests nothing.
    h_on = _Harness([])
    h_on._animate_cells = True
    assert _one_throttled_refresh(h_on) == 0, \
        "animation ON must honor the back-off while draining (else it runs forever)"

    # Animation OFF: back-off bypassed while draining -> the call ingests its cap.
    h_off = _Harness([])
    h_off._animate_cells = False
    assert _one_throttled_refresh(h_off) == CAP, \
        "animation OFF must bypass the back-off while draining (fast, cheap flush)"


def test_animation_only_events_dropped_at_drain(monkeypatch):
    """The live GRC drain path must retain ONLY what the waveform plots.

    Measured on a 256-sample SSB batch (359,168 chip events): exec_tick 41%,
    output_ready 30%, instr_arrival 17% — all NEVER plotted (the waveform plots
    port_capture / port_injection / data_arrival). Retaining that 87% of
    animation-only fabric detail pegged the TraceModel at its cap and made every
    ~30 Hz pull tick re-render 200k+ transactions; with the adaptive back-off the
    residual drained slower than the timer fired, so it never cleared and the view
    churned the same stuck buffer forever ('scrolls forever'; GRC gone / Stop /
    animation toggle all irrelevant — it's pure GUI-side). So the animation-only
    kinds must NEVER enter the retained buffer, UNCONDITIONALLY (a burst drained
    while animation was ON must not buffer them either). Animation ON still SHOWS
    the overlay, from this drain's copy, used for the refresh then discarded."""
    monkeypatch.setattr(sc, "_PULL_MAX_EVENTS_PER_TICK", 10_000)
    ANIM = ("exec_tick", "output_ready", "instr_arrival")

    def _mixed():
        # 100 plotted data_arrivals interleaved with 300 animation-only events
        # (100 of each anim kind) — the realistic ratio: anim detail dominates.
        evs = []
        for i in range(100):
            evs.append({"kind": "data_arrival", "cell_id": 0,
                        "time_ns": float(i), "dest": 1, "data": 0})
            for j, k in enumerate(ANIM):
                evs.append({"kind": k, "cell_id": 0,
                            "time_ns": float(i) + 0.1 * (j + 1)})
        return evs

    # Animation OFF: only the 100 plotted data_arrivals reach the model; nothing
    # animation-only is left in the buffer.
    h_off = _Harness(_mixed())
    h_off._animate_cells = False
    h_off.unthrottled_refresh(force=True, final=True, full_capture=True)
    assert len(h_off.trace_model.transactions) == 100, \
        "only plotted events must reach the model (animation-only kinds dropped)"
    assert h_off._pending_batch_events == []

    # Animation ON: animation-only kinds STILL never retained (buffer stays bounded),
    # but the overlay emit fires (it saw this drain's animation events).
    h_on = _Harness(_mixed())
    h_on._animate_cells = True
    h_on.cell_states.count = 0
    h_on.unthrottled_refresh(force=True, final=True, full_capture=True)
    assert len(h_on.trace_model.transactions) == 100, \
        "animation-only kinds must NOT be retained even when animation is ON"
    assert not any(t.kind in ANIM for t in h_on.trace_model.transactions)
    assert h_on.cell_states.count > 0, \
        "animation overlay must still fire when animation is ON"


def test_streaming_path_unchanged_window_trim():
    """The pure streaming path (full_capture=False) still keeps only the rolling
    window — bounded for an unbounded stream, exactly as before the fix."""
    h = _Harness([_fake_event(i) for i in range(N)], retain_all=False)
    h.unthrottled_refresh(force=True)                   # streaming default
    assert len(h.trace_model.transactions) == h._live_trace_max
    assert h._pending_batch_events == []


# ---------------------------------------------------------------------------
# Real-SimController structural tests (empty-project placeholder host: the
# server binds without a design, which is all these need).
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def sim(qapp):
    from ui.controller import AppController
    from ui.sim_controller import SimController

    ctrl = AppController(catalog=BlockCatalog.from_gr_kyttar())
    s = SimController(ctrl)
    yield s
    s.stop_gnuradio_server()


def test_no_per_sample_gui_signal_free_running(sim):
    """THE core anti-freeze property: with animation OFF (the default), a batch
    of many samples emits ZERO per-sample server_activity signals from the
    server-thread hook — the GUI-side pull timer shows progress instead. The
    prior lockup was exactly O(nsamp) queued emissions."""
    port = sim.start_gnuradio_server(port=0)
    assert port
    emits = []
    sim.server_activity.connect(lambda *a: emits.append(a))
    hooks = sim._batch_debug
    assert isinstance(hooks, BatchDebugHooks)
    NSAMP = 300
    for k in range(NSAMP):                      # what process_batch does
        hooks.after_sample(None, k, "x16_out")
    assert emits == [], (
        f"free-running batch must emit NO per-sample GUI signal, got "
        f"{len(emits)} (O(nsamp) queued emissions is the freeze root cause)")


def test_lockstep_still_emits_per_sample_self_paced(sim):
    """LOCKSTEP (animation on, below top speed) keeps the per-sample forced
    refresh — required so apply_handshakes runs and frame_done releases the
    chip. Self-paced by construction: the chip BLOCKS until the GUI acks, so
    at most one emission is outstanding (pre-armed here via frame_done)."""
    sim.start_gnuradio_server(port=0)
    emits = []
    sim.server_activity.connect(lambda *a: emits.append(a))
    hooks = sim._batch_debug

    sim.set_animate_cells(True)                 # default speed 8 < top ⇒ lockstep
    assert sim._lockstep_active()
    NSAMP = 20
    for k in range(NSAMP):
        hooks.frame_done()                      # pre-arm the frame gate (no GUI)
        hooks.after_sample(None, k, "x16_out")
    assert len(emits) == NSAMP
    assert all(a == (True, True) for a in emits), \
        "lockstep refreshes must be full_capture AND forced past the throttle"

    # Top (fast-forward) speed drops lockstep → back to zero per-sample emits.
    sim.set_speed_index(len(sc.SPEED_STEPS) - 1)
    assert not sim._lockstep_active()
    emits.clear()
    for k in range(NSAMP):
        hooks.after_sample(None, k, "x16_out")
    assert emits == []


def test_pull_timer_runs_exactly_while_server_hosted(sim):
    """The GUI-side pull timer starts with the server and stops with it (idle
    ticks are cheap, but a dangling timer after Stop would be a leak)."""
    assert not sim._server_pull_timer.isActive()
    sim.start_gnuradio_server(port=0)
    assert sim._server_pull_timer.isActive()
    sim.stop_gnuradio_server()
    assert not sim._server_pull_timer.isActive()


def test_controller_stop_aborts_batch_and_next_batch_rearms(sim):
    """Stop during a batch: SimController.stop() trips the hooks so the server
    loop aborts at the next sample boundary; the next batch's clear_stop
    re-arms so Stop → Run-again works (the one-shot latch must not stick)."""
    sim.start_gnuradio_server(port=0)
    hooks = sim._batch_debug
    sim.stop()                                  # user hits Stop mid-batch
    with pytest.raises(BatchAborted):
        hooks.after_sample(None, 0, "x16_out")
    hooks.clear_stop()                          # what the next batch RPC does
    hooks.after_sample(None, 1, "x16_out")      # runs — no abort, no hang



def test_bottleneck_busy_accumulates_across_drain():
    """The bottleneck view's per-cell busy-time survives the drain path that DROPS
    exec_ticks from the retained model + clears the chip trace (the live GRC-server
    path where the table was EMPTY until the incremental accumulator was added)."""
    events = []
    for t in (0.0, 100.0, 200.0, 300.0):
        events.append({"kind": "exec_tick", "cell_id": 0, "time_ns": t, "pc": 0})
    for t in (0.0, 10.0, 20.0, 30.0):
        events.append({"kind": "exec_tick", "cell_id": 1, "time_ns": t, "pc": 0})
    events.sort(key=lambda e: e["time_ns"])
    h = _Harness(events, retain_all=True)
    # Drain the whole batch through the REAL refresh path (final=True → drain all).
    h.unthrottled_refresh(full_capture=True, force=True, final=True)
    # exec_ticks never reached the retained model (they're animation-only)...
    assert all(t.kind != "exec_tick" for t in h.trace_model.transactions)
    # ...but the accumulator captured their busy-time, and cell 0 is the bottleneck.
    rep = h.cell_busy_report()
    assert rep is not None
    busy = rep["busy"]
    assert busy[(0, 0, 0)] > busy[(0, 1, 0)]


# ---------------------------------------------------------------------------
# DUPLEX RUN-BOUNDARY: data-dependent streams arrive as SEPARATE duplex RPCs
# within ONE GRC Run, and must ACCUMULATE in the waveform instead of erasing
# each other.
#
# Root cause of the reported "the LZ4 traces don't show up automatically":
# _process_batch_duplex fired on_new_run + _clear_chip_trace UNCONDITIONALLY on
# every RPC, on the assumption that a duplex batch is always one whole Run (all
# streams rendezvous into one RPC). That assumption fails for DATA-DEPENDENT
# streams: examples/lz4_stream's `cmp` source is fed by the compressed bytes the
# `raw` stream produced ON the chip, so it cannot submit inside the leader's
# 0.4 s collect window — one Run arrives as TWO sequential duplex RPCs. The
# second RPC's unconditional reset wiped the first stream's finished trace, and
# the LAST RPC's reset left the pane empty for the entire Run (the seeded rows
# read "Analog: —" over a collapsed ~0..1 ns ruler).
#
# MEASURED against the real shipped lz4 chip, before the fix:
#     after the raw stream : x1_out tag2=320  tag5=455  ports=6
#     after the cmp RPC    : x1_out tag2=0    tag5=0    ports=0
# The fix gates BOTH the on_new_run signal and the chip-trace clear on a
# stream_id REPEATING, exactly as the single-stream process_batch path does.
# ---------------------------------------------------------------------------
def _duplex_boundary_decider():
    """Exercise the SHIPPED _process_batch_duplex Run-boundary prologue.

    Executes the real source region (not a copy) against a stub carrying only
    the collaborators that prologue touches, so the gate tracks the shipped
    decision and fails if that logic reverts to unconditional."""
    import inspect

    from engine.sim_bridge import SimServer

    src = inspect.getsource(SimServer._process_batch_duplex)
    start = src.index("_run_keys = [")
    end = src.index("# Fresh-build guard")
    prologue = inspect.cleandoc(src[start:end])
    # The prologue MUST gate the clear (the pre-fix code called it outright).
    assert "if _new_run:" in prologue and "_clear_chip_trace()" in prologue, (
        "duplex prologue no longer gates the Run-boundary chip-trace clear")
    code = compile(prologue, "<duplex-prologue>", "exec")

    class _Srv:
        def __init__(self):
            self._run_seen_streams = set()
            self.new_runs = 0
            self.clears = 0

        def _on_new_run(self):
            self.new_runs += 1

        def _clear_chip_trace(self):
            self.clears += 1

    srv = _Srv()

    def decide(stream_ids):
        ns = {"self": srv,
              "streams_hdr": [{"stream_id": s} for s in stream_ids]}
        exec(code, ns)
        return bool(ns["_new_run"])

    return srv, decide


def test_duplex_data_dependent_streams_are_one_run():
    """Two DATA-DEPENDENT streams (separate duplex RPCs, distinct stream_ids)
    are ONE Run: no reset, no chip-trace clear on the second RPC."""
    srv, decide = _duplex_boundary_decider()
    assert decide(["raw"]) is False
    assert decide(["cmp"]) is False          # same Run — must NOT reset
    assert srv.new_runs == 0
    assert srv.clears == 0
    assert srv._run_seen_streams == {"raw", "cmp"}


def test_duplex_repeated_stream_id_starts_a_new_run():
    """A stream_id REPEATING means a genuinely new Run — reset and clear fire
    exactly once, and the seen-set restarts from that RPC's streams."""
    srv, decide = _duplex_boundary_decider()
    decide(["raw"])
    decide(["cmp"])
    assert decide(["raw"]) is True           # Run 2
    assert srv.new_runs == 1
    assert srv.clears == 1
    assert srv._run_seen_streams == {"raw"}
    assert decide(["cmp"]) is False          # accumulates into Run 2
    assert srv.new_runs == 1


def test_duplex_rendezvoused_streams_still_one_run():
    """The classic full-duplex case (both streams in ONE RPC) is unchanged:
    first RPC is not a boundary, the same pair repeating starts Run 2."""
    srv, decide = _duplex_boundary_decider()
    assert decide(["tx", "rx"]) is False
    assert decide(["tx", "rx"]) is True
    assert srv.new_runs == 1
    assert srv.clears == 1


def test_reset_op_clears_the_run_seen_streams():
    """An explicit ``reset`` RPC is a Run boundary: the stream-cycling seen-set
    must be emptied so the NEXT Run's first stream is not mistaken for a repeat
    (and its streams are not folded into the finished Run)."""
    import inspect

    from engine.sim_bridge import SimServer

    src = inspect.getsource(SimServer)
    i = src.index('if op == "reset":')
    body = src[i:i + 900]
    assert "self._run_seen_streams = set()" in body, (
        "the reset op no longer clears the Run-boundary stream seen-set")
