# SPDX-License-Identifier: GPL-3.0-or-later
"""PIControllerBlock verification — the FOC current-loop PI with the 32-bit
anti-windup integrator, gated on the REAL placed + routed + built chip.

The block is placeKYT-native (no stock GNU Radio streaming counterpart), so the
golden is a pair of host references carried by the block itself:

* ``process_reference_q15`` — an EXACT integer model of the shipped arithmetic
  (MULQ truncation, bias-trick ASR, the full-precision 32-bit increment, the
  V-pinned add, strict clamps, the sign-gated wrapping 32-bit accumulate).
  Chip vs model is compared at tolerance ZERO everywhere.
* ``process_reference`` — a double-precision model of the IDENTICAL
  discretization (same operation order, same strict saturation points, same
  conditional-skip anti-windup) at the chip's quantized constants.

THE HEADLINE GATE (the reason this block exists — "is 16 bits enough for
FOC?"): the RESOLUTION gate drives a regime where ``Ki*e`` is a small fraction
of one Q15 LSB per step. The shipped 32-bit accumulator integrates it to the
double-precision value within 1 LSB; the 16-bit-only-accumulator mutant
(INV-4, built and run on the same chip) integrates EXACTLY NOTHING. That
difference is the measured answer, printed and recorded in the report.

INV-4: every structural mutant below is patched IN MEMORY (a
``build_cell_programs`` wrapper — never on disk, so no stale-pyc hazard), its
firing is FIRST proven in the mutated integer model on the same stimulus (a
mutant that cannot fire proves nothing), and the chip is then shown to (a)
diverge from the golden and (b) match the mutated model exactly — the failure
is the predicted one, not noise.

Run:
    QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_pi_controller.py -q
"""

from __future__ import annotations

import dataclasses
import os
import random
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PLACEKYT = Path(__file__).resolve().parents[2] / "placekyt"
_VERIFY = Path(__file__).resolve().parents[1]
_RUNTIME = Path(__file__).resolve().parents[2] / "runtime" / "python"
for p in (str(_PLACEKYT), str(_VERIFY), str(_RUNTIME)):
    if p not in sys.path:
        sys.path.insert(0, p)

from kyttar_verify import run_block_dut, D4_ORIENTATIONS, write_session_report  # noqa: E402
from kyttar_verify.dut_runner import run_block_dut_pipelined  # noqa: E402

from gr_kyttar.placement.blocks.pi_controller_block import (  # noqa: E402
    PIControllerBlock, _s16)
from gr_kyttar.placement.resolver import CellProgramResolver  # noqa: E402

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
pytestmark = pytest.mark.skipif(not os.path.exists(CHIP_YAML),
                                reason="chip yaml absent")

_DEFAULTS = {"kp": 0.25, "ki": 0.05, "limit": 0.5}


def _q15(f: float) -> int:
    return max(-32768, min(32767, int(round(f * 32768.0)))) & 0xFFFF


def _golden(params, stim):
    return PIControllerBlock("g", **params).process_reference_q15(stim)


def _run(params, stim, **kw):
    dut = run_block_dut("PIControllerBlock", stim, params=params,
                        chip_yaml=CHIP_YAML, **kw)
    assert dut.ok, dut.reason
    return dut


# ---------------------------------------------------------------------------
# The mutated-arithmetic integer model (mirrors process_reference_q15, with a
# variant switch). Used ONLY to PREDICT what a mutant should compute — the
# verdicts always compare the chip against the block's own golden.
# ---------------------------------------------------------------------------

