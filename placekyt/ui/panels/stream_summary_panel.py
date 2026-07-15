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

Reads a :class:`engine.trace_model.TraceModel` (set on ``trace_updated``) and
pulls the power report on demand via an injected provider.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt
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

    # -- wiring ---------------------------------------------------------------

    def set_perf_report_provider(self, provider) -> None:
        """Inject a ``() -> dict | None`` that returns the chip's
        ``performance_report()`` for the latest run (aggregate power/latency)."""
        self._perf_provider = provider

    def set_stream_namer(self, namer) -> None:
        """Inject a ``(chip, port, tag) -> str | None`` that gives a stream a
        friendly logical name (e.g. 'rrc.xi'), for the Stream column."""
        self._namer = namer

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
