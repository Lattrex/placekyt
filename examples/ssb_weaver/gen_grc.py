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


def cmix(name, freq, x, y):
    """A fused oscillator-mixer (kyttar_complex_mixer, 11 cells): real signal in (xi; xq
    defaults 0) -> yi = signal*cos(θ), yq = signal*sin(θ), θ += freq. Its TWO outputs ARE
    both Weaver rails (cos=yi, sin=yq), each to a distinct consumer — so ONE cmix replaces
    a whole {shared NCO + 2 Multiply} DOWN-mix cluster with no dead NCO / no carrier
    fan-out.  Used for the two DOWN-mixes (which need BOTH rails)."""
    return blk(name, "kyttar_complex_mixer", {
        "affinity": "''", "alias": "''", "comment": "''",
        "device_id": "'\"kyttar_0\"'", "frequency": repr(freq),
        "maxoutbuf": "'0'", "minoutbuf": "'0'", "sample_rate": repr(FS),
    }, x, y)


def iqup(name, freq, x, y):
    """A LEAN fused oscillator-mixer (kyttar_iq_upconvert, 6 cells): out = xi·cos − xq·sin.
    Used for the UP-mixes, which need only ONE rail each:
      * feed signal on xi -> out = sig·cos   (the COS rail)
      * feed signal on xq -> out = −sig·sin  (the −SIN rail)
    6 cells vs the ComplexMixer's 11 — this is what makes the Weaver FIT one chip (80 cells
    vs 100). The −sin means the final Weaver combine is an ADD, not a subtract."""
    return blk(name, "kyttar_iq_upconvert", {
        "affinity": "''", "alias": "''", "comment": "''",
        "device_id": "'\"kyttar_0\"'", "frequency": repr(freq),
        "maxoutbuf": "'0'", "minoutbuf": "'0'", "sample_rate": repr(FS),
    }, x, y)


def add(name, x, y):
    return blk(name, "kyttar_add", {
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

    # === FUSED-OSCILLATOR WEAVER (no shared NCO, no carrier fan-out) ===
    # This chip is clockless: a standalone NCO drawn as a source gets no trigger and is
    # DEAD (see dev_docs/OSCILLATOR_TOPOLOGY_ANALYSIS.md). So each mixer carries its OWN
    # oscillator. kyttar_complex_mixer emits BOTH rails (yi=sig*cos, yq=sig*sin) as two
    # separate output ports, so the DOWN-mix (which needs both rails from one signal) is
    # ONE mixer; each UP-mix (which needs one rail from one signal) is one mixer using a
    # single output. All fa-mixers start phase 0 at sample 0 (coherent); likewise all
    # fc-mixers. Replicating the cheap phase-accumulator per mixer costs cells (plentiful)
    # but removes the fan-out (wires, scarce) — the fabric-native trade.

    # === TX half: cmix(fa) [both rails] -> 2x LPF -> iqup(fc) x2 [one rail each] -> Add ===
    # Down-mix = 11-cell ComplexMixer (needs both rails).  Up-mixes = lean 6-cell
    # IQUpconvert (one rail each): xi->cos rail, xq->(-sin) rail.  Because the sin rail is
    # NEGATED, the Weaver combine is an ADD (uI + (-Q'sin)), not a subtract.
    out.append(cmix("tx_ma", FA, 900, 200))      # audio -> yi=a*cos(fa), yq=a*sin(fa)
    out.append(lpf("tx_lpi", 1060, 140))         # I' = LPF(a*cos)
    out.append(lpf("tx_lpq", 1060, 300))         # Q' = LPF(a*sin)
    out.append(iqup("tx_uic", FC, 1320, 140))    # uI  = I' * cos(fc)   (xi in)
    out.append(iqup("tx_uqc", FC, 1320, 300))    # -uQ = -Q' * sin(fc)  (xq in)
    out.append(add("tx_ssb", 1500, 200))         # SSB = uI + (-Q'sin)

    # === RX half: cmix(fc) [both rails] -> 2x LPF -> iqup(fa) x2 [one rail each] -> Add ==
    out.append(cmix("rx_mc", FC, 1760, 200))     # ssb -> yi=ssb*cos(fc), yq=ssb*sin(fc)
    out.append(lpf("rx_lpi", 1920, 140))
    out.append(lpf("rx_lpq", 1920, 300))
    out.append(iqup("rx_uia", FA, 2180, 140))    # rI  = I' * cos(fa)   (xi in)
    out.append(iqup("rx_uqa", FA, 2180, 300))    # -rQ = -Q' * sin(fa)  (xq in)
    out.append(add("rx_aud", 2360, 200))         # recovered audio (pre-gain)

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
        # --- TX: down-mix cmix(fa) emits BOTH rails (yi=port0 cos, yq=port1 sin) ---
        ("msrc", 0, "tx_ma", 0),        # audio -> fused fa-mixer (xi)
        ("tx_ma", 0, "tx_lpi", 0),      # yi = a*cos(fa) -> LPF I
        ("tx_ma", 1, "tx_lpq", 0),      # yq = a*sin(fa) -> LPF Q
        ("tx_lpi", 0, "tx_uic", 0),     # I' -> fc cos-mixer xi  -> uI = I'*cos(fc)
        ("tx_lpq", 0, "tx_uqc", 1),     # Q' -> fc sin-mixer xq  -> -Q'*sin(fc)
        ("tx_uic", 0, "tx_ssb", 0),     # uI
        ("tx_uqc", 0, "tx_ssb", 1),     # -Q'*sin  ;  SSB = uI + (-Q'sin)  (ADD)
        # --- RX: down-mix cmix(fc) emits BOTH rails ---
        ("tx_ssb", 0, "rx_mc", 0),      # ssb -> fused fc-mixer
        ("rx_mc", 0, "rx_lpi", 0),      # yi = ssb*cos(fc) -> LPF I
        ("rx_mc", 1, "rx_lpq", 0),      # yq = ssb*sin(fc) -> LPF Q
        ("rx_lpi", 0, "rx_uia", 0),     # I' -> fa cos-mixer xi  -> rI = I'*cos(fa)
        ("rx_lpq", 0, "rx_uqa", 1),     # Q' -> fa sin-mixer xq  -> -Q'*sin(fa)
        ("rx_uia", 0, "rx_aud", 0),     # rI
        ("rx_uqa", 0, "rx_aud", 1),     # -Q'*sin  ;  audio = rI + (-Q'sin)  (ADD)
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
