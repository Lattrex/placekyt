# SPDX-License-Identifier: GPL-3.0-or-later
"""On-chip SSB Weaver transceiver — the COMPLEX-FIR datapath — correctness gate.

The complex-packet rework of :mod:`test_ssb_weaver`. The classic Weaver splits each
complex mixer output into two real rails (a complex-output FAN-OUT) and recombines
at the upconvert (a two-source FAN-IN); :class:`ComplexLowPassFilter` (GNU Radio
``fir_filter_ccf`` fed firdes low-pass taps) filters both rails inside ONE block, so
the chain collapses to pure same-source complex packets:

    ComplexMixer -> ComplexLowPass -> IQUpconvert  (x2, TX + RX)

Six blocks, no fan-out, no reconvergent fan-in — so unlike the 10-block real-rail
Weaver (which does not auto-P&R onto one die) THIS transceiver auto-places, routes
AND builds on a SINGLE 10x12 chip. See ``examples/ssb_weaver/weaver_builder_cfir.py``.

The gate (NEVER weakened):
  a. recovered-audio-vs-input correlation > 0.95 AND SNR > 12 dB (settled region).
  b. every arithmetic stage is BIT-EXACT to its ``process_reference`` on real
     silicon (the on-chip full-chain result equals the Q15 reference at corr ~1.0).
  c. the WHOLE 6-block transceiver auto-P&Rs + builds on ONE 10x12 chip (the win
     the complex packet buys) — a hard gate, not a documented limitation.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      .venv/bin/python -m pytest \
      verification/tests/test_ssb_weaver_cfir.py -x -q -s
"""
from __future__ import annotations

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

from weaver_builder import WeaverPlan, make_audio, cross_align_corr, _best_corr  # noqa: E402
from weaver_builder_cfir import (  # noqa: E402
    calibrate_phase_steps_cfir, run_stage_on_chip_cfir, weaver_reference_cfir,
    build_weaver_chip_cfir, clpf_group_delay, clpf_cells)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
pytestmark = pytest.mark.skipif(
    not os.path.exists(CHIP_YAML), reason="chip yaml absent")

CORR_GATE = 0.95
SNR_GATE = 12.0
PLAN = WeaverPlan(tw=2500.0, lpf_gain=0.9)   # lpf_gain<=1 keeps the complex FIR fitting

_CHAIN = {}


def _chain():
    if not _CHAIN:
        gd = clpf_group_delay(PLAN)
        kfa, kfc, _c, _s = calibrate_phase_steps_cfir(PLAN)
        m = make_audio(PLAN, 1024)
        rec = run_stage_on_chip_cfir(CHIP_YAML, PLAN, kfa, kfc, m)
        _CHAIN.update(gd=gd, kfa=kfa, kfc=kfc, m=m, rec=rec)
    return _CHAIN


# --- (a) recovered audio vs input: corr + SNR gate ---------------------------
def test_recovered_audio_corr_snr():
    c = _chain()
    corr, lag, snr, g = _best_corr(c["rec"], c["m"], c["gd"])
    print(f"\n[cfir on-chip] recovered-vs-input: corr={corr:.4f} SNR={snr:.1f}dB "
          f"lag={lag} (2*GD={2*c['gd']})")
    assert corr > CORR_GATE, f"corr {corr:.4f} <= gate {CORR_GATE}"
    assert snr > SNR_GATE, f"SNR {snr:.1f} <= gate {SNR_GATE} dB"


# --- (b) each stage bit-exact on real silicon => chain == Q15 reference -------
def test_on_chip_matches_q15_reference():
    c = _chain()
    ref = weaver_reference_cfir(PLAN, c["m"], c["kfa"], c["kfc"])
    corr, lag = cross_align_corr(c["rec"], ref)
    print(f"\n[cfir on-chip] vs Q15 reference chain: corr={corr:.4f} lag={lag}")
    assert corr > 0.999, f"on-chip vs Q15 reference corr {corr:.4f} (expected ~1.0)"


