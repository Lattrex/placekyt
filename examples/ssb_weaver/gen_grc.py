#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate ssb_weaver.grc — the FULL on-chip SSB Weaver transceiver flowgraph.

Emits a GNU Radio Companion .grc (YAML flowgraph) built from the REAL Kyttar DSP
blocks so it IMPORTS into placeKYT (File -> Import GNURadio Flowgraph): the 10-block
Weaver chain places + auto-P&R-routes, and source/sink map to chip ports x16_in /
x16_out. The SAME flowgraph runs linked to a placeKYT-hosted chip (Simulation ->
Run as GNURadio Server), streaming an audio burst through and plotting input-audio
vs recovered-audio.

Weaver chain (real audio in -> recovered audio out), USB, fa=1500 fc=6000 @ 32 kHz:
  audio -> ComplexMixer(-fa) -> ComplexToFloat -> [LPF I, LPF Q] -> IQUpconvert(fc)
        -> SSB -> ComplexMixer(-fc) -> ComplexToFloat -> [LPF I, LPF Q]
        -> IQUpconvert(fa) -> Gain(x4) -> recovered audio

Run: <venv>/python examples/ssb_weaver/gen_grc.py   (writes ssb_weaver.grc alongside)
"""
import os

FS = 32000.0
FA = 1500.0        # Weaver audio-band centre
FC = 6000.0        # carrier
CUT = 1200.0       # Weaver LPF cutoff (half the audio bandwidth)
TW = 2500.0        # LPF transition width (tap count)

HDR = """options:
  parameters:
    author: Kyttar
    catch_exceptions: 'True'
    category: '[GRC Hier Blocks]'
    cmake_opt: ''
    comment: ''
    copyright: ''
    description: "FULL on-chip SSB Weaver TRANSCEIVER (real blocks -> imports into\\
      \\ placeKYT). audio -> ComplexMixer(-fa) -> ComplexToFloat -> 2x LowPass ->\\
      \\ IQUpconvert(fc) [SSB] -> ComplexMixer(-fc) -> ComplexToFloat -> 2x LowPass\\
      \\ -> IQUpconvert(fa) -> Gain -> recovered audio. Import here to auto-P&R the\\
      \\ 10-block chain onto the chip and SEE where the router struggles; or Run as\\
      \\ GNURadio Server in placeKYT + Execute here to stream audio through the chip."
    gen_cmake: 'On'
    gen_linking: dynamic
    generate_options: qt_gui
    hier_block_src_path: '.:'
    id: ssb_weaver_transceiver
    max_nouts: '0'
    output_language: python
    placement: (0,0)
    qt_qss_theme: ''
    realtime_scheduling: ''
    run: 'True'
    run_command: '{python} -u {filename}'
    run_options: prompt
    sizing_mode: fixed
    thread_safe_setters: ''
    title: "SSB Weaver transceiver (on-chip) — audio in vs recovered audio"
    window_size: (1600,1000)
  states:
    bus_sink: false
    bus_source: false
    bus_structure: null
    coordinate: [8, 8]
    rotation: 0
    state: enabled

