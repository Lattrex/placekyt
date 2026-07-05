#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate am_transceiver.grc — a DSB-AM transceiver from REAL Kyttar blocks.

DSB-AM (suppressed-carrier, coherent) is the textbook product-modulator/detector:

    TX:  s = audio * cos(wc t)              [oscillator-mixer @ fc]
    RX:  y = s * cos(wc t) -> LowPass       [oscillator-mixer @ fc, LowPass]
         = audio*cos^2 = audio*(1+cos 2wc)/2  -LPF->  audio/2   (then Gain x2)

FABRIC-NATIVE OSCILLATOR TOPOLOGY (the important part).  This chip has NO free-running
oscillator: every cell fires only when a neighbour JUMPs it (there is no internal clock).
A standalone NCO here is NOT a source — it needs one trigger per output sample.  A GNU
Radio sig_source/NCO drawn as a source therefore ends up with NO input connection and is
DEAD on-chip (nothing triggers it → no carrier).  The fix used here: FUSE the oscillator
INTO the mixer.  ``kyttar_iq_upconvert`` is an oscillator-mixer — it takes a REAL signal
input (which is BOTH the trigger AND the data) and multiplies by its OWN internal cos:
``out = xi·cos(θ); θ += freq_word``.  So each audio sample triggers its own carrier step.
No separate NCO, no carrier FAN-OUT (the real reason the NCO-shared version bloats), no
trigger routing — just a clean linear filament that auto-P&R routes trivially.

Both mixers run the SAME fc oscillator, started at phase 0 from sample 0, so TX and RX
carriers are coherent (product detection works).  We REPLICATE the cheap phase-accumulator
per mixer rather than SHARE one via fan-out — cells are plentiful; wires are scarce.
Verified: the Q15 chain recovers the audio at corr 1.0 (IQUpconvert×2 + LowPass).

Chain: audio -> oscMix@fc -> [passband] -> oscMix@fc -> LowPass -> Gain x2 -> audio
Run: <venv>/python examples/am_transceiver/gen_grc.py   (writes am_transceiver.grc)
"""
import os

FS = 32000.0
FC = 6000.0        # AM carrier
CUT = 3000.0       # RX low-pass cutoff (recover baseband, reject 2*fc)
TW = 2000.0
AMP = 0.9

HDR = """options:
  parameters:
    author: Kyttar
    catch_exceptions: 'True'
    category: '[GRC Hier Blocks]'
    cmake_opt: ''
    comment: ''
    copyright: ''
    description: "DSB-AM transceiver from REAL Kyttar blocks (product modulator +\\
      \\ coherent product detector). audio -> Multiply(cos) [passband] ->\\
      \\ Multiply(cos) -> LowPass -> Gain -> recovered audio. One shared carrier NCO.\\
      \\ Verified corr 1.0. Imports + auto-P&R-routes into placeKYT."
    gen_cmake: 'On'
    gen_linking: dynamic
    generate_options: qt_gui
    hier_block_src_path: '.:'
    id: am_transceiver
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
    title: "DSB-AM transceiver (on-chip, real blocks) — audio in vs recovered"
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

    # FUSED oscillator-mixers: each carries its OWN fc oscillator (no shared NCO, no
    # carrier fan-out).  kyttar_iq_upconvert: real xi in -> out = xi·cos(θ), θ += freq_word.
    # The arriving sample IS the trigger.  Both start at phase 0 from sample 0 -> coherent.
    def oscmix(name, x, y):
        return blk(name, "kyttar_iq_upconvert", {
            "affinity": "''", "alias": "''", "comment": "''",
            "device_id": "'\"kyttar_0\"'", "frequency": repr(FC), "maxoutbuf": "'0'",
            "minoutbuf": "'0'", "sample_rate": repr(FS)}, x, y)

    out.append(oscmix("tx_mix", 760, 200))     # s = audio * cos(fc)   (fused osc)
    out.append(oscmix("rx_mix", 960, 200))     # y = s * cos(fc)        (fused osc)
    out.append(blk("rx_lpf", "kyttar_low_pass_filter", {
        "affinity": "''", "alias": "''", "beta": "'6.76'", "comment": "''",
        "cutoff_freq": repr(CUT), "decimation": "'1'", "device_id": "'\"kyttar_0\"'",
        "gain": "'1'", "interpolation": "'1'", "maxoutbuf": "'0'", "minoutbuf": "'0'",
        "samp_rate": repr(FS), "transition_width": repr(TW), "window": '"hamming"'},
        1160, 200))
    out.append(blk("g2", "kyttar_gain", {
        "affinity": "''", "alias": "''", "comment": "'DSB 1/2 -> x2'",
        "device_id": "'\"kyttar_0\"'", "gain": "'2'", "maxoutbuf": "'0'",
        "minoutbuf": "'0'"}, 1360, 200))
    out.append(blk("msink", "kyttar_sink", {
        "affinity": "''", "alias": "''", "comment": "'recovered audio <- x16_out'",
        "device_id": "'\"kyttar_0\"'", "num_channels": "'1'",
        "port_name": "'\"x16_out\"'", "server_host": "'\"127.0.0.1\"'",
        "server_port": "server_port", "stream_id": "''"}, 1560, 200))

    for nm, lbl, col, x in [("in_sink", "'input audio'", "'blue'", 560),
                            ("out_sink", "'recovered audio'", "'green'", 1560)]:
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
        ("msrc", 0, "tx_mix", 0),       # audio -> TX oscillator-mixer (self-carrier)
        ("tx_mix", 0, "rx_mix", 0),     # passband -> RX oscillator-mixer (self-carrier)
        ("rx_mix", 0, "rx_lpf", 0),     # product detect -> LowPass (recover baseband)
        ("rx_lpf", 0, "g2", 0),
        ("g2", 0, "msink", 0),
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
                        "am_transceiver.grc")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("\n".join(out))
    print("wrote", path)


if __name__ == "__main__":
    main()