def _model(params, stim, variant=None, lim_bump=0):
    """Returns (outputs, per-step (correct_acc_lo, mutant_acc_lo)) — the low
    words let the carry-loss test PROVE the sneaky half of INV-58: the mutant's
    LOW word stays bit-exact while the pair diverges."""
    b = PIControllerBlock("m", **params)
    kp_m, kp_s = _s16(b.kp_mantissa_q15), b.kp_shift
    ki_m, ki_s = _s16(b.ki_mantissa_q15), b.ki_shift
    lim = b.limit_q15 + lim_bump
    acc = 0            # the shipped 32-bit pair
    acch16 = 0         # the mutant's 16-bit-only accumulator
    accl16 = 0         # the carry-loss mutant's low word
    out, lows = [], []
    for w in stim:
        e = _s16(int(w) & 0xFFFF)
        p = _s16(((kp_m * e) >> 15) & 0xFFFF) >> kp_s
        inc = (2 * ki_m * e) >> ki_s
        inc_u = inc & 0xFFFFFFFF
        inc_hi = _s16((inc_u >> 16) & 0xFFFF)
        inc_lo = inc_u & 0xFFFF
        if variant in ("acc16", "carryloss"):
            iterm = acch16
        else:
            iterm = _s16((acc >> 16) & 0xFFFF)
        usum = p + iterm
        wrapped = _s16(usum & 0xFFFF)
        if usum != wrapped:
            case = "hi" if usum > 0 else "lo"
            u = lim if usum > 0 else -lim
        elif wrapped > lim:
            case, u = "hi", lim
        elif wrapped < -lim:
            case, u = "lo", -lim
        else:
            case, u = None, wrapped
        if variant == "nowindup" or case is None:
            do_int = True
        elif case == "hi":
            do_int = inc_hi < 0
        else:
            do_int = inc_hi >= 0
        if do_int:
            if variant == "acc16":
                acch16 = _s16((acch16 + inc_hi) & 0xFFFF)
            elif variant == "carryloss":
                accl16 = (accl16 + inc_lo) & 0xFFFF
                acch16 = _s16((acch16 + inc_hi) & 0xFFFF)
            else:
                acc = (acc + inc_u) & 0xFFFFFFFF
        lows.append((acc & 0xFFFF, accl16))
        out.append(u & 0xFFFF)
    return out, lows


def _run_mutant(params, stim, mutate):
    """Build + run with an IN-MEMORY program mutation (the Poly1305 pattern)."""
    orig = PIControllerBlock.build_cell_programs
    try:
        if mutate is not None:
            def patched(self):
                progs = orig(self)
                mutate(self, progs)
                return progs
            PIControllerBlock.build_cell_programs = patched
        dut = run_block_dut("PIControllerBlock", stim, params=params,
                            chip_yaml=CHIP_YAML)
    finally:
        PIControllerBlock.build_cell_programs = orig
    assert dut.ok, dut.reason
    return dut.outputs_q15


# The exact template fragments the structural mutants rewrite. Asserted present
# before each replacement so a refactor of the block cannot silently turn a
# mutant into a no-op (a mutant that no longer mutates certifies nothing).
_GATE_BLOCK = """\
sathi:
    OR R{in:hi}, R{in:hi}
    BR.N integ
    BR.NN post
satlo:
    OR R{in:hi}, R{in:hi}
    BR.N post
integ:
"""

_INTEG_BLOCK = """\
integ:
    ADD R{in:lo}, R{state:accl}
    MOVE R{state:accl}, R0
    ADC R{in:hi}, R{state:acch}
    MOVE R{state:acch}, R0
"""


# ---------------------------------------------------------------------------
# Exactness: chip == integer model, tolerance ZERO
# ---------------------------------------------------------------------------

EDGE = [0x0000, 0x7FFF, 0x8000, 0x8001, 0x4000, 0xC000, 0x0001, 0xFFFF]

_PARAM_SWEEP = [
    {"kp": 0.25, "ki": 0.05, "limit": 0.5},
    {"kp": 0.9, "ki": 0.5, "limit": 1.0},      # shift 0 / shift 0
    {"kp": 0.25, "ki": 0.25, "limit": 1.0},    # ki shift 1 (pass-through pair)
    {"kp": 0.0, "ki": 0.6, "limit": 1.0},      # pure I
    {"kp": 0.25, "ki": 0.0, "limit": 0.125},   # pure P, tight limit
    {"kp": -0.3, "ki": -0.02, "limit": 0.25},  # negative gains
    {"kp": 0.05, "ki": 0.001, "limit": 1.0},   # deep ki shift (s=9)
]


