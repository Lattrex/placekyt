#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate gain_2p2s.grc — a 4-stream demo across the 2P2S board's TWO parallel
daisy-chains (the multi-chip live bridge).

Two chains, each a 2-chip daisy-chain of gain cells (0.25x end to end). The chip
design is the shipped gain_2p2s.kyt (open it in placeKYT, Run as GNURadio Server).
Each chain has ONE kyttar_source (carrying its chip_id + landing) driving the chain
HEAD and ONE kyttar_sink (same stream_id) draining the chain TAIL:

    chain A:  sineA -> srcA[chip_id=0] --(chip0 gain, wire, chip1 gain)--> sinkA[stream A]
    chain B:  sineB -> srcB[chip_id=2] --(chip2 gain, wire, chip3 gain)--> sinkB[stream B]

The two sources rendezvous and dispatch ONE process_batch_multichip; each chain's
recovered words (0.25x of its own sine) come back to its sink and plot on the time
sink. Set server_port to the port placeKYT prints when it starts the (multi-chip)
GNURadio server.

The chip_id / out_chip / entry_addr / hop_count / data_addrs on each source are the
resolved landing from placeKYT's multi_chip_stream_targets — for the at-landing
gain_2p2s.kyt they are entry=28, hop=30, data_addr=0 (a gain sitting at (0,0)).
placeKYT prints the resolved values on the server console at start-up.

Run:  python3 examples/gain_2p2s/gen_grc.py   (writes gain_2p2s.grc next to it)
"""
from __future__ import annotations

import os

try:
    import yaml
except ImportError:  # pragma: no cover
    raise SystemExit("PyYAML required: pip install pyyaml")

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "gain_2p2s.grc")

# Chain head chip / tail chip / stream_id / plot color per chain.
CHAINS = [
    {"sid": "A", "head": 0, "tail": 1, "color": "blue",  "freq": 2},
    {"sid": "B", "head": 2, "tail": 3, "color": "red",   "freq": 3},
]
# At-landing gain@(0,0) landing (gain_2p2s.kyt). placeKYT prints these at start-up;
# a routed design would use its own entry/hop/data_addrs.
ENTRY, HOP, DATA_ADDRS = 28, 30, "0"


def _blk(name, bid, params, x, y):
    return {
        "name": name, "id": bid, "parameters": params,
        "states": {"bus_sink": False, "bus_source": False, "bus_structure": None,
                   "coordinate": [x, y], "rotation": 0, "state": "enabled"},
    }


def build():
    blocks = []
    connections = []

    # Flowgraph variables.
    blocks.append(_blk("burst_len", "variable", {"comment": "", "value": "256"},
                       200, 12))
    blocks.append(_blk("server_port", "variable",
                       {"comment": "set to the port placeKYT prints", "value": "0"},
                       360, 12))

    for i, ch in enumerate(CHAINS):
        y = 200 + i * 260
        sid = ch["sid"]
        # bounded slow-sine stimulus (repeat so no EOF)
        vec = (f"[0.8*float(__import__('math').sin(2*3.141592653589793*"
               f"{ch['freq']}*n/burst_len)) for n in range(burst_len)]")
        blocks.append(_blk(f"src{sid}", "blocks_vector_source_x", {
            "affinity": "", "alias": "",
            "comment": f"chain {sid} sine -> chip{ch['head']} head",
            "maxoutbuf": "0", "minoutbuf": "0", "repeat": "True", "tags": "[]",
            "type": "float", "vector": vec, "vlen": "1"}, 40, y))
        # kyttar_source carrying this chain's chip_id + landing
        blocks.append(_blk(f"msrc{sid}", "kyttar_source", {
            "affinity": "", "alias": "", "burst_len": "burst_len",
            "comment": f"stream {sid} -> chip{ch['head']} (chain head)",
            "complex_in": "float", "device_id": '"kyttar_0"',
            "maxoutbuf": "0", "minoutbuf": "0", "num_channels": "1",
            "port_name": '"x16_in"', "server_host": '"127.0.0.1"',
            "server_port": "server_port", "stream_id": f'"{sid}"',
            "pipelined": "False", "schedule": '"interleaved"',
            "chip_id": str(ch["head"]), "out_chip": str(ch["tail"]),
            "entry_addr": str(ENTRY), "hop_count": str(HOP),
            "data_addrs": f'"{DATA_ADDRS}"'}, 300, y))
        # kyttar_sink drains this chain's recovered words (same stream_id)
        blocks.append(_blk(f"msink{sid}", "kyttar_sink", {
            "affinity": "", "alias": "",
            "comment": f"chain {sid} tail (chip{ch['tail']} x16_out), 0.25x",
            "device_id": '"kyttar_0"', "hold_secs": "8.0", "in_type": "float",
            "maxoutbuf": "0", "minoutbuf": "0", "num_channels": "1",
            "port_name": '"x16_out"', "server_port": "server_port",
            "server_repeat": "False", "stream_id": f'"{sid}"'}, 560, y))
        # time sink per chain: input sine (top) vs recovered 0.25x (bottom)
        ts = {f"alpha{k}": "1.0" for k in range(1, 11)}
        ts.update({f"color{k}": "blue" for k in range(1, 11)})
        ts.update({f"label{k}": f"Signal {k}" for k in range(1, 11)})
        ts.update({
            "affinity": "", "alias": "", "autoscale": "False",
            "axislabels": "True", "color1": "blue", "color2": ch["color"],
            "comment": f"chain {sid}: input vs 0.25x", "ctrlpanel": "False",
            "entags": "True", "grid": "True", "gui_hint": "", "label1": "input",
            "label2": f"chain {sid} out (0.25x)", "legend": "True",
            "name": f'"Chain {sid}"', "nconnections": "2", "size": "burst_len",
            "srate": "burst_len", "type": "float", "tr_chan": "0",
            "tr_delay": "0", "tr_level": "0.0", "tr_mode": "qtgui.TRIG_MODE_FREE",
            "tr_slope": "qtgui.TRIG_SLOPE_POS", "tr_tag": '""',
            "ylabel": "Amplitude", "ymax": "1", "ymin": "-1", "yunit": '""'})
        blocks.append(_blk(f"time_sink_{sid}", "qtgui_time_sink_x", ts, 820, y))

        # wiring: src -> msrc -> [chip] -> msink -> time_sink(bottom); src -> top
        connections.append([f"src{sid}", "0", f"msrc{sid}", "0"])
        connections.append([f"msink{sid}", "0", f"time_sink_{sid}", "1"])
        connections.append([f"src{sid}", "0", f"time_sink_{sid}", "0"])

    doc = {
        "options": {"parameters": {
            "author": "Lattrex", "catch_exceptions": "True",
            "category": "[GRC Hier Blocks]", "cmake_opt": "", "comment": "",
            "copyright": "", "description":
                "2P2S multi-chip demo: two parallel gain daisy-chains driven over "
                "the placeKYT multi-chip GNURadio server. Each chain -> 0.25x.",
            "gen_cmake": "On", "gen_linking": "dynamic",
            "generate_options": "qt_gui", "hier_block_src_path": ".:",
            "id": "gain_2p2s_demo", "max_nouts": "0", "output_language": "python",
            "placement": "(0,0)", "qt_qss_theme": "", "realtime_scheduling": "",
            "run": "True", "run_command": "{python} -u {filename}",
            "run_options": "prompt", "sizing_mode": "fixed",
            "thread_safe_setters": "",
            "title": "Kyttar 2P2S — two parallel chains (0.25x each)",
            "window_size": "(1200,800)"},
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
