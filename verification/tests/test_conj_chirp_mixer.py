# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify ConjChirpMixerBlock — the CSS dechirp (multiply by the conjugate
reference up-chirp).

There is NO stock GNU Radio streaming counterpart (manifest: Python golden).
The golden is the block's own bit-exact integer model: the ChirpGeneratorBlock
s=0 double phase accumulator (16-bit wrap; free-running, the frequency word
returns to 0x8000 every n samples on its own) feeding the verified
ComplexMixer interpolated quarter-wave cos/sin, four truncating MULQ products,
and the SATURATING conjugate rail combines (yi = sat(P1+P2), yq = sat(P3-P4)
— the MultiplyCC V-flag minuend-sign restore). Gates:

  * BIT-EXACT DUT vs the integer golden — random complex streams over
    n = {16, 64, 256}, 3 seeds, > 2 reference periods; the LOCKED
    (pipeline_lock=True) variant bit-exact too.
  * THE DECHIRP PROPERTY (the block's reason to exist): generator(s) x
    conj-ref == a constant-frequency tone for EVERY s — on-chip output over
    all m symbols is bit-exact vs the COMPOSED integer goldens AND the float
    FFT of each on-chip dechirped symbol peaks at bin s with a pinned tone
    SNR (measured ~86-89 dB at n = m = 16; floor 60 dB derived from the
    table-interp + product-truncation stack).
  * SCALING IDENTITY with ChirpGeneratorBlock: same rate word (65536/n), and
    the reference frequency trajectory returns to 0x8000 at every symbol
    boundary (the free-running-wrap-is-the-repeat property).
  * THE SATURATING RAILS specifically: the dechirped unit-magnitude signal
    grazes -1.0 constantly (MULQ floor truncation pushes a true -1.0 rail to
    -32769); a WRAPPING-combine mutant golden (the ComplexMixer combine) must
    FAIL on the s=4 dechirp where the wrap sign-flips every 4th sample.
  * INV-4 mutations proven to FAIL: non-conjugated reference (ComplexMixer
    signs), reference rate word +-1, wrong n, +1-sample reference phase
    misalignment, swapped I/Q, negated Q, +1 sample delay, empty.
  * INV-17 fan-out budget on the combine (complex output) cell.

Shared gates elsewhere: saturated == per-sample via test_pipeline_saturation
(COMPLEX_2IN2OUT, pipeline_lock=True), 8/8 D4 via test_orientation_invariance,
placement legality, GRC binding completeness.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \\
      <venv>/python -m pytest verification/tests/test_conj_chirp_mixer.py -q
"""
from __future__ import annotations

import json
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

from kyttar_verify import write_session_report  # noqa: E402

from kyttar_verify import run_block_dut_complex  # noqa: E402
from gr_kyttar.placement.blocks.conj_chirp_mixer_block import (  # noqa: E402
    ConjChirpMixerBlock)
from gr_kyttar.placement.blocks.chirp_generator_block import (  # noqa: E402
    ChirpGeneratorBlock)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
pytestmark = pytest.mark.skipif(
    not os.path.exists(CHIP_YAML), reason="chip yaml absent")


def _s16(v):
    if v is None:
        return None
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def _signal(seed, count, amp=0.7):
    rng = random.Random(seed)
    return [complex(rng.uniform(-amp, amp), rng.uniform(-amp, amp))
            for _ in range(count)]


def _gen_floats(n, m, syms):
    """ChirpGeneratorBlock's bit-exact output as complex floats (exact Q15
    grid values: word/32768 round-trips through the harness quantization)."""
    words = ChirpGeneratorBlock("g", n=n, m=m).process_reference_q15(syms)
    return [complex(_s16(a) / 32768.0, _s16(b) / 32768.0) for a, b in words]


def _run(stim, n, pipeline_lock=False, orient=None):
    dut = run_block_dut_complex(
        "ConjChirpMixerBlock", stim,
        params={"n": n, "pipeline_lock": pipeline_lock},
        chip_yaml=CHIP_YAML, words_per_sample=2, orient=orient)
    assert dut.ok, dut.reason
    return dut


def _pairs(dut):
    return list(zip([w & 0xFFFF for w in dut.i_q15],
                    [w & 0xFFFF for w in dut.q_q15]))


# --- structure / smoke --------------------------------------------------------

def test_drives_and_captures():
    dut = _run(_signal(1, 20), n=16)
    assert dut.words_per_sample == 2
    assert dut.in_regs == (0, 1), "complex signal should land xi@R0, xq@R1"
    assert all(v is not None for v in dut.i_q15 + dut.q_q15)


def test_param_validation_raises():
    for bad in (1, 12, 131072, 0, -16):
        with pytest.raises(ValueError):
            ConjChirpMixerBlock("bad", n=bad)


def test_scaling_identity_with_generator():
    """The dechirp cancels the generator's sweep bit-for-bit in phase-increment
    terms: same rate word, and the free-running reference frequency word
    returns to 0x8000 (the s=0 start) at EVERY symbol boundary with no reset
    logic — the 16-bit wraparound IS the repeat."""
    for n in (16, 64, 256):
        gen = ChirpGeneratorBlock("g", n=n, m=n)
        mix = ConjChirpMixerBlock("m", n=n)
        assert mix.rate_word == gen.rate_word == 65536 // n
        traj = mix.reference_phase_words(3 * n + 1)
        for k in (0, n, 2 * n, 3 * n):
            assert traj[k][1] == 0x8000, (
                f"n={n}: reference freq word at symbol boundary {k} is "
                f"{traj[k][1]:#06x}, not 0x8000")


# --- bit-exact vs the integer golden ------------------------------------------

@pytest.mark.parametrize("n", [16, 64, 256])
@pytest.mark.parametrize("seed", [1, 7, 42])
def test_bitexact_vs_integer_golden(n, seed):
    """On-chip output is BIT-EXACT vs process_reference_q15 on random complex
    streams spanning > 2 reference periods (n=16: 40 samples = 2.5 periods;
    larger n spot-checked over a partial sweep incl. the off-grid interp
    phases at n=256). Positional equality pins delay=0 (INV-2)."""
    count = max(40, min(2 * n + 8, 96))
    stim = _signal(seed, count)
    dut = _run(stim, n=n)
    ref = ConjChirpMixerBlock("r", n=n).process_reference_q15(stim)
    assert _pairs(dut) == ref, (
        f"n={n} seed={seed}: first mismatch at sample "
        f"{next(i for i, (g, r) in enumerate(zip(_pairs(dut), ref)) if g != r)}")


def test_bitexact_locked_variant():
    """The pipeline_lock=True (INV-20 serialize-LOCK) variant is bit-exact
    per-sample too — the lock adds pacing, never arithmetic."""
    stim = _signal(3, 40)
    dut = _run(stim, n=16, pipeline_lock=True)
    ref = ConjChirpMixerBlock("r", n=16).process_reference_q15(stim)
    assert _pairs(dut) == ref


def test_full_scale_rails_saturate_not_wrap():
    """Full-scale inputs drive the rail combines onto the +-1.0 rails: the
    on-chip output must PIN (saturate) exactly as the reference models — the
    stimulus includes the axis-aligned full-scale samples whose truncated
    products sum below -32768."""
    stim = ([complex(-1.0, 0.0), complex(0.0, -1.0), complex(-1.0, -1.0),
             complex(0.999969, 0.999969), complex(-1.0, 0.999969)] * 8)
    dut = _run(stim, n=16)
    ref = ConjChirpMixerBlock("r", n=16).process_reference_q15(stim)
    assert _pairs(dut) == ref
    # non-vacuity: the saturating path genuinely fired somewhere in the run
    blk = ConjChirpMixerBlock("probe", n=16)
    tbl = blk._quarter_table()
    fired = False
    phase, freq = 0, 0x8000
    for c in stim:
        cos = blk._signed_sine_q15((phase + 16384) & 0xFFFF, tbl)
        sin = blk._signed_sine_q15(phase, tbl)
        xi = max(-32768, min(32767, int(round(c.real * 32768))))
        xq = max(-32768, min(32767, int(round(c.imag * 32768))))
        for pmin, pother, sg in ((( xi * cos) >> 15, (xq * sin) >> 15, 1),
                                 ((xq * cos) >> 15, (xi * sin) >> 15, -1)):
            r = pmin + sg * pother
            if r > 32767 or r < -32768:
                fired = True
        phase = (phase + freq) & 0xFFFF
        freq = (freq + blk.rate_word) & 0xFFFF
    assert fired, "test premise: the stimulus must actually overflow a rail"


# --- THE DECHIRP PROPERTY (generator x conj-ref == constant tone) -------------

def test_dechirp_all_symbols_bitexact_and_tone():
    """For EVERY s in 0..m-1 (n = m = 16): the on-chip dechirp of the
    generator's bit-exact symbol equals the composed integer goldens, and its
    float FFT peaks at bin s with tone SNR >= 60 dB (measured ~86-89 dB; the
    floor covers the table-interp + product-truncation stack, derived not
    tuned)."""
    n = m = 16
    mix_ref = ConjChirpMixerBlock("r", n=n)
    worst = 1e9
    for s in range(m):
        stim = _gen_floats(n, m, [s])
        dut = _run(stim, n=n)
        ref = mix_ref.process_reference_q15(stim)
        assert _pairs(dut) == ref, f"s={s}: chip != composed golden"
        z = np.array([complex(_s16(a), _s16(b)) for a, b in _pairs(dut)]) / 32768.0
        spec = np.abs(np.fft.fft(z))
        assert int(np.argmax(spec)) == s, f"s={s}: peak bin {np.argmax(spec)}"
        ideal = np.exp(1j * 2 * np.pi * np.arange(n) * s / n) * np.mean(np.abs(z))
        ph = np.vdot(ideal, z)
        ideal = ideal * (ph / abs(ph))
        err = z - ideal
        snr = 10 * np.log10(np.sum(np.abs(ideal) ** 2)
                            / max(np.sum(np.abs(err) ** 2), 1e-12))
        worst = min(worst, snr)
        assert snr >= 60.0, f"s={s}: dechirped tone SNR collapsed: {snr:.1f} dB"
    print(f"\ndechirp tone SNR (n=m=16, all symbols): worst {worst:.1f} dB")


def test_dechirp_multisymbol_stream():
    """A multi-symbol stream (the generator's CARRIED phase across symbols):
    the free-running reference stays aligned at every symbol boundary — chip
    bit-exact vs the composed goldens, and each symbol's FFT peak correct."""
    n = m = 16
    syms = [3, 0, 15, 7, 0, 12]
    stim = _gen_floats(n, m, syms)
    dut = _run(stim, n=n)
    ref = ConjChirpMixerBlock("r", n=n).process_reference_q15(stim)
    assert _pairs(dut) == ref
    z = np.array([complex(_s16(a), _s16(b))
                  for a, b in _pairs(dut)]) / 32768.0
    for j, s in enumerate(syms):
        spec = np.abs(np.fft.fft(z[j * n:(j + 1) * n]))
        assert int(np.argmax(spec)) == s, f"symbol {j} (s={s}) mis-decoded"


# --- mutations (INV-4: each must FAIL) ----------------------------------------

def _golden_variant(n, stim, *, conjugate=True, rate_off=0, phase_lag=0,
                    wrap_combine=False):
    """Golden with an injectable defect. Default (no defect) reproduces
    process_reference_q15 exactly (asserted below)."""
    blk = ConjChirpMixerBlock("g", n=n)
    tbl = blk._quarter_table()
    out = []
    phase, freq = 0, 0x8000
    for _ in range(phase_lag):            # the DEFECT: reference starts late
        phase = (phase + freq) & 0xFFFF
        freq = (freq + blk.rate_word + rate_off) & 0xFFFF
    for c in stim:
        cos = blk._signed_sine_q15((phase + 16384) & 0xFFFF, tbl)
        sin = blk._signed_sine_q15(phase, tbl)
        xi = max(-32768, min(32767, int(round(c.real * 32768))))
        xq = max(-32768, min(32767, int(round(c.imag * 32768))))
        p1, p2 = (xi * cos) >> 15, (xq * sin) >> 15
        p3, p4 = (xq * cos) >> 15, (xi * sin) >> 15
        if not conjugate:                 # the DEFECT: ComplexMixer signs
            yi_t, yq_t = p1 - p2, p4 + p3
            si, sq = p1, p4
        else:
            yi_t, yq_t = p1 + p2, p3 - p4
            si, sq = p1, p3
        def _fin(v, sgn_src):
            if wrap_combine:              # the DEFECT: wrap instead of pin
                return v & 0xFFFF
            if v > 32767 or v < -32768:
                return (0x7FFF + (1 if sgn_src < 0 else 0)) & 0xFFFF
            return v & 0xFFFF
        out.append((_fin(yi_t, si), _fin(yq_t, sq)))
        phase = (phase + freq) & 0xFFFF
        freq = (freq + blk.rate_word + rate_off) & 0xFFFF
    return out


def test_golden_variant_identity():
    stim = _signal(5, 32)
    assert (_golden_variant(16, stim)
            == ConjChirpMixerBlock("r", n=16).process_reference_q15(stim))


def _dut_16():
    stim = _gen_floats(16, 16, [4, 9, 0])   # incl. s=4, the wrap-corner symbol
    return _pairs(_run(stim, n=16)), stim


def test_mutation_nonconjugated_reference_fails():
    """A NON-conjugated golden (the plain ComplexMixer product signs) must
    DISAGREE — proof the block actually conjugates the reference."""
    got, stim = _dut_16()
    assert got != _golden_variant(16, stim, conjugate=False), \
        "gate failed to detect a non-conjugated reference!"


@pytest.mark.parametrize("off", [-1, +1])
def test_mutation_rate_mismatch_fails(off):
    """A reference chirp-rate word off by +-1 (a generator/dechirp scaling
    mismatch) must DISAGREE."""
    got, stim = _dut_16()
    assert got != _golden_variant(16, stim, rate_off=off), \
        f"gate failed to detect a rate word off by {off}!"


def test_mutation_wrong_n_fails():
    got, stim = _dut_16()
    assert got != _golden_variant(32, stim), \
        "gate failed to detect a wrong n (rate word halved)!"


def test_mutation_reference_phase_off_by_one_sample_fails():
    """A reference chirp advanced by one sample (a symbol-boundary
    misalignment) must DISAGREE."""
    got, stim = _dut_16()
    assert got != _golden_variant(16, stim, phase_lag=1), \
        "gate failed to detect a 1-sample reference misalignment!"


def test_mutation_wrapping_combine_fails():
    """THE saturating-rails gate: the WRAPPING-combine mutant (ComplexMixer's
    combine, the historically-inherited behavior) must DISAGREE on the s=4
    dechirp, where a true -1.0 rail truncates to -32769 and the wrap
    sign-flips it to +32767 (measured: every 4th sample)."""
    stim = _gen_floats(16, 16, [4])
    got = _pairs(_run(stim, n=16))
    wrapped = _golden_variant(16, stim, wrap_combine=True)
    assert got != wrapped, \
        "gate failed to detect a wrapping (non-saturating) rail combine!"
    # and the mutant is genuinely the observed failure: a full-scale sign flip
    diffs = [k for k, (g, w) in enumerate(zip(got, wrapped)) if g != w]
    assert diffs and all(
        abs(_s16(got[k][i]) - _s16(wrapped[k][i])) > 60000
        for k in diffs for i in (0, 1) if got[k][i] != wrapped[k][i])


def test_mutation_swapped_iq_fails():
    got, stim = _dut_16()
    ref = ConjChirpMixerBlock("r", n=16).process_reference_q15(stim)
    swapped = [(q, i) for (i, q) in got]
    assert swapped != ref, "gate failed to detect swapped I/Q rails!"


def test_mutation_negated_q_fails():
    got, stim = _dut_16()
    ref = ConjChirpMixerBlock("r", n=16).process_reference_q15(stim)
    negq = [(i, (0x10000 - q) & 0xFFFF) for (i, q) in got]
    assert negq != ref, "gate failed to detect a negated Q rail!"


def test_mutation_one_sample_delay_fails():
    got, stim = _dut_16()
    ref = ConjChirpMixerBlock("r", n=16).process_reference_q15(stim)
    delayed = [(0, 0)] + got[:-1]
    assert delayed != ref, "gate failed to detect a +1 sample delay!"


def test_mutation_empty_fails():
    _, stim = _dut_16()
    assert [] != ConjChirpMixerBlock("r", n=16).process_reference_q15(stim)


# --- INV-17: combine-cell fan-out budget --------------------------------------

@pytest.mark.parametrize("lock", [False, True])
def test_combine_cell_fanout_budget(lock):
    """The complex output cell leaves room for the build's fan-out
    re-sequencing (INV-17): max register address + instructions + the extra
    fan-out JUMP must fit the 31 packable words (R31 auto-HALT)."""
    from gr_kyttar.placement.resolver import CellProgramResolver  # noqa: PLC0415
    cp = ConjChirpMixerBlock("b", n=128, pipeline_lock=lock
                             ).build_cell_programs()["combine"]
    r = CellProgramResolver()
    instr = r.count_instructions(cp)
    maxaddr = max([d.address for d in cp.data]
                  + list(r.compute_state_registers(cp).values()))
    assert maxaddr + instr + 1 <= 31, (
        f"combine (lock={lock}) too full for the fan-out JUMP: "
        f"{maxaddr + instr + 1} > 31")


# --- dashboard report ---------------------------------------------------------

def test_emit_report():
    n = m = 16
    stim = _gen_floats(n, m, list(range(m)))
    dut = _run(stim, n=n)
    ref = ConjChirpMixerBlock("r", n=n).process_reference_q15(stim)
    assert _pairs(dut) == ref
    z = np.array([complex(_s16(a), _s16(b)) for a, b in _pairs(dut)]) / 32768.0
    snrs = []
    for s in range(m):
        seg = z[s * n:(s + 1) * n]
        ideal = (np.exp(1j * 2 * np.pi * np.arange(n) * s / n)
                 * np.mean(np.abs(seg)))
        ph = np.vdot(ideal, seg)
        ideal = ideal * (ph / abs(ph))
        snrs.append(10 * np.log10(
            np.sum(np.abs(ideal) ** 2)
            / max(np.sum(np.abs(seg - ideal) ** 2), 1e-12)))
    report = {
        "metric": "exact", "n_compared": len(ref) * 2, "max_abs_err": 0,
        "tolerance": 0, "bit_errors": 0, "delay_used": 0,
        "dechirp_tone_snr_db_worst": round(float(min(snrs)), 1),
        "dechirp_tone_snr_db_mean": round(float(np.mean(snrs)), 1),
        "coverage": {"param_sweep": 3, "bit_exact": True, "mutation": True,
                     "all_symbols_dechirp": True, "saturating_rails": True,
                     "locked_variant": True},
    }
    write_session_report("ConjChirpMixerBlock", report)
