#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate am_transceiver.grc — a REAL DSB-AM TRANSCEIVER from Kyttar blocks.

This is a TRANSCEIVER, not a loopback: it has a SEPARATE transmit chain and a
SEPARATE receive chain that SHARE ONE chip, demuxed by ``stream_id`` — exactly
the structure of the BPSK modem example (bpsk_modem.grc).

    TX (modulator, stream 'tx'):
        audio -> tx_src[x16_in] -> oscMix(fc) -> tx_sink[x16_out]  ==> AM passband
    RX (demodulator, stream 'rx'):
        am_rf -> rx_src[x16_in] -> oscMix(fc) -> LowPass -> Gain x2 -> rx_sink[x16_out]
                                                                      ==> recovered audio

DSB-AM (suppressed-carrier, coherent) is the textbook product modulator / product
detector:

    TX:  s = audio * cos(wc t)                      [oscillator-mixer @ fc]
    RX:  y = s * cos(wc t) -> LowPass -> x2         [oscillator-mixer @ fc, LPF, Gain]
         = audio*cos^2 = audio*(1+cos 2wc)/2 --LPF--> audio/2 --x2--> audio

The RX input burst (``am_rf``) is the SAME passband the TX chain emits, generated
in am_demo_stim from the identical audio + carrier, so the RX chain independently
recovers the transmitted audio — a true end-to-end transceiver across the shared
chip.

FABRIC-NATIVE OSCILLATOR (why oscillator-mixers, not a shared NCO).  This chip has
NO free-running oscillator: every cell fires only when a neighbour JUMPs it. A GNU
Radio NCO drawn as a source has NO input trigger and is DEAD on-chip. The fix:
FUSE the oscillator INTO the mixer. ``kyttar_iq_upconvert`` takes a REAL input
(BOTH the trigger AND the data) and multiplies by its OWN internal cos:
``out = xi*cos(theta); theta += freq_word``. Each audio/passband sample triggers
its own carrier step — a clean linear filament, no carrier fan-out.

Both mixers run the SAME fc oscillator started at phase 0 from sample 0, so TX and
RX carriers are coherent (product detection works). Verified: the Q15 chain
recovers the audio at |corr| ~ 1.0.

Run: <venv>/python examples/am_transceiver/gen_grc.py   (writes am_transceiver.grc)
"""
import os

FS = 32000.0
FC = 6000.0        # AM carrier
CUT = 3000.0       # RX low-pass cutoff (recover baseband, reject 2*fc)
TW = 2000.0

HDR = """options:
  parameters:
    author: Kyttar
    catch_exceptions: 'True'
    category: '[GRC Hier Blocks]'
    cmake_opt: ''
    comment: ''
    copyright: ''
    description: "DSB-AM TRANSCEIVER from REAL Kyttar blocks. SEPARATE TX chain\\
      \\ (audio -> oscMix(fc) -> AM passband, stream 'tx') and SEPARATE RX chain\\
      \\ (AM passband -> oscMix(fc) -> LowPass -> Gain -> recovered audio, stream\\
      \\ 'rx') sharing ONE chip by stream_id, like the BPSK modem. Verified\\
      \\ |corr| ~ 1.0. Imports + auto-P&R-routes into placeKYT."
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
    title: "DSB-AM transceiver (on-chip, real blocks) — TX modulate + RX demodulate"
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


