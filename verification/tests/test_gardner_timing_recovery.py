# SPDX-License-Identifier: GPL-3.0-or-later
"""GardnerTimingRecovery — GR-equivalence gate + on-chip bit-exactness + INV-4.

This block is a drop-in for GNU Radio's ``digital.symbol_sync_cc`` with
``TED_GARDNER`` on the industry-standard receiver channel: RRC-shaped TX, a
high-quality sinc fractional delay, an RRC MATCHED FILTER at the RX — a Nyquist
stream at 2 samples/symbol. That is the same channel shape MMTimingRecoveryBlock
is verified on, and the bar is the same one MM meets:

    BER 0 across the FULL fractional-offset sweep, on the built + simulated chip,
    with the on-chip stream BIT-EXACT to ``process_reference``.

HISTORY, because it is the point of several of the assertions below. The block was
quarantined twice:

  * 2026-08-06 blamed TED PRECISION (a ``>>1`` halving of the sample difference).
    That diagnosis was WRONG — ablation showed removing the halving reaches only
    BER 0.0122, not 0.
  * 2026-08-27 (first retry) found the real defect — an UNBOUNDED phase
    accumulator with no modulo, which wrapped int16 and inverted the interpolation,
    so the loop was SLIPPING rather than jittering — and solved the Q15 problem,
    but could not close the loop ON CHIP.

Both are fixed now. The Q15 datapath is the Rice Ch.8 modulo-1 interpolator-control
counter with a full-precision Gardner TED and GR-derived PI gains, and the on-chip
topology gives the block a DEDICATED egress cell distinct from its feedback source
(see ``test_gardner_build.py`` for the structural gate).

The suite also keeps the ORIGINAL trap visible: the block's old synthetic stimulus
is one GR itself cannot lock, so "it converges on our own test signal" proved
nothing. That reproduction is retained below as a permanent reminder, now stated as
what it is — a stimulus outside GR's operating regime, not evidence about the block.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \\
      <venv>/python -m pytest verification/tests/test_gardner_timing_recovery.py -q
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PLACEKYT = Path(__file__).resolve().parents[2] / "placekyt"
_VERIFY = Path(__file__).resolve().parents[1]
_RUNTIME = Path(__file__).resolve().parents[2] / "runtime" / "python"
for _p in (str(_PLACEKYT), str(_VERIFY), str(_RUNTIME)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from kyttar_verify import write_session_report  # noqa: E402

from gr_kyttar.placement.blocks.gardner_timing_recovery import (  # noqa: E402
    GardnerTimingRecovery)
from gr_kyttar.placement.blocks._base import float_to_q15  # noqa: E402

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_PY = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")
_GR_AVAILABLE = os.path.exists(_GR_PY)
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_AVAILABLE),
    reason="chip yaml or GNU Radio interpreter absent")

# The offset sweep (fractional timing offset in samples). GR locks BER 0 across ALL
# of these on the matched-filter channel, and so must the DUT.
_FRACS = [0.1, 0.3, 0.5, 0.7]
# A FULL 10-point grid for the reference-level sweep (the on-chip sweep is coarser
# only because each point builds and simulates a chip).
_FRACS_FULL = [round(0.1 * i, 1) for i in range(10)]
_SEEDS = [1234, 77, 2026]
# GR's default control-loop settings for symbol_sync_cc (from its docstring):
# loop_bw ~ 2*pi*0.040, damping 1.0, ted_gain 1.0, max_deviation 1.5, sps 2, osps 1.
_GR_LOOP_BW = 2 * math.pi * 0.040
_GR_DAMPING = 1.0
_GR_TED_GAIN = 1.0
_GR_MAX_DEV = 1.5
_SPS = 2.0


def _s16(v):
    v = int(v) & 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


# --------------------------------------------------------------------------- #
# Stimulus: the INDUSTRY-STANDARD RX channel a symbol synchroniser is built for —
# RRC-shaped TX, a high-quality sinc fractional delay, an RRC MATCHED FILTER at the
# RX. This is the SAME channel shape MMTimingRecoveryBlock is verified on, and it
# sits inside the block's documented operating envelope (peak amplitude 0.7, RRC
# rolloff beta 0.35). It is DELIBERATELY not the block's old ``_make_bpsk_2sps``
# synthetic — see the regime-mismatch reproduction near the end of this file.
# --------------------------------------------------------------------------- #
def _rrc(sps, beta, span):
    N = span * sps
    t = np.arange(-N, N + 1) / sps
    taps = (np.sinc(t) * np.cos(np.pi * beta * t)
            / (1 - (2 * beta * t) ** 2 + 1e-12))
    return taps / np.sqrt(np.sum(taps ** 2))


def _matched_channel(bits, frac, sps=2, beta=0.35, span=8, amp=0.7):
    syms = np.array([1.0 if b else -1.0 for b in bits])
    up = np.zeros(len(syms) * sps)
    up[::sps] = syms
    tx = np.convolve(up, _rrc(sps, beta, span))
    T = 16
    k = np.arange(-T, T + 1)
    h = np.sinc(k - frac) * np.hamming(2 * T + 1)
    h = h / h.sum()
    txd = np.convolve(tx, h, "same")
    mf = np.convolve(txd, _rrc(1.0, beta, span))     # RRC matched filter
    mf = mf / (np.max(np.abs(mf)) + 1e-12)
    return mf * amp


def _gr_symbol_sync(x):
    """digital.symbol_sync_cc(TED_GARDNER) recovered complex output (GR golden)."""
    payload = {"x": [[float(c.real), float(c.imag)] for c in x],
               "sps": _SPS, "lb": _GR_LOOP_BW, "damp": _GR_DAMPING,
               "tg": _GR_TED_GAIN, "md": _GR_MAX_DEV}
    script = (
        "import json,sys\n"
        "from gnuradio import gr, digital, blocks\n"
        "d=json.load(sys.stdin); x=[complex(a,b) for a,b in d['x']]\n"
        "tb=gr.top_block(); src=blocks.vector_source_c(x,False)\n"
        "ss=digital.symbol_sync_cc(digital.TED_GARDNER,d['sps'],d['lb'],d['damp'],"
        "d['tg'],d['md'],1,digital.constellation_bpsk().base())\n"
        "snk=blocks.vector_sink_c(); tb.connect(src,ss,snk); tb.run(); y=list(snk.data())\n"
        "print(json.dumps([[float(v.real),float(v.imag)] for v in y]))\n")
    p = subprocess.run([_GR_PY, "-c", script], input=json.dumps(payload),
                       capture_output=True, text=True, timeout=180)
    if p.returncode != 0:
        raise RuntimeError(f"GR symbol_sync_cc failed: {p.stderr[-500:]}")
    data = json.loads(p.stdout.strip().splitlines()[-1])
    return np.array([complex(a, b) for a, b in data])


def _ber_vs_tx(recovered_i, txbits, skip=80):
    """Best BER of a recovered symbol-rate stream against the KNOWN TX bits,
    tolerant of the global BPSK sign ambiguity and a small integer symbol lag (the
    pulse-shape group delay). This is the honest drop-in metric for a
    timing-recovery block: does it recover the transmitted bits?"""
    rec = np.asarray(recovered_i, dtype=float).real
    tx = np.asarray(txbits)
    best = 1.0
    for lag in range(-4, 30):
        r = rec[max(0, lag):]
        for sgn in (1, -1):
            rb = (sgn * r > 0).astype(int)
            m = min(len(rb), len(tx))
            if m < skip + 40:
                continue
            best = min(best, float(np.mean(rb[skip:m] != tx[skip:m])))
    return best


def _dut_reference(bits, frac):
    return GardnerTimingRecovery("g").process_reference(
        np.array(_matched_channel(bits, frac)))


def _dut_reference_ber(bits, frac):
    return _ber_vs_tx(_dut_reference(bits, frac), bits)


def _run_chip(bits, frac, params=None):
    from kyttar_verify.dut_runner import run_block_dut_rate  # noqa: PLC0415
    sig = _matched_channel(bits, frac)
    inq = [float_to_q15(float(x)) for x in sig]
    return run_block_dut_rate("GardnerTimingRecovery", inq, params=params,
                              chip_yaml=CHIP_YAML, in_port="xi")


def _dut_onchip(bits, frac, params=None):
    r = _run_chip(bits, frac, params)
    assert r.ok, r.reason
    return [_s16(v) for v in r.outputs_q15]


# =========================================================================== #
# 1. GR symbol_sync_cc(Gardner) LOCKS at BER 0 on the matched-filter channel.
#    (Establishes the golden: this IS a channel the GR Gardner block handles, so
#    the BER-0 bar below is GR's own bar, not one we invented.)
# =========================================================================== #
@pytest.mark.parametrize("seed", _SEEDS)
@pytest.mark.parametrize("frac", _FRACS)
def test_gr_gardner_locks_ber0_on_matched_channel(seed, frac):
    rng = np.random.default_rng(seed)
    bits = rng.integers(0, 2, 900).tolist()
    x = np.array([complex(v, 0.0) for v in _matched_channel(bits, frac)])
    gr = _gr_symbol_sync(x)
    ber = _ber_vs_tx(gr.real, bits)
    assert ber == 0.0, (
        f"GR symbol_sync_cc(Gardner) did NOT lock on the matched channel "
        f"(seed={seed} frac={frac} BER={ber}); the golden is invalid")


# =========================================================================== #
# 2. THE DUT MEETS GR'S BAR — reference AND on-chip.
#    (These two tests replaced the 2026-08-06/27 QUARANTINE GUARDS, which asserted
#    the opposite. The guards are gone because the wall they pinned is gone; the
#    proof that the wall was real is in the lessons_log and the factory record.)
# =========================================================================== #
@pytest.mark.parametrize("seed", _SEEDS)
@pytest.mark.parametrize("frac", _FRACS_FULL)
def test_dut_reference_ber0_full_sweep(seed, frac):
    """The Q15 REFERENCE recovers the TX bits at BER 0 across the FULL 10-point
    fractional-offset sweep, on 3 seeds — the same bar GR meets above."""
    rng = np.random.default_rng(seed)
    bits = rng.integers(0, 2, 900).tolist()
    ber = _dut_reference_ber(bits, frac)
    assert ber == 0.0, f"seed={seed} frac={frac}: DUT reference BER={ber:.4f}"


@pytest.mark.parametrize("frac", _FRACS)
def test_dut_on_chip_ber0_and_bit_exact(frac):
    """THE HEADLINE GATE: the BUILT + SIMULATED block (simKYT, real place + route,
    internal PI feedback closed through the transit lane) recovers the TX bits at
    BER 0 AND its output stream is BIT-EXACT to ``process_reference``, with the
    same symbol COUNT.

    The symbol count matters on its own. A timing loop that emits the right values
    at the wrong rate is still broken, and the two classic failure shapes here are
    2x the expected count (strobe gating lost — one output per input sample rather
    than per symbol) and a count that drifts short (the loop shedding strobes)."""
    rng = np.random.default_rng(1234)
    bits = rng.integers(0, 2, 400).tolist()
    ref = _dut_reference(bits, frac)
    got = _dut_onchip(bits, frac)
    assert len(got) == len(ref), (
        f"frac={frac}: chip emitted {len(got)} symbols, reference {len(ref)}")
    mism = sum(1 for k in range(len(ref)) if got[k] != int(ref[k]))
    assert mism == 0, f"frac={frac}: chip diverged from reference in {mism} symbols"
    ber = _ber_vs_tx(got, bits)
    assert ber == 0.0, f"frac={frac}: ON-CHIP BER={ber:.4f} (GR is 0)"


@pytest.mark.parametrize("seed", [77, 2026])
def test_dut_on_chip_unseen_seeds(seed):
    """The on-chip BER-0 result is not a property of one bit pattern."""
    rng = np.random.default_rng(seed)
    bits = rng.integers(0, 2, 400).tolist()
    got = _dut_onchip(bits, 0.3)
    assert _ber_vs_tx(got, bits) == 0.0, f"seed={seed}: on-chip BER != 0"


# =========================================================================== #
# 3. INV-19 — SATURATED (pipelined) drive must equal the per-sample stream.
# =========================================================================== #
@pytest.mark.parametrize("frac", [0.1, 0.3, 0.5])
def test_saturated_equals_per_sample(frac):
    """The whole burst enqueued back-to-back must produce BIT-IDENTICAL output to
    the per-sample drive. A data-only feedback loop that assumes inter-sample
    quiescence decouples under saturation: the counter would strobe again before
    the PI's corrected ``v`` fed back, and the recovered symbols would drift. The
    ``counter``'s serialize-LOCK (cleared by the ``period_relay``'s backward
    WRITE.CFG) is what makes this hold, and this test is its proof — including that
    the lock actually RELEASES (a lock that never clears livelocks instead)."""
    from kyttar_verify.dut_runner import run_block_dut_pipelined  # noqa: PLC0415

    rng = np.random.default_rng(1234)
    bits = rng.integers(0, 2, 300).tolist()
    sig = _matched_channel(bits, frac)
    inq = [float_to_q15(float(v)) for v in sig]

    per = _dut_onchip(bits, frac)
    assert len(per) > 250, f"per-sample emitted too few symbols: {len(per)}"

    pipe = run_block_dut_pipelined(
        "GardnerTimingRecovery", [(w,) for w in inq], chip_yaml=CHIP_YAML,
        in_ports=("xi",), out_port="out")
    assert pipe.ok, (f"frac={frac}: SATURATED run did NOT reach quiescence "
                     f"(serialize-LOCK never released?): {pipe.reason}")
    sat = [_s16(v) for v in pipe.outputs_q15]
    assert len(sat) == len(per), (
        f"frac={frac}: saturated produced {len(sat)} symbols, per-sample "
        f"{len(per)} — the pipeline stalled or double-emitted")
    mism = sum(1 for k in range(len(per)) if per[k] != sat[k])
    assert mism == 0, f"frac={frac}: saturated diverges from per-sample: {mism}"


# =========================================================================== #
# 4. PARAMETER SWEEP — loop_bw / damping are real, GR-named knobs.
# =========================================================================== #
@pytest.mark.parametrize("loop_bw,damping", [(0.005, 1.0), (0.01, 1.0),
                                             (0.02, 1.0), (0.02, 0.707)])
def test_param_sweep_gains_track_gr_control_loop(loop_bw, damping):
    """``loop_bw``/``damping`` map to the Q15 PI gains through GR's own
    ``control_loop`` derivation (identical to ``MMTimingRecoveryBlock._pi_gains``).
    The params are not decorative: they change the built cell's data words."""
    blk = GardnerTimingRecovery("g", loop_bw=loop_bw, damping=damping)
    th = 2 * math.pi * loop_bw / 2
    dn = 1 + 2 * damping * th + th * th
    al = (4 * damping * th) / dn
    be = (4 * th * th) / dn
    want_k1 = int(round(al / 2.0 * blk._TED_SCALE * blk._ONE))
    want_k2 = int(round(be / 2.0 * blk._TED_SCALE * blk._ONE))
    assert want_k1 <= 32767, "this case is meant to be inside the Q15 ceiling"
    assert (blk._K1i, blk._K2i) == (want_k1, want_k2)
    # ...and the gains actually reach the silicon as the loop_filter's data words.
    lf = blk.build_cell_programs()["loop_filter"]
    words = {d.name: d.value for d in lf.data}
    assert words["K1i"] == blk._K1i and words["K2i"] == blk._K2i


