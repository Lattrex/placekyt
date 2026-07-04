#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate ssb_weaver.grc — the FULL on-chip SSB Weaver transceiver flowgraph,
built from REAL Kyttar blocks exactly as the textbook Weaver diagram prescribes.

The Weaver ("third method") SSB generator is FOUR real multipliers + an adder per
half — there are NO complex multipliers anywhere on the signal-flow diagram. Every
wire carries a real signal. So the chip chain is, per half:

    m(t) ─┬─ ×cos(w0)  → LowPass ─ ×cos(wc+w0) ─┐
          │                                      (−) → SSB
          └─ ×sin(w0)  → LowPass ─ ×sin(wc+w0) ─┘   (I·cos − Q·sin)

built from: NCO (emits cos on yi + sin on yq), Multiply (m × cos / m × sin),
LowPassFilter, Subtract (the I·cos − Q·sin combine), Gain. NO ComplexMixer, NO
ComplexToFloat, NO complex IQUpconvert — those wrongly modeled a real signal flow
with complex blocks and created a reconvergent I/Q fan-in the router couldn't thread.
The real-block chain is two clean real filaments joined by a subtract.

Verified: the Q15 real-block chain is bit-identical to the proven complex-block
reference (corr 0.9999) — see dev_docs/weaver_real_ref.py.

Weaver plan (USB, fa=1500 Hz audio-band centre, fc=6000 Hz carrier @ 32 kHz):
  TX: NCO(fa) mixers → 2×LowPass → NCO(fc) mixers → Subtract  (= SSB)
  RX: NCO(fc) mixers → 2×LowPass → NCO(fa) mixers → Subtract → Gain×4  (= audio)

Run: <venv>/python examples/ssb_weaver/gen_grc.py   (writes ssb_weaver.grc alongside)
"""
import os

FS = 32000.0
FA = 1500.0        # Weaver audio-band centre
FC = 6000.0        # carrier
CUT = 1200.0       # Weaver LPF cutoff (half the audio bandwidth)
TW = 2500.0        # LPF transition width (tap count)
AMP = 0.9          # NCO oscillator amplitude (Q15)

HDR = """options:
  parameters:
    author: Kyttar
    catch_exceptions: 'True'
    category: '[GRC Hier Blocks]'
    cmake_opt: ''
    comment: ''
    copyright: ''
    description: "FULL on-chip SSB Weaver TRANSCEIVER built from REAL blocks (4 real\\
      \\ multipliers + a subtract per half, exactly the Weaver diagram). audio ->\\
      \\ NCO(fa) mixers -> 2x LowPass -> NCO(fc) mixers -> Subtract [SSB] -> NCO(fc)\\
      \\ mixers -> 2x LowPass -> NCO(fa) mixers -> Subtract -> Gain -> recovered\\
      \\ audio. Imports into placeKYT: two clean real filaments, no complex-block\\
      \\ fan-in. Run as GNURadio Server in placeKYT + Execute here to stream audio."
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
    title: "SSB Weaver transceiver (on-chip, real blocks) — audio in vs recovered"
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


def nco(name, freq, x, y):
    return blk(name, "kyttar_nco", {
        "affinity": "''", "alias": "''", "amplitude": repr(AMP), "comment": "''",
        "device_id": "'\"kyttar_0\"'", "frequency": repr(freq),
        "maxoutbuf": "'0'", "minoutbuf": "'0'", "offset": "'0'", "phase": "'0'",
        "sample_rate": repr(FS), "waveform": '"cos"',
    }, x, y)


def mul(name, x, y):
    return blk(name, "kyttar_multiply", {
        "affinity": "''", "alias": "''", "comment": "''",
        "device_id": "'\"kyttar_0\"'", "maxoutbuf": "'0'", "minoutbuf": "'0'",
        "num_inputs": "'2'",
    }, x, y)


