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
     exactly, and a SER inside the derived control band over the -10 dB
     segment (see ``_SER_B_MIN`` / ``_SER_B_MAX`` for why those two numbers:
     the floor rejects a control that has started decoding, the ceiling
     rejects a dead chain emitting a constant);
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
  8. THE VERDICT LAYOUT ITSELF. A third display defect outlived the first two:
     A and B split onto four traces of ONE scope is arithmetically right and
     still unreadable, because a single axis where half the points lock and
     half scatter reads as "half of it is broken". It was reported verbatim as
     "the +10 dB works flawlessly but the -10 dB doesn't work at all" — a
     description of the demo behaving exactly as designed. The plot now gives
     each segment its OWN scope carrying its OWN verdict, and publishes each
     segment's MEASURED SER as a live number. ``test_display_layout_is_two_
     verdict_panels_plus_ser`` pins that structure in the shipped ``.grc``
     (scopes, titles, wiring) so it cannot silently collapse back into one
     ambiguous axis, and ``_assert_plotted_traces`` now also asserts the two
     SER channels read the numbers the panels claim.

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

_GRC = _ROOT / "examples" / "css_transceiver" / "css_transceiver.grc"

# --- the segment-B (negative control) SER BAND, and why it is these numbers ---
#
# Segment B is noise-driven, so its SER is asserted as a RANGE, never an exact
# count. Both ends are derived from what a DIFFERENT failure would score on this
# exact 24-symbol frame, not from the observed value:
#
#   FLOOR 0.40 — a working link scores 0 (segment A does, in the same run). Any
#     partially-working chain would land near 0, not near 0.4. 0.40 is double the
#     old "> 0.2" vacuity bar and leaves the measured 0.625 a wide margin, while
#     still failing loudly if the control ever quietly starts decoding.
#
#   CEILING 0.75 (exclusive) — the interesting failure at the TOP is a chain that
#     has stopped computing and emits a CONSTANT. Scored against this frame, the
#     16 possible stuck-at-k streams score 0.7500 (k=5), 0.7917 (k=0, k=4) and up
#     to 1.0000; the cheapest of them is 0.75. A ceiling strictly below 0.75
#     therefore rejects EVERY constant-output chain, which a bare "SER is high"
#     assertion would happily accept as a healthy negative control. Uniform-random
#     guessing over a 16-ary alphabet averages 0.9375 and effectively never drops
#     below 0.667, so the band also says something real about segment B: it is not
#     guessing — the -10 dB decode retains partial signal and beats chance.
#
# Measured on the shipped burst (pinned seeds): 15 errors of 24 -> SER 0.6250.
_SER_B_MIN = 0.40
_SER_B_MAX = 0.75

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
    assert _SER_B_MIN <= ser_b < _SER_B_MAX, (
        f"the -10 dB on-chip control scored {err_b} errors, SER {ser_b:.4f}, "
        f"outside the derived band [{_SER_B_MIN}, {_SER_B_MAX}) — below the "
        f"floor it has started decoding and segment A's SER 0 is vacuous; at "
        f"or above the ceiling the chain is emitting a constant, not noise")

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
    """THE TRACES THE USER ACTUALLY SEES on the two per-segment verdict scopes.

    A gate that asserts only the kyttar sink's recovered stream is testing the
    wrong thing: it passes while the PLOT is unusable. That is exactly what
    happened here, three times over — the chip was bit-exact (SER 0, message
    recovered) every time:

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
      3. THE CONTROL READ AS A DEFECT. Fixing 1 and 2 with four traces on ONE
         axis made the arithmetic right and the MEANING no clearer: an axis
         where half the points lock and half scatter reads as "half of it is
         broken". Each segment now owns a scope, each scope carries its own
         verdict, and each segment's measured SER is published as a number.

    So this asserts the display contract directly:

      * all SIX channels carry the SAME number of items (they come from ONE
        block driven by ONE stream — drift is impossible by construction, and
        an unequal count would mean it crept back in);
      * segment A's decoded trace matches its reference at EVERY plotted point;
      * segment B's decoded trace visibly does NOT, with its SER inside the
        derived band ``[_SER_B_MIN, _SER_B_MAX)`` (see that constant for why
        those two numbers and not others);
      * A and B never draw at the same position, so neither overplots the
        other and each is identifiable;
      * the two SER readouts agree with the traces they sit beside — the panel
        titles' claims are the measured numbers, not decoration;
      * every scope-sized frame across the whole run is identical — the traces
        stay phase-locked, they do not walk.
    """
    ch = [sinks.get(f"bin_to_sym.{i}") for i in range(6)]
    assert all(c is not None for c in ch), (
        "the display block's six channels were not tapped — the gate cannot "
        "see what the two verdict scopes and the SER readout draw")
    a_dec, a_ref, b_dec, b_ref, a_ser, b_ser = ch

    n = stim.n_out_words()
    assert len({len(c) for c in ch}) == 1, (
        f"the six display channels carry different item counts "
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
        f"but {len(bad_a)} of {len(pa)} plotted points differ: {bad_a[:6]}")

    pb = pairs(b_dec, b_ref)
    assert len(pb) == n_data, (
        f"segment B plots {len(pb)} point-pairs, expected {n_data}")
    bad_b = sum(1 for d, r in pb if int(d) != int(r))
    ser_b_plotted = bad_b / len(pb)
    assert _SER_B_MIN <= ser_b_plotted < _SER_B_MAX, (
        f"segment B (-10 dB) is the on-chip negative control: on screen it "
        f"must MISS its reference across the derived band "
        f"[{_SER_B_MIN}, {_SER_B_MAX}), but {bad_b}/{len(pb)} plotted points "
        f"differ (SER {ser_b_plotted:.4f}). Below the floor the control has "
        f"started decoding and segment A's SER 0 means nothing; at or above "
        f"the ceiling the chain is emitting a constant (the cheapest stuck-at "
        f"stream scores exactly {_SER_B_MAX} on this frame), which is a dead "
        f"chain, not a noisy one")

    overlap = [i for i in range(n)
               if not np.isnan(a_dec[i]) and not np.isnan(b_dec[i])]
    assert not overlap, (
        f"segments A and B draw at the same positions {overlap[:6]} — they "
        f"overplot and a viewer cannot tell which trace is which")

    # THE NUMBERS BESIDE THE PANELS. Each scope's title states a verdict; the
    # SER readout must be that verdict MEASURED, or the titles are decoration.
    # Read the last complete frame: the first frame is the warm-up pass, before
    # either segment has been seen end to end.
    assert len(a_ser) >= 2 * n, (
        f"only {len(a_ser)} SER items — need at least two {n}-word frames to "
        f"read a settled value (the first frame is the warm-up pass)")
    last = slice(-n, None)
    for name, c in (("A", a_ser), ("B", b_ser)):
        vals = sorted({round(float(v), 6) for v in c[last] if not np.isnan(v)})
        assert len(vals) == 1, (
            f"the segment {name} SER readout is not settled over a whole "
            f"frame — it shows {vals[:6]}. A number that swings mid-segment "
            f"is unreadable; it must hold the last COMPLETE pass")
    ser_a_shown = float(a_ser[-1])
    ser_b_shown = float(b_ser[-1])
    assert ser_a_shown == 0.0, (
        f"the segment A panel claims SER 0.00 but its readout shows "
        f"{ser_a_shown:.4f}")
    assert _SER_B_MIN <= ser_b_shown < _SER_B_MAX, (
        f"the segment B readout shows {ser_b_shown:.4f}, outside the derived "
        f"control band [{_SER_B_MIN}, {_SER_B_MAX})")
    assert abs(ser_b_shown - ser_b_plotted) < 1e-6, (
        f"the segment B SER readout ({ser_b_shown:.4f}) disagrees with the "
        f"mismatches actually PLOTTED on its panel ({ser_b_plotted:.4f}) — "
        f"the number and the picture must be the same measurement")

    # Phase lock across the WHOLE run: every frame identical. The two SER
    # channels are compared from frame 1 on, since frame 0 is the warm-up pass
    # in which no segment has yet been scored end to end.
    def frames(c):
        return [c[f * n:(f + 1) * n] for f in range(len(c) // n)]

    for name, c, skip in (("A decoded", a_dec, 0), ("A reference", a_ref, 0),
                          ("B decoded", b_dec, 0), ("B reference", b_ref, 0),
                          ("A SER readout", a_ser, 1),
                          ("B SER readout", b_ser, 1)):
        fr = frames(c)[skip:]
        if len(fr) < 2:
            continue
        first = np.asarray(fr[0])
        for k, f in enumerate(fr[1:], 1):
            assert np.array_equal(np.asarray(f), first, equal_nan=True), (
                f"{name}: scope frame {k + skip} differs from frame {skip} — "
                f"the trace is drifting, not holding a stable picture")


def test_display_layout_is_two_verdict_panels_plus_ser():
    """THE PLOT MUST EXPLAIN ITSELF WITHOUT THE README.

    The failure this pins is not a DSP failure: with A and B split across four
    traces of ONE scope, the chip was bit-exact and the display was still read
    as broken — "the +10 dB works flawlessly but the -10 dB doesn't work at
    all", which is a description of the demo working exactly as designed. One
    axis carrying both a lock and a deliberate collapse cannot say which is
    which; a viewer sees half the points miss and concludes half of it is
    broken.

    So the shipped ``.grc`` must carry the structure that makes the intent
    legible at a glance, and this asserts it directly from the file:

      * TWO separate symbol scopes, one per segment, each fed only its own
        segment's decoded + transmitted pair (never four traces on one axis);
      * each scope's TITLE names its segment, its SNR and its VERDICT, and
        segment B's says the collapse is EXPECTED and is a CONTROL — the word
        a viewer needs in order to read a mismatch as success;
      * a live SER readout carrying both measured numbers, so the values the
        titles claim are on screen and not only in the log or the README.

    A structural gate, deliberately: the pixels are not reachable headlessly,
    but the wiring and the words are, and it is the wiring and the words that
    regressed.
    """
    import yaml

    doc = yaml.safe_load(_GRC.read_text())
    blocks = {b["name"]: b for b in doc["blocks"]}
    conns = [tuple(c) for c in doc["connections"]]

    # -- the single ambiguous four-trace scope must be gone -------------------
    scopes = {n: b for n, b in blocks.items()
              if b["id"] == "qtgui_time_sink_x"}
    fed_by_display = {
        n for n, b in scopes.items()
        if any(c[0] == "bin_to_sym" and c[2] == n for c in conns)}
    assert len(fed_by_display) == 2, (
        f"the decoded-vs-transmitted display must be TWO scopes, one per "
        f"segment, but {len(fed_by_display)} are fed by the display block: "
        f"{sorted(fed_by_display)}. Four traces on one axis is the layout that "
        f"made the negative control read as a defect")
    for n in fed_by_display:
        assert int(scopes[n]["parameters"]["nconnections"]) == 2, (
            f"{n} carries {scopes[n]['parameters']['nconnections']} traces — a "
            f"per-segment verdict panel shows exactly its own decoded + "
            f"transmitted pair")

    # -- NO TRACE MAY USE LINE STYLE 0 (NoPen) -------------------------------
    # A MEASURED GNU Radio rendering defect, reproduced standalone on a real X
    # display as well as offscreen: a qtgui time_sink channel set to style 0
    # (NoPen, "markers only") draws NOTHING on any channel above channel 0. Two
    # vector sources of DIFFERENT amplitude both set to NoPen render as ONE
    # trace — so it is not occlusion, the second is simply never painted. The
    # previous four-trace scope used style 0 on all four traces, which means the
    # decoded traces this demo exists to show were among the ones not drawn.
    # Any real pen (style 1, Solid) makes the markers appear immediately.
    for n in fed_by_display:
        for i in (1, 2):
            style = str(scopes[n]["parameters"][f"style{i}"])
            assert style != "0", (
                f"{n} trace {i} uses line style 0 (NoPen) — a qtgui time_sink "
                f"does not paint a NoPen channel above channel 0, so this "
                f"trace would be INVISIBLE while the gate on its data passes")

    # The two traces of a panel must be distinguishable where they coincide:
    # different markers, and the reference (channel 0, painted first) drawn
    # wider than the decoded X that lands on top of it.
    for n in fed_by_display:
        p = scopes[n]["parameters"]
        assert p["marker1"] != p["marker2"], (
            f"{n} draws both traces with marker {p['marker1']} — where the "
            f"decode matches its reference exactly the two are indistinguish"
            f"able, which is precisely the case the panel must show")
        assert int(p["width1"]) > int(p["width2"]), (
            f"{n}: the reference marker (width {p['width1']}) must be drawn "
            f"WIDER than the decoded marker (width {p['width2']}) — the decode "
            f"is painted last and would otherwise cover the reference it is "
            f"being compared against")

    # -- each panel is fed ONLY its own segment's channel pair ---------------
    ports = {n: sorted(int(c[1]) for c in conns
                       if c[0] == "bin_to_sym" and c[2] == n)
             for n in fed_by_display}
    assert sorted(ports.values()) == [[0, 1], [2, 3]], (
        f"the two panels must split the display block's channels as A=(0,1) "
        f"and B=(2,3), but they are wired {ports} — a panel drawing the other "
        f"segment's channel is the conflation defect returning")
    by_pair = {tuple(v): k for k, v in ports.items()}
    a_scope, b_scope = by_pair[(0, 1)], by_pair[(2, 3)]

    # ORIENTATION within a panel: the REFERENCE channel (1 for A, 3 for B) must
    # land on scope input 0 and the DECODED channel (0 / 2) on scope input 1.
    # A time_sink paints its highest-numbered channel LAST, and the decode is
    # the trace that has to survive a perfect overlay — wired the other way
    # round, segment A's panel renders as a single reference-coloured trace and
    # the decode it exists to show is hidden underneath.
    wiring = {(c[0], int(c[1]), c[2]): int(c[3]) for c in conns
              if c[0] == "bin_to_sym"}
    for panel, dec_ch, ref_ch in ((a_scope, 0, 1), (b_scope, 2, 3)):
        assert wiring[("bin_to_sym", ref_ch, panel)] == 0, (
            f"{panel}: the reference (display channel {ref_ch}) must feed scope "
            f"input 0, so it is painted FIRST and the decode lands on top")
        assert wiring[("bin_to_sym", dec_ch, panel)] == 1, (
            f"{panel}: the decoded chip output (display channel {dec_ch}) must "
            f"feed scope input 1 — the last-painted trace — or an exact overlay "
            f"hides the decode entirely")

    a_title = scopes[a_scope]["parameters"]["name"]
    b_title = scopes[b_scope]["parameters"]["name"]

    # -- the words that carry the meaning ------------------------------------
    for what, title, needles in (
            ("segment A", a_title,
             ("SEGMENT A", "+10", "EXPECTED")),
            ("segment B", b_title,
             ("SEGMENT B", "10 dB", "CONTROL", "EXPECTED"))):
        up = title.upper()
        missing = [s for s in needles if s.upper() not in up]
        assert not missing, (
            f"the {what} panel title {title!r} is missing {missing} — the "
            f"title is the only thing telling a viewer whether what they are "
            f"looking at is the intended result")

    # B's title must say the mismatch IS the outcome, in words, not by omission.
    assert any(w in b_title.upper()
               for w in ("COLLAPSE", "MISS", "FAIL", "GARBAGE")), (
        f"segment B's panel title {b_title!r} never says the decode misses — a "
        f"viewer has to be told the mismatch is the outcome, not an error")
    # ...and it must say so is EXPECTED, right next to the word for the miss.
    assert "THIS IS THE POINT" in b_title.upper() or "EXPECTED" in b_title.upper(), (
        f"segment B's panel title {b_title!r} describes the miss without "
        f"claiming it: the title has to assert the failure is intended")

    # -- the measured numbers are on screen ----------------------------------
    numbers = {n: b for n, b in blocks.items() if b["id"] == "qtgui_number_sink"}
    assert len(numbers) == 1, (
        f"expected exactly one SER readout, found {sorted(numbers)} — the two "
        f"headline numbers must be visible without reading the log")
    ser = next(iter(numbers.values()))
    assert int(ser["parameters"]["nconnections"]) == 2, (
        "the SER readout must carry BOTH segments' measured values")
    ser_name = next(iter(numbers))
    ser_ports = sorted(int(c[1]) for c in conns
                       if c[0] == "bin_to_sym" and c[2] == ser_name)
    assert ser_ports == [4, 5], (
        f"the SER readout is wired to display channels {ser_ports}, expected "
        f"the two SER channels (4, 5)")
    labels = (ser["parameters"]["label1"] + " " +
              ser["parameters"]["label2"]).upper()
    assert "CONTROL" in labels, (
        f"neither SER label marks segment B as the control: {labels!r}")

    # -- the display block really does publish six channels ------------------
    io_cache = blocks["bin_to_sym"]["parameters"]["_io_cache"]
    assert io_cache.count("'float', 1)") == 7, (
        "the display block's io cache does not describe 1 input + 6 outputs — "
        "GRC reads this cache for the port count, so a stale one silently "
        "drops the panels it no longer knows about")

    # -- the embedded copy is the SAME code as the standalone module ---------
    # An epy_block carries its source INSIDE the .grc; ``css_decode_map.py``
    # beside it is the readable/reviewable copy. Two copies drift, and only the
    # embedded one runs — so the file a reviewer reads could describe behaviour
    # the flowgraph does not have.
    embedded = blocks["bin_to_sym"]["parameters"]["_source_code"]
    standalone = (_ROOT / "examples" / "css_transceiver"
                  / "css_decode_map.py").read_text()
    assert embedded == standalone, (
        "the .grc's embedded display-block source has drifted from "
        "examples/css_transceiver/css_decode_map.py — only the EMBEDDED copy "
        "runs, so the reviewable file would be describing code that is not "
        "what the flowgraph executes")


def _synth_channels(a_dec_seg, a_ref_seg, b_dec_seg, b_ref_seg, reps=4):
    """Six synthetic display channels with the shipped block's frame layout —
    the shape ``_assert_plotted_traces`` reads, built from whatever per-segment
    traces a mutation wants to inject. The two SER channels are computed from
    the injected traces (as the real block computes them), so a mutation that
    changes what is plotted also changes what the readout claims."""
    n = stim.n_out_words()
    w = n // 2
    nan = [np.nan] * w

    def ser(dec, ref):
        pts = [(d, r) for d, r in zip(dec, ref)
               if not np.isnan(d) and not np.isnan(r)]
        return (sum(1 for d, r in pts if int(d) != int(r)) / len(pts)
                if pts else np.nan)

    a, b = ser(a_dec_seg, a_ref_seg), ser(b_dec_seg, b_ref_seg)
    return {
        "bin_to_sym.0": (list(a_dec_seg) + nan) * reps,
        "bin_to_sym.1": (list(a_ref_seg) + nan) * reps,
        "bin_to_sym.2": (nan + list(b_dec_seg)) * reps,
        "bin_to_sym.3": (nan + list(b_ref_seg)) * reps,
        "bin_to_sym.4": [a] * (n * reps),
        "bin_to_sym.5": [b] * (n * reps),
    }


def test_mutation_display_gate_catches_the_old_broken_plot():
    """INV-4 for the DISPLAY gate: it must FAIL on the defects that made a
    bit-exact decode look broken on screen. A display gate never shown to fail
    certifies nothing — and these are not hypothetical, they are what shipped.

    Replays each defect against ``_assert_plotted_traces`` with synthetic
    channels built from the real reference trace.
    """
    n = stim.n_out_words()
    w = n // 2
    ref_seg = ([np.nan] + [float(s) for s in
                           stim.framed_symbols()[:stim.n_data_symbols()]])[:w]
    nan = [np.nan] * w
    reps = 4

    def chans(*segs):
        return _synth_channels(*segs, reps=reps)

    # A REALISTIC collapse to stand in for segment B: the real -10 dB decode
    # misses most symbols but not all (it retains partial signal and beats
    # chance), so a stand-in that misses EVERY symbol would sit outside the
    # control band for the same reason a dead chain does. Corrupt every symbol
    # except a scattered few, landing inside the band the real segment occupies.
    _tx = stim.framed_symbols()[:stim.n_data_symbols()]
    collapsed = ([np.nan]
                 + [float(s if i % 3 == 0 else (int(s) + 7) % 16)
                    for i, s in enumerate(_tx)])[:w]

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
    both = chans(ref_seg, ref_seg, collapsed, ref_seg)
    both["bin_to_sym.0"] = (list(ref_seg) + list(ref_seg)) * reps
    both["bin_to_sym.1"] = (list(ref_seg) + list(ref_seg)) * reps
    both["bin_to_sym.2"] = (list(collapsed) + list(collapsed)) * reps
    both["bin_to_sym.3"] = (list(ref_seg) + list(ref_seg)) * reps
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

    # MUTATION 5 — THE CONTROL QUIETLY STARTS DECODING. If segment B locks too,
    # its panel's "collapses" verdict is a lie and segment A's SER 0 stops
    # meaning anything: a chain that cannot fail proves nothing. The band's
    # FLOOR is what catches this, and the old "> 0.2" bar caught it too — but
    # only this direction. Mutation 6 is the direction it could not see.
    with pytest.raises(AssertionError, match="derived band"):
        _assert_plotted_traces(chans(ref_seg, ref_seg, ref_seg, ref_seg))

    # MUTATION 6 — A DEAD CHAIN MASQUERADING AS THE NEGATIVE CONTROL. A chain
    # that has stopped computing and emits a CONSTANT scores a very HIGH SER,
    # so "SER is high" accepts it as a healthy control. The band's CEILING is
    # derived to reject it: every stuck-at-k stream scores >= 0.75 on this
    # frame. Replayed here with the cheapest one (stuck-at-5).
    for k in range(16):
        stuck = [np.nan] + [float(k)] * (len(ref_seg) - 1)
        with pytest.raises(AssertionError, match="derived band"):
            _assert_plotted_traces(chans(ref_seg, ref_seg, stuck, ref_seg))

    # MUTATION 7 — THE READOUT LIES ABOUT THE PICTURE. The number beside a
    # panel must be that panel's own measurement; a hard-coded or stale value
    # would let the titles claim a verdict the traces do not support.
    lying = chans(ref_seg, ref_seg, collapsed, ref_seg)
    lying["bin_to_sym.5"] = [0.5] * len(lying["bin_to_sym.5"])
    with pytest.raises(AssertionError, match="disagrees with the"):
        _assert_plotted_traces(lying)

    # MUTATION 8 — AN UNSETTLED READOUT. A number that swings mid-segment (the
    # running ratio, published instead of the completed pass) is unreadable.
    swinging = chans(ref_seg, ref_seg, collapsed, ref_seg)
    swinging["bin_to_sym.4"] = [
        float(i % 2) * 0.5 for i in range(len(swinging["bin_to_sym.4"]))]
    with pytest.raises(AssertionError, match="not settled"):
        _assert_plotted_traces(swinging)


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
    ctrl, sim = _serve(demo.KYT_PATH)
    try:
        # Tap all SIX DISPLAY channels too, so the same run proves the
        # recovered stream, the traces on the two verdict panels, AND the SER
        # numbers printed beside them.
        sinks = _run_flowgraph(
            _GRC, taps=",".join(f"bin_to_sym.{i}" for i in range(6)))
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
    assert _SER_B_MIN <= ser_b < _SER_B_MAX, (
        f"the -10 dB control scored SER {ser_b:.4f} through the user path, "
        f"outside the derived band [{_SER_B_MIN}, {_SER_B_MAX})")

    # server_repeat integrity: every later full batch is a clean repetition.
    for r in range(1, len(idx) // n):
        assert idx[r * n:(r + 1) * n] == first, \
            f"repetition {r} diverges — not a genuine looped batch"
