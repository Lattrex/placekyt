"""Drag a chip port to the waveform viewer, demultiplexed by channel/tag.

A chip port is a TIME-MULTIPLEXED bus — several logical streams can share it. The
user drags a port onto the waveform dock and picks ONE stream (by tag) to view,
rather than all interleaved words at once. This exercises:
  * TraceModel.port_streams_by_tag / port_tags — demux by the per-event dest tag.
  * WaveformView.add_port_stream — a tagged port trace, distinct from the plain
    port traces set_streams manages, surviving a live refresh.
  * WaveformPanel._on_port_dropped — single-tag auto-add; the tag namer labels.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.trace_model import TraceModel  # noqa: E402
from ui.widgets.waveform_view import WaveformView  # noqa: E402
from ui.panels.waveform_panel import WaveformPanel  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _events(rows):
    """rows = [(t_ns, port, dest_tag, value, kind)] -> raw trace-event dicts."""
    return [{"time_ns": t, "kind": k, "port_name": p, "dest": d, "data": v,
             "cell_id": 0} for (t, p, d, v, k) in rows]


def test_port_streams_by_tag_demux():
    m = TraceModel()
    # x16_in carries two interleaved channels (tag 0 = xi, tag 1 = xq).
    m.ingest(0, _events([
        (10, "x16_in", 0, 0x4000, "port_injection"),
        (12, "x16_in", 1, 0x1000, "port_injection"),
        (20, "x16_in", 0, 0x4200, "port_injection"),
        (22, "x16_in", 1, 0x1100, "port_injection"),
    ]), width=10)
    by_tag = m.port_streams_by_tag()
    assert [v for _t, v in by_tag[(0, "x16_in", 0)]] == [0x4000, 0x4200]
    assert [v for _t, v in by_tag[(0, "x16_in", 1)]] == [0x1000, 0x1100]
    assert m.port_tags(0, "x16_in") == [0, 1]
    # The plain (untagged) stream still has all 4 interleaved words.
    assert len(m.port_streams()[(0, "x16_in")]) == 4


def test_input_tag_falls_back_to_entry_address():
    """An input injection has no WRITE dest; its per-stream tag is the JUMP
    entry_address (which stream it triggered). Two entries demux into two tags."""
    m = TraceModel()
    m.transactions = []
    evs = [
        {"time_ns": 10, "kind": "port_injection", "port_name": "x16_in",
         "data": 0x4000, "entry_address": 1, "cell_id": 0},
        {"time_ns": 20, "kind": "port_injection", "port_name": "x16_in",
         "data": 0x4100, "entry_address": 1, "cell_id": 0},
        {"time_ns": 30, "kind": "port_injection", "port_name": "x16_in",
         "data": 0x0001, "entry_address": 7, "cell_id": 0},
    ]
    m.ingest(0, evs, width=10)
    assert m.port_tags(0, "x16_in") == [1, 7]
    by_tag = m.port_streams_by_tag()
    assert [v for _t, v in by_tag[(0, "x16_in", 1)]] == [0x4000, 0x4100]
    assert [v for _t, v in by_tag[(0, "x16_in", 7)]] == [0x0001]


def test_untagged_port_buckets_under_none():
    m = TraceModel()
    m.ingest(0, _events([
        (10, "x16_out", None, 0x0001, "port_capture"),
        (20, "x16_out", None, 0x0000, "port_capture"),
    ]), width=10)
    assert m.port_tags(0, "x16_out") == [None]
    assert len(m.port_streams_by_tag()[(0, "x16_out", None)]) == 2


def test_view_tagged_trace_survives_refresh(qapp):
    v = WaveformView()
    v.set_streams({(0, "x16_in"): [(10, 1), (20, 2)]})       # one plain port trace
    v.add_port_stream(0, "x16_in", 1, [(12, 0x1000)], label="x16_in xq")
    keys = {s["key"] for s in v._streams}
    assert ("port", 0, "x16_in") in keys
    assert ("ptag", 0, "x16_in", 1) in keys
    # A refresh of plain port streams must NOT drop the tagged trace.
    v.set_streams({(0, "x16_in"): [(10, 1), (20, 2), (30, 3)]})
    keys = {s["key"] for s in v._streams}
    assert ("ptag", 0, "x16_in", 1) in keys, "tagged port trace dropped on refresh"


def test_panel_drop_single_tag_auto_adds(qapp):
    panel = WaveformPanel()
    m = TraceModel()
    m.ingest(0, _events([
        (10, "x16_out", None, 0x0001, "port_capture"),
        (20, "x16_out", None, 0x0000, "port_capture"),
    ]), width=10)
    panel.set_trace_model(m)
    # One tag (None) → drop auto-adds it without a picker dialog.
    panel._on_port_dropped(0, "x16_out")
    keys = {s["key"] for s in panel.view._streams}
    assert ("ptag", 0, "x16_out", None) in keys


def test_sample_index_axis_mode(qapp):
    """Index mode remaps each trace's samples to ordinals (0,1,2,…) so two
    streams sampled at different ns spacings line up 1:1 — like GRC's Time Sink."""
    v = WaveformView()
    v.set_streams({(0, "x16_in"): [(10, 1), (60, 0), (110, 1)],
                   (0, "x16_out"): [(1000, 1), (1005, 0), (1010, 1)]})
    assert v.x_axis_mode() == "time"
    v.set_x_axis_mode("index")
    assert v.x_axis_mode() == "index"
    assert v._data_bounds() == (0.0, 3.0)        # one column per sample
    lead = v._lead_samples(v._streams[0])
    # The real samples are at ordinals 0,1,2 (any synthetic lead sits before 0).
    real = [t for t, _ in lead if t >= 0]
    assert real == [0.0, 1.0, 2.0]
    v.set_x_axis_mode("time")
    assert v.x_axis_mode() == "time"


