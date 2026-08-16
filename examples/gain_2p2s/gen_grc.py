#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate gain_2p2s.grc — a 4-STREAM demo across the 2P2S board's two parallel
daisy-chains (the multi-chip live bridge).

FOUR independent gain streams, one per chip — that's the multiplexing: each source/
sink pair carries ONE stream, and each stream taps ONE gain cell. The chip design is
the shipped gain_2p2s.kyt (open it in placeKYT, Run as GNURadio Server).

    chain A (chip0 -> chip1):  stream A -> chip0's gain ;  stream B -> chip1's gain
    chain B (chip2 -> chip3):  stream C -> chip2's gain ;  stream D -> chip3's gain

Streams A+B share chain A's head (chip0.x16_in) and tail (chip1.x16_out); C+D share
chain B's. They are distinguished purely by their stream_id / tag — the multiplex.
Each stream's output is 0.5x its input (one gain of 0.5). placeKYT (the PHYSICAL side)
owns WHICH chip/port/hop each stream_id maps to — the GRC source/sink (the LOGICAL
side) carry ONLY the stream_id. Clean separation of concerns.

Run:  python3 examples/gain_2p2s/gen_grc.py   (writes gain_2p2s.grc next to it)
Then: open gain_2p2s.kyt in placeKYT -> Run as GNURadio Server, set server_port here
to the printed port, and Run this flowgraph.
"""
from __future__ import annotations

import os

try:
    import yaml
except ImportError:  # pragma: no cover
    raise SystemExit("PyYAML required: pip install pyyaml")

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "gain_2p2s.grc")

# One entry per stream: (stream_id, which chip's gain it taps, plot color, sine freq).
STREAMS = [
    {"sid": "A", "gain_chip": 0, "color": "blue",  "freq": 2},
    {"sid": "B", "gain_chip": 1, "color": "red",   "freq": 3},
    {"sid": "C", "gain_chip": 2, "color": "green", "freq": 4},
    {"sid": "D", "gain_chip": 3, "color": "magenta", "freq": 5},
]


def _blk(name, bid, params, x, y):
    return {
        "name": name, "id": bid, "parameters": params,
        "states": {"bus_sink": False, "bus_source": False, "bus_structure": None,
                   "coordinate": [x, y], "rotation": 0, "state": "enabled"},
    }


def _time_sink(name, title, color2, x, y):
    ts = {f"alpha{k}": "1.0" for k in range(1, 11)}
    ts.update({f"color{k}": "blue" for k in range(1, 11)})
    ts.update({f"label{k}": f"Signal {k}" for k in range(1, 11)})
    ts.update({
        "affinity": "", "alias": "", "autoscale": "False", "axislabels": "True",
        "color1": "blue", "color2": color2, "comment": f"{title}: input vs 0.5x",
        "ctrlpanel": "False", "entags": "True", "grid": "True", "gui_hint": "",
        "label1": "input", "label2": f"{title} out (0.5x)", "legend": "True",
        "name": f'"{title}"', "nconnections": "2", "size": "burst_len",
        "srate": "burst_len", "type": "float", "tr_chan": "0", "tr_delay": "0",
        "tr_level": "0.0", "tr_mode": "qtgui.TRIG_MODE_FREE",
        "tr_slope": "qtgui.TRIG_SLOPE_POS", "tr_tag": '""', "ylabel": "Amplitude",
        "ymax": "1", "ymin": "-1", "yunit": '""'})
    return _blk(name, "qtgui_time_sink_x", ts, x, y)


def build():
    blocks = []
    connections = []
    blocks.append(_blk("burst_len", "variable", {"comment": "", "value": "256"},
                       200, 12))
    blocks.append(_blk("server_port", "variable",
                       {"comment": "placeKYT's default host port (Run as GNURadio "
                                   "Server binds 58950; change if it prints another)",
                        "value": "58950"},
                       360, 12))

    # LIVE per-die gain sliders: dragging one retunes ITS chip's gain cell on the
    # RUNNING fabric (a coefficient WRITE injected at the chain head, riding the
    # inter-chip wire for the far dies) — no reflash. block_name pins each slider
    # to its placeKYT block ("gain"/"gain_1"/"gain_2"/"gain_3" in gain_2p2s.kyt):
    # REQUIRED with four same-type gains (GRC construction order is not the .grc
    # order, so order-based matching could retune the wrong die).
    for i, s in enumerate(STREAMS):
        sid = s["sid"]
        blocks.append(_blk(f"gain_{sid.lower()}", "variable_qtgui_range", {
            "comment": f"LIVE gain for stream {sid} (chip{s['gain_chip']}'s cell)",
            "gui_hint": "", "label": f"gain {sid} (chip{s['gain_chip']}, live)",
            "min_len": "200", "orient": "Qt.Horizontal", "rangeType": "float",
            "start": "-1.0", "step": "0.01", "stop": "1.0", "value": "0.5",
            "widget": "counter_slider"}, 520 + i * 170, 12))

    for i, s in enumerate(STREAMS):
        y = 180 + i * 220
        sid = s["sid"]
        vec = (f"[0.8*float(__import__('math').sin(2*3.141592653589793*"
               f"{s['freq']}*n/burst_len)) for n in range(burst_len)]")
        blocks.append(_blk(f"src{sid}", "blocks_vector_source_x", {
            "affinity": "", "alias": "",
            "comment": f"stream {sid} sine (taps chip{s['gain_chip']}'s gain)",
            "maxoutbuf": "0", "minoutbuf": "0", "repeat": "True", "tags": "[]",
            "type": "float", "vector": vec, "vlen": "1"}, 40, y))
        # kyttar_source: LOGICAL — carries only the stream_id. placeKYT maps it.
        blocks.append(_blk(f"msrc{sid}", "kyttar_source", {
            "affinity": "", "alias": "", "burst_len": "burst_len",
            "comment": f"stream {sid} -> placeKYT (stream_id only; chip/hop resolved "
                       f"there)",
            "complex_in": "float", "device_id": '"kyttar_0"',
            "maxoutbuf": "0", "minoutbuf": "0", "num_channels": "1",
            "port_name": '"x16_in"', "server_host": '"127.0.0.1"',
            "server_port": "server_port", "stream_id": f'"{sid}"',
            "pipelined": "False", "repeat": "'yes'",
            "schedule": '"interleaved"'}, 290, y))
        # kyttar_gain: the DSP block, LIVE-tuned by its slider. placeKYT places it
        # on chip{gain_chip}; block_name pins the slider to that placed block.
        kyt_name = "gain" if s["gain_chip"] == 0 else f"gain_{s['gain_chip']}"
        blocks.append(_blk(f"gain{sid}", "kyttar_gain", {
            "affinity": "", "alias": "",
            "comment": f"live gain (chip{s['gain_chip']} = {kyt_name})",
            "device_id": '"kyttar_0"', "gain": f"gain_{sid.lower()}",
            "block_name": f'"{kyt_name}"',
            "maxoutbuf": "0", "minoutbuf": "0"}, 520, y))
        # kyttar_sink: drains stream {sid}'s recovered words (same stream_id).
        blocks.append(_blk(f"msink{sid}", "kyttar_sink", {
            "affinity": "", "alias": "",
            "comment": f"stream {sid} recovered (0.5x)", "device_id": '"kyttar_0"',
            "hold_secs": "8.0", "in_type": "float", "maxoutbuf": "0",
            "minoutbuf": "0", "num_channels": "1", "port_name": '"x16_out"',
            "server_port": "server_port", "server_repeat": "True",
            "stream_id": f'"{sid}"'}, 750, y))
        blocks.append(_time_sink(f"time_sink_{sid}", f"Stream {sid}", s["color"],
                                 980, y))
        # wiring: src -> msrc -> gain -> msink -> time_sink(bottom); src -> top
        connections.append([f"src{sid}", "0", f"msrc{sid}", "0"])
        connections.append([f"msrc{sid}", "0", f"gain{sid}", "0"])
        connections.append([f"gain{sid}", "0", f"msink{sid}", "0"])
        connections.append([f"msink{sid}", "0", f"time_sink_{sid}", "1"])
        connections.append([f"src{sid}", "0", f"time_sink_{sid}", "0"])

    doc = {
        "options": {"parameters": {
            "author": "Lattrex", "catch_exceptions": "True",
            "category": "[GRC Hier Blocks]", "cmake_opt": "", "comment": "",
            "copyright": "", "description":
                "2P2S multi-chip demo: FOUR gain streams (one per chip) across two "
                "parallel daisy-chains, multiplexed over the placeKYT multi-chip "
                "GNURadio server. Each stream -> 0.5x.",
            "gen_cmake": "On", "gen_linking": "dynamic",
            "generate_options": "qt_gui", "hier_block_src_path": ".:",
            "id": "gain_2p2s_demo", "max_nouts": "0", "output_language": "python",
            "placement": "(0,0)", "qt_qss_theme": "", "realtime_scheduling": "",
            "run": "True", "run_command": "{python} -u {filename}",
            "run_options": "prompt", "sizing_mode": "fixed",
            "thread_safe_setters": "",
            "title": "Kyttar 2P2S — four multiplexed gain streams (0.5x each)",
            "window_size": "(1400,900)"},
            "states": {"bus_sink": False, "bus_source": False,
                       "bus_structure": None, "coordinate": [8, 8],
                       "rotation": 0, "state": "enabled"}},
        "blocks": blocks,
        "connections": connections,
        "metadata": {"file_format": 1, "grc_version": "3.10.12.0"},
    }
    return doc


def main():
    with open(OUT, "w") as f:
        yaml.safe_dump(build(), f, sort_keys=False, default_flow_style=False)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
