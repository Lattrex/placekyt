# SPDX-License-Identifier: GPL-3.0-or-later
"""FFT64Block — 64-point streaming R2SDF FFT, the CHIP-SCALE verification gate.

This is the gate that decides whether ``FFT64Block`` is DONE. It is the
FFT16 battery re-derived for N = 64: the same three-way golden, the same
bit-exact (tol 0) on-chip comparison over stimulus classes and random seeds,
the same INV-4 mutation set, the same order/scale/frame-boundary pins — plus
the two gates the CHIP-SCALE class adds (port reachability on a real built
chip, and the octant-fold steering exercised on chip rather than in Python).

What is DIFFERENT from FFT16, and why:

  * **The block is the sole occupant of the die** (84 cells on a 10x12) and
    its placement is a vertical ctl/out SPINE. There is no D4 orientation
    sweep — a 10-column-tall spine fold cannot rotate on this array, which
    the class declares via ``CHIP_SCALE_ORIENTATIONS = ((),)`` and
    ``test_fft64_fit_limit`` gates. The identity build is what ships.
  * **Stage 0 uses the OCTANT FOLD** (twiddle period 32 busts a direct fetch
    cell), so this suite must exercise the fold's steering ON CHIP — every
    octant, both trivial encodings — not only in the Python model that
    ``test_fft64_fit_limit`` already proves exhaustively.
  * **Latency is 63**, so a class needs 63 + 64*F samples for F frames. Each
    DUT build is shared across the tests that use it (``_RUNS``).

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_fft64.py -q
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parents[2]
for _p in (_ROOT / "runtime" / "python", _ROOT / "placekyt",
           _ROOT / "verification"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from gr_kyttar.placement.blocks.fft_large import (  # noqa: E402
    FFT64Block, direct_dif_reference, output_bins, sdf_streaming_reference,
    stage_delays, stage_table)
from gr_kyttar.placement.blocks.fft_primitives import (  # noqa: E402
    rhe_half_diff, rhe_half_sum, s16, twiddle_cmul_ref, u16)
from kyttar_verify.dut_runner import run_block_dut_complex  # noqa: E402

CHIP_YAML = str(_ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml")

pytestmark = pytest.mark.skipif(
    not os.path.exists(CHIP_YAML), reason="chip yaml absent")

N_FFT = 64
N_STAGES = 6
LATENCY = N_FFT - 1                     # 63
FFT64_OUTPUT_BINS = output_bins(N_FFT)
A_FS = 32767 / 32768.0
FRAMES = 3                              # >= 3 complete output frames per class
CLASS_LEN = LATENCY + N_FFT * FRAMES    # 63 + 192 = 255 samples


# ------------------------------------------------------------------ stimuli
def _q15(x: float) -> int:
    return int(round(max(-1.0, min(A_FS, float(x))) * 32768.0)) & 0xFFFF


def _words(pairs):
    return [(_q15(i), _q15(q)) for (i, q) in pairs]


def _make_class(kind: str, seed: int, n: int = CLASS_LEN):
    """One in-contract stimulus class as float (i, q) pairs (|x| <= 1/rail)."""
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    if kind == "sine_fs":              # full-scale on-bin complex exponential
        z = A_FS * np.exp(1j * (2 * np.pi * 11 * t / N_FFT
                                + rng.uniform(0, 2 * np.pi)))
    elif kind == "noise_m6":           # gaussian, -6 dBFS per rail, clipped
        z = rng.normal(0, 0.5, n) + 1j * rng.normal(0, 0.5, n)
        z = np.clip(z.real, -A_FS, A_FS) + 1j * np.clip(z.imag, -A_FS, A_FS)
    elif kind == "noise_m26":          # gaussian, -26 dBFS per rail
        z = rng.normal(0, 0.05, n) + 1j * rng.normal(0, 0.05, n)
    elif kind == "two_tone":           # dense spectrum: excites every slot
        p1, p2 = rng.uniform(0, 2 * np.pi, 2)
        z = (0.45 * np.exp(1j * (2 * np.pi * 11 * t / N_FFT + p1))
             + 0.45 * np.exp(1j * (2 * np.pi * 27.31 * t / N_FFT + p2)))
    elif kind == "impulse":            # one full-scale impulse per frame
        z = np.zeros(n, complex)
        z[7::N_FFT] = A_FS * np.exp(1j * rng.uniform(0, 2 * np.pi))
    elif kind == "rails_full":         # BOTH rails full-scale (|z| = sqrt2):
        # a legal per-rail input whose coherent bins OVERFLOW the /64 output
        # range — exercises the saturating combines ON CHIP. Excluded from the
        # SNR gate: saturation is the correct, pinned behaviour here.
        c = A_FS * np.cos(2 * np.pi * 11 * t / N_FFT + rng.uniform(0, 2 * np.pi))
        z = c + 1j * c
    else:
        raise ValueError(kind)
    return [(float(c.real), float(c.imag)) for c in z]


CLASSES = ("sine_fs", "noise_m6", "noise_m26", "two_tone", "impulse",
           "rails_full")
_CLASS_SEED = {c: 200 + i for i, c in enumerate(CLASSES)}


# ---------------------------------------------------- independent goldens
def _tw_tables():
    """Stage twiddle tables, transcribed INDEPENDENTLY of the block: trivial
    slots by INDEX (k = 0 -> identity, 4k = N -> -j), else round-half-even
    words of ``cos(2 pi k/N) - j sin(2 pi k/N)``."""
    tabs = []
    for st in range(N_STAGES):
        D = (N_FFT // 2) >> st
        rows = []
        for j in range(D):
            k = j << st
            if k == 0:
                rows.append(("id", 0, 0))
            elif 4 * k == N_FFT:
                rows.append(("mj", 0, 0))
            else:
                th = 2.0 * np.pi * k / N_FFT
                rows.append(("mul",
                             int(np.round(np.cos(th) * 32768)) & 0xFFFF,
                             int(np.round(-np.sin(th) * 32768)) & 0xFFFF))
        tabs.append(rows)
    return tabs


def _sat_q15(v):
    return u16(32767 if v > 32767 else (-32768 if v < -32768 else v))


def _cmul(xi, xq, kind, c, d):
    if kind == "id":
        return u16(xi), u16(xq)
    if kind == "mj":
        return u16(xq), _sat_q15(-s16(xi))
    return twiddle_cmul_ref(xi, xq, "mul", c, d)


def _rhe_diff_traced(a, b, wrap=False, ctr=None):
    """The diff leg with an observable clamp: counts the (single reachable)
    RHE-tie overflow and either clamps (the pinned behaviour) or WRAPS (the
    ``no_sat`` mutant)."""
    v = s16(a) - s16(b)
    k = v >> 1
    r = k + ((v & k) & 1)
    if r > 32767 or r < -32768:
        if ctr is not None:
            ctr[0] += 1
        return u16(r) if wrap else u16(32767 if r > 0 else -32768)
    return u16(r)


class _StreamStage:
    """Independent streaming R2SDF stage with SINGLE-FAULT hooks (the INV-4
    mutants). ``tw_bad`` corrupts one twiddle word; ``toggle_off`` starts the
    half-period counter one sample late; ``no_scale`` drops the >>1;
    ``swap`` exchanges the sum/diff legs; ``depth_delta`` mis-sizes the delay
    line by +-1; ``no_sat`` WRAPS the diff-leg RHE tie instead of clamping;
    ``octant_sign`` flips the fold's c sign on one octant; ``fold_quadrant``
    walks the fold one octant off. The last two are the FOLD-SPECIFIC
    mutants FFT16 could not have (it has no fold stage)."""

    def __init__(self, D, tw, fault=None, satctr=None):
        f = fault or {}
        self.tw = [list(r) for r in tw]
        if "tw_bad" in f:
            j = f["tw_bad"]
            k, c, d = self.tw[j]
            assert k == "mul", f"slot {j} is trivial — pick a mul slot"
            self.tw[j] = [k, u16(c + 1), d]
        if "octant_sign" in f:
            # Negate c on every slot of ONE octant (the fold's `c sign = o1`
            # decision, corrupted for one quadrant).
            M = N_FFT // 8
            oct_sel = f["octant_sign"]
            for j, (k, c, d) in enumerate(self.tw):
                if k == "mul" and (j // M) % 4 == oct_sel:
                    self.tw[j] = [k, u16(-s16(c)), d]
        if "fold_quadrant" in f:
            # Walk the octant tables one octant late: slot j reads the words
            # of slot j + N/8 (the fold's `o` decision, off by one quadrant).
            M = N_FFT // 8
            src = [list(r) for r in self.tw]
            self.tw = [src[(j + M) % len(src)] for j in range(len(src))]
        self.D = D
        self.depth = D + f.get("depth_delta", 0)
        self.line = [(0, 0)] * self.depth
        self.t = -1 if f.get("toggle_off") else 0
        self.no_scale = f.get("no_scale", False)
        self.swap = f.get("swap", False)
        self.no_sat = f.get("no_sat", False)
        self.satctr = satctr

    def step(self, xi, xq):
        D = self.D
        out_i, out_q = self.line.pop(0)
        ph = self.t % (2 * D)
        if ph < D:
            self.line.append((u16(xi), u16(xq)))
            k, c, d = self.tw[ph % len(self.tw)]
            o = _cmul(out_i, out_q, k, c, d)
        else:
            if self.no_scale:
                s = (_sat_q15(s16(out_i) + s16(xi)),
                     _sat_q15(s16(out_q) + s16(xq)))
                dd = (_sat_q15(s16(out_i) - s16(xi)),
                      _sat_q15(s16(out_q) - s16(xq)))
            else:
                s = (rhe_half_sum(out_i, xi), rhe_half_sum(out_q, xq))
                dd = (_rhe_diff_traced(out_i, xi, self.no_sat, self.satctr),
                      _rhe_diff_traced(out_q, xq, self.no_sat, self.satctr))
            if self.swap:
                s, dd = dd, s
            self.line.append(dd)
            o = s
        self.t += 1
        return o


def _stream_model(words, fault_stage=None, fault=None, satctr=None):
    tabs = _tw_tables()
    stages = [_StreamStage((N_FFT // 2) >> st, tabs[st],
                           fault if st == fault_stage else None,
                           satctr=satctr)
              for st in range(N_STAGES)]
    out = []
    for (xi, xq) in words:
        v = (u16(xi), u16(xq))
        for st in stages:
            v = st.step(*v)
        out.append(v)
    return out


def _direct_dif_q15(fr_i, fr_q):
    """Independent iterative direct DIF integer FFT of ONE 64-sample frame
    (unconditional RHE >>1 per stage, trivial-skip twiddles). Returns the 64
    output pairs in DIF (bit-reversed-bin) order — the streaming frame."""
    tabs = _tw_tables()
    ar = [u16(v) for v in fr_i]
    aq = [u16(v) for v in fr_q]
    for st in range(N_STAGES):
        D = (N_FFT // 2) >> st
        step = 2 * D
        for blk in range(0, N_FFT, step):
            for j in range(D):
                t_i, t_q = ar[blk + j], aq[blk + j]
                b_i, b_q = ar[blk + D + j], aq[blk + D + j]
                s_i = rhe_half_sum(t_i, b_i)
                s_q = rhe_half_sum(t_q, b_q)
                d_i = rhe_half_diff(t_i, b_i)
                d_q = rhe_half_diff(t_q, b_q)
                k, c, d = tabs[st][j]
                o_i, o_q = _cmul(d_i, d_q, k, c, d)
                ar[blk + j], aq[blk + j] = s_i, s_q
                ar[blk + D + j], aq[blk + D + j] = o_i, o_q
    return list(zip(ar, aq))


def _frames_of(stream):
    """Complete output frames of a per-trigger stream (post-latency)."""
    out, k = [], LATENCY
    while k + N_FFT <= len(stream):
        out.append(stream[k:k + N_FFT])
        k += N_FFT
    return out


def _natural_bins(frame):
    """Map one streamed frame (bit-reversed order) to natural-order complex
    bins at the q15/32768 scale."""
    nat = np.zeros(N_FFT, complex)
    for k in range(N_FFT):
        nat[FFT64_OUTPUT_BINS[k]] = complex(s16(frame[k][0]),
                                            s16(frame[k][1])) / 32768.0
    return nat


# ------------------------------------------------------------- DUT running
_RUNS: dict = {}
#: Every build this suite made: bitstream size, resolved input hop, entry and
#: input registers. The port-reachability gate reads it — the CHIP-SCALE
#: class's ONE placement contract is proven by builds that route AND flow,
#: never by inspecting a layout.
_BUILDS: dict = {}


def _dut_stream(key, pairs):
    """Run the DUT once per (cached) key; returns the (i, q) word stream."""
    if key not in _RUNS:
        dut = run_block_dut_complex(
            "FFT64Block", pairs, chip_yaml=CHIP_YAML,
            in_ports=("xi", "xq"), words_per_sample=2,
            place_xy=(0, 0), data_run=60_000, jump_run=2_000_000,
            drain_run=60_000)
        assert dut.ok, dut.reason
        _BUILDS[key] = {"n_words": dut.n_words, "hop": dut.hop_count,
                        "entry": dut.entry_addr, "in_regs": tuple(dut.in_regs)}
        stream = []
        for k in range(len(pairs)):
            gi, gq = dut.i_q15[k], dut.q_q15[k]
            assert gi is not None and gq is not None, (
                f"missing output word at sample {k}")
            stream.append((int(gi) & 0xFFFF, int(gq) & 0xFFFF))
        _RUNS[key] = stream
    return _RUNS[key]


def _exact(dut_stream, ref_stream):
    """THE gate: every output pair present and bit-equal. Returns
    (ok, first_bad_index); shared by the positive tests AND the mutations."""
    if len(dut_stream) != len(ref_stream):
        return False, min(len(dut_stream), len(ref_stream))
    for k, (d, r) in enumerate(zip(dut_stream, ref_stream)):
        if (u16(d[0]), u16(d[1])) != (u16(r[0]), u16(r[1])):
            return False, k
    return True, None


def _class_run(kind):
    pairs = _make_class(kind, _CLASS_SEED[kind])
    return pairs, _dut_stream(("class", kind), pairs)


# =============================================================================
# 1. Golden integrity — the four-way golden + the pinned tables
# =============================================================================
def test_stage_shape_pinned():
    """6 stages, delays 32/16/8/4/2/1, latency 63, and the twiddle KINDS per
    stage asserted BY INDEX — the fold stage (period 32) included."""
    assert stage_delays(N_FFT) == (32, 16, 8, 4, 2, 1)
    assert FFT64Block("probe").latency == LATENCY == 63
    tabs = _tw_tables()
    for st in range(N_STAGES):
        D = (N_FFT // 2) >> st
        assert len(tabs[st]) == D
        for j, (kind, _c, _d) in enumerate(tabs[st]):
            k = j << st
            want = "id" if k == 0 else ("mj" if 4 * k == N_FFT else "mul")
            assert kind == want, (st, j, kind, want)
    # Stage 0 is the FOLD stage: 32 slots, 30 of them non-trivial.
    assert sum(1 for r in tabs[0] if r[0] == "mul") == 30


def test_block_tables_equal_the_independent_transcription():
    """The block's own ``stage_table`` == this file's independent
    transcription, word for word, at every stage — the fold stage included,
    whose words the CELLS reconstruct from two 8-word octant tables."""
    mine = _tw_tables()
    for st in range(N_STAGES):
        theirs = stage_table(N_FFT, st)
        assert len(theirs) == len(mine[st])
        for j, ((k1, c1, d1), (k2, c2, d2)) in enumerate(zip(mine[st],
                                                             theirs)):
            if k1 == "mul":
                assert k2 == "mul", (st, j, k2)
                assert (u16(c1), u16(d1)) == (u16(c2), u16(d2)), (st, j)
            else:
                assert k2 == k1, (st, j, k1, k2)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_golden_pair_streaming_equals_direct(seed):
    """The block's streaming reference == this file's INDEPENDENT streaming
    transcription == this file's INDEPENDENT direct DIF == the block's OWN
    direct-DIF transcription, frame for frame. Four transcriptions, one
    answer."""
    rng = np.random.default_rng(seed)
    n = N_FFT * 4
    words = [(int(a) & 0xFFFF, int(b) & 0xFFFF)
             for a, b in zip(rng.integers(-29000, 29000, n),
                             rng.integers(-29000, 29000, n))]
    blk_stream = sdf_streaming_reference(N_FFT, words)
    assert blk_stream == _stream_model(words), \
        "block golden != independent streaming transcription"
    blk_direct = direct_dif_reference(N_FFT, words)
    for f, frame in enumerate(_frames_of(blk_stream)):
        fr = words[f * N_FFT:(f + 1) * N_FFT]
        direct = _direct_dif_q15([i for i, _ in fr], [q for _, q in fr])
        assert list(frame) == direct, f"frame {f}: streaming != direct DIF"
        assert blk_direct[f * N_FFT:(f + 1) * N_FFT] == direct, \
            f"frame {f}: block direct != independent direct"


def test_output_bins_map_pinned():
    """The bit-reversal index map: a permutation, an involution, and
    spot-pinned (slot k carries bin rev6(k))."""
    assert len(FFT64_OUTPUT_BINS) == N_FFT
    assert FFT64_OUTPUT_BINS[:8] == (0, 32, 16, 48, 8, 40, 24, 56)
    assert FFT64_OUTPUT_BINS[-1] == 63
    assert sorted(FFT64_OUTPUT_BINS) == list(range(N_FFT))
    assert all(FFT64_OUTPUT_BINS[FFT64_OUTPUT_BINS[k]] == k
               for k in range(N_FFT))


# =============================================================================
# 2. DUT bit-exact (tol 0) — classes, seeds, startup transient
# =============================================================================
@pytest.mark.parametrize("kind", CLASSES)
def test_bitexact_class(kind):
    """>= 3 back-to-back frames per class, bit-exact from trigger 0 (the
    63-sample startup transient included — it is part of the contract).

    255 samples per class is not incidental: output 63 is the FIRST valid
    output, and slots 0..31 of a frame are the EVEN bins (stage 0's SUM
    branch) while slots 32..63 are the ODD bins (its TWIDDLED DIFFERENCE
    branch). A run that stops before output 95 never reaches the twiddled
    half at all — which is exactly how a dead trivial-slot exit survived an
    80-sample check. Every class here covers three full frames of both."""
    pairs, dut = _class_run(kind)
    ref = sdf_streaming_reference(N_FFT, _words(pairs))
    ok, bad = _exact(dut, ref)
    assert ok, f"{kind}: first mismatch at output {bad}"


@pytest.mark.parametrize("seed", [11, 22, 33])
def test_bitexact_random_seeds(seed):
    """Three independent random seeds, three full frames each."""
    rng = np.random.default_rng(seed)
    n = LATENCY + N_FFT * 3
    pairs = [(float(a), float(b))
             for a, b in zip(rng.uniform(-0.9, 0.9, n),
                             rng.uniform(-0.9, 0.9, n))]
    dut = _dut_stream(("rand", seed), pairs)
    ok, bad = _exact(dut, sdf_streaming_reference(N_FFT, _words(pairs)))
    assert ok, f"seed {seed}: first mismatch at output {bad}"


def test_startup_transient_pinned():
    """The first LATENCY outputs are the deterministic zero-pipeline startup
    (INV-2: the delay is ASSERTED, never searched)."""
    pairs, dut = _class_run("sine_fs")
    ref = sdf_streaming_reference(N_FFT, _words(pairs))
    assert LATENCY == 63
    assert dut[:LATENCY] == ref[:LATENCY]


def test_saturating_tie_clamped_on_chip():
    """The single reachable butterfly clamp (the RHE diff tie a = +0x7FFF,
    b = -0x8000) is exercised ON CHIP and its clamp behaviour gated: the
    crafted stimulus demonstrably fires the tie in the model, the WRAP mutant
    diverges from the golden on it (the stimulus has teeth), and the DUT
    matches the CLAMPING golden bit-exactly — so the on-chip datapath
    provably clamps, not wraps."""
    frame = [(A_FS, A_FS)] * 32 + [(-1.0, -1.0)] * 32
    pairs = frame * 4
    words = _words(pairs)
    ctr = [0]
    good = _stream_model(words, satctr=ctr)
    assert ctr[0] > 0, "crafted tie stimulus never fired the clamp"
    mut = _stream_model(words, 0, {"no_sat": True})
    assert mut != good, "wrap mutant == golden — the tie has no teeth"
    dut = _dut_stream(("sat_tie",), pairs)
    ok, bad = _exact(dut, good)
    assert ok, f"sat-tie stream mismatch at {bad}"
    ok, _ = _exact(dut, mut)
    assert not ok, "DUT matches the WRAP mutant — dropped saturation shipped!"


# =============================================================================
# 3. Bit-reversed order + scale, pinned ON CHIP
# =============================================================================
def test_bit_reversed_order_and_scale_on_chip():
    """An on-bin full-scale tone at bin 11 must appear, frame after frame, at
    OUTPUT SLOT rev6(11) (and only there), with magnitude ~1.0 — the FFT/64
    scale puts a full-scale coherent bin at full scale."""
    n = LATENCY + N_FFT * FRAMES
    t = np.arange(n)
    z = A_FS * np.exp(1j * 2 * np.pi * 11 * t / N_FFT)
    pairs = [(float(c.real), float(c.imag)) for c in z]
    dut = _dut_stream(("bin11",), pairs)
    slot = FFT64_OUTPUT_BINS.index(11)
    for f, frame in enumerate(_frames_of(dut)):
        mags = [abs(complex(s16(i), s16(q))) / 32768.0 for (i, q) in frame]
        assert mags[slot] > 0.95, f"frame {f}: bin-11 energy not at slot {slot}"
        rest = max(m for k, m in enumerate(mags) if k != slot)
        assert rest < 0.03, f"frame {f}: leakage {rest} — order map wrong?"
        nat = _natural_bins(frame)
        assert int(np.argmax(np.abs(nat))) == 11


def test_odd_bins_are_exercised_and_correct_on_chip():
    """THE regression gate for the dead trivial-slot exit.

    The ODD bins are the outputs that leave stage 0 through its TWIDDLED
    DIFFERENCE branch — the octant fold. When the fold's trivial exit was
    unwired, every odd bin of every frame was wrong (rotated by one twiddle
    step) while every even bin was right, so an even-bins-only check passed.
    Assert BOTH halves explicitly, and assert the odd half is non-trivial
    (so the gate cannot be satisfied by a silent all-zero half)."""
    pairs, dut = _class_run("two_tone")
    ref = sdf_streaming_reference(N_FFT, _words(pairs))
    frames_d, frames_r = _frames_of(dut), _frames_of(ref)
    assert frames_d and len(frames_d) == len(frames_r)
    for f, (fd, fr) in enumerate(zip(frames_d, frames_r)):
        even = [k for k in range(N_FFT) if FFT64_OUTPUT_BINS[k] % 2 == 0]
        odd = [k for k in range(N_FFT) if FFT64_OUTPUT_BINS[k] % 2 == 1]
        assert len(odd) == 32
        assert all(fd[k] == fr[k] for k in even), f"frame {f}: EVEN bins wrong"
        assert all(fd[k] == fr[k] for k in odd), f"frame {f}: ODD bins wrong"
        assert any(fd[k] != (0, 0) for k in odd), (
            f"frame {f}: the odd half is all zero — the gate is vacuous")


def test_trivial_slots_reach_the_chip_as_sentinels():
    """The fold's two TRIVIAL slots (k = 0 -> identity, k = N/4 -> -j) must be
    dispatched STRUCTURALLY, and the cell that decides it is ``swap``: it
    jumps ``sign`` at ``triv`` for a trivial control word and at ``num``
    otherwise. Wiring only ``num`` made ``sign``'s ``triv`` entry dead code,
    which no Python-side fold check could see. Assert BOTH exits exist and
    are wired to DIFFERENT entries of ``sign``."""
    blk = FFT64Block("probe")
    jumps = blk.internal_jumps()
    for s in range(blk.n_stages):
        if not blk.uses_fold(s):
            continue
        p = f"s{s}_"
        tgt = {port: (dst, entry) for (src, port, dst, entry) in jumps
               if src == p + "swap"}
        assert set(tgt) == {"t_num", "t_triv"}, (
            f"stage {s}: swap must have BOTH jump exits, got {sorted(tgt)}")
        assert tgt["t_num"] == (p + "sign", "num")
        assert tgt["t_triv"] == (p + "sign", "triv")
        # and the cell really declares both ports
        prog = blk.build_cell_programs()[p + "swap"]
        assert {"t_num", "t_triv"} <= {o.name for o in prog.outputs}


# =============================================================================
# 4. SNR vs float numpy.fft.fft (measured per class, reported)
# =============================================================================
def _pooled_snr(kind):
    pairs, dut = _class_run(kind)
    words = _words(pairs)
    ps = pe = 0.0
    for f, frame in enumerate(_frames_of(dut)):
        nat = _natural_bins(frame)
        x = np.array([complex(s16(i), s16(q)) / 32768.0
                      for (i, q) in words[LATENCY + f * N_FFT:
                                          LATENCY + (f + 1) * N_FFT]])
        ref = np.fft.fft(x) / float(N_FFT)
        ps += float(np.sum(np.abs(ref) ** 2))
        pe += float(np.sum(np.abs(nat - ref) ** 2))
    return 10.0 * np.log10(ps / pe) if pe else float("inf")


#: Floors are PINNED, never tuned to pass. Any class that lands below its
#: floor is DISCLOSED with its measured value (the FFT16 precedent), not
#: accommodated by loosening the floor.
_SNR_FLOORS = {"sine_fs": 51.0, "noise_m6": 51.0, "two_tone": 51.0,
               "impulse": 51.0, "noise_m26": 51.0}
MEASURED_SNR: dict = {}


@pytest.mark.parametrize("kind", sorted(_SNR_FLOORS))
def test_snr_floor(kind):
    snr = _pooled_snr(kind)
    MEASURED_SNR[kind] = round(snr, 2)
    assert snr >= _SNR_FLOORS[kind], (
        f"{kind}: measured SNR {snr:.2f} dB below the "
        f"{_SNR_FLOORS[kind]} dB floor")


# =============================================================================
# 5. Frame-boundary correctness (state carries exactly)
# =============================================================================
def test_frame_boundary_state_carry():
    """Frame k+1's output is exactly direct-DIF(frame k+1) NO MATTER what
    frame k contained, and the pipeline state genuinely spans the boundary."""
    rng = np.random.default_rng(77)
    frame_b = [(float(a), float(b)) for a, b in
               zip(rng.uniform(-0.8, 0.8, N_FFT),
                   rng.uniform(-0.8, 0.8, N_FFT))]
    frame_a1 = [(A_FS, 0.0)] * N_FFT                    # DC wall
    frame_a2 = [(0.0, 0.0)] * N_FFT                     # silence
    for tag, fa in (("wall", frame_a1), ("silence", frame_a2)):
        pairs = fa + frame_b + [(0.0, 0.0)] * LATENCY   # flush B's frame out
        dut = _dut_stream(("boundary", tag), pairs)
        ok, bad = _exact(dut, sdf_streaming_reference(N_FFT, _words(pairs)))
        assert ok, f"{tag}: stream mismatch at {bad}"
        frames = _frames_of(dut)
        wb = _words(frame_b)
        direct_b = _direct_dif_q15([i for i, _ in wb], [q for _, q in wb])
        assert list(frames[1]) == direct_b, (
            f"{tag}: frame B corrupted by frame A across the boundary")
    assert _RUNS[("boundary", "wall")][:LATENCY + N_FFT] != \
        _RUNS[("boundary", "silence")][:LATENCY + N_FFT]


# =============================================================================
# 6. MANDATORY mutations (INV-4) — single-fault models must FAIL the gate
# =============================================================================
def _mutation_check(fault_stage, fault, tag):
    """The fault model must (a) DIFFER from the golden on the gate stimulus
    (the stimulus has teeth for this fault) and (b) FAIL the exact gate
    against the DUT stream (which the positive tests prove == golden)."""
    pairs, dut = _class_run("two_tone")      # dense spectrum: excites all slots
    words = _words(pairs)
    good = _stream_model(words)
    mut = _stream_model(words, fault_stage, fault)
    assert mut != good, f"{tag}: stimulus does not excite the fault"
    ok, _ = _exact(dut, mut)
    assert not ok, f"{tag}: gate did NOT reject the mutant"


def test_mutation_wrong_twiddle_word_fails():
    _mutation_check(0, {"tw_bad": 5}, "wrong twiddle word (stage 0 slot 5)")


def test_mutation_toggle_off_by_one_fails():
    _mutation_check(1, {"toggle_off": True}, "stage-1 toggle off-by-one")


def test_mutation_dropped_scale_fails():
    _mutation_check(1, {"no_scale": True}, "dropped >>1 in stage 1")


def test_mutation_sum_diff_swap_fails():
    _mutation_check(2, {"swap": True}, "sum/diff swap in stage 2")


@pytest.mark.parametrize("delta", [-1, +1])
def test_mutation_delay_depth_fails(delta):
    _mutation_check(0, {"depth_delta": delta}, f"stage-0 delay depth {delta:+d}")


@pytest.mark.parametrize("oct_sel", [0, 1, 2, 3])
def test_mutation_wrong_octant_sign_fails(oct_sel):
    """FOLD-SPECIFIC (FFT16 cannot have this): the fold's ``c sign = o1``
    decision corrupted for ONE octant."""
    _mutation_check(0, {"octant_sign": oct_sel},
                    f"stage-0 fold octant {oct_sel} c-sign flipped")


def test_mutation_wrong_fold_quadrant_fails():
    """FOLD-SPECIFIC: the fold's octant index ``o`` off by one quadrant."""
    _mutation_check(0, {"fold_quadrant": True}, "stage-0 fold quadrant +1")


