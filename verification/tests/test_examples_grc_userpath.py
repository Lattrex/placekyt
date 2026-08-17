# SPDX-License-Identifier: GPL-3.0-or-later
"""USER-PATH gate for the duplex transceivers: host the SHIPPED .kyt exactly
as the GUI's "Run as GNURadio Server" does (port 58950 — the .grc's baked
server_port), GRC-generate the SHIPPED .grc, run the generated flowgraph
under the real GNU Radio interpreter, and assert on what the kyttar sinks
actually recovered.

This is the gate the 2026-08-10 audit demanded: the GR-client-loop tests use
their OWN hand-written client scripts, so a shipped flowgraph whose RX
stimulus was a silent placeholder (``rx_sig = [0.0]*64`` — the "I don't see
decoded characters" report) passed every existing gate while showing the
user nothing. Here the stimulus IS the .grc's, end to end:

  * CW: TX keys 'CQ CQ DE KYTTAR' bit-exact vs the keyer golden while RX
    decodes the .grc's embedded keyed envelope back to 'RST59973';
  * PSK31: TX is sample-exact vs the PSK31 golden while RX decodes the
    .grc's embedded soft-symbol burst back to 'R 599 73'.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT / "placekyt"), str(_ROOT / "runtime" / "python"),
           str(_ROOT / "examples" / "cw_transceiver"),
           str(_ROOT / "examples" / "psk31_transceiver"),
           str(_ROOT / "examples" / "robust_rx"),
           str(_ROOT / "examples" / "complex_math")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_RUNNER = _ROOT / "verification" / "grc_userpath_run.py"
_GR_PYTHON = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")
_PORT = 58950     # the .grcs bake the GUI's default bind

pytestmark = pytest.mark.skipif(
    not os.path.exists(_GR_PYTHON), reason="GNU Radio interpreter absent")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _serve(kyt):
    from engine.catalog import BlockCatalog
    from engine.io.project_io import load_project
    from ui.controller import AppController
    from ui.sim_controller import SimController

    cat = BlockCatalog.from_gr_kyttar()
    ctrl = AppController(catalog=cat)
    ctrl.set_project(load_project(str(kyt)))
    sim = SimController(ctrl)
    bound = sim.start_gnuradio_server(port=_PORT)
    assert bound == _PORT, f"port 58950 busy (bound {bound})"
    return ctrl, sim


def _run_flowgraph(grc, secs=60):
    r = subprocess.run(
        [_GR_PYTHON, str(_RUNNER), str(grc), str(secs)],
        capture_output=True, text=True, timeout=secs + 240,
        env=dict(os.environ, QT_QPA_PLATFORM="offscreen"))
    sinks = {}
    for line in r.stdout.splitlines():
        if line.startswith("SINK "):
            parts = line.split()
            sinks[parts[1]] = [float(x) for x in parts[2:]]
    assert r.returncode == 0 and sinks, (
        f"generated flowgraph failed (rc={r.returncode}):\n"
        f"{r.stdout[-1000:]}\n{r.stderr[-1500:]}")
    return sinks


def _words(floats):
    """kyttar_sink emits the recovered stream as q15/32768 floats — undo the
    scaling back to the raw 16-bit words (the CLIENT_Q15 convention)."""
    return [int(round(v * 32768.0)) & 0xFFFF for v in floats]


def test_cw_transceiver_shipped_grc_user_path(qapp):
    from cw_transceiver_demo import KYT_PATH, keyed_envelope

    grc = _ROOT / "examples" / "cw_transceiver" / "cw_transceiver.grc"
    ctrl, sim = _serve(KYT_PATH)
    try:
        sinks = _run_flowgraph(grc)
    finally:
        sim.stop_gnuradio_server()
    tx = _words(sinks.get("tx_sink", []))
    gold = keyed_envelope("CQ CQ DE KYTTAR")
    assert tx == gold, (
        f"TX not bit-exact through the shipped flowgraph "
        f"({len(tx)} vs {len(gold)} samples)")
    # The RX display sink LOOPS the genuine one-batch result (server_repeat=True
    # — a QT time sink strands the tail of a finite stream, so an 8-char burst
    # can never paint without the loop). Assert the decoded text AND that the
    # loop is a clean repetition of it (data integrity, not a fake stream).
    rx = "".join(chr(w & 0x7F) for w in _words(sinks.get("rx_sink", [])) if w)
    want = "RST59973"
    assert len(rx) >= len(want), f"RX decoded only {rx!r}"
    reps = -(-len(rx) // len(want))
    assert rx == (want * reps)[:len(rx)],         f"RX decoded {rx[:32]!r}... (want repetitions of {want!r})"


def test_psk31_transceiver_shipped_grc_user_path(qapp):
    from psk31_transceiver_demo import KYT_PATH
    from psk31_tx_golden import golden_tx_q15

    grc = _ROOT / "examples" / "psk31_transceiver" / "psk31_transceiver.grc"
    ctrl, sim = _serve(KYT_PATH)
    try:
        sinks = _run_flowgraph(grc)
    finally:
        sim.stop_gnuradio_server()
    tx = _words(sinks.get("tx_sink", []))
    gold = [int(v) & 0xFFFF
            for v in golden_tx_q15("CQ CQ DE KYTTAR", sps=8, amplitude=1.0)]
    assert tx == gold, (
        f"TX not sample-exact through the shipped flowgraph "
        f"({len(tx)} vs {len(gold)} samples)")
    # Looping display sink (server_repeat=True — see the CW test): assert the
    # decoded text and clean repetition.
    rx = "".join(chr(w & 0x7F) for w in _words(sinks.get("rx_sink", [])) if w)
    want = "R 599 73"
    assert len(rx) >= len(want), f"RX decoded only {rx!r}"
    reps = -(-len(rx) // len(want))
    assert rx == (want * reps)[:len(rx)], \
        f"RX decoded {rx[:32]!r}... (want repetitions of {want!r})"


def test_robust_rx_shipped_grc_user_path(qapp):
    """Host the SHIPPED robust_rx.kyt, run the SHIPPED .grc's generated top
    block: the 'rx' sink recovers BER 0 at foff=0.18 while the 'ctl' sink
    (Costas-only) fails, and both display sinks loop their genuine one-batch
    result cleanly (server_repeat=True — repetition integrity asserted, not
    just presence). Complex-input chains emit RAW word floats, so the bit
    words are read directly."""
    from robust_rx_demo import CTL_FAIL_BER, KYT_PATH, chain_ber, stim

    grc = _ROOT / "examples" / "robust_rx" / "robust_rx.grc"
    ctrl, sim = _serve(KYT_PATH)
    try:
        sinks = _run_flowgraph(grc)
    finally:
        sim.stop_gnuradio_server()
    bits = stim.tx_bits()
    n = stim.n_rx_bits()
    for sink_name, expect_lock in (("rx_sink", True), ("ctl_sink", False)):
        w = [int(round(v)) & 0xFFFF for v in sinks.get(sink_name, [])]
        assert len(w) >= n, f"{sink_name} recovered only {len(w)}/{n}"
        first = w[:n]
        ber = chain_ber(first, bits)
        if expect_lock:
            assert ber == 0.0, f"{sink_name} BER {ber}"
        else:
            assert ber > CTL_FAIL_BER, (
                f"negative control void through the shipped user path: {ber}")
        # server_repeat integrity: everything after the first batch is a
        # clean repetition of it (genuine data looped, not a fake stream).
        reps = len(w) // n
        for r in range(1, reps):
            assert w[r * n:(r + 1) * n] == first, \
                f"{sink_name}: repetition {r} diverges"


def test_complex_math_shipped_grc_user_path(qapp):
    """Host the SHIPPED complex_math.kyt, run the SHIPPED .grc's generated
    top block: all three sinks recover their block's reference stream
    BIT-EXACTLY (interleaved I/Q, q15 float convention) with clean
    server_repeat repetition."""
    from complex_math_demo import KYT_PATH, references

    grc = _ROOT / "examples" / "complex_math" / "complex_math.grc"
    ctrl, sim = _serve(KYT_PATH)
    try:
        sinks = _run_flowgraph(grc)
    finally:
        sim.stop_gnuradio_server()
    refs = references()
    for sink_name, ref_name in (("sum_sink", "sum"), ("diff_sink", "diff"),
                                ("prod_sink", "prod")):
        w = [v - 0x10000 if v & 0x8000 else v
             for v in _words(sinks.get(sink_name, []))]
        ref = refs[ref_name]
        n = len(ref)
        assert len(w) >= n, f"{sink_name} recovered only {len(w)}/{n}"
        assert w[:n] == ref, f"{sink_name} diverges from the block reference"
        reps = len(w) // n
        for r in range(1, reps):
            assert w[r * n:(r + 1) * n] == ref, \
                f"{sink_name}: repetition {r} diverges"


def test_lms_equalizer_shipped_grc_user_path(qapp):
    """The LMS demo's DISPLAY path, end to end: host the shipped .kyt, run the
    GRC-generated flowgraph (CONTINUOUS repeat-burst mode), and assert:

      * the FIRST burst through the real client stack is BIT-EXACT to the
        verified equalizer reference (as interleaved I,Q q15 floats);
      * every LATER full burst is the bit-exact reference of a ROTATION of
        the stimulus — in repeat mode the source keeps consuming the
        repeating vector during a dispatch, so subsequent burst windows
        start mid-vector (any window is a valid cold-started convergence;
        the display story is identical). This proves everything painted is
        a genuine chip-equalized stream, not garbage or a stale replay.
    """
    import numpy as np

    sys.path.insert(0, str(_ROOT / "examples" / "lms_equalizer"))
    from lms_eq_demo import IQ_STIM, KYT_PATH, reference_output

    grc = _ROOT / "examples" / "lms_equalizer" / "lms_equalizer.grc"
    ctrl, sim = _serve(KYT_PATH)
    try:
        sinks = _run_flowgraph(grc)
    finally:
        sim.stop_gnuradio_server()
    got = _words(sinks.get("ksink", []))
    want = [w & 0xFFFF for w in reference_output(IQ_STIM)]
    n = len(want)
    assert len(got) >= n, (
        f"recovered only {len(got)}/{n} words through the shipped flowgraph")
    assert got[:n] == want, (
        "first burst through the shipped flowgraph diverges from the "
        "verified equalizer reference")
    # Later full bursts: identify each burst's stimulus rotation by its head
    # words, then verify the WHOLE burst bit-exact against that rotation's
    # reference.
    arr = np.array(IQ_STIM)
    head_to_rot = {}
    for r in range(len(IQ_STIM)):
        ref = [w & 0xFFFF for w in
               reference_output([complex(c) for c in np.roll(arr, -r)])]
        head_to_rot.setdefault(tuple(ref[:6]), (r, ref))
    for b in range(1, len(got) // n):
        burst = got[b * n:(b + 1) * n]
        key = tuple(burst[:6])
        assert key in head_to_rot, (
            f"burst {b} matches NO rotation of the stimulus — not a genuine "
            "chip-equalized stream")
        _r, ref = head_to_rot[key]
        assert burst == ref, (
            f"burst {b} (stimulus rotation {_r}) diverges from its reference")
