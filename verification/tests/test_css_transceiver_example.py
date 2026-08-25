# SPDX-License-Identifier: GPL-3.0-or-later
"""The CSS (chirp-spread-spectrum) example's gate — ``examples/css_transceiver``.

The example places the WHOLE CSS receive spine on ONE 10x12 array:

    x16_in -> ConjChirpMixer(n=16) -> FFT16 -> ComplexToMagSquared
           -> Delay(1) -> BinArgmax(16) -> x16_out

and drives it with ONE continuous two-segment burst: the framed message
``KYTTAR CSS`` at +10 dB SNR (which must decode exactly) followed by the SAME
message at -10 dB (the on-chip NEGATIVE CONTROL, which must not). One chain,
one stream, one run — the control is measured on the chip, not asserted from a
host-side model.

What is gated here:

  1. the shipped stimulus module's TX goldens are bit-identical to the TX
     BLOCKS' own chip-verified references (never self-consistent only);
  2. the shipped ``.grc`` imports, pins its geometry, routes and builds, and
     the chip output is BIT-EXACT vs the composed integer golden of the five
     RX blocks — per-sample AND fully SATURATED;
  3. the shipped ``.kyt`` (what the user opens in placeKYT) gives the same
     stream as the freshly-imported design;
  4. the decode: SER 0 over the +10 dB segment with the message recovered
     exactly, and SER > 0.2 over the -10 dB control segment;
  5. MUTATIONS that must FAIL — the wrong decode map, the missing Delay(1)
     alignment, and a non-conjugated dechirp reference;
  6. THE USER PATH: the shipped ``.kyt`` hosted exactly as the GUI's
     *Run as GNURadio Server* (port 58950) with the shipped ``.grc``
     GRC-generated and run under the real GNU Radio interpreter, asserting the
     decoded output the user actually sees;
  7. THE TRACES ON SCREEN (``_assert_plotted_traces``, tapped in that same
     run). Asserting the sink stream is NOT enough: this example's chip was
     bit-exact — SER 0, message recovered — while the symbol scope showed a
     smear, because the reference came from a SEPARATE free-running source
     that outran the batch-gated chip stream by 27.9% and slid off it (3 items
     of slip makes 22 of segment A's 24 correct symbols look wrong), and
     because segment B's deliberate garbage was overplotted on segment A's
     lock. So the gate now asserts what is DRAWN: segment A matches its
     reference at every plotted point, segment B visibly does not, the two
     never draw at the same position, and every frame is identical across the
     run. ``test_mutation_display_gate_catches_the_old_broken_plot`` proves
     that gate FAILS on both original defects (INV-4).

Run the user-path test STANDALONE — it binds port 58950 and self-contends with
the other examples' user-path gates under concurrent load::

    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \\
      <venv>/python -m pytest \\
      verification/tests/test_css_transceiver_example.py -q
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT / "placekyt"), str(_ROOT / "runtime" / "python"),
           str(_ROOT / "examples" / "css_transceiver")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_RUNNER = _ROOT / "verification" / "grc_userpath_run.py"
_GR_PYTHON = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")
_PORT = 58950                       # the .grc's baked server_port (GUI default)

CHIP_YAML = _ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml"
pytestmark = pytest.mark.skipif(
    not CHIP_YAML.exists(), reason="chip yaml absent")

import css_transceiver_demo as demo  # noqa: E402

stim = demo.stim
N = demo.N


# --- fixtures -----------------------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def imported(qapp):
    """The shipped .grc imported, geometry pinned, routed and built."""
    return demo.import_and_pnr()


@pytest.fixture(scope="module")
def burst():
    return stim.rx_burst()


# --- 1. the stimulus module IS the blocks' own references ---------------------

def test_stim_symbols_match_mapper_reference():
    """The stim module's bits->symbols is bit-identical to
    ChirpSymbolMapperBlock's own verified reference."""
    from gr_kyttar.placement.blocks.chirp_symbol_mapper_block import (
        ChirpSymbolMapperBlock)

    bits = stim.message_bits()
    ref = [int(w) & 0xFFFF for w in ChirpSymbolMapperBlock("m", m=stim.M)
           .process_reference(np.asarray(bits, dtype=np.uint8))]
    assert stim.message_symbols() == ref
    assert stim.symbols_to_text(ref) == stim.MESSAGE


