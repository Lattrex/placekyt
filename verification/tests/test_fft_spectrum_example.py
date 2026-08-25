# SPDX-License-Identifier: GPL-3.0-or-later
"""Gate for the ``examples/fft_spectrum`` spectrum-analyzer example.

The example is a placed 64-point streaming FFT (``FFT64Block``, 84 cells)
feeding a placed ``ComplexToMagSquaredBlock``, so the transform AND the
per-bin power both run on chip; the shipped ``.grc`` un-reverses the block's
DIF bin order and paints a natural-order 64-bin spectrum.

The gates, in the order they matter:

  1. **THE USER-PATH GATE** (``test_shipped_grc_user_path``) — host the SHIPPED
     ``.kyt`` exactly as the GUI's "Run as GNURadio Server" does (port 58950),
     GRC-generate the SHIPPED ``.grc``, run it under the real GNU Radio
     interpreter, and assert on what the kyttar sink actually recovered: a
     real spectrum with the demo tone in its true bin. This is the gate that
     decides whether the example is shippable; everything else is support.

     ⚠️ RUN IT STANDALONE. Every user-path suite binds port 58950, so two of
     them in one session collide (the known harness flakiness). ``_serve``
     waits for an EXCLUSIVE bind rather than accepting whatever it got —
     see its docstring for why "it bound something" is the dangerous case.

  2. **The whole chain on a real built chip** — bit-exact against the composed
     verified block references, with the tone in its true bin after
     un-reversal, on the placement the ``.kyt`` ships.

  3. **The bin-order / scale / latency contracts**, pinned.

  4. **The two INPUT RAILS** — ``x16_in`` carries a genuine COMPLEX stream:
     the xi and xq rails deliver DIFFERENT words (the tone's cosine and sine,
     bit-exact vs the reference), on distinct registers, and the waveform pane
     names them DISTINGUISHABLY. Identical rails would mean a real input,
     whose conjugate-symmetric spectrum splits the tone into two quarter-power
     peaks — a failure this example has already suffered once.

  5. **The bin -> Hz mapping** — bin ``k`` of ``N`` at declared rate ``fs`` is
     ``k*fs/N``, bins at or above ``N/2`` being the negative frequencies
     ``(k-N)*fs/N``. Pinned against literals, checked against the centred axis
     the shipped ``.grc``s configure, and measured on the real chip: the demo
     tone peaks at **+5500 Hz** (N=64, 500 Hz/bin) and **+11000 Hz** (N=32,
     1000 Hz/bin), both at -0.92 dBFS.

  6. **MANDATORY mutations (INV-4)** — a display path that does NOT un-reverse,
     one that un-reverses with the wrong width, one at the wrong scale, a frame
     read at the wrong latency offset, an ingress with duplicated rails, a
     namer that collides both rail labels, a mapping that ignores the sample
     rate, and an axis read without the fftshift must ALL fail. The un-reversal
     is the one thing a plausible-looking wrong plot hides, so it is attacked
     directly.

Run (standalone — see the port note above)::

    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      .venv/bin/python -m pytest verification/tests/test_fft_spectrum_example.py -q
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
           str(_ROOT / "verification"),
           str(_ROOT / "examples" / "fft_spectrum")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fft_spectrum_demo import (  # noqa: E402
    AMPLITUDE, COHERENT_MIN, KYT32_PATH, KYT_PATH, LATENCY, LEAKAGE_MAX,
    N_FFT, SAMP_RATE, SIZES, TONE_BIN, _q15, _s16, _wr, _jp, axis_hz,
    bin_hz, bin_to_hz, build_chain, burst_of, centred_spectrum,
    fftshift_order, frames_of, latency_of, natural_spectrum, reference_power,
    run_chain, tone, unreverse)

CHIP_YAML = _ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml"
_EX = _ROOT / "examples" / "fft_spectrum"
GRC_PATH = _EX / "fft_spectrum.grc"
GRC32_PATH = _EX / "fft_spectrum_32.grc"
_RUNNER = _ROOT / "verification" / "grc_userpath_run.py"
_GR_PYTHON = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")
_PORT = 58950                       # the .grcs' baked server_port

pytestmark = pytest.mark.skipif(
    not CHIP_YAML.exists(), reason="chip yaml absent")

#: The bit-reversed SLOT the demo tone leaves the chip on, per size. Pinned as
#: LITERALS (not recomputed from the map the code under test uses) so a
#: corrupted map cannot silently agree with itself: rev6(11) = 52, rev5(11) = 26.
TONE_SLOT = 52
TONE_SLOT_32 = 26


# ------------------------------------------------------------------ fixtures
_CHAIN: dict = {}


def _chain():
    """Build + run the shipped chain ONCE for the whole session."""
    if "out" not in _CHAIN:
        _ctrl, bres, _cat, _ct = build_chain()
        iq = tone()
        _CHAIN["build"] = bres
        _CHAIN["iq"] = iq
        _CHAIN["out"] = run_chain(bres, iq)
        _CHAIN["cells"] = sum(c.cell_count for c in bres.chips.values())
    return _CHAIN


# =============================================================================
# 1. THE USER-PATH GATE — the shipped .kyt hosted, the shipped .grc run
# =============================================================================
@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _serve(kyt, *, wait_s: float = 240.0):
    """Host ``kyt`` on the GUI's default port, waiting for an EXCLUSIVE bind.

    The wait is not politeness, it is correctness. Port 58950 is the one bind
    every user-path suite uses, so a concurrently-running suite (or a leftover
    server) holds it. Two failure modes follow, and the second is the
    dangerous one:

      * the bind raises ``OSError: Address already in use`` — loud, harmless;
      * ``start_gnuradio_server`` returns None (it did not bind) and the
        flowgraph then happily talks to *somebody else's* server on 58950 and
        recovers SOMEBODY ELSE'S CHIP OUTPUT. That produced a confidently
        wrong reading during development (304215 words of another design's
        stream, read as a spectrum defect).

    So this retries until it holds 58950 itself, and asserts the exclusive
    bind rather than accepting whatever it got.
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
    deadline = time.monotonic() + wait_s
    bound = None
    while time.monotonic() < deadline:
        try:
            bound = sim.start_gnuradio_server(port=_PORT)
        except OSError:
            bound = None
        if bound == _PORT:
            return ctrl, sim
        try:
            sim.stop_gnuradio_server()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2.0)
    raise AssertionError(
        f"never obtained an EXCLUSIVE bind of port {_PORT} within {wait_s}s "
        f"(last bind result {bound!r}) — another user-path suite or a stale "
        "server is holding it; run this suite STANDALONE")


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
        f"{r.stdout[-1200:]}\n{r.stderr[-1800:]}")
    return sinks