def oscmix(name, comment, x, y):
    """A fused oscillator-mixer @ fc (kyttar_iq_upconvert): real in -> in*cos(theta)."""
    return blk(name, "kyttar_iq_upconvert", {
        "affinity": "''", "alias": "''", "comment": comment,
        "device_id": "'\"kyttar_0\"'", "frequency": repr(FC), "maxoutbuf": "'0'",
        "minoutbuf": "'0'", "sample_rate": repr(FS)}, x, y)


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
                   {"imports": "from gnuradio.kyttar import am_demo_stim as stim"},
                   560, 12))

    # ==================== TX chain (modulator, stream 'tx') ====================
    # audio -> tx_src[x16_in,'tx'] -> tx_mix(oscMix fc) -> tx_sink[x16_out,'tx']
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
    out.append(oscmix("tx_mix", "'AM modulate: s = audio*cos(fc)'", 440, 120))
    out.append(blk("tx_sink", "kyttar_sink", {
        "affinity": "''", "alias": "''",
        "comment": "AM passband <- chip x16_out (stream 'tx')",
        "device_id": "'\"kyttar_0\"'", "num_channels": "'1'",
        "port_name": "'\"x16_out\"'", "server_host": "'\"127.0.0.1\"'",
        "server_port": "server_port", "stream_id": "'\"tx\"'"}, 640, 120))
    out.append(blk("tx_passband", "qtgui_time_sink_x", _timesink(
        "'AM passband (TX)'", "'blue'", "stim.points(n_samp)"), 840, 40))

    # ==================== RX chain (demodulator, stream 'rx') ====================
    # am_rf -> rx_src[x16_in,'rx'] -> rx_mix(oscMix fc) -> rx_lpf -> rx_gain
    #          -> rx_sink[x16_out,'rx']
    out.append(blk("am_rf", "blocks_vector_source_x", {
        "affinity": "''", "alias": "''",
        "comment": "'RX input: the AM passband (same audio+carrier); finite, no repeat'",
        "maxoutbuf": "'0'", "minoutbuf": "'0'", "type": "float",
        "vector": "stim.am_passband(n_samp)", "repeat": "'False'"}, 40, 320))
    out.append(blk("rx_src", "kyttar_source", {
        "affinity": "''", "alias": "''", "burst_len": "n_samp",
        "comment": "AM passband -> chip x16_in (shared duplex port, stream 'rx')",
        "complex_in": "float", "device_id": "'\"kyttar_0\"'", "maxoutbuf": "'0'",
        "minoutbuf": "'0'", "num_channels": "'1'", "port_name": "'\"x16_in\"'",
        "server_host": "'\"127.0.0.1\"'", "server_port": "server_port",
        "stream_id": "'\"rx\"'"}, 240, 320))
    out.append(oscmix("rx_mix", "'AM demod: y = s*cos(fc) (coherent product detect)'",
                      440, 320))
    out.append(blk("rx_lpf", "kyttar_low_pass_filter", {
        "affinity": "''", "alias": "''", "beta": "'6.76'",
        "comment": "'recover baseband, reject 2*fc'",
        "cutoff_freq": repr(CUT), "decimation": "'1'", "device_id": "'\"kyttar_0\"'",
        "gain": "'1'", "interpolation": "'1'", "maxoutbuf": "'0'", "minoutbuf": "'0'",
        "samp_rate": repr(FS), "transition_width": repr(TW), "window": '"hamming"'},
        640, 320))
    out.append(blk("rx_gain", "kyttar_gain", {
        "affinity": "''", "alias": "''", "comment": "'DSB 1/2 -> x2'",
        "device_id": "'\"kyttar_0\"'", "gain": "'2'", "maxoutbuf": "'0'",
        "minoutbuf": "'0'"}, 840, 320))
    out.append(blk("rx_sink", "kyttar_sink", {
        "affinity": "''", "alias": "''",
        "comment": "recovered audio <- chip x16_out (stream 'rx')",
        "device_id": "'\"kyttar_0\"'", "num_channels": "'1'",
        "port_name": "'\"x16_out\"'", "server_host": "'\"127.0.0.1\"'",
        "server_port": "server_port", "stream_id": "'\"rx\"'"}, 1040, 320))
    out.append(blk("rx_audio", "qtgui_time_sink_x", _timesink(
        "'recovered audio (RX)'", "'green'", "stim.points(n_samp)"), 1240, 240))

    conns = [
        # TX chain
        ("tx_audio", 0, "tx_src", 0),
        ("tx_src", 0, "tx_mix", 0),
        ("tx_mix", 0, "tx_sink", 0),
        ("tx_sink", 0, "tx_passband", 0),
        # RX chain
        ("am_rf", 0, "rx_src", 0),
        ("rx_src", 0, "rx_mix", 0),
        ("rx_mix", 0, "rx_lpf", 0),
        ("rx_lpf", 0, "rx_gain", 0),
        ("rx_gain", 0, "rx_sink", 0),
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
                        "am_transceiver.grc")
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
