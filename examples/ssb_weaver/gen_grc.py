#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate ssb_weaver.grc — the FULL on-chip SSB Weaver transceiver flowgraph on
the COMPLEX-FIR datapath (the topology that fits ONE 10x12 die).

The Weaver ("third method") SSB, per half, is a down-mix into a complex baseband, a
low-pass on BOTH rails, and an up-mix that recombines (I·cos − Q·sin). On the fabric
that is THREE blocks — no split, no fan-in:

    audio ─ ComplexMixer(fa) ─ ComplexLowPass ─ IQUpconvert(fc) ─ SSB
             (complex packet)   (complex in/out)  (I'·cos − Q'·sin)

ComplexLowPass (kyttar_complex_low_pass_filter = GNU Radio fir_filter_ccf) filters
both I/Q rails with ONE shared tap set, so the classic Weaver's complex→2-real-LPF
FAN-OUT and 2-real→1 recombine FAN-IN both disappear. Each half is a straight
complex filament; every hop is a same-source complex packet the placeKYT importer
expands into its I/Q rail pair. Result: the WHOLE 6-block transceiver auto-places,
routes and builds on ONE 10x12 chip (78/120 cells, all 11 nets) — the 10-block
real-rail Weaver could not. See examples/ssb_weaver/weaver_builder_cfir.py.

Verified: the Q15 complex-FIR chain recovers the audio at corr 0.986 on the real
substrate (== the Q15 reference at 0.9999) — see verification/tests/test_ssb_weaver_cfir.py.
The complex FIR is bit-exact to fir_filter_ccf (verification/tests/test_complex_fir.py).

Weaver plan (USB, fa=1500 Hz audio-band centre, fc=6000 Hz carrier @ 32 kHz):
  TX: ComplexMixer(fa) → ComplexLowPass → IQUpconvert(fc)          (= SSB)
  RX: ComplexMixer(fc) → ComplexLowPass → IQUpconvert(fa) → Gain×4 (= audio)

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


def clpf(name, x, y):
    """A COMPLEX low-pass filter (kyttar_complex_low_pass_filter): ONE block that
    filters BOTH I/Q rails with the SAME firdes.low_pass taps (= fir_filter_ccf).
    gain=0.9 keeps Sum|h|<=1 so the multi-cell filter fits the cell budget."""
    return blk(name, "kyttar_complex_low_pass_filter", {
        "affinity": "''", "alias": "''", "beta": "'6.76'", "comment": "''",
        "cutoff_freq": repr(CUT), "device_id": "'\"kyttar_0\"'", "gain": "'0.9'",
        "maxoutbuf": "'0'", "minoutbuf": "'0'", "samp_rate": "samp_rate",
        "transition_width": repr(TW), "window": "'\"hamming\"'",
    }, x, y)


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