@pytest.mark.skipif(not os.path.exists(_GR_PYTHON),
                    reason="GNU Radio interpreter absent")
@pytest.mark.parametrize("n_fft,kyt,grc,want_slot", [
    (64, KYT_PATH, GRC_PATH, TONE_SLOT),
    (32, KYT32_PATH, GRC32_PATH, TONE_SLOT_32),
])
def test_shipped_grc_user_path(qapp, n_fft, kyt, grc, want_slot):
    """THE gate: host the SHIPPED ``.kyt``, run the SHIPPED ``.grc``'s
    generated top block under the real GNU Radio interpreter, and assert the
    recovered stream IS a spectrum with the demo tone in its true bin.

    The tap is on the kyttar SINK (the chip's own recovered stream — the
    ground truth the display is built from), so this asserts the CHIP is
    producing a spectrum through the real client stack, then re-applies the
    example's own published un-reversal to prove the display contract lands
    the tone at ``TONE_BIN``.

    Both shipped sizes go through the SAME gate: the contracts (bit-reversed
    order, FFT/N scale, N-1 latency) are the same statement at each N, and the
    only per-size input is the bit-reversed slot the tone must arrive on.
    """
    assert kyt.exists(), (
        f"{kyt} is missing — run examples/fft_spectrum/build_kyt.py")
    latency = latency_of(n_fft)
    _ctrl, sim = _serve(kyt)
    try:
        sinks = _run_flowgraph(grc)
    finally:
        sim.stop_gnuradio_server()

    assert "ksink" in sinks, f"no ksink stream recovered (got {sorted(sinks)})"
    # kyttar_sink emits the recovered stream as q15/32768 floats.
    words = [int(round(v * 32768.0)) for v in sinks["ksink"]]
    assert len(words) >= latency + n_fft, (
        f"N={n_fft}: recovered only {len(words)} words — not even one whole "
        f"frame ({latency + n_fft} needed)")

    # server_repeat LOOPS the genuine one-batch result. Assert the repetition is
    # a clean replay of the FIRST batch (real data looped, never a fake stream)
    # and analyse the first batch — a rotated/garbled replay would otherwise
    # slide the frame grid and be read as a spectrum defect.
    ref = reference_power(tone(n_fft=n_fft), n_fft)
    nb = len(ref)
    assert words[:nb] == ref, (
        f"N={n_fft}: the live user path's FIRST batch diverges from the "
        "composed block references at index "
        f"{next(i for i in range(nb) if words[i] != ref[i])}")
    for r in range(1, len(words) // nb):
        assert words[r * nb:(r + 1) * nb] == ref, (
            f"N={n_fft}: server_repeat repetition {r} diverges — the looped "
            "display is not a clean replay of the real batch")

    got_frames = frames_of(words[:nb], n_fft)
    assert got_frames, f"N={n_fft}: no whole frame recovered past the latency"
    for f, frame in enumerate(got_frames):
        slot = int(np.argmax(frame))
        assert slot == want_slot, (
            f"N={n_fft} frame {f}: peak at chip SLOT {slot}, expected the "
            f"bit-reversed slot {want_slot} for bin {TONE_BIN}")
        nat = natural_spectrum(frame, n_fft)
        peak_bin = int(np.argmax(nat))
        assert peak_bin == TONE_BIN, (
            f"N={n_fft} frame {f}: after un-reversal the peak is at bin "
            f"{peak_bin}, expected {TONE_BIN}")
        # an ON-BIN tone of amplitude A lands at power A^2 at the FFT/N scale
        assert nat[TONE_BIN] > COHERENT_MIN, (
            f"N={n_fft} frame {f}: tone bin power {nat[TONE_BIN]} — too weak "
            "to be the coherent bin")
        others = max(v for b, v in enumerate(nat) if b != TONE_BIN)
        assert others < LEAKAGE_MAX, (
            f"N={n_fft} frame {f}: leakage {others} into other bins — an "
            "ON-BIN tone must be a single clean line")
    assert len(got_frames) == 3, (
        f"N={n_fft}: expected 3 whole frames in the batch, got "
        f"{len(got_frames)}")


# =============================================================================
# 2. The whole chain on a real built chip
# =============================================================================
def test_kyt_builds_and_routes_as_one_chip():
    """The shipped placement routes every net and builds — and the FFT is
    genuinely the chip-scale spine, not some re-packed variant."""
    c = _chain()
    bres = c["build"]
    assert bres.ok
    assert c["cells"] <= 120, f"{c['cells']} cells does not fit the array"
    lands = bres.chips[0].input_landings
    assert lands, "no input landing resolved — the ingress corridor is not wired"
    lin = next(iter(lands.values()))
    assert 0 <= lin["hop"] <= 31
    assert len(lin["data_addrs"]) == 2, "not a complex landing"


def test_chain_is_bit_exact_vs_the_block_references():
    """Data FLOWS through the placed topology, and what comes out is bit-exact
    against the FFT's verified streaming reference composed with the power
    stage's verified reference — the routing and hand-off included."""
    c = _chain()
    ref = reference_power(c["iq"])
    assert len(c["out"]) == len(ref), (
        f"chip produced {len(c['out'])} words, reference {len(ref)}")
    assert c["out"] == ref, (
        "chip diverges from the composed references at index "
        f"{next(i for i in range(len(ref)) if c['out'][i] != ref[i])}")


def test_tone_lands_in_its_true_bin_after_unreversal():
    """The headline claim, measured: the demo tone leaves the chip at the
    BIT-REVERSED slot and un-reverses to its true bin, in EVERY whole frame."""
    c = _chain()
    frames = frames_of(c["out"])
    assert len(frames) == 3, f"expected 3 whole frames, got {len(frames)}"
    for f, frame in enumerate(frames):
        assert int(np.argmax(frame)) == TONE_SLOT, f"frame {f}: wrong slot"
        nat = natural_spectrum(frame)
        assert int(np.argmax(nat)) == TONE_BIN, f"frame {f}: wrong bin"
        assert nat[TONE_BIN] > COHERENT_MIN
        assert max(v for b, v in enumerate(nat) if b != TONE_BIN) < LEAKAGE_MAX


@pytest.mark.parametrize("bin_index", [1, 5, 11, 23, 32, 47, 63])
def test_every_probed_bin_lands_correctly(bin_index):
    """Sweep the tone across the band: an ON-BIN tone at bin b must un-reverse
    to bin b, for bins spanning the whole spectrum (including bin 32, the
    Nyquist-adjacent slot the bit-reversal map sends to slot 1)."""
    from fft_spectrum_demo import build_chain as _bc

    if "sweep_build" not in _CHAIN:
        _ctrl, bres, _cat, _ct = _bc()
        _CHAIN["sweep_build"] = bres
    iq = tone(bin_index)
    out = run_chain(_CHAIN["sweep_build"], iq)
    frames = frames_of(out)
    assert frames, f"bin {bin_index}: no whole frame"
    rev = unreverse()
    want_slot = rev.index(bin_index)
    for f, frame in enumerate(frames):
        assert int(np.argmax(frame)) == want_slot, (
            f"bin {bin_index} frame {f}: peak at slot {int(np.argmax(frame))}, "
            f"expected {want_slot}")
        nat = natural_spectrum(frame)
        assert int(np.argmax(nat)) == bin_index
        assert nat[bin_index] > COHERENT_MIN


def test_noise_and_two_tone_behave_sanely():
    """A second stimulus class: TWO on-bin tones must produce exactly TWO
    dominant bins at the right places, and broadband noise must NOT
    concentrate in one bin (a chain that emits a constant would pass a
    single-tone gate)."""
    from fft_spectrum_demo import build_chain as _bc

    if "sweep_build" not in _CHAIN:
        _ctrl, bres, _cat, _ct = _bc()
        _CHAIN["sweep_build"] = bres
    bres = _CHAIN["sweep_build"]
    n = LATENCY + N_FFT * 2
    t = np.arange(n)

    b1, b2 = 7, 21
    z = 0.45 * np.exp(2j * np.pi * b1 * t / N_FFT) \
        + 0.45 * np.exp(2j * np.pi * b2 * t / N_FFT)
    frames = frames_of(run_chain(bres, [complex(c) for c in
                                        z.astype(np.complex64)]))
    assert frames
    for frame in frames:
        nat = natural_spectrum(frame)
        top = sorted(range(N_FFT), key=lambda b: nat[b], reverse=True)[:2]
        assert set(top) == {b1, b2}, (
            f"two-tone: dominant bins {sorted(top)}, expected {[b1, b2]}")
        rest = max(nat[b] for b in range(N_FFT) if b not in (b1, b2))
        assert rest < nat[b1] / 8, "two-tone: spectrum is not two clean lines"

    rng = np.random.default_rng(5)
    zn = rng.normal(0, 0.3, n) + 1j * rng.normal(0, 0.3, n)
    frames = frames_of(run_chain(bres, [complex(c) for c in
                                        zn.astype(np.complex64)]))
    assert frames
    for frame in frames:
        nat = natural_spectrum(frame)
        assert sum(1 for v in nat if v > 0) > N_FFT // 2, (
            "noise excited fewer than half the bins — the chain is not "
            "transforming")
        assert max(nat) < 0.5 * 32768, (
            "broadband noise concentrated in one bin — that is not a spectrum")