@pytest.mark.parametrize("params", _PARAM_SWEEP,
                         ids=[f"kp{p['kp']}_ki{p['ki']}_lim{p['limit']}"
                              for p in _PARAM_SWEEP])
def test_edge_vectors_exact(params):
    """Edge stimulus (0, +/-full-scale, the 0x8000 corner, +/-1 LSB) x the
    parameter sweep: the chip must equal the integer model BIT-EXACTLY."""
    stim = EDGE * 3
    dut = _run(params, stim)
    assert dut.outputs_q15 == _golden(params, stim)


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_random_exact(seed):
    rng = random.Random(seed)
    stim = [rng.randint(0, 0xFFFF) for _ in range(60)]
    params = {"kp": 0.35, "ki": 0.02, "limit": 0.6}
    dut = _run(params, stim)
    assert dut.outputs_q15 == _golden(params, stim)


def test_step_response_1000_exact_and_float_bound():
    """GATE 1 — step response, 1000 samples on the built chip.

    (a) EXACT against the integer model (tolerance 0, every sample).
    (b) Within a DERIVED bound of the double-precision reference of the
        identical discretization. The bound is derived, not tuned:
        1 LSB (MULQ floor on the p term) + 1 LSB (acc_hi floor on the integral
        read-out) + ceil(N * 2**-16) LSB (per-step increment truncation,
        N = 1000) + 1 LSB (one gate-boundary step at the rail) = 4 LSB.
    """
    params = {"kp": 0.25, "ki": 0.01, "limit": 0.9}
    stim = [_q15(0.2)] * 1000
    dut = _run(params, stim, jump_run=120000)
    want = _golden(params, stim)
    assert dut.outputs_q15 == want, "chip != integer model on the step"

    b = PIControllerBlock("f", **params)
    ref = b.process_reference([0.2] * 1000)
    errs = [abs(_s16(g) - r * 32768.0) for g, r in zip(dut.outputs_q15, ref)]
    peak = max(errs)
    print(f"\nstep response: peak |chip - float| = {peak:.3f} LSB "
          f"(derived bound 4)")
    assert peak <= 4.0, f"step response drifted {peak:.3f} LSB from the float golden"


def test_windup_release_exact_and_no_overshoot():
    """GATE 3 — anti-windup. Drive hard into +limit, release with a reversed
    error. Chip == integer model exactly; and against the float golden the chip
    must not out-overshoot it: it leaves the +limit rail no later than one
    sample after the golden and never exceeds the rail."""
    params = {"kp": 0.25, "ki": 0.05, "limit": 0.5}
    rel = 60
    stim = [_q15(0.9)] * rel + [_q15(-0.4)] * rel
    dut = _run(params, stim)
    want = _golden(params, stim)
    assert dut.outputs_q15 == want

    lim_q = PIControllerBlock("b", **params).limit_q15
    assert max(abs(_s16(w)) for w in dut.outputs_q15) <= lim_q, \
        "command exceeded the saturation bound"
    b = PIControllerBlock("f", **params)
    ref = b.process_reference([0.9] * rel + [-0.4] * rel)
    g_leave = next((i for i, r in enumerate(ref[rel:])
                    if r * 32768.0 < lim_q - 0.5), rel)
    c_leave = next((i for i, w in enumerate(dut.outputs_q15[rel:])
                    if _s16(w) < lim_q), rel)
    print(f"\nanti-windup: golden leaves +limit at release+{g_leave}, "
          f"chip at release+{c_leave}")
    assert c_leave <= g_leave + 1, \
        f"chip hung on the rail {c_leave - g_leave} samples past the golden (windup)"


# ---------------------------------------------------------------------------
# GATE 2 — THE RESOLUTION GATE (the 16-bit question, answered by measurement)
# ---------------------------------------------------------------------------

_RES_PARAMS = {"kp": 0.1, "ki": 0.001, "limit": 1.0}
_RES_E = 30                      # error = 30 LSB (~0.0009 FS)
_RES_N = 1200


