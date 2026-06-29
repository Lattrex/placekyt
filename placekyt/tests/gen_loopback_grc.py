"""Generate examples/bpsk_transceiver_loopback/bpsk_loopback.grc (+ a copy under
tests/data/grc/). The qtgui time/const sink param dicts are reused VERBATIM from
the existing coherent_bpsk_rx.grc so they carry every field GRC's generator wants;
only name/type/nconnections/labels are adapted. The DSP chain is the locked,
deterministic BER-0 passband loopback recipe (see test_bpsk_loopback_e2e.py)."""
from __future__ import annotations

import copy
from pathlib import Path

import yaml

REPO = Path("/home/system/placekyt")
SRC_GRC = REPO / "examples" / "coherent_bpsk_rx" / "coherent_bpsk_rx.grc"
OUT1 = REPO / "examples" / "bpsk_transceiver_loopback" / "bpsk_loopback.grc"
OUT2 = REPO / "placekyt" / "tests" / "data" / "grc" / "bpsk_loopback.grc"


def _blocks_by_id(doc, bid):
    return [b for b in doc["blocks"] if b["id"] == bid]


def _state(coord):
    return {"bus_sink": False, "bus_source": False, "bus_structure": None,
            "coordinate": coord, "rotation": 0, "state": "enabled"}


def _var(name, value, comment, coord):
    return {"name": name, "id": "variable",
            "parameters": {"comment": comment, "value": value},
            "states": _state(coord)}