# =============================================================================
# 3. The pinned contracts (order, scale, latency)
# =============================================================================
@pytest.mark.parametrize("n_fft,want_slot,want_cells", [
    (64, TONE_SLOT, 84),
    (32, TONE_SLOT_32, 60),
])
def test_second_variant_chain_on_chip(n_fft, want_slot, want_cells):
    """BOTH shipped sizes, on a real built chip: the placement routes, the data
    flows, the stream is bit-exact against the composed block references, and
    the tone un-reverses to its true bin in every whole frame.

    This is the headline claim restated per size, so the smaller variant is a
    genuinely verified deliverable rather than a copy of a `.grc` nobody ran.
    """
    key = ("size", n_fft)
    if key not in _CHAIN:
        _ctrl, bres, _cat, _ct = build_chain(n_fft)
        iq = tone(n_fft=n_fft)
        _CHAIN[key] = (bres, iq, run_chain(bres, iq),
                       sum(c.cell_count for c in bres.chips.values()))
    bres, iq, out, cells = _CHAIN[key]

    assert bres.ok and cells <= 120, f"N={n_fft}: {cells} cells"
    # the FFT block itself is the size's verified cell count (the rest is
    # the 1-cell power stage plus routing)
    assert SIZES[n_fft][1] == want_cells

    ref = reference_power(iq, n_fft)
    assert len(out) == len(ref) == burst_of(n_fft), (
        f"N={n_fft}: chip produced {len(out)} words, reference {len(ref)}")
    assert out == ref, (
        f"N={n_fft}: chip diverges from the composed references at index "
        f"{next(i for i in range(len(ref)) if out[i] != ref[i])}")

    frames = frames_of(out, n_fft)
    assert len(frames) == 3, f"N={n_fft}: {len(frames)} whole frames"
    for f, frame in enumerate(frames):
        assert int(np.argmax(frame)) == want_slot, (
            f"N={n_fft} frame {f}: peak at slot {int(np.argmax(frame))}, "
            f"expected {want_slot}")
        nat = natural_spectrum(frame, n_fft)
        assert int(np.argmax(nat)) == TONE_BIN
        assert nat[TONE_BIN] > COHERENT_MIN
        assert max(v for b, v in enumerate(nat)
                   if b != TONE_BIN) < LEAKAGE_MAX
    # and the pinned slot really is the bit-reversal of the bin at THIS size
    assert unreverse(n_fft).index(TONE_BIN) == want_slot


@pytest.mark.parametrize("n_fft,want", [(64, 63), (32, 31)])
def test_latency_per_size_pinned(n_fft, want):
    """``N - 1``, asserted against a literal at each shipped size."""
    assert latency_of(n_fft) == want
    assert burst_of(n_fft) == want + n_fft * 3