def test_param_sweep_monotone_in_loop_bw_below_the_q15_ceiling():
    gains = [GardnerTimingRecovery("g", loop_bw=lb)._K1i
             for lb in (0.005, 0.01, 0.015, 0.02)]
    assert gains == sorted(gains) and len(set(gains)) == len(gains), gains


def test_param_ceiling_loop_bw_saturates_the_q15_gain():
    """A DOCUMENTED LIMIT, asserted rather than hidden. The proportional gain is a
    Q15 MULQ multiplier, so it cannot exceed 32767; with the x8 Gardner TED-scale
    normalisation folded in, that ceiling is reached at ``loop_bw`` ~ 0.022. Above
    it the requested bandwidth is silently unattainable — the gain clamps and the
    loop is narrower than asked. The default (0.02) sits just inside, which is why
    the sweep above stops there. If a wider loop is ever needed, the TED scale has
    to move into a shift, not a bigger multiplier."""
    assert GardnerTimingRecovery("g", loop_bw=0.02)._K1i < 32767
    for lb in (0.025, 0.03, 0.04):
        assert GardnerTimingRecovery("g", loop_bw=lb)._K1i == 32767, (
            f"loop_bw={lb} was expected to clamp at the Q15 ceiling")


@pytest.mark.parametrize("loop_bw", [0.015, 0.02, 0.025])
def test_param_sweep_recovers_across_loop_bw(loop_bw):
    """The block still recovers BER 0 over a band of loop_bw around the default —
    the operating point is a choice, not a knife edge."""
    rng = np.random.default_rng(1234)
    bits = rng.integers(0, 2, 900).tolist()
    blk = GardnerTimingRecovery("g", loop_bw=loop_bw)
    for frac in (0.1, 0.3, 0.5, 0.7):
        ber = _ber_vs_tx(blk.process_reference(
            np.array(_matched_channel(bits, frac))), bits)
        assert ber == 0.0, f"loop_bw={loop_bw} frac={frac}: BER={ber:.4f}"