def main():
    src = yaml.safe_load(SRC_GRC.read_text())
    # Reuse a fully-populated qtgui_time_sink_x and (we'll synthesize const from it).
    time_tpl = copy.deepcopy(_blocks_by_id(src, "qtgui_time_sink_x")[0])

    opts = {
        "parameters": {
            "author": "Lattrex",
            "catch_exceptions": "True",
            "category": "[GRC Hier Blocks]",
            "cmake_opt": "",
            "comment": "",
            "copyright": "",
            "description": (
                "BPSK transceiver PASSBAND LOOPBACK. A DRIVER flowgraph (NOT a "
                "placeKYT chip design): it drives a placeKYT-hosted chip that has "
                "BOTH a TX and an RX chain (the bpsk_modem design). TX bits -> "
                "kyttar.chip_batch[tx] -> REAL passband (carrier = fs/8) -> "
                "downconvert in stock GR (float_to_complex * sig_source, image-"
                "reject gain 2.0, skiphead(1) + keep_one_in_n(2)) -> "
                "kyttar.chip_batch[rx] -> recovered bits. Closes the loop: bits in "
                "== bits out (BER 0). Run placeKYT's 'Run as GNURadio Server' on the "
                "bpsk_modem design, set Host/Port to match, then Execute this."),
            "gen_cmake": "On",
            "gen_linking": "dynamic",
            "generate_options": "qt_gui",
            "hier_block_src_path": ".:",
            "id": "bpsk_loopback",
            "max_nouts": "0",
            "output_language": "python",
            "placement": "(0,0)",
            "qt_qss_theme": "",
            "realtime_scheduling": "",
            "run": "True",
            "run_command": "{python} -u {filename}",
            "run_options": "prompt",
            "sizing_mode": "fixed",
            "thread_safe_setters": "",
            "title": "BPSK transceiver passband loopback "
                     "(TX -> upconvert -> downconvert -> RX)",
            "window_size": "(1600,1000)",
        },
        "states": {"bus_sink": False, "bus_source": False, "bus_structure": None,
                   "coordinate": [8, 8], "rotation": 0, "state": "enabled"},
    }

    variables = [
        _var("n_bits", "64", "TX bits in the burst", [200, 12]),
        _var("fs", "32000", "passband sample rate (Hz)", [320, 12]),
        _var("carrier", "4000", "carrier = fs/8 -> 0.125 cyc/sample", [440, 12]),
        _var("sps_tx", "4", "TX samples-per-symbol", [560, 12]),
        _var("host", '"127.0.0.1"', "placeKYT GNURadio server host", [680, 12]),
        _var("port", "58950", "placeKYT GNURadio server port", [800, 12]),
    ]
    imp = {"name": "import_stim", "id": "import",
           "parameters": {"comment": "TX bit stimulus",
                          "imports": "from gnuradio.kyttar import "
                                     "modem_demo_stim as stim"},
           "states": _state([920, 12])}

    # --- TX: bits -> chip_batch[tx] -> passband ------------------------------
    tx_bits = {
        "name": "tx_bits", "id": "blocks_vector_source_x",
        "parameters": {
            "affinity": "", "alias": "", "comment": "TX bits (+ pad so the rate-"
            "expanding chip_batch[tx] drains all 256 passband words live)",
            "maxoutbuf": "0", "minoutbuf": "0", "repeat": "False",
            "tags": "[]", "type": "float",
            "vector": "[float(b) for b in stim.tx_bits(n_bits)] + [0.0]*512",
            "vlen": "1"},
        "states": _state([40, 180])}
    tx_chip = {
        "name": "tx_chip", "id": "kyttar_chip_batch",
        "parameters": {
            "affinity": "", "alias": "",
            "comment": "TX chain on the hosted chip: bits -> real passband",
            "host": "host", "port": "port", "stream_id": '"tx"',
            "in_kind": "real", "out_kind": "real",
            "in_port": '"x16_in"', "out_port": '"x16_out"',
            "data_addr0": "0", "data_addr1": "1", "raw": "False",
            "burst_len": "n_bits", "maxoutbuf": "0", "minoutbuf": "0"},
        "states": _state([280, 172])}

    # --- downconvert: pb -> complex baseband (stock GR) ----------------------
    f2c = {
        "name": "f2c", "id": "blocks_float_to_complex",
        "parameters": {"affinity": "", "alias": "",
                       "comment": "real passband -> complex (imag = 0)",
                       "maxoutbuf": "0", "minoutbuf": "0", "vlen": "1"},
        "states": _state([520, 180])}
    lo = {
        "name": "lo", "id": "analog_sig_source_x",
        "parameters": {
            "affinity": "", "alias": "",
            "amp": "2.0", "comment": "LO = 2*exp(-j*2*pi*0.125*n) (image-reject "
            "gain 2.0); freq=-carrier, phase=0 (skiphead(1) absorbs the NCO "
            "increment-before-emit delay-1)",
            "freq": "-carrier", "maxoutbuf": "0", "minoutbuf": "0",
            "offset": "0", "phase": "0", "samp_rate": "fs",
            "showports": "False", "type": "complex",
            "waveform": "analog.GR_COS_WAVE"},
        "states": _state([520, 320])}
    mix = {
        "name": "mix", "id": "blocks_multiply_xx",
        "parameters": {"affinity": "", "alias": "",
                       "comment": "bb = 2*pb*exp(-j*2*pi*0.125*n)",
                       "maxoutbuf": "0", "minoutbuf": "0", "num_inputs": "2",
                       "type": "complex", "vlen": "1"},
        "states": _state([760, 188])}
    skip = {
        "name": "skip", "id": "blocks_skiphead",
        "parameters": {"affinity": "", "alias": "",
                       "comment": "bb[1:]  (delay 1: exact, not blocks.delay)",
                       "maxoutbuf": "0", "minoutbuf": "0", "num_items": "1",
                       "type": "complex"},
        "states": _state([960, 188])}
    keep = {
        "name": "keep", "id": "blocks_keep_one_in_n",
        "parameters": {"affinity": "", "alias": "",
                       "comment": "bb[::2]  (decimate sps 4 -> RX sps 2)",
                       "maxoutbuf": "0", "minoutbuf": "0", "n": "2",
                       "type": "complex"},
        "states": _state([1120, 188])}

    # --- RX: baseband -> chip_batch[rx] -> recovered bits --------------------
    rx_chip = {
        "name": "rx_chip", "id": "kyttar_chip_batch",
        "parameters": {
            "affinity": "", "alias": "",
            "comment": "RX chain on the hosted chip: complex baseband -> bits",
            "host": "host", "port": "port", "stream_id": '"rx"',
            "in_kind": "complex", "out_kind": "real",
            "in_port": '"x16_in"', "out_port": '"x16_out"',
            "data_addr0": "0", "data_addr1": "1", "raw": "True",
            "burst_len": "n_bits*sps_tx//2-1", "maxoutbuf": "0",
            "minoutbuf": "0"},
        "states": _state([1320, 172])}

    # --- sinks ---------------------------------------------------------------
    bit_sink = copy.deepcopy(time_tpl)
    bit_sink["name"] = "bit_sink"
    bit_sink["parameters"]["comment"] = "recovered bits (loopback out)"
    bit_sink["parameters"]["name"] = '"Recovered bits (loopback)"'
    bit_sink["parameters"]["type"] = "float"
    bit_sink["parameters"]["nconnections"] = "1"
    bit_sink["parameters"]["label1"] = "recovered bit"
    bit_sink["parameters"]["gui_hint"] = "1,0,1,1"
    bit_sink["states"] = _state([1560, 156])

    pb_sink = copy.deepcopy(time_tpl)
    pb_sink["name"] = "pb_sink"
    pb_sink["parameters"]["comment"] = "TX passband (real)"
    pb_sink["parameters"]["name"] = '"TX passband"'
    pb_sink["parameters"]["type"] = "float"
    pb_sink["parameters"]["nconnections"] = "1"
    pb_sink["parameters"]["label1"] = "passband"
    pb_sink["parameters"]["gui_hint"] = "0,0,1,1"
    pb_sink["parameters"]["ymax"] = "1.0"
    pb_sink["parameters"]["ymin"] = "-1.0"
    pb_sink["states"] = _state([520, 60])

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
            "grid": "False", "gui_hint": "1,1,1,1", "label1": "baseband",
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
        "states": _state([1320, 320])}

    blocks = ([opts] + variables + [imp, tx_bits, tx_chip, f2c, lo, mix, skip,
              keep, rx_chip, bit_sink, pb_sink, const_sink])
    connections = [
        ["tx_bits", "0", "tx_chip", "0"],
        ["tx_chip", "0", "f2c", "0"],
        ["tx_chip", "0", "pb_sink", "0"],
        ["f2c", "0", "mix", "0"],
        ["lo", "0", "mix", "1"],
        ["mix", "0", "skip", "0"],
        ["skip", "0", "keep", "0"],
        ["keep", "0", "rx_chip", "0"],
        ["keep", "0", "const_sink", "0"],
        ["rx_chip", "0", "bit_sink", "0"],
    ]
    doc = {"options": opts, "blocks": blocks, "connections": connections,
           "metadata": {"file_format": 1, "grc_version": "3.10.12.0"}}
    # NB: the leading element of "blocks" is the options block (GRC convention);
    # "options" key mirrors it. We must NOT duplicate it — fix: options is separate.
    doc["blocks"] = blocks[1:]
    doc["options"] = opts

    text = yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, width=200)
    for out in (OUT1, OUT2):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
        print("wrote", out)


if __name__ == "__main__":
    main()