def test_stim_chirp_matches_generator_reference():
    """The stim module's symbols->chirp waveform is bit-identical to
    ChirpGeneratorBlock's own chip-verified integer reference (the module
    cannot import gr_kyttar — it runs under the GR interpreter — so this
    equality is what keeps its transcription honest)."""
    from gr_kyttar.placement.blocks.chirp_generator_block import (
        ChirpGeneratorBlock)

    syms = stim.framed_symbols()
    ref = list(ChirpGeneratorBlock("g", n=N, m=stim.M)
               .process_reference_q15(syms))
    assert stim.chirp_words(syms) == ref


def test_stim_shapes_are_self_consistent():
    """The burst / word-count / display-trace lengths the .grc's scopes are
    sized from must agree with each other."""
    assert stim.burst_len() == 2 * stim.seg_samples()
    assert stim.n_out_words() == stim.burst_len() // N
    assert len(stim.display_symbols()) == stim.n_out_words()
    assert len(stim.rx_burst()) == stim.burst_len()
    # The .grc's display block is handed ONE segment's transmitted symbols plus
    # the per-segment word width; it rebuilds the reference trace itself (that
    # is what keeps the reference phase-locked to the decode). Those two must
    # tile the output word grid exactly, or the on-screen reference is misframed.
    seg_words = stim.n_out_words() // 2
    assert 2 * seg_words == stim.n_out_words()
    assert 1 + stim.n_data_symbols() == seg_words


# --- 2. the placed chain is bit-exact vs the composed golden ------------------

def test_chain_builds_on_one_chip(imported):
    project, bres, _cat, _ctrl = imported
    assert bres.ok
    used = sum(c.cell_count for c in bres.chips.values())
    assert used <= 120, f"{used} cells exceeds the array"
    assert len(project.blocks) == 5
    # every net routed or abutted — no gaps
    for c in project.connections:
        assert c.route is not None, f"net {c.name} never routed"


def test_chip_stream_bit_exact_saturated(imported, burst):
    """THE WHOLE-CHAIN PROOF: the entire burst queued back to back (one
    continuous run — the real streaming condition), chip output BIT-EXACT vs
    the composed integer golden of the five RX blocks."""
    project, bres, cat, ctrl = imported
    got = demo.run_stream(project, bres, cat, ctrl, burst, saturated=True)
    assert got == demo.golden_rx(burst)
    assert len(got) == stim.n_out_words()


def test_chip_stream_bit_exact_per_sample(imported, burst):
    """The same chain driven PER-SAMPLE (inject-and-flush) — same stream."""
    project, bres, cat, ctrl = imported
    got = demo.run_stream(project, bres, cat, ctrl, burst, saturated=False)
    assert got == demo.golden_rx(burst)


def test_shipped_kyt_parity(qapp, burst):
    """The SHIPPED .kyt — the file a user opens in placeKYT — builds and
    produces the SAME stream as the freshly-imported design."""
    project, bres, cat, ctrl = demo.load_shipped()
    got = demo.run_stream(project, bres, cat, ctrl, burst, saturated=True)
    assert got == demo.golden_rx(burst)


# --- 3. the decode: SER 0 at +10 dB, the on-chip control collapses ------------

def test_decode_ser_and_negative_control(imported, burst):
    """The headline numbers, both measured ON THE CHIP in ONE run: segment A
    (+10 dB) decodes every symbol and recovers the message exactly; segment B
    (-10 dB) — the same chain, the same stream — collapses."""
    project, bres, cat, ctrl = imported
    got = demo.run_stream(project, bres, cat, ctrl, burst, saturated=True)
    seg_a, seg_b = demo.segments(got)

    dec_a, err_a, ser_a, text_a = demo.score(seg_a)
    assert ser_a == 0.0, f"+10 dB segment SER {ser_a} ({err_a} errors)"
    assert text_a == stim.MESSAGE, f"recovered {text_a!r}"

    _dec_b, err_b, ser_b, _text_b = demo.score(seg_b)
    assert ser_b > 0.2, (
        f"the -10 dB on-chip control is too clean ({err_b} errors, SER "
        f"{ser_b}) — the SER metric would be vacuous")

    from kyttar_verify import write_session_report
    write_session_report("CssTransceiverExample", {
        "example": "css_transceiver",
        "n_symbols_per_segment": len(dec_a),
        "snr_good_db": stim.SNR_GOOD_DB, "snr_control_db": stim.SNR_BAD_DB,
        "attenuation": stim.ATTEN,
        "ser_good": ser_a, "ser_control": ser_b,
        "message": stim.MESSAGE, "recovered": text_a,
        "cells": sum(c.cell_count for c in bres.chips.values()),
        "onchip": "the whole CSS receive spine (dechirp + FFT16 + mag^2 + "
                  "align + argmax) on one 10x12 chip, saturated drive",
        "host_side": "TX (mapper + generator integer goldens, chip-verified "
                     "in their own suites) and the numpy channel",
    })


