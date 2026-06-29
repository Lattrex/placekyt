"""Generate examples/bpsk_transceiver_loopback/bpsk_loopback.grc (+ a copy under
tests/data/grc/).

THE LOOPBACK IS THE REAL MODEM DESIGN + A GR DOWNCONVERT. We start from the proven,
importable bpsk_modem.grc (all real kyttar_* DSP blocks: PSK mapper, upsampler, RRC,
IQUpconvert on TX; complex RRC matched filter, Costas, Gardner, BPSK slicer on RX;
kyttar_source/sink as the chip x16_in/x16_out). We keep every kyttar block + the TX
bit source, and we CLOSE THE LOOP: instead of the RX source reading an independent
rx_iq stimulus, it reads the TX sink's REAL passband downconverted to complex baseband
through stock GR blocks (float_to_complex -> * sig_source_c(-carrier, ampl 2) ->
skiphead(1) -> keep_one_in_n(2) — the proven BER-0 recipe). A constellation sink taps
the downconverted baseband.

So the .grc is FULL of real Kyttar blocks and imports into placeKYT EXACTLY like the
modem (the stock-GR downconvert is dropped on import, leaving the two on-chip filaments);
and it RUNS the transceiver loop live (TX bits -> chip TX -> passband -> downconvert ->
chip RX -> recovered bits == sent, BER 0).
"""
from __future__ import annotations

import copy
from pathlib import Path

import yaml

REPO = Path("/home/system/placekyt")
MODEM = REPO / "examples" / "bpsk_modem" / "bpsk_modem.grc"
OUT1 = REPO / "examples" / "bpsk_transceiver_loopback" / "bpsk_loopback.grc"
OUT2 = REPO / "placekyt" / "tests" / "data" / "grc" / "bpsk_loopback.grc"


def _state(coord):
    return {"bus_sink": False, "bus_source": False, "bus_structure": None,
            "coordinate": coord, "rotation": 0, "state": "enabled"}


def _by_name(doc, name):
    for b in doc["blocks"]:
        if b.get("name") == name:
            return b
    raise KeyError(name)