def test_resolution_slow_ramp_integrates():
    """Ki*e here is ~0.03 LSB per step — far below one Q15 LSB, the regime
    where a 16-bit integrator silently loses everything. The shipped 32-bit
    accumulator must (a) equal the integer model exactly and (b) track the
    double-precision reference within DERIVED bounds (derived, not tuned):

    * INTEGRAL drift (the end-to-end command growth, where the constant
      p-term floor cancels): < 1 LSB (the acc_hi read-out floor)
      + N * 2**-16 LSB (per-step increment truncation) = 1.02 LSB at N=1200.
    * command-level drift: the integral drift + 1 LSB more (MULQ+ASR compose
      to a single floor of the exact Kp*e product) = 2.02 LSB.

    The measured drift is printed (and recorded in the report)."""
    stim = [_RES_E] * _RES_N
    dut = _run(_RES_PARAMS, stim, jump_run=120000)
    want = _golden(_RES_PARAMS, stim)
    assert dut.outputs_q15 == want, "chip != integer model on the slow ramp"

    b = PIControllerBlock("f", **_RES_PARAMS)
    ref = b.process_reference([_RES_E / 32768.0] * _RES_N)
    growth = _s16(dut.outputs_q15[-1]) - _s16(dut.outputs_q15[0])
    ref_growth = (ref[-1] - ref[0]) * 32768.0
    int_drift = abs(growth - ref_growth)
    u_drift = abs(_s16(dut.outputs_q15[-1]) - ref[-1] * 32768.0)
    int_bound = 1.0 + _RES_N / 65536.0
    print(f"\nresolution: Ki*e = {b.ki_effective * _RES_E:.4f} LSB/step, "
          f"{_RES_N} steps; chip integral growth = {growth} LSB "
          f"(double: {ref_growth:.2f}); integral drift = {int_drift:.3f} LSB "
          f"(bound {int_bound:.2f}), command drift = {u_drift:.3f} LSB "
          f"(bound {int_bound + 1:.2f})")
    assert growth > 0, "sub-LSB increments did not integrate at all"
    assert int_drift <= int_bound, \
        f"slow-ramp INTEGRAL drifted {int_drift:.3f} LSB from double"
    assert u_drift <= int_bound + 1.0, \
        f"slow-ramp command drifted {u_drift:.3f} LSB from double"


def test_mutant_16bit_accumulator_fails_the_resolution_gate():
    """THE ARGUMENT, demonstrated: a 16-bit-only accumulator (the naive Q15
    integrator) built and run on the same chip integrates NOTHING on the slow
    ramp — its integral term stays exactly 0 for the whole run — while the
    shipped 32-bit pair (gated above) tracks the double-precision value. The
    mutant is first proven to fire in the model, and the chip is required to
    match the mutated model exactly (the failure is the predicted one)."""
    stim = [_RES_E] * 300
    correct, _ = _model(_RES_PARAMS, stim)
    pred, _ = _model(_RES_PARAMS, stim, variant="acc16")
    assert pred != correct, "the 16-bit mutant cannot fire on this stimulus"

    def m_acc16(self, progs):
        t = progs["acc"].assembly_template
        assert _INTEG_BLOCK in t, "integ block not found — mutant would be a no-op"
        progs["acc"].assembly_template = t.replace(_INTEG_BLOCK, """\
integ:
    ADD R{in:hi}, R{state:acch}
    MOVE R{state:acch}, R0
""")

    got = _run_mutant(_RES_PARAMS, stim, m_acc16)
    assert got != correct, "the 16-bit accumulator went UNDETECTED by the gate!"
    assert got == pred, "the mutant failed differently than predicted"
    # The whole 16-bit failure, quantified: the mutant's integral term is zero
    # at every sample (u == P-term only, constant), the golden's grows.
    g_growth = _s16(correct[-1]) - _s16(correct[0])
    m_growth = _s16(got[-1]) - _s16(got[0])
    print(f"\n16-bit mutant: integral growth {m_growth} LSB vs golden "
          f"{g_growth} LSB over {len(stim)} steps")
    assert m_growth == 0
    assert g_growth > 0


# ---------------------------------------------------------------------------
# INV-4 — the other structural mutants (each proven able to fire, on chip)
# ---------------------------------------------------------------------------