# --- 4. mutations that MUST fail (INV-4) --------------------------------------

def test_mutation_wrong_decode_map_fails(imported, burst):
    """The decode map is s = brev4(index) because FFT16 emits bins in
    bit-reversed order. Decoding the index DIRECTLY (the natural-order
    mistake) must NOT recover the message."""
    project, bres, cat, ctrl = imported
    got = demo.run_stream(project, bres, cat, ctrl, burst, saturated=True)
    seg_a, _ = demo.segments(got)
    tx = stim.framed_symbols()[:stim.n_data_symbols()]
    naive = [int(i) for i in seg_a[1:1 + len(tx)]]      # no brev4
    assert naive != tx, "the gate failed to detect a wrong decode map!"


def test_mutation_missing_alignment_delay_fails(burst):
    """FFT16's latency is 15 == -1 (mod 16): WITHOUT the Delay(1) the argmax
    frames straddle two FFT frames. The no-delay golden must DISAGREE with the
    chip-proven aligned golden."""
    from gr_kyttar.placement.blocks.bin_argmax_block import BinArgmaxBlock
    from gr_kyttar.placement.blocks.complex_mag_block import (
        ComplexToMagSquaredBlock)
    from gr_kyttar.placement.blocks.conj_chirp_mixer_block import (
        ConjChirpMixerBlock)
    from gr_kyttar.placement.blocks.fft16_block import fft16_streaming_reference

    y = ConjChirpMixerBlock("m", n=N).process_reference_q15(
        np.asarray(burst, dtype=complex))
    f = fft16_streaming_reference(y)
    mag = ComplexToMagSquaredBlock("g").process_reference_q15(
        [a for a, _ in f], [b for _, b in f])
    amx = BinArgmaxBlock("a", n=N)
    aligned = [w & 0xFFFF for w in amx.process_reference_q15(
        [0] + list(mag[:-1]))]
    misframed = [w & 0xFFFF for w in amx.process_reference_q15(list(mag))]
    assert aligned != misframed, \
        "the gate failed to detect the missing alignment delay!"
    # and the aligned one is the one that decodes
    tx = stim.framed_symbols()[:stim.n_data_symbols()]
    assert stim.decode(aligned[:stim.seg_samples() // N]) == tx
    assert stim.decode(misframed[:stim.seg_samples() // N]) != tx


def test_mutation_non_conjugated_reference_fails(burst):
    """The dechirp multiplies by the CONJUGATE of the reference up-chirp. A
    non-conjugated (plain) mix doubles the sweep instead of cancelling it, so
    no bin concentrates the symbol and the decode must break."""
    from gr_kyttar.placement.blocks.bin_argmax_block import BinArgmaxBlock
    from gr_kyttar.placement.blocks.complex_mag_block import (
        ComplexToMagSquaredBlock)
    from gr_kyttar.placement.blocks.conj_chirp_mixer_block import (
        ConjChirpMixerBlock)
    from gr_kyttar.placement.blocks.fft16_block import fft16_streaming_reference

    mix = ConjChirpMixerBlock("m", n=N)
    y = mix.process_reference_q15(np.asarray(burst, dtype=complex))
    # the MUTANT: conjugate the mixer OUTPUT, which is algebraically the same
    # as having mixed by the non-conjugated reference (x * ref instead of
    # x * conj(ref)) up to the sign of the residual sweep.
    ymut = [(a, (-demo._s16(b)) & 0xFFFF) for (a, b) in y]

    def spine(pairs):
        f = fft16_streaming_reference(pairs)
        mag = ComplexToMagSquaredBlock("g").process_reference_q15(
            [a for a, _ in f], [b for _, b in f])
        return [w & 0xFFFF for w in BinArgmaxBlock("a", n=N)
                .process_reference_q15([0] + list(mag[:-1]))]

    tx = stim.framed_symbols()[:stim.n_data_symbols()]
    w = stim.seg_samples() // N
    assert stim.decode(spine(y)[:w]) == tx           # the real reference works
    assert stim.decode(spine(ymut)[:w]) != tx, \
        "the gate failed to detect a non-conjugated dechirp reference!"


# --- 5. THE USER PATH: hosted .kyt + the shipped .grc under real GNU Radio ----

def _serve(kyt, wait_secs=900):
    """Host the .kyt exactly as the GUI's 'Run as GNURadio Server' does.

    Port 58950 is the GUI's single default bind, so every example's user-path
    gate wants the SAME port and they self-contend under concurrent load. Wait
    (bounded) for a competing holder to release it rather than failing the gate
    for a reason that has nothing to do with this example; a genuine, lasting
    occupant still fails, loudly, with the wait time named.
    """
    import time

    from engine.catalog import BlockCatalog
    from engine.io.project_io import load_project
    from ui.controller import AppController
    from ui.sim_controller import SimController

    cat = BlockCatalog.from_gr_kyttar()
    ctrl = AppController(catalog=cat)
    ctrl.set_project(load_project(str(kyt)))
    sim = SimController(ctrl)
    deadline = time.time() + wait_secs
    last = None
    while True:
        try:
            bound = sim.start_gnuradio_server(port=_PORT)
            if bound == _PORT:
                return ctrl, sim
            # A busy port surfaces EITHER as OSError(EADDRINUSE) or as a
            # None/other return, depending on where the bind fails; treat both
            # as "not ours yet" and make sure nothing half-started is left.
            last = f"bound {bound} instead"
            try:
                sim.stop_gnuradio_server()
            except Exception:  # noqa: BLE001
                pass
        except OSError as e:            # EADDRINUSE
            last = str(e)
            try:
                sim.stop_gnuradio_server()
            except Exception:  # noqa: BLE001
                pass
        if time.time() >= deadline:
            pytest.fail(
                f"port {_PORT} still busy after {wait_secs}s ({last}) — run "
                f"this user-path gate STANDALONE; it self-contends with the "
                f"other examples' user-path gates on the GUI's default bind")
        time.sleep(3.0)


def _run_flowgraph(grc, secs=90, taps=""):
    r = subprocess.run(
        [_GR_PYTHON, str(_RUNNER), str(grc), str(secs), taps],
        capture_output=True, text=True, timeout=secs + 300,
        env=dict(os.environ, QT_QPA_PLATFORM="offscreen"))
    sinks = {}
    for line in r.stdout.splitlines():
        if line.startswith("SINK "):
            parts = line.split()
            sinks[parts[1]] = [float(x) for x in parts[2:]]
    assert r.returncode == 0 and sinks, (
        f"generated flowgraph failed (rc={r.returncode}):\n"
        f"{r.stdout[-1500:]}\n{r.stderr[-2000:]}")
    return sinks


def _assert_plotted_traces(sinks):
    """THE TRACES THE USER ACTUALLY SEES on the DECODED-vs-TRANSMITTED scope.

    A gate that asserts only the kyttar sink's recovered stream is testing the
    wrong thing: it passes while the PLOT is unusable. That is exactly what
    happened here. The chip was bit-exact (SER 0, message recovered) and the
    scope still showed a smear, for two independent display reasons:

      1. PHASE DRIFT. The transmitted reference came from a SEPARATE
         ``vector_source`` on channel 1. It free-ran while the chip stream was
         gated by the simulator's batch turnaround — measured at +27.9% more
         items over the same run. A time_sink pulls the same count from both
         channels, so the reference SLID against the decode; offset by as few
         as 3 items, 22 of segment A's 24 correct symbols rendered as
         mismatches.
      2. A/B CONFLATION. Segment B's deliberate garbage was drawn on the same
         axis as segment A's perfect lock, with nothing marking which was
         which — 17 of 50 plotted points disagreed BY DESIGN.

    So this asserts the display contract directly:

      * all four channels carry the SAME number of items (they come from ONE
        block driven by ONE stream — drift is impossible by construction, and
        an unequal count would mean it crept back in);
      * segment A's decoded trace matches its reference at EVERY plotted point;
      * segment B's decoded trace visibly does NOT (the negative control is
        still doing its job on screen);
      * A and B never draw at the same position, so neither overplots the
        other and each is identifiable;
      * every scope-sized frame across the whole run is identical to the
        first — the traces stay phase-locked, they do not walk.
    """
    ch = [sinks.get(f"bin_to_sym.{i}") for i in range(4)]
    assert all(c is not None for c in ch), (
        "the display block's four channels were not tapped — the gate cannot "
        "see what the scope draws")
    a_dec, a_ref, b_dec, b_ref = ch

    n = stim.n_out_words()
    assert len({len(c) for c in ch}) == 1, (
        f"the four scope channels carry different item counts "
        f"{[len(c) for c in ch]} — a free-running producer has been "
        f"reintroduced and the reference will slide against the decode")
    assert len(a_dec) >= n, (
        f"only {len(a_dec)} display items — fewer than one {n}-word frame")

    def pairs(dec, ref):
        return [(d, r) for d, r in zip(dec[:n], ref[:n])
                if not np.isnan(d) and not np.isnan(r)]

    n_data = stim.n_data_symbols()

    pa = pairs(a_dec, a_ref)
    assert len(pa) == n_data, (
        f"segment A plots {len(pa)} point-pairs, expected {n_data}")
    bad_a = [(i, d, r) for i, (d, r) in enumerate(pa) if int(d) != int(r)]
    assert not bad_a, (
        f"segment A (+10 dB) must overlay its reference EXACTLY on screen, "
        f"but {len(bad_a)} plotted points differ: {bad_a[:6]}")

    pb = pairs(b_dec, b_ref)
    assert len(pb) == n_data, (
        f"segment B plots {len(pb)} point-pairs, expected {n_data}")
    bad_b = sum(1 for d, r in pb if int(d) != int(r))
    assert bad_b > 0.2 * len(pb), (
        f"segment B (-10 dB) is the on-chip negative control and must VISIBLY "
        f"miss its reference, but only {bad_b}/{len(pb)} plotted points differ")

    overlap = [i for i in range(n)
               if not np.isnan(a_dec[i]) and not np.isnan(b_dec[i])]
    assert not overlap, (
        f"segments A and B draw at the same positions {overlap[:6]} — they "
        f"overplot and a viewer cannot tell which trace is which")

    # Phase lock across the WHOLE run: every frame identical to the first.
    def frames(c):
        return [c[f * n:(f + 1) * n] for f in range(len(c) // n)]

    for name, c in (("A decoded", a_dec), ("A reference", a_ref),
                    ("B decoded", b_dec), ("B reference", b_ref)):
        fr = frames(c)
        first = np.asarray(fr[0])
        for k, f in enumerate(fr[1:], 1):
            assert np.array_equal(np.asarray(f), first, equal_nan=True), (
                f"{name}: scope frame {k} differs from frame 0 — the trace is "
                f"drifting, not holding a stable picture")


def test_mutation_display_gate_catches_the_old_broken_plot():
    """INV-4 for the DISPLAY gate: it must FAIL on the two defects that made a
    bit-exact decode look broken on screen. A display gate never shown to fail
    certifies nothing — and these are not hypothetical, they are what shipped.

    Replays both defects against ``_assert_plotted_traces`` with synthetic
    channels built from the real reference trace.
    """
    n = stim.n_out_words()
    w = n // 2
    ref_seg = ([np.nan] + [float(s) for s in
                           stim.framed_symbols()[:stim.n_data_symbols()]])[:w]
    nan = [np.nan] * w
    reps = 4

    def chans(a_dec_seg, a_ref_seg, b_dec_seg, b_ref_seg):
        return {
            "bin_to_sym.0": (list(a_dec_seg) + nan) * reps,
            "bin_to_sym.1": (list(a_ref_seg) + nan) * reps,
            "bin_to_sym.2": (nan + list(b_dec_seg)) * reps,
            "bin_to_sym.3": (nan + list(b_ref_seg)) * reps,
        }

    collapsed = [np.nan] + [float((int(s) + 7) % 16) for s in
                            stim.framed_symbols()[:stim.n_data_symbols()]]
    collapsed = collapsed[:w]

    # Sanity: the HEALTHY shape passes, so the failures below are real.
    _assert_plotted_traces(chans(ref_seg, ref_seg, collapsed, ref_seg))

    # MUTATION 1 — PHASE DRIFT: segment A's reference slid by 3 words against a
    # perfectly-decoded trace (the +27.9% free-running vector_source defect).
    # The blank stays put so this isolates the VALUE drift: same plotted
    # points, wrong values — the smear the owner reported.
    body = [x for x in ref_seg[1:]]
    slid = [ref_seg[0]] + [body[(i + 3) % len(body)] for i in range(len(body))]
    with pytest.raises(AssertionError, match="overlay its reference EXACTLY"):
        _assert_plotted_traces(chans(ref_seg, slid, collapsed, ref_seg))

    # MUTATION 2 — A/B CONFLATION: both segments drawn across the whole sweep,
    # so segment B's garbage overplots segment A's lock.
    both = {
        "bin_to_sym.0": (list(ref_seg) + list(ref_seg)) * reps,
        "bin_to_sym.1": (list(ref_seg) + list(ref_seg)) * reps,
        "bin_to_sym.2": (list(collapsed) + list(collapsed)) * reps,
        "bin_to_sym.3": (list(ref_seg) + list(ref_seg)) * reps,
    }
    with pytest.raises(AssertionError, match="overplot|point-pairs"):
        _assert_plotted_traces(both)

    # MUTATION 3 — UNEQUAL CHANNEL LENGTHS: the signature of a reintroduced
    # free-running reference producer.
    uneven = chans(ref_seg, ref_seg, collapsed, ref_seg)
    uneven["bin_to_sym.1"] = uneven["bin_to_sym.1"] + [0.0] * 7
    with pytest.raises(AssertionError, match="different item counts"):
        _assert_plotted_traces(uneven)

    # MUTATION 4 — A DRIFTING (non-repeating) TRACE: frames must be stable.
    drifting = chans(ref_seg, ref_seg, collapsed, ref_seg)
    drifting["bin_to_sym.0"] = (
        list(ref_seg) + nan + list(slid) + nan
        + (list(ref_seg) + nan) * (reps - 2))
    with pytest.raises(AssertionError, match="drifting"):
        _assert_plotted_traces(drifting)


@pytest.mark.skipif(not os.path.exists(_GR_PYTHON),
                    reason="GNU Radio interpreter absent")
def test_shipped_grc_user_path(qapp):
    """THE DELIVERABLE'S CORE GATE — the exact workflow the user follows:
    open the .kyt in placeKYT, Run as GNURadio Server, open the .grc in GNU
    Radio Companion, press Run.

    Here: the SHIPPED .kyt is hosted on port 58950, the SHIPPED .grc is
    GRC-generated and executed by the real GNU Radio interpreter, and the
    kyttar sink's recovered stream is asserted to decode the message.

    A complex-input chain egresses RAW word floats, so each recovered item IS
    the argmax bin index (0..15) — no q15 rescale. The sink loops its genuine
    one-batch result (``server_repeat=True``), so repetition integrity is
    asserted too: everything after the first batch must be a clean repeat of
    it, never a fabricated or stale stream.

    Run this test STANDALONE (it binds 58950).
    """
    grc = _ROOT / "examples" / "css_transceiver" / "css_transceiver.grc"
    ctrl, sim = _serve(demo.KYT_PATH)
    try:
        # Tap the four DISPLAY channels too, so the same run proves both the
        # recovered stream AND the traces the user actually looks at.
        sinks = _run_flowgraph(
            grc, taps="bin_to_sym.0,bin_to_sym.1,bin_to_sym.2,bin_to_sym.3")
    finally:
        sim.stop_gnuradio_server()

    _assert_plotted_traces(sinks)

    idx = [int(round(v)) & 0xFFFF for v in sinks.get("rx_sink", [])]
    n = stim.n_out_words()
    assert len(idx) >= n, (
        f"the shipped flowgraph recovered only {len(idx)}/{n} index words")
    first = idx[:n]
    assert first == demo.golden_rx(stim.rx_burst()), (
        "the stream recovered through the REAL client stack is not the "
        "chip-proven golden")

    seg_a, seg_b = demo.segments(first)
    _dec_a, err_a, ser_a, text_a = demo.score(seg_a)
    assert ser_a == 0.0 and text_a == stim.MESSAGE, (
        f"user path decoded {text_a!r} with SER {ser_a} ({err_a} errors)")
    _dec_b, _err_b, ser_b, _text_b = demo.score(seg_b)
    assert ser_b > 0.2, (
        f"the -10 dB control is too clean through the user path ({ser_b})")

    # server_repeat integrity: every later full batch is a clean repetition.
    for r in range(1, len(idx) // n):
        assert idx[r * n:(r + 1) * n] == first, \
            f"repetition {r} diverges — not a genuine looped batch"
