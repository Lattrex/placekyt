#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate ``fft128_2p2s.grc`` — drive the FFT128 across CHAIN A of the 2P2S
board through the placeKYT MULTI-CHIP GNURadio server and display what comes
back AS A SPECTRUM.

    tone burst ─▶ kyttar_source ─▶ [FFT128 die0] ─▶ [FFT128 die1]
                                  (chip 0, A0 head) (chip 1, A1 tail)
                  ─▶ kyttar_sink ─▶ spectrum ─▶ to_db ─▶ VECTOR sink (Hz axis)

The GR flowgraph is the LOGICAL app: it carries only ``stream_id``. placeKYT
owns which chip / port / hop / tag that maps to, resolved from the placed and
routed ``fft128_2p2s.kyt``. The two die markers sit in the chain so the
flowgraph reads as the transform it is; they compute nothing in-process.

THE DISPLAY IS NOT THE SINK STREAM. The chain tail is a COMPLEX exit cell, so
the sink's float stream is out_i, out_q, out_i, out_q ... in BIT-REVERSED DIF
slot order at the FFT/128 scale. Plotting that directly on a time sink is a
time series of raw words — which is exactly the "spikes flow across the screen
and the x axis is time, not frequency" report this flowgraph's display chain
exists to answer. ``fft128_2p2s_spectrum.py`` de-interleaves the pair,
un-reverses the slots, fftshifts, and emits per-bin POWER; the vector sink then
labels the points from -samp_rate/2 in steps of ``bin_hz`` so the peak reads
directly in Hz. Same shape as ``examples/fft_spectrum``, with N and the
complex de-interleave being what differ.

Run:  python3 examples/fft128_2p2s/gen_grc.py
Then: open fft128_2p2s.kyt in placeKYT -> Simulation -> Run as GNURadio Server,
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
OUT = os.path.join(HERE, "fft128_2p2s.grc")

#: The transform size, its pinned pipeline latency (64 from die 0 + 63 from
#: die 1) and the sample rate the stimulus is DECLARED at. The array is
#: asynchronous — it has no clock of its own — so the sample rate is entirely
#: the user's statement, and it is what turns a bin index into a physical
#: frequency. 32000 is the repo-wide example convention, giving 250 Hz/bin.
N_FFT = 128
LATENCY = N_FFT - 1
SAMP_RATE = 32000
BIN_HZ = SAMP_RATE / N_FFT              # 250.0

#: The two stimulus tones, as NATURAL bin indices, and their amplitudes.
#: ON-BIN (an integer number of cycles per 128-sample frame) so a correct
#: transform is two clean lines with no leakage.
TONE_A, AMP_A = 9, 0.45
TONE_B, AMP_B = 37, 0.35
TONE_A_HZ = TONE_A * BIN_HZ             # 2250.0
TONE_B_HZ = TONE_B * BIN_HZ             # 9250.0

#: Two whole frames past the transform's 127-sample latency, so the display
#: gets real bins rather than the zero-fill transient. Rounded up from
#: latency + 2*n_fft = 383 to a round 384; the spare sample is consumed by
#: the transient strip, and the value the gates compare against is this one.
BURST = 384
#: A QT time_sink draws NOTHING until a FULL ``size`` buffer arrives, and the
#: GR scheduler strands the tail of a finite stream — so a scope sized at (or
#: above) its burst NEVER paints. Stay clear of the burst by a margin.
SCOPE_POINTS = BURST - 64


def _epy_source(name):
    """The display block's source, read from its companion module so the
    flowgraph and the readable file cannot drift apart."""
    with open(os.path.join(HERE, name)) as fh:
        return fh.read()


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


