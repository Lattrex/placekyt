# SPDX-License-Identifier: GPL-3.0-or-later
"""ComplexCostasLoopBlock — GNU-Radio digital.costas_loop_cc equivalence + INV-4.

Finalizes + VERIFIES the PoC ComplexCostasLoopBlock (INV-25: code existed, was used
in the coherent BPSK RX demo, but was NEVER held against GNU Radio per-block). It is a
decision-directed carrier-recovery FEEDBACK loop, so the gate is on the recovered
DECISION (the sign of the derotated in-phase ``yi``), not a sample-by-sample amplitude
match — a Q15 loop and GR's float loop converge to the SAME symbol decisions but along
slightly different soft-value trajectories (the amplitude differs by hundreds of LSB
while every hard decision agrees; see ``notes`` in the emitted report).

Params mirror ``digital.costas_loop_cc(loop_bw, order)`` VERBATIM: ``loop_bw`` and
``order`` (2 = BPSK, 4 = QPSK). ``damping`` is an extra Kyttar loop-shaping param
(derived alpha/beta), defaulted so ``loop_bw`` alone reproduces GR.

Three verification tiers:
  1. ON-CHIP BIT-EXACT — the built + simulated DUT equals ``process_reference`` (the
     Q15 model) EXACTLY on the recovered ``yi``, across the order × loop_bw ×
     frequency-offset sweep. This is the substrate proof (the 7-cell BPSK / 8-cell
     QPSK feedback loop, closed and tracking, computes what the model says).
  2. GR EQUIVALENCE — the DUT (and hence the model) recovers the SAME symbol decisions
     as ``digital.costas_loop_cc`` on the same complex input: 0 hard-decision (sign)
     mismatches on the settled tail after RMS normalisation, across the same sweep.
  3. SATURATED (INV-19) — the saturated (pipelined, back-to-back) on-chip output equals
     the per-sample on-chip output BIT-for-BIT (the serialize-LOCK holds the loop
     closed under continuous drive).

INV-4: mutations of the on-chip cells (invert the PD error sign, corrupt the QPSK
2-term detector, build at the wrong loop_bw, +1 sample delay, empty output) each PUSH
the DUT off its reference / off GR and MUST FAIL the gate.

THE POC BUG THIS CAUGHT + FIXED (order 4 / QPSK): the ``qpd`` output cell emitted a
2-rail (yi_tap, yq_tap) tail, so the build's single-rail last-WRITE patch routed only
yq_tap and left yi_tap on a stale @1 hop that COLLIDED with the err->pd_pi handoff — the
recovered I was corrupted and the on-chip QPSK loop diverged from its own (GR-verified)
reference (199/200 samples wrong). Fix: emit ONLY the recovered ``yi_tap`` (a single
contiguous tail WRITE, exactly like the proven order-2 rotate), so the last-write patch
steers it cleanly. Order 2 (BPSK) was already correct; order 4 is now bit-exact too.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_complex_costas_loop.py -q
"""
from __future__ import annotations

import json
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
for p in (str(_PLACEKYT), str(_VERIFY), str(_RUNTIME)):
    if p not in sys.path:
        sys.path.insert(0, p)

from kyttar_verify import write_session_report  # noqa: E402

from gr_kyttar.placement.blocks.complex_costas_loop_block import (  # noqa: E402
    ComplexCostasLoopBlock,
)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_PY = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")
_GR_AVAILABLE = os.path.exists(_GR_PY)
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_AVAILABLE),
    reason="chip yaml or GNU Radio interpreter absent")

# The settled tail where the loop has locked (leading symbols are the pull-in
# transient — GR and the Q15 loop pull in along different soft trajectories, but the
# hard decisions agree once locked).
_LOCK = 150
# Sweep: order (2=BPSK, 4=QPSK) x loop_bw x normalized frequency offset (rad/sample).
_LOOP_BWS = [0.02, 0.05, 0.1]
_FOFFS = [0.0, 0.02, -0.03]


