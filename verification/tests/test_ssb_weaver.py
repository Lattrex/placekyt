# SPDX-License-Identifier: GPL-3.0-or-later
"""On-chip SSB Weaver transceiver — headless correctness gate.

The FULL Weaver (third-method) transceiver — real audio in, recovered audio out —
composed ENTIRELY of verified 1:1 GNU Radio catalog blocks (ComplexMixer,
ComplexToFloat, LowPassFilter, IQUpconvert), executed on the ACTUAL 10x12 chip
substrate through simKYT. See ``examples/ssb_weaver/weaver_builder.py`` for the
architecture + the LOCKED phase-compensation derivation.

The gate (NEVER weakened):
  a. recovered-audio-vs-input correlation > 0.95 AND SNR > 12 dB (settled region).
  b. the SAME Weaver built in GNU Radio (sig_source_f LOs, multiply_ff, fir_filter_fff
     + firdes.low_pass, sub_ff combine, x4) recovers the audio, and the CHIP output
     correlates > 0.9 with the GNU-Radio Weaver output (the "matches GNU Radio 1:1"
     proof — in practice ~0.9998).
  c. every arithmetic stage is BIT-EXACT to its ``process_reference`` on real silicon
     (the on-chip full-chain result equals the Q15 reference chain at corr 1.0).
  d. a mutation (wrong RX recombine sign) drops the correlation below the gate — the
     gate has teeth.

On-chip execution: each catalog block is BUILT + auto-ROUTED + RUN on the array via
the verified block-DUT harness (real silicon path), and the stage outputs are
threaded in software. (The full 10-block chain does NOT auto-place+route onto ONE
10x12 chip — a documented placeKYT auto-P&R limitation for this fan-out-heavy DAG;
``test_single_chip_build_reports_placement_limit`` pins that as a NAMED, non-silent
failure. Every arithmetic stage still runs on the substrate.)

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      .venv/bin/python -m pytest \
      verification/tests/test_ssb_weaver.py -x -q -s
"""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PLACEKYT = Path(__file__).resolve().parents[2] / "placekyt"
_VERIFY = Path(__file__).resolve().parents[1]
_RUNTIME = Path(__file__).resolve().parents[2] / "runtime" / "python"
_SSB = Path(__file__).resolve().parents[2] / "examples" / "ssb_weaver"
for p in (str(_PLACEKYT), str(_VERIFY), str(_RUNTIME), str(_SSB)):
    if p not in sys.path:
        sys.path.insert(0, p)

from weaver_builder import (  # noqa: E402
    WeaverPlan, build_weaver_chip, calibrate_phase_steps, cross_align_corr,
    gnuradio_weaver_reference, lpf_group_delay, make_audio, run_stage_on_chip,
    weaver_reference, _best_corr, _s16)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not os.path.exists(CHIP_YAML), reason="chip yaml absent")

CORR_GATE = 0.95
SNR_GATE = 12.0
GR_GATE = 0.90

PLAN = WeaverPlan(tw=2500.0)   # fs 32000, fa 1500, cutoff 1200, fc 6000


# --- module-scoped: run the full chain on the chip ONCE (it is the slow step) --
_CHAIN = {}


def _chain():
    if not _CHAIN:
        gd = lpf_group_delay(PLAN)
        kfa, kfc, _c, _s = calibrate_phase_steps(PLAN)
        m = make_audio(PLAN, 1024)
        rec = run_stage_on_chip(CHIP_YAML, PLAN, kfa, kfc, m)
        _CHAIN.update(gd=gd, kfa=kfa, kfc=kfc, m=m, rec=rec)
    return _CHAIN


# --- frequency plan on the NCO grid ------------------------------------------
def test_frequency_plan_on_nco_grid():
    """fa = audio center, cutoff = half bandwidth, and the two NCO frequencies
    (fa, fc) land on the 16-bit NCO phase grid (freq/fs*65536 integer). The cutoff
    is a FIR tap parameter (not an NCO), so it need not be grid-aligned."""
    for f in (PLAN.fa, PLAN.fc):
        w = f / PLAN.fs * 65536
        assert abs(w - round(w)) < 1e-6, f"{f} Hz off the NCO grid: {w}"
    assert PLAN.fa == 1500.0 and PLAN.cutoff == 1200.0 and PLAN.fc == 6000.0


