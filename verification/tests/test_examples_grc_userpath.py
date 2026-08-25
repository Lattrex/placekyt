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
           str(_ROOT / "examples" / "gru_classifier"),
           str(_ROOT / "examples" / "fft128_2p2s"),
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


def test_gru_classifier_shipped_grc_user_path(qapp):
    """THE OWNER'S WORKFLOW, end to end: host the SHIPPED
    ``gru_classifier.kyt`` on the GUI's default server port, GRC-generate and
    run the SHIPPED ``gru_classifier.grc``, and assert the class stream the
    ``cls_scope`` actually receives.

    This example shipped BROKEN precisely because this gate did not exist. Its
    headless suite was green — ``run_on_chip`` reads the build's
    ``input_landings`` itself and drives the three ingress arms correctly — while
    the HOSTED path returned literally nothing. Three independent faults, each
    invisible to every pre-existing gate:

    1. **The ``.kyt`` carried no ``stream_id``/``out_tag``.** The server resolves
       an input net's injection landing only for nets that carry a ``stream_id``
       (``engine.port_config.stream_targets``); with none it fell back to the
       single-net ``input_port_config``, which resolves the FIRST arm only. The
       ZeroCrossingRate arm was never injected, so ``FeaturePairJoin`` never
       rendezvoused and the GRU never ran. Observed: ``stream_targets resolved:
       {}`` and ``15360 samples -> 0 recovered``.
    2. **The live bridge could not drive a multi-arm COMPLEX stream.** Its
       fan-out path was gated on ``xq is None`` (real-rail joins only) and its
       landings carried one address each, so even a correctly tagged complex
       stream would have driven one arm. Fixed in ``engine.sim_bridge``.
    3. **The ``.grc`` rescaled a RAW stream by x32768.** A complex-input chain
       returns RAW word floats (``output_words='auto'`` ties raw to
       ``complex_in``), so the class index 0..3 already arrives as 0.0..3.0. The
       x32768 drove every sample to 0/32768/65536/98304, far off the scope's
       ``[-0.5, 3.5]`` axis.

    Asserted here: the FIRST burst is bit-exact to the shipped golden, every
    value lands inside the scope's axis, ``server_repeat`` loops it cleanly, and
    each of the four segments votes for its own class.
    """
    import json

    grc = _ROOT / "examples" / "gru_classifier" / "gru_classifier.grc"
    kyt = _ROOT / "examples" / "gru_classifier" / "gru_classifier.kyt"
    gold_path = (_ROOT / "examples" / "gru_classifier"
                 / "gru_classifier_golden.json")
    gold_doc = json.loads(gold_path.read_text())
    gold = [int(w) for w in gold_doc["class_words"]]
    classes = list(gold_doc["classes"])

    # 90s is comfortably past the ~60s the 15360-sample burst takes through 102
    # cells; the flowgraph then keeps looping the recovered batch
    # (server_repeat), so a longer run only adds repetitions.
    ctrl, sim = _serve(kyt)
    try:
        sinks = _run_flowgraph(grc, secs=90)
    finally:
        sim.stop_gnuradio_server()

    # The tap sits on the kyttar sink's OWN output — with the x32768 removed
    # that is exactly the stream cls_scope draws.
    vals = sinks.get("chip_sink", [])
    n = len(gold)
    assert len(vals) >= n, (
        f"the shipped flowgraph recovered only {len(vals)}/{n} class words "
        f"through the hosted server — the chain is starved, not merely wrong "
        f"(this is the shipped-broken signature: 0 recovered)")

    got = [int(round(v)) for v in vals]
    first = got[:n]
    assert first == gold, (
        f"the class stream through the SHIPPED user path diverges from the "
        f"shipped golden: "
        f"{sum(1 for a, b in zip(first, gold) if a != b)}/{n} windows differ")

    # DISPLAY: every value must land inside the scope's y-axis (-0.5 .. 3.5), or
    # the window paints a flat line off-scale even though the chip is correct.
    assert set(first) <= {0, 1, 2, 3}, (
        f"class values outside the scope's 0..3 axis: "
        f"{sorted(set(first) - {0, 1, 2, 3})[:8]} — a rescale crept back in "
        f"front of a RAW (complex-input) stream")
    assert all(-0.5 < v < 3.5 for v in vals[:n]), (
        "recovered values fall outside the cls_scope y-axis")

    # server_repeat=True loops the genuine one-batch result: assert repetition
    # integrity (real data looped, never a fabricated stream).
    for r in range(1, len(got) // n):
        assert got[r * n:(r + 1) * n] == first, \
            f"server_repeat repetition {r} diverges from the first burst"

    # THE STORY THE SCOPE TELLS: the stimulus walks the four classes in order,
    # so each segment must vote for its own class (settling window skipped —
    # the GRU's recurrence needs a few steps after each class boundary).
    seg = n // len(classes)
    for ci, name in enumerate(classes):
        w = first[ci * seg + 30:(ci + 1) * seg]
        vote = max(set(w), key=w.count)
        assert vote == ci, (
            f"segment {ci} ({name}) voted {vote} ({classes[vote]}) through the "
            f"shipped user path")


def test_fft128_2p2s_shipped_grc_user_path(qapp):
    """THE OWNER'S WORKFLOW for the 2P2S FFT128, end to end: host the SHIPPED
    ``fft128_2p2s.kyt`` (a FOUR-die board design, so placeKYT hosts the
    MULTI-CHIP server) on the GUI's default port, GRC-generate and run the
    SHIPPED ``fft128_2p2s.grc``, and assert the bins the scope actually
    receives.

    This is the multi-chip analogue of the transceiver user-path gates, and it
    covers ground the headless gate cannot: the headless suite drives
    ``MultiChipSimEngine`` directly, whereas THIS path goes through the GRC
    client stack, the multi-chip server's stream_target resolution, and the
    chain-tail tag demux. A ``.kyt`` missing its ``stream_id``/``out_tag``
    passes every headless gate and returns literally nothing here.

    THE THREE DEFECTS IT FOUND, all invisible headless:

    1. A chain continuing across the CARRIER WIRE resolved ``out_tag=None``
       (``stream_targets`` walks block -> block within ONE chip; here the
       tagged egress net belongs to the tail die on the next chip), so the
       host demux dropped every word the tail emitted.
    2. The multi-chip demux kept only ONE tag of the COMPLEX PAIR — a complex
       exit cell emits I then Q on ``(out_tag, out_tag+1)``, and matching
       ``out_tag`` alone returned the stream at HALF LENGTH with the
       imaginary part gone.
    3. The ``.grc`` left ``output_words`` on "auto" (raw iff complex-in) for a
       chain whose output is a Q15 VALUE, so the sink emitted raw +-30000 word
       floats. Every consumer applies the q15/32768 convention, under which
       those ALIAS (14746.0 -> 0x0000, 11469.0 -> 0x8000) — and only the
       non-zero bins alias, so the burst came back looking nearly right.

    Asserted: the sink recovers the FULL complex stream off chain A's tail
    and it is BIT-EXACT against the whole-transform reference for the .grc's
    own embedded stimulus. Bit-exactness is the right bar here rather than a
    liveliness heuristic — this .grc drives two pure tones at exactly bins 9
    and 37 of 128, so a CORRECT transform is nearly all zeros (measured: 3
    distinct values over 768 words). "Looks busy" would fail on a correct
    chain; "equals the reference" cannot pass on a broken one."""
    import cmath

    grc = _ROOT / "examples" / "fft128_2p2s" / "fft128_2p2s.grc"
    kyt = _ROOT / "examples" / "fft128_2p2s" / "fft128_2p2s.kyt"
    if not kyt.exists() or not grc.exists():
        pytest.skip("fft128_2p2s example not generated (run build_kyt.py)")
    import fft128_2p2s as EX

    ctrl, sim = _serve(kyt)
    try:
        assert len(ctrl.project.chips) == 4, (
            "the 2P2S design must host as a FOUR-die multi-chip server")
        sinks = _run_flowgraph(grc, secs=90)
    finally:
        sim.stop_gnuradio_server()

    got = _words(sinks.get("kyt_sink", []))
    assert got, (
        "the shipped flowgraph recovered NOTHING off chain A's tail — the "
        "multi-chip server resolved no stream target (check the .kyt's "
        "stream_id on the input nets and out_tag on the egress net)")
    assert all(0 <= w <= 0xFFFF for w in got), "recovered non-16-bit words"

    # The .grc's OWN embedded stimulus: two complex tones, 384 samples.
    n = 384
    stim = []
    for k in range(n):
        c = (0.45 * cmath.exp(2j * cmath.pi * 9 * k / 128)
             + 0.35 * cmath.exp(2j * cmath.pi * 37 * k / 128))
        stim.append((EX.q15(c.real), EX.q15(c.imag)))
    ref = EX.reference(stim)

    # BOTH rails must arrive: 2 words per sample. A demux that kept only the
    # I tag returns exactly half of this. This is the assertion that pins the
    # complex-pair fix, and it PASSES.
    assert len(got) >= 2 * n, (
        f"recovered {len(got)} words for {n} samples, expected >= {2 * n} — "
        "the complex pair is arriving half-length (only one tag demuxed)")

    # THE THIRD DEFECT, and the one that kept this gate xfail: the .grc left
    # ``output_words`` on "auto", which ties RAW-int16 output to complex_in.
    # That is the BIT-PACKING receiver convention (a slicer's decoded bit
    # lives in the word LSB, which Q15 scaling would crush). This chain is the
    # exact opposite — its output is a Q15 VALUE, the transform's bins. So the
    # sink emitted raw +-30000 word floats while every consumer applied the
    # documented q15/32768 convention, and ``round(w * 32768) & 0xFFFF``
    # aliases: 14746.0 -> 0x0000 and 11469.0 -> 0x8000. Only the NON-ZERO bins
    # alias (0.0 survives), so a two-tone transform came back looking almost
    # right — 4 samples wrong out of 384 — rather than obviously broken. The
    # scope told the same story: raw +-30000 against its -1..1 axis is a flat
    # off-scale line. Fixed in gen_grc.py with output_words="q15", the same
    # fix (and the same failure class) as the LMS equalizer's
    # missing-constellation report.
    pairs = [(got[2 * k], got[2 * k + 1]) for k in range(n)]
    bad = [k for k in range(n) if pairs[k] != ref[k]]
    assert not bad, (
        f"the hosted burst is not this stimulus's transform — "
        f"{len(bad)}/{n} samples differ (first at {bad[0]}: "
        f"got {(hex(pairs[bad[0]][0]), hex(pairs[bad[0]][1]))} "
        f"want {(hex(ref[bad[0]][0]), hex(ref[bad[0]][1]))})")

    # NOT VACUOUS: this .grc's two tones land on exactly four non-zero
    # samples of the 384. If the sink ever returned all zeros the comparison
    # above would pass while proving nothing.
    nz = [k for k in range(n) if ref[k] != (0, 0)]
    assert len(nz) == 4, f"the reference has {len(nz)} non-zero samples, not 4"
    assert all(pairs[k] != (0, 0) for k in nz), (
        "the recovered burst is zero where the transform has energy — a dead "
        "chain would pass the bit-exact comparison on the zeros alone")

    # server_repeat=True loops the genuine one-batch result: every later full
    # burst must be a clean repetition of it (real data looped, not a
    # fabricated or drifting stream).
    for r in range(1, min(8, len(got) // (2 * n))):
        seg = got[r * 2 * n:(r + 1) * 2 * n]
        assert seg == got[:2 * n], (
            f"server_repeat repetition {r} diverges from the first burst")


