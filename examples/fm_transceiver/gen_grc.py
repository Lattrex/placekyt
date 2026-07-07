#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate fm_transceiver.grc — a REAL FM TRANSCEIVER from Kyttar blocks.

This is a TRANSCEIVER, not a loopback: it has a SEPARATE transmit chain and a
SEPARATE receive chain that SHARE ONE chip, demuxed by ``stream_id`` — exactly
the structure of the AM transceiver (am_transceiver.grc) and the BPSK modem
(bpsk_modem.grc).

    TX (modulator, stream 'tx'):
        audio -> tx_src[x16_in] -> frequency_modulator(sens) -> tx_sink[x16_out]
                                                             ==> FM passband (Re part)
    RX (demodulator, stream 'rx'):
        fm_iq -> rx_src[x16_in] -> quadrature_demod(gain) -> rx_sink[x16_out]
                                                             ==> recovered audio

FM is the textbook VCO modulator / quadrature discriminator demodulator:

    TX:  phi += sensitivity*audio;  s = exp(j*phi)   [frequency_modulator, float->cpx]
    RX:  y   = gain*arg(s[n]*conj(s[n-1]))           [quadrature_demod, cpx->float]
         = gain*sensitivity*audio  ->  audio         (with gain = 1/sensitivity)

The RX input burst (``fm_iq``) is the SAME complex FM passband the TX chain emits,
generated in fm_demo_stim from the identical audio + sensitivity, so the RX chain
independently recovers the transmitted audio — a true end-to-end transceiver across
the shared chip.

COMPLEX I/Q the PROVEN way. The FM passband is genuinely complex (both I and Q carry
signal, unlike DSB-AM where Q=0). The RX source streams it INTO the chip with
``complex_in='complex'`` — the interleaved xi/xq path already proven by the coherent
BPSK RX demo. The TX VCO is a complex-output block, so it emits the passband OUT of
x16_out as the I and Q rails interleaved ([I0,Q0,I1,Q1,...] — 256 audio samples ->
512 words). Verified: the I rail tracks cos(phi) and Q tracks sin(phi) at |corr| ~ 1.0.

FABRIC-NATIVE OSCILLATOR (why the VCO, not a free-running NCO). This chip has NO
free-running oscillator: every cell fires only when a neighbour JUMPs it. The
``frequency_modulator`` VCO is INPUT-PACED — each audio sample is BOTH the trigger
AND the phase increment (``phi += sensitivity*x``), so a clean linear filament emits
the FM passband with no carrier fan-out. Verified: the Q15 chain recovers the audio
at |corr| ~ 1.0 (see verification/tests/test_frequency_modulator.py +
test_quadrature_demod.py).