def test_bit_reversal_map_pinned():
    """The display map is a permutation, an involution, and spot-pinned
    against literals — never against the code that produces it."""
    rev = unreverse()
    assert len(rev) == N_FFT
    assert rev[:8] == [0, 32, 16, 48, 8, 40, 24, 56]
    assert rev[TONE_SLOT] == TONE_BIN and rev[TONE_BIN] == TONE_SLOT
    assert sorted(rev) == list(range(N_FFT))
    assert all(rev[rev[k]] == k for k in range(N_FFT)), "not an involution"


def test_scale_is_fft_over_n():
    """FFT/64: a full-scale ON-BIN complex exponential yields a coherent bin
    at ~full scale (power ~1.0), not 1/64 of it and not saturated garbage."""
    c = _chain()
    nat = natural_spectrum(frames_of(c["out"])[0])
    p = nat[TONE_BIN] / 32768.0
    want = AMPLITUDE ** 2                    # 0.81 — the coherent-bin POWER
    assert abs(p - want) < 0.01, (
        f"coherent-bin power {p:.4f}, expected ~{want:.4f} (A^2 at the FFT/64 "
        "scale) — the scale is wrong")


def test_latency_is_63_and_the_transient_is_not_a_frame():
    """The first 63 outputs are the zero-pipeline startup, and the example
    strips exactly that many — reading a frame at offset 0 must NOT show the
    tone in its bin (which is what proves the strip is load-bearing)."""
    c = _chain()
    assert LATENCY == 63
    early = c["out"][:N_FFT]
    nat = natural_spectrum(early)
    assert int(np.argmax(nat)) != TONE_BIN or max(nat) < 30000, (
        "the startup transient already looks like the answer — the latency "
        "strip would be untestable")


# =============================================================================
# 4. MANDATORY mutations (INV-4) — wrong display paths must FAIL
# =============================================================================
def _bin_gate(nat):
    """The example's own display assertion, reusable by the mutants."""
    return (int(np.argmax(nat)) == TONE_BIN
            and nat[TONE_BIN] > COHERENT_MIN
            and max(v for b, v in enumerate(nat) if b != TONE_BIN) < LEAKAGE_MAX)


def test_mutation_no_unreversal_fails():
    """THE mutation this example exists to catch: plot the chip's raw slots
    without un-reversing. It is a plausible-looking spectrum with a clean
    line — in the WRONG bin."""
    frame = frames_of(_chain()["out"])[0]
    assert not _bin_gate(list(frame)), (
        "the raw bit-reversed slots passed the bin gate — the un-reversal is "
        "not actually load-bearing, so the gate certifies nothing")


def test_mutation_wrong_width_unreversal_fails():
    """Un-reverse with the wrong transform width (5 bits, i.e. an FFT32 map
    applied to 64 slots) — a subtly wrong permutation."""
    frame = frames_of(_chain()["out"])[0]
    bad = [0] * N_FFT
    for slot in range(N_FFT):
        r, v = 0, slot
        for _ in range(5):
            r = (r << 1) | (v & 1)
            v >>= 1
        bad[r % N_FFT] = frame[slot]
    assert not _bin_gate(bad), "a 5-bit reversal map passed the bin gate"


def test_mutation_wrong_scale_fails():
    """A display that mis-scales the power (the classic q15-vs-raw slip)
    must fail the coherent-bin amplitude assertion."""
    nat = natural_spectrum(frames_of(_chain()["out"])[0])
    scaled = [v / 64 for v in nat]
    assert not _bin_gate(scaled), "a /64-scaled spectrum passed the bin gate"


def test_mutation_wrong_frame_offset_fails():
    """Read the frame at the wrong latency offset (off by one sample): the
    frame straddles a boundary and the line smears."""
    out = _chain()["out"]
    bad_frame = out[LATENCY + 1:LATENCY + 1 + N_FFT]
    assert not _bin_gate(natural_spectrum(bad_frame)), (
        "a frame read one sample late passed the bin gate")


def test_mutation_empty_and_flat_streams_fail():
    """Degenerate streams a broken chain would produce."""
    assert not _bin_gate([0] * N_FFT), "an all-zero spectrum passed"
    assert not _bin_gate([32767] * N_FFT), "a flat full-scale spectrum passed"


# =============================================================================
# 5. THE TWO INPUT RAILS — genuinely complex, and DISTINGUISHABLY labelled
# =============================================================================
# The user report was "why do both inputs say 'xi'? Is this complex or not?".
# Two separate claims live here and both are gated, because only one of them
# was ever a bug:
#
#   * the DATA: the I and Q rails must carry DIFFERENT streams — a real complex
#     tone, I = cos and Q = sin, matching the reference sample-for-sample. This
#     was never broken (the spectrum was clean because the ingress is correct),
#     but it is the claim that MATTERS, so it is measured on a live run rather
#     than assumed. If both rails ever carried identical data, the block would
#     see a real input, whose spectrum is conjugate-symmetric — the tone would
#     split into two quarter-power peaks. That failure mode already happened
#     once on this example (the un-named-stream landing bug), so it is gated.
#   * the LABEL: the waveform pane must name the two rails DIFFERENTLY (xi/xq).
#     This WAS the bug. GNU Radio collapses an I/Q pair into one complex port,
#     so the project holds ONE connection (`...->fft64.xi`) while the port
#     physically carries two tagged rails; naming by net alone labelled BOTH
#     'fft64.xi'.


def _rail_words(n_fft, count=16):
    """The words ACTUALLY delivered to each input rail on a live run, keyed by
    the waveform pane's own tag. Drives the built chip with the trace on and
    reads the streams back through the SAME ``TraceModel.port_streams_by_tag``
    the GUI plots — so this asserts on what the user is shown, not a proxy."""
    from engine.trace_model import TraceModel
    import simkyt

    key = ("rails", n_fft, count)
    if key in _CHAIN:
        return _CHAIN[key]
    _ctrl, bres, _cat, _ct = build_chain(n_fft)
    lin = bres.chips[0].input_landings["ingress"]
    chip = simkyt.Chip.from_yaml(str(CHIP_YAML))
    chip.load_bitstream_physical(bres.words(0))
    chip.enable_trace()
    iq = tone(n_fft=n_fft)[:count]
    for c in iq:
        chip.queue_words_physical("x16_in", [
            _wr(lin["hop"], lin["data_addrs"][0]), _q15(c.real),
            _wr(lin["hop"], lin["data_addrs"][1]), _q15(c.imag),
            _jp(lin["hop"], lin["entry"])])
        for _ in range(3000):
            chip.run(max_events=64)
            chip.read_port_words_timed("x16_out")
    tm = TraceModel()
    tm.ingest(0, chip.get_trace(), 16)
    by_tag = tm.port_streams_by_tag()
    hop = int(lin["hop"])
    i_tag = (hop, int(lin["data_addrs"][0]))
    q_tag = (hop, int(lin["data_addrs"][1]))
    got = (
        lin,
        [_s16(v) for _t, v in by_tag.get((0, "x16_in", i_tag), [])],
        [_s16(v) for _t, v in by_tag.get((0, "x16_in", q_tag), [])],
        iq,
    )
    _CHAIN[key] = got
    return got


