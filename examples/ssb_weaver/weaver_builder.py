# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless builder for the on-chip SSB Weaver transceiver (TX + RX on ONE chip).

Real audio in -> recovered audio out, composed ENTIRELY of verified 1:1 GNU Radio
catalog blocks (no new blocks). The classic Weaver (third-method) SSB, USB:

  TX:  b   = ComplexMixer(m, freq=-fa)          # real audio m -> complex (I,Q)
       I   = LowPass(b.re),  Q = LowPass(b.im)
       ssb = IQUpconvert(I, Q, freq=fc)         # I*cos(wc) - Q*sin(wc)   (USB)
  RX:  r   = ComplexMixer(ssb, freq=-fc)
       I'  = LowPass(r.re), Q' = LowPass(r.im)
       out = IQUpconvert(I', Q', freq=fa)       # I'*cos(wa) - Q'*sin(wa)
  gain: fixed Weaver 1/4 amplitude -> x4 applied by the host/verifier (the on-chip
        GainBlock's extended-range >1 path mis-scales; corr is gain-invariant and
        SNR is measured after optimal gain-align).

Block chain (10 blocks): the ComplexMixer emits a complex (yi@R0, yq@R1) pair, so
each mixer output is split into its two real rails by ONE ComplexToFloat
(out_re = I, out_im = Q) feeding the two LowPass filters, then both rails are
recombined by IQUpconvert.

CRITICAL on-chip subtlety (the "Weaver + causal FIR" phase skew), proven at the
Q15-reference level (see test_ssb_weaver.py notes):

  * The verified ComplexMixer NCO is POST-increment (phase = exp(j*0) at n=0), while
    the verified IQUpconvert NCO is PRE-increment (phase advanced before the first
    emit). Weaver reconstruction needs the down-mix and up-mix at a given frequency
    to share a phase convention, so each ComplexMixer's initial ``phase`` is advanced
    by ONE NCO step to match the upconvert (+1 step).
  * Each on-chip LowPassFilter is a CAUSAL linear-phase FIR: it delays the baseband
    envelope by its group delay GD = (ntaps-1)/2 samples. The free-running carrier
    NCOs are NOT delayed, so the carrier phase and the (delayed) envelope drift apart
    and Weaver image cancellation degrades (corr collapses ~0.98 -> ~0.10). The fix
    is to advance each ComplexMixer's carrier phase to TRACK the accumulated envelope
    delay: the TX-fa mixer by 2*GD steps (both cascaded filters sit "after" it in the
    recovered-audio sense) and the RX-fc mixer by ~GD steps. The exact integer step
    counts are auto-calibrated against the Q15 reference chain at build time (the
    interpolated Q15 NCO group delay is not exactly (ntaps-1)/2). Both compensations
    ride the ComplexMixer ``phase`` param (the IQUpconvert has no phase param, and a
    linear phase can be freely relocated onto the mixers).

This module returns a fully placed + routed + built chip (a real .kyt-equivalent
project) reusable by a later .grc / .kyt demo step.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

LIB = "lattrex.official"


# --- Weaver frequency plan (LOCKED; on the NCO 16-bit grid) -------------------
@dataclass(frozen=True)
class WeaverPlan:
    fs: float = 32000.0        # sample rate
    fa: float = 1500.0         # Weaver offset = audio band center = (flo+fhi)/2
    fc: float = 6000.0         # carrier
    cutoff: float = 1200.0     # Weaver LPF cutoff = half audio bandwidth
    tw: float = 2500.0         # LPF transition width (sets tap/cell count)
    lpf_gain: float = 1.0
    end_gain: float = 4.0      # Weaver fixed 1/4 amplitude -> x4

    @property
    def wa(self) -> float:
        return 2 * math.pi * self.fa / self.fs

    @property
    def wc(self) -> float:
        return 2 * math.pi * self.fc / self.fs


def _lpf(plan: WeaverPlan):
    from gr_kyttar.placement.blocks.low_pass_filter_block import LowPassFilter
    return LowPassFilter("l", gain=plan.lpf_gain, samp_rate=plan.fs,
                         cutoff_freq=plan.cutoff, transition_width=plan.tw)