def test_mutant_low_half_first_carry_loss_fails():
    """INV-58's sneaky shape: replace the ADC with a carry-blind ADD. The
    mutant's LOW accumulator word stays BIT-EXACT for the entire run (asserted
    in the model — this is exactly why a one-word gate cannot see it), yet the
    integral (the high word) misses every carry and the output diverges. The
    chip must diverge from the golden and match the mutated model."""
    params = {"kp": 0.1, "ki": 0.4, "limit": 1.0}
    rng = random.Random(3)
    stim = [rng.randint(0, 0xFFFF) for _ in range(120)]
    correct, lows = _model(params, stim)
    pred, lows_m = _model(params, stim, variant="carryloss")
    assert pred != correct, "the carry-loss mutant cannot fire on this stimulus"
    # the full-pair assertion: low word identical throughout, pair still wrong
    assert all(a == b for (a, _), (_, b) in zip(lows, lows_m)), \
        "carry-loss changed the LOW word — the mutant is not the sneaky class"

    def m_carry(self, progs):
        t = progs["acc"].assembly_template
        assert "    ADC R{in:hi}, R{state:acch}\n" in t
        progs["acc"].assembly_template = t.replace(
            "    ADC R{in:hi}, R{state:acch}\n",
            "    ADD R{in:hi}, R{state:acch}\n")

    got = _run_mutant(params, stim, m_carry)
    assert got != correct, "carry loss went UNDETECTED by the gate!"
    assert got == pred, "the mutant failed differently than predicted"


def test_mutant_windup_clamp_removed_fails():
    """Remove the anti-windup gate (both saturation entries fall straight into
    the integrate path). The integrator winds up during saturation and the
    command hangs on the +limit rail after the error reverses — the overshoot
    gate must catch it."""
    params = {"kp": 0.25, "ki": 0.05, "limit": 0.5}
    rel = 60
    stim = [_q15(0.9)] * rel + [_q15(-0.4)] * rel
    correct, _ = _model(params, stim)
    pred, _ = _model(params, stim, variant="nowindup")
    assert pred != correct, "the no-windup mutant cannot fire on this stimulus"

    def m_nowindup(self, progs):
        t = progs["acc"].assembly_template
        assert _GATE_BLOCK in t, "gate block not found — mutant would be a no-op"
        progs["acc"].assembly_template = t.replace(
            _GATE_BLOCK, "sathi:\nsatlo:\ninteg:\n")

    got = _run_mutant(params, stim, m_nowindup)
    assert got != correct, "removed windup clamp went UNDETECTED by the gate!"
    assert got == pred, "the mutant failed differently than predicted"
    # quantify the hang (measured: golden leaves the rail immediately)
    lim_q = 16384
    g_leave = next(i for i, w in enumerate(correct[rel:]) if _s16(w) < lim_q)
    m_leave = next(i for i, w in enumerate(got[rel:]) if _s16(w) < lim_q)
    print(f"\nno-windup mutant hangs at +limit for {m_leave} post-release "
          f"samples (golden: {g_leave})")
    assert m_leave > g_leave


def test_mutant_saturation_bound_off_by_one_fails():
    """A +/-1 error in the stored limit word must be caught by the exact gate
    (the clamped samples emit limit+1)."""
    params = {"kp": 0.25, "ki": 0.05, "limit": 0.5}
    stim = [_q15(0.9)] * 30
    correct, _ = _model(params, stim)
    pred, _ = _model(params, stim, lim_bump=1)
    assert pred != correct, "the off-by-one mutant cannot fire on this stimulus"

    def m_offby1(self, progs):
        data = progs["sat"].data
        assert any(dw.name == "lim" for dw in data)
        progs["sat"].data = [
            dataclasses.replace(dw, value=(dw.value + 1) & 0xFFFF)
            if dw.name == "lim" else dw for dw in data]

    got = _run_mutant(params, stim, m_offby1)
    assert got != correct, "an off-by-one saturation bound went UNDETECTED!"
    assert got == pred, "the mutant failed differently than predicted"


# --- the classic template mutations (data-path-independent gate teeth) -------

