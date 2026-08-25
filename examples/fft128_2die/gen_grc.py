#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate ``fft128_2die.grc`` — drive the two-die FFT128 through the placeKYT
MULTI-CHIP GNURadio server and display what comes back.

    tone/noise burst ─▶ kyttar_source ─▶ [FFT128 die0] ─▶ [FFT128 die1]
                                              (chip 0)        (chip 1)
                        ─▶ kyttar_sink ─▶ magnitude ─▶ scope

The GR flowgraph is the LOGICAL app: it carries only ``stream_id``. placeKYT
owns which chip / port / hop / tag that maps to, resolved from the placed and
routed ``fft128_2die.kyt``. The two die markers sit in the chain so the
flowgraph reads as the transform it is; they compute nothing in-process.

Run:  python3 examples/fft128_2die/gen_grc.py
Then: open fft128_2die.kyt in placeKYT -> Simulation -> Run as GNURadio Server,
      and run this flowgraph (server_port must match the printed port; the
      default 58950 is placeKYT's).
"""
from __future__ import annotations

import os

try:
    import yaml
except ImportError:  # pragma: no cover
    raise SystemExit("PyYAML required: pip install pyyaml")

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "fft128_2die.grc")

#: One frame plus the transform's 127-sample latency, so the scope sees real
#: bins rather than the zero-fill transient.
BURST = 384
#: A QT time_sink draws NOTHING until a FULL ``size`` buffer arrives, and the
#: GR scheduler strands the tail of a finite stream — so a scope sized at (or
#: above) its burst NEVER paints. Stay clear of the burst by a margin.
SCOPE_POINTS = BURST - 64


def _blk(name, bid, params, x, y):
    return {
        "name": name, "id": bid, "parameters": params,
        "states": {"bus_sink": False, "bus_source": False,
                   "bus_structure": None, "coordinate": [x, y],
                   "rotation": 0, "state": "enabled"},
    }


def _time_sink(name, title, comment, x, y):
    ts = {f"alpha{k}": "1.0" for k in range(1, 11)}
    ts.update({f"color{k}": "blue" for k in range(1, 11)})
    ts.update({f"label{k}": f"Signal {k}" for k in range(1, 11)})
    ts.update({
        "affinity": "", "alias": "", "autoscale": "True",
        "axislabels": "True", "color1": "blue", "comment": comment,
        "ctrlpanel": "False", "entags": "True", "grid": "True",
        "gui_hint": "", "label1": title, "legend": "True",
        "name": f'"{title}"', "nconnections": "1", "size": "scope_points",
        "srate": "scope_points", "type": "float", "tr_chan": "0",
        "tr_delay": "0", "tr_level": "0.0",
        "tr_mode": "qtgui.TRIG_MODE_FREE",
        "tr_slope": "qtgui.TRIG_SLOPE_POS", "tr_tag": '""',
        "ylabel": "Magnitude", "ymax": "1", "ymin": "-1", "yunit": '""'})
    return _blk(name, "qtgui_time_sink_x", ts, x, y)


def build():
    blocks, connections = [], []

    blocks.append(_blk("burst_len", "variable",
                       {"comment": "one 128-bin frame past the 127-sample "
                                   "latency", "value": str(BURST)}, 200, 12))
    blocks.append(_blk("scope_points", "variable",
                       {"comment": "< burst_len: a QT time_sink never paints "
                                   "a buffer it cannot fill",
                        "value": str(SCOPE_POINTS)}, 360, 12))
    blocks.append(_blk("server_port", "variable",
                       {"comment": "placeKYT's default host port (Run as "
                                   "GNURadio Server binds 58950; change if it "
                                   "prints another)",
                        "value": "58950"}, 560, 12))

    # A two-tone complex stimulus: distinct bins so a correct transform is
    # visually obvious and a wrong one is not.
    vec = ("[0.45*__import__('cmath').exp(2j*3.141592653589793*9*n/128)"
           " + 0.35*__import__('cmath').exp(2j*3.141592653589793*37*n/128)"
           " for n in range(burst_len)]")
    blocks.append(_blk("stim", "blocks_vector_source_x", {
        "affinity": "", "alias": "",
        "comment": "two complex tones (bins 9 and 37 of 128)",
        "maxoutbuf": "0", "minoutbuf": "0", "repeat": "True", "tags": "[]",
        "type": "complex", "vector": vec, "vlen": "1"}, 40, 200))

    # kyttar_source: LOGICAL — carries only the stream_id. placeKYT resolves
    # it to chip 0's head, its hop, entry and the xi/xq registers.
    blocks.append(_blk("kyt_src", "kyttar_source", {
        "affinity": "", "alias": "", "burst_len": "burst_len",
        "comment": "stream 'fft' -> chip 0's head (chip/hop resolved in "
                   "placeKYT)",
        "complex_in": "complex", "device_id": '"kyttar_0"',
        "maxoutbuf": "0", "minoutbuf": "0", "num_channels": "1",
        "output_words": "False", "port_name": '"x16_in"',
        "pipelined": "False", "repeat": "'yes'",
        "schedule": '"interleaved"', "server_host": '"127.0.0.1"',
        "server_port": "server_port", "stream_id": '"fft"'}, 290, 200))

    # The two dies, in chain order. Markers: the DSP runs on the chips.
    blocks.append(_blk("die0", "kyttar_fft128_die0", {
        "affinity": "", "alias": "",
        "comment": "chip 0 — stage 0 (period-64 octant fold). Its output is a "
                   "PARTIAL transform, not bins.",
        "device_id": '"kyttar_0"', "maxoutbuf": "0", "minoutbuf": "0"},
        540, 200))
    blocks.append(_blk("die1", "kyttar_fft128_die1", {
        "affinity": "", "alias": "",
        "comment": "chip 1 — stages 1..6. Output = the transform's bins, "
                   "BIT-REVERSED, scale FFT/128.",
        "device_id": '"kyttar_0"', "maxoutbuf": "0", "minoutbuf": "0"},
        720, 200))

    # kyttar_sink: drains the recovered words off the CHAIN TAIL (chip 1's
    # x16_out). Same stream_id; placeKYT knows the tail is chip 1.
    blocks.append(_blk("kyt_sink", "kyttar_sink", {
        "affinity": "", "alias": "",
        "comment": "recovered bins off chip 1's x16_out (the chain tail)",
        "device_id": '"kyttar_0"', "hold_secs": "8.0", "in_type": "complex",
        "maxoutbuf": "0", "minoutbuf": "0", "num_channels": "1",
        "port_name": '"x16_out"', "server_port": "server_port",
        "server_repeat": "True", "stream_id": '"fft"'}, 920, 200))

    # The kyttar sink's monitoring output is a FLOAT stream of the recovered
    # words — the transform's bins as interleaved I, Q at the q15/32768 scale.
    # Plot it directly: the two input tones show as a repeating structure with
    # energy concentrated in a few slots, and a dead chain shows a flat line.
    blocks.append(_time_sink(
        "bins", "FFT128 output words (I, Q interleaved)",
        "the transform's bins off chip 1. Slot k of each 128-bin frame "
        "carries bin bit_reverse_7(k) — the DIF order, no reorder buffer.",
        1160, 180))

    # The input, for comparison.
    blocks.append(_blk("in_mag", "blocks_complex_to_mag", {
        "affinity": "", "alias": "", "comment": "|input| for comparison",
        "maxoutbuf": "0", "minoutbuf": "0", "vlen": "1"}, 960, 420))
    blocks.append(_time_sink("in_scope", "input |x[n]| (two tones)",
                             "the stimulus driving the pair", 1160, 400))

    connections.append(["stim", "0", "kyt_src", "0"])
    connections.append(["kyt_src", "0", "die0", "0"])
    connections.append(["die0", "0", "die1", "0"])
    connections.append(["die1", "0", "kyt_sink", "0"])
    connections.append(["kyt_sink", "0", "bins", "0"])
    connections.append(["stim", "0", "in_mag", "0"])
    connections.append(["in_mag", "0", "in_scope", "0"])

    return {
        "options": {"parameters": {
            "author": "Lattrex", "catch_exceptions": "True",
            "category": "[GRC Hier Blocks]", "cmake_opt": "", "comment": "",
            "copyright": "", "description":
                "FFT128 across TWO DIES: chip 0 runs stage 0, chip 1 runs "
                "stages 1..6, joined by the inter-chip crossing. Driven "
                "through the placeKYT multi-chip GNURadio server.",
            "gen_cmake": "On", "gen_linking": "dynamic",
            "generate_options": "qt_gui", "hier_block_src_path": ".:",
            "id": "fft128_2die_demo", "max_nouts": "0",
            "output_language": "python", "placement": "(0,0)",
            "qt_qss_theme": "", "realtime_scheduling": "", "run": "True",
            "run_command": "{python} -u {filename}", "run_options": "prompt",
            "sizing_mode": "fixed", "thread_safe_setters": "",
            "title": "Kyttar FFT128 — a 128-point transform across two dies",
            "window_size": "(1500,900)"},
            "states": {"bus_sink": False, "bus_source": False,
                       "bus_structure": None, "coordinate": [8, 8],
                       "rotation": 0, "state": "enabled"}},
        "blocks": blocks,
        "connections": connections,
        "metadata": {"file_format": 1, "grc_version": "3.10.12.0"},
    }


def main():
    with open(OUT, "w") as f:
        yaml.safe_dump(build(), f, sort_keys=False, default_flow_style=False)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
