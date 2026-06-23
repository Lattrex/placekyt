"""WaveformView — a GTKWave-style time-series viewer (DEBUG_ARCHITECTURE §3.3).

A custom-painted widget that draws the design's port streams as stacked rows,
each an **analog step-function** (Q15 signal) or a **bus trace** (Hex/Dec/Bin
labelled segments), with a left **value gutter** (each stream's value at the
main cursor), a **main cursor** (left-click → the shared time cursor) and a
**measurement cursor** (middle-click → Δt to the main cursor for reading
period/frequency/latency by hand). Zoom-to-region between the two cursors.

Pure Qt/QPainter — no new dependency (keeps packaging clean). Reads streams as
``{(chip, port): [(time_ns, value)]}`` (see ``TraceModel.port_streams``); it
holds no model reference, just the plotted data + view state.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QWidget

from ui.canvas.cell_item import block_palette_color

# Layout metrics (logical px).
_GUTTER_W = 150       # left value-gutter width
_ROW_H = 56           # default per-stream row height (per-trace overridable)
_ROW_GAP = 6          # vertical gap between rows
_TOP_PAD = 8
_RULER_H = 20         # bottom time-ruler height
_MIN_ROW_H = 24       # floor for a resized trace
_RESIZE_GRIP = 5      # px band at a row's bottom edge for the height-drag grip
_DRAG_THRESHOLD = 4   # px the mouse must move before a gutter press = a drag
_GUTTER_LINE_H = 30   # px per member label in a group gutter (matches paint)
_STACK_FRINGE = 0.10  # top/bottom fraction of a row that is "the gap" (no stack)

_BG = QColor(28, 30, 34)
_GUTTER_BG = QColor(38, 41, 46)
_GRID = QColor(60, 64, 70)
_LABEL = QColor(225, 225, 225)
_SUBLABEL = QColor(160, 165, 172)
_TRACE = QColor(90, 200, 255)        # analog trace (cyan)
_BUS = QColor(120, 220, 140)         # bus-trace outline (green)
_BUS_TEXT = QColor(230, 230, 230)
_MAIN_CURSOR = QColor(255, 215, 60)  # main time cursor (yellow)
_MEAS_CURSOR = QColor(255, 120, 200) # measurement cursor (magenta)

# Per-stream radix options.
RADIX_ANALOG = "Analog"
RADIX_HEX = "Hex"
RADIX_DEC = "Dec"
RADIX_BIN = "Bin"
RADICES = [RADIX_ANALOG, RADIX_HEX, RADIX_DEC, RADIX_BIN]


def _q15(v: int) -> float:
    """uint16 → signed → Q15 float in [-1, 1)."""
    s = v - 0x10000 if v >= 0x8000 else v
    return s / 32768.0


# 'Nice' tick generation lives in a shared, Qt-free module so the time ruler,
# the analog amplitude scale, and the timeline scrubber all use one algorithm.
from ui.widgets.ruler_ticks import nice_ticks as _nice_ticks  # noqa: E402


def _fmt_value(v: int, radix: str) -> str:
    v &= 0xFFFF
    if radix == RADIX_HEX:
        return f"0x{v:04X}"
    if radix == RADIX_DEC:
        return str(v - 0x10000 if v >= 0x8000 else v)
    if radix == RADIX_BIN:
        return f"{v:016b}"
    return f"{_q15(v):+.4f}"  # Analog → show the Q15 value


class WaveformView(QWidget):
    """Stacked analog/bus traces over the design's port streams.

    Streams are ``[(label, (chip, port), [(t_ns, value)], radix)]`` internally.
    The widget owns its view window (``_t0``/``_t1`` in ns) and the two cursors.
    """

    # The user moved the main cursor (left-click) → the shared time cursor.
    cursor_requested = Signal(float)
    # A stream's radix changed (via the right-click gutter menu): (row, radix).
    radix_changed = Signal(int, str)
    # The trace list changed (color/delete/height) — the panel may persist it.
    streams_changed = Signal()
    # The measurement (middle-click) cursor moved — refresh the Δt/freq readout.
    measurement_changed = Signal()
    # A register was dropped from the Program pane: (chip, x, y, addr).
    register_dropped = Signal(int, int, int, int)
    # A chip PORT was dropped (from the canvas): (chip, port_name). The panel
    # opens a channel/tag picker so the user views one demuxed stream.
    port_dropped = Signal(int, str)
    # A ROUTE (connection) was dropped from the canvas: connection_name. The panel
    # plots the channels flowing through it (same picker).
    route_dropped = Signal(str)
    # The time window (_t0/_t1) changed — the fixed header ruler follows it.
    window_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(120)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)  # register drag from the Program pane
        # Plotted streams: list of dicts
        #   {label, key, samples, radix, color, height, amp_scale}.
        self._streams: list[dict] = []
        self._t0 = 0.0       # view window start (ns, or sample index in index mode)
        self._t1 = 1.0       # view window end
        # X-axis mode: "time" (real ns) or "index" (per-stream sample ordinal —
        # 0,1,2,… like GRC's Time Sink, so the two can be lined up 1:1). In index
        # mode each trace's samples are remapped to (ordinal, value) for painting;
        # the stored time-keyed samples are untouched.
        self._x_mode = "time"
        # Optional callback ``() -> [(menu_label, on_trigger_callable)]`` supplying
        # the channels that can be ADDED via the right-click "Add channel" submenu
        # (set by the panel, which knows the trace model). None → no submenu.
        self._channel_provider = None
        self._main_ns: float | None = None   # main cursor time
        self._meas_ns: float | None = None   # measurement cursor time
        # Height-resize drag state (dragging a row's bottom edge).
        self._resize_row = -1
        self._resize_y0 = 0.0
        self._resize_h0 = 0.0
        # Trace-drag state (dragging a gutter label to overlay / reorder).
        # A gutter press arms a drag; it only becomes a real drag once the mouse
        # moves past _DRAG_THRESHOLD px — a plain click selects (does not move).
        self._drag_stream = -1
        self._press_y = 0.0
        self._dragging = False
        # The selected trace (highlighted in the gutter, the drag subject).
        self._selected = -1

    # -- data -----------------------------------------------------------------

    def _digital_min_height(self) -> int:
        """The minimum height for a digital (bus) trace — just enough for the
        gutter's two text lines (label + value). Digital traces default to this
        compact height; analog traces want the taller default for waveform
        detail."""
        fm = QFontMetrics(self.font())
        return max(_MIN_ROW_H, 2 * fm.height() + 6)

    def _default_height(self, radix: str) -> int:
        return _ROW_H if radix == RADIX_ANALOG else self._digital_min_height()

    def _new_settings(self, idx: int) -> dict:
        """Default per-trace display settings for a brand-new trace."""
        return {
            "radix": RADIX_ANALOG,
            "color": block_palette_color(idx),   # rotating palette (like blocks)
            "height": _ROW_H,
            "amp_scale": 1.0,
            "group": idx,                         # own row (overlay = shared id)
            "initial": None,                      # value before the first sample
        }

    def set_streams(self, streams: dict, *, keep_radix: bool = True) -> None:
        """Refresh the PORT streams from ``{(chip, port): [(t_ns, value)]}`` while
        PRESERVING user-added register traces, per-trace settings, ordering, and
        overlay grouping. Each port becomes one trace whose ``key`` is
        ``("port", chip, port)``; only its samples are updated on a refresh."""
        # Index existing streams by key to preserve their settings + position.
        existing = {s["key"]: s for s in self._streams}
        port_keys = set()
        for key in sorted(streams):
            chip, port = key
            skey = ("port", chip, port)
            port_keys.add(skey)
            samples = sorted(streams[key])
            if skey in existing:
                existing[skey]["samples"] = samples  # refresh data only
            else:
                # New port trace — append, keeping any pre-existing order. An
                # analog port reads 0 before its first sample (#initial value).
                st = self._new_settings(len(self._streams))
                st["initial"] = 0
                self._streams.append({
                    "label": (f"chip{chip}.{port}" if chip is not None
                              else str(port)),
                    "key": skey,
                    "source": {"type": "port", "chip": chip, "port": port},
                    "samples": samples,
                    **st,
                })
        # Drop port traces that no longer exist (register traces are kept).
        self._streams = [s for s in self._streams
                         if s["key"][0] != "port" or s["key"] in port_keys]
        # keep_radix kept for API compatibility (settings always persist now).
        _ = keep_radix
        if self._selected >= len(self._streams):
            self._selected = -1
        self._fit_time_window()
        self._update_content_height()
        self.updateGeometry()
        self.update()

    def add_register_stream(self, chip: int, x: int, y: int, addr: int,
                            samples: list, *, label: str | None = None,
                            initial: int | None = None) -> None:
        """Add a REGISTER trace (value-over-time of one cell register), e.g.
        dragged from the Program pane. Defaults to a HEX bus display (a register
        is control/data, not an analog signal). ``samples`` is
        ``[(t_ns, value)]`` (from ``TraceModel.register_stream``). ``initial`` is
        the register's programmed/reset value, shown before the first write (a
        register always has one; None → an 'unknown' hashed segment)."""
        skey = ("reg", chip, x, y, addr)
        for s in self._streams:                       # update if already present
            if s["key"] == skey:
                s["samples"] = sorted(samples)
                if initial is not None:
                    s["initial"] = initial
                self.update()
                return
        st = self._new_settings(len(self._streams))
        st["radix"] = RADIX_HEX                        # registers default to hex
        st["height"] = self._digital_min_height()      # compact: just text tall
        st["initial"] = initial
        self._streams.append({
            "label": label or f"c{chip}:({x},{y}).R{addr}",
            "key": skey,
            "source": {"type": "register", "chip": chip, "x": x, "y": y,
                       "addr": addr},
            "samples": sorted(samples),
            **st,
        })
        self._fit_time_window()
        self._update_content_height()
        self.updateGeometry()
        self.update()
        self.streams_changed.emit()

    def add_port_stream(self, chip: int, port: str, tag, samples: list,
                        *, label: str | None = None) -> None:
        """Add a DEMUXED PORT trace — one tag/channel of a multiplexed port (e.g.
        the user dragged a port to the viewer and picked a stream). ``tag`` is the
        per-stream key (WRITE dest / target addr, or None for an untagged
        single-stream port). Distinct from the plain ``("port", …)`` traces that
        ``set_streams`` manages: a tagged trace's key is ``("ptag", chip, port,
        tag)`` and is refreshed from the demuxed model on each live tick."""
        skey = ("ptag", chip, port, tag)
        for s in self._streams:                        # update if already present
            if s["key"] == skey:
                s["samples"] = sorted(samples)
                self.update()
                return
        st = self._new_settings(len(self._streams))
        st["initial"] = 0
        self._streams.append({
            "label": label or (f"chip{chip}.{port}"
                               + ("" if tag is None else f" [tag {tag}]")),
            "key": skey,
            "source": {"type": "port_tag", "chip": chip, "port": port,
                       "tag": tag},
            "samples": sorted(samples),
            **st,
        })
        self._fit_time_window()
        self._update_content_height()
        self.updateGeometry()
        self.update()
        self.streams_changed.emit()

    def update_register_samples(self, fetch) -> None:
        """Refresh every register trace's samples via ``fetch(chip,x,y,addr) ->
        [(t,v)]`` (called on each live refresh so register traces keep up)."""
        for s in self._streams:
            src = s.get("source", {})
            if src.get("type") == "register":
                s["samples"] = sorted(fetch(src["chip"], src["x"], src["y"],
                                            src["addr"]))

    def update_port_tag_samples(self, fetch) -> None:
        """Refresh every DEMUXED port trace's samples via
        ``fetch(chip, port, tag) -> [(t,v)]`` (called on each live refresh so a
        demuxed port trace keeps up with the run, like register traces)."""
        for s in self._streams:
            src = s.get("source", {})
            if src.get("type") == "port_tag":
                s["samples"] = sorted(
                    fetch(src["chip"], src["port"], src["tag"]) or [])

    def register_sources(self) -> list[tuple]:
        """``(chip,x,y,addr)`` of every register trace (so the host can fetch
        their samples)."""
        return [(s["source"]["chip"], s["source"]["x"], s["source"]["y"],
                 s["source"]["addr"])
                for s in self._streams
                if s.get("source", {}).get("type") == "register"]

    # -- serialization (signal-list save/load, YAML) --------------------------

    def to_signal_list(self) -> list[dict]:
        """A plain-data description of every trace (source + display settings)
        for saving to YAML. Re-resolvable via :meth:`from_signal_list`."""
        out = []
        for s in self._streams:
            out.append({
                "source": dict(s["source"]),
                "label": s["label"],
                "radix": s["radix"],
                "color": QColor(s["color"]).name(),
                "height": int(s.get("height", _ROW_H)),
                "amp_scale": float(s.get("amp_scale", 1.0)),
                "group": int(s.get("group", 0)),
                "initial": (None if s.get("initial") is None
                            else int(s["initial"])),
            })
        return out

    def from_signal_list(self, items: list, resolve) -> None:
        """Rebuild the trace list from a saved signal list. ``resolve(source)``
        returns the current ``[(t,v)]`` samples for a source descriptor (port or
        register) — the data isn't saved, only the signal identity + settings."""
        self._streams = []
        self._selected = -1
        for it in items or ():
            src = it.get("source", {})
            samples = sorted(resolve(src) or [])
            stype = src.get("type")
            if stype == "port":
                key = ("port", src["chip"], src["port"])
            elif stype == "port_tag":
                key = ("ptag", src["chip"], src["port"], src.get("tag"))
            else:
                key = ("reg", src["chip"], src["x"], src["y"], src["addr"])
            self._streams.append({
                "label": it.get("label", str(key)),
                "key": key,
                "source": dict(src),
                "samples": samples,
                "radix": it.get("radix", RADIX_ANALOG),
                "color": QColor(it.get("color", "#5ac8ff")),
                "height": int(it.get("height", _ROW_H)),
                "amp_scale": float(it.get("amp_scale", 1.0)),
                "group": int(it.get("group", 0)),
                "initial": (0 if src.get("type") == "port"
                            else it.get("initial")),
            })
        self._fit_time_window()
        self._update_content_height()
        self.updateGeometry()
        self.update()
        self.streams_changed.emit()

    def clear(self) -> None:
        self._streams = []
        self._selected = -1
        self._main_ns = self._meas_ns = None
        self._t0, self._t1 = 0.0, 1.0
        self.update()

    def stream_count(self) -> int:
        return len(self._streams)

    def set_radix(self, row: int, radix: str) -> None:
        if 0 <= row < len(self._streams) and radix in RADICES:
            s = self._streams[row]
            prev = s["radix"]
            s["radix"] = radix
            # Adjust height to the new radix's default ONLY if the trace is still
            # at the old radix's default (i.e. the user hasn't hand-resized it):
            # digital → compact (text-tall), analog → the taller waveform row.
            if s.get("height") == self._default_height(prev):
                s["height"] = self._default_height(radix)
            # Digital traces never stack — if this trace was overlaid with
            # others, split it out into its own pane (#160).
            if radix != RADIX_ANALOG:
                grp = self._group_of(row)
                if len(grp) > 1:
                    self._split_to_own_pane(row)
            self._update_content_height()
            self.updateGeometry()
            self.update()
            self.radix_changed.emit(row, radix)
            self.streams_changed.emit()

    def _group_of(self, row: int) -> list[int]:
        """The list of stream indices sharing ``row``'s rendered group."""
        for grp in self._groups():
            if row in grp:
                return grp
        return [row]

    def _split_to_own_pane(self, row: int) -> None:
        """Move ``row`` out of its overlay group into its own pane (kept right
        after the group so ordering is stable)."""
        grp = self._group_of(row)
        s = self._streams.pop(row)
        s["group"] = self._fresh_group_id()
        rest = [g - 1 if g > row else g for g in grp if g != row]
        pos = (max(rest) + 1) if rest else len(self._streams)
        self._streams.insert(pos, s)
        self._selected = self._streams.index(s)

    def radix_of(self, row: int) -> str:
        return self._streams[row]["radix"] if 0 <= row < len(self._streams) else ""

    def stream_labels(self) -> list[str]:
        return [s["label"] for s in self._streams]

    # -- cursor ---------------------------------------------------------------

    def set_main_cursor(self, ns: float | None) -> None:
        """Move the main cursor (e.g. driven by the shared cursor). No signal —
        this is the inbound direction."""
        self._main_ns = None if ns is None else float(ns)
        self.update()

    @property
    def measurement_dt(self) -> float | None:
        """Δt between the measurement and main cursors (ns), or None."""
        if self._main_ns is None or self._meas_ns is None:
            return None
        return abs(self._meas_ns - self._main_ns)

    def latency_ns(self) -> float | None:
        """Input→output latency: first capture time − first injection time,
        across all streams (DEBUG §3.3). None if either side is absent."""
        first_in = first_out = None
        for s in self._streams:
            src = s.get("source", {})
            port = src.get("port", "")
            if not s["samples"]:
                continue
            t0 = s["samples"][0][0]
            # Heuristic: 'in' ports feed injection, others are captures. The
            # trace tags injection vs capture by kind, but port_streams merges
            # them; we use the port-name convention (…_in) as the input marker.
            if str(port).endswith("_in"):
                first_in = t0 if first_in is None else min(first_in, t0)
            else:
                first_out = t0 if first_out is None else min(first_out, t0)
        if first_in is None or first_out is None:
            return None
        return max(0.0, first_out - first_in)

    def amplitude_of(self, row: int) -> tuple[int, int] | None:
        """(min, max) signed sample value of a stream, for the amplitude
        measurement. None if empty."""
        if not (0 <= row < len(self._streams)):
            return None
        vals = [v - 0x10000 if v >= 0x8000 else v
                for _t, v in self._streams[row]["samples"]]
        return (min(vals), max(vals)) if vals else None

    # -- time/pixel mapping ---------------------------------------------------

    def _data_bounds(self):
        """``(lo, hi)`` bounds across all streams' samples, or ``None``. In index
        mode the bound is ``(0, max sample count)`` — one column per sample."""
        if self._x_mode == "index":
            n = max((len(s["samples"]) for s in self._streams), default=0)
            return (0.0, float(max(1, n))) if any(
                s["samples"] for s in self._streams) else None
        lo = hi = None
        for s in self._streams:
            for t, _v in s["samples"]:
                lo = t if lo is None else min(lo, t)
                hi = t if hi is None else max(hi, t)
        if lo is None:
            return None
        if hi <= lo:
            hi = lo + 1.0
        return lo, hi

    def set_x_axis_mode(self, mode: str) -> None:
        """Switch the x-axis between real time ('time', ns) and per-stream sample
        ordinal ('index', 0,1,2,… — so the plot lines up 1:1 with GRC's Time Sink,
        which plots by sample index). Re-fits the window + repaints."""
        if mode not in ("time", "index") or mode == self._x_mode:
            return
        self._x_mode = mode
        self._main_ns = self._meas_ns = None      # cursors are in the old units
        self._fit_time_window()
        self.update()

    def x_axis_mode(self) -> str:
        return self._x_mode

    def _fit_time_window(self) -> None:
        b = self._data_bounds()
        if b is None:
            self._t0, self._t1 = 0.0, 1.0
        else:
            lo, hi = b
            pad = (hi - lo) * 0.04
            self._t0, self._t1 = lo - pad, hi + pad
        self.window_changed.emit()

    def _clamp_window(self) -> None:
        """Keep the view inside the data (plus a small margin) so the user can't
        pan/zoom out into empty space before the first or after the last sample.
        The visible span is preserved where possible (a pan just stops at the
        edge); only a span wider than the data collapses to the full data range."""
        b = self._data_bounds()
        if b is not None:
            lo, hi = b
            pad = max(1.0, (hi - lo) * 0.04)
            lo -= pad
            hi += pad
            span = self._t1 - self._t0
            full = hi - lo
            if span >= full:                   # zoomed out past the data → fit
                self._t0, self._t1 = lo, hi
            elif self._t0 < lo:                # panned off the left edge
                self._t0, self._t1 = lo, lo + span
            elif self._t1 > hi:                # panned off the right edge
                self._t1, self._t0 = hi, hi - span
        self.window_changed.emit()

    def _plot_rect(self) -> QRectF:
        # The time ruler now lives in a fixed header above the scroll area, so
        # the view uses its full height for traces.
        return QRectF(_GUTTER_W, 0,
                      max(1, self.width() - _GUTTER_W),
                      max(1, self.height()))

    def time_window(self) -> tuple[float, float]:
        """The visible ``(t0, t1)`` window in ns (for the header ruler)."""
        return self._t0, self._t1

    def plot_left(self) -> int:
        """X of the plot area's left edge (where the gutter ends) — the header
        ruler aligns its ticks to this."""
        return _GUTTER_W

    def _groups(self):
        """Group consecutive streams sharing a ``group`` id — they render
        OVERLAID in one row. Returns a list of lists of stream indices, in
        display order."""
        groups: list[list[int]] = []
        last_gid = object()
        for i, s in enumerate(self._streams):
            gid = s.get("group", i)
            if groups and gid == last_gid:
                groups[-1].append(i)
            else:
                groups.append([i])
                last_gid = gid
        return groups

    def _group_rows(self):
        """Yield ``(group_indices, top, height)`` per rendered row. A group's
        height is the max of its members' heights."""
        top = _TOP_PAD
        for grp in self._groups():
            h = max(self._streams[i].get("height", _ROW_H) for i in grp)
            yield grp, top, h
            top += h + _ROW_GAP

    def _row_tops(self):
        """Yield ``(i, top, height)`` for the FIRST stream of each rendered row
        (back-compat for callers that key by a single trace per row)."""
        for grp, top, h in self._group_rows():
            yield grp[0], top, h

    def _row_at_y(self, y: float):
        """``(group_indices, top, height)`` of the rendered row at ``y``, or
        ``None``."""
        for grp, top, h in self._group_rows():
            if top <= y < top + h:
                return grp, top, h
        return None

    def _stream_at_y(self, y: float) -> int:
        """The first stream index of the row at ``y`` (or -1)."""
        row = self._row_at_y(y)
        return row[0][0] if row else -1

    def _member_at_y(self, y: float) -> int:
        """The EXACT stream index whose gutter label is at ``y`` (each member of
        an overlaid row gets its own ``_GUTTER_LINE_H`` gutter band). Lets the
        user click/drag a single overlaid trace, not just the row's first. Falls
        back to the row's first stream if ``y`` is past the listed members."""
        row = self._row_at_y(y)
        if row is None:
            return -1
        grp, top, h = row
        idx = int((y - (top + 2)) // _GUTTER_LINE_H)
        if 0 <= idx < len(grp):
            return grp[idx]
        return grp[0]

    def _resize_grip_at_y(self, y: float) -> int:
        """First-stream index of the row whose BOTTOM-edge resize grip is at
        ``y`` (for height drag), or -1."""
        for grp, top, h in self._group_rows():
            if abs(y - (top + h)) <= _RESIZE_GRIP:
                return grp[0]
        return -1

    def _t_to_x(self, t: float) -> float:
        r = self._plot_rect()
        span = self._t1 - self._t0 or 1.0
        return r.left() + (t - self._t0) / span * r.width()

    def _x_to_t(self, x: float) -> float:
        r = self._plot_rect()
        span = self._t1 - self._t0 or 1.0
        return self._t0 + (x - r.left()) / max(1.0, r.width()) * span

    def _content_height(self) -> int:
        h = _TOP_PAD
        for s in self._streams:
            h += s.get("height", _ROW_H) + _ROW_GAP
        return max(120, h)

    def _update_content_height(self) -> None:
        """Request the full content height so the enclosing QScrollArea scrolls
        (with setWidgetResizable, the widget grows to its minimum height)."""
        self.setMinimumHeight(self._content_height())

    def sizeHint(self):  # noqa: N802
        from PySide6.QtCore import QSize
        return QSize(600, self._content_height())

    # -- value lookup ---------------------------------------------------------

    @staticmethod
    def _value_at(samples, t: float):
        """The held value of a step-function stream at time ``t`` (the last
        sample at/<= t). None before the first sample."""
        val = None
        for st, sv in samples:
            if st <= t:
                val = sv
            else:
                break
        return val

    def _lead_samples(self, s) -> list:
        """``s``'s samples with its INITIAL value prepended at the view start so
        the leading region (before the first real sample) is drawn with that
        value — e.g. an analog port reads 0, a register reads its programmed
        value. Returns the samples unchanged when there's no initial value (the
        bus then draws an 'unknown' segment for the lead)."""
        samples = s["samples"]
        if self._x_mode == "index":
            # Replace each sample's timestamp with its per-stream ordinal so the
            # k-th sample plots at x=k (GRC-style sample-index axis). The initial
            # value occupies index -? — we just hold the first sample's value back
            # to the left edge by prepending it at index 0 region via the painter's
            # lead-hold, so no synthetic lead is needed here.
            idx = [(float(i), v) for i, (_t, v) in enumerate(samples)]
            init = s.get("initial")
            if init is not None and idx and idx[0][0] > self._t0:
                idx = [(self._t0, init & 0xFFFF)] + idx
            return idx
        init = s.get("initial")
        if init is None:
            return samples
        if samples and samples[0][0] <= self._t0:
            return samples                       # already covers the left edge
        lead_t = self._t0 if not samples else min(self._t0, samples[0][0])
        return [(lead_t, init & 0xFFFF)] + list(samples)

    # -- painting -------------------------------------------------------------

    def paintEvent(self, _ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), _BG)
        r = self._plot_rect()

        if not self._streams:
            p.setPen(QPen(_SUBLABEL))
            p.drawText(self.rect(), Qt.AlignCenter,
                       "No streams — run a simulation")
            p.end()
            return

        fm = QFontMetrics(self.font())
        # Traces + grid + cursors are CLIPPED to the plot rect so nothing draws
        # over (or into) the left gutter when the view is scrolled.
        p.save()
        p.setClipRect(r)
        for grp, top, h in self._group_rows():
            row = QRectF(r.left(), top, r.width(), h)
            self._paint_grid(p, row)
            if self._group_is_analog(grp):
                self._paint_analog_grid(p, grp, row)
            # Overlaid analog traces share ONE amplitude scale (the pane's, taken
            # from its first member) so they're directly comparable (#158). The
            # pane's combined [min,max] maps to the FULL row height so the lowest
            # sample sits at the bottom and no vertical space is wasted (#8).
            pane_amp = self._streams[grp[0]].get("amp_scale", 1.0)
            vrange = self._pane_value_range(grp)
            for i in grp:
                s = self._streams[i]
                color = s.get("color", _TRACE)
                lead = self._lead_samples(s)
                if s["radix"] == RADIX_ANALOG:
                    self._paint_analog(p, row, lead, color, pane_amp, vrange)
                else:
                    self._paint_bus(p, row, lead, s["radix"], fm, color,
                                    unknown_lead=(s.get("initial") is None
                                                  and not s["samples"]))
        self._paint_cursor(p, self._main_ns, _MAIN_CURSOR)
        self._paint_cursor(p, self._meas_ns, _MEAS_CURSOR)
        p.restore()

        # Opaque gutter LAST, on top of everything — the signal name + value must
        # never be seen through by the waveforms.
        p.fillRect(QRectF(0, 0, _GUTTER_W, self.height()), _GUTTER_BG)
        p.setPen(QPen(_GRID, 0))
        p.drawLine(_GUTTER_W, 0, _GUTTER_W, self.height())  # gutter divider
        for grp, top, h in self._group_rows():
            self._paint_group_gutter(p, grp, top, h, fm)
        p.end()

    def _paint_grid(self, p: QPainter, row: QRectF) -> None:
        p.setPen(QPen(_GRID, 0))
        p.drawLine(int(row.left()), int(row.bottom()),
                   int(row.right()), int(row.bottom()))

    def _pane_value_range(self, grp) -> tuple[float, float]:
        """The combined Q15 [min, max] across all ANALOG members of a pane, used
        to map the data to the full row height (lowest sample → bottom). Falls
        back to the full [-1, 1] range when the pane is flat/empty."""
        lo = hi = None
        for i in grp:
            s = self._streams[i]
            if s["radix"] != RADIX_ANALOG:
                continue
            for _t, v in self._lead_samples(s):   # include the leading value
                q = _q15(v)
                lo = q if lo is None else min(lo, q)
                hi = q if hi is None else max(hi, q)
        if lo is None or hi - lo < 1e-6:
            # Flat or empty → centre a symmetric range so a constant sits mid-row.
            c = lo if lo is not None else 0.0
            return c - 1.0, c + 1.0
        return lo, hi

    def _paint_analog(self, p: QPainter, row: QRectF, samples,
                      color=_TRACE, amp_scale: float = 1.0,
                      vrange: tuple[float, float] = (-1.0, 1.0)) -> None:
        if not samples:
            return
        # Map the pane's [vmin, vmax] across the FULL row height: the lowest
        # sample sits at the bottom, the highest at the top (#8). ``amp_scale``
        # magnifies about the data centre (Ctrl+wheel) for small detail; the
        # trace is clipped to the row so a magnified signal can't bleed out.
        vmin, vmax = vrange
        vmid = 0.5 * (vmin + vmax)
        ytop, ybot = row.top() + 1, row.bottom() - 1
        usable = (ybot - ytop)
        span = (vmax - vmin) or 1.0
        # px per unit value (scaled by amp_scale, about the data centre).
        ppu = (usable / span) * amp_scale
        ymid = 0.5 * (ytop + ybot)
        p.setPen(QPen(QColor(color), 1.5))
        prev_x = prev_y = None
        for t, v in samples:
            x = self._t_to_x(t)
            y = ymid - (_q15(v) - vmid) * ppu
            y = max(ytop, min(ybot, y))  # clamp within the row
            if prev_x is not None:
                p.drawLine(int(prev_x), int(prev_y), int(x), int(prev_y))  # hold
                p.drawLine(int(x), int(prev_y), int(x), int(y))            # step
            prev_x, prev_y = x, y
        # extend the last held value to the row's right edge
        if prev_x is not None:
            p.drawLine(int(prev_x), int(prev_y), int(row.right()), int(prev_y))

    def _paint_bus(self, p: QPainter, row: QRectF, samples, radix, fm,
                   color=_BUS, unknown_lead: bool = False) -> None:
        """GTKWave bus trace: a labelled hex/dec/bin segment per held value,
        drawn as a flat band with the value centered. Each transition is a
        single symmetric 'X' crossover centred on the boundary (both sides
        chamfer in to a point at the mid-line with the SAME angle); the first
        sample opens from a point and the last closes to a point — so every
        chamfer is identical and nothing overlaps.

        ``unknown_lead`` draws a cross-hatched 'unknown' band for the whole row
        when the value is genuinely unknown (no samples and no initial value —
        normally only an unwritten input/output port)."""
        if not samples:
            if unknown_lead:
                self._paint_unknown(p, row, row.left(), row.right())
            return
        bus = QColor(color)
        y0 = row.top() + row.height() * 0.2
        y1 = row.bottom() - row.height() * 0.2
        ymid = 0.5 * (y0 + y1)
        p.setPen(QPen(bus, 1.4))
        # Boundary x of each held value, plus the right edge as the final close.
        bounds = [self._t_to_x(t) for t, _v in samples]
        right = row.right()
        # Per-segment chamfer: half the smaller neighbouring gap, capped, so the
        # opening and closing chamfers of a segment are equal and never overlap.
        def cw_at(i):
            left_gap = bounds[i] - (bounds[i - 1] if i > 0 else bounds[i] - 8)
            right_gap = ((bounds[i + 1] if i + 1 < len(bounds) else right)
                         - bounds[i])
            return max(0.0, min(4.0, left_gap * 0.5, right_gap * 0.5))
        for k, (t, v) in enumerate(samples):
            x = bounds[k]
            xn = bounds[k + 1] if k + 1 < len(bounds) else right
            cwl = cw_at(k)                                   # opening chamfer
            cwr = (cw_at(k + 1) if k + 1 < len(samples)
                   else min(4.0, (xn - x) * 0.5))            # closing chamfer
            # Rails, inset by the chamfer at each end.
            p.drawLine(int(x + cwl), int(y0), int(xn - cwr), int(y0))
            p.drawLine(int(x + cwl), int(y1), int(xn - cwr), int(y1))
            # Opening: from the mid-line point at x out to the rails (half-X).
            p.drawLine(int(x), int(ymid), int(x + cwl), int(y0))
            p.drawLine(int(x), int(ymid), int(x + cwl), int(y1))
            # Closing: rails converge back to the mid-line point at xn (half-X).
            p.drawLine(int(xn - cwr), int(y0), int(xn), int(ymid))
            p.drawLine(int(xn - cwr), int(y1), int(xn), int(ymid))
            txt = _fmt_value(v, radix)
            band = QRectF(x + cwl, y0, max(1.0, (xn - x) - cwl - cwr), y1 - y0)
            if band.width() > fm.horizontalAdvance(txt) + 6:
                p.setPen(QPen(_BUS_TEXT))
                p.drawText(band, Qt.AlignCenter, txt)
                p.setPen(QPen(bus, 1.4))

    def _paint_unknown(self, p: QPainter, row: QRectF, x0: float,
                       x1: float) -> None:
        """A cross-hatched 'unknown' band between ``x0`` and ``x1`` — the value
        is genuinely unknown (an unwritten register/port with no initial)."""
        y0 = row.top() + row.height() * 0.2
        y1 = row.bottom() - row.height() * 0.2
        unk = QColor(200, 90, 90)
        p.setPen(QPen(unk, 1.2))
        p.drawRect(QRectF(x0, y0, max(1.0, x1 - x0), y1 - y0))
        # diagonal hatch fill
        step = 6
        xx = x0
        while xx < x1:
            p.drawLine(int(xx), int(y1), int(min(x1, xx + (y1 - y0))), int(y0))
            xx += step

    def _gutter_value(self, s):
        """The value to show in the gutter for stream ``s``: the held value at
        the main cursor, or — when there's no cursor or the cursor is before the
        first sample — the stream's first/constant value so a constant signal
        always shows its value (not '—')."""
        samples = s["samples"]
        init = s.get("initial")
        if self._main_ns is not None:
            v = self._value_at(samples, self._main_ns)
            if v is not None:
                return v
            # cursor is before the first sample → show the initial value
            if init is not None:
                return init & 0xFFFF
        if samples:
            return samples[0][1]   # no cursor → show the value
        return init & 0xFFFF if init is not None else None

    def _paint_group_gutter(self, p, grp, top, h, fm) -> None:
        """One gutter block per rendered row, listing each member trace
        (colour swatch + label + value at the cursor) — so overlaid traces are
        all labelled. Analog panes also show a +/- amplitude scale (min/mid/max)
        at the right edge of the gutter so the vertical axis is readable."""
        if self._group_is_analog(grp):
            self._paint_analog_scale(p, grp, top, h, fm)
        line = top + 2
        for i in grp:
            s = self._streams[i]
            if i == self._selected:
                # Highlight the selected trace's gutter band (drag subject, #159).
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(70, 90, 120))
                p.drawRect(QRectF(2, line, _GUTTER_W - 6, _GUTTER_LINE_H - 2))
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(s.get("color", _TRACE)))
            p.drawRect(QRectF(6, line + 3, 8, 9))
            p.setPen(QPen(_LABEL))
            p.drawText(QRectF(18, line, _GUTTER_W - 24, 15),
                       Qt.AlignLeft | Qt.AlignVCenter, s["label"])
            cur_v = self._gutter_value(s)
            sub = (_fmt_value(cur_v, s["radix"]) if cur_v is not None else "—")
            p.setPen(QPen(_SUBLABEL))
            p.drawText(QRectF(18, line + 14, _GUTTER_W - 24, 14),
                       Qt.AlignLeft | Qt.AlignVCenter, f"{s['radix']}: {sub}")
            line += 30
            if line > top + h - 4:
                break  # ran out of room in this row

    def _analog_value_ticks(self, grp, top, h):
        """Yield ``(value, y)`` for each 'nice' amplitude tick of an analog
        pane, using the SAME vertical mapping as ``_paint_analog`` (pane range,
        amp_scale, full-height). Denser as the row grows / you zoom amplitude —
        the value-axis counterpart of the time ruler."""
        vmin, vmax = self._pane_value_range(grp)
        vmid = 0.5 * (vmin + vmax)
        amp_scale = self._streams[grp[0]].get("amp_scale", 1.0)
        ytop, ybot = top + 1, top + h - 1
        ymid = 0.5 * (ytop + ybot)
        usable = (ybot - ytop)
        span = (vmax - vmin) or 1.0
        ppu = (usable / span) * amp_scale
        # Visible value range at this row height/amp; ticks chosen over it.
        half = (usable * 0.5) / (ppu or 1.0)
        lo, hi = vmid - half, vmid + half
        for val in _nice_ticks(lo, hi, usable, min_px_per_tick=22.0):
            y = ymid - (val - vmid) * ppu
            if ytop - 0.5 <= y <= ybot + 0.5:
                yield val, max(ytop, min(ybot, y))

    def _time_major_ticks(self):
        """The major (numbered) time-tick values currently shown by the header
        ruler — so the analog grid's vertical lines land exactly under them."""
        r = self._plot_rect()
        return _nice_ticks(self._t0, self._t1, r.width(), min_px_per_tick=90.0)

    def _paint_analog_grid(self, p, grp, row: QRectF) -> None:
        """Subtle dotted grid lines for an analog pane: HORIZONTAL at the pane's
        amplitude ticks and VERTICAL under the header ruler's numbered times, so
        it's easy to read how close a trace sits to each value/time (item 6)."""
        pen = QPen(QColor(80, 84, 92))
        pen.setStyle(Qt.DotLine)
        pen.setWidth(0)
        p.setPen(pen)
        for _val, y in self._analog_value_ticks(grp, row.top(), row.height()):
            p.drawLine(int(row.left()), int(y), int(row.right()), int(y))
        for t in self._time_major_ticks():
            x = self._t_to_x(t)
            if row.left() <= x <= row.right():
                p.drawLine(int(x), int(row.top()), int(x), int(row.bottom()))

    def _paint_analog_scale(self, p, grp, top, h, fm) -> None:
        """Draw the pane's amplitude scale as a true ruler at the right edge of
        the gutter: a tick + value label at each 'nice' amplitude step (more
        labels fill in as the row grows / amplitude zooms), aligned to the grid
        lines in the plot."""
        p.setPen(QPen(QColor(120, 124, 132)))
        tick_x = _GUTTER_W - 6
        for val, y in self._analog_value_ticks(grp, top, h):
            p.drawLine(int(tick_x), int(y), int(_GUTTER_W - 1), int(y))
            p.drawText(QRectF(_GUTTER_W - 52, y - 7, 44, 14),
                       Qt.AlignRight | Qt.AlignVCenter, f"{val:+.3g}")

    def _paint_cursor(self, p: QPainter, ns, color) -> None:
        if ns is None:
            return
        x = self._t_to_x(ns)
        r = self._plot_rect()
        if x < r.left() - 1 or x > r.right() + 1:
            return
        p.setPen(QPen(color, 1.5))
        p.drawLine(int(x), 0, int(x), int(r.bottom()))

    # -- interaction ----------------------------------------------------------

    def mousePressEvent(self, ev) -> None:  # noqa: N802
        # Right-click is handled by contextMenuEvent (the trace menu).
        if ev.button() == Qt.RightButton:
            return
        x, y = ev.position().x(), ev.position().y()
        if ev.button() == Qt.LeftButton:
            # A left-press on a row's bottom edge starts a HEIGHT resize drag.
            grip = self._resize_grip_at_y(y)
            if grip >= 0:
                self._resize_row = grip
                self._resize_y0 = y
                self._resize_h0 = self._streams[grip].get("height", _ROW_H)
                return
            # A left-press on the GUTTER selects that trace and ARMS a drag —
            # it only becomes a real drag once the mouse moves (a plain click
            # just selects, it does not move/rotate signals, see #157/#159).
            if x < _GUTTER_W:
                member = self._member_at_y(y)
                if member >= 0:
                    self._selected = member
                    self._drag_stream = member
                    self._press_y = y
                    self._dragging = False
                    self.update()
                return
        if x < _GUTTER_W:
            return
        t = self._x_to_t(x)
        if ev.button() == Qt.LeftButton:
            self._main_ns = t
            self.update()
            self.cursor_requested.emit(t)
        elif ev.button() == Qt.MiddleButton:
            self._meas_ns = t
            self.update()
            self.measurement_changed.emit()

    def mouseMoveEvent(self, ev) -> None:  # noqa: N802
        y = ev.position().y()
        if self._resize_row >= 0:
            new_h = max(_MIN_ROW_H, self._resize_h0 + (y - self._resize_y0))
            self._streams[self._resize_row]["height"] = new_h
            self._update_content_height()
            self.updateGeometry()
            self.update()
            return
        from PySide6.QtGui import QCursor
        if self._drag_stream >= 0:
            if not self._dragging and abs(y - self._press_y) > _DRAG_THRESHOLD:
                self._dragging = True   # crossed the threshold → a real drag
            if self._dragging:
                self.setCursor(QCursor(Qt.DragMoveCursor))
            return
        # Resize cursor hint when hovering a row's bottom edge.
        if self._resize_grip_at_y(y) >= 0:
            self.setCursor(QCursor(Qt.SizeVerCursor))
        elif ev.position().x() < _GUTTER_W and self._stream_at_y(y) >= 0:
            self.setCursor(QCursor(Qt.OpenHandCursor))  # gutter = draggable
        else:
            self.unsetCursor()

    def mouseReleaseEvent(self, ev) -> None:  # noqa: N802
        if self._resize_row >= 0:
            self._resize_row = -1
            self.streams_changed.emit()
            return
        if self._drag_stream >= 0:
            src = self._drag_stream
            was_drag = self._dragging
            self._drag_stream = -1
            self._dragging = False
            self.unsetCursor()
            if was_drag:                       # a plain click only selects (#157)
                self._drop_trace(src, ev.position().y())

    def set_channel_provider(self, provider) -> None:
        """Set ``provider() -> [(label, on_add_callable)]`` listing the channels
        the right-click 'Add channel' submenu offers (the panel supplies it from
        the trace model). ``None`` hides the submenu."""
        self._channel_provider = provider

    def _populate_add_channel_menu(self, menu) -> None:
        if self._channel_provider is None:
            return
        try:
            items = self._channel_provider() or []
        except Exception:  # noqa: BLE001
            items = []
        sub = menu.addMenu("Add channel")
        if not items:
            empty = sub.addAction("(no captured channels — run the sim)")
            empty.setEnabled(False)
            return
        for label, on_add in items:
            sub.addAction(label, on_add)

    def contextMenuEvent(self, ev) -> None:  # noqa: N802
        """Right-click → add a channel (always), and on a trace also radix /
        colour / delete. Works on empty area so a channel can be added without
        dragging a port in."""
        from PySide6.QtWidgets import QMenu

        menu = QMenu(self)
        row = self._member_at_y(ev.pos().y())
        if row >= 0:
            menu.addAction(self._streams[row]["label"]).setEnabled(False)
            menu.addSeparator()
            radix_menu = menu.addMenu("Radix")
            cur = self._streams[row]["radix"]
            for radix in RADICES:
                act = radix_menu.addAction(radix)
                act.setCheckable(True)
                act.setChecked(radix == cur)
                act.triggered.connect(
                    lambda _c=False, rw=row, rx=radix: self.set_radix(rw, rx))
            menu.addAction("Change Colour…", lambda: self._pick_color(row))
            menu.addSeparator()
            menu.addAction("Delete Trace", lambda: self.remove_stream(row))
            menu.addSeparator()
        self._populate_add_channel_menu(menu)
        menu.exec(ev.globalPos())

    def _pick_color(self, row: int) -> None:
        from PySide6.QtWidgets import QColorDialog
        if not (0 <= row < len(self._streams)):
            return
        cur = QColor(self._streams[row].get("color", _TRACE))
        chosen = QColorDialog.getColor(cur, self, "Trace colour")
        if chosen.isValid():
            self._streams[row]["color"] = chosen
            self.update()
            self.streams_changed.emit()

    def _is_analog(self, idx: int) -> bool:
        return self._streams[idx]["radix"] == RADIX_ANALOG

    def _group_is_analog(self, grp) -> bool:
        return all(self._is_analog(i) for i in grp)

    def _drop_trace(self, src: int, y: float) -> None:
        """Drop the dragged trace ``src`` at pixel ``y`` (DROP SEMANTICS, #160):

          * Drop in the **middle 10–90%** of a target row → **STACK** (overlay)
            — but ONLY when both the dragged trace and the target row are analog.
            Stacked analog traces share ONE amplitude scale so they're directly
            comparable (#158); the moved trace inherits the row's ``amp_scale``.
          * Drop in the **fringe top/bottom 10%** of a row, or onto a digital
            row, or while dragging a digital trace → **REORDER** into its own
            pane (digital traces NEVER stack — they only reorder).

        ``_streams`` is reordered so each group stays contiguous."""
        if not (0 <= src < len(self._streams)):
            return
        target = self._row_at_y(y)
        s = self._streams[src]
        stack = False
        insert_after = True
        if target is not None and src not in target[0]:
            grp, top, h = target
            frac = (y - top) / max(1.0, h)
            in_middle = _STACK_FRINGE < frac < (1.0 - _STACK_FRINGE)
            # Stack only when middle-drop AND both sides are analog.
            stack = (in_middle and self._is_analog(src)
                     and self._group_is_analog(grp))
            insert_after = frac >= 0.5
        # Now remove src and recompute target indices (they shift after the pop).
        self._streams.pop(src)
        if target is not None and src not in target[0]:
            grp = [g - 1 if g > src else g for g in target[0]]
            if stack:
                # Share the group id + the pane's single amplitude scale (#158).
                s["group"] = self._streams[grp[0]]["group"]
                s["amp_scale"] = self._streams[grp[0]].get("amp_scale", 1.0)
                self._streams.insert(grp[-1] + 1, s)      # contiguous in group
            else:
                # Reorder: drop as its own pane just before/after the target row.
                s["group"] = self._fresh_group_id()
                pos = (grp[-1] + 1) if insert_after else grp[0]
                self._streams.insert(pos, s)
        else:
            # Dropped off any row → own pane. Above the first row (y in the top
            # pad) → insert at the TOP so a lower wave can be dragged above the
            # current top wave; otherwise (gap below the last row) → append.
            s["group"] = self._fresh_group_id()
            if y < _TOP_PAD:
                self._streams.insert(0, s)
            else:
                self._streams.append(s)
        self._selected = self._streams.index(s)
        self._update_content_height()
        self.updateGeometry()
        self.update()
        self.streams_changed.emit()

    def _fresh_group_id(self) -> int:
        used = {s.get("group", i) for i, s in enumerate(self._streams)}
        n = 0
        while n in used:
            n += 1
        return n

    def remove_stream(self, row: int) -> None:
        """Delete a trace from the viewer."""
        if 0 <= row < len(self._streams):
            del self._streams[row]
            if self._selected == row:
                self._selected = -1
            elif self._selected > row:
                self._selected -= 1
            self._update_content_height()
            self.updateGeometry()
            self.update()
            self.streams_changed.emit()

    # -- register drag-drop (from the Program pane) ---------------------------

    _REG_MIME = "application/x-placekyt-register"
    _PORT_MIME = "application/x-placekyt-port"
    _ROUTE_MIME = "application/x-placekyt-route"

    def dragEnterEvent(self, ev) -> None:  # noqa: N802
        md = ev.mimeData()
        if (md.hasFormat(self._REG_MIME) or md.hasFormat(self._PORT_MIME)
                or md.hasFormat(self._ROUTE_MIME)):
            ev.acceptProposedAction()

    def dragMoveEvent(self, ev) -> None:  # noqa: N802
        md = ev.mimeData()
        if (md.hasFormat(self._REG_MIME) or md.hasFormat(self._PORT_MIME)
                or md.hasFormat(self._ROUTE_MIME)):
            ev.acceptProposedAction()

    def dropEvent(self, ev) -> None:  # noqa: N802
        md = ev.mimeData()
        if md.hasFormat(self._ROUTE_MIME):
            try:
                name = bytes(md.data(self._ROUTE_MIME)).decode()
            except UnicodeDecodeError:
                return
            ev.acceptProposedAction()
            self.route_dropped.emit(name)
            return
        if md.hasFormat(self._PORT_MIME):
            # "<chip>,<port_name>" — the panel pops a channel/tag picker.
            try:
                payload = bytes(md.data(self._PORT_MIME)).decode()
                chip_s, port = payload.split(",", 1)
                chip = int(chip_s)
            except (ValueError, UnicodeDecodeError):
                return
            ev.acceptProposedAction()
            self.port_dropped.emit(chip, port)
            return
        if not md.hasFormat(self._REG_MIME):
            return
        try:
            chip, x, y, addr = (int(v) for v in
                                bytes(md.data(self._REG_MIME)).decode().split(","))
        except (ValueError, UnicodeDecodeError):
            return
        ev.acceptProposedAction()
        self.register_dropped.emit(chip, x, y, addr)

    def keyPressEvent(self, ev) -> None:  # noqa: N802
        # 'z' zooms to the region between the two cursors; 'f' fits all;
        # Delete/Backspace removes the selected trace.
        if ev.key() in (Qt.Key_Delete, Qt.Key_Backspace) and self._selected >= 0:
            self.remove_stream(self._selected)
        elif ev.key() == Qt.Key_Z and self._main_ns is not None \
                and self._meas_ns is not None:
            lo, hi = sorted((self._main_ns, self._meas_ns))
            if hi > lo:
                pad = (hi - lo) * 0.05
                self._t0, self._t1 = lo - pad, hi + pad
                self._clamp_window()
                self.update()
        elif ev.key() == Qt.Key_F:
            self._fit_time_window()
            self.update()
        else:
            super().keyPressEvent(ev)

    def wheelEvent(self, ev) -> None:  # noqa: N802
        """Wheel interactions:
          * plain wheel  → ZOOM in/out time (about the cursor x)
          * Shift+wheel  → PAN in time
          * Ctrl+wheel   → adjust the AMPLITUDE scale of the trace under the
            cursor (focus on small-amplitude sections amid large transients)
        """
        mods = ev.modifiers()
        up = ev.angleDelta().y() > 0
        if mods & Qt.ControlModifier:
            row = self._row_at_y(ev.position().y())
            if row is not None:
                grp, _top, _h = row
                factor = 1.25 if up else 0.8
                for i in grp:  # scale all overlaid traces in the row together
                    s = self._streams[i]
                    s["amp_scale"] = max(0.1, min(
                        50.0, s.get("amp_scale", 1.0) * factor))
                self.update()
            return
        if mods & Qt.ShiftModifier:
            span = self._t1 - self._t0
            shift = span * 0.12 * (-1 if up else 1)
            self._t0 += shift
            self._t1 += shift
            self._clamp_window()   # don't pan past the data edges
            self.update()
            return
        # Plain wheel → zoom about the cursor.
        anchor = self._x_to_t(ev.position().x())
        factor = 0.8 if up else 1.25
        self._t0 = anchor - (anchor - self._t0) * factor
        self._t1 = anchor + (self._t1 - anchor) * factor
        self._clamp_window()       # don't zoom out past the data edges
        self.update()