def lpf_group_delay(plan: WeaverPlan) -> int:
    """The causal FIR group delay GD = (ntaps-1)/2 in samples."""
    return (len(_lpf(plan).design_taps) - 1) // 2


def lpf_cells(plan: WeaverPlan) -> int:
    return _lpf(plan).cell_count


def _s16(w: int) -> int:
    return w - 0x10000 if w & 0x8000 else w


# --- bit-exact Q15 reference chain (the golden predictor of the chip) ---------
def weaver_reference(plan: WeaverPlan, m, kfa: int, kfc: int) -> np.ndarray:
    """Chain the EXACT Q15 block reference models: real audio ``m`` -> recovered
    audio (float, x end_gain). ``kfa``/``kfc`` = extra NCO steps added to each
    mixer's initial phase (beyond the +1 pre-increment match to the upconvert).

    This composes the SAME arithmetic the built chip runs (each block's
    ``process_reference_q15`` / ``process_reference``), so its correlation vs the
    input is a faithful predictor of the on-chip result — used both to
    auto-calibrate the phase steps and as the GNU-Radio-independent golden model.
    """
    from gr_kyttar.placement.blocks.complex_mixer_block import ComplexMixerBlock
    from gr_kyttar.placement.blocks.iq_upconvert_block import IQUpconvertBlock

    fs, fa, fc = plan.fs, plan.fa, plan.fc
    ph_fa = 2 * math.pi * (-fa) / fs * (1 + kfa)
    ph_fc = 2 * math.pi * (-fc) / fs * (1 + kfc)

    # TX down-mix by -fa (real audio -> complex)
    bpair = ComplexMixerBlock("txmix", sample_rate=fs, frequency=-fa,
                              phase=ph_fa).process_reference_q15(
        [complex(x, 0.0) for x in m])
    I = [p[0] for p in bpair]
    Q = [p[1] for p in bpair]
    Il = _lpf(plan).process_reference_q15(I)
    Ql = _lpf(plan).process_reference_q15(Q)
    tx = np.array([complex(_s16(a & 0xFFFF) / 32768, _s16(b & 0xFFFF) / 32768)
                   for a, b in zip(Il, Ql)])
    ssb = IQUpconvertBlock("txup", sample_rate=fs,
                           frequency=fc).process_reference(tx)

    # RX down-mix by -fc
    ssb_c = [complex(_s16(int(w) & 0xFFFF) / 32768, 0.0) for w in ssb]
    rpair = ComplexMixerBlock("rxmix", sample_rate=fs, frequency=-fc,
                              phase=ph_fc).process_reference_q15(ssb_c)
    Ir = [p[0] for p in rpair]
    Qr = [p[1] for p in rpair]
    Irl = _lpf(plan).process_reference_q15(Ir)
    Qrl = _lpf(plan).process_reference_q15(Qr)
    rx = np.array([complex(_s16(a & 0xFFFF) / 32768, _s16(b & 0xFFFF) / 32768)
                   for a, b in zip(Irl, Qrl)])
    audio = IQUpconvertBlock("rxup", sample_rate=fs,
                             frequency=fa).process_reference(rx)
    return np.array([_s16(int(w) & 0xFFFF) / 32768 for w in audio]) * plan.end_gain


def make_audio(plan: WeaverPlan, n: int = 1024, amp: float = 0.7) -> np.ndarray:
    """Test audio: three tones inside the SSB passband (800/1800/2400 Hz)."""
    t = np.arange(n) / plan.fs
    return (0.5 * np.sin(2 * np.pi * 800 * t)
            + 0.3 * np.sin(2 * np.pi * 1800 * t)
            + 0.2 * np.sin(2 * np.pi * 2400 * t)) * amp


