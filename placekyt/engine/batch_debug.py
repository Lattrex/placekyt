"""BatchDebugHooks — make the GUI debug controls first-class during a GRC batch.

A GRC flowgraph drives the placeKYT-hosted chip by sending a whole burst in one
``process_batch`` RPC (see ``sim_bridge.SimServer.process_batch``), which runs
server-side on a background thread against the chip — bypassing the GUI's
interactive ``SimController`` loop. So breakpoints, the speed slider, and single
-step did nothing during a GRC run: the burst ran flat out.

This object is the bridge. The host (``SimController``, GUI side) owns one and
passes it to ``SimServer(debug_hooks=...)``. The server's per-sample loop calls
:meth:`after_sample` after every sample; this consults thread-safe state the GUI
mutates — a breakpoint check, a pause gate, a single-step latch, and a playback
delay — so the controls behave the same as in an interactive run.

It is intentionally Qt-free and uses only ``threading`` primitives: the server
runs on its own thread and must never touch Qt objects directly. The host wires
the GUI signals to the plain setters here; a per-sample callback (``on_sample``)
lets the host marshal a debug-view refresh back to the GUI thread.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional


class BatchDebugHooks:
    """Thread-safe debug state consulted by the batch loop after each sample.

    Setters are called from the GUI thread; :meth:`after_sample` is called from
    the server thread. All shared flags are guarded by an internal lock / Event,
    so the two threads coordinate without Qt.
    """

    def __init__(
        self,
        *,
        breakpoint_check: Optional[Callable[[object, int], bool]] = None,
        on_sample: Optional[Callable[[int, bool], None]] = None,
    ):
        # breakpoint_check(chip, sample_index) -> True if a breakpoint fired at
        # this sample (the host evaluates its BreakpointSet against the chip's
        # recent trace). None ⇒ no breakpoints.
        self._breakpoint_check = breakpoint_check
        # on_sample(sample_index, paused) -> None: the host marshals a debug-view
        # refresh to the GUI thread. Called once per sample (paused=True when the
        # loop is about to block, so the views show the paused frame).
        self._on_sample = on_sample

        # All pause/step/stop state lives under ONE Condition so the breakpoint
        # check + the blocking wait are atomic — a resume()/step()/stop() from
        # the GUI thread that lands between "decide to pause" and "start waiting"
        # cannot be lost (the classic Event-pair lost-wakeup race). _paused is the
        # gate; _step re-pauses after one sample; _stop aborts; _delay_s paces.
        self._cv = threading.Condition()
        self._paused = False
        self._step = False
        self._stop_flag = False
        self._delay_s = 0.0

    # -- GUI-thread setters ---------------------------------------------------

    def pause(self) -> None:
        with self._cv:
            self._paused = True
            self._cv.notify_all()

    def resume(self) -> None:
        with self._cv:
            self._paused = False
            self._step = False
            self._cv.notify_all()

    def step(self) -> None:
        """Advance exactly one sample, then pause again."""
        with self._cv:
            self._step = True
            self._paused = False
            self._cv.notify_all()

    def stop(self) -> None:
        """Abort the in-progress burst at the next sample boundary."""
        with self._cv:
            self._stop_flag = True
            self._cv.notify_all()

    def set_delay(self, seconds: float) -> None:
        with self._cv:
            self._delay_s = max(0.0, float(seconds))

    @property
    def is_paused(self) -> bool:
        with self._cv:
            return self._paused

    # -- server-thread hook ---------------------------------------------------

    def after_sample(self, chip, sample_index: int, port) -> None:
        """Called by process_batch after each sample. Honors stop, breakpoint,
        pause, single-step, and the speed delay. Raises BatchAborted on stop."""
        from engine.sim_bridge import BatchAborted

        with self._cv:
            if self._stop_flag:
                raise BatchAborted()

        # A breakpoint fires → pause the loop (the host shows the hit). Evaluated
        # outside the lock (the check may touch the chip); apply the pause under
        # the lock so it composes atomically with the wait below.
        bp_fired = False
        if self._breakpoint_check is not None:
            try:
                bp_fired = bool(self._breakpoint_check(chip, sample_index))
            except Exception:
                # A faulty check must never wedge the burst — treat as no hit.
                bp_fired = False

        with self._cv:
            if bp_fired:
                self._paused = True
            paused_now = self._paused
        # Let the host refresh debug views for this sample (paused or not).
        if self._on_sample is not None:
            try:
                self._on_sample(sample_index, paused_now)
            except Exception:
                pass

        with self._cv:
            # Block while paused; wake on resume()/step()/stop() (notify_all).
            while self._paused and not self._stop_flag:
                self._cv.wait()
            if self._stop_flag:
                raise BatchAborted()
            # Single-step: ran one sample → re-arm the pause for the next.
            if self._step:
                self._step = False
                self._paused = True
            delay = self._delay_s

        # Speed pacing (slow-motion), outside the lock. Re-check stop after.
        if delay > 0.0:
            time.sleep(delay)
            with self._cv:
                if self._stop_flag:
                    raise BatchAborted()
