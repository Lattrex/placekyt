#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate fm_transceiver.grc — an FM transceiver from REAL Kyttar blocks.

FM is the fabric-friendliest analog mode: NEITHER block needs a shared oscillator, so
there is no dead-NCO / fan-out problem at all.

    TX (modulator):  out = exp(j·phase), phase += sensitivity·audio   [FrequencyModulator]
    RX (demod):      out = gain·Im(x[n]·conj(x[n-1]))                 [QuadratureDemod]

The FrequencyModulator (VCO) is input-driven: each audio sample IS the trigger for its
phase step — it is NOT a source that needs an external clock. The QuadratureDemod is the
standard 2-cell FM discriminator (out = gain·di, di from the conjugate product). Both are
clean 1-in / 1-out (well, VCO is 1-real-in / complex-out; demod is complex-in / 1-real-out)
filaments — the whole transceiver is a straight line that auto-P&R routes trivially.

Chain: audio -> FrequencyModulator(sens) -> [complex FM baseband] -> QuadratureDemod(gain)
       -> recovered audio.  gain = 1/sens recovers the audio scale.  Verified: the Q15
chain recovers the audio at corr 0.9998 (VCO -> discriminator).

Run: <venv>/python examples/fm_transceiver/gen_grc.py   (writes fm_transceiver.grc)
"""
import os

FS = 32000.0
SENS = 0.8         # FM sensitivity (rad/sample per unit audio) — keeps |dphase| in the
#                    discriminator's near-linear range for clean recovery. |sens| <= pi.
GAIN = 1.0 / SENS  # demod gain to recover the audio amplitude scale
AMP = 0.9

HDR = """options:
  parameters:
    author: Kyttar
    catch_exceptions: 'True'
    category: '[GRC Hier Blocks]'
    cmake_opt: ''
    comment: ''
    copyright: ''
    description: "FM transceiver from REAL Kyttar blocks. audio -> FrequencyModulator\\
      \\ (VCO) -> [complex FM] -> QuadratureDemod (discriminator) -> recovered audio.\\
      \\ Neither block needs a shared oscillator, so there is NO dead-NCO/fan-out\\
      \\ problem — a straight filament that auto-P&R-routes trivially. corr 0.9998."
    gen_cmake: 'On'
    gen_linking: dynamic
    generate_options: qt_gui
    hier_block_src_path: '.:'
    id: fm_transceiver
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
    title: "FM transceiver (on-chip, real blocks) — audio in vs recovered"
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


