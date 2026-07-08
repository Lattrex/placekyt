# SPDX-License-Identifier: GPL-3.0-or-later
"""Standalone proof of every float<->complex converter flavor (GRC import path).

The logical dtype converters
let a REAL GNU Radio flowgraph type-check where a float stream meets a complex
block, WITHOUT the converter ever becoming a placeKYT cell. This test proves, for
every flavor, that:
  (a) the .grc imports (0 unknown blocks), and
  (b) the converter is CONSUMED — it adds ZERO cells (the cell-count invariant),
      and the upstream is wired to the downstream with the correct rail semantics.

Flavors covered:
  1. float_to_complex, SINGLE real (Q = null_source): audio -> mixer.xi (xq=0).
  2. complex_to_real: LPF -> gain, the LPF's I rail (out_i) drives the gain.
  3. complex_to_float, BOTH rails: LPF -> gain_i (out_i) + gain_q (out_q).

The DualFloatToComplexBlock (TWO independent real producers -> one complex packet
via the LOCK rendezvous) is proven to BUILD on-chip in
test_dual_float_to_complex.py; its importer AUTO-INSERTION from a 2-real .grc is a
tracked follow-up (the single-real path above is what the SSB Weaver needs).

Run::

    cd /home/system/placekyt
    QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest \
        verification/tests/test_logical_converters.py -q
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[2]
for p in (str(_ROOT / "placekyt"), str(_ROOT / "runtime" / "python")):
    if p not in sys.path:
        sys.path.insert(0, p)

CHIP = "kyttar_10x12"


def _import(grc_text: str):
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from engine.catalog import BlockCatalog
    from engine.grc_import import import_grc
    tmp = _ROOT / "verification" / "tests" / "_tmp_conv.grc"
    tmp.write_text(grc_text)
    try:
        cat = BlockCatalog.from_gr_kyttar()
        return import_grc(str(tmp), cat, chip_type=CHIP)
    finally:
        tmp.unlink(missing_ok=True)


def _ep(e):
    from model.connection import BlockEndpoint, ChipPortEndpoint
    if isinstance(e, BlockEndpoint):
        return f"{e.block}.{e.port}"
    if isinstance(e, ChipPortEndpoint):
        return f"PORT:{e.port}"
    return str(e)


def _nets(res):
    return {(_ep(c.source), _ep(c.target)) for c in res.project.connections}


def _src(name, dtype, sid, x, y):
    return (f"- name: {name}\n  id: kyttar_source\n  parameters: {{port_name: '\"x16_in\"',"
            f" complex_in: {dtype}, stream_id: '\"{sid}\"', burst_len: '256',"
            f" device_id: '\"kyttar_0\"', num_channels: '1',"
            f" server_host: '\"127.0.0.1\"', server_port: '0'}}\n"
            f"  states: {{coordinate: [{x},{y}], rotation: 0, state: enabled}}\n")


def _sink(name, sid, x, y):
    return (f"- name: {name}\n  id: kyttar_sink\n  parameters: {{port_name: '\"x16_out\"',"
            f" stream_id: '\"{sid}\"', device_id: '\"kyttar_0\"', num_channels: '1',"
            f" server_host: '\"127.0.0.1\"', server_port: '0'}}\n"
            f"  states: {{coordinate: [{x},{y}], rotation: 0, state: enabled}}\n")


def _lpf(name, x, y):
    return (f"- name: {name}\n  id: kyttar_complex_low_pass_filter\n"
            f"  parameters: {{device_id: '\"kyttar_0\"', gain: '0.9', samp_rate: '32000',"
            f" cutoff_freq: '1200', transition_width: '2500', window: '\"hamming\"',"
            f" beta: '6.76'}}\n  states: {{coordinate: [{x},{y}], rotation: 0, state: enabled}}\n")


def _gain(name, g, x, y):
    return (f"- name: {name}\n  id: kyttar_gain\n  parameters: {{device_id: '\"kyttar_0\"',"
            f" gain: '{g}'}}\n  states: {{coordinate: [{x},{y}], rotation: 0, state: enabled}}\n")


def _mixer(name, x, y):
    return (f"- name: {name}\n  id: kyttar_complex_mixer\n  parameters:"
            f" {{device_id: '\"kyttar_0\"', sample_rate: '32000', frequency: '-1500',"
            f" amplitude: '1.0', offset: '0.0', phase: '0.0'}}\n"
            f"  states: {{coordinate: [{x},{y}], rotation: 0, state: enabled}}\n")


_HDR = ("options: {parameters: {id: t, generate_options: qt_gui, title: t},"
        " states: {coordinate: [8,8], rotation: 0, state: enabled}}\nblocks:\n")
_FOOT = "metadata: {file_format: 1}\n"


# --- flavor 1: single-real float_to_complex (Q = null_source) -----------------
def test_single_real_float_to_complex():
    grc = _HDR + (
        _src("src", "float", "tx", 100, 100)
        + "- name: nq\n  id: blocks_null_source\n  parameters: {num_outputs: '1',"
          " type: float, vlen: '1'}\n  states: {coordinate: [100,200], rotation: 0,"
          " state: enabled}\n"
        + "- name: f2c\n  id: blocks_float_to_complex\n  parameters: {num_streams: '1',"
          " vlen: '1'}\n  states: {coordinate: [300,120], rotation: 0, state: enabled}\n"
        + _mixer("mix", 500, 120) + _sink("snk", "tx", 700, 120)
        + "connections:\n- [src, '0', f2c, '0']\n- [nq, '0', f2c, '1']\n"
          "- [f2c, '0', mix, '0']\n- [mix, '0', snk, '0']\n") + _FOOT
    res = _import(grc)
    assert res.ok and not res.unknown, res.unknown
    types = [b.type for b in res.project.blocks]
    # ONLY the mixer is placed — f2c + null_source add zero cells.
    assert types == ["ComplexMixerBlock"], types
    nets = _nets(res)
    assert ("PORT:x16_in", "complexmixer.xi") in nets, nets
    assert ("complexmixer.yi", "PORT:x16_out") in nets, nets


# --- flavor 2: complex_to_real (drop Q) ---------------------------------------
def test_complex_to_real_drop_q():
    grc = _HDR + (
        _src("src", "complex", "tx", 100, 100) + _lpf("lpf", 300, 100)
        + "- name: c2r\n  id: blocks_complex_to_real\n  parameters: {vlen: '1'}\n"
          "  states: {coordinate: [500,100], rotation: 0, state: enabled}\n"
        + _gain("g", 4, 660, 100) + _sink("snk", "tx", 820, 100)
        + "connections:\n- [src, '0', lpf, '0']\n- [lpf, '0', c2r, '0']\n"
          "- [c2r, '0', g, '0']\n- [g, '0', snk, '0']\n") + _FOOT
    res = _import(grc)
    assert res.ok and not res.unknown, res.unknown
    types = sorted(b.type for b in res.project.blocks)
    assert types == ["ComplexLowPassFilter", "GainBlock"], types
    nets = _nets(res)
    # the LPF's REAL rail (out_i) drives the gain; Q dropped.
    assert ("complexlowpassfilter.out_i", "gain.sample") in nets, nets


# --- flavor 3: complex_to_float (both rails) ----------------------------------
def test_complex_to_float_both_rails():
    grc = _HDR + (
        _src("src", "complex", "tx", 100, 100) + _lpf("lpf", 300, 100)
        + "- name: c2f\n  id: blocks_complex_to_float\n  parameters: {vlen: '1'}\n"
          "  states: {coordinate: [500,100], rotation: 0, state: enabled}\n"
        + _gain("gi", 2, 660, 60) + _gain("gq", 3, 660, 160)
        + _sink("si", "i", 820, 60) + _sink("sq", "q", 820, 160)
        + "connections:\n- [src, '0', lpf, '0']\n- [lpf, '0', c2f, '0']\n"
          "- [c2f, '0', gi, '0']\n- [c2f, '1', gq, '0']\n"
          "- [gi, '0', si, '0']\n- [gq, '0', sq, '0']\n") + _FOOT
    res = _import(grc)
    assert res.ok and not res.unknown, res.unknown
    types = sorted(b.type for b in res.project.blocks)
    assert types == ["ComplexLowPassFilter", "GainBlock", "GainBlock"], types
    nets = _nets(res)
    # each rail steered to its own downstream: out_i -> one gain, out_q -> the other.
    i_rail = {t for (s, t) in nets if s == "complexlowpassfilter.out_i"}
    q_rail = {t for (s, t) in nets if s == "complexlowpassfilter.out_q"}
    assert i_rail and q_rail and i_rail != q_rail, nets


# --- flavor 4 (#429): TWO real producers -> a DualFloatToComplex BLOCK ---------
def test_two_real_float_to_complex_places_dual_block():
    """A float_to_complex fed by TWO independent real streams (no null_source on Q)
    is auto-inserted as a physical DualFloatToComplexBlock: its port-0 (I) producer
    wires to `i`, its port-1 (Q) producer wires to `q`, and its output wires
    downstream. Contrast the single-real case, which places NO cell."""
    grc = _HDR + (
        _src("srcI", "float", "i", 100, 60)
        + _src("srcQ", "float", "q", 100, 160)
        + "- name: f2c\n  id: blocks_float_to_complex\n  parameters: {num_streams: '2',"
          " vlen: '1'}\n  states: {coordinate: [300,110], rotation: 0, state: enabled}\n"
        + _mixer("mix", 500, 110) + _sink("snk", "tx", 700, 110)
        + "connections:\n- [srcI, '0', f2c, '0']\n- [srcQ, '0', f2c, '1']\n"
          "- [f2c, '0', mix, '0']\n- [mix, '0', snk, '0']\n") + _FOOT
    res = _import(grc)
    assert res.ok and not res.unknown, res.unknown
    types = sorted(b.type for b in res.project.blocks)
    # the f2c became a DualFloatToComplex block; the mixer stays. No extra cells.
    assert types == ["ComplexMixerBlock", "DualFloatToComplexBlock"], types
    nets = _nets(res)
    # find the dual block's instance name
    dual = next(b.name for b in res.project.blocks
                if b.type == "DualFloatToComplexBlock")
    # I producer -> dual.i (port 0), Q producer -> dual.q (port 1).
    assert ("PORT:x16_in", f"{dual}.i") in nets, nets
    assert ("PORT:x16_in", f"{dual}.q") in nets, nets
    # dual output -> the mixer downstream, as a 2-rail COMPLEX PACKET: the recovered I
    # rail (yi) -> mixer.xi AND the recovered Q rail (yq) -> mixer.xq. The dual emits
    # both rails (like ComplexMixer) so a GENUINE 2-input complex consumer keeps Q — it
    # is NOT dropped (the pre-#438 single-`out` dual would have lost the imaginary rail).
    assert (f"{dual}.yi", "complexmixer.xi") in nets, nets
    assert (f"{dual}.yq", "complexmixer.xq") in nets, nets


# --- NEGATIVE gate: a converter that is NOT consumed would place an extra cell -
def test_converter_never_adds_a_cell():
    """The cell-count invariant across all flavors: importing WITH the converters
    places exactly the DSP blocks, never a converter cell."""
    for fn in (test_single_real_float_to_complex,
               test_complex_to_real_drop_q,
               test_complex_to_float_both_rails):
        fn()  # each asserts its own exact placed-type list (no converter type)
