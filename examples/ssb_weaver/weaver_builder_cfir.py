# SPDX-License-Identifier: GPL-3.0-or-later
"""SSB Weaver transceiver on ONE chip — the COMPLEX-FIR datapath (6 blocks).

The complex-packet rework of :mod:`weaver_builder`. The classic Weaver splits each
complex mixer output into two real rails, low-passes each, then recombines — which
on the fabric means a COMPLEX-OUTPUT FAN-OUT (mixer.yi -> LPF_I, mixer.yq -> LPF_Q,
two different downstream blocks) and a reconvergent TWO-SOURCE FAN-IN at the
upconvert (xi from LPF_I, xq from LPF_Q). :class:`ComplexLowPassFilter` (GNU Radio
``fir_filter_ccf`` fed firdes low-pass taps) filters BOTH rails inside ONE block,
so the chain collapses to pure same-source complex packets:

  TX:  b   = ComplexMixer(m, freq=-fa)            # real audio m -> complex packet
       bl  = ComplexLowPass(b)                     # I/Q packet in, I/Q packet out
       ssb = IQUpconvert(bl, freq=fc)              # I*cos(wc) - Q*sin(wc)  (USB)
  RX:  r   = ComplexMixer(ssb, freq=-fc)
       rl  = ComplexLowPass(r)
       out = IQUpconvert(rl, freq=fa)

That drops the 2 ComplexToFloat splits and halves the filter block instances
(2 ComplexLowPass vs 4 LowPass) — 6 blocks total. NO cell ever fans a complex
pair out to two blocks and no two-source fan-in ever forms, so the whole chain is
a straight complex filament: ideal for abutment-first placement and a candidate to
fit a SINGLE 10x12 die.

The frequency plan, phase-compensation calibration and the audio-recovery proof
are inherited from :mod:`weaver_builder` (the Weaver physics is unchanged); only
the block topology and the LPF gain differ. ``lpf_gain=0.9`` keeps the firdes taps
at ``Σ|h|<=1`` so the multi-cell complex FIR fits the 32-word cell budget (the
0.9 baseband scale is folded into ``end_gain`` — correlation is gain-invariant).
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from weaver_builder import (  # noqa: E402  (same-dir import when run as script)
    WeaverPlan, _s16, _best_corr, cross_align_corr, make_audio)

LIB = "lattrex.official"


def _clpf(plan: WeaverPlan):
    from gr_kyttar.placement.blocks.complex_low_pass_filter_block import (
        ComplexLowPassFilter)
    return ComplexLowPassFilter("l", gain=plan.lpf_gain, samp_rate=plan.fs,
                                cutoff_freq=plan.cutoff,
                                transition_width=plan.tw)


def clpf_group_delay(plan: WeaverPlan) -> int:
    return (len(_clpf(plan).design_taps) - 1) // 2


def clpf_cells(plan: WeaverPlan) -> int:
    return _clpf(plan).cell_count


def _q2f(w: int) -> float:
    return _s16(int(w or 0) & 0xFFFF) / 32768.0


# --- bit-exact Q15 reference chain (complex-FIR topology) ---------------------
def weaver_reference_cfir(plan: WeaverPlan, m, kfa: int, kfc: int) -> np.ndarray:
    """Chain the EXACT Q15 block references through the complex-FIR topology:
    real audio ``m`` -> recovered audio (float, x end_gain). ``end_gain`` already
    folds in the 1/lpf_gain compensation (see :func:`plan_end_gain`)."""
    from gr_kyttar.placement.blocks.complex_mixer_block import ComplexMixerBlock
    from gr_kyttar.placement.blocks.iq_upconvert_block import IQUpconvertBlock

    fs, fa, fc = plan.fs, plan.fa, plan.fc
    ph_fa = 2 * math.pi * (-fa) / fs * (1 + kfa)
    ph_fc = 2 * math.pi * (-fc) / fs * (1 + kfc)
    eg = plan_end_gain(plan)

    bpair = ComplexMixerBlock("txmix", sample_rate=fs, frequency=-fa,
                              phase=ph_fa).process_reference_q15(
        [complex(x, 0.0) for x in m])
    biq = np.array([complex(_q2f(a), _q2f(b)) for a, b in bpair])
    txl = _clpf(plan).process_reference(biq)
    ssb = IQUpconvertBlock("txup", sample_rate=fs,
                           frequency=fc).process_reference(txl)

    ssb_c = [complex(_q2f(w), 0.0) for w in ssb]
    rpair = ComplexMixerBlock("rxmix", sample_rate=fs, frequency=-fc,
                              phase=ph_fc).process_reference_q15(ssb_c)
    riq = np.array([complex(_q2f(a), _q2f(b)) for a, b in rpair])
    rxl = _clpf(plan).process_reference(riq)
    audio = IQUpconvertBlock("rxup", sample_rate=fs,
                             frequency=fa).process_reference(rxl)
    return np.array([_q2f(w) for w in audio]) * eg


def plan_end_gain(plan: WeaverPlan) -> float:
    """The recovered-audio gain: the Weaver x4, divided by the LPF passband gain
    (each rail is filtered by a gain=lpf_gain low-pass, twice cascaded is
    lpf_gain**2 on the envelope, but the Weaver combine only sees lpf_gain per
    stage in-band — empirically the single-power compensation 1/lpf_gain aligns
    the level; the exact figure is irrelevant to correlation and is optimally
    gain-aligned before the SNR read)."""
    return plan.end_gain / max(1e-6, plan.lpf_gain)


def calibrate_phase_steps_cfir(plan: WeaverPlan, m=None):
    """Auto-calibrate the two ComplexMixer phase-compensation step counts against
    the complex-FIR Q15 reference chain. Same physics anchors as the real-rail
    builder (kfa ~ 2*GD, kfc ~ GD)."""
    gd = clpf_group_delay(plan)
    if m is None:
        n = np.arange(2048)
        t = n / plan.fs
        m = (0.5 * np.sin(2 * np.pi * 800 * t)
             + 0.3 * np.sin(2 * np.pi * 1800 * t)
             + 0.2 * np.sin(2 * np.pi * 2400 * t)) * 0.7
    best = (-2.0, 0.0, 2 * gd, gd)
    for kfa in range(2 * gd - 4, 2 * gd + 5):
        for kfc in range(gd - 6, gd + 4):
            rec = weaver_reference_cfir(plan, m, kfa, kfc)
            c, _d, snr, _g = _best_corr(rec, np.asarray(m), gd)
            if c > best[0]:
                best = (c, snr, kfa, kfc)
    corr, snr, kfa, kfc = best
    return kfa, kfc, corr, snr


def run_stage_on_chip_cfir(chip_yaml: str, plan: WeaverPlan,
                           kfa: int, kfc: int, m):
    """Execute the WHOLE complex-FIR Weaver chain on the ACTUAL chip (simKYT), one
    verified catalog block at a time via the block-DUT harness. Every arithmetic
    stage IS computed by the placed+routed+built block on the 10x12 array; the
    stage outputs are threaded in software (the honest on-chip proof). Returns the
    recovered-audio float stream."""
    import sys as _sys
    _VERIFY = Path(__file__).resolve().parents[2] / "verification"
    if str(_VERIFY) not in _sys.path:
        _sys.path.insert(0, str(_VERIFY))
    from kyttar_verify import run_block_dut_complex

    fs = plan.fs
    ph_fa = 2 * math.pi * (-plan.fa) / fs * (1 + kfa)
    ph_fc = 2 * math.pi * (-plan.fc) / fs * (1 + kfc)
    clpp = dict(gain=plan.lpf_gain, samp_rate=fs, cutoff_freq=plan.cutoff,
                transition_width=plan.tw)
    eg = plan_end_gain(plan)

    def cx(block, iq, params, wps):
        d = run_block_dut_complex(block, iq, params=params, chip_yaml=chip_yaml,
                                  words_per_sample=wps)
        assert d.ok, f"{block}: {d.reason}"
        return d

    # TX
    tx = cx("ComplexMixerBlock", [complex(x, 0.0) for x in m],
            {"sample_rate": fs, "frequency": -plan.fa, "phase": ph_fa}, 2)
    tx_iq = [complex(_q2f(a), _q2f(b)) for a, b in zip(tx.i_q15, tx.q_q15)]
    txl = cx("ComplexLowPassFilter", tx_iq, clpp, 2)
    txl_iq = [complex(_q2f(a), _q2f(b)) for a, b in zip(txl.i_q15, txl.q_q15)]
    up = cx("IQUpconvertBlock", txl_iq,
            {"sample_rate": fs, "frequency": plan.fc}, 1)
    ssb = [_q2f(w) for w in up.i_q15]

    # RX
    rx = cx("ComplexMixerBlock", [complex(x, 0.0) for x in ssb],
            {"sample_rate": fs, "frequency": -plan.fc, "phase": ph_fc}, 2)
    rx_iq = [complex(_q2f(a), _q2f(b)) for a, b in zip(rx.i_q15, rx.q_q15)]
    rxl = cx("ComplexLowPassFilter", rx_iq, clpp, 2)
    rxl_iq = [complex(_q2f(a), _q2f(b)) for a, b in zip(rxl.i_q15, rxl.q_q15)]
    rup = cx("IQUpconvertBlock", rxl_iq,
             {"sample_rate": fs, "frequency": plan.fa}, 1)
    return np.array([_q2f(w) for w in rup.i_q15]) * eg


# --- the built transceiver ----------------------------------------------------
@dataclass
class WeaverChipCfir:
    ok: bool
    reason: str = ""
    ctrl: object = None
    bres: object = None
    chip_type: object = None
    plan: Optional[WeaverPlan] = None
    kfa: int = 0
    kfc: int = 0
    entry: int = 0
    in_regs: tuple = ()
    hop: int = 0
    block_cells: int = 0
    route_cells: int = 0
    total_cells: int = 0
    grid_cells: int = 0
    routed_nets: list = field(default_factory=list)


def build_weaver_chip_cfir(chip_yaml: str, plan: WeaverPlan = None,
                           *, use_bus: str = "never") -> WeaverChipCfir:
    """Compose, auto-place, auto-route and build the complex-FIR Weaver (6 blocks)
    on one chip. ``plan`` defaults to the LOCKED Weaver plan with lpf_gain=0.9 so
    the multi-cell complex FIR fits the cell budget."""
    if plan is None:
        plan = WeaverPlan(tw=2500.0, lpf_gain=0.9)
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])  # noqa: F841
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from engine.build import BuildEngine
    from ui.controller import AppController
    from model.connection import ChipPortEndpoint, BlockEndpoint

    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(chip_yaml)
    ctk = getattr(ct, "name", None) or "kyttar_10x12"

    kfa, kfc, cal_corr, cal_snr = calibrate_phase_steps_cfir(plan)
    ph_fa = 2 * math.pi * (-plan.fa) / plan.fs * (1 + kfa)
    ph_fc = 2 * math.pi * (-plan.fc) / plan.fs * (1 + kfc)

    ctrl = AppController(catalog=cat)
    ctrl.new_project("ssb_weaver_cfir", ctk)

    def P(t, x, y, **params):
        return ctrl.place_block(t, 0, x, y, library=LIB, params=params)

    lpp = dict(gain=plan.lpf_gain, samp_rate=plan.fs, cutoff_freq=plan.cutoff,
               transition_width=plan.tw)

    tx_mix = P("ComplexMixerBlock", 1, 1, sample_rate=plan.fs,
               frequency=-plan.fa, phase=ph_fa)
    tx_lp = P("ComplexLowPassFilter", 4, 1, **lpp)
    tx_up = P("IQUpconvertBlock", 8, 1, sample_rate=plan.fs, frequency=plan.fc)

    rx_mix = P("ComplexMixerBlock", 1, 6, sample_rate=plan.fs,
               frequency=-plan.fc, phase=ph_fc)
    rx_lp = P("ComplexLowPassFilter", 4, 6, **lpp)
    rx_up = P("IQUpconvertBlock", 8, 6, sample_rate=plan.fs, frequency=plan.fa)

    def C(a, ap, b, bp, name):
        ctrl.add_logical_connection(BlockEndpoint(block=a, port=ap),
                                    BlockEndpoint(block=b, port=bp), name=name)

    # x16_in feeds the TX mixer real audio (xi=m, xq left 0 by the injector).
    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=tx_mix, port="xi"),
                                name="ingress")

    # TX: mixer complex packet -> complex LPF -> IQ upconvert (all packets).
    C(tx_mix, "yi", tx_lp, "xi", "tx_lp_i")
    C(tx_mix, "yq", tx_lp, "xq", "tx_lp_q")
    C(tx_lp, "out_i", tx_up, "xi", "tx_up_i")
    C(tx_lp, "out_q", tx_up, "xq", "tx_up_q")

    # TX -> RX (the SSB passband on-chip)
    C(tx_up, "out", rx_mix, "xi", "ssb")

    # RX
    C(rx_mix, "yi", rx_lp, "xi", "rx_lp_i")
    C(rx_mix, "yq", rx_lp, "xq", "rx_lp_q")
    C(rx_lp, "out_i", rx_up, "xi", "rx_up_i")
    C(rx_lp, "out_q", rx_up, "xq", "rx_up_q")

    ctrl.add_logical_connection(BlockEndpoint(block=rx_up, port="out"),
                                ChipPortEndpoint(chip=0, port="x16_out"),
                                name="egress")

    rep = ctrl.auto_pnr({ctk: ct}, use_bus=use_bus)
    if not rep.ok:
        return WeaverChipCfir(False, reason="route failed: " + "; ".join(
            f"{r.name}:{r.reason}" for r in rep.failed),
            ctrl=ctrl, chip_type=ct, plan=plan, kfa=kfa, kfc=kfc)

    bres = BuildEngine(cat, chip_yaml).build(ctrl.project, {ctk: ct})
    if not bres.ok:
        return WeaverChipCfir(False, reason="build failed: " + "; ".join(
            str(e) for e in bres.errors),
            ctrl=ctrl, chip_type=ct, plan=plan, kfa=kfa, kfc=kfc)

    entry, ins = cat.resolved_io("ComplexMixerBlock",
                                 {"sample_rate": plan.fs, "frequency": -plan.fa,
                                  "phase": ph_fa}, library=LIB)
    port = ct.port("x16_in")
    blk = ctrl.project.block(tx_mix)
    landing = (blk.placement.cells[0]
               if blk and blk.placement and blk.placement.cells else None)
    if landing is not None:
        dist = abs(landing.x - port.cell_x) + abs(landing.y - port.cell_y) + 1
    else:
        dist = 3
    hop = max(0, 31 - dist)

    cells = bres.chips[0].cells
    programmed = [(x, y) for (x, y), info in cells.items()
                  if any(w for w in info["memory"])]
    block_cells = 0
    for b in ctrl.project.blocks:
        if b.placement and b.placement.cells:
            block_cells += len(b.placement.cells)
    total = len(programmed)
    route = max(0, total - block_cells)
    grid = getattr(ct, "width", 10) * getattr(ct, "height", 12)

    return WeaverChipCfir(
        True, ctrl=ctrl, bres=bres, chip_type=ct, plan=plan, kfa=kfa, kfc=kfc,
        entry=int(entry), in_regs=tuple(int(i) for i in ins), hop=hop,
        block_cells=block_cells, route_cells=route, total_cells=total,
        grid_cells=grid, routed_nets=[r.name for r in rep.routed])


def _footprint_summary(plan: WeaverPlan) -> str:
    lc = clpf_cells(plan)
    total = 2 * 11 + 2 * lc + 2 * 6  # 2 mixers, 2 complex LPFs, 2 upconverts
    return (f"block-cell footprint (6 blocks): 2x ComplexMixer(11) + "
            f"2x ComplexLowPass({lc}) + 2x IQUpconvert(6) = {total} / 120 cells")


if __name__ == "__main__":
    import sys as _sys
    _root = Path(__file__).resolve().parents[2]
    for _p in (str(_root / "placekyt"), str(_root / "runtime" / "python"),
               str(Path(__file__).resolve().parent)):
        if _p not in _sys.path:
            _sys.path.insert(0, _p)
    _chip = str(_root / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml")
    _plan = WeaverPlan(tw=2500.0, lpf_gain=0.9)
    print("SSB Weaver transceiver (COMPLEX-FIR datapath) — headless builder")
    print(f"  plan: fs={_plan.fs} fa={_plan.fa} fc={_plan.fc} "
          f"cutoff={_plan.cutoff} tw={_plan.tw} lpf_gain={_plan.lpf_gain} "
          f"(complex-LPF GD={clpf_group_delay(_plan)}, {clpf_cells(_plan)} cells)")
    kfa, kfc, corr, snr = calibrate_phase_steps_cfir(_plan)
    print(f"  phase steps: kfa={kfa} kfc={kfc}  (Q15-ref corr={corr:.4f} "
          f"SNR={snr:.1f}dB)")
    print("  " + _footprint_summary(_plan))
    res = build_weaver_chip_cfir(_chip, _plan)
    if res.ok:
        print(f"  SINGLE-CHIP BUILD: OK — utilization "
              f"{res.total_cells}/{res.grid_cells} "
              f"(block {res.block_cells} + route {res.route_cells}), "
              f"{len(res.routed_nets)} nets routed")
    else:
        print("  SINGLE-CHIP BUILD: does not fit/route on one 10x12 chip")
        print(f"    reason: {res.reason[:300]}")
    _sys.exit(0)