def test_mutation_inverted_output_fails():
    stim = EDGE * 2
    dut = _run(_DEFAULTS, stim)
    want = _golden(_DEFAULTS, stim)
    mutated = [(0x10000 - (w or 0)) & 0xFFFF for w in dut.outputs_q15]
    assert mutated != want, "inversion went undetected"


def test_mutation_one_sample_offset_fails():
    stim = EDGE * 2
    dut = _run(_DEFAULTS, stim)
    want = _golden(_DEFAULTS, stim)
    shifted = [0x0000] + list(dut.outputs_q15[:-1])
    assert shifted != want, "a +1-sample delay went undetected"


def test_mutation_wrong_param_fails():
    stim = EDGE * 2
    dut = _run({"kp": 0.5, "ki": 0.05, "limit": 0.5}, stim)
    want = _golden(_DEFAULTS, stim)   # golden for kp=0.25
    assert dut.outputs_q15 != want, "a wrong-kp DUT went undetected"


def test_empty_output_fails():
    want = _golden(_DEFAULTS, EDGE)
    assert [] != want


# ---------------------------------------------------------------------------
# INV-19 — saturated (pipelined) drive == per-sample; INV-23 — orientation
# ---------------------------------------------------------------------------

def test_saturated_drive_equals_per_sample():
    """The whole burst enqueued back-to-back (queue_words_physical, one
    continuous run) — the serialize-LOCK must hold the loop exact under a full
    pipeline, on a stimulus that actually exercises the saturation rails and
    the anti-windup skip. (The catalog-wide REAL_1IN sweep also covers this
    block; this is the saturation-heavy bespoke version.)"""
    params = {"kp": 0.25, "ki": 0.05, "limit": 0.5}
    stim = [_q15(v) for v in (0.9, 0.9, 0.9, -0.5, 0.2, -0.9, -0.9, 0.7,
                              0.1, -0.2, 0.9, 0.9, -0.1, 0.4, -0.6, 0.05)]
    want = _golden(params, stim)
    pipe = run_block_dut_pipelined("PIControllerBlock", [(w,) for w in stim],
                                   params=params, chip_yaml=CHIP_YAML)
    # INV-56: on a saturated-drive failure the reason carries stop_reason —
    # surface it verbatim rather than a bare boolean.
    assert pipe.ok, f"saturated drive did not complete: {pipe.reason}"
    assert pipe.outputs_q15 == want, \
        "saturated (pipelined) output diverged from the per-sample golden"


@pytest.mark.parametrize("orient", D4_ORIENTATIONS,
                         ids=["identity" if not o else "+".join(o)
                              for o in D4_ORIENTATIONS])
def test_orientation_invariant(orient):
    """INV-23: identical on-chip output in all 8 D4 orientations (every face
    word is is_face=True; the lock face, the feedback face and the dual-face
    tap must all transform rigidly)."""
    params = {"kp": 0.25, "ki": 0.05, "limit": 0.5}
    stim = [_q15(v) for v in (0.9, 0.9, -0.5, 0.2, -0.9, 0.7, 0.1, -0.2,
                              0.9, -0.1, 0.4, -0.6)]
    want = _golden(params, stim)
    dut = _run(params, stim, orient=list(orient))
    assert dut.outputs_q15 == want, \
        f"orientation {'+'.join(orient) or 'identity'} diverged"


# ---------------------------------------------------------------------------
# Static structure gates (INV-33) + parameter validation (INV-0)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("params", _PARAM_SWEEP,
                         ids=[f"kp{p['kp']}_ki{p['ki']}_lim{p['limit']}"
                              for p in _PARAM_SWEEP])
def test_word_budget_and_register_overlap(params):
    """INV-33 static gate: for every cell, at every parameter corner, no data /
    state / input address reaches the instruction range (31 - instr_count)."""
    b = PIControllerBlock("w", **params)
    r = CellProgramResolver()
    for cid, cp in b.build_cell_programs().items():
        n = r.count_instructions(cp)
        base = 31 - n
        addrs = (list(r._allocate_data(cp.data).values())
                 + list(r.compute_state_registers(cp).values())
                 + [p_.register for p_ in cp.inputs if p_.register is not None])
        assert max(addrs) < base, \
            f"cell {cid}: address {max(addrs)} inside instruction range " \
            f"(base {base}, {n} instrs)"
        for ep, addr in r.compute_entry_addresses(cp).items():
            assert base <= addr <= 30, f"cell {cid} entry {ep} at {addr}"