def gnuradio_weaver_reference(plan: WeaverPlan, m, kfa: int, kfc: int):
    """The SAME Weaver flow built in GNU Radio (system Python), for the 1:1 proof.

    ``analog.sig_source_f`` cos/sin LOs, ``blocks.multiply_ff`` mixers,
    ``filter.fir_filter_fff`` + ``firdes.low_pass`` (matching the LowPassFilter
    params), ``blocks.sub_ff`` for the I*cos - Q*sin combine, x4 gain. The two
    DOWN-mix LOs (TX fa, RX fc) carry the SAME causal-FIR group-delay phase
    compensation the chip applies to its mixers (kfa/kfc NCO steps -> radians): a
    correct Weaver with causal filters needs it on EITHER platform. Returns the
    recovered-audio float stream (x end_gain). Requires KYTTAR_GR_PYTHON.
    """
    import math as _math
    import sys
    from pathlib import Path
    _VERIFY = Path(__file__).resolve().parents[2] / "verification"
    if str(_VERIFY) not in sys.path:
        sys.path.insert(0, str(_VERIFY))
    from kyttar_verify import run_gnuradio_ref

    m_q15 = [int(round(max(-1.0, min(0.999, float(v))) * 32768)) & 0xFFFF for v in m]
    wa = 2 * _math.pi * plan.fa / plan.fs
    wc = 2 * _math.pi * plan.fc / plan.fs
    script = """
from gnuradio import gr, analog, blocks, filter
from gnuradio.fft import window
from gnuradio.filter import firdes
tb = gr.top_block()
src = blocks.vector_source_f(input_float, False)
# TX: down-mix by fa (phase-compensated cos/sin LOs), LPF both rails, up-mix by fc.
ca = analog.sig_source_f(fs, analog.GR_COS_WAVE, fa, 1.0, 0, pa)
sa = analog.sig_source_f(fs, analog.GR_SIN_WAVE, fa, 1.0, 0, pa)
mI = blocks.multiply_ff(); mQ = blocks.multiply_ff()
tb.connect(src, (mI, 0)); tb.connect(ca, (mI, 1))
tb.connect(src, (mQ, 0)); tb.connect(sa, (mQ, 1))
taps = firdes.low_pass(1.0, fs, cutoff, tw, window.WIN_HAMMING)
fI = filter.fir_filter_fff(1, taps); fQ = filter.fir_filter_fff(1, taps)
tb.connect(mI, fI); tb.connect(mQ, fQ)
cc = analog.sig_source_f(fs, analog.GR_COS_WAVE, fc, 1.0)
scc = analog.sig_source_f(fs, analog.GR_SIN_WAVE, fc, 1.0)
uI = blocks.multiply_ff(); uQ = blocks.multiply_ff()
tb.connect(fI, (uI, 0)); tb.connect(cc, (uI, 1))
tb.connect(fQ, (uQ, 0)); tb.connect(scc, (uQ, 1))
ssb = blocks.sub_ff()
tb.connect(uI, (ssb, 0)); tb.connect(uQ, (ssb, 1))
# RX: down-mix by fc (phase-compensated), LPF, up-mix by fa.
rI = blocks.multiply_ff(); rQ = blocks.multiply_ff()
cc2 = analog.sig_source_f(fs, analog.GR_COS_WAVE, fc, 1.0, 0, pc)
sc2 = analog.sig_source_f(fs, analog.GR_SIN_WAVE, fc, 1.0, 0, pc)
tb.connect(ssb, (rI, 0)); tb.connect(cc2, (rI, 1))
tb.connect(ssb, (rQ, 0)); tb.connect(sc2, (rQ, 1))
frI = filter.fir_filter_fff(1, taps); frQ = filter.fir_filter_fff(1, taps)
tb.connect(rI, frI); tb.connect(rQ, frQ)
ca2 = analog.sig_source_f(fs, analog.GR_COS_WAVE, fa, 1.0)
sa2 = analog.sig_source_f(fs, analog.GR_SIN_WAVE, fa, 1.0)
oI = blocks.multiply_ff(); oQ = blocks.multiply_ff()
tb.connect(frI, (oI, 0)); tb.connect(ca2, (oI, 1))
tb.connect(frQ, (oQ, 0)); tb.connect(sa2, (oQ, 1))
out = blocks.sub_ff(); g = blocks.multiply_const_ff(gain)
tb.connect(oI, (out, 0)); tb.connect(oQ, (out, 1)); tb.connect(out, g)
snk = blocks.vector_sink_f(); tb.connect(g, snk)
tb.run()
output_float = list(snk.data())
"""
    def _gr(ka, kc):
        r = run_gnuradio_ref(m_q15, script, extra_args={
            "fs": plan.fs, "fa": plan.fa, "fc": plan.fc, "cutoff": plan.cutoff,
            "tw": plan.tw, "gain": plan.end_gain, "pa": wa * ka, "pc": wc * kc})
        return np.array(r.floats)

    # The chip's mixer NCO (post-increment, interpolated Q15 table) and GR's
    # sig_source (pure cosine) reach their best Weaver reconstruction at slightly
    # DIFFERENT integer LO step counts. Anchored at the chip's (kfa, kfc), sweep a
    # tiny window so GR realizes ITS best-recovering (i.e. correct) Weaver — the
    # fair 1:1 reference. Pick the steps that maximize GR-vs-input correlation.
    gd = lpf_group_delay(plan)
    m_arr = np.asarray([_s16(w) / 32768.0 for w in m_q15])
    best = (-2.0, None)
    for ka in range(kfa - 2, kfa + 3):
        for kc in range(kfc - 2, kfc + 3):
            out = _gr(ka, kc)
            c, _d, _s, _g = _best_corr(out, m_arr, gd)
            if c > best[0]:
                best = (c, out)
    return best[1]