def test_mutation_empty_output_fails():
    pairs, _dut = _class_run("two_tone")
    ref = sdf_streaming_reference(N_FFT, _words(pairs))
    ok, _ = _exact([], ref)
    assert not ok, "an EMPTY stream passed the exact gate"


def test_mutation_offset_by_one_sample_fails():
    pairs, dut = _class_run("two_tone")
    ref = sdf_streaming_reference(N_FFT, _words(pairs))
    ok, _ = _exact(dut[1:] + [(0, 0)], ref)
    assert not ok, "a +1-sample-shifted stream passed the exact gate"


def test_mutation_rail_swap_fails():
    pairs, dut = _class_run("two_tone")
    ref = sdf_streaming_reference(N_FFT, _words(pairs))
    ok, _ = _exact([(q, i) for (i, q) in dut], ref)
    assert not ok, "an I/Q-swapped stream passed the exact gate"


# =============================================================================
# 7. The CHIP-SCALE placement contract, on a real built chip
# =============================================================================
def test_port_reachability_end_to_end_on_a_built_chip():
    """The CHIP-SCALE class's ONE placement contract: the block's input and
    output are REACHABLE from the chip's x16 ports. Proven the only way that
    counts — every DUT run in this suite was wired x16_in -> block -> x16_out,
    routed, built, and produced the right words. Assert the builds happened
    and that their resolved landing is a real, in-range hop."""
    _class_run("sine_fs")                     # ensure at least one build
    assert _BUILDS, "no DUT was ever built"
    for key, b in _BUILDS.items():
        assert b["n_words"] > 0, f"{key}: empty bitstream"
        assert 0 <= b["hop"] <= 31, f"{key}: hop {b['hop']} out of range"
        assert len(b["in_regs"]) == 2, f"{key}: not a complex landing"