def test_dispatch_entries_all_targeted():
    """INV-39: every declared acc entry is the target of a declared internal
    jump (an entry nothing jumps at is dead code only the chip can reveal)."""
    b = PIControllerBlock("j", **_DEFAULTS)
    targeted = {(dst, entry) for (_s_, _j, dst, entry) in b.internal_jumps()}
    for cid, cp in b.build_cell_programs().items():
        for ep in cp.entries:
            if ep.name == "default" and cid == "front":
                continue  # the block's external entry (the host jumps it)
            assert (cid, ep.name) in targeted or ep.name == "default", \
                f"entry {cid}:{ep.name} is targeted by no internal jump"


def test_hardware_limits_raise():
    """INV-0: out-of-range params RAISE loudly, never silently clamp."""
    for bad in ({"kp": 1.0}, {"kp": -1.5}, {"ki": 1.0}, {"limit": 0.0},
                {"limit": 1.5}, {"limit": -0.5}, {"ki": 1e-12}):
        params = dict(_DEFAULTS)
        params.update(bad)
        with pytest.raises(ValueError):
            PIControllerBlock("x", **params)


def test_effective_gains_match_mantissa_shift():
    """The derived mantissa/shift pair reproduces the requested gain to within
    the mantissa's own quantization (half an LSB of the normalized mantissa)."""
    for kp, ki in ((0.25, 0.01), (0.9, 0.5), (-0.3, -0.001), (0.05, 0.001)):
        b = PIControllerBlock("q", kp=kp, ki=ki, limit=1.0)
        for want, got, s in ((kp, b.kp_effective, b.kp_shift),
                             (ki, b.ki_effective, b.ki_shift)):
            tol = (2.0 ** -s) / 65536.0
            assert abs(want - got) <= tol, (want, got, s)


# ---------------------------------------------------------------------------
# Report (INV-38: via the session writer ONLY; runs last)
# ---------------------------------------------------------------------------

def test_zz_emit_report():
    """Assemble the report from MEASURED results of a fresh chip run (never
    literals) and hand it to the session writer, which refuses to write if any
    gate in this file failed."""
    # re-measure the two headline numbers for the report body
    stim = [_RES_E] * _RES_N
    dut = _run(_RES_PARAMS, stim, jump_run=120000)
    want = _golden(_RES_PARAMS, stim)
    assert dut.outputs_q15 == want
    b = PIControllerBlock("f", **_RES_PARAMS)
    ref = b.process_reference([_RES_E / 32768.0] * _RES_N)
    drift = float(abs(_s16(dut.outputs_q15[-1]) - ref[-1] * 32768.0))
    growth = int(_s16(dut.outputs_q15[-1]) - _s16(dut.outputs_q15[0]))
    mut, _ = _model(_RES_PARAMS, stim, variant="acc16")
    mut_growth = _s16(mut[-1]) - _s16(mut[0])
    report = {
        "metric": "exact",
        "n_compared": len(want),
        "max_abs_err": 0,
        "tolerance": 0,
        "resolution_gate": {
            "ki_e_lsb_per_step": round(b.ki_effective * _RES_E, 5),
            "steps": _RES_N,
            "chip_integral_growth_lsb": growth,
            "end_drift_vs_double_lsb": round(drift, 4),
            "acc16_mutant_growth_lsb": mut_growth,
        },
        "coverage": {
            "edge": True, "random": 3, "param_sweep": len(_PARAM_SWEEP),
            "step_1000": True, "resolution_slow_ramp": True,
            "anti_windup": True, "saturated_pipelined": True,
            "orientation_8d4": True, "mutation": 8,
            "structural_mutants_on_chip": 4,
        },
    }
    write_session_report("PIControllerBlock", report)