class WaveformRuler(QWidget):
    """A thin, fixed time-axis header for the waveform panel. Lives ABOVE the
    scroll area (so it's always visible while the traces scroll vertically) and
    mirrors a :class:`WaveformView`'s time window. It paints tick labels across
    the plot region, aligned to the view's gutter so ticks sit over the traces.
    """

    def __init__(self, view: "WaveformView", parent=None):
        super().__init__(parent)
        self._view = view
        self.setFixedHeight(_RULER_H + 6)
        view.window_changed.connect(self.update)

    def paintEvent(self, _ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), _GUTTER_BG)
        left = self._view.plot_left()
        t0, t1 = self._view.time_window()
        w = max(1, self.width() - left)
        bottom = self.height() - 1
        p.setPen(QPen(_GRID, 0))
        p.drawLine(int(left), bottom, int(self.width()), bottom)
        p.drawLine(int(left), 0, int(left), bottom)   # gutter divider

        def t_to_x(t):
            return left + (t - t0) / (t1 - t0 or 1.0) * w

        # Minor ticks (dense, short, unlabelled) + major ticks (labelled). As
        # you zoom in, the 'nice' step shrinks → more majors get labels.
        minors = _nice_ticks(t0, t1, w, min_px_per_tick=18.0)
        majors = _nice_ticks(t0, t1, w, min_px_per_tick=90.0)
        major_set = set(majors)
        p.setPen(QPen(_GRID, 0))
        for t in minors:
            x = t_to_x(t)
            h = 9 if t in major_set else 5
            p.drawLine(int(x), bottom - h, int(x), bottom)
        index_mode = self._view.x_axis_mode() == "index"
        unit = "" if index_mode else " ns"
        p.setPen(QPen(_SUBLABEL))
        for t in majors:
            x = t_to_x(t)
            label = f"{t:.0f}" if (index_mode or abs(t) >= 100) else f"{t:.3g}"
            p.drawText(QRectF(x - 45, 1, 90, self.height() - 8),
                       Qt.AlignCenter, f"{label}{unit}")
        # Label the gutter side too.
        p.setPen(QPen(_SUBLABEL))
        p.drawText(QRectF(6, 1, left - 10, self.height() - 4),
                   Qt.AlignLeft | Qt.AlignVCenter,
                   "sample →" if index_mode else "time →")
        p.end()
