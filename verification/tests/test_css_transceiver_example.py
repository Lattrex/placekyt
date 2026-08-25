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
     decoded output the user actually sees.

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


def _run_flowgraph(grc, secs=90):
    r = subprocess.run(
        [_GR_PYTHON, str(_RUNNER), str(grc), str(secs)],
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
        sinks = _run_flowgraph(grc)
    finally:
        sim.stop_gnuradio_server()

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