def _best_corr(rec: np.ndarray, m: np.ndarray, gd: int):
    """Best (corr, lag, snr) aligning ``rec`` to ``m`` around the 2*GD filter delay.
    SNR is measured after an optimal scalar gain-align (the raw Weaver x4 undershoots
    a hair in Q15), which is standard for a recovered-audio SNR figure."""
    D = 2 * gd
    best = (-2.0, 0)
    for d in range(max(0, D - 14), D + 15):
        a = rec[d:]
        mm = m[: len(a)]
        L = min(len(a), len(mm))
        if L < 300:
            continue
        s = slice(100, L - 60)
        c = float(np.corrcoef(a[s], mm[s])[0, 1])
        if c > best[0]:
            best = (c, d)
    c, d = best
    a = rec[d:]
    mm = m[: len(a)]
    L = min(len(a), len(mm))
    s = slice(100, L - 60)
    g = float(np.dot(a[s], mm[s]) / np.dot(a[s], a[s]))
    err = a[s] * g - mm[s]
    snr = 10 * math.log10(float(np.mean(mm[s] ** 2) / np.mean(err ** 2)))
    return c, d, snr, g


def calibrate_phase_steps(plan: WeaverPlan, m=None):
    """Auto-calibrate the two ComplexMixer phase-compensation step counts (kfa, kfc)
    against the Q15 reference chain. Returns (kfa, kfc, corr, snr).

    Physics anchor: kfa ~ 2*GD (the TX-fa mixer must pre-rotate for BOTH cascaded
    filter delays), kfc ~ GD (the RX-fc mixer for the TX-filter delay). The search
    is a small integer window around those anchors; the Q15-interpolated NCO group
    delay is not exactly (ntaps-1)/2 so we pick the empirical best."""
    gd = lpf_group_delay(plan)
    if m is None:
        n = np.arange(2048)
        t = n / plan.fs
        m = (0.5 * np.sin(2 * np.pi * 800 * t)
             + 0.3 * np.sin(2 * np.pi * 1800 * t)
             + 0.2 * np.sin(2 * np.pi * 2400 * t)) * 0.7
    best = (-2.0, 0.0, 2 * gd, gd)
    for kfa in range(2 * gd - 4, 2 * gd + 5):
        for kfc in range(gd - 6, gd + 4):
            rec = weaver_reference(plan, m, kfa, kfc)
            c, _d, snr, _g = _best_corr(rec, np.asarray(m), gd)
            if c > best[0]:
                best = (c, snr, kfa, kfc)
    corr, snr, kfa, kfc = best
    return kfa, kfc, corr, snr


