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

  4. **MANDATORY mutations (INV-4)** — a display path that does NOT un-reverse,
     one that un-reverses with the wrong width, one at the wrong scale, and a
     frame read at the wrong latency offset must ALL fail the bin gate. The
     un-reversal is the one thing a plausible-looking wrong plot hides, so it
     is attacked directly.

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
    N_FFT, SIZES, TONE_BIN, build_chain, burst_of, frames_of, latency_of,
    natural_spectrum, reference_power, run_chain, tone, unreverse)

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
# 5. Report
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
            "output_order": "bit_reversed (un-reversed for display)",
            "scale": "fft_over_64",
            "user_path": True,
        },
    })
