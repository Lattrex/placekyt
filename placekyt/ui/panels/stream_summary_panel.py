"""StreamSummaryPanel — per-stream throughput / latency / power summary.

A run-summary view the user can read WITHOUT hand-digging per design: one row
per logical DATA stream (each demultiplexed input operand and each output net),
showing the SETTLED sample rate that stream sustains through the chip, plus an
aggregate power/energy/latency block underneath.

All numbers are honest CHIP-TIME figures from simKYT's cycle-accurate GLS timing
(NOT host wall-clock):

* **Per-stream rate** — from ``TraceModel.stream_summary()``: the steady-state
  rate at which real DATA samples cross that port, computed from the median
  inter-sample gap (dropping the pipeline-fill transient).
* **Latency** — ``TraceModel.io_latency_ns()``: first input sample in → first
  output sample out (the pipeline depth in ns). This is the AGGREGATE end-to-end
  latency; per-stream input↔output association would need the dataflow graph, so
  we surface the one honest chip-level number in its own row.
* **Power / energy** — from the chip's ``performance_report()`` (the populated
  char_tt per-instruction energy table): total energy, average/idle/total power,
  and energy per output sample.

* **Bottleneck (serial barrier)** — from ``TraceModel.block_bottleneck()``: each
  block's INPUT/OUTPUT stall DIFFERENTIAL = the backpressure wait at its input
  landing cell minus the wait at its freest-draining cell. The #1 block is where
  input samples pile up but the output drains freely (downstream keeps up) — it
  MANUFACTURES the backpressure and sets the pace. A block that merely RELAYS
  upstream backpressure stalls on both sides equally → differential ~0, not the
  culprit. The table shows Serial barrier + its In-stall / Out-stall components.
  Needs the simKYT ``stall`` trace + a saturated run; falls back to the busy-dwell
  headline (from ``block_utilization``) when nothing parks.

Reads a :class:`engine.trace_model.TraceModel` (set on ``trace_updated``) and
pulls the power report + block-placement map on demand via injected providers.
The same block-utilization data drives the canvas's toggleable bottleneck
heatmap (View → Show Bottleneck Heatmap).
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

# Per-stream table columns.
_COLS = ["Direction", "Chip", "Port", "Stream", "Samples",
         "Settled rate", "Mean rate", "Sample gap"]

# Per-block utilization table columns. "Dwell" = critical-cell busy relative to
# the busiest block (bottleneck=100%; size-independent, NOT summed over cells).
# "Cell duty" = average per-cell fraction of the run (≈100% everywhere ⇒ the array
# is saturated). "Instr/cell" = mean instructions per cell (work-per-sample; a
# feedback loop runs many, a flat filter few).
# Bottleneck table: the serial-barrier DIFFERENTIAL and the two sides that make it.
#   Serial barrier = In-stall − Out-stall — the block's per-sample throttle.
#   In-stall  = median backpressure wait at its INPUT landing cell.
#   Out-stall = stall at its freest-draining cell (the output side when it drains).
# A block ranks high only when input piles up AND output drains (In ≫ Out).
_UTIL_COLS = ["Rank", "Block", "Type", "Serial barrier", "In-stall", "Out-stall",
              "Cells"]

# Row tint for the #1 (bottleneck) block — a translucent hot red.
_HOT_ROW = QColor(180, 60, 60, 90)


def _fmt_rate(sps: float | None) -> str:
    """Samples/second as MSa/s or kSa/s (whichever reads cleaner), or '—'."""
    if not sps:
        return "—"
    if sps >= 1e6:
        return f"{sps / 1e6:.3f} MSa/s"
    if sps >= 1e3:
        return f"{sps / 1e3:.2f} kSa/s"
    return f"{sps:.1f} Sa/s"


def _fmt_ns(ns: float | None) -> str:
    if ns is None:
        return "—"
    if ns >= 1000.0:
        return f"{ns / 1000.0:.2f} µs"
    return f"{ns:.0f} ns"


class StreamSummaryPanel(QWidget):
    """Per-stream throughput + aggregate power/latency, over a TraceModel."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._model = None                       # engine.trace_model.TraceModel
        self._perf_provider: Callable[[], dict | None] | None = None
        self._namer: Callable[[int, str, object], str | None] | None = None
        # () -> (block_lookup, block_types): the placement→block map for utilization.
        self._block_provider: Callable[[], tuple[dict, dict] | None] | None = None
        # () -> {"busy": {(chip,x,y): ns}, "span_ns": float} | None: per-cell busy
        # time from the hosted chip's trace (exec_ticks are dropped from the
        # retained TraceModel, so utilization can't come from ``self._model``).
        self._busy_provider: Callable[[], dict | None] | None = None
        # () -> {(chip,x,y): [waited_ns, …]} | None: per-cell STALL (backpressure)
        # waits from the hosted chip. The honest serial-barrier / bottleneck signal.
        self._stall_provider: Callable[[], dict | None] | None = None
        # () -> {block name: [(chip,x,y), …]} | None: each block's cells, [0]=landing.
        self._block_cells_provider: Callable[[], dict | None] | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(6)

        self._title = QLabel("Stream Summary — run a simulation to populate.")
        self._title.setStyleSheet("font-weight: bold;")
        outer.addWidget(self._title)

        # Per-stream throughput table.
        self._table = QTableWidget(0, len(_COLS))
        self._table.setHorizontalHeaderLabels(_COLS)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.verticalHeader().setVisible(False)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setStretchLastSection(True)
        outer.addWidget(self._table, 1)

        # Aggregate latency + power/energy block.
        self._latency = QLabel("Latency (in→out): —")
        outer.addWidget(self._latency)

        pwr = QHBoxLayout()
        self._power = QLabel("Power: —")
        self._energy = QLabel("Energy: —")
        pwr.addWidget(self._power)
        pwr.addStretch()
        pwr.addWidget(self._energy)
        outer.addLayout(pwr)

        # -- Block utilization (the throughput-bottleneck view) ------------------
        self._bottleneck = QLabel("Bottleneck: —")
        self._bottleneck.setStyleSheet("font-weight: bold;")
        outer.addWidget(self._bottleneck)

        self._util = QTableWidget(0, len(_UTIL_COLS))
        self._util.setHorizontalHeaderLabels(_UTIL_COLS)
        self._util.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._util.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._util.verticalHeader().setVisible(False)
        uh = self._util.horizontalHeader()
        uh.setSectionResizeMode(QHeaderView.ResizeToContents)
        uh.setStretchLastSection(True)
        outer.addWidget(self._util, 1)

    # -- wiring ---------------------------------------------------------------

    def set_perf_report_provider(self, provider) -> None:
        """Inject a ``() -> dict | None`` that returns the chip's
        ``performance_report()`` for the latest run (aggregate power/latency)."""
        self._perf_provider = provider

    def set_stream_namer(self, namer) -> None:
        """Inject a ``(chip, port, tag) -> str | None`` that gives a stream a
        friendly logical name (e.g. 'rrc.xi'), for the Stream column."""
        self._namer = namer

    def set_block_provider(self, provider) -> None:
        """Inject a ``() -> (block_lookup, block_types) | None`` where
        ``block_lookup`` maps ``(chip, x, y) -> block name`` and ``block_types``
        maps name -> block type, both from the current placement. Powers the
        per-block utilization / bottleneck table."""
        self._block_provider = provider

    def set_busy_provider(self, provider) -> None:
        """Inject a ``() -> {"busy": {(chip,x,y): ns}, "span_ns": float} | None``
        giving per-cell executing chip-time for the last run. Needed because the
        retained TraceModel drops exec_ticks — this pulls busy-time from the hosted
        chip's own trace instead (see SimController.cell_busy_report)."""
        self._busy_provider = provider

    def set_stall_provider(self, provider) -> None:
        """Inject a ``() -> {(chip,x,y): [waited_ns, …]} | None`` giving per-cell
        STALL (parked-REQ backpressure) waits for the last run (see
        SimController.cell_stall_report). The block-BOTTLENECK / serial-barrier
        ranking uses the median wait at each block's landing cell."""
        self._stall_provider = provider

    def set_block_cells_provider(self, provider) -> None:
        """Inject a ``() -> {block name: [(chip,x,y), …]} | None`` giving each block's
        cells with ``[0]`` the input LANDING cell, for the serial-barrier (input/
        output stall differential) view."""
        self._block_cells_provider = provider

    def set_trace_model(self, model) -> None:
        """Bind (or rebind) the TraceModel and refresh — the ``trace_updated``
        slot, mirroring the other debug panels."""
        self._model = model
        self.refresh()

    # -- rendering ------------------------------------------------------------

    def _stream_label(self, chip: int, port: str, tag) -> str:
        """Human label for a stream: the friendly net name if the namer resolves
        one, else the raw tag ((hop,dest) for an input operand, an int for an
        output net, or 'single' when untagged)."""
        name = None
        if self._namer is not None:
            try:
                name = self._namer(chip, port, tag)
            except Exception:  # noqa: BLE001
                name = None
        if tag is None:
            raw = "single"
        elif isinstance(tag, tuple):
            raw = f"hop{tag[0]}/a{tag[1]}"
        else:
            raw = f"tag {tag}"
        return f"{name} ({raw})" if name else raw

    def refresh(self) -> None:
        m = self._model
        if m is None or not getattr(m, "transactions", None):
            self._title.setText("Stream Summary — run a simulation to populate.")
            self._table.setRowCount(0)
            self._latency.setText("Latency (in→out): —")
            self._power.setText("Power: —")
            self._energy.setText("Energy: —")
            self._bottleneck.setText("Bottleneck: —")
            self._util.setRowCount(0)
            return

        rows = m.stream_summary()
        n_in = sum(1 for r in rows if r["direction"] == "in")
        n_out = len(rows) - n_in
        self._title.setText(
            f"Stream Summary — {n_in} input stream(s), {n_out} output stream(s)")

        self._table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            gap = r["median_gap_ns"] or r["mean_gap_ns"]
            cells = [
                "IN" if r["direction"] == "in" else "OUT",
                str(r["chip"]),
                r["port"],
                self._stream_label(r["chip"], r["port"], r["tag"]),
                str(r["samples"]),
                _fmt_rate(r["settled_sps"]),
                _fmt_rate(r["mean_sps"]),
                _fmt_ns(gap),
            ]
            for j, txt in enumerate(cells):
                item = QTableWidgetItem(txt)
                if j >= 4:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self._table.setItem(i, j, item)

        # Aggregate latency — prefer the trace's own in→out fill latency.
        lat = None
        try:
            lat = m.io_latency_ns()
        except Exception:  # noqa: BLE001
            lat = None
        self._latency.setText(f"Latency (first in → first out): {_fmt_ns(lat)}")

        self._render_power()
        self._render_utilization()

    def _render_power(self) -> None:
        rep = None
        if self._perf_provider is not None:
            try:
                rep = self._perf_provider()
            except Exception:  # noqa: BLE001
                rep = None
        if not rep:
            self._power.setText("Power: (unavailable)")
            self._energy.setText("Energy: —")
            return
        if not rep.get("power_data_available", False):
            self._power.setText("Power: (no energy table for this chip corner)")
            self._energy.setText("Energy: —")
            return

        avg = rep.get("average_power_mw")
        idle = rep.get("idle_power_mw")
        total = rep.get("total_power_mw")
        e_tot = rep.get("total_energy_pj")
        e_out = rep.get("energy_per_output_pj")

        def _mw(x):
            return f"{x:.2f} mW" if isinstance(x, (int, float)) else "—"

        self._power.setText(
            f"Power: active {_mw(avg)}  +  idle {_mw(idle)}  =  total {_mw(total)}")

        def _energy(pj):
            if not isinstance(pj, (int, float)):
                return "—"
            if pj >= 1e6:
                return f"{pj / 1e6:.2f} µJ"
            if pj >= 1e3:
                return f"{pj / 1e3:.1f} nJ"
            return f"{pj:.0f} pJ"

        parts = [f"total {_energy(e_tot)}"]
        if e_out:
            parts.append(f"{_energy(e_out)}/output")
        self._energy.setText("Energy: " + "  ·  ".join(parts))

    def _render_utilization(self) -> None:
        """Per-block busy-time table + the bottleneck callout. The #1 block (by
        chip-time spent executing) is the worst-case serial path — where samples
        get stuck, and the place to optimize for throughput."""
        prov = self._block_provider
        pair = None
        if prov is not None:
            try:
                pair = prov()
            except Exception:  # noqa: BLE001
                pair = None
        if not pair:
            self._bottleneck.setText(
                "Bottleneck: (place & build a design to see per-block utilization)")
            self._util.setRowCount(0)
            return
        block_lookup, block_types = pair
        # Busy-time comes from the hosted chip's trace (exec_ticks are dropped from
        # the retained TraceModel), via the busy provider. Fall back to the model
        # itself (headless/test path where the model DOES hold exec_ticks).
        busy = span = None
        if self._busy_provider is not None:
            try:
                rep = self._busy_provider()
            except Exception:  # noqa: BLE001
                rep = None
            if rep:
                busy = rep.get("busy")
                span = rep.get("span_ns")
        rows = self._model.block_utilization(
            block_lookup, block_types, busy=busy, span_ns=span)

        # SERIAL BARRIER (the honest bottleneck): median STALL at each block's INPUT
        # LANDING cell — how much serial execution the block must finish before it
        # can accept the NEXT input sample. A feedback loop (Costas/Gardner) that
        # LOCKs while it iterates holds off its input → large barrier; a feed-forward
        # block accepts back-to-back → ~zero. Decimation- and saturation-immune (it
        # only counts a stall when a sample actually arrives at THIS block's door,
        # and it's a wait, not a duty). Requires the stall trace + a saturated run;
        # falls back to the busy "dwell" ranking when there are no stalls.
        barrier = self._block_bottleneck(block_lookup, block_types)
        by_block = {b["block"]: b for b in barrier}
        have_barrier = any(b["stall_ns"] > 0 for b in barrier)
        for r in rows:
            b = by_block.get(r["block"])
            r["barrier_ns"] = b["stall_ns"] if b else 0.0
            r["in_stall_ns"] = b["in_stall_ns"] if b else 0.0
            r["out_stall_ns"] = b["out_stall_ns"] if b else 0.0
        if have_barrier:
            # Rank by the true serial barrier; routing/transit (no landing) sink low.
            rows.sort(key=lambda r: (r.get("barrier_ns", 0.0), r["crit_ns"]),
                      reverse=True)
            for i, r in enumerate(rows):
                r["rank"] = i + 1

        dsp = [r for r in rows if r["block"] != "(routing)"]
        if have_barrier and dsp:
            top = dsp[0]
            self._bottleneck.setText(
                f"Bottleneck: {top['block']} "
                f"({_fmt_ns(top['barrier_ns'])}/sample) — input backpressure piles up "
                "here but its output drains freely (downstream keeps up), so THIS "
                "block sets the pace. Optimize here for throughput.")
        elif dsp:
            # No stalls captured — either a feed-forward-only design, or the run
            # wasn't saturated (nothing parks). Fall back to the busy dwell view +
            # the saturation callout, and say the barrier needs a saturated run.
            top = dsp[0]
            pct = top["util_pct"]
            pct_txt = f"{pct:.0f}% busy" if isinstance(pct, (int, float)) else ""
            duties = [r.get("occupancy_pct") for r in dsp
                      if isinstance(r.get("occupancy_pct"), (int, float))]
            saturated = bool(duties) and min(duties) >= 90.0
            hint = " (run at saturation to measure per-block serial barriers)"
            if saturated:
                self._bottleneck.setText(
                    f"Array saturated (~{min(duties):.0f}% cell duty) — throughput-"
                    f"bound. Slowest per sample: {top['block']}"
                    + (f" ({pct_txt})" if pct_txt else "") + "." + hint)
            else:
                self._bottleneck.setText(
                    f"Busiest block: {top['block']}"
                    + (f" ({pct_txt})" if pct_txt else "") + "." + hint)
        else:
            self._bottleneck.setText("Bottleneck: —")

        self._util.setRowCount(len(rows))
        top_block = dsp[0]["block"] if dsp else None
        for i, r in enumerate(rows):
            bns = r.get("barrier_ns", 0.0)
            barrier_txt = _fmt_ns(bns) if bns else "—"
            ins = r.get("in_stall_ns", 0.0)
            outs = r.get("out_stall_ns", 0.0)
            in_txt = _fmt_ns(ins) if ins else "—"
            out_txt = _fmt_ns(outs) if outs else "—"
            cells = [
                str(r["rank"]),
                r["block"],
                r["type"] or "",
                barrier_txt,
                in_txt,
                out_txt,
                str(r["cells"]),
            ]
            is_top = (r["block"] != "(routing)" and r["block"] == top_block)
            for j, txt in enumerate(cells):
                item = QTableWidgetItem(txt)
                if j in (0, 3, 4, 5, 6):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if is_top:
                    item.setBackground(_HOT_ROW)
                self._util.setItem(i, j, item)

    def _block_bottleneck(self, block_lookup, block_types):
        """Compute the per-block serial-barrier (input/output stall DIFFERENTIAL)
        rows from the stall provider + block-cells provider. Returns [] if either is
        missing. The provider gives every block's cells ([0]=landing); the stall
        provider gives per-cell parked-REQ waits; TraceModel.block_bottleneck ranks
        by (landing stall − freest-cell stall) so a block that merely relays upstream
        backpressure (input≈output stall) is not mistaken for the true throttle."""
        if self._stall_provider is None or self._block_cells_provider is None:
            return []
        try:
            stall = self._stall_provider()
            cells_by_block = self._block_cells_provider()
        except Exception:  # noqa: BLE001
            return []
        if not cells_by_block:
            return []
        # Load the stall waits into a throwaway model to reuse block_bottleneck's
        # median/differential/rank logic. Feed cell_id already mapped: encode with a
        # synthetic width and normalise back with the SAME width.
        try:
            from engine.trace_model import TraceModel
        except Exception:  # noqa: BLE001
            return []
        tm = TraceModel()
        W = 4096
        raw = []
        if stall:
            for (chip, x, y), waits in stall.items():
                for w in waits:
                    raw.append({"time_ns": 0.0, "cell_id": y * W + x,
                                "kind": "stall", "waited_ns": float(w)})
        tm.ingest(0, raw, W)
        block_cells = {name: [(0, c[1], c[2]) for c in cells]
                       for name, cells in cells_by_block.items()}
        return tm.block_bottleneck(block_cells, block_types)