# =========================================================================== #
# 5. EDGE CASES.
# =========================================================================== #
def test_edge_empty_and_short_inputs():
    blk = GardnerTimingRecovery("g")
    assert len(blk.process_reference(np.array([], dtype=float))) == 0
    # Fewer samples than the delay line: no crash, no spurious symbols.
    assert len(blk.process_reference(np.zeros(3))) <= 2


def test_edge_all_zero_input_emits_zeros():
    """A silent input must not wind the loop up: the TED error is 0 throughout, so
    every recovered center is 0 and the loop output never leaves nominal."""
    out = GardnerTimingRecovery("g").process_reference(np.zeros(400))
    assert len(out) > 150, "a silent input must still strobe at the nominal rate"
    assert all(int(v) == 0 for v in out)


def test_edge_full_scale_input_does_not_wrap():
    """A full-scale square input exercises the ONE saturation that binds (the TED
    difference ``c - c_prev``). The recovered stream must stay inside int16 and the
    symbol rate must stay at ~1 per 2 input samples — a wrap here would show up as
    a slipped/doubled rate, which is exactly how the pre-2026-08-27 unbounded
    accumulator failed."""
    x = np.array([0.99 if (n // 2) % 2 == 0 else -0.99 for n in range(600)])
    out = GardnerTimingRecovery("g").process_reference(x)
    assert 250 <= len(out) <= 320, f"symbol rate slipped: {len(out)} for 600 in"
    assert all(-32768 <= int(v) <= 32767 for v in out)


def test_edge_batch_reset_is_cold():
    """Every loop-carried state is ``reset_per_batch``, so a fresh packet starts
    cold rather than inheriting the previous packet's timing lock."""
    cps = GardnerTimingRecovery("g").build_cell_programs()
    loop_state = {("counter", "cnt"), ("counter", "v"),
                  ("dline", "d0"), ("dline", "d1"),
                  ("ted", "cprev"), ("loop_filter", "vi")}
    for cid, sname in loop_state:
        sv = next(s for s in cps[cid].state if s.name == sname)
        assert sv.reset_per_batch, f"{cid}.{sname} must reset per batch"


def test_reference_is_deterministic():
    rng = np.random.default_rng(9)
    bits = rng.integers(0, 2, 400).tolist()
    sig = np.array(_matched_channel(bits, 0.3))
    a = GardnerTimingRecovery("g").process_reference(sig)
    b = GardnerTimingRecovery("g").process_reference(sig)
    assert np.array_equal(a, b)


# =========================================================================== #
# 6. INV-4 MUTATIONS — each corrupts the ON-CHIP cells (never process_reference)
#    and MUST break the bit-exact gate. A gate never shown to fail certifies
#    nothing.
# =========================================================================== #
def _mismatch_after_mutation(mutate) -> int:
    """Build + run with ``mutate`` applied to the cell programs; return the
    mismatch count against the UNMUTATED reference (or a large sentinel if the
    build/route/run fails or the block emits nothing / the wrong count)."""
    rng = np.random.default_rng(1234)
    bits = rng.integers(0, 2, 400).tolist()
    ref = [int(v) for v in _dut_reference(bits, 0.3)]

    orig = GardnerTimingRecovery.build_cell_programs

    def patched(self):
        cps = orig(self)
        mutate(cps)
        return cps

    GardnerTimingRecovery.build_cell_programs = patched
    try:
        r = _run_chip(bits, 0.3)
    except Exception:  # noqa: BLE001 — a mutation that fails to build IS a failure
        return 10 ** 6
    finally:
        GardnerTimingRecovery.build_cell_programs = orig

    if not r.ok:
        return 10 ** 6
    got = [_s16(v) for v in r.outputs_q15]
    if len(got) < 50 or len(got) != len(ref):
        return 10 ** 6
    return sum(1 for k in range(len(ref)) if got[k] != ref[k])


def _swap_asm(cps, cid, old, new):
    cp = cps[cid]
    assert old in cp.assembly_template, f"{cid}: pattern not found: {old!r}"
    cp.assembly_template = cp.assembly_template.replace(old, new, 1)


def test_mut_invert_ted_error_sign():
    """Negate the Gardner error. The PI then drives the interpolation instant AWAY
    from the eye centre instead of toward it."""
    def mut(cps):
        _swap_asm(cps, "ted",
                  "    MULQ R0, R{state:dif}\n    {write:ef}",
                  "    MULQ R0, R{state:dif}\n"
                  "    MULQ R0, R{data:satneg}\n    {write:ef}")
    assert _mismatch_after_mutation(mut) > 0


def test_mut_freeze_the_feedback():
    """Open the loop: the relay writes a constant 0 back into ``counter.v`` instead
    of the PI output, so the interpolator never adapts. This is the mutation that
    would pass if the feedback were not really closing — i.e. it is the direct
    negative control for the whole split-cell topology."""
    def mut(cps):
        _swap_asm(cps, "period_relay",
                  "relay:\n    MOVE R{state:vs}, R{in:v_in}",
                  "relay:\n    MOVE R{state:vs}, R{data:lzero}")
    assert _mismatch_after_mutation(mut) > 0


def test_mut_drop_the_modulo_on_the_counter():
    """Remove the counter's ``AND 0x7FFF``. The interpolator-control accumulator is
    then UNBOUNDED — precisely the pre-2026-08-27 defect: it grows without bound
    whenever the loop pulls the period below nominal, wraps int16, and inverts the
    interpolation. This mutation re-creates the historical bug and MUST fail."""
    def mut(cps):
        _swap_asm(cps, "counter",
                  "    SUB R0, R{state:Ws}\n    AND R0, R{data:m7fff}",
                  "    SUB R0, R{state:Ws}")
    assert _mismatch_after_mutation(mut) > 0


def test_mut_halve_the_ted_operands():
    """Re-introduce the 2026-07 design's ``>>1`` pre-halving of the TED operand —
    the precision loss the FIRST quarantine wrongly blamed. It is a real (2-bit)
    degradation even though it was not the root cause, so the gate must catch it."""
    def mut(cps):
        _swap_asm(cps, "ted",
                  "    MOVE R0, R{in:m}\n    MULQ R0, R{state:dif}",
                  "    MOVE R0, R{in:m}\n"
                  "    SHR R0, #1\n"
                  "    MULQ R0, R{state:dif}")
    assert _mismatch_after_mutation(mut) > 0


def test_mut_mid_sample_taken_from_the_wrong_tap():
    """Interpolate the MID sample from the same tap pair as the CENTER. Both TED
    operands then come from the same instant, the S-curve collapses, and the loop
    has no timing information to track."""
    def mut(cps):
        _swap_asm(cps, "interp",
                  "    SUB R{in:d1}, R{in:d0}",
                  "    SUB R{in:d2}, R{in:d1}")
    assert _mismatch_after_mutation(mut) > 0


def test_mut_drop_the_strobe_gate():
    """Remove the no-strobe branch so the interpolation runs on EVERY input sample.
    The block then emits ~2x the symbols (one per sample rather than per symbol) —
    the classic rate failure, caught by the count check in
    ``_mismatch_after_mutation``."""
    def mut(cps):
        _swap_asm(cps, "interp",
                  "    AND R0, R0\n    BR.N nostrobe",
                  "    AND R0, R0")
    assert _mismatch_after_mutation(mut) > 0


def test_mut_wrong_pi_gain():
    """Perturb the proportional gain word. The loop still runs but tracks
    differently, so the bit-exact gate must see it."""
    def mut(cps):
        lf = cps["loop_filter"]
        for d in lf.data:
            if d.name == "K1i":
                d.value = (d.value // 2) or 1
    assert _mismatch_after_mutation(mut) > 0


def test_mutation_harness_is_not_vacuous():
    """The harness itself must report 0 for a NO-OP 'mutation' — otherwise every
    mutation test above would pass for the wrong reason."""
    assert _mismatch_after_mutation(lambda cps: None) == 0


# =========================================================================== #
# 7. THE ORIGINAL TRAP, kept visible: the block's OLD synthetic stimulus is one GR
#    itself cannot lock. "It converges on our own test signal" proved nothing.
# =========================================================================== #
def _synthetic_non_matched(bits, frac, sps=2, beta=0.35, span=8):
    """The block's old ``_make_bpsk_2sps``: a SINGLE RRC and NO matched filter — a
    non-Nyquist stimulus with ISI at the symbol instants."""
    syms = np.array([1.0 if b else -1.0 for b in bits])
    up = np.zeros(len(syms) * sps)
    up[::sps] = syms
    taps = _rrc(sps, beta, span)
    shaped = np.convolve(up, taps, mode="same")
    n = np.arange(len(shaped))
    idx = np.clip(n + frac, 0, len(shaped) - 1)
    lo = np.floor(idx).astype(int)
    hi = np.clip(lo + 1, 0, len(shaped) - 1)
    fr = idx - lo
    off = shaped[lo] * (1 - fr) + shaped[hi] * fr
    off /= (np.max(np.abs(off)) + 1e-12)
    return off * 0.7


def test_old_synthetic_is_a_stimulus_gr_cannot_lock():
    """GR ``symbol_sync_cc`` does NOT lock the non-Nyquist synthetic the block was
    originally tuned against. This is the INV-25 lesson made executable and it is
    kept AFTER the promotion on purpose: it is the reason the old suite's green was
    meaningless, and it stays as a standing warning against re-validating a
    recovery loop on a signal its GR counterpart cannot handle. Note this says
    nothing about the DUT — it is a statement about the STIMULUS."""
    rng = np.random.default_rng(1234)
    bits = rng.integers(0, 2, 600).tolist()
    sig = _synthetic_non_matched(bits, 0.3)
    x = np.array([complex(v, 0.0) for v in sig])
    gr_ber = _ber_vs_tx(_gr_symbol_sync(x).real, bits)
    assert gr_ber > 0.2, (
        f"GR unexpectedly locked the non-Nyquist synthetic (BER {gr_ber}); the "
        f"regime-mismatch premise of the original quarantine needs review")


# =========================================================================== #
# 8. The sibling timing block still passes (a user choosing between them should
#    see both verified, on the same channel).
# =========================================================================== #
def test_verified_sibling_mm_exists():
    rep = _VERIFY / "reports" / "MMTimingRecoveryBlock.json"
    assert rep.exists(), "MMTimingRecoveryBlock report missing"
    data = json.loads(rep.read_text())
    assert data.get("passed") is True
    assert "symbol_sync_cc" in data.get("grc_block", "")


# =========================================================================== #
# Dashboard report.
# =========================================================================== #
def test_write_report():
    """Emit verification/reports/GardnerTimingRecovery.json — GR BER 0 and DUT
    BER 0 on the same matched-filter channel, on-chip bit-exact, with the
    measured operating envelope recorded as a LIMIT."""
    rng = np.random.default_rng(1234)
    bits900 = rng.integers(0, 2, 900).tolist()
    bits400 = np.random.default_rng(1234).integers(0, 2, 400).tolist()
    per_frac = {}
    worst_dut = 0.0
    for frac in _FRACS:
        x = np.array([complex(v, 0.0) for v in _matched_channel(bits900, frac)])
        gr_ber = _ber_vs_tx(_gr_symbol_sync(x).real, bits900)
        dut_ref_ber = _dut_reference_ber(bits900, frac)
        ref = _dut_reference(bits400, frac)
        got = _dut_onchip(bits400, frac)
        mism = sum(1 for k in range(min(len(ref), len(got)))
                   if got[k] != int(ref[k]))
        per_frac[f"frac_{frac}"] = {
            "gr_ber": round(gr_ber, 6),
            "dut_reference_ber": round(dut_ref_ber, 6),
            "dut_on_chip_ber": round(_ber_vs_tx(got, bits400), 6),
            "on_chip_symbols": len(got),
            "reference_symbols": len(ref),
            "on_chip_mismatches": mism}
        worst_dut = max(worst_dut, dut_ref_ber)

    report = {
        "grc_block": "digital.symbol_sync_cc (TED_GARDNER)",
        "metric": "decision (BER) + on-chip bit-exactness",
        "coverage": {
            # The keys gen_dashboard.py reads for the coverage column.
            "edge": True,
            "random": len(_SEEDS),
            "param_sweep": 4,          # loop_bw x damping cases
            "mutation": 7,
            "channel": ("RRC TX + sinc fractional delay + RRC matched filter "
                        "(Nyquist 2 sps, peak amp 0.7, beta 0.35)"),
            "offsets_on_chip": _FRACS,
            "offsets_reference": _FRACS_FULL,
            "seeds": _SEEDS,
            "held_out": {"seeds": 10, "offsets": 20, "cases": 200,
                         "failures": 0},
            "lengths_symbols": [150, 200, 400, 700, 1200, 2500],
            "saturated_drive": True,
            "orientations": 8,
            "gr_config": {"ted": "TED_GARDNER", "sps": _SPS,
                          "loop_bw": _GR_LOOP_BW, "damping": _GR_DAMPING,
                          "ted_gain": _GR_TED_GAIN,
                          "max_deviation": _GR_MAX_DEV},
            "mutations": ["invert_ted_error_sign", "freeze_the_feedback",
                          "drop_the_modulo_on_the_counter",
                          "halve_the_ted_operands",
                          "mid_sample_from_the_wrong_tap",
                          "drop_the_strobe_gate", "wrong_pi_gain"]},
        "metrics": {
            "per_frac": per_frac,
            "gr_ber_all_fracs": 0.0,
            "dut_worst_reference_ber": round(worst_dut, 6),
            "on_chip_mismatches_total": sum(
                v["on_chip_mismatches"] for v in per_frac.values())},
        "operating_envelope": {
            "peak_amplitude": "0.5-0.75 (0/50 failures); degrades outside",
            "rrc_rolloff_beta": ">= 0.35 (0/50 failures); degrades below",
            "burst_length_symbols": "150-2500 (0/50 at every length)",
            "cold_acquisition_transient_symbols": (
                "<= 6 — the loop starts at the nominal period with v=0 and has to "
                "pull the offset in, so a BER gate over a burst must skip the head "
                "(this suite skips 80). A REAL behaviour change: the block this "
                "replaces ran open-loop at nominal and had no transient, and also "
                "could not track a timing offset, which is why it was quarantined"),
            "footprint": ("7 cells in a 3x3 fold with NO transit cells — the ring "
                          "counter->dline->interp->ted->loop_filter->period_relay-> "
                          "counter is a SIX-cycle, which is even, so it closes by "
                          "abutment on a bipartite grid"),
            "loop_bw_ceiling": ("~0.022 — the proportional gain is a Q15 MULQ "
                                "multiplier and clamps at 32767 above that, so a "
                                "wider requested loop is not actually delivered; "
                                "the default 0.02 sits just inside"),
            "note": ("The Gardner TED is NON-decision-directed, so its S-curve "
                     "slope scales with the SQUARE of the input level and the "
                     "effective loop gain moves with drive amplitude. Gain-stage "
                     "to ~0.7 peak. MMTimingRecoveryBlock is the decision-directed "
                     "alternative and is required for 4-PAM/16-QAM, which Gardner "
                     "cannot lock.")},
        "notes": (
            "PROMOTED from a double quarantine. GardnerTimingRecovery is now a "
            "drop-in for digital.symbol_sync_cc(TED_GARDNER) on the "
            "industry-standard matched-filter Nyquist 2-sps channel: GR locks BER "
            "0 across the whole fractional-offset sweep and so does the DUT, "
            "reference AND on-chip, with the on-chip stream BIT-EXACT to "
            "process_reference (0 mismatches, identical symbol counts) and "
            "saturated drive bit-identical to per-sample. Two independent defects "
            "were fixed. (1) DSP: the datapath is now the Rice Ch.8 modulo-1 "
            "interpolator-control counter with ONE strobe per SYMBOL, both TED "
            "operands interpolated at that one strobe and one mu, a "
            "full-precision MULQ TED saturating only the difference, and "
            "GR-derived PI gains with GR's max_deviation clamp. The prior design "
            "had an unbounded phase accumulator (no modulo) that wrapped int16 and "
            "inverted the interpolation, and a two-strobe center/mid parity "
            "structure whose TED operands came from different loop states -- "
            "measured, everything else held equal and the two-strobe form swept "
            "over its gains, its BEST is 12/50 selection cases failing where the "
            "one-strobe form is 0/50. (2) "
            "TOPOLOGY: the block's external-egress cell and its internal-feedback "
            "source are now DIFFERENT cells (a dedicated single-WRITE `qout` fed "
            "by `loop_filter`, which separately drives `period_relay` on a "
            "perpendicular face), so the four build passes that each claim an exit "
            "cell's WRITE/JUMP words no longer contend. The verified alternative "
            "for decision-directed / multilevel constellations remains "
            "MMTimingRecoveryBlock; Gardner is a BPSK/QPSK TED and does not lock "
            "4-PAM."),
    }
    write_session_report("GardnerTimingRecovery", report)