def cross_align_corr(a: np.ndarray, b: np.ndarray, max_lag: int = 12):
    """Best correlation of ``a`` vs ``b`` over a small mutual lag (they share the
    same filter latency, so the lag is near 0). Returns (corr, lag)."""
    best = (-2.0, 0)
    for d in range(-max_lag, max_lag + 1):
        if d >= 0:
            x = a[d:]
            y = b[: len(x)]
        else:
            y = b[-d:]
            x = a[: len(y)]
        L = min(len(x), len(y))
        if L < 200:
            continue
        s = slice(80, L - 40)
        c = float(np.corrcoef(x[s], y[s])[0, 1])
        if c > best[0]:
            best = (c, d)
    return best


def run_stage_on_chip(chip_yaml: str, plan: WeaverPlan, kfa: int, kfc: int, m):
    """Execute the WHOLE Weaver chain on the ACTUAL chip substrate (simKYT), one
    verified catalog block at a time, via the block-DUT harness. Each block is
    BUILT + ROUTED + RUN on the 10x12 array (real silicon path), and the stage
    outputs are threaded in software. Returns the recovered-audio float stream.

    This is the honest on-chip proof: every arithmetic stage IS computed by the
    placed+routed+built block on the substrate (each block is separately proven
    bit-exact to its ``process_reference`` in :func:`verify_stage_bitexact`)."""
    import math as _math
    import sys
    from pathlib import Path
    _VERIFY = Path(__file__).resolve().parents[2] / "verification"
    if str(_VERIFY) not in sys.path:
        sys.path.insert(0, str(_VERIFY))
    from kyttar_verify import run_block_dut, run_block_dut_complex

    fs = plan.fs
    ph_fa = 2 * _math.pi * (-plan.fa) / fs * (1 + kfa)
    ph_fc = 2 * _math.pi * (-plan.fc) / fs * (1 + kfc)
    lpp = dict(gain=plan.lpf_gain, samp_rate=fs, cutoff_freq=plan.cutoff,
               transition_width=plan.tw)

    def q2f(w):
        return _s16(int(w or 0) & 0xFFFF) / 32768.0

    def cx(block, iq, params, wps):
        d = run_block_dut_complex(block, iq, params=params, chip_yaml=chip_yaml,
                                  words_per_sample=wps)
        assert d.ok, f"{block}: {d.reason}"
        return d

    def real(block, xs, params, in_port="sample"):
        d = run_block_dut(block, [int(w) & 0xFFFF for w in xs], params=params,
                          chip_yaml=chip_yaml, in_port=in_port)
        assert d.ok, f"{block}: {d.reason}"
        return d.outputs_q15

    # TX
    tx = cx("ComplexMixerBlock", [complex(x, 0.0) for x in m],
            {"sample_rate": fs, "frequency": -plan.fa, "phase": ph_fa}, 2)
    Il = real("LowPassFilter", tx.i_q15, lpp)
    Ql = real("LowPassFilter", tx.q_q15, lpp)
    tx_iq = [complex(q2f(a), q2f(b)) for a, b in zip(Il, Ql)]
    up = cx("IQUpconvertBlock", tx_iq, {"sample_rate": fs, "frequency": plan.fc}, 1)
    ssb = [q2f(w) for w in up.i_q15]

    # RX
    rx = cx("ComplexMixerBlock", [complex(x, 0.0) for x in ssb],
            {"sample_rate": fs, "frequency": -plan.fc, "phase": ph_fc}, 2)
    Irl = real("LowPassFilter", rx.i_q15, lpp)
    Qrl = real("LowPassFilter", rx.q_q15, lpp)
    rx_iq = [complex(q2f(a), q2f(b)) for a, b in zip(Irl, Qrl)]
    rup = cx("IQUpconvertBlock", rx_iq, {"sample_rate": fs, "frequency": plan.fa}, 1)
    # Fixed Weaver 1/4 amplitude -> x end_gain, applied in software (the on-chip
    # GainBlock's extended-range >1 path mis-scales; the recovered-audio level is a
    # verifier concern, not a datapath one — corr is gain-invariant, SNR is measured
    # after optimal gain-align).
    return np.array([q2f(w) for w in rup.i_q15]) * plan.end_gain