def _vector_sink(name, title, comment, x, y):
    """A FREQUENCY-domain sink: one point per bin, on a real Hz axis.

    ``x_start``/``x_step``/``x_units`` are what make the plot readable in Hz
    (the generated flowgraph calls ``set_x_axis(-samp_rate/2, bin_hz)`` and
    ``set_x_axis_units("Hz")``). A time sink cannot do this at all — its x axis
    is time, which is precisely the display defect this replaces.
    """
    vs = {f"alpha{k}": "1.0" for k in range(1, 11)}
    vs.update({f"color{k}": '"blue"' for k in range(1, 11)})
    vs.update({f"label{k}": "" for k in range(1, 11)})
    vs.update({f"width{k}": "1" for k in range(1, 11)})
    vs.update({
        "affinity": "", "alias": "", "autoscale": "False", "average": "1.0",
        "color1": '"blue"', "color2": '"red"', "comment": comment,
        "grid": "True", "gui_hint": "",
        "label1": f"on-chip FFT{N_FFT} power spectrum "
                  f"(bin k = k*samp_rate/{N_FFT} Hz)",
        "legend": "True", "maxoutbuf": "0", "minoutbuf": "0",
        "name": f'"{title}"', "nconnections": "1", "ref_level": "0",
        "showports": "False", "update_time": "0.10", "vlen": "n_fft",
        "width1": "3",
        "x_axis_label": '"frequency (Hz) — bin k of '
                        f'{N_FFT} at samp_rate = k*samp_rate/{N_FFT}"',
        "x_start": "-samp_rate / 2", "x_step": "bin_hz", "x_units": '"Hz"',
        "y_axis_label": '"power (dBFS)"', "y_units": '""',
        "ymax": "5", "ymin": "-95"})
    return _blk(name, "qtgui_vector_sink_f", vs, x, y)