def test_panel_tag_namer_labels(qapp):
    panel = WaveformPanel()
    panel.set_port_tag_namer(lambda chip, port, tag:
                             {0: "xi", 1: "xq"}.get(tag))
    assert panel._tag_label(0, "x16_in", 0) == "xi (tag 0)"
    assert panel._tag_label(0, "x16_in", 1) == "xq (tag 1)"
    # No namer entry → bare tag.
    assert panel._tag_label(0, "x16_in", 5) == "tag 5"


def test_right_click_add_channel_lists_and_adds(qapp):
    """The panel's channel provider (right-click 'Add channel') lists every
    captured demuxed port channel and adds the chosen one without a drag."""
    panel = WaveformPanel()
    m = TraceModel()
    m.ingest(0, _events([
        (10, "x16_in", 0, 0x4000, "port_injection"),
        (12, "x16_in", 1, 0x1000, "port_injection"),
        (20, "x16_out", None, 0x0001, "port_capture"),
    ]), width=10)
    panel.set_trace_model(m)
    # x16_in (multiplexed) is auto-split into 2 demuxed traces at set_trace_model;
    # x16_out (single channel) is a plain trace. So the "Add channel" menu only
    # offers channels NOT already shown — here x16_out's lone (None) channel.
    items = panel._available_channels()
    labels = [lbl for lbl, _cb in items]
    assert any("x16_out" in lbl for lbl in labels)
    n0 = len(items)
    assert n0 >= 1
    # Invoking an item adds that demuxed trace and removes it from the next listing.
    items[0][1]()
    keys = {s["key"] for s in panel.view._streams}
    assert any(k[0] == "ptag" for k in keys)
    assert len(panel._available_channels()) == n0 - 1


def test_default_view_splits_multiplexed_port(qapp):
    """A port carrying multiple tagged channels (xi/xq) shows ONE demuxed trace
    per tag by default — not all channels overlaid on one trace. A single-channel
    port stays a single plain-port trace."""
    panel = WaveformPanel()
    m = TraceModel()
    m.ingest(0, _events([
        (10, "x16_in", 0, 0x4000, "port_injection"),   # xi
        (12, "x16_in", 1, 0x1000, "port_injection"),   # xq (multiplexed)
        (20, "x16_in", 0, 0x4200, "port_injection"),
        (22, "x16_in", 1, 0x1100, "port_injection"),
        (30, "x16_out", None, 0x0001, "port_capture"),  # single channel
    ]), width=10)
    panel.set_trace_model(m)
    keys = {s["key"] for s in panel.view._streams}
    # x16_in split into per-tag demuxed traces; NOT a single plain ("port",…) trace.
    assert ("ptag", 0, "x16_in", 0) in keys
    assert ("ptag", 0, "x16_in", 1) in keys
    assert ("port", 0, "x16_in") not in keys
    # x16_out (one channel) stays a single plain-port trace.
    assert ("port", 0, "x16_out") in keys


def test_route_drop_plots_its_channels(qapp):
    """Dropping a route plots the channels flowing through it, via the route
    channel provider (single channel auto-adds; the descriptor becomes a trace)."""
    panel = WaveformPanel()
    m = TraceModel()
    m.ingest(0, _events([
        (20, "x16_out", None, 0x0001, "port_capture"),
        (30, "x16_out", None, 0x0000, "port_capture"),
    ]), width=10)
    panel.set_trace_model(m)
    # Provider: route 'r' delivers to the x16_out port (one channel).
    panel.set_route_channel_provider(lambda name: (
        [("r → x16_out", {"type": "port_tag", "chip": 0, "port": "x16_out",
                          "tag": None})] if name == "r" else []))
    panel._on_route_dropped("r")
    keys = {s["key"] for s in panel.view._streams}
    assert ("ptag", 0, "x16_out", None) in keys
    # (The empty-route case pops a modal info dialog in the GUI; not exercised
    # here because an offscreen modal would block the test.)


if __name__ == "__main__":
    app = QApplication.instance() or QApplication([])
    test_port_streams_by_tag_demux()
    test_input_tag_falls_back_to_entry_address()
    test_untagged_port_buckets_under_none()
    test_view_tagged_trace_survives_refresh(app)
    test_panel_drop_single_tag_auto_adds(app)
    test_panel_tag_namer_labels(app)
    test_sample_index_axis_mode(app)
    test_right_click_add_channel_lists_and_adds(app)
    test_default_view_splits_multiplexed_port(app)
    test_route_drop_plots_its_channels(app)
    print("port-to-waveform demux: ALL PASS")