# --- the built transceiver ----------------------------------------------------
@dataclass
class WeaverChip:
    ok: bool
    reason: str = ""
    ctrl: object = None                 # AppController (the placed+routed project)
    bres: object = None                 # BuildEngine result
    chip_type: object = None
    plan: Optional[WeaverPlan] = None
    kfa: int = 0
    kfc: int = 0
    entry: int = 0                      # x16_in JUMP entry address (TX mixer landing)
    in_regs: tuple = ()                 # (xi, xq) input registers of the TX mixer
    hop: int = 0                        # x16_in target hop count
    block_cells: int = 0
    route_cells: int = 0
    total_cells: int = 0
    grid_cells: int = 0
    routed_nets: list = field(default_factory=list)


def build_weaver_chip(chip_yaml: str, plan: WeaverPlan = WeaverPlan(),
                      *, use_bus: str = "always") -> WeaverChip:
    """Compose, auto-place, auto-route and build the on-chip Weaver transceiver.

    Returns a :class:`WeaverChip` with the built bitstream + the input-port
    injection parameters (entry/in_regs/hop) and cell utilization. ``ok`` is False
    with ``reason`` set if the chain does not fit / route / build.
    """
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

    kfa, kfc, cal_corr, cal_snr = calibrate_phase_steps(plan)
    ph_fa = 2 * math.pi * (-plan.fa) / plan.fs * (1 + kfa)
    ph_fc = 2 * math.pi * (-plan.fc) / plan.fs * (1 + kfc)

    ctrl = AppController(catalog=cat)
    ctrl.new_project("ssb_weaver", ctk)

    def P(t, x, y, **params):
        return ctrl.place_block(t, 0, x, y, library=LIB, params=params)

    # Drop the 11 blocks; auto_place re-arranges them. Positions are hints only.
    tx_mix = P("ComplexMixerBlock", 1, 1, sample_rate=plan.fs,
               frequency=-plan.fa, phase=ph_fa, pipeline_lock=False)  # fixed batch demo
    tx_split = P("ComplexToFloatBlock", 5, 1)   # yi->out_re (I), yq->out_im (Q)
    tx_lpi = P("LowPassFilter", 7, 1, gain=plan.lpf_gain, samp_rate=plan.fs,
               cutoff_freq=plan.cutoff, transition_width=plan.tw)
    tx_lpq = P("LowPassFilter", 7, 3, gain=plan.lpf_gain, samp_rate=plan.fs,
               cutoff_freq=plan.cutoff, transition_width=plan.tw)
    tx_up = P("IQUpconvertBlock", 9, 1, sample_rate=plan.fs, frequency=plan.fc)

    rx_mix = P("ComplexMixerBlock", 1, 6, sample_rate=plan.fs,
               frequency=-plan.fc, phase=ph_fc, pipeline_lock=False)  # fixed batch demo
    rx_split = P("ComplexToFloatBlock", 5, 6)
    rx_lpi = P("LowPassFilter", 7, 6, gain=plan.lpf_gain, samp_rate=plan.fs,
               cutoff_freq=plan.cutoff, transition_width=plan.tw)
    rx_lpq = P("LowPassFilter", 7, 8, gain=plan.lpf_gain, samp_rate=plan.fs,
               cutoff_freq=plan.cutoff, transition_width=plan.tw)
    rx_up = P("IQUpconvertBlock", 9, 6, sample_rate=plan.fs, frequency=plan.fa)

    def C(a, ap, b, bp, name):
        ctrl.add_logical_connection(BlockEndpoint(block=a, port=ap),
                                    BlockEndpoint(block=b, port=bp), name=name)

    # x16_in feeds the TX mixer's real audio: xi = m, xq = 0 (Q rail seeded 0 by
    # the injector — only xi is driven, xq is left at its reset 0).
    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=tx_mix, port="xi"), name="ingress")

    # TX rail: mixer complex (yi,yq) -> split -> two LPFs -> IQ upconvert
    C(tx_mix, "yi", tx_split, "re", "tx_re")
    C(tx_mix, "yq", tx_split, "im", "tx_im")
    C(tx_split, "out_re", tx_lpi, "sample", "tx_lpi")
    C(tx_split, "out_im", tx_lpq, "sample", "tx_lpq")
    C(tx_lpi, "out", tx_up, "xi", "tx_up_i")
    C(tx_lpq, "out", tx_up, "xq", "tx_up_q")

    # TX -> RX (the SSB passband on-chip)
    C(tx_up, "out", rx_mix, "xi", "ssb")

    # RX rail
    C(rx_mix, "yi", rx_split, "re", "rx_re")
    C(rx_mix, "yq", rx_split, "im", "rx_im")
    C(rx_split, "out_re", rx_lpi, "sample", "rx_lpi")
    C(rx_split, "out_im", rx_lpq, "sample", "rx_lpq")
    C(rx_lpi, "out", rx_up, "xi", "rx_up_i")
    C(rx_lpq, "out", rx_up, "xq", "rx_up_q")

    # RX upconvert -> egress (the fixed Weaver x1/4 -> x end_gain is applied by the
    # host/verifier, not an on-chip GainBlock — see run_stage_on_chip).
    ctrl.add_logical_connection(BlockEndpoint(block=rx_up, port="out"),
                                ChipPortEndpoint(chip=0, port="x16_out"),
                                name="egress")

    rep = ctrl.auto_pnr({ctk: ct}, use_bus=use_bus)
    if not rep.ok:
        return WeaverChip(False, reason="route failed: " + "; ".join(
            f"{r.name}:{r.reason}" for r in rep.failed),
            ctrl=ctrl, chip_type=ct, plan=plan, kfa=kfa, kfc=kfc)

    bres = BuildEngine(cat, chip_yaml).build(ctrl.project, {ctk: ct})
    if not bres.ok:
        return WeaverChip(False, reason="build failed: " + "; ".join(
            str(e) for e in bres.errors),
            ctrl=ctrl, chip_type=ct, plan=plan, kfa=kfa, kfc=kfc)

    # --- input-port injection params (INV-1 / INV-6): entry + in regs + hop -----
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

    # --- cell utilization -------------------------------------------------------
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

    return WeaverChip(
        True, ctrl=ctrl, bres=bres, chip_type=ct, plan=plan, kfa=kfa, kfc=kfc,
        entry=int(entry), in_regs=tuple(int(i) for i in ins), hop=hop,
        block_cells=block_cells, route_cells=route, total_cells=total,
        grid_cells=grid, routed_nets=[r.name for r in rep.routed])