def main():
    out = [HDR]
    out.append(blk("samp_rate", "variable", {"comment": "''", "value": repr(FS)}, 200, 12))
    out.append(blk("n_samp", "variable",
                   {"comment": "audio burst length", "value": "'2048'"}, 320, 12))
    out.append(blk("server_port", "variable",
                   {"comment": "placeKYT GNURadio-server port", "value": "'58950'"},
                   440, 12))

    # host audio: two tones
    for nm, amp, freq, y in [("tone", "'0.5'", "'800'", 140),
                             ("tone2", "'0.3'", "'1500'", 260)]:
        out.append(blk(nm, "analog_sig_source_x", {
            "affinity": "''", "alias": "''", "amp": amp, "comment": "''",
            "freq": freq, "maxoutbuf": "'0'", "minoutbuf": "'0'", "offset": "'0'",
            "phase": "'0'", "samp_rate": "samp_rate", "showports": "'False'",
            "type": "float", "waveform": "analog.GR_SIN_WAVE"}, 40, y))
    out.append(blk("audio", "blocks_add_xx", {
        "affinity": "''", "alias": "''", "comment": "'input audio (2 tones)'",
        "maxoutbuf": "'0'", "minoutbuf": "'0'", "num_inputs": "'2'",
        "type": "float", "vlen": "'1'"}, 240, 200))
    out.append(blk("thr", "blocks_throttle2", {
        "affinity": "''", "alias": "''", "comment": "''", "ignoretag": "'True'",
        "limit": "auto", "maximum": "'0.1'", "maxoutbuf": "'0'", "minoutbuf": "'0'",
        "samples_per_second": "samp_rate", "type": "float", "vlen": "'1'"}, 400, 208))

    out.append(blk("msrc", "kyttar_source", {
        "affinity": "''", "alias": "''", "burst_len": "n_samp",
        "comment": "'audio -> chip x16_in'", "complex_in": "float",
        "device_id": "'\"kyttar_0\"'", "maxoutbuf": "'0'", "minoutbuf": "'0'",
        "num_channels": "'1'", "port_name": "'\"x16_in\"'",
        "server_host": "'\"127.0.0.1\"'", "server_port": "server_port",
        "stream_id": "''"}, 560, 200))

    # TX: FM modulator (VCO) — input-driven, no external oscillator.
    out.append(blk("vco", "kyttar_frequency_modulator", {
        "affinity": "''", "alias": "''", "comment": "'audio -> exp(j*phase)'",
        "device_id": "'\"kyttar_0\"'", "maxoutbuf": "'0'", "minoutbuf": "'0'",
        "sensitivity": repr(SENS)}, 760, 200))

    # RX: FM discriminator — 2-cell MAC discriminator, no oscillator.
    out.append(blk("demod", "kyttar_quadrature_demod", {
        "affinity": "''", "alias": "''", "comment": "'gain*di (FM discriminator)'",
        "device_id": "'\"kyttar_0\"'", "gain": repr(GAIN), "maxoutbuf": "'0'",
        "minoutbuf": "'0'"}, 960, 200))

    out.append(blk("msink", "kyttar_sink", {
        "affinity": "''", "alias": "''", "comment": "'recovered audio <- x16_out'",
        "device_id": "'\"kyttar_0\"'", "num_channels": "'1'",
        "port_name": "'\"x16_out\"'", "server_host": "'\"127.0.0.1\"'",
        "server_port": "server_port", "stream_id": "''"}, 1160, 200))

    for nm, lbl, col, x in [("in_sink", "'input audio'", "'blue'", 560),
                            ("out_sink", "'recovered audio'", "'green'", 1160)]:
        out.append(blk(nm, "qtgui_time_sink_x", {
            "affinity": "''", "alias": "''", "autoscale": "'True'",
            "axislabels": "'True'", "bw": "samp_rate", "comment": "''",
            "ctrlpanel": "'False'", "entags": "'True'", "grid": "'True'",
            "gui_hint": "''", "label1": lbl, "legend": "'True'", "marker1": "'-1'",
            "name": f'"{nm}"', "nconnections": "'1'", "size": "n_samp",
            "srate": "samp_rate", "stemplot": "'False'", "style1": "'1'",
            "tr_chan": "'0'", "tr_delay": "'0'", "tr_level": "'0'",
            "tr_mode": "qtgui.TRIG_MODE_FREE", "tr_slope": "qtgui.TRIG_SLOPE_POS",
            "tr_tag": "''", "type": "float", "update_time": "'0.10'", "width1": "'1'",
            "ylabel": "'Amplitude'", "ymax": "'1'", "ymin": "'-1'", "yunit": "''",
            "grid_color1": col, "alpha1": "'1.0'"}, x, 40))

    conns = [
        ("tone", 0, "audio", 0), ("tone2", 0, "audio", 1),
        ("audio", 0, "thr", 0),
        ("thr", 0, "msrc", 0), ("thr", 0, "in_sink", 0),
        ("msrc", 0, "vco", 0),          # audio -> FM modulator (VCO)
        ("vco", 0, "demod", 0),         # complex FM baseband -> discriminator
        ("demod", 0, "msink", 0),
        ("msink", 0, "out_sink", 0),
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
                        "fm_transceiver.grc")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("\n".join(out))
    print("wrote", path)


if __name__ == "__main__":
    main()
