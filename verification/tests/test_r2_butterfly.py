# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify R2ButterflyBlock — the radix-2 DIF butterfly with the pinned
unconditional scale-by-2 and round-half-to-even (RHE) rounding.

There is no GNU Radio counterpart block: the golden is the Python integer
reference in ``gr_kyttar.placement.blocks.fft_primitives`` (``rhe_half_sum`` /
``rhe_half_diff``), itself the pinned result of the FFT numeric design spike.
This suite therefore carries TWO independent reference tiers:

  * an INDEPENDENT true-17-bit RHE implementation (``_rhe17`` below: numpy
    int64, ``k = v >> 1; k + ((v & k) & 1)``, clamped) — cross-checked
    EXHAUSTIVELY against the block's 16-bit-safe decomposition (which never
    materializes the 17-bit ``v``), so the fabric formula
    ``k = floor(a/2) ± floor(b/2) ± carry; corr = ((a XOR b) AND k) AND 1``
    is PROVEN equal to the textbook definition, not assumed;
  * the on-chip DUT vs that reference, BIT-EXACT (tol 0), over edge + random
    (>=3 seeds) + two-tone/sine/impulse-shaped and ADVERSARIAL exact-full-scale
    stimulus, including the single reachable saturation tie
    (``a=+0x7FFF, b=-0x8000`` on a difference rail -> clamps +0x7FFF).

Output capture: the butterfly has TWO complex output pairs (sum, diff) on two
physically-separate output cells; ``run_block_dut_complex2_dual`` wires BOTH
pairs to ``x16_out`` on their own nets with per-rail out_tags (sum -> dests
0/1, diff -> 2/3) and demuxes by dest — order-free, since the two packets'
interleave at the port varies with corridor length/orientation.