def build():
    blocks, connections = [], []

    blocks.append(_blk("n_fft", "variable",
                       {"comment": "the transform size split across chain A's "
                                   "two dies", "value": str(N_FFT)}, 40, 12))
    blocks.append(_blk("latency", "variable",
                       {"comment": "the chain's pinned pipeline latency "
                                   "(64 from die 0 + 63 from die 1) — the "
                                   "first 127 complex outputs are the "
                                   "zero-initialised startup transient, not a "
                                   "frame", "value": str(LATENCY)}, 120, 12))
    blocks.append(_blk("burst_len", "variable",
                       {"comment": "two whole 128-bin frames past the "
                                   "127-sample latency", "value": str(BURST)},
                       200, 12))
    blocks.append(_blk("scope_points", "variable",
                       {"comment": "< burst_len: a QT time_sink never paints "
                                   "a buffer it cannot fill",
                        "value": str(SCOPE_POINTS)}, 360, 12))
    blocks.append(_blk("samp_rate", "variable",
                       {"comment": "the rate the I/Q stimulus is DECLARED to "
                                   "be sampled at. The array is asynchronous "
                                   "— it has no clock of its own, so it "
                                   "transforms whatever stream you hand it and "
                                   "the sample rate is entirely YOURS to "
                                   "state. It is what turns a bin index into a "
                                   "physical frequency, so the spectrum axis "
                                   "can read in Hz.",
                        "value": str(SAMP_RATE)}, 440, 12))
    blocks.append(_blk("bin_hz", "variable",
                       {"comment": "BIN WIDTH = samp_rate / n_fft = "
                                   f"{SAMP_RATE}/{N_FFT} = {BIN_HZ:g} Hz. Bin "
                                   "k of N at rate fs is centred on k*fs/N, "
                                   "and bins at or above N/2 are NEGATIVE "
                                   "frequencies (k-N)*fs/N. The demo tones at "
                                   f"bins {TONE_A} and {TONE_B} are therefore "
                                   f"{TONE_A_HZ:g} Hz and {TONE_B_HZ:g} Hz.",
                        "value": "samp_rate / n_fft"}, 480, 12))
    blocks.append(_blk("server_port", "variable",
                       {"comment": "placeKYT's default host port (Run as "
                                   "GNURadio Server binds 58950; change if it "
                                   "prints another)",
                        "value": "58950"}, 560, 12))

    blocks.append(_blk("tone_a", "variable",
                       {"comment": "the FIRST demo tone's natural FFT bin. "
                                   "ON-BIN (an integer number of cycles per "
                                   "128-sample frame) so the answer is a clean "
                                   "line with no leakage. The chip emits it at "
                                   f"SLOT bit_reverse_7({TONE_A}) = 72; the "
                                   "spectrum block puts it back at "
                                   f"{TONE_A}. In Hz: {TONE_A} * bin_hz = "
                                   f"{TONE_A_HZ:g} Hz.",
                        "value": str(TONE_A)}, 640, 12))
    blocks.append(_blk("tone_b", "variable",
                       {"comment": "the SECOND demo tone's natural FFT bin. "
                                   f"SLOT bit_reverse_7({TONE_B}) = 82 off the "
                                   f"chip; {TONE_B} * bin_hz = {TONE_B_HZ:g} "
                                   "Hz on the plot.",
                        "value": str(TONE_B)}, 720, 12))

    # A two-tone complex stimulus: distinct bins so a correct transform is
    # visually obvious and a wrong one is not. Written against the tone_a /
    # tone_b / n_fft variables so the flowgraph's stimulus and its published
    # frequencies cannot drift apart.
    vec = (f"[{AMP_A}*__import__('cmath').exp("
           "2j*3.141592653589793*tone_a*n/n_fft)"
           f" + {AMP_B}*__import__('cmath').exp("
           "2j*3.141592653589793*tone_b*n/n_fft)"
           " for n in range(burst_len)]")
    blocks.append(_blk("stim", "blocks_vector_source_x", {
        "affinity": "", "alias": "",
        "comment": f"two complex tones — bins {TONE_A} and {TONE_B} of "
                   f"{N_FFT}, i.e. {TONE_A_HZ:g} Hz and {TONE_B_HZ:g} Hz at "
                   f"samp_rate = {SAMP_RATE}",
        "maxoutbuf": "0", "minoutbuf": "0", "repeat": "True", "tags": "[]",
        "type": "complex", "vector": vec, "vlen": "1"}, 40, 200))

    # kyttar_source: LOGICAL — carries only the stream_id. placeKYT resolves
    # it to chip 0's head, its hop, entry and the xi/xq registers.
    blocks.append(_blk("kyt_src", "kyttar_source", {
        "affinity": "", "alias": "", "burst_len": "burst_len",
        "comment": "stream 'fft' -> chain A's head, chip 0 x16_in "
                   "(chip/hop resolved in placeKYT)",
        "complex_in": "complex", "device_id": '"kyttar_0"',
        "maxoutbuf": "0", "minoutbuf": "0", "num_channels": "1",
        # OUTPUT WORD ENCODING — load-bearing, and NOT the default here.
        # "auto" ties raw-int16 output to complex_in, which is the BIT-PACKING
        # receiver convention (a slicer's decoded bit lives in the word LSB, so
        # Q15 scaling would crush it). This chain is the opposite case: its
        # output is a Q15 VALUE (the transform's bins). Left on "auto" the sink
        # emits raw +-30000 word floats — off the scope's -1..1 axis, and any
        # client applying the documented q15/32768 convention decodes garbage
        # (14746.0 * 32768 & 0xFFFF == 0). Same class as the LMS equalizer's
        # missing-constellation report; the fix is the same.
        "output_words": '"q15"', "port_name": '"x16_in"',
        # One burst per Run (the SINK loops the genuine result for the display
        # via server_repeat). A repeat-burst SOURCE keeps consuming the
        # repeating stimulus during a dispatch, which rotates the frame grid.
        "pipelined": "False", "repeat": "no",
        "schedule": '"interleaved"', "server_host": '"127.0.0.1"',
        "server_port": "server_port", "stream_id": '"fft"'}, 290, 200))

    # The two dies, in chain order. Markers: the DSP runs on the chips.
    blocks.append(_blk("die0", "kyttar_fft128_die0", {
        "affinity": "", "alias": "",
        "comment": "chip 0 (A0, chain A head) — stage 0, the period-64 octant "
                   "fold. Its output is a PARTIAL transform, not bins.",
        "device_id": '"kyttar_0"', "maxoutbuf": "0", "minoutbuf": "0"},
        540, 200))
    blocks.append(_blk("die1", "kyttar_fft128_die1", {
        "affinity": "", "alias": "",
        "comment": "chip 1 (A1, chain A tail) — stages 1..6. Output = the "
                   "transform's bins, BIT-REVERSED, scale FFT/128.",
        "device_id": '"kyttar_0"', "maxoutbuf": "0", "minoutbuf": "0"},
        720, 200))

    # kyttar_sink: drains the recovered words off the CHAIN TAIL (chip 1's
    # x16_out). Same stream_id; placeKYT knows the tail is chip 1.
    blocks.append(_blk("kyt_sink", "kyttar_sink", {
        "affinity": "", "alias": "",
        "comment": "recovered bins off chain A's tail, chip 1 x16_out",
        "device_id": '"kyttar_0"', "hold_secs": "8.0", "in_type": "complex",
        "maxoutbuf": "0", "minoutbuf": "0", "num_channels": "1",
        "port_name": '"x16_out"', "server_port": "server_port",
        "server_repeat": "True", "stream_id": '"fft"'}, 920, 200))

    # ---- THE DISPLAY CHAIN: raw words -> a real spectrum on a Hz axis ----
    #
    # The kyttar sink's monitoring output is a FLOAT stream of the recovered
    # words at the q15/32768 scale, and it is NOT a spectrum: the chain tail is
    # a COMPLEX exit cell, so the stream is out_i, out_q, out_i, out_q ... in
    # BIT-REVERSED DIF slot order, with the first 127 complex samples being the
    # pipeline's zero-fill transient. A time sink on that stream shows raw
    # words against a TIME axis — spikes scrolling sideways with no frequency
    # anywhere on the plot. That was the reported display defect.
    #
    # ``spectrum`` de-interleaves the I/Q pair into complex bins, strips the
    # latency, un-reverses the slots, fftshifts, and emits per-bin POWER as one
    # 128-point vector per whole frame; ``to_db`` makes it dBFS; the VECTOR
    # sink labels the points from -samp_rate/2 in steps of bin_hz.
    blocks.append(_blk("spectrum", "epy_block", {
        "_source_code": _epy_source("fft128_2p2s_spectrum.py"),
        "affinity": "", "alias": "", "burst_len": "burst_len",
        "comment": "interleaved I/Q chip words -> a 128-bin POWER vector in "
                   "ASCENDING FREQUENCY order from -samp_rate/2 "
                   "(de-interleave, un-reverse, fftshift), with the "
                   "127-sample startup transient stripped",
        "latency": "latency", "maxoutbuf": "0", "minoutbuf": "0",
        "n_fft": "n_fft"}, 1120, 180))
    blocks[-1]["states"]["_io_cache"] = (
        "('FFT128 words to centred spectrum', 'blk', [('n_fft', '128'), "
        "('latency', '127'), ('burst_len', '384')], [('0', 'float', 1)], "
        "[('0', 'float', 128)], '', ['burst_len', 'latency', 'n_fft'])")

    blocks.append(_blk("to_db", "epy_block", {
        "_source_code": _epy_source("fft128_2p2s_to_db.py"),
        "affinity": "", "alias": "",
        "comment": "linear power -> dBFS (0 dBFS = a full-scale coherent bin)",
        "floor_db": "-90.0", "maxoutbuf": "0", "minoutbuf": "0",
        "n_fft": "n_fft"}, 1120, 340))
    blocks[-1]["states"]["_io_cache"] = (
        "('Power to dBFS', 'blk', [('n_fft', '128'), ('floor_db', '-90.0')], "
        "[('0', 'float', 128)], [('0', 'float', 128)], '', "
        "['floor_db', 'n_fft'])")

    blocks.append(_vector_sink(
        "spectrum_sink",
        f"On-chip FFT128 spectrum — {BIN_HZ:g} Hz/bin, tones at "
        f"+{TONE_A_HZ:g} Hz and +{TONE_B_HZ:g} Hz (dBFS)",
        f"THE SPECTRUM — {N_FFT} bins off chain A's tail, on a real "
        f"FREQUENCY axis. x runs from -samp_rate/2 = -{SAMP_RATE // 2} Hz in "
        f"steps of bin_hz = samp_rate/n_fft = {BIN_HZ:g} Hz, so point i is "
        f"(i - {N_FFT // 2})*{BIN_HZ:g} Hz. The demo tones (natural bins "
        f"{TONE_A} and {TONE_B}) are the lines at +{TONE_A_HZ:g} Hz and "
        f"+{TONE_B_HZ:g} Hz; every other bin sits on the -90 dBFS floor.",
        1400, 320))

    # The input, for comparison.
    blocks.append(_blk("in_mag", "blocks_complex_to_mag", {
        "affinity": "", "alias": "", "comment": "|input| for comparison",
        "maxoutbuf": "0", "minoutbuf": "0", "vlen": "1"}, 960, 520))
    blocks.append(_time_sink("in_scope", "input |x[n]| (two tones)",
                             "the stimulus driving the pair", 1160, 500))

    connections.append(["stim", "0", "kyt_src", "0"])
    connections.append(["kyt_src", "0", "die0", "0"])
    connections.append(["die0", "0", "die1", "0"])
    connections.append(["die1", "0", "kyt_sink", "0"])
    connections.append(["kyt_sink", "0", "spectrum", "0"])
    connections.append(["spectrum", "0", "to_db", "0"])
    connections.append(["to_db", "0", "spectrum_sink", "0"])
    connections.append(["stim", "0", "in_mag", "0"])
    connections.append(["in_mag", "0", "in_scope", "0"])

    return {
        "options": {"parameters": {
            "author": "Lattrex", "catch_exceptions": "True",
            "category": "[GRC Hier Blocks]", "cmake_opt": "", "comment": "",
            "copyright": "", "description":
                "FFT128 on the 2P2S board: chain A's head (chip 0) runs stage 0, "
                "its tail (chip 1) runs stages 1..6, joined by the board's "
                "on-carrier series link. Driven through the placeKYT multi-chip "
                "GNURadio server. The spectrum plot reads in Hz: bin k of "
                f"{N_FFT} at samp_rate is k*fs/N, so at {SAMP_RATE} Hz each bin "
                f"is {BIN_HZ:g} Hz wide and the two demo tones (bins {TONE_A} "
                f"and {TONE_B}) peak at +{TONE_A_HZ:g} Hz and "
                f"+{TONE_B_HZ:g} Hz.",
            "gen_cmake": "On", "gen_linking": "dynamic",
            "generate_options": "qt_gui", "hier_block_src_path": ".:",
            "id": "fft128_2p2s_demo", "max_nouts": "0",
            "output_language": "python", "placement": "(0,0)",
            "qt_qss_theme": "", "realtime_scheduling": "", "run": "True",
            "run_command": "{python} -u {filename}", "run_options": "prompt",
            "sizing_mode": "fixed", "thread_safe_setters": "",
            "title": f"Kyttar FFT128 — a {N_FFT}-point spectrum across the "
                     f"2P2S board's chain A ({BIN_HZ:g} Hz/bin, tones at "
                     f"+{TONE_A_HZ:g} and +{TONE_B_HZ:g} Hz)",
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