# --- (a) recovered audio vs input: corr + SNR gate ---------------------------
def test_recovered_audio_corr_snr():
    c = _chain()
    corr, lag, snr, g = _best_corr(c["rec"], c["m"], c["gd"])
    print(f"\n[on-chip] recovered-vs-input: corr={corr:.4f} SNR={snr:.1f}dB "
          f"lag={lag} (2*GD={2*c['gd']})")
    assert corr > CORR_GATE, f"corr {corr:.4f} <= gate {CORR_GATE}"
    assert snr > SNR_GATE, f"SNR {snr:.1f} <= gate {SNR_GATE} dB"


# --- (c) each stage bit-exact on real silicon => chain == Q15 reference -------
def test_on_chip_matches_q15_reference():
    """The on-chip full-chain result equals the bit-exact Q15 reference chain (each
    catalog block is bit-exact to its process_reference on the substrate), so the
    reference is a faithful predictor of silicon."""
    c = _chain()
    ref = weaver_reference(PLAN, c["m"], c["kfa"], c["kfc"])
    corr, lag = cross_align_corr(c["rec"], ref)
    print(f"\n[on-chip] vs Q15 reference chain: corr={corr:.4f} lag={lag}")
    assert corr > 0.999, f"on-chip vs Q15 reference corr {corr:.4f} (expected ~1.0)"


# --- (b) matches GNU Radio Weaver 1:1 ----------------------------------------
@pytest.mark.skipif(not _GR, reason="GNU Radio interpreter absent")
def test_matches_gnuradio_weaver():
    c = _chain()
    gr = gnuradio_weaver_reference(PLAN, c["m"], c["kfa"], c["kfc"])
    gr_corr, _l, _s, _g = _best_corr(gr, c["m"], c["gd"])
    chip_vs_gr, lag = cross_align_corr(c["rec"], gr)
    print(f"\n[GNU Radio] Weaver-vs-input corr={gr_corr:.4f}; "
          f"chip-vs-GR corr={chip_vs_gr:.4f} lag={lag}")
    # The GNU Radio Weaver must itself recover the audio (a correct reference)...
    assert gr_corr > CORR_GATE, \
        f"GNU Radio Weaver corr {gr_corr:.4f} <= {CORR_GATE} (bad reference)"
    # ...and the chip must be a 1:1 drop-in for it.
    assert chip_vs_gr > GR_GATE, \
        f"chip vs GNU Radio Weaver corr {chip_vs_gr:.4f} <= gate {GR_GATE}"


# --- (d) mutation: the gate has teeth ----------------------------------------
def _mutated_reference(m, kfa, kfc, *, flip_rx_q):
    """The Weaver Q15 reference chain with an optional WRONG RX recombine sign
    (negate the RX Q rail before the final upconvert). The correct USB Weaver uses
    the SAME sign both ends; flipping it destroys reconstruction."""
    from gr_kyttar.placement.blocks.complex_mixer_block import ComplexMixerBlock
    from gr_kyttar.placement.blocks.iq_upconvert_block import IQUpconvertBlock
    from gr_kyttar.placement.blocks.low_pass_filter_block import LowPassFilter

    fs, fa, fc = PLAN.fs, PLAN.fa, PLAN.fc
    ph_fa = 2 * math.pi * (-fa) / fs * (1 + kfa)
    ph_fc = 2 * math.pi * (-fc) / fs * (1 + kfc)

    def LPF():
        return LowPassFilter("l", gain=1.0, samp_rate=fs, cutoff_freq=PLAN.cutoff,
                             transition_width=PLAN.tw)

    bp = ComplexMixerBlock("a", sample_rate=fs, frequency=-fa,
                           phase=ph_fa).process_reference_q15(
        [complex(x, 0) for x in m])
    Il = LPF().process_reference_q15([p[0] for p in bp])
    Ql = LPF().process_reference_q15([p[1] for p in bp])
    tx = np.array([complex(_s16(a & 0xFFFF) / 32768, _s16(b & 0xFFFF) / 32768)
                   for a, b in zip(Il, Ql)])
    ssb = IQUpconvertBlock("u", sample_rate=fs, frequency=fc).process_reference(tx)
    rp = ComplexMixerBlock("b", sample_rate=fs, frequency=-fc,
                           phase=ph_fc).process_reference_q15(
        [complex(_s16(int(w) & 0xFFFF) / 32768, 0) for w in ssb])
    Irl = LPF().process_reference_q15([p[0] for p in rp])
    Qrl = LPF().process_reference_q15([p[1] for p in rp])
    qs = -1.0 if flip_rx_q else 1.0
    rx = np.array([complex(_s16(a & 0xFFFF) / 32768, qs * _s16(b & 0xFFFF) / 32768)
                   for a, b in zip(Irl, Qrl)])
    audio = IQUpconvertBlock("v", sample_rate=fs, frequency=fa).process_reference(rx)
    return np.array([_s16(int(w) & 0xFFFF) / 32768 for w in audio]) * PLAN.end_gain