Mutations proven to FAIL (INV-4): I/Q rail swap, sum/diff cross-swap, sign
inversion, HALF-UP-instead-of-RHE at the tie points, DROPPED SATURATION
(wrap) at the tie, +1 sample delay, empty output, corrupted single rail.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_r2_butterfly.py -x -q
"""
from __future__ import annotations

import os
import random
import sys
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PLACEKYT = Path(__file__).resolve().parents[2] / "placekyt"
_VERIFY = Path(__file__).resolve().parents[1]
_RUNTIME = Path(__file__).resolve().parents[2] / "runtime" / "python"
for p in (str(_PLACEKYT), str(_VERIFY), str(_RUNTIME)):
    if p not in sys.path:
        sys.path.insert(0, p)

from kyttar_verify import (  # noqa: E402
    run_block_dut_complex2_dual, write_report, CompareResult, Metric,
    D4_ORIENTATIONS)
from gr_kyttar.placement.blocks.fft_primitives import (  # noqa: E402
    R2ButterflyBlock, rhe_half_sum, rhe_half_diff, s16, u16)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
pytestmark = pytest.mark.skipif(not os.path.exists(CHIP_YAML),
                                reason="chip yaml absent")


def _q15(v: float) -> int:
    return max(-32768, min(32767, int(round(v * 32768.0)))) & 0xFFFF


def _words(stream):
    return ([_q15(c.real) for c in stream], [_q15(c.imag) for c in stream])


def _ref(a, b):
    ai, aq = _words(a)
    bi, bq = _words(b)
    return R2ButterflyBlock("ref").process_reference_q15(ai, aq, bi, bq)


def _run(a, b, **kw):
    r = run_block_dut_complex2_dual("R2ButterflyBlock", a, b,
                                    chip_yaml=CHIP_YAML, **kw)
    assert r.ok, r.reason
    return r


# --- independent 17-bit-true-value RHE (the transcribed spike definition) ----

def _rhe17(v: int, sat: bool = True) -> int:
    """RHE(v/2) computed on the TRUE (17-bit-capable) value: k = floor(v/2);
    +1 iff v odd and k odd; clamped to Q15."""
    v = int(v)
    k = v >> 1
    r = k + ((v & k) & 1)
    if sat:
        r = max(-32768, min(32767, r))
    return r & 0xFFFF


def _rhe17_halfup(v: int) -> int:
    """The WRONG-rounding mutation: round-half-UP (floor((v+1)/2))."""
    r = (int(v) + 1) >> 1
    return u16(max(-32768, min(32767, r)))


def _rhe17_nosat(v: int) -> int:
    """The DROPPED-SATURATION mutation: 16-bit wrap instead of the clamp."""
    v = int(v)
    k = v >> 1
    return u16(k + ((v & k) & 1))


# Stimulus builders ----------------------------------------------------------

def _random_streams(seed, n=24, amp=1.0):
    rng = random.Random(seed)
    mk = lambda: [complex(rng.uniform(-amp, amp), rng.uniform(-amp, amp))  # noqa: E731
                  for _ in range(n)]
    return mk(), mk()


_FS = 32767.0 / 32768.0
# Adversarial exact-full-scale pairs, incl. the single reachable saturation
# tie (a=+0x7FFF, b=-0x8000 -> diff rail 65535/2 -> RHE 32768 -> clamp) on
# both rails, and max-magnitude sums.
_ADVERSARIAL_A = [complex(_FS, -1.0), complex(-1.0, _FS), complex(_FS, _FS),
                  complex(-1.0, -1.0), complex(1.0, 1.0), complex(0.0, _FS)]
_ADVERSARIAL_B = [complex(-1.0, _FS), complex(_FS, -1.0), complex(_FS, _FS),
                  complex(-1.0, -1.0), complex(-1.0, -1.0), complex(-1.0, 0.0)]


def _tones(n=32):
    """Full-scale complex sine + two-tone + impulse (the spike's signal
    classes), Q15-grid snapped by the drive."""
    t = np.arange(n)
    a = _FS * np.exp(1j * 2 * np.pi * 3 * t / n)
    b = (0.45 * np.exp(1j * 2 * np.pi * 3 * t / n)
         + 0.45 * np.exp(1j * (2 * np.pi * 7.37 * t / n + 0.61)))
    imp = np.zeros(n, complex)
    imp[3] = 0.999
    return list(a), list(b + imp)


# --- reference self-consistency (the transcription gate) ---------------------

_CORNER_WORDS = [0x8000, 0x8001, 0xFFFF, 0x0000, 0x0001, 0x0002, 0x0003,
                 0x7FFE, 0x7FFF, 0x4000, 0xC000, 0x5555, 0xAAAA]


def test_reference_equals_true_rhe_exhaustive_corners():
    """The block's 16-bit-safe decomposition == the true-17-bit RHE definition
    for EVERY corner-word pair (both legs) — the formula transcription is
    proven, not assumed."""
    for aw in _CORNER_WORDS:
        for bw in _CORNER_WORDS:
            a, b = s16(aw), s16(bw)
            assert rhe_half_sum(aw, bw) == _rhe17(a + b), (aw, bw)
            assert rhe_half_diff(aw, bw) == _rhe17(a - b), (aw, bw)


def test_reference_equals_true_rhe_random():
    rng = random.Random(1234)
    for _ in range(20000):
        aw, bw = rng.randrange(0x10000), rng.randrange(0x10000)
        a, b = s16(aw), s16(bw)
        assert rhe_half_sum(aw, bw) == _rhe17(a + b), (aw, bw)
        assert rhe_half_diff(aw, bw) == _rhe17(a - b), (aw, bw)


def test_rhe_tie_points_round_half_to_even():
    """At every tie (odd v) the result must be the EVEN neighbour of v/2."""
    for v in list(range(-31, 32, 2)) + [65535, 65533, -65535, 32767, -32767]:
        r = s16(_rhe17(v))
        true = v / 2.0
        assert abs(r - max(-32768, min(32767, true))) <= 0.5
        if -32768 <= v // 2 and (v >> 1) + 1 <= 32767:
            assert r % 2 == 0, f"tie v={v} rounded to ODD {r}"


def test_sum_leg_overflow_impossible():
    """The SUM leg's k+corr provably never leaves [-32768, 32767]: for every
    corner pair the UNCLAMPED value equals the clamped one (the diff leg's
    single reachable overflow is exercised separately)."""
    for aw in _CORNER_WORDS:
        for bw in _CORNER_WORDS:
            v = s16(aw) + s16(bw)
            k = v >> 1
            assert -32768 <= k + ((v & k) & 1) <= 32767, (aw, bw)


def test_diff_tie_saturates():
    """a=+0x7FFF, b=-0x8000: true RHE((a-b)/2) = 32768 -> the mandatory
    saturating combine clamps to +0x7FFF."""
    assert rhe_half_diff(0x7FFF, 0x8000) == 0x7FFF
    # ... and the true value really is out of range (the clamp is live):
    assert _rhe17_nosat(32767 - (-32768)) == 0x8000  # wraps without the clamp


# --- structure / budget ------------------------------------------------------

def test_cell_budget_and_layout():
    """All 8 cells fit (max_data_address + instructions <= 31); the landing
    cell's first entry is the counting join; both output cells keep >=1 free
    word for the INV-17 fan-out JUMP; the 2x4 fold is even-column with I/O
    co-located on the top edge."""
    from gr_kyttar.placement.resolver import CellProgramResolver
    blk = R2ButterflyBlock("probe")
    cps = blk.build_cell_programs()
    assert list(cps) == ["pair", "sumi", "diffi", "sumq", "diffq", "relay",
                         "sum_out", "diff_out"]
    r = CellProgramResolver()
    for cid, cp in cps.items():
        n_instr = r.count_instructions(cp)
        regs = [p.register for p in cp.inputs] \
            + [d.address for d in (cp.data or ())] \
            + [s.register for s in (cp.state or ())]
        max_addr = max([a for a in regs if a is not None], default=-1)
        assert max_addr + n_instr <= 31, (
            f"{cid}: {n_instr} instr from addr {max_addr + 1} overflow the cell")
        if cid in ("sum_out", "diff_out"):
            used = n_instr + len(cp.inputs) + len(cp.data or ()) \
                + len(cp.state or ())
            assert 32 - used >= 1, f"{cid}: no room for the fan-out JUMP"
    assert cps["pair"].entries[0].name == "join"
    lay = blk.default_layout()
    xs = {x for (x, _y, _f) in lay.values()}
    ys = {y for (_x, y, _f) in lay.values()}
    assert max(xs) - min(xs) + 1 == 2 and max(ys) - min(ys) + 1 == 4
    assert lay["pair"][:2] == (0, 0) and lay["diff_out"][:2] == (1, 0)


def test_drives_and_captures():
    a, b = _random_streams(1, 8)
    r = _run(a, b)
    assert r.in_regs == (0, 1, 2, 3)
    assert set(r.streams) == {0, 1, 2, 3}
    assert all(len(v) == len(a) for v in r.streams.values())


# --- DUT bit-exact vs the golden (tol 0) -------------------------------------

def _assert_bitexact(a, b, r=None):
    si, sq, di, dq = _ref(a, b)
    r = r or _run(a, b)
    assert r.streams.get(0) == si, "sum I rail diverges"
    assert r.streams.get(1) == sq, "sum Q rail diverges"
    assert r.streams.get(2) == di, "diff I rail diverges"
    assert r.streams.get(3) == dq, "diff Q rail diverges"
    return r


def test_bitexact_edge_adversarial():
    """Exact-full-scale adversarial pairs, incl. the saturation tie — the
    clamp path is exercised ON CHIP and gated."""
    si, sq, di, dq = _ref(_ADVERSARIAL_A, _ADVERSARIAL_B)
    # the tie really saturates in this stimulus (non-vacuity):
    assert 0x7FFF in (di + dq)
    _assert_bitexact(_ADVERSARIAL_A, _ADVERSARIAL_B)


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_bitexact_random(seed):
    a, b = _random_streams(seed)
    _assert_bitexact(a, b)


def test_bitexact_tones():
    a, b = _tones()
    _assert_bitexact(a, b)


def test_float_reference_parity():
    """The float reference ((a±b)/2 clipped) agrees with the bit-exact one
    within the 1-LSB RHE quantization everywhere in range."""
    a, b = _random_streams(5, 40)
    si, sq, di, dq = _ref(a, b)
    fs, fd = R2ButterflyBlock("ref").process_reference(a, b)
    for k in range(len(a)):
        assert abs(s16(si[k]) / 32768.0 - fs[k].real) <= 1.01 / 32768.0
        assert abs(s16(sq[k]) / 32768.0 - fs[k].imag) <= 1.01 / 32768.0
        assert abs(s16(di[k]) / 32768.0 - fd[k].real) <= 1.01 / 32768.0
        assert abs(s16(dq[k]) / 32768.0 - fd[k].imag) <= 1.01 / 32768.0


# --- MANDATORY mutations (INV-4) ---------------------------------------------

def test_mutation_halfup_rounding_fails():
    """The WRONG-ROUNDING mutation: a half-up model must FAIL the gate at the
    tie points — proof the RHE tie behaviour is genuinely under test."""
    # Odd-sum pairs whose k is ODD (RHE rounds up) and EVEN (RHE rounds down):
    a = [complex(3 / 32768.0, 5 / 32768.0), complex(1 / 32768.0, 7 / 32768.0)]
    b = [complex(0.25, 0.25), complex(-0.125, 0.5)]
    ai, aq = _words(a)
    bi, bq = _words(b)
    mut_si = [_rhe17_halfup(s16(x) + s16(y)) for x, y in zip(ai, bi)]
    si, sq, di, dq = _ref(a, b)
    assert mut_si != si, "half-up == RHE on the tie stimulus — stimulus has no teeth"
    r = _run(a, b)
    assert r.streams.get(0) == si
    assert r.streams.get(0) != mut_si, "DUT matches HALF-UP — wrong rounding shipped!"


def test_mutation_dropped_saturation_fails():
    """The DROPPED-SATURATION mutation: a wrap model must FAIL on the
    saturation tie — proof the on-chip clamp is genuinely under test."""
    a = [complex(_FS, 0.1), complex(0.2, _FS)]
    b = [complex(-1.0, 0.05), complex(0.1, -1.0)]
    ai, aq = _words(a)
    bi, bq = _words(b)
    wrap_di = [_rhe17_nosat(s16(x) - s16(y)) for x, y in zip(ai, bi)]
    wrap_dq = [_rhe17_nosat(s16(x) - s16(y)) for x, y in zip(aq, bq)]
    si, sq, di, dq = _ref(a, b)
    assert wrap_di != di or wrap_dq != dq, "wrap == sat — stimulus has no teeth"
    r = _run(a, b)
    assert r.streams.get(2) == di and r.streams.get(3) == dq
    assert (r.streams.get(2), r.streams.get(3)) != (wrap_di, wrap_dq), \
        "DUT matches the WRAP model — the saturating combine is missing!"


def test_mutation_iq_rail_swap_fails():
    a, b = _random_streams(11, 16)
    si, sq, di, dq = _ref(a, b)
    r = _run(a, b)
    assert r.streams.get(0) != sq or r.streams.get(1) != si, \
        "degenerate stimulus (I==Q)"
    assert not (r.streams.get(0) == sq and r.streams.get(1) == si), \
        "gate cannot detect an I/Q rail swap"
    # and the swapped assignment must fail the gate:
    assert r.streams.get(0) == si and r.streams.get(1) == sq


def test_mutation_sum_diff_swap_fails():
    """Sum/diff stream cross-assignment must fail (the two pairs are
    distinguishable and correctly tagged)."""
    a, b = _random_streams(13, 16)
    si, sq, di, dq = _ref(a, b)
    r = _run(a, b)
    assert r.streams.get(0) != di and r.streams.get(2) != si, \
        "sum and diff coincide — degenerate stimulus"


def test_mutation_sign_inverted_fails():
    a, b = _random_streams(17, 16)
    si, _sq, _di, _dq = _ref(a, b)
    r = _run(a, b)
    inv = [(0x10000 - w) & 0xFFFF for w in r.streams.get(0)]
    assert inv != si, "gate cannot detect an inverted output"


def test_mutation_swapped_operands_fails_diff():
    """diff(a,b) != diff(b,a): a DUT driven with swapped streams must fail the
    (a, b) diff gate (sum is commutative — documented, not a corruption)."""
    a, b = _random_streams(19, 16)
    si, sq, di, dq = _ref(a, b)
    r = _run(b, a)                       # swapped drive
    assert r.streams.get(2) != di, "gate cannot detect swapped operands"
    assert r.streams.get(0) == si, "sum must stay commutative"


def test_mutation_one_sample_offset_fails():
    a, b = _random_streams(23, 16)
    si, *_ = _ref(a, b)
    r = _run(a, b)
    shifted = [0x0000] + r.streams.get(0)[:-1]
    assert shifted != si, "gate cannot detect a +1 sample delay"


def test_mutation_empty_output_fails():
    a, b = _random_streams(29, 8)
    si, *_ = _ref(a, b)
    assert [] != si


def test_mutation_corrupt_single_rail_fails():
    """Corrupting ONLY stream a's Q rail must fail the Q-side gates while the
    I-side stays clean — every rail is genuinely under test."""
    a, b = _random_streams(31, 16)
    a_bad = [complex(c.real, -c.imag if abs(c.imag) > 0.05 else 0.3) for c in a]
    si, sq, di, dq = _ref(a, b)
    r = _run(a_bad, b)
    assert r.streams.get(0) == si and r.streams.get(2) == di, \
        "I rails must stay clean under an aq-only corruption"
    assert r.streams.get(1) != sq and r.streams.get(3) != dq, \
        "gate cannot detect an aq corruption"


# --- orientation invariance (INV-23, all 8 D4) -------------------------------

@pytest.mark.parametrize(
    "orient", D4_ORIENTATIONS[1:],
    ids=["+".join(o) for o in D4_ORIENTATIONS[1:]])
def test_orientation_invariant(orient):
    """Identical demuxed streams in every D4 orientation.  The dest-tag demux
    is deliberately interleave-free: the two packets' arrival ORDER at the
    port varies with corridor length, but each tagged stream must be
    IDENTICAL."""
    a, b = _random_streams(3, 10)
    base = _run(a, b)
    rot = _run(a, b, orient=list(orient))
    assert rot.streams == base.streams, (
        f"{'+'.join(orient)}: demuxed streams diverge from identity")


# --- SATURATED (pipelined) gate — bespoke (INV-19/20) ------------------------
# The shared test_pipeline_saturation harnesses deliver all operands with ONE
# JUMP per sample; this block's samples are TWO packets (two JUMPs) for its
# counting join, and it has two complex output pairs — the saturated gate
# lives here (NEEDS_BESPOKE points at this file).

def test_pipelined_equals_per_sample():
    a, b = _random_streams(21, 32)      # full-range incl. saturating samples
    seq = _run(a, b)
    pipe = _run(a, b, pipelined=True)
    si, sq, di, dq = _ref(a, b)
    # non-vacuity: the per-sample run equals the independent reference…
    assert seq.streams == {0: si, 1: sq, 2: di, 3: dq}
    assert len(set(si)) > 4, "degenerate stimulus"
    # …and the saturated run equals it bit-exact, stream for stream.
    assert pipe.streams == seq.streams, (
        "saturated (queue_words) output diverges from per-sample — "
        "join/handshake hazard under back-to-back drive")


def test_pipelined_drive_is_not_vacuous():
    a, b = _random_streams(37, 16)
    good = _run(a, b, pipelined=True)
    swapped = _run(b, a, pipelined=True)
    assert good.streams and swapped.streams
    assert good.streams != swapped.streams, (
        "swapping the pipelined streams changed nothing — vacuous drive")


# --- GRC import: numeric OUTPUT index counts COMPLEX pairs -------------------

_BFLY_GRC = """options:
  parameters: {id: min_bfly, generate_options: qt_gui}
  states: {coordinate: [8, 8], rotation: 0, state: enabled}