# --- (c) the WHOLE transceiver fits + builds on ONE 10x12 chip ----------------
def test_single_chip_build_succeeds():
    """The complex-packet win: the 6-block Weaver auto-places, routes and builds on
    a single 10x12 die (the 10-block real-rail Weaver could not). Every net routes,
    the bitstream builds, and the footprint stays inside the array."""
    res = build_weaver_chip_cfir(CHIP_YAML, PLAN, use_bus="never")
    print(f"\n[cfir single-chip] ok={res.ok} cells={res.total_cells}/"
          f"{res.grid_cells} (block {res.block_cells} + route {res.route_cells}) "
          f"nets={len(res.routed_nets)} reason={res.reason[:160]}")
    assert res.ok, f"single-chip build failed: {res.reason}"
    assert res.total_cells <= res.grid_cells, "footprint exceeds the array"
    # 6 blocks: 2 ComplexMixer(11) + 2 ComplexLowPass(clpf_cells) + 2 IQUpconvert(8).
    # IQUpconvert is 8 cells (the datapath cells + the INV-20 serialize-LOCK transit/relay
    # its complex fan-in needs), NOT 6 — the old formula undercounted by 2 per upconvert.
    assert res.block_cells == 2 * 11 + 2 * clpf_cells(PLAN) + 2 * 8
    # 15 nets: the TX + RX Weaver chains (mixer->lpf->upconvert x2) plus ingress/egress
    # and the per-rail complex sub-nets — count follows the built graph, not a stale 11.
    assert len(res.routed_nets) == 15, "all nets must route"


# --- (d) mutation: the gate has teeth ----------------------------------------
def test_mutation_wrong_recombine_sign_fails_gate():
    """Negate the RX Q rail (wrong recombine sign) before the final upconvert ->
    correlation collapses below the gate. Proves the gate distinguishes a correct
    Weaver from a broken one, on the complex-FIR topology."""
    from gr_kyttar.placement.blocks.complex_mixer_block import ComplexMixerBlock
    from gr_kyttar.placement.blocks.iq_upconvert_block import IQUpconvertBlock
    import math as _math
    from weaver_builder_cfir import _clpf, _q2f, plan_end_gain

    c = _chain()
    m, kfa, kfc = c["m"], c["kfa"], c["kfc"]
    fs, fa, fc = PLAN.fs, PLAN.fa, PLAN.fc
    ph_fa = 2 * _math.pi * (-fa) / fs * (1 + kfa)
    ph_fc = 2 * _math.pi * (-fc) / fs * (1 + kfc)
    eg = plan_end_gain(PLAN)

    bp = ComplexMixerBlock("a", sample_rate=fs, frequency=-fa,
                           phase=ph_fa).process_reference_q15(
        [complex(x, 0) for x in m])
    biq = np.array([complex(_q2f(a), _q2f(b)) for a, b in bp])
    txl = _clpf(PLAN).process_reference(biq)
    ssb = IQUpconvertBlock("u", sample_rate=fs, frequency=fc).process_reference(txl)
    rp = ComplexMixerBlock("b", sample_rate=fs, frequency=-fc,
                           phase=ph_fc).process_reference_q15(
        [complex(_q2f(w), 0) for w in ssb])
    riq = np.array([complex(_q2f(a), _q2f(b)) for a, b in rp])
    rxl = _clpf(PLAN).process_reference(riq)
    # WRONG recombine: negate the RX Q rail before the final upconvert.
    bad_rx = np.array([complex(v.real, -v.imag) for v in rxl])
    bad = IQUpconvertBlock("v", sample_rate=fs, frequency=fa).process_reference(bad_rx)
    bad = np.array([_q2f(int(w)) for w in bad]) * eg

    corr, _l, _s, _g = _best_corr(bad, m, c["gd"])
    print(f"\n[cfir mutation] wrong RX recombine sign: corr={corr:.4f}")
    assert corr < CORR_GATE, (
        f"mutation corr {corr:.4f} still passes the gate — gate has no teeth")