def cmix(name, freq, phase, x, y):
    """A fused oscillator down-mixer (kyttar_complex_mixer, 11 cells): real signal in
    (xi; xq defaults 0) -> yi = signal*cos(θ), yq = signal*sin(θ). ``freq`` is the
    Weaver LO (negative for a down-shift), ``phase`` the calibrated initial phase
    that pre-rotates the carrier to track the causal-FIR envelope group delay (so
    Weaver image cancellation holds) — see weaver_builder_cfir.calibrate_phase_steps_cfir."""
    return blk(name, "kyttar_complex_mixer", {
        "affinity": "''", "alias": "''", "amplitude": "'1.0'", "comment": "''",
        "device_id": "'\"kyttar_0\"'", "frequency": repr(freq), "offset": "'0.0'",
        "maxoutbuf": "'0'", "minoutbuf": "'0'", "phase": repr(phase),
        "sample_rate": repr(FS),
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


def _weaver_phases():
    """The calibrated ComplexMixer initial phases (radians) for the two down-mixes,
    computed by the PROVEN weaver_builder_cfir calibration against the Q15 reference
    chain. These pre-rotate each carrier to track the causal complex-FIR envelope
    group delay so Weaver image cancellation holds (corr ~0.986). Baked into the .grc
    so the flowgraph recovers audio when opened — no manual tuning."""
    import math as _math
    import sys as _sys
    from pathlib import Path as _Path
    _root = _Path(__file__).resolve().parents[2]
    for _p in (str(_root / "placekyt"), str(_root / "runtime" / "python"),
               str(_Path(__file__).resolve().parent)):
        if _p not in _sys.path:
            _sys.path.insert(0, _p)
    from weaver_builder import WeaverPlan
    from weaver_builder_cfir import calibrate_phase_steps_cfir
    plan = WeaverPlan(tw=TW, lpf_gain=0.9)
    kfa, kfc, _c, _s = calibrate_phase_steps_cfir(plan)
    ph_fa = 2 * _math.pi * (-FA) / FS * (1 + kfa)
    ph_fc = 2 * _math.pi * (-FC) / FS * (1 + kfc)
    return ph_fa, ph_fc


def main():
    out = [HDR]
    PH_FA, PH_FC = _weaver_phases()
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

    # --- TX chip source: real audio -> x16_in, stream 'tx' (the TRANSMITTER input) ---
    out.append(blk("tx_src", "kyttar_source", {
        "affinity": "''", "alias": "''", "burst_len": "n_samp",
        "comment": "'audio -> chip x16_in (TX chain, stream tx)'",
        "complex_in": "float",
        "device_id": "'\"kyttar_0\"'", "maxoutbuf": "'0'", "minoutbuf": "'0'",
        "num_channels": "'1'", "port_name": "'\"x16_in\"'",
        "server_host": "'\"127.0.0.1\"'", "server_port": "server_port",
        "stream_id": "'\"tx\"'",
    }, 560, 180))

    # === FUSED-OSCILLATOR WEAVER (no shared NCO, no carrier fan-out) ===
    # This chip is clockless: a standalone NCO drawn as a source gets no trigger and is
    # DEAD (see dev_docs/OSCILLATOR_TOPOLOGY_ANALYSIS.md). So each mixer carries its OWN
    # oscillator. kyttar_complex_mixer emits BOTH rails (yi=sig*cos, yq=sig*sin) as two
    # separate output ports, so the DOWN-mix (which needs both rails from one signal) is
    # ONE mixer; each UP-mix (which needs one rail from one signal) is one mixer using a
    # single output. All fa-mixers start phase 0 at sample 0 (coherent); likewise all
    # fc-mixers. Replicating the cheap phase-accumulator per mixer costs cells (plentiful)
    # but removes the fan-out (wires, scarce) — the fabric-native trade.

    # === COMPLEX-FIR WEAVER (pure complex packets, one filter block per half) ===
    # ComplexLowPass filters BOTH rails of the mixer's complex packet in ONE block
    # (= fir_filter_ccf), so the classic Weaver's complex-to-2-real-LPF FAN-OUT and
    # the 2-real-to-1 recombine FAN-IN both vanish. Each half is a straight complex
    # filament: cmix (complex out) -> clpf (complex in/out) -> iqup (complex in, real
    # out; out = I*cos - Q*sin). No split, no add. This is the topology that fits ONE
    # 10x12 die (78/120 cells) — see examples/ssb_weaver/weaver_builder_cfir.py.

    # === TX chain (transmitter): cmix(-fa) -> ComplexLowPass -> iqup(fc) [= SSB] ===
    out.append(cmix("tx_ma", -FA, PH_FA, 900, 180))  # audio -> yi=a*cos(fa), yq=a*sin(fa)
    out.append(clpf("tx_lp", 1120, 180))         # complex LPF of (I,Q)
    out.append(iqup("tx_up", FC, 1360, 180))     # SSB = I'*cos(fc) - Q'*sin(fc)

    # --- TX chip sink: SSB passband <- x16_out, stream 'tx' (the TRANSMITTER output) ---
    out.append(blk("tx_sink", "kyttar_sink", {
        "affinity": "''", "alias": "''",
        "comment": "'SSB passband <- x16_out (TX chain, stream tx)'",
        "device_id": "'\"kyttar_0\"'", "num_channels": "'1'",
        "port_name": "'\"x16_out\"'", "server_host": "'\"127.0.0.1\"'",
        "server_port": "server_port", "stream_id": "'\"tx\"'",
    }, 1580, 180))

    # === RX chip source: SSB passband -> x16_in, stream 'rx' (the RECEIVER input) ===
    # Fed by the TX chain's passband output (tx_sink) — an over-the-air loopback
    # through TWO INDEPENDENT chip chains (exactly the BPSK duplex model).
    out.append(blk("rx_src", "kyttar_source", {
        "affinity": "''", "alias": "''", "burst_len": "n_samp",
        "comment": "'SSB passband -> chip x16_in (RX chain, stream rx)'",
        "complex_in": "float",
        "device_id": "'\"kyttar_0\"'", "maxoutbuf": "'0'", "minoutbuf": "'0'",
        "num_channels": "'1'", "port_name": "'\"x16_in\"'",
        "server_host": "'\"127.0.0.1\"'", "server_port": "server_port",
        "stream_id": "'\"rx\"'",
    }, 1760, 340))

    # === RX chain (receiver): cmix(-fc) -> ComplexLowPass -> iqup(fa) [= audio] ===
    out.append(cmix("rx_mc", -FC, PH_FC, 1980, 340))  # ssb -> yi=ssb*cos(fc), yq=ssb*sin(fc)
    out.append(clpf("rx_lp", 2200, 340))
    out.append(iqup("rx_up", FA, 2440, 340))     # recovered audio (pre-gain)

    # --- x4 Weaver gain (recovered audio scale) — RX chain ---
    out.append(blk("g4", "kyttar_gain", {
        "affinity": "''", "alias": "''", "comment": "'Weaver 1/4 -> x4'",
        "device_id": "'\"kyttar_0\"'", "gain": "'4'",
        "maxoutbuf": "'0'", "minoutbuf": "'0'",
    }, 2760, 340))

    # --- RX chip sink: recovered audio <- x16_out, stream 'rx' (RECEIVER output) ---
    out.append(blk("rx_sink", "kyttar_sink", {
        "affinity": "''", "alias": "''",
        "comment": "'recovered audio <- x16_out (RX chain, stream rx)'",
        "device_id": "'\"kyttar_0\"'", "num_channels": "'1'",
        "port_name": "'\"x16_out\"'", "server_host": "'\"127.0.0.1\"'",
        "server_port": "server_port", "stream_id": "'\"rx\"'",
    }, 2940, 340))

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
    }, 2940, 200))
    # TX passband scope (the transmitted SSB signal off the chip's TX chain)
    out.append(blk("pb_sink", "qtgui_time_sink_x", {
        "affinity": "''", "alias": "''", "autoscale": "'True'",
        "axislabels": "'True'", "bw": "samp_rate", "comment": "''",
        "ctrlpanel": "'False'", "entags": "'True'", "grid": "'True'",
        "gui_hint": "''", "label1": "'SSB passband'", "legend": "'True'",
        "marker1": "'-1'", "name": '"TX passband (SSB, chip out)"',
        "nconnections": "'1'", "size": "n_samp", "srate": "samp_rate",
        "stemplot": "'False'", "style1": "'1'", "tr_chan": "'0'",
        "tr_delay": "'0'", "tr_level": "'0'", "tr_mode": "qtgui.TRIG_MODE_FREE",
        "tr_slope": "qtgui.TRIG_SLOPE_POS", "tr_tag": "''", "type": "float",
        "update_time": "'0.10'", "width1": "'1'", "ylabel": "'Amplitude'",
        "ymax": "'1'", "ymin": "'-1'", "yunit": "''", "grid_color1": "'red'",
        "alpha1": "'1.0'",
    }, 1580, 40))

    # --- connections: TWO INDEPENDENT chip chains, shared x16_in/x16_out by tag ---
    # This is the BPSK-duplex model: a TX chain (audio -> SSB passband) and a SEPARATE
    # RX chain (SSB passband -> recovered audio) each live on the SAME array, sharing
    # x16_in / x16_out demultiplexed by stream_id ("tx"/"rx"). The chip DSP blocks sit
    # BETWEEN each chain's kyttar_source and kyttar_sink; the source/sink ARE the port
    # taps. The TX passband output (tx_sink) feeds the RX input (rx_src) — an
    # over-the-air loopback through two independent chains, NOT a single on-chip wire.
    conns = [
        ("tone", 0, "audio", 0), ("tone2", 0, "audio", 1),
        ("audio", 0, "thr", 0),
        ("thr", 0, "in_sink", 0),       # input-audio scope
        # ============================ TX chain (stream 'tx') ============================
        ("thr", 0, "tx_src", 0),        # audio -> chip x16_in (tag tx)
        ("tx_src", 0, "tx_ma", 0),      # audio -> fused fa-mixer (xi)
        ("tx_ma", 0, "tx_lp", 0),       # (I,Q) packet -> complex LPF
        ("tx_lp", 0, "tx_up", 0),       # filtered (I',Q') -> iqup: SSB = I'cos - Q'sin
        ("tx_up", 0, "tx_sink", 0),     # SSB passband -> chip x16_out (tag tx)
        ("tx_sink", 0, "pb_sink", 0),   # transmitted-passband scope
        # ============================ RX chain (stream 'rx') ============================
        ("tx_sink", 0, "rx_src", 0),    # TX passband -> RX chip x16_in (tag rx) [OTA loopback]
        ("rx_src", 0, "rx_mc", 0),      # ssb -> fused fc-mixer
        ("rx_mc", 0, "rx_lp", 0),       # (I,Q) packet -> complex LPF
        ("rx_lp", 0, "rx_up", 0),       # filtered -> iqup: audio = I'cos(fa) - Q'sin(fa)
        ("rx_up", 0, "g4", 0),
        ("g4", 0, "rx_sink", 0),        # recovered audio -> chip x16_out (tag rx)
        ("rx_sink", 0, "out_sink", 0),  # recovered-audio scope
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