blocks:
- name: mixa
  id: kyttar_complex_mixer
  parameters: {frequency: '1000', sample_rate: '48000'}
  states: {coordinate: [100, 100], rotation: 0, state: enabled}
- name: mixb
  id: kyttar_complex_mixer
  parameters: {frequency: '2000', sample_rate: '48000'}
  states: {coordinate: [100, 240], rotation: 0, state: enabled}
- name: src
  id: kyttar_source
  parameters: {device_id: '"kyttar_0"', complex_in: 'True'}
  states: {coordinate: [20, 160], rotation: 0, state: enabled}
- name: bfly
  id: kyttar_r2_butterfly
  parameters: {device_id: '"kyttar_0"'}
  states: {coordinate: [300, 160], rotation: 0, state: enabled}
- name: snk
  id: kyttar_sink
  parameters: {device_id: '"kyttar_0"'}
  states: {coordinate: [520, 120], rotation: 0, state: enabled}
- name: snk2
  id: kyttar_sink
  parameters: {device_id: '"kyttar_0"'}
  states: {coordinate: [520, 220], rotation: 0, state: enabled}
connections:
- [src, '0', mixa, '0']
- [src, '0', mixb, '0']
- [mixa, '0', bfly, '0']
- [mixb, '0', bfly, '1']
- [bfly, '0', snk, '0']
- [bfly, '1', snk2, '0']
"""


def test_grc_import_output_index_counts_complex_pairs():
    """IMPORTER contract for the first 2-complex-OUTPUT-pair block: GNURadio's
    numeric port index counts COMPLEX ports on the OUTPUT side too, so
    ``[bfly, '1', ...]`` must resolve to the SECOND pair's I-half (``do_i``),
    NOT the first pair's Q-half (``so_q``); the port split then synthesises
    each wired pair's Q net; the four input arms all join-elect; and the
    imported design auto-P&Rs and BUILDS."""
    import tempfile
    from engine.catalog import BlockCatalog
    from engine.grc_import import import_grc
    from engine.io.chip_type_io import load_chip_type
    from ui.controller import AppController
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])

    cat = BlockCatalog.from_gr_kyttar()
    with tempfile.NamedTemporaryFile("w", suffix=".grc", delete=False) as tf:
        tf.write(_BFLY_GRC)
        path = tf.name
    try:
        res = import_grc(path, cat, chip_type="kyttar_10x12")
    finally:
        os.unlink(path)
    assert res.ok and not res.unknown, res.unknown
    bname = next(b.name for b in res.project.blocks
                 if b.type == "R2ButterflyBlock")
    outs = {}
    for c in res.project.connections:
        if getattr(c.source, "block", None) == bname:
            outs.setdefault(c.source.port, []).append(c)
    # index 0 -> sum pair (so_i + synthesised so_q), index 1 -> DIFF pair
    # (do_i + synthesised do_q) — NOT so_q.
    assert "so_i" in outs and "do_i" in outs, (
        f"output index 1 must land on the diff pair's I-half; wired {set(outs)}")
    assert "so_q" in outs and "do_q" in outs, (
        f"each wired pair's Q-half must be synthesised; wired {set(outs)}")
    ins = {c.target.port for c in res.project.connections
           if getattr(c.target, "block", None) == bname}
    assert ins == {"ai", "aq", "bi", "bq"}
    ct = load_chip_type(CHIP_YAML)
    ctk = getattr(ct, "name", None) or "kyttar_10x12"
    ctrl = AppController(catalog=cat)
    ctrl.project = res.project
    assert ctrl.auto_pnr({ctk: ct}).ok, "imported butterfly design did not route"
    bres = ctrl.build()
    assert bres.ok, "build failed: " + "; ".join(str(e) for e in bres.errors)


# --- dashboard report --------------------------------------------------------

def test_emit_report():
    a, b = _random_streams(7, 24)
    si, sq, di, dq = _ref(a, b)
    r = _run(a, b)
    ok = r.streams == {0: si, 1: sq, 2: di, 3: dq}
    assert ok
    write_report("R2ButterflyBlock",
                 CompareResult(passed=ok, metric=Metric.EXACT,
                               n_compared=4 * len(a), max_abs_err=0.0,
                               tolerance=0.0, delay_used=0),
                 coverage={"edge": True, "random": 3, "tones": True,
                           "adversarial_full_scale": True,
                           "rhe_tie_points": True, "saturation": True,
                           "orientation_d4": True, "pipelined": True,
                           "mutation": True})