@pytest.mark.parametrize("n_fft", [64, 32])
def test_the_two_input_rails_carry_different_data(n_fft):
    """THE ingress claim: ``x16_in`` delivers a genuine COMPLEX stream — the
    two rails carry DIFFERENT words, and each matches the reference tone's own
    real/imaginary part sample-for-sample.

    Measured (N=64, first words): xi = 29491, 13902, -16384, -29349, ... and
    xq = 0, 26009, 24521, -2891, ... — the cosine and the sine of the 0.9-
    amplitude bin-11 tone.
    """
    lin, i_words, q_words, iq = _rail_words(n_fft)
    assert len(lin["data_addrs"]) == 2, (
        f"N={n_fft}: the landing is not complex — data_addrs {lin['data_addrs']}")
    assert lin["data_addrs"][0] != lin["data_addrs"][1], (
        f"N={n_fft}: both rails land on ONE register {lin['data_addrs']} — the "
        "block would see a real input, whose spectrum is conjugate-symmetric")
    assert i_words and q_words, (
        f"N={n_fft}: a rail delivered NOTHING (xi {len(i_words)} words, xq "
        f"{len(q_words)}) — the port is not carrying two streams")
    assert len(i_words) == len(q_words) == len(iq)

    # The rails are DIFFERENT streams. A real tone has distinct I and Q, so
    # identical rails would mean the ingress is duplicating one register.
    assert i_words != q_words, (
        f"N={n_fft}: the xi and xq rails carry IDENTICAL words — that is not a "
        "complex input, it is one rail delivered twice")

    # And each is the right half of the tone, bit for bit.
    assert i_words == [_s16(_q15(c.real)) for c in iq], (
        f"N={n_fft}: the xi rail is not the tone's real part")
    assert q_words == [_s16(_q15(c.imag)) for c in iq], (
        f"N={n_fft}: the xq rail is not the tone's imaginary part")
    # Q of a complex exponential starts at sin(0) = 0 while I starts at the
    # amplitude — the cheapest independent check that the rails are not swapped.
    assert q_words[0] == 0 and i_words[0] == _s16(_q15(AMPLITUDE)), (
        f"N={n_fft}: the rails look SWAPPED (xi[0]={i_words[0]}, "
        f"xq[0]={q_words[0]}; want xi[0]={_s16(_q15(AMPLITUDE))}, xq[0]=0)")


@pytest.mark.parametrize("n_fft,kyt,blk", [
    (64, KYT_PATH, "fft64"),
    (32, KYT32_PATH, "fft32"),
])
def test_waveform_labels_the_two_rails_distinguishably(qapp, n_fft, kyt, blk):
    """THE label gate: the waveform pane must name the two rails DIFFERENTLY.

    This is the regression guard for the reported "both inputs say xi". The
    project holds ONE connection for the complex link (GNU Radio collapses the
    I/Q pair; the Q net is synthesised), so a namer that keys off the net alone
    returns the same string for both tags. Asserting they DIFFER is the part
    that cannot regress; asserting they are exactly ``blk.xi``/``blk.xq`` pins
    that they are also the right way round.
    """
    from engine.catalog import BlockCatalog
    from engine.io.project_io import load_project
    from ui.controller import AppController
    from ui.main_window import MainWindow

    assert kyt.exists(), f"{kyt} is missing"
    lin, _i, _q, _iq = _rail_words(n_fft, count=2)
    hop, (ri, rq) = int(lin["hop"]), lin["data_addrs"]

    ctrl = AppController(catalog=BlockCatalog.from_gr_kyttar())
    ctrl.set_project(load_project(str(kyt)))
    win = MainWindow(ctrl)
    name_i = win._port_tag_name(0, "x16_in", (hop, int(ri)))
    name_q = win._port_tag_name(0, "x16_in", (hop, int(rq)))

    assert name_i and name_q, (
        f"N={n_fft}: a rail has no name at all (xi {name_i!r}, xq {name_q!r})")
    assert name_i != name_q, (
        f"N={n_fft}: BOTH rails are labelled {name_i!r} — the user cannot tell "
        "the real rail from the imaginary one, and the plot looks like a real "
        "input when it is complex")
    assert name_i == f"{blk}.xi", f"N={n_fft}: I rail named {name_i!r}"
    assert name_q == f"{blk}.xq", f"N={n_fft}: Q rail named {name_q!r}"


def _rails_gate(i_words, q_words, iq):
    """The example's own ingress assertion, reusable by the mutants: two rails,
    each present, DIFFERENT from one another, and each the right half of the
    tone."""
    return bool(
        i_words and q_words
        and len(i_words) == len(q_words) == len(iq)
        and i_words != q_words
        and i_words == [_s16(_q15(c.real)) for c in iq]
        and q_words == [_s16(_q15(c.imag)) for c in iq])


def test_mutation_ingress_corruptions_fail_the_rails_gate():
    """INV-4 for the ingress claim. The gate must REJECT every way the ingress
    could be broken while still producing a plausible-looking plot:

      * both rails driven from the SAME register (the real-input failure this
        example already suffered — the tone splits into two quarter-power peaks
        at bins b and N-b, and every "is there a peak" check still passes);
      * the rails SWAPPED (I into xq, Q into xi) — conjugates the spectrum;
      * a rail delivering NOTHING;
      * a rail delayed by one sample.
    """
    _lin, i_words, q_words, iq = _rail_words(64)
    assert _rails_gate(i_words, q_words, iq), "the honest chain must PASS"

    assert not _rails_gate(i_words, list(i_words), iq), (
        "duplicated rails passed — a REAL input delivered as a fake complex "
        "one would not be caught")
    assert not _rails_gate(q_words, i_words, iq), "swapped rails passed"
    assert not _rails_gate(i_words, [], iq), "an empty Q rail passed"
    assert not _rails_gate(i_words, [0] + q_words[:-1], iq), (
        "a one-sample-delayed Q rail passed")
    assert not _rails_gate([0] * len(i_words), [0] * len(q_words), iq), (
        "an all-zero ingress passed")