blocks:
"""


def blk(name, bid, params, x, y):
    lines = [f"- name: {name}", f"  id: {bid}", "  parameters:"]
    for k, v in params.items():
        lines.append(f"    {k}: {v}")
    lines += ["  states:",
              "    bus_sink: false", "    bus_source: false",
              "    bus_structure: null",
              f"    coordinate: [{x}, {y}]", "    rotation: 0",
              "    state: enabled", ""]
    return "\n".join(lines)


def lpf(name, x, y):
    return blk(name, "kyttar_low_pass_filter", {
        "affinity": "''", "alias": "''", "beta": "'6.76'", "comment": "''",
        "cutoff_freq": repr(CUT), "decimation": "'1'",
        "device_id": "'\"kyttar_0\"'", "gain": "'1'", "interpolation": "'1'",
        "maxoutbuf": "'0'", "minoutbuf": "'0'", "samp_rate": repr(FS),
        "transition_width": repr(TW), "window": '"hamming"',
    }, x, y)


def mixer(name, freq, x, y):
    return blk(name, "kyttar_complex_mixer", {
        "affinity": "''", "alias": "''", "comment": "''",
        "device_id": "'\"kyttar_0\"'", "frequency": repr(freq),
        "maxoutbuf": "'0'", "minoutbuf": "'0'", "sample_rate": repr(FS),
    }, x, y)


def upc(name, freq, x, y):
    return blk(name, "kyttar_iq_upconvert", {
        "affinity": "''", "alias": "''", "comment": "''",
        "device_id": "'\"kyttar_0\"'", "frequency": repr(freq),
        "maxoutbuf": "'0'", "minoutbuf": "'0'", "sample_rate": repr(FS),
    }, x, y)


def c2f(name, x, y):
    return blk(name, "kyttar_complex_to_float", {
        "affinity": "''", "alias": "''", "comment": "''",
        "device_id": "'\"kyttar_0\"'", "maxoutbuf": "'0'", "minoutbuf": "'0'",
    }, x, y)


def main():
    out = [HDR]
    # --- variables ---
    out.append(blk("samp_rate", "variable",
                   {"comment": "''", "value": repr(FS)}, 200, 12))
    out.append(blk("n_samp", "variable",
                   {"comment": "audio burst length", "value": "'2048'"}, 320, 12))
    out.append(blk("server_port", "variable",
                   {"comment": "placeKYT GNURadio-server port",
                    "value": "'58950'"}, 440, 12))

    # --- host-side audio stimulus: two in-band tones (a simple 'voice') ---
    out.append(blk("tone", "analog_sig_source_x", {
        "affinity": "''", "alias": "''", "amp": "'0.5'", "comment": "''",
        "freq": "'800'", "maxoutbuf": "'0'", "minoutbuf": "'0'",
        "offset": "'0'", "phase": "'0'", "samp_rate": "samp_rate",
        "showports": "'False'", "type": "float", "waveform": "analog.GR_SIN_WAVE",
    }, 40, 140))
    out.append(blk("tone2", "analog_sig_source_x", {
        "affinity": "''", "alias": "''", "amp": "'0.3'", "comment": "''",
        "freq": "'1800'", "maxoutbuf": "'0'", "minoutbuf": "'0'",
        "offset": "'0'", "phase": "'0'", "samp_rate": "samp_rate",
        "showports": "'False'", "type": "float", "waveform": "analog.GR_SIN_WAVE",
    }, 40, 260))
    out.append(blk("audio", "blocks_add_xx", {
        "affinity": "''", "alias": "''", "comment": "'input audio (2 tones)'",
        "maxoutbuf": "'0'", "minoutbuf": "'0'", "num_inputs": "'2'",
        "type": "float", "vlen": "'1'",
    }, 240, 200))
    out.append(blk("thr", "blocks_throttle2", {
        "affinity": "''", "alias": "''", "comment": "''",
        "ignoretag": "'True'", "limit": "auto", "maximum": "'0.1'",
        "maxoutbuf": "'0'", "minoutbuf": "'0'", "samples_per_second": "samp_rate",
        "type": "float", "vlen": "'1'",
    }, 400, 208))

    # --- chip source: real audio -> x16_in (as the mixer's real input) ---
    out.append(blk("msrc", "kyttar_source", {
        "affinity": "''", "alias": "''", "burst_len": "n_samp",
        "comment": "'audio -> chip x16_in (batch)'", "complex_in": "float",
        "device_id": "'\"kyttar_0\"'", "maxoutbuf": "'0'", "minoutbuf": "'0'",
        "num_channels": "'1'", "port_name": "'\"x16_in\"'",
        "server_host": "'\"127.0.0.1\"'", "server_port": "server_port",
        "stream_id": "''",
    }, 560, 200))

    # --- TX half: mixer(-fa) -> split -> LPF I/Q -> upconvert(fc) [SSB] ---
    out.append(mixer("tx_mix", -FA, 720, 180))
    out.append(c2f("tx_split", 900, 188))
    out.append(lpf("tx_lpi", 1040, 140))
    out.append(lpf("tx_lpq", 1040, 260))
    out.append(upc("tx_up", FC, 1220, 188))
    # --- RX half: mixer(-fc) -> split -> LPF I/Q -> upconvert(fa) ---
    out.append(mixer("rx_mix", -FC, 1400, 180))
    out.append(c2f("rx_split", 1580, 188))
    out.append(lpf("rx_lpi", 1720, 140))
    out.append(lpf("rx_lpq", 1720, 260))
    out.append(upc("rx_up", FA, 1900, 188))
    # --- x4 Weaver gain (recovered audio scale) ---
    out.append(blk("g4", "kyttar_gain", {
        "affinity": "''", "alias": "''", "comment": "'Weaver 1/4 -> x4'",
        "device_id": "'\"kyttar_0\"'", "gain": "'4'",
        "maxoutbuf": "'0'", "minoutbuf": "'0'",
    }, 2080, 188))

    # --- chip sink: recovered audio <- x16_out ---
    out.append(blk("msink", "kyttar_sink", {
        "affinity": "''", "alias": "''", "comment": "'recovered audio <- x16_out'",
        "device_id": "'\"kyttar_0\"'", "num_channels": "'1'",
        "port_name": "'\"x16_out\"'", "server_host": "'\"127.0.0.1\"'",
        "server_port": "server_port", "stream_id": "''",
    }, 2260, 200))

    # --- waveform sinks: input audio (top) vs recovered audio (bottom) ---
    out.append(blk("in_sink", "qtgui_time_sink_x", {
        "affinity": "''", "alias": "''", "autoscale": "'False'",
        "axislabels": "'True'", "bw": "samp_rate", "comment": "''",
        "ctrlpanel": "'False'", "entags": "'True'", "grid": "'True'",
        "gui_hint": "''", "label1": "'input audio'", "legend": "'True'",
        "marker1": "'-1'", "name": '"Input audio"', "nconnections": "'1'",
        "size": "n_samp", "srate": "samp_rate", "stemplot": "'False'",
        "style1": "'1'", "tr_chan": "'0'", "tr_delay": "'0'", "tr_level": "'0'",
        "tr_mode": "qtgui.TRIG_MODE_FREE", "tr_slope": "qtgui.TRIG_SLOPE_POS",
        "tr_tag": "''", "type": "float", "update_time": "'0.10'",
        "width1": "'1'", "ylabel": "'Amplitude'", "ymax": "'1'", "ymin": "'-1'",
        "yunit": "''", "grid_color1": "'blue'", "alpha1": "'1.0'",
    }, 720, 40))
    out.append(blk("out_sink", "qtgui_time_sink_x", {
        "affinity": "''", "alias": "''", "autoscale": "'True'",
        "axislabels": "'True'", "bw": "samp_rate", "comment": "''",
        "ctrlpanel": "'False'", "entags": "'True'", "grid": "'True'",
        "gui_hint": "''", "label1": "'recovered audio'", "legend": "'True'",
        "marker1": "'-1'", "name": '"Recovered audio (chip out)"',
        "nconnections": "'1'", "size": "n_samp", "srate": "samp_rate",
        "stemplot": "'False'", "style1": "'1'", "tr_chan": "'0'",
        "tr_delay": "'0'", "tr_level": "'0'", "tr_mode": "qtgui.TRIG_MODE_FREE",
        "tr_slope": "qtgui.TRIG_SLOPE_POS", "tr_tag": "''", "type": "float",
        "update_time": "'0.10'", "width1": "'1'", "ylabel": "'Amplitude'",
        "ymax": "'1'", "ymin": "'-1'", "yunit": "''", "grid_color1": "'green'",
        "alpha1": "'1.0'",
    }, 2260, 40))

    # --- connections ---
    conns = [
        ("tone", 0, "audio", 0), ("tone2", 0, "audio", 1),
        ("audio", 0, "thr", 0),
        ("thr", 0, "msrc", 0),          # audio -> chip source
        ("thr", 0, "in_sink", 0),       # input-audio scope
        ("msrc", 0, "tx_mix", 0),       # x16_in -> TX mixer (real xi)
        # TX half
        ("tx_mix", 0, "tx_split", 0),
        ("tx_split", 0, "tx_lpi", 0),   # I rail -> LPF
        ("tx_split", 1, "tx_lpq", 0),   # Q rail -> LPF
        ("tx_lpi", 0, "tx_up", 0),      # I -> upconvert (as its complex xi)
        ("tx_lpq", 0, "tx_up", 1),      # Q -> upconvert (xq)
        ("tx_up", 0, "rx_mix", 0),      # SSB -> RX mixer
        # RX half
        ("rx_mix", 0, "rx_split", 0),
        ("rx_split", 0, "rx_lpi", 0),
        ("rx_split", 1, "rx_lpq", 0),
        ("rx_lpi", 0, "rx_up", 0),
        ("rx_lpq", 0, "rx_up", 1),
        ("rx_up", 0, "g4", 0),
        ("g4", 0, "msink", 0),          # recovered audio -> x16_out
        ("msink", 0, "out_sink", 0),    # recovered-audio scope
    ]
    out.append("connections:")
    for s, sp, d, dp in conns:
        out.append(f"- [{s}, '{sp}', {d}, '{dp}']")
    out.append("")
    out.append("metadata:")
    out.append("  file_format: 1")
    out.append("  grc_version: 3.10.12.0")
    out.append("")

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "ssb_weaver.grc")
    with open(path, "w") as f:
        f.write("\n".join(out))
    print("wrote", path)


if __name__ == "__main__":
    main()