def sub(name, x, y):
    return blk(name, "kyttar_subtract", {
        "affinity": "''", "alias": "''", "comment": "''",
        "device_id": "'\"kyttar_0\"'", "maxoutbuf": "'0'", "minoutbuf": "'0'",
        "num_inputs": "'2'",
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

    # --- chip source: real audio -> x16_in ---
    out.append(blk("msrc", "kyttar_source", {
        "affinity": "''", "alias": "''", "burst_len": "n_samp",
        "comment": "'audio -> chip x16_in (batch)'", "complex_in": "float",
        "device_id": "'\"kyttar_0\"'", "maxoutbuf": "'0'", "minoutbuf": "'0'",
        "num_channels": "'1'", "port_name": "'\"x16_in\"'",
        "server_host": "'\"127.0.0.1\"'", "server_port": "server_port",
        "stream_id": "''",
    }, 560, 200))

    # === TX half: NCO(fa) mixers -> 2x LPF -> NCO(fc) mixers -> Subtract (SSB) ===
    # (oscillators are the SHARED lo_a / lo_c below — the Weaver has 2 frequencies)
    out.append(mul("tx_mi", 900, 140))           # I = m * cos(wa)
    out.append(mul("tx_mq", 900, 300))           # Q = m * sin(wa)
    out.append(lpf("tx_lpi", 1060, 140))
    out.append(lpf("tx_lpq", 1060, 300))
    out.append(mul("tx_ui", 1320, 140))          # uI = I' * cos(wc)
    out.append(mul("tx_uq", 1320, 300))          # uQ = Q' * sin(wc)
    out.append(sub("tx_ssb", 1500, 200))         # SSB = uI - uQ

    # === RX half: NCO(fc) mixers -> 2x LPF -> NCO(fa) mixers -> Subtract ===
    out.append(mul("rx_mi", 1760, 140))
    out.append(mul("rx_mq", 1760, 300))
    out.append(lpf("rx_lpi", 1920, 140))
    out.append(lpf("rx_lpq", 1920, 300))
    out.append(mul("rx_ui", 2180, 140))
    out.append(mul("rx_uq", 2180, 300))
    out.append(sub("rx_aud", 2360, 200))         # recovered audio (pre-gain)

    # === SHARED oscillators — the Weaver uses only TWO distinct frequencies (fa, fc),
    # so ONE fa-NCO + ONE fc-NCO drive ALL the mixers (the 10-cell NCO is heavy; sharing
    # halves the oscillator cost, 4 NCOs -> 2). Each NCO's cos (port 0) fans out to the
    # TWO cos-mixers of that frequency, and sin (port 1) to the two sin-mixers. Placed
    # centrally so the fan-out reaches both TX and RX halves. ===
    out.append(nco("lo_a", FA, 1180, 40))        # fa: TX down-mix + RX up-mix
    out.append(nco("lo_c", FC, 1620, 40))        # fc: TX up-mix + RX down-mix

    # --- x4 Weaver gain (recovered audio scale) ---
    out.append(blk("g4", "kyttar_gain", {
        "affinity": "''", "alias": "''", "comment": "'Weaver 1/4 -> x4'",
        "device_id": "'\"kyttar_0\"'", "gain": "'4'",
        "maxoutbuf": "'0'", "minoutbuf": "'0'",
    }, 2540, 200))

    # --- chip sink: recovered audio <- x16_out ---
    out.append(blk("msink", "kyttar_sink", {
        "affinity": "''", "alias": "''", "comment": "'recovered audio <- x16_out'",
        "device_id": "'\"kyttar_0\"'", "num_channels": "'1'",
        "port_name": "'\"x16_out\"'", "server_host": "'\"127.0.0.1\"'",
        "server_port": "server_port", "stream_id": "''",
    }, 2720, 200))

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
    }, 560, 40))
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
    }, 2720, 40))

    # --- connections ---
    conns = [
        ("tone", 0, "audio", 0), ("tone2", 0, "audio", 1),
        ("audio", 0, "thr", 0),
        ("thr", 0, "msrc", 0),          # audio -> chip source
        ("thr", 0, "in_sink", 0),       # input-audio scope
        ("msrc", 0, "tx_mi", 0),        # x16_in -> I mixer (real)
        ("msrc", 0, "tx_mq", 0),        # x16_in -> Q mixer (real)
        # SHARED fa-NCO: cos (0) -> TX down-mix I AND RX up-mix I; sin (1) -> both Q.
        ("lo_a", 0, "tx_mi", 1),        # fa cos -> TX I mixer
        ("lo_a", 1, "tx_mq", 1),        # fa sin -> TX Q mixer
        ("lo_a", 0, "rx_ui", 1),        # fa cos -> RX up-mix I
        ("lo_a", 1, "rx_uq", 1),        # fa sin -> RX up-mix Q
        # SHARED fc-NCO: cos (0) -> TX up-mix I AND RX down-mix I; sin (1) -> both Q.
        ("lo_c", 0, "tx_ui", 1),        # fc cos -> TX up-mix I
        ("lo_c", 1, "tx_uq", 1),        # fc sin -> TX up-mix Q
        ("lo_c", 0, "rx_mi", 1),        # fc cos -> RX down-mix I
        ("lo_c", 1, "rx_mq", 1),        # fc sin -> RX down-mix Q
        # TX dataflow
        ("tx_mi", 0, "tx_lpi", 0),
        ("tx_mq", 0, "tx_lpq", 0),
        ("tx_lpi", 0, "tx_ui", 0),
        ("tx_lpq", 0, "tx_uq", 0),
        ("tx_ui", 0, "tx_ssb", 0),      # SSB = uI - uQ
        ("tx_uq", 0, "tx_ssb", 1),
        # RX dataflow
        ("tx_ssb", 0, "rx_mi", 0),
        ("tx_ssb", 0, "rx_mq", 0),
        ("rx_mi", 0, "rx_lpi", 0),
        ("rx_mq", 0, "rx_lpq", 0),
        ("rx_lpi", 0, "rx_ui", 0),
        ("rx_lpq", 0, "rx_uq", 0),
        ("rx_ui", 0, "rx_aud", 0),
        ("rx_uq", 0, "rx_aud", 1),
        ("rx_aud", 0, "g4", 0),
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
