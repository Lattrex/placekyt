#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless 2-CHIP SSB Weaver transceiver — proves the multi-chip methodology.

The full SSB Weaver transceiver does not auto-place on ONE 10x12 chip (100 cells at
83% density; the CP-SAT packer can't tile it). But each HALF fits one chip comfortably,
so we split the transceiver across two chips exactly at the SSB passband signal:

    CHIP 0  (TX / modulator):  audio -> ComplexMixer(-fa) -> 2x LowPass ->
                               IQUpconvert(fc) -> x16_out   (the real SSB passband)
                                      |
                                      |  board wire: chip0.x16_out -> chip1.x16_in
                                      v
    CHIP 1  (RX / demodulator): x16_in -> ComplexMixer(-fc) -> 2x LowPass ->
                               IQUpconvert(fa) -> x16_out   (recovered audio)

Each half is HAND-PLACED on a deterministic layout (mixer top-left fanning its two
rails directly out to two LowPass filters -- no split block, INV-17 -- and the
IQUpconvert in the upper-right so its output cell reaches the x16_out egress port) and then
ROUTE-ONLY auto-routed. This is the "hand-place + hand-route" path the coherent-RX /
duplex demos use: the compact CP-SAT auto-placer scatters the IQUpconvert far from the
egress port (task #396 IQUpconvert reconvergent-fan-in limitation), so we pin the layout
instead of letting auto-P&R relocate blocks. All nets of each half route + build (the mixer fans its 2 rails out to the 2 LPFs directly — no split block; INV-17).

The inter-chip link is NOT an in-fabric route (placeKYT's auto-router does not do
cross-chip nets) -- it is a port-to-port board wire: chip0's OUTPUT port drives chip1's
INPUT port, relayed verbatim by ``MultiChipSimulation`` (the continuous-HOP_CNT
inter-chip model). This is the same mechanism the 2-chip verification testbench uses.

This is the FIRST multi-chip DSP demo -- it proves how Kyttar chips SCALE beyond one die.

Run: <venv>/python examples/ssb_weaver_2chip/build_2chip.py
"""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT / "placekyt"), str(_ROOT / "runtime" / "python")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Reuse the proven single-chip Weaver plan + phase calibration + reference.
sys.path.insert(0, str(_ROOT / "examples" / "ssb_weaver"))
from weaver_builder import (  # noqa: E402
    WeaverPlan, calibrate_phase_steps, weaver_reference, make_audio,
    cross_align_corr)

LIB = "lattrex.official"
CHIP_YAML = str(_ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml")

# Deterministic HAND-PLACEMENT origins (block anchor cell). Proven to route + build on
# kyttar_10x12 for BOTH halves (identical dataflow shape). The mixer is top-left (its
# ``phase`` cell = the x16_in landing cell) and FANS OUT its two rails directly to the
# two LowPass filters spread across the lower rows (no split block — INV-17); the
# IQUpconvert sits upper-right so its output cell reaches the x16_out egress.
_LAYOUT = {
    "mix": (0, 0),
    "lpi": (0, 6),
    "lpq": (3, 6),
    "up": (6, 2),
}


def _s16(w: int) -> int:
    return w - 0x10000 if w & 0x8000 else w


def _place_half(ctrl, plan, mix_freq, mix_phase, up_freq):
    """HAND-PLACE one Weaver half (identical dataflow for TX and RX). ``mix_freq`` is
    the down-mix carrier (-fa for TX, -fc for RX); ``up_freq`` is the up-mix carrier
    (fc for TX, fa for RX). Returns the mixer block (the x16_in landing block)."""
    from model.connection import ChipPortEndpoint, BlockEndpoint

    def P(t, x, y, **params):
        return ctrl.place_block(t, 0, x, y, library=LIB, params=params)

    def C(a, ap, b, bp, name):
        ctrl.add_logical_connection(BlockEndpoint(block=a, port=ap),
                                    BlockEndpoint(block=b, port=bp), name=name)

    mix = P("ComplexMixerBlock", *_LAYOUT["mix"], sample_rate=plan.fs,
            frequency=mix_freq, phase=mix_phase)
    lpi = P("LowPassFilter", *_LAYOUT["lpi"], gain=plan.lpf_gain, samp_rate=plan.fs,
            cutoff_freq=plan.cutoff, transition_width=plan.tw)
    lpq = P("LowPassFilter", *_LAYOUT["lpq"], gain=plan.lpf_gain, samp_rate=plan.fs,
            cutoff_freq=plan.cutoff, transition_width=plan.tw)
    up = P("IQUpconvertBlock", *_LAYOUT["up"], sample_rate=plan.fs, frequency=up_freq)

    # audio/SSB ingress: the chip input port feeds the down-mixer's real rail (xi);
    # xq stays at its reset 0 (only the real audio/passband is driven in).
    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=mix, port="xi"), name="in")
    # FABRIC-NATIVE: the mixer's complex output is already two words multiplexed on
    # the bus (I=yi, Q=yq). The I rail goes STRAIGHT to LowPass_I, the Q rail to
    # LowPass_Q -- NO "complex-to-float" split block (a complex pair is not a thing
    # that needs splitting; a downstream that wants one rail just reads it). This is a
    # complex-output FAN-OUT: the build steers each rail's WRITE+JUMP to its own filter
    # (INV-17). Each LowPass then feeds one rail of the up-mixer.
    C(mix, "yi", lpi, "sample", "lpi")
    C(mix, "yq", lpq, "sample", "lpq")
    C(lpi, "out", up, "xi", "up_i")
    C(lpq, "out", up, "xq", "up_q")
    ctrl.add_logical_connection(BlockEndpoint(block=up, port="out"),
                                ChipPortEndpoint(chip=0, port="x16_out"), name="out")
    return mix


def _build_one(cat, ct, ctk, mix_freq, mix_phase, up_freq):
    """HAND-PLACE + ROUTE-ONLY + build ONE half on its own single-chip project.

    Returns (ctrl, bres, words, entry, in_regs, hop, rep). Raises on any unrouted net
    or build error -- a hand-built demo must route ALL nets (never a silent
    partial)."""
    from ui.controller import AppController
    from engine.build import BuildEngine

    ctrl = AppController(catalog=cat)
    ctrl.new_project("half", ctk)
    mix = _place_half(ctrl, plan_for(ct), mix_freq, mix_phase, up_freq)

    # ROUTE-ONLY: keep the hand-placement (auto_orient=False so orientation cannot
    # relocate a block onto its neighbour); the bus/broker router threads the mixer
    # rail fan-out + IQUpconvert fan-in + the egress corridor.
    rep = ctrl.auto_route_all({ctk: ct}, auto_orient=False, use_bus="always")
    if not rep.ok:
        raise RuntimeError("route failed: " + "; ".join(
            f"{r.name}:{r.reason}" for r in rep.failed))

    bres = BuildEngine(cat, CHIP_YAML).build(ctrl.project, {ctk: ct})
    if not bres.ok:
        raise RuntimeError("build failed: " + "; ".join(str(e) for e in bres.errors))

    # input-port injection params (INV-1/INV-6): entry addr, input regs, hop distance.
    # The down-mixer is a COMPLEX block (xi=re, xq=im) so ins = (a0, a1).
    entry, ins = cat.resolved_io(
        "ComplexMixerBlock",
        {"sample_rate": plan_for(ct).fs, "frequency": mix_freq, "phase": mix_phase},
        library=LIB)
    port = ct.port("x16_in")
    blk = ctrl.project.block(mix)
    landing = (blk.placement.cells[0]
               if blk and blk.placement and blk.placement.cells else None)
    dist = (abs(landing.x - port.cell_x) + abs(landing.y - port.cell_y) + 1
            if landing is not None else 3)
    hop = max(0, 31 - dist)
    words = list(bres.words(0))
    return ctrl, bres, words, int(entry), tuple(int(i) for i in ins), hop, rep


def _to_q15(f: float) -> int:
    return int(round(max(-1.0, min(1.0, float(f))) * 32767)) & 0xFFFF


def _run_chip_complex(words, entry, in_regs, hop, pairs, *,
                      data_run=6000, jump_run=90000, drain_run=4000):
    """Drive ONE built chip on the real simKYT substrate with a stream of complex
    ``(i, q)`` float samples, returning the drained x16_out real-word stream (last
    word per trigger). Mirrors ``run_block_dut_complex``'s whole-chip drive: per
    sample WRITE xi->a0, WRITE xq->a1, JUMP entry, then drain x16_out. This IS the
    faithful per-chip substrate run; the inter-chip wire is the software relay of one
    chip's x16_out into the next chip's (i,0) input (identical to the hardware
    continuous-HOP_CNT view -- memory ``project_interchip_hop_model``)."""
    import simkyt  # noqa: PLC0415

    a0, a1 = int(in_regs[0]), int(in_regs[1])
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical([w & 0xFFFF for w in words])
    chip.set_port_entry_address("x16_in", entry)

    # A DEEP multi-block chain (mixer 11 cells + split + broker + LPF 7 cells +
    # upmix 6 cells) is a PIPELINE: the output for input N drains several triggers
    # LATER. A per-trigger ``got[-1]`` would mis-align (grab a stale sample), so we
    # collect the FLAT output stream across ALL triggers and return it whole — the
    # recovered stream is the input delayed by the fixed pipeline latency (the caller
    # cross-aligns). This is the per-sample analogue of the batch bridge.
    out: list[int] = []
    for (i_f, q_f) in pairs:
        chip.inject_data_physical([_to_q15(i_f)], target_hop_cnt=hop, target_addr=a0)
        chip.run(max_events=data_run)
        chip.inject_data_physical([_to_q15(q_f)], target_hop_cnt=hop, target_addr=a1)
        chip.run(max_events=data_run)
        chip.inject_jump_physical(target_hop_cnt=hop, entry_addr=entry)
        chip.run(max_events=jump_run)
        while chip.output_available("x16_out"):
            w = chip.read_port_i16("x16_out").view("uint16").tolist()
            out.extend(int(x) & 0xFFFF for x in w)
            chip.release_output_ack("x16_out")
            chip.run(max_events=drain_run)
    return out


# The plan is a module-level singleton so the helpers can reach fs/lpf_gain/etc.
_PLAN: WeaverPlan | None = None


def plan_for(_ct) -> WeaverPlan:
    assert _PLAN is not None
    return _PLAN


def build_2chip(plan: WeaverPlan = WeaverPlan(tw=2500.0)):
    """Build both halves, drive them across the inter-chip wire, and correlate the
    recovered audio against the Q15 Weaver reference. Returns a result dict."""
    global _PLAN
    _PLAN = plan
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type

    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    ctk = getattr(ct, "name", None) or "kyttar_10x12"

    kfa, kfc, cal_corr, cal_snr = calibrate_phase_steps(plan)
    ph_fa = 2 * math.pi * (-plan.fa) / plan.fs * (1 + kfa)
    ph_fc = 2 * math.pi * (-plan.fc) / plan.fs * (1 + kfc)
    print(f"[cal] kfa={kfa} kfc={kfc} Q15-ref corr={cal_corr:.4f} SNR={cal_snr:.1f}dB")

    # --- Chip 0: TX half (audio -> SSB passband) ---
    tx_ctrl, tx_bres, tx_words, tx_entry, tx_ins, tx_hop, tx_rep = _build_one(
        cat, ct, ctk, -plan.fa, ph_fa, plan.fc)
    print(f"[chip0/TX] routed {len(tx_rep.routed)} nets, built OK  "
          f"entry={tx_entry} in_regs={tx_ins} hop={tx_hop}")

    # --- Chip 1: RX half (SSB passband -> recovered audio) ---
    rx_ctrl, rx_bres, rx_words, rx_entry, rx_ins, rx_hop, rx_rep = _build_one(
        cat, ct, ctk, -plan.fc, ph_fc, plan.fa)
    print(f"[chip1/RX] routed {len(rx_rep.routed)} nets, built OK  "
          f"entry={rx_entry} in_regs={rx_ins} hop={rx_hop}")

    # --- Drive both bitstreams across the inter-chip wire -----------------------
    # CHIP 0: audio (real) -> down-mix(-fa) -> LPF -> up-mix(fc) -> SSB passband.
    # The audio drives the mixer's real rail xi; xq = 0.
    m = make_audio(plan, n=256)
    ssb_q15 = _run_chip_complex(tx_words, tx_entry, tx_ins, tx_hop,
                                [(float(x), 0.0) for x in m])
    ssb = [_s16(w) / 32768.0 for w in ssb_q15]
    print(f"[chip0/TX] {len(m)} audio samples -> x16_out emitted "
          f"{len(ssb_q15)} SSB-passband samples")

    # The board wire: chip0's x16_out SSB stream becomes chip1's x16_in real input.
    # CHIP 1: SSB (real) -> down-mix(-fc) -> LPF -> up-mix(fa) -> recovered audio.
    rec_q15 = _run_chip_complex(rx_words, rx_entry, rx_ins, rx_hop,
                                [(float(s), 0.0) for s in ssb])
    print(f"[chip1/RX] {len(ssb)} SSB samples -> x16_out emitted "
          f"{len(rec_q15)} recovered-audio samples")

    # --- Correlate recovered audio vs the Q15 Weaver reference ------------------
    rec = np.array([_s16(w) / 32768.0 for w in rec_q15]) * plan.end_gain
    ref = weaver_reference(plan, m, kfa, kfc)
    n = min(len(rec), len(ref))
    corr, lag = (0.0, 0)
    if n >= 16:
        corr, lag = cross_align_corr(rec[:n], ref[:n])
    print(f"[2chip] recovered-vs-reference corr={corr:.4f} (lag={lag}, n={n})")

    return {
        "kfa": kfa, "kfc": kfc, "cal_corr": cal_corr, "cal_snr": cal_snr,
        "tx_words": tx_words, "rx_words": rx_words,
        "tx_ctrl": tx_ctrl, "rx_ctrl": rx_ctrl,
        "recovered": rec, "reference": ref, "corr": corr, "lag": lag,
        "ssb": ssb, "n_in": len(m), "n_ssb": len(ssb_q15), "n_out": len(rec_q15),
    }


if __name__ == "__main__":
    plan = WeaverPlan(tw=2500.0)
    print("2-CHIP SSB Weaver transceiver — headless builder + inter-chip drive")
    print(f"  plan: fs={plan.fs} fa={plan.fa} fc={plan.fc} cutoff={plan.cutoff}")
    try:
        res = build_2chip(plan)
        ok = res["corr"] >= 0.90 and res["n_out"] > 0
        print("\n" + ("PASS" if ok else "FAIL") +
              f": 2-chip SSB Weaver recovers audio at corr={res['corr']:.4f} "
              f"across the inter-chip wire.")
        sys.exit(0 if ok else 1)
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"\nFAILED: {e}")
        sys.exit(1)