Run: <venv>/python examples/fm_transceiver/gen_grc.py   (writes fm_transceiver.grc)
"""
import math
import os

FS = 32000.0
FDEV = 1500.0                               # FM deviation (Hz)
SENS = 2.0 * math.pi * FDEV / FS            # sensitivity (rad/sample), <= pi
GAIN = 1.0 / SENS                           # discriminator gain = 1/sensitivity

HDR = """options:
  parameters:
    author: Kyttar
    catch_exceptions: 'True'
    category: '[GRC Hier Blocks]'
    cmake_opt: ''
    comment: ''
    copyright: ''
    description: "FM TRANSCEIVER from REAL Kyttar blocks. SEPARATE TX chain\\
      \\ (audio -> frequency_modulator -> FM passband, stream 'tx') and SEPARATE\\
      \\ RX chain (complex FM I/Q -> quadrature_demod -> recovered audio, stream\\
      \\ 'rx') sharing ONE chip by stream_id, like the AM transceiver. Verified\\
      \\ |corr| ~ 1.0. Imports + auto-P&R-routes into placeKYT."
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
    title: "FM transceiver (on-chip, real blocks) — TX modulate + RX demodulate"
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

    # ---- variables + stimulus import --------------------------------------
    out.append(blk("samp_rate", "variable", {"comment": "''", "value": repr(FS)}, 200, 12))
    out.append(blk("n_samp", "variable",
                   {"comment": "audio burst length", "value": "'2048'"}, 320, 12))
    out.append(blk("server_port", "variable",
                   {"comment": "placeKYT GNURadio-server port", "value": "'58950'"},
                   440, 12))
    out.append(blk("import_stim", "import",
                   {"imports": "from gnuradio.kyttar import fm_demo_stim as stim"},
                   560, 12))

    # ==================== TX chain (modulator, stream 'tx') ====================
    # audio -> tx_src[x16_in,'tx'] -> tx_vco(frequency_modulator) -> tx_sink[x16_out,'tx']
    out.append(blk("tx_audio", "blocks_vector_source_x", {
        "affinity": "''", "alias": "''",
        "comment": "TX audio (two tones) -> chip (stream 'tx'); finite, no repeat",
        "maxoutbuf": "'0'", "minoutbuf": "'0'", "type": "float",
        "vector": "stim.tx_audio(n_samp)", "repeat": "'False'"}, 40, 120))
    out.append(blk("tx_src", "kyttar_source", {
        "affinity": "''", "alias": "''", "burst_len": "n_samp",
        "comment": "TX audio -> chip x16_in (shared duplex port, stream 'tx')",
        "complex_in": "float", "device_id": "'\"kyttar_0\"'", "maxoutbuf": "'0'",
        "minoutbuf": "'0'", "num_channels": "'1'", "port_name": "'\"x16_in\"'",
        "server_host": "'\"127.0.0.1\"'", "server_port": "server_port",
        "stream_id": "'\"tx\"'"}, 240, 120))
    out.append(blk("tx_vco", "kyttar_frequency_modulator", {
        "affinity": "''", "alias": "''",
        "comment": "'FM modulate: phi += sensitivity*audio; out = exp(j*phi)'",
        "device_id": "'\"kyttar_0\"'", "sensitivity": repr(SENS),
        "maxoutbuf": "'0'", "minoutbuf": "'0'"}, 440, 120))
    out.append(blk("tx_sink", "kyttar_sink", {
        "affinity": "''", "alias": "''",
        "comment": "FM passband (I/Q interleaved) <- chip x16_out (stream 'tx')",
        "device_id": "'\"kyttar_0\"'", "in_type": "complex", "num_channels": "'1'",
        "port_name": "'\"x16_out\"'", "server_host": "'\"127.0.0.1\"'",
        "server_port": "server_port", "stream_id": "'\"tx\"'"}, 640, 120))
    out.append(blk("tx_passband", "qtgui_time_sink_x", _timesink(
        "'FM passband (TX)'", "'blue'", "stim.points(n_samp)"), 840, 40))

    # ==================== RX chain (demodulator, stream 'rx') ====================
    # fm_iq -> rx_src[x16_in,'rx',COMPLEX] -> rx_demod(quadrature_demod) -> rx_sink
    out.append(blk("fm_rf", "blocks_vector_source_x", {
        "affinity": "''", "alias": "''",
        "comment": "'RX input: the complex FM passband (same audio+sensitivity); I/Q, no repeat'",
        "maxoutbuf": "'0'", "minoutbuf": "'0'", "type": "complex",
        "vector": "stim.fm_iq(n_samp)", "repeat": "'False'"}, 40, 320))
    out.append(blk("rx_src", "kyttar_source", {
        "affinity": "''", "alias": "''", "burst_len": "n_samp",
        "comment": "complex FM I/Q -> chip x16_in (shared duplex port, stream 'rx')",
        "complex_in": "complex", "device_id": "'\"kyttar_0\"'", "maxoutbuf": "'0'",
        "minoutbuf": "'0'", "num_channels": "'1'", "port_name": "'\"x16_in\"'",
        "server_host": "'\"127.0.0.1\"'", "server_port": "server_port",
        "stream_id": "'\"rx\"'"}, 240, 320))
    out.append(blk("rx_demod", "kyttar_quadrature_demod", {
        "affinity": "''", "alias": "''",
        "comment": "'FM demod: y = gain*arg(x[n]*conj(x[n-1]))'",
        "device_id": "'\"kyttar_0\"'", "gain": repr(GAIN),
        "maxoutbuf": "'0'", "minoutbuf": "'0'"}, 440, 320))
    out.append(blk("rx_sink", "kyttar_sink", {
        "affinity": "''", "alias": "''",
        "comment": "recovered audio <- chip x16_out (stream 'rx')",
        "device_id": "'\"kyttar_0\"'", "num_channels": "'1'",
        "port_name": "'\"x16_out\"'", "server_host": "'\"127.0.0.1\"'",
        "server_port": "server_port", "stream_id": "'\"rx\"'"}, 640, 320))
    out.append(blk("rx_audio", "qtgui_time_sink_x", _timesink(
        "'recovered audio (RX)'", "'green'", "stim.points(n_samp)"), 840, 240))

    conns = [
        # TX chain: audio -> frequency_modulator -> FM passband
        ("tx_audio", 0, "tx_src", 0),
        ("tx_src", 0, "tx_vco", 0),
        ("tx_vco", 0, "tx_sink", 0),
        ("tx_sink", 0, "tx_passband", 0),
        # RX chain: complex FM I/Q -> quadrature_demod -> recovered audio
        ("fm_rf", 0, "rx_src", 0),
        ("rx_src", 0, "rx_demod", 0),
        ("rx_demod", 0, "rx_sink", 0),
        ("rx_sink", 0, "rx_audio", 0),
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


def _timesink(label, col, size):
    return {
        "affinity": "''", "alias": "''", "autoscale": "'True'",
        "axislabels": "'True'", "bw": "samp_rate", "comment": "''",
        "ctrlpanel": "'False'", "entags": "'True'", "grid": "'True'",
        "gui_hint": "''", "label1": label, "legend": "'True'", "marker1": "'-1'",
        "name": label, "nconnections": "'1'", "size": size,
        "srate": "samp_rate", "stemplot": "'False'", "style1": "'1'",
        "tr_chan": "'0'", "tr_delay": "'0'", "tr_level": "'0'",
        "tr_mode": "qtgui.TRIG_MODE_FREE", "tr_slope": "qtgui.TRIG_SLOPE_POS",
        "tr_tag": "''", "type": "float", "update_time": "'0.10'", "width1": "'1'",
        "ylabel": "'Amplitude'", "ymax": "'1'", "ymin": "'-1'", "yunit": "''",
        "grid_color1": col, "alpha1": "'1.0'"}


if __name__ == "__main__":
    main()