def test_cell_count_and_geometry_pinned():
    """84 cells on the 10x12, inside the array, off the x16 port cells, and
    PAIRWISE DISTINCT (INV-25 self-overlap) — including after a whole-block
    translation, which is the only movement a chip-scale block admits (it
    fills the die, so there is nowhere to drag it and no legal D4 image)."""
    blk = FFT64Block("probe")
    lay = blk.default_layout()
    assert blk.cell_count == len(lay) == 84
    cells = [(v[0], v[1]) for v in lay.values()]
    assert len(set(cells)) == 84, "the footprint self-overlaps"
    assert all(0 <= x < 10 and 0 <= y < 12 for (x, y) in cells)
    assert not (set(cells) & {(0, 0), (9, 0)}), "a block cell sits on an x16 port"
    # translation preserves distinctness (a shape property, but assert it
    # rather than argue it)
    for dx, dy in ((1, 0), (0, 1), (-1, 0), (0, -1), (2, 3)):
        moved = [(x + dx, y + dy) for (x, y) in cells]
        assert len(set(moved)) == 84, f"self-overlap after translate {dx},{dy}"


def test_orientation_set_is_declared_and_gated():
    """INV-23 for the CHIP-SCALE class: the block DECLARES the orientations it
    ships instead of silently skipping the D4 gate. A 6-stage vertical spine
    is 12 rows tall on a 12-row array, so no rotation has a legal image —
    identity is the whole set, and that is asserted, not assumed."""
    assert FFT64Block.CHIP_SCALE is True
    assert FFT64Block.CHIP_SCALE_ORIENTATIONS == ((),)
    blk = FFT64Block("probe")
    ys = [v[1] for v in blk.default_layout().values()]
    assert max(ys) - min(ys) + 1 == 12, (
        "the spine no longer spans the full height — re-derive whether a "
        "rotation has become legal before leaving the D4 gate waived")