def test_mutation_old_namer_rule_would_label_both_rails_the_same(qapp):
    """INV-4 for the label claim, run against the REAL project rather than a
    hand-written stand-in.

    The old rule was "one net on the port -> that net's name for every tag".
    This reproduces it from the shipped ``.kyt``'s ACTUAL connections and shows
    it collides, which is what makes the new label gate load-bearing: the fix
    is not cosmetic renaming, it is that the project genuinely holds only one
    connection for a port that carries two rails.
    """
    from engine.catalog import BlockCatalog
    from engine.io.project_io import load_project
    from model.connection import BlockEndpoint, ChipPortEndpoint
    from ui.controller import AppController
    from ui.main_window import MainWindow

    ctrl = AppController(catalog=BlockCatalog.from_gr_kyttar())
    ctrl.set_project(load_project(str(KYT_PATH)))

    # The OLD rule, verbatim: collect the nets touching the port, and with
    # exactly one net return its name regardless of tag.
    nets = []
    for c in ctrl.project.connections:
        for ep, other in ((c.source, c.target), (c.target, c.source)):
            if (isinstance(ep, ChipPortEndpoint) and ep.chip == 0
                    and ep.port == "x16_in"):
                nets.append(f"{other.block}.{other.port}"
                            if isinstance(other, BlockEndpoint) else c.name)
    assert len(nets) == 1, (
        f"x16_in has {len(nets)} nets {nets} — the collision this gate "
        "describes needs exactly one (the Q half is SYNTHESISED, not stored)")
    old_i = old_q = nets[0]
    assert old_i == old_q == "fft64.xi", (
        "the old rule did not actually collide on 'fft64.xi'")

    # The NEW rule must separate them on the same project.
    win = MainWindow(ctrl)
    new_i = win._port_tag_name(0, "x16_in", (26, 1))
    new_q = win._port_tag_name(0, "x16_in", (26, 2))
    assert new_i != new_q, "the fix does not actually separate the rails"
    assert (new_i, new_q) == ("fft64.xi", "fft64.xq")


def test_iq_rail_naming_leaves_real_scalar_ports_alone():
    """The rail-naming rule must not invent an ``xq`` for a REAL block input.
    A scalar port has no I/Q sibling, so the plain net label must survive —
    otherwise every single-rail example would gain a phantom second rail."""
    from engine.catalog import BlockCatalog
    from engine.grc_import import _iq_sibling

    cat = BlockCatalog.from_gr_kyttar()
    # GainBlock is the canonical real, single-input block.
    assert _iq_sibling(cat, "GainBlock", "in", want_out=False, params=None) is None
    # while the FFT's xi genuinely has one
    assert _iq_sibling(cat, "FFT64Block", "xi", want_out=False,
                       params=None) == "xq"
    assert _iq_sibling(cat, "FFT32Block", "xi", want_out=False,
                       params=None) == "xq"


# =============================================================================
# 6. BIN -> Hz — the frequency mapping the display publishes
# =============================================================================
# "The 'FFT bin' doesn't tell me what frequency it captured." A bin index is
# dimensionless; the array is asynchronous and has no clock, so the sample rate
# is DECLARED by the stimulus (SAMP_RATE / the .grcs' samp_rate). Bin k of N at
# rate fs is k*fs/N, with bins at or above N/2 being the negative frequencies
# (k-N)*fs/N. These gates pin that map against LITERALS, pin the axis the
# shipped .grcs actually configure, and pin the peak's frequency measured on
# the real chip.