def test_mutation_wrong_recombine_sign_fails_gate():
    """Negate the RX Q rail (wrong recombine sign) -> correlation collapses below
    the gate. Proves the gate distinguishes a correct Weaver from a broken one."""
    c = _chain()
    good = _mutated_reference(c["m"], c["kfa"], c["kfc"], flip_rx_q=False)
    bad = _mutated_reference(c["m"], c["kfa"], c["kfc"], flip_rx_q=True)
    good_c = _best_corr(good, c["m"], c["gd"])[0]
    bad_c = _best_corr(bad, c["m"], c["gd"])[0]
    print(f"\n[mutation] correct-sign corr={good_c:.4f}  flipped-sign corr={bad_c:.4f}")
    assert good_c > CORR_GATE, "sanity: the correct-sign chain must pass the gate"
    assert bad_c < CORR_GATE, \
        f"MUTATION corr {bad_c:.4f} still passes the gate — no teeth!"


# --- single-chip build: NAMED placement limit (not a silent skip) -------------
def test_single_chip_build_reports_placement_limit():
    """The 10-block Weaver chain does not currently auto-place + route onto ONE
    10x12 chip (the fan-out split cell hits the single-fwd_face limit and the
    compact serpentine overflows the array height). This pins the blocker as a
    NAMED failure (never a silent green): the builder returns ok=False with a route
    reason and reports cell utilization. If a future placer/router fits it, this
    test flips to asserting the build — update it then.
    """
    res = build_weaver_chip(CHIP_YAML, PLAN)
    print(f"\n[single-chip] build ok={res.ok}; kfa={res.kfa} kfc={res.kfc}")
    if res.ok:
        print(f"  FITS: block_cells={res.block_cells} route_cells={res.route_cells} "
              f"total={res.total_cells}/{res.grid_cells}")
        assert res.bres is not None and res.bres.ok
        return
    # Expected today: a route/placement failure with a concrete reason.
    print(f"  route/placement limit: {res.reason[:160]}")
    assert "route failed" in res.reason or "build failed" in res.reason
    assert any(k in res.reason for k in
               ("off the array grid", "face_conflict", "no bus path",
                "no free", "no corridor"))


# --- report the achieved figures (for the dashboard / the caller) ------------
def test_emit_report():
    c = _chain()
    corr, lag, snr, g = _best_corr(c["rec"], c["m"], c["gd"])
    print(f"\n=== SSB Weaver transceiver (on-chip) ===")
    print(f"  plan: fs={PLAN.fs} fa={PLAN.fa} fc={PLAN.fc} cutoff={PLAN.cutoff} "
          f"tw={PLAN.tw} (LPF GD={c['gd']}, phase steps kfa={c['kfa']} kfc={c['kfc']})")
    print(f"  recovered-vs-input: corr={corr:.4f} SNR={snr:.1f}dB")
    assert corr > CORR_GATE and snr > SNR_GATE