def _footprint_summary(plan: WeaverPlan) -> str:
    lc = lpf_cells(plan)
    total = 2 * 11 + 2 * 1 + 4 * lc + 2 * 6  # mixers, C2F splits, LPFs, upconverts
    return (f"block-cell footprint (10 blocks): 2x ComplexMixer(11) + "
            f"2x ComplexToFloat(1) + 4x LowPassFilter({lc}) + 2x IQUpconvert(6) "
            f"= {total} / 120 cells (x4 Weaver gain applied in host)")


if __name__ == "__main__":
    import sys as _sys
    _root = Path(__file__).resolve().parents[2]
    for _p in (str(_root / "placekyt"), str(_root / "runtime" / "python")):
        if _p not in _sys.path:
            _sys.path.insert(0, _p)
    _chip = str(_root / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml")
    _plan = WeaverPlan(tw=2500.0)
    print("SSB Weaver transceiver — headless builder")
    print(f"  plan: fs={_plan.fs} fa={_plan.fa} fc={_plan.fc} "
          f"cutoff={_plan.cutoff} tw={_plan.tw}  (LPF GD={lpf_group_delay(_plan)})")
    kfa, kfc, corr, snr = calibrate_phase_steps(_plan)
    print(f"  phase steps: kfa={kfa} kfc={kfc}  (Q15-ref corr={corr:.4f} "
          f"SNR={snr:.1f}dB)")
    print("  " + _footprint_summary(_plan))
    res = build_weaver_chip(_chip, _plan)
    if res.ok:
        print(f"  SINGLE-CHIP BUILD: OK — utilization "
              f"{res.total_cells}/{res.grid_cells} "
              f"(block {res.block_cells} + route {res.route_cells})")
    else:
        print("  SINGLE-CHIP BUILD: does not fit/route on one 10x12 chip "
              "(auto-P&R limit)")
        print(f"    reason: {res.reason[:200]}")
    _sys.exit(0)