@pytest.mark.parametrize("n_fft,want_bin_hz,want_tone_hz", [
    (64, 500.0, 5500.0),
    (32, 1000.0, 11000.0),
])
def test_bin_to_hz_mapping_pinned(n_fft, want_bin_hz, want_tone_hz):
    """``bin_hz = fs/N`` and ``f(k) = k*fs/N`` — against literals, at both
    shipped sizes. The SAME bin index is twice the frequency at N=32, because
    a half-length transform has twice the bin width."""
    assert SAMP_RATE == 32000.0
    assert bin_hz(n_fft) == want_bin_hz
    assert bin_to_hz(TONE_BIN, n_fft) == want_tone_hz
    assert bin_to_hz(0, n_fft) == 0.0
    # The positive half is k*fs/N right up to (but not including) N/2 ...
    assert bin_to_hz(n_fft // 2 - 1, n_fft) == (n_fft // 2 - 1) * want_bin_hz
    # ... and bins at or above N/2 are NEGATIVE frequencies.
    assert bin_to_hz(n_fft // 2, n_fft) == -(n_fft / 2) * want_bin_hz
    assert bin_to_hz(n_fft - 1, n_fft) == -want_bin_hz
    with pytest.raises(ValueError):
        bin_to_hz(n_fft, n_fft)


@pytest.mark.parametrize("n_fft", [64, 32])
def test_centred_axis_is_monotonic_and_spans_the_band(n_fft):
    """The display axis is ``-fs/2 + i*bin_hz`` — monotonic (which is the whole
    reason for the fftshift; the natural order jumps from +fs/2 to -fs/2 and no
    linear axis can label that) and covering exactly one band."""
    axis = axis_hz(n_fft)
    step = bin_hz(n_fft)
    assert len(axis) == n_fft
    assert axis[0] == -SAMP_RATE / 2
    assert axis[-1] == SAMP_RATE / 2 - step
    assert all(axis[i + 1] - axis[i] == step for i in range(n_fft - 1))
    # the fftshift map and bin_to_hz must AGREE for every bin
    shift = fftshift_order(n_fft)
    for k in range(n_fft):
        assert axis[shift[k]] == bin_to_hz(k, n_fft), (
            f"N={n_fft} bin {k}: shifted to axis point {shift[k]} = "
            f"{axis[shift[k]]} Hz, but bin_to_hz says {bin_to_hz(k, n_fft)}")


@pytest.mark.parametrize("n_fft,want_hz,want_index", [
    (64, 5500.0, 43),
    (32, 11000.0, 27),
])
def test_peak_frequency_on_the_real_chip(n_fft, want_hz, want_index):
    """THE answer to "what frequency did it capture": run the built chip and
    read the peak off the SAME centred Hz axis the .grc plots.

    Measured: N=64 -> point 43 = +5500.0 Hz at -0.92 dBFS; N=32 -> point 27 =
    +11000.0 Hz at -0.92 dBFS.
    """
    key = ("size", n_fft)
    if key not in _CHAIN:
        _ctrl, bres, _cat, _ct = build_chain(n_fft)
        iq = tone(n_fft=n_fft)
        _CHAIN[key] = (bres, iq, run_chain(bres, iq),
                       sum(c.cell_count for c in bres.chips.values()))
    out = _CHAIN[key][2]

    axis = axis_hz(n_fft)
    for f, frame in enumerate(frames_of(out, n_fft)):
        centred = centred_spectrum(natural_spectrum(frame, n_fft), n_fft)
        i = int(np.argmax(centred))
        assert i == want_index, (
            f"N={n_fft} frame {f}: peak at centred point {i} "
            f"({axis[i]:.0f} Hz), expected {want_index} ({want_hz:.0f} Hz)")
        assert axis[i] == want_hz, (
            f"N={n_fft} frame {f}: peak at {axis[i]:.0f} Hz, "
            f"expected {want_hz:.0f} Hz")
        assert centred[i] > COHERENT_MIN
        # and it is the SAME statement as the bin claim
        assert axis[i] == bin_to_hz(TONE_BIN, n_fft)


def test_mutation_wrong_sample_rate_moves_the_frequency():
    """INV-4: the Hz claim is a claim about fs, not a constant. Halving the
    declared rate must halve every frequency — a mapping that ignored fs would
    keep reading 5500 Hz."""
    assert bin_to_hz(TONE_BIN, 64, samp_rate=16000.0) == 2750.0
    assert bin_to_hz(TONE_BIN, 64, samp_rate=16000.0) != bin_to_hz(TONE_BIN, 64)


def test_mutation_unshifted_axis_mislabels_the_negative_half():
    """INV-4: reading the NATURAL-order vector against the centred axis (i.e.
    forgetting the fftshift) puts the tone at the wrong frequency — which is
    exactly the wrong-but-plausible plot the shift exists to prevent."""
    axis = axis_hz(N_FFT)
    # natural bin 11 sits at index 11 of an UNSHIFTED vector; on the centred
    # axis index 11 is -16000 + 11*500 = -10500 Hz, not +5500.
    assert axis[TONE_BIN] == -10500.0
    assert axis[TONE_BIN] != bin_to_hz(TONE_BIN, N_FFT)


# =============================================================================
# 7. The shipped .grcs really carry the frequency axis + the stimulus scope
# =============================================================================
# The gates above prove the ARITHMETIC. These prove the shipped flowgraphs are
# actually configured with it — a correct mapping the .grc doesn't use would
# leave the user staring at "FFT bin" exactly as before.

def _grc_doc(path):
    import yaml
    with open(path) as fh:
        return yaml.safe_load(fh)


def _grc_block(doc, name):
    for b in doc.get("blocks", []):
        if b.get("name") == name:
            return b
    raise AssertionError(f"no block named {name!r} in the .grc")


@pytest.mark.parametrize("grc,n_fft,want_bin_hz", [
    (GRC_PATH, 64, 500.0),
    (GRC32_PATH, 32, 1000.0),
])
def test_shipped_grc_plots_a_frequency_axis(grc, n_fft, want_bin_hz):
    """The vector sink is configured with a REAL Hz axis: units "Hz", origin
    ``-samp_rate/2``, step ``bin_hz`` — not the old dimensionless
    ``x_start=0, x_step=1`` bin index."""
    doc = _grc_doc(grc)
    params = {b["name"]: b["parameters"].get("value")
              for b in doc["blocks"] if b.get("id") == "variable"}
    assert params.get("samp_rate") == "32000", (
        f"{grc.name}: samp_rate is {params.get('samp_rate')!r}, expected 32000 "
        "— the documented Hz mapping is stated at that rate")
    assert params.get("bin_hz") == "samp_rate / n_fft", (
        f"{grc.name}: bin_hz is {params.get('bin_hz')!r} — the bin width must "
        "be DERIVED from samp_rate and n_fft, never a hard-coded number")
    assert float(params["samp_rate"]) / n_fft == want_bin_hz

    sink = _grc_block(doc, "spectrum_sink")["parameters"]
    assert sink["x_units"] == '"Hz"', (
        f"{grc.name}: the spectrum x axis is in {sink['x_units']!r}, not Hz — "
        "the user still cannot tell what frequency the peak is on")
    assert sink["x_start"] == "-samp_rate / 2", (
        f"{grc.name}: x_start is {sink['x_start']!r}")
    assert sink["x_step"] == "bin_hz", f"{grc.name}: x_step is {sink['x_step']!r}"
    assert "Hz" in sink["x_axis_label"] and "samp_rate" in sink["x_axis_label"], (
        f"{grc.name}: the x label {sink['x_axis_label']!r} does not state the "
        "bin -> Hz mapping")


@pytest.mark.parametrize("grc,n_fft", [(GRC_PATH, 64), (GRC32_PATH, 32)])
def test_shipped_grc_display_block_fftshifts(grc, n_fft):
    """The display block un-reverses AND fftshifts, so the vector it emits is
    ascending in frequency and the linear Hz axis labels it correctly. Without
    the shift the axis would be monotonic but the DATA would not be."""
    src = _grc_block(_grc_doc(grc), "unreverse")["parameters"]["_source_code"]
    assert "self.shift" in src, (
        f"{grc.name}: the display block has no fftshift — a linear Hz axis "
        "would mislabel every negative-frequency bin")
    assert "(np.arange(self.n) + self.n // 2) % self.n" in src, (
        f"{grc.name}: the shift is not the fftshift permutation")
    assert "centred[self.shift] = nat" in src, (
        f"{grc.name}: the shift is computed but not APPLIED to the output")
    # and the permutation the .grc computes matches the module's published one
    n = int(n_fft)
    assert [(k + n // 2) % n for k in range(n)] == fftshift_order(n)


@pytest.mark.parametrize("grc,n_fft", [(GRC_PATH, 64), (GRC32_PATH, 32)])
def test_shipped_grc_shows_the_stimulus(grc, n_fft):
    """The flowgraph carries a time scope on the I/Q stimulus, so a user can
    SEE the sinusoid the spectral spike comes from rather than only the Q15
    word-stream staircase at the port. Sized 4 frames and connected to the
    source, at the declared samp_rate."""
    doc = _grc_doc(grc)
    scope = _grc_block(doc, "stim_scope")
    p = scope["parameters"]
    assert scope["id"] == "qtgui_time_sink_x"
    assert p["type"] == "complex", (
        f"{grc.name}: the stimulus scope is {p['type']!r}, not complex — it "
        "would show one rail and hide the other")
    assert p["size"] == "4 * n_fft"
    assert p["srate"] == "samp_rate", (
        f"{grc.name}: the stimulus scope's time axis is not at samp_rate")
    assert ["src", "0", "stim_scope", "0"] in [
        list(c) for c in doc["connections"]], (
        f"{grc.name}: the stimulus scope is not connected to the source")
    # A QT time_sink draws NOTHING until a FULL ``size`` buffer arrives, so a
    # scope on a FINITE stream must be sized under it. This scope is NOT on a
    # finite stream: it taps ``src``, the vector source, which ships with
    # ``repeat = True`` and streams the stimulus forever — only ``ksrc``
    # dispatches the single burst. So the scope fills whatever its size
    # (measured: a 256-sample scope on the N=64 source receives 768+ samples).
    # What MUST hold is that the source really is the repeating one.
    assert _grc_block(doc, "src")["parameters"]["repeat"] == "True", (
        f"{grc.name}: the stimulus source no longer repeats, so a "
        f"{4 * n_fft}-sample scope on a {burst_of(n_fft)}-sample burst may "
        "never fill and would paint a blank window")
    # And the window must span enough CYCLES of the tone to read as a sinusoid
    # rather than as the few-samples-per-cycle staircase the port trace shows.
    cycles = 4 * n_fft * TONE_BIN / n_fft
    assert cycles >= 40, (
        f"{grc.name}: the stimulus scope spans only {cycles:g} cycles of the "
        "tone — too few to look like a sinusoid")


@pytest.mark.parametrize("n_fft,want_sps", [(64, 64 / 11), (32, 32 / 11)])
def test_port_trace_staircase_is_expected_not_a_defect(n_fft, want_sps):
    """The third user report: "the waveform isn't a sinusoid". It IS the tone —
    what the port trace draws is the Q15 WORD stream, one step per sample, and
    the shipped stimulus has only ``n_fft/tone_bin`` samples per cycle. Pinning
    that number is what makes "a staircase is expected" a MEASURED statement
    rather than an excuse.

    A trace is legible as a sinusoid at roughly 10+ samples per cycle; the
    shipped stimulus is far under that at BOTH sizes, so the .grc ships a
    stimulus scope and the README points at lowering ``tone_bin``.
    """
    lin, i_words, q_words, iq = _rail_words(n_fft)
    sps = n_fft / TONE_BIN
    assert abs(sps - want_sps) < 1e-9
    assert sps < 10, (
        f"N={n_fft}: {sps:.2f} samples per cycle would already look smooth — "
        "the staircase explanation no longer applies and the docs are stale")
    # tone_bin = 1 is the documented escape hatch: one whole cycle per frame.
    assert n_fft / 1 == n_fft >= 32, (
        "tone_bin = 1 must give a full frame of samples per cycle")
    # and the words really are a sampled sinusoid, not a square/constant
    assert len(set(i_words)) > 4 and len(set(q_words)) > 4, (
        f"N={n_fft}: a rail carries fewer than 5 distinct values — that is not "
        "a sampled sinusoid")
    peak = max(max(abs(v) for v in i_words), max(abs(v) for v in q_words))
    assert abs(peak - _s16(_q15(AMPLITUDE))) <= 1, (
        f"N={n_fft}: rail peak {peak}, expected the tone amplitude "
        f"{_s16(_q15(AMPLITUDE))}")


@pytest.mark.parametrize("grc,want_hz", [(GRC_PATH, "5500"), (GRC32_PATH, "11000")])
def test_shipped_grc_states_the_peak_frequency(grc, want_hz):
    """The flowgraph's own description tells the user, in Hz, where the peak
    is — so the example is self-explanatory opened cold, without the README."""
    doc = _grc_doc(grc)
    desc = doc["options"]["parameters"]["description"]
    assert want_hz in desc and "Hz" in desc, (
        f"{grc.name}: the description never states the peak frequency")
    assert "k*fs/N" in desc, (
        f"{grc.name}: the description does not state the bin -> Hz mapping")


# =============================================================================
# 8. Report
# =============================================================================
def test_zz_write_report(request):
    """Emit the example's report LAST, and ONLY if the session had zero
    failures (the report file is unlinked first, so a failing run leaves no
    stale green artifact behind)."""
    from kyttar_verify.session_report import write_session_report

    c = _chain()
    nat = natural_spectrum(frames_of(c["out"])[0])
    write_session_report("FftSpectrumExample", {
        "metric": "exact",
        "n_compared": len(c["out"]),
        "max_abs_err": 0.0,
        "tolerance": 0.0,
        "nmse_db": None,
        "correlation": None,
        "bit_errors": 0,
        "delay_used": 0,
        "coverage": {
            "edge": True,
            "random": 1,
            "mutation": True,
            "cells": c["cells"],
            "n_fft": N_FFT,
            "latency": LATENCY,
            "tone_bin": TONE_BIN,
            "tone_slot": TONE_SLOT,
            "coherent_bin_power": round(nat[TONE_BIN] / 32768.0, 4),
            "output_order": "bit_reversed (un-reversed + fftshifted for display)",
            "scale": "fft_over_64",
            "user_path": True,
            "samp_rate": SAMP_RATE,
            "bin_hz": bin_hz(N_FFT),
            "tone_hz": bin_to_hz(TONE_BIN, N_FFT),
            "tone_hz_32": bin_to_hz(TONE_BIN, 32),
            "complex_ingress": "xi/xq on distinct registers, distinctly labelled",
        },
    })