def main():
    doc = yaml.safe_load(MODEM.read_text())

    # 1. Title/description: this is a transceiver loopback, not two independent streams.
    doc["options"]["parameters"]["id"] = "bpsk_loopback"
    doc["options"]["parameters"]["title"] = (
        "BPSK transceiver loopback (TX -> upconvert -> downconvert -> RX) on one chip")
    doc["options"]["parameters"]["description"] = (
        "ONE importable design of REAL Kyttar blocks. The TX chain (PSK mapper -> "
        "upsampler -> RRC -> I/Q upconvert) and the coherent RX chain (complex RRC "
        "matched filter -> Costas -> Gardner -> BPSK slicer) both live on ONE placeKYT "
        "array. GRC closes the loop: the TX passband leaves the chip, is downconverted "
        "to complex baseband in stock GR (the 'channel'), AWGN is added (noise_volt "
        "variable; 0 = clean, BER 0), and it re-enters as the RX input -> recovered "
        "bits == transmitted bits. Raise noise_volt live to watch the constellation "
        "smear and the BER climb. Import into placeKYT (File -> Import GNURadio "
        "Flowgraph): all 8 DSP blocks place on the chip; the GR downconvert + AWGN "
        "channel are dropped. Then 'Run as GNURadio Server' + Execute this graph.")

    # 2. Variables: the modem already has sps/samp_rate/carrier/n_syms/n_bits. The
    #    downconvert needs fs + carrier; the modem's samp_rate (32k) and carrier (4k)
    #    already match the proven recipe (carrier = fs/8). Reuse them.

    # 3. Drop the independent RX stimulus (rx_iq) — the loop now feeds rx_src.
    doc["blocks"] = [b for b in doc["blocks"] if b.get("name") != "rx_iq"]

    # 4. Build the DOWNCONVERT (stock GR; dropped on import). pb = real passband off
    #    tx_sink; bb = 2*pb*exp(-j*2*pi*(carrier/samp_rate)*n); bb[1:][::2].
    f2c = {
        "name": "f2c", "id": "blocks_float_to_complex",
        "parameters": {"affinity": "", "alias": "",
                       "comment": "TX passband (real) -> complex (imag = 0)",
                       "maxoutbuf": "0", "minoutbuf": "0", "vlen": "1"},
        "states": _state([1264, 196])}
    lo = {
        "name": "lo", "id": "analog_sig_source_x",
        "parameters": {
            "affinity": "", "alias": "", "amp": "2.0",
            "comment": "LO = 2*exp(-j*2*pi*(carrier/samp_rate)*n) (image-reject gain "
            "2.0); skiphead(1) absorbs the upconvert NCO increment-before-emit.",
            "freq": "-carrier", "maxoutbuf": "0", "minoutbuf": "0", "offset": "0",
            "phase": "0", "samp_rate": "samp_rate", "showports": "False",
            "type": "complex", "waveform": "analog.GR_COS_WAVE"},
        "states": _state([1264, 320])}
    mix = {
        "name": "mix", "id": "blocks_multiply_xx",
        "parameters": {"affinity": "", "alias": "",
                       "comment": "downconvert to complex baseband",
                       "maxoutbuf": "0", "minoutbuf": "0", "num_inputs": "2",
                       "type": "complex", "vlen": "1"},
        "states": _state([1456, 204])}
    skip = {
        "name": "skip", "id": "blocks_skiphead",
        "parameters": {"affinity": "", "alias": "",
                       "comment": "bb[1:] (exact 1-sample delay; not blocks.delay)",
                       "maxoutbuf": "0", "minoutbuf": "0", "num_items": "1",
                       "type": "complex"},
        "states": _state([1640, 204])}
    keep = {
        "name": "keep", "id": "blocks_keep_one_in_n",
        "parameters": {"affinity": "", "alias": "",
                       "comment": "bb[::2] (decimate TX sps 4 -> RX sps 2)",
                       "maxoutbuf": "0", "minoutbuf": "0", "n": "2",
                       "type": "complex"},
        "states": _state([1816, 204])}

    # 5. The RX source: its complex input now comes from the downconvert. Reuse the
    #    modem's rx_src block as-is (stream_id rx, complex_in complex) — only its
    #    upstream connection changes. Make its burst_len match the downconverted length
    #    (n_syms*sps-1 downconverted to sps2 -> the RX expects ~ n_syms*2-1; the modem
    #    already sets burst_len = stim.rx_burst_len(n_syms). For the loop the burst is
    #    the downconverted passband length = (n_bits*sps_tx)/2 - 1). Set it from n_bits.
    rx_src = _by_name(doc, "rx_src")
    rx_src["parameters"]["burst_len"] = "n_bits*4//2 - 1"
    rx_src["parameters"]["comment"] = (
        "RX I/Q baseband (from the downconverted TX passband) -> matched filter")

    # 5b. Stage D — AWGN channel (stock GR, dropped on import). A complex Gaussian
    #     noise source is ADDED to the downconverted baseband before the RX chain.
    #     noise_volt is a GUI variable: 0.0 = clean (BER 0, the default so the
    #     headless loopback test still passes); raise it live to watch the
    #     constellation smear and the recovered-bit BER climb. The add_cc + noise
    #     source are stock GR blocks → dropped on import (the chip is unchanged).
    noise_volt = {
        "name": "noise_volt", "id": "variable",
        "parameters": {"comment": "AWGN std-dev on the baseband I/Q (0 = clean). "
                       "Raise live to degrade BER + smear the constellation.",
                       "value": "0.0"},
        "states": _state([1056, 12])}
    noise = {
        "name": "awgn", "id": "analog_noise_source_x",
        "parameters": {"affinity": "", "alias": "", "amp": "noise_volt",
                       "comment": "complex AWGN (channel noise)",
                       "maxoutbuf": "0", "minoutbuf": "0",
                       "noise_type": "analog.GR_GAUSSIAN", "seed": "0",
                       "type": "complex"},
        "states": _state([1816, 432])}
    chan = {
        "name": "chan", "id": "blocks_add_xx",
        "parameters": {"affinity": "", "alias": "",
                       "comment": "baseband + AWGN -> noisy RX input",
                       "maxoutbuf": "0", "minoutbuf": "0", "num_inputs": "2",
                       "type": "complex", "vlen": "1"},
        "states": _state([2008, 204])}

    # 6. Constellation sink off the downconverted baseband (2 clean BPSK points).
    const_sink = {
        "name": "const_sink", "id": "qtgui_const_sink_x",
        "parameters": {
            "affinity": "", "alias": "",
            "alpha1": "1.0", "alpha10": "1.0", "alpha2": "1.0", "alpha3": "1.0",
            "alpha4": "1.0", "alpha5": "1.0", "alpha6": "1.0", "alpha7": "1.0",
            "alpha8": "1.0", "alpha9": "1.0", "autoscale": "False",
            "color1": '"blue"', "color10": '"red"', "color2": '"red"',
            "color3": '"red"', "color4": '"red"', "color5": '"red"',
            "color6": '"red"', "color7": '"red"', "color8": '"red"',
            "color9": '"red"',
            "comment": "downconverted BPSK constellation (2 clean points)",
            "grid": "False", "gui_hint": "", "label1": "baseband",
            "label10": "", "label2": "", "label3": "", "label4": "",
            "label5": "", "label6": "", "label7": "", "label8": "", "label9": "",
            "legend": "True",
            "marker1": "0", "marker10": "0", "marker2": "0", "marker3": "0",
            "marker4": "0", "marker5": "0", "marker6": "0", "marker7": "0",
            "marker8": "0", "marker9": "0",
            "name": '"Downconverted constellation"', "nconnections": "1",
            "size": "1024", "style1": "0", "style10": "0", "style2": "0",
            "style3": "0", "style4": "0", "style5": "0", "style6": "0",
            "style7": "0", "style8": "0", "style9": "0",
            "tr_chan": "0", "tr_level": "0.0", "tr_mode": "qtgui.TRIG_MODE_FREE",
            "tr_slope": "qtgui.TRIG_MODE_POS", "tr_tag": '""', "type": "complex",
            "update_time": "0.10", "width1": "1", "width10": "1", "width2": "1",
            "width3": "1", "width4": "1", "width5": "1", "width6": "1",
            "width7": "1", "width8": "1", "width9": "1",
            "xmax": "2", "xmin": "-2", "ymax": "2", "ymin": "-2"},
        "states": _state([1816, 320])}

    doc["blocks"].extend([f2c, lo, mix, skip, keep, noise_volt, noise, chan,
                          const_sink])

    # 7. Connections: keep the modem's, but RE-WIRE the loop:
    #    - drop  rx_iq -> rx_src   (independent stimulus gone)
    #    - drop  tx_sink -> tx_passband stays (the passband scope) — keep it.
    #    - add   tx_sink -> f2c -> mix(*lo) -> skip -> keep -> chan(+AWGN) ->
    #            rx_src  (+ const off chan, so the scope shows the NOISY baseband)
    conns = [c for c in doc["connections"]
             if not (c[0] == "rx_iq" and c[2] == "rx_src")]
    conns += [
        ["tx_sink", "0", "f2c", "0"],
        ["f2c", "0", "mix", "0"],
        ["lo", "0", "mix", "1"],
        ["mix", "0", "skip", "0"],
        ["skip", "0", "keep", "0"],
        ["keep", "0", "chan", "0"],          # baseband -> AWGN adder
        ["awgn", "0", "chan", "1"],          # noise -> AWGN adder
        ["chan", "0", "rx_src", "0"],        # noisy baseband -> RX chain
        ["chan", "0", "const_sink", "0"],    # constellation taps the NOISY baseband
    ]
    doc["connections"] = conns

    text = yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, width=200)
    for out in (OUT1, OUT2):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
        print("wrote", out)


if __name__ == "__main__":
    main()