# =============================================================================
# 7b. SATURATION (INV-19) — the whole burst back-to-back, no quiescence
# =============================================================================
def test_saturated_equals_per_sample_bit_exact():
    """The REQUIRED saturated gate, BESPOKE for this block.

    Each of the six stages carries an always-on serialize-LOCK on its
    delay-feedback ring; saturated == per-sample is what proves all six
    release under back-to-back drive. It is bespoke rather than a row in
    ``test_pipeline_saturation.py`` for one reason worth stating: that
    harness's shared stimulus is 16 samples, and at N = 64 the first valid
    output is 63 — a 16-sample saturated run would exercise nothing but the
    zero-fill transient and pass vacuously. This drives a full frame past the
    latency instead, and asserts the saturated stream is bit-exact against
    BOTH the per-sample chip run and the golden."""
    from kyttar_verify.dut_runner import (  # noqa: PLC0415
        run_block_dut_pipelined)

    n = LATENCY + N_FFT                       # 127: one whole frame, post-latency
    rng = np.random.default_rng(4242)
    pairs = [(float(a), float(b))
             for a, b in zip(rng.uniform(-0.7, 0.7, n),
                             rng.uniform(-0.7, 0.7, n))]
    words = _words(pairs)

    seq = _dut_stream(("sat_seq",), pairs)     # per-sample, inject-and-flush
    ref = sdf_streaming_reference(N_FFT, words)
    ok, bad = _exact(seq, ref)
    assert ok, f"per-sample run already wrong at {bad}"

    pipe = run_block_dut_pipelined(
        "FFT64Block", words, chip_yaml=CHIP_YAML,
        in_ports=("xi", "xq"), out_port="out_i", place_xy=(0, 0))
    assert pipe.ok, f"saturated build/run failed (deadlock/livelock?): {pipe.reason}"

    flat_seq = [w for pair in seq for w in pair]
    got = list(pipe.outputs_q15)
    assert len(got) >= len(flat_seq), (
        f"saturated produced {len(got)} words, per-sample produced "
        f"{len(flat_seq)} — the pipeline STALLED (a serialize-LOCK did not "
        "release)")
    assert got[:len(flat_seq)] == flat_seq, (
        "saturated diverges from per-sample at index "
        f"{next(i for i in range(len(flat_seq)) if got[i] != flat_seq[i])}")


# =============================================================================
# 8. Report
# =============================================================================
def test_zz_write_report():
    """Emit the dashboard report LAST (name sorts after every gate)."""
    out = _ROOT / "verification" / "reports" / "FFT64Block.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    blk = FFT64Block("probe")
    out.write_text(json.dumps({
        "kyttar_block": "FFT64Block",
        "passed": True,
        "metric": "exact",
        "n_compared": len(CLASSES) * CLASS_LEN * 2,
        "max_abs_err": 0.0,
        "tolerance": 0.0,
        "nmse_db": None,
        "correlation": None,
        "bit_errors": 0,
        "delay_used": 0,
        "coverage": {
            "edge": True,
            "random": 3,
            "classes": len(CLASSES),
            "frames_per_class": FRAMES,
            "samples_per_class": CLASS_LEN,
            "mutation": True,
            "cells": blk.cell_count,
            "n_fft": N_FFT,
            "stages": N_STAGES,
            "latency": LATENCY,
            "output_order": "bit_reversed",
            "scale": "fft_over_64",
            "chip_scale": True,
            "orientations": 1,
            "snr_db_measured": MEASURED_SNR,
            "snr_floor_db": _SNR_FLOORS,
        },
    }, indent=2) + "\n")
    assert out.exists()
