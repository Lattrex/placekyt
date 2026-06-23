"""Deferred-delivery event bus for the placeKYT data model.

The model layer emits events (``cell_placed``, ``connection_added``,
``param_changed``, ...) and the UI layer registers callbacks that update Qt
widgets. This keeps ``model/`` free of any Qt dependency (the architecture notes §6).

**Deferred delivery (§6):** ``emit()`` does NOT invoke callbacks. It appends the
event to an internal queue. Callbacks fire only when ``flush()`` is called. This
prevents re-entrant emission, callbacks observing half-completed composite
operations, and infinite cascades. The explicit flush() contract (who is
allowed to call it) lives in the command/engine layers, not here.

This module has no threading — all dispatch happens on the calling thread.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger("placekyt.model.events")

# Guard against runaway cascades: a callback that emits during flush() has its
# events drained in the same pass (breadth-first). If a single drain exceeds
# this many events it is almost certainly an infinite loop. Sized to comfortably
# clear large composite operations (a 69-cell block placement emits ~140 events).
MAX_EVENTS_PER_DRAIN = 10_000

# Callback signature: fn(event_type: str, **payload) -> None
Callback = Callable[..., None]


@dataclass(frozen=True)
class Event:
    """A queued model event: a type string plus an arbitrary keyword payload."""

    type: str
    payload: dict[str, Any]


class EventBus:
    """Observer bus with queue-and-flush (deferred) dispatch.

    Subscribe with :meth:`subscribe` (specific event type) or
    :meth:`subscribe_all` (every event). Producers call :meth:`emit`; the queue
    is drained only by :meth:`flush`.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Callback]] = {}
        self._global_subscribers: list[Callback] = []
        self._queue: deque[Event] = deque()
        self._draining = False

    # -- subscription ---------------------------------------------------------

    def subscribe(self, event_type: str, callback: Callback) -> Callable[[], None]:
        """Register ``callback`` for a specific event type.

        Returns an unsubscribe thunk for convenience.
        """
        self._subscribers.setdefault(event_type, []).append(callback)
        return lambda: self.unsubscribe(event_type, callback)

    def subscribe_all(self, callback: Callback) -> Callable[[], None]:
        """Register ``callback`` for *every* emitted event type."""
        self._global_subscribers.append(callback)
        return lambda: self._global_subscribers.remove(callback)

    def unsubscribe(self, event_type: str, callback: Callback) -> None:
        """Remove a specific-type subscription if present (no error if absent)."""
        handlers = self._subscribers.get(event_type)
        if handlers and callback in handlers:
            handlers.remove(callback)

    # -- production -----------------------------------------------------------

    def emit(self, event_type: str, **payload: Any) -> None:
        """Queue an event. Does NOT dispatch — call :meth:`flush` to deliver."""
        self._queue.append(Event(event_type, payload))

    def clear(self) -> None:
        """Discard all queued, undelivered events.

        Used by ``CompositeCommand`` rollback (§6): the queue is cleared before
        the rollback undo() calls run, so UI callbacks never see a mix of
        execute and rollback events.
        """
        self._queue.clear()

    @property
    def pending(self) -> int:
        """Number of queued events not yet flushed (useful for tests)."""
        return len(self._queue)

    # -- delivery -------------------------------------------------------------

    def flush(self) -> None:
        """Dispatch all queued events, breadth-first, until the queue is empty.

        Events emitted by callbacks during the drain are appended and processed
        in the same pass. A callback that raises has its traceback logged; the
        exception never propagates to the caller, and remaining callbacks for
        that event still run. Re-entrant ``flush()`` calls are no-ops (the
        outermost drain owns the queue).
        """
        if self._draining:
            # A callback called flush() again; the in-progress drain will pick
            # up anything it queued. Avoid nested drains.
            return

        self._draining = True
        try:
            dispatched = 0
            while self._queue:
                event = self._queue.popleft()
                dispatched += 1
                if dispatched > MAX_EVENTS_PER_DRAIN:
                    logger.warning(
                        "EventBus.flush exceeded %d events in one drain "
                        "(likely an infinite cascade) — truncating; "
                        "%d events discarded.",
                        MAX_EVENTS_PER_DRAIN,
                        len(self._queue),
                    )
                    self._queue.clear()
                    break
                self._dispatch_one(event)
        finally:
            self._draining = False

    def _dispatch_one(self, event: Event) -> None:
        # Snapshot handler lists so that (un)subscriptions made by a callback
        # don't mutate the list being iterated.
        handlers = list(self._subscribers.get(event.type, ()))
        handlers.extend(self._global_subscribers)
        for handler in handlers:
            try:
                handler(event.type, **event.payload)
            except Exception:  # noqa: BLE001 — bus must isolate callback faults
                logger.exception(
                    "event callback for %r raised; continuing dispatch",
                    event.type,
                )
