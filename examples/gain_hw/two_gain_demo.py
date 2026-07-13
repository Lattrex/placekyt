#!/usr/bin/env python3
"""Two multiplexed gain cells on ONE chip, sharing one input + one output port, each
with a LIVE gain slider. Proves the shared-port multiplex model + live coefficient
writes on the real Kyttar-emulating FPGA (the two-cell fake_kyttar_gain2 gateway).

Run in the placekyt venv, board flashed with the two-cell gateware:
    .venv/bin/python examples/gain_hw/two_gain_demo.py

Two sine sources (different frequencies) → two streams multiplexed onto x16_in →
two gain cells (A tag0, B tag1) → demuxed off x16_out → two live plots. Two sliders
set each cell's gain live (a coefficient WRITE to that cell). Standalone (no GRC) so it
runs today while the auto-P&R router work (distinct per-block input tags) is separate.

Tagging (matches fake_kyttar_gain2 + the sim stream_targets spec):
    cell A: sample dest=1, jump entry=1, coeff dest=28, out_tag=0
    cell B: sample dest=2, jump entry=2, coeff dest=29, out_tag=1
    chip-group tag = 0 (single logical chip on this demo board)
"""
import sys
import numpy as np

from PySide6 import QtWidgets, QtCore, QtGui
from PySide6.QtCharts import QChart, QChartView, QLineSeries, QValueAxis

sys.path.insert(0, "/home/system/placekyt/placekyt")
from engine.hw_chip import HwChip, _encode_write, _as_i16  # noqa: E402

CELLS = {
    "A": {"samp_dest": 1, "entry": 1, "coeff_dest": 28, "out_tag": 0, "freq": 3},
    "B": {"samp_dest": 2, "entry": 2, "coeff_dest": 29, "out_tag": 1, "freq": 5},
}
N = 256


def q15(f):
    return int(round(max(-0.999969, min(0.999969, f)) * 32768)) & 0xFFFF


class Board:
    def __init__(self):
        self.chip = HwChip()
        self.chip.connect(verify_dataplane=False)
        self.chip._t.reset(leave=True)
        self.chip._t.reset(leave=False)
        self.chip.drain(timeout_ms=100)
        self.chip._out_words.clear()

    def set_gain(self, cell, g):
        self.chip._t.send_words([_encode_write(30, CELLS[cell]["coeff_dest"]), q15(g)])
        self.chip.drain(timeout_ms=5)

    def stream(self, cell, sq):
        c = CELLS[cell]
        tg = self.chip.stream_samples(sq, target_hop_cnt=30, target_addr=c["samp_dest"],
                                      entry_addr=c["entry"], with_tags=True)
        return [v for (v, t) in tg if t == c["out_tag"]]

    def close(self):
        self.chip.close()


def main():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    board = Board()
    board.set_gain("A", 0.5)
    board.set_gain("B", 0.5)

    win = QtWidgets.QWidget()
    win.setWindowTitle("Two multiplexed gain cells — live sliders (hardware)")
    layout = QtWidgets.QVBoxLayout(win)

    chart = QChart()
    chart.setTitle("cell A (blue) vs cell B (red) — gained outputs, shared ports")
    ser_a = QLineSeries(); ser_a.setName("A out")
    ser_b = QLineSeries(); ser_b.setName("B out")
    ser_a.setColor(QtGui.QColor("blue")); ser_b.setColor(QtGui.QColor("red"))
    chart.addSeries(ser_a); chart.addSeries(ser_b)
    ax = QValueAxis(); ax.setRange(0, N)
    ay = QValueAxis(); ay.setRange(-1.1, 1.1)
    chart.addAxis(ax, QtCore.Qt.AlignBottom); chart.addAxis(ay, QtCore.Qt.AlignLeft)
    for s in (ser_a, ser_b):
        s.attachAxis(ax); s.attachAxis(ay)
    view = QChartView(chart)
    layout.addWidget(view)

    def slider_row(cell):
        row = QtWidgets.QHBoxLayout()
        lbl = QtWidgets.QLabel(f"gain {cell}: 0.50")
        s = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        s.setMinimum(-100); s.setMaximum(100); s.setValue(50)

        def on_change(v):
            g = v / 100.0
            lbl.setText(f"gain {cell}: {g:+.2f}")
            board.set_gain(cell, g)
        s.valueChanged.connect(on_change)
        row.addWidget(lbl); row.addWidget(s)
        layout.addLayout(row)
    slider_row("A")
    slider_row("B")
    win.resize(900, 560)
    win.show()

    ph = {"A": 0, "B": 0}

    def tick():
        for cell, ser in (("A", ser_a), ("B", ser_b)):
            f = CELLS[cell]["freq"]
            n = np.arange(N)
            sig = 0.8 * np.sin(2 * np.pi * f * (n + ph[cell]) / N)
            ph[cell] += N
            out = board.stream(cell, [q15(x) for x in sig])
            if out:
                pts = [QtCore.QPointF(i, _as_i16(v) / 32768.0)
                       for i, v in enumerate(out)]
                ser.replace(pts)

    timer = QtCore.QTimer()
    timer.timeout.connect(tick)
    timer.start(50)

    try:
        app.exec()
    finally:
        board.close()


if __name__ == "__main__":
    main()