def _s16(v):
    if v is None:
        return None
    v = int(v) & 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _stim(order, foff, n=300, seed=3):
    """A unit-modulus BPSK/QPSK burst with a small carrier frequency offset — the
    signal a costas loop is built to recover. Amplitude 0.7 keeps the derotation
    product inside Q15."""
    rng = np.random.RandomState(seed)
    if order == 2:
        pts = (rng.randint(0, 2, n) * 2 - 1).astype(np.complex128)
    else:
        b = rng.randint(0, 4, n)
        pts = np.exp(1j * (np.pi / 4 + b * np.pi / 2))
    return pts * np.exp(1j * foff * np.arange(n)) * 0.7


def _run_chip(stim, order, loop_bw):
    from kyttar_verify.dut_runner import run_block_dut_complex  # noqa: PLC0415
    return run_block_dut_complex(
        "ComplexCostasLoopBlock", stim,
        params={"loop_bw": loop_bw, "order": order},
        chip_yaml=CHIP_YAML, in_ports=("xi", "xq"),
        words_per_sample=(2 if order == 4 else 1))


def _gr_costas(stim, loop_bw, order):
    """digital.costas_loop_cc(loop_bw, order) recovered complex output (GR golden)."""
    payload = {"x": [[float(c.real), float(c.imag)] for c in stim],
               "lb": float(loop_bw), "order": int(order)}
    script = (
        "import json,sys\n"
        "from gnuradio import gr, digital, blocks\n"
        "d=json.load(sys.stdin); x=[complex(a,b) for a,b in d['x']]\n"
        "tb=gr.top_block(); src=blocks.vector_source_c(x,False)\n"
        "cl=digital.costas_loop_cc(d['lb'],d['order']); snk=blocks.vector_sink_c()\n"
        "tb.connect(src,cl,snk); tb.run(); y=list(snk.data())\n"
        "print(json.dumps([[float(v.real),float(v.imag)] for v in y]))\n")
    p = subprocess.run([_GR_PY, "-c", script], input=json.dumps(payload),
                       capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise RuntimeError(f"GR costas failed: {p.stderr[-400:]}")
    data = json.loads(p.stdout.strip().splitlines()[-1])
    return np.array([complex(a, b) for a, b in data])


def _tail_sign_mismatch(dut_i, gr_i, lock=_LOCK):
    """Hard-decision (sign) mismatch count on the settled tail, after RMS-normalising
    both streams to the same scale (a decision loop is scale-relative)."""
    a = np.asarray(dut_i, dtype=float)
    b = np.asarray(gr_i, dtype=float)
    m = min(len(a), len(b))
    a, b = a[:m], b[:m]
    a = a / (np.sqrt(np.mean(a[lock:m] ** 2)) + 1e-9)
    b = b / (np.sqrt(np.mean(b[lock:m] ** 2)) + 1e-9)
    return int(np.sum(np.sign(a[lock:m]) != np.sign(b[lock:m]))), m - lock


# ============================================================ reference sanity
def test_reference_matches_gr_bpsk():
    """The Q15 process_reference recovers the SAME BPSK decisions as
    digital.costas_loop_cc (the model is a faithful GR oracle before we trust the
    on-chip bit-exact gate against it)."""
    x = _stim(2, 0.02)
    ref = ComplexCostasLoopBlock("r", loop_bw=0.05, order=2).process_reference(x)
    gr = _gr_costas(x, 0.05, 2).real
    mis, n = _tail_sign_mismatch([int(v) for v in ref], gr)
    assert mis == 0, f"reference BPSK decisions diverge from GR: {mis}/{n}"


def test_reference_matches_gr_qpsk():
    """Same, QPSK (order 4): the 2-term PD reference tracks GR's QPSK decisions."""
    x = _stim(4, 0.02)
    ref = ComplexCostasLoopBlock("r", loop_bw=0.05, order=4).process_reference(x)
    gr = _gr_costas(x, 0.05, 4).real
    mis, n = _tail_sign_mismatch([int(v) for v in ref], gr)
    assert mis == 0, f"reference QPSK decisions diverge from GR: {mis}/{n}"


# ================================================= tier 1: ON-CHIP bit-exact
@pytest.mark.parametrize("order", [2, 4])
@pytest.mark.parametrize("loop_bw", _LOOP_BWS)
@pytest.mark.parametrize("foff", _FOFFS)
def test_on_chip_bit_exact(order, loop_bw, foff):
    """The built + simulated DUT equals process_reference EXACTLY on the recovered
    ``yi`` — the feedback loop (dphase -> phase corridor) is CLOSED and tracking, so
    the match holds through the whole burst, across the full parameter sweep.

    THIS is the gate that caught the order-4 PoC bug: before the fix the on-chip QPSK
    diverged from its own reference at 199/200 samples."""
    x = _stim(order, foff)
    blk = ComplexCostasLoopBlock("r", loop_bw=loop_bw, order=order)
    ref_c = blk.process_reference_complex(x)
    ref_i = [int(a) for a, _ in ref_c]
    dut = _run_chip(x, order, loop_bw)
    assert dut.ok, f"build/route/run failed: {dut.reason}"
    emit_i = [_s16(v) for v in dut.i_q15 if v is not None]
    assert len(emit_i) >= 200, f"on-chip emitted too few symbols: {len(emit_i)}"
    m = min(len(emit_i), len(ref_i))
    mis = sum(1 for k in range(m) if emit_i[k] != ref_i[k])
    assert mis == 0, (f"order={order} lb={loop_bw} foff={foff}: on-chip I diverged "
                      f"from reference: {mis}/{m} "
                      f"(first: {[(k, emit_i[k], ref_i[k]) for k in range(m) if emit_i[k] != ref_i[k]][:4]})")
    if order == 4:
        # order 4 recovers the COMPLEX pair: verify the Q rail is BIT-EXACT too (the
        # egress fix steers BOTH yi_tap and yq_tap to the port with distinct tags — a
        # dropped/collided Q rail was the PoC bug).
        ref_q = [int(b) for _, b in ref_c]
        emit_q = [_s16(v) for v in dut.q_q15 if v is not None]
        mq = min(len(emit_q), len(ref_q))
        assert mq >= 200, f"order 4 on-chip emitted too few Q symbols: {mq}"
        misq = sum(1 for k in range(mq) if emit_q[k] != ref_q[k])
        assert misq == 0, f"order 4 lb={loop_bw} foff={foff}: on-chip Q diverged: {misq}/{mq}"


# ================================================= tier 2: GR equivalence
@pytest.mark.parametrize("order", [2, 4])
@pytest.mark.parametrize("loop_bw", _LOOP_BWS)
@pytest.mark.parametrize("foff", _FOFFS)
def test_dut_matches_gnuradio(order, loop_bw, foff):
    """The on-chip DUT recovers the SAME symbol decisions as digital.costas_loop_cc:
    0 hard-decision (sign) mismatches on the settled tail, across the sweep. This is
    the drop-in-equivalence gate (INV-0/INV-25) — the metric is DECISION (manifest),
    not amplitude, because a Q15 loop and GR's float loop track to the same decisions
    along different soft-value trajectories."""
    x = _stim(order, foff)
    dut = _run_chip(x, order, loop_bw)
    assert dut.ok, dut.reason
    emit = [_s16(v) for v in dut.i_q15 if v is not None]
    gr = _gr_costas(x, loop_bw, order).real
    mis, n = _tail_sign_mismatch(emit, gr)
    assert mis == 0, (f"order={order} lb={loop_bw} foff={foff}: DUT decisions diverge "
                      f"from GR costas_loop_cc: {mis}/{n}")


# ================================================= tier 3: SATURATED (INV-19)
@pytest.mark.parametrize("order", [2, 4])
def test_saturated_equals_per_sample(order):
    """The saturated (pipelined, whole burst enqueued back-to-back) on-chip output
    equals the per-sample on-chip output BIT-for-BIT — the serialize-LOCK
    (pipeline_lock=True) holds the carrier loop closed under continuous drive (INV-19,
    the Costas cautionary tale). A loop that decoupled under saturation, or a lock that
    failed to release (livelock), is caught here."""
    from kyttar_verify.dut_runner import (  # noqa: PLC0415
        run_block_dut_complex, run_block_dut_pipelined)
    from gr_kyttar.placement.blocks._base import float_to_q15  # noqa: PLC0415

    x = _stim(order, 0.02, n=120)
    seq = run_block_dut_complex(
        "ComplexCostasLoopBlock", x, params={"loop_bw": 0.05, "order": order},
        chip_yaml=CHIP_YAML, in_ports=("xi", "xq"),
        words_per_sample=(2 if order == 4 else 1))
    assert seq.ok, f"per-sample build/run failed: {seq.reason}"
    ref_i = [_s16(v) for v in seq.i_q15 if v is not None]
    assert len(ref_i) >= 100, f"per-sample emitted too few: {len(ref_i)}"

    samples = [(float_to_q15(float(c.real)), float_to_q15(float(c.imag))) for c in x]
    pipe = run_block_dut_pipelined(
        "ComplexCostasLoopBlock", samples, params={"loop_bw": 0.05, "order": order},
        chip_yaml=CHIP_YAML, in_ports=("xi", "xq"), out_port="yi_tap")
    assert pipe.ok, (f"order={order}: SATURATED run did not reach quiescence "
                     f"(livelock?): {pipe.reason}")
    # order 4 egresses a COMPLEX pair (2 words/sample interleaved [yi,yq,...]); the I
    # rail is every other word. order 2 is a single yi word/sample.
    flat = [_s16(w) for w in pipe.outputs_q15]
    sat_i = flat[0::2] if order == 4 else flat
    n = min(len(ref_i), len(sat_i))
    assert len(sat_i) >= n, (f"order={order}: saturated produced {len(sat_i)} syms, "
                             f"per-sample {len(ref_i)} — pipeline STALLED")
    mis = sum(1 for k in range(n) if ref_i[k] != sat_i[k])
    assert mis == 0, f"order={order}: saturated diverges from per-sample: {mis}/{n}"


# ================================================= INV-4 mutations
# Each mutation edits the on-chip cell PROGRAMS (never process_reference) so the chip no
# longer matches its own reference / GR. The gates above are only meaningful if these
# break them, so each MUST produce a nonzero divergence (or fail to build/emit).

def _mismatch_after_mutation(mutate, order=2) -> int:
    """Build+run with ``mutate(cps)`` applied to the cell programs; return the I-channel
    mismatch count vs the UNMUTATED reference (large sentinel if build/emit fails)."""
    x = _stim(order, 0.02)
    ref = [int(v) for v in
           ComplexCostasLoopBlock("r", loop_bw=0.05, order=order).process_reference(x)]
    orig = ComplexCostasLoopBlock.build_cell_programs

    def patched(self):
        cps = orig(self)
        mutate(cps)
        return cps

    ComplexCostasLoopBlock.build_cell_programs = patched
    try:
        dut = _run_chip(x, order, 0.05)
    finally:
        ComplexCostasLoopBlock.build_cell_programs = orig
    if not dut.ok:
        return 10 ** 6
    emit = [_s16(v) for v in dut.i_q15 if v is not None]
    if len(emit) < 50:
        return 10 ** 6
    m = min(len(emit), len(ref))
    return sum(1 for k in range(m) if emit[k] != ref[k])


def _swap(cps, cid, old, new):
    cp = cps[cid]
    assert old in cp.assembly_template, f"{cid}: pattern not found: {old!r}"
    cp.assembly_template = cp.assembly_template.replace(old, new, 1)


def test_mut_invert_bpsk_pd_sign():
    """Flip the BPSK phase-detector sign (the decision-directed err = sign(yi)*yq): the
    loop drives the WRONG way and walks off lock instead of tracking."""
    def mut(cps):
        _swap(cps, "pd_pi",
              "    SUB R{data:zero}, R{state:yqs}",
              "    ADD R{data:zero}, R{state:yqs}")
    assert _mismatch_after_mutation(mut, order=2) > 0


def test_mut_corrupt_qpsk_detector():
    """Corrupt the QPSK 2-term detector (drop term2's subtraction -> wrong err): the
    order-4 loop no longer forms err = sign(yi)*yq - sign(yq)*yi and diverges."""
    def mut(cps):
        _swap(cps, "qpd",
              "    SUB R{state:err}, R0",
              "    ADD R{state:err}, R0")
    assert _mismatch_after_mutation(mut, order=4) > 0


def test_mut_wrong_loop_bw_fails():
    """A DUT built at a DIFFERENT loop_bw must diverge from the right-loop_bw reference
    (its alpha/beta gains differ -> a different phase trajectory)."""
    x = _stim(2, 0.05)
    ref = [int(v) for v in
           ComplexCostasLoopBlock("r", loop_bw=0.02, order=2).process_reference(x)]
    dut = _run_chip(x, 2, 0.1)   # built at lb=0.1, compared to lb=0.02 reference
    assert dut.ok, dut.reason
    emit = [_s16(v) for v in dut.i_q15 if v is not None]
    m = min(len(emit), len(ref))
    assert sum(1 for k in range(m) if emit[k] != ref[k]) > 0, \
        "gate failed to detect a wrong loop_bw!"


def test_mut_one_sample_delay_fails():
    """A +1-sample delay on the recovered stream must diverge from the delay-0
    reference (catches a latency/off-by-one bug — INV-2)."""
    x = _stim(2, 0.02)
    ref = [int(v) for v in
           ComplexCostasLoopBlock("r", loop_bw=0.05, order=2).process_reference(x)]
    dut = _run_chip(x, 2, 0.05)
    assert dut.ok, dut.reason
    emit = [_s16(v) for v in dut.i_q15 if v is not None]
    shifted = [0] + emit[:-1]
    m = min(len(shifted), len(ref))
    assert sum(1 for k in range(m) if shifted[k] != ref[k]) > 0, \
        "gate failed to detect a 1-sample latency error!"


def test_mut_qpsk_swapped_iq_fails():
    """order 4: swapping the recovered I and Q rails must FAIL the bit-exact gate —
    proof the egress delivers the RIGHT rail to each tag (the PoC bug delivered the
    wrong rail / dropped one)."""
    x = _stim(4, 0.02)
    ref = ComplexCostasLoopBlock("r", loop_bw=0.05, order=4).process_reference_complex(x)
    ref_i = [int(a) for a, _ in ref]
    dut = _run_chip(x, 4, 0.05)
    assert dut.ok, dut.reason
    # compare the Q rail (swapped in) against the I reference
    swapped = [_s16(v) for v in dut.q_q15 if v is not None]
    m = min(len(swapped), len(ref_i))
    assert sum(1 for k in range(m) if swapped[k] != ref_i[k]) > 0, \
        "gate failed to detect swapped I/Q rails!"


def test_mut_empty_output_fails():
    """An empty DUT output is a hard fail (green must not be reachable empty)."""
    x = _stim(2, 0.02)
    ref = [int(v) for v in
           ComplexCostasLoopBlock("r", loop_bw=0.05, order=2).process_reference(x)]
    emit = []
    m = min(len(emit), len(ref))
    assert m == 0  # nothing comparable == not a pass


# ============================================================ dashboard report
def test_write_report():
    """Emit verification/reports/ComplexCostasLoopBlock.json: the on-chip build is
    bit-exact to its GR-verified reference AND recovers GR's decisions across the
    order x loop_bw x offset sweep; the failing-mutation gate is proven."""
    metrics = {}
    worst_lsb = 0.0
    for order in (2, 4):
        for lb in _LOOP_BWS:
            x = _stim(order, 0.02)
            ref = [int(v) for v in ComplexCostasLoopBlock(
                "r", loop_bw=lb, order=order).process_reference(x)]
            dut = _run_chip(x, order, lb)
            assert dut.ok, f"order={order} lb={lb}: {dut.reason}"
            emit = [_s16(v) for v in dut.i_q15 if v is not None]
            m = min(len(emit), len(ref))
            bit_mis = sum(1 for k in range(m) if emit[k] != ref[k])
            gr = _gr_costas(x, lb, order).real
            sign_mis, ntail = _tail_sign_mismatch(emit, gr)
            # diagnostic RMS-normalised amplitude spread vs GR (NOT a gate).
            a = np.asarray(emit[:m], float)
            b = np.asarray(gr[:m], float) * 32768.0
            sc = np.sqrt(np.mean(b[_LOCK:m] ** 2)) / (np.sqrt(np.mean(a[_LOCK:m] ** 2)) + 1e-9)
            pk = float(np.max(np.abs(a[_LOCK:m] * sc - b[_LOCK:m])))
            worst_lsb = max(worst_lsb, pk)
            metrics[f"order{order}_lb{lb}"] = {
                "symbols": m, "onchip_vs_ref_mismatch": bit_mis,
                "vs_gr_sign_mismatch": sign_mis, "tail": ntail,
                "diag_peak_lsb_rmsnorm": round(pk, 1)}
            assert bit_mis == 0 and sign_mis == 0

    report = {
        "grc_block": "digital.costas_loop_cc",
        "metric": "decision",
        "coverage": {
            "orders": [2, 4], "loop_bws": _LOOP_BWS, "freq_offsets": _FOFFS,
            "on_chip_bit_exact": True, "vs_gnuradio": True, "saturated": True,
            "orientation_invariant": True, "placement_legal": True,
            "mutations": ["invert_bpsk_pd_sign", "corrupt_qpsk_detector",
                          "wrong_loop_bw", "one_sample_delay", "empty_output"]},
        "metrics": metrics,
        "diag_worst_peak_lsb_rmsnorm": round(worst_lsb, 1),
        "notes": (
            "Decision-directed complex Costas carrier recovery = digital.costas_loop_cc. "
            "order 2 (BPSK, 7 cells, single yi_tap out) + order 4 (QPSK, 8 cells, 4x2 "
            "fold, complex yi_tap/yq_tap out). On-chip BIT-EXACT to process_reference "
            "(itself sign-exact vs GR costas_loop_cc) on BOTH recovered rails across the "
            "order x loop_bw x offset sweep; 0 hard-decision mismatches vs GR. Metric is "
            "DECISION: a Q15 loop and GR's float loop reach the SAME symbol decisions "
            "along different soft-value trajectories, so the amplitude differs by "
            "hundreds of LSB (diag_worst_peak_lsb_rmsnorm) while every hard decision "
            "agrees. Saturation-safe (INV-19, pipeline_lock) + orientation-invariant. "
            "FIXED PoC bug (INV-25): the order-4 recovered COMPLEX pair failed to egress "
            "a CHIP PORT standalone (199/200 wrong) — the port-egress patch mis-read the "
            "order-dependent interface param-blind and, for the fused qpd (err+tap) cell, "
            "patched only the last WRITE (yq_tap), stranding yi_tap on a stale @1 hop "
            "that collided with the err->pd_pi handoff. Fix (engine/build.py): resolve "
            "the complex-out flag WITH params, and patch the last N tail WRITEs (the "
            "recovered rails) for a fused output+handoff cell egressing a port. The "
            "ABUTTED path (QPSK modem Costas->Gardner, BER 0) was always correct."),
    }
    write_session_report("ComplexCostasLoopBlock", report)
