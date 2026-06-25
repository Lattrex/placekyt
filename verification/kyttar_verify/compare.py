# SPDX-License-Identifier: GPL-3.0-or-later
"""compare_against_grc — the comparison engine for block verification.

Compares a Kyttar block's simKYT output (DUT) against its GNU Radio reference
(predictor) and returns a hard pass/fail plus a diagnostic record.

Design rules (from the reviewed verification plan, §6):
  * Alignment is by a PREDICTED group delay, not a free cross-correlation lag
    search — a free search hides latency/off-by-one bugs (it slides the streams
    until they look right). The caller states the expected delay; the engine
    verifies the DUT actually exhibits it.
  * Tolerance is bounded by a DERIVED Q15 quantization floor (worst-case, from
    the block's op count), not a hand-tuned magic number, and Q15 SATURATION is
    modeled on the float reference before comparing so full-scale edge vectors
    do not false-fail.
  * Per-metric class dispatch: amplitude blocks gate on max-abs-error; decision
    blocks gate on bit/symbol exactness; deterministic integer streams gate on
    exact match. NMSE is reported for diagnosis but never the sole gate, and is
    skipped when the reference signal power is near zero.
  * NaN/Inf in either stream is a hard fail. Empty / length-mismatched / too-short
    comparisons are a hard fail — "green" must not be reachable by producing
    nothing.

The engine is INTENTIONALLY conservative: when in doubt it fails. A loose gate is
worse than no gate, because the whole value proposition is drop-in equivalence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import numpy as np


class Metric(Enum):
    """How DUT and reference are compared for a given block class."""

    AMPLITUDE = "amplitude"   # filters/mixers/gain: max-abs Q15 error within floor
    EXACT = "exact"           # deterministic int streams: bit-for-bit
    DECISION = "decision"     # slicers/demods: bit/symbol error rate


def _to_signed(words) -> np.ndarray:
    """uint16 Q15 words -> signed int array. ``None`` entries become a sentinel
    that forces a length/empty failure rather than a silent skip."""
    out = []
    for w in words:
        if w is None:
            out.append(None)
        else:
            v = int(w) & 0xFFFF
            out.append(v - 0x10000 if v >= 0x8000 else v)
    return out


@dataclass
class CompareResult:
    """Outcome of one DUT-vs-reference comparison."""

    passed: bool
    metric: Metric
    n_compared: int = 0
    max_abs_err: int = 0          # Q15 LSB
    tolerance: int = 0            # Q15 LSB bound used (amplitude metric)
    nmse_db: float = float("nan")  # diagnostic only
    correlation: float = float("nan")  # diagnostic only
    bit_errors: int = 0           # decision metric
    delay_used: int = 0
    reason: str = ""              # populated on failure
    worst: list = field(default_factory=list)  # [(idx, dut, ref, err)] worst few

    def summary(self) -> str:
        if self.metric is Metric.DECISION:
            core = f"bit_errors={self.bit_errors}/{self.n_compared}"
        else:
            core = (f"max_abs_err={self.max_abs_err} LSB (tol {self.tolerance}), "
                    f"NMSE={self.nmse_db:.1f} dB, corr={self.correlation:.4f}")
        head = "PASS" if self.passed else "FAIL"
        tail = f" — {self.reason}" if self.reason else ""
        return f"[{head}] {self.metric.value}: n={self.n_compared}, {core}{tail}"


def write_report(kyttar_block: str, result: "CompareResult", *,
                 coverage: dict | None = None,
                 reports_dir: str | Path | None = None) -> Path:
    """Persist a block's verification result as the JSON the dashboard reads.

    ``coverage`` records which stimulus families ran (e.g.
    ``{"edge": True, "random": 3, "param_sweep": 4, "mutation": True}``) so the
    dashboard can show coverage at a glance. Written to
    ``verification/reports/<KyttarBlock>.json`` by default.
    """
    out_dir = Path(reports_dir) if reports_dir else (
        Path(__file__).resolve().parents[1] / "reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    rec = {
        "kyttar_block": kyttar_block,
        "passed": bool(result.passed),
        "metric": result.metric.value,
        "n_compared": result.n_compared,
        "max_abs_err": result.max_abs_err,
        "tolerance": result.tolerance,
        "nmse_db": (None if result.nmse_db != result.nmse_db else result.nmse_db),
        "correlation": (None if result.correlation != result.correlation
                        else result.correlation),
        "bit_errors": result.bit_errors,
        "delay_used": result.delay_used,
        "coverage": coverage or {},
    }
    path = out_dir / f"{kyttar_block}.json"
    path.write_text(json.dumps(rec, indent=2) + "\n")
    return path


def q15_quant_floor(op_count: int, head_shift: int = 0) -> int:
    """Worst-case Q15 quantization bound (LSB) for a feed-forward block.

    Each Q15 multiply/MAC truncation contributes up to ~1 LSB; ``op_count`` such
    operations accumulate worst-case-linearly, plus one final-quantization LSB.

    ``head_shift`` (S) accounts for COEFFICIENT-HEADROOM blocks (INV-13): a MAC
    chain whose Σ|coeff| > 1 pre-scales every coefficient by ``2^-S`` so the
    accumulator cannot overflow, then restores the gain with a saturating shift.
    That scaling costs precision — a coefficient stored as ``round(c·2^15/2^S)``
    and reconstructed as ``q·2^S/2^15`` carries up to ``2^(S-1)`` LSB of effective
    coefficient error (vs 0.5 LSB at S=0), so each of the ``op_count`` taps can
    contribute up to ``2^(S-1)`` coeff-error LSB ON TOP of its ~1 LSB MAC
    truncation. Hence the bound is ``op_count·(2^(S-1)+1) + 1`` for S>0, and the
    plain ``op_count+1`` for S=0 (the no-headroom case — a no-op, unchanged). This
    is a DERIVED fixed-point worst case, not a tuned number; verified empirically
    to bound the dc_blocker (a headroom FIR) with ~18% margin.

    Valid ONLY for feed-forward MAC blocks (a single MULQ, an N-tap FIR/MAC
    chain, with or without coefficient headroom). Recursive (IIR) and
    feedback/loop blocks do NOT obey an op-count bound — their error depends on
    pole radius / trajectory divergence; those classes must pass an explicit
    ``tolerance`` and use a behavioral metric, never this floor.
    """
    n = int(op_count)
    if head_shift and head_shift > 0:
        return n * ((1 << (head_shift - 1)) + 1) + 1
    return max(1, n + 1)


def _saturate_ref_q15(ref_floats) -> np.ndarray:
    """Clip the float reference to the Q15 representable range and quantize.

    Models the DUT's hardware saturation so a full-scale input (where float GR
    keeps growing but Q15 clips) does not produce a spurious large error.
    """
    out = []
    for v in ref_floats:
        q = int(round(float(v) * 32768.0))
        q = max(-32768, min(32767, q))   # already signed; this IS the Q15 value
        out.append(q)
    return np.asarray(out, dtype=np.int64)


def compare_against_grc(
    dut_q15: list,
    ref_floats: list,
    *,
    metric: Metric = Metric.AMPLITUDE,
    delay: int = 0,
    op_count: int = 1,
    head_shift: int = 0,
    tolerance: int | None = None,
    min_samples: int = 4,
    polarity_dont_care: bool = False,
) -> CompareResult:
    """Compare a DUT output against a GNU Radio float reference.

    Args:
        dut_q15: DUT output as uint16 Q15 words (``None`` = a missing output).
        ref_floats: the GNU Radio float reference (same stimulus).
        metric: comparison class (see :class:`Metric`).
        delay: the PREDICTED group delay of the block in samples — the DUT output
            ``y[n]`` is compared to reference ``x[n-delay]``. Stated by the
            caller; the engine does not search for it.
        op_count: number of Q15 multiply/MAC ops (for the derived amplitude floor).
        head_shift: coefficient-headroom shift S of the block (INV-13); widens the
            derived amplitude floor to account for the precision lost to scaling
            the coefficients by 2^-S. 0 (default) for non-headroom blocks.
        tolerance: explicit Q15 LSB bound; overrides the derived floor. Required
            for recursive/loop blocks.
        min_samples: fail if fewer than this many samples can be compared.
        polarity_dont_care: allow a global sign flip (only where the block's
            contract says absolute polarity is don't-care, e.g. some carrier
            loops). Off by default — an inverted output is normally a real bug.

    Returns:
        :class:`CompareResult`. ``passed`` is the hard gate.
    """
    dut = _to_signed(dut_q15)

    # --- hard structural failures (no silent skips) -------------------------
    if not dut:
        return CompareResult(False, metric, reason="DUT produced no output")
    if any(d is None for d in dut):
        n_missing = sum(1 for d in dut if d is None)
        return CompareResult(False, metric,
                             reason=f"{n_missing} DUT outputs missing (no egress)")
    if not ref_floats:
        return CompareResult(False, metric, reason="empty reference")

    dut_arr = np.asarray(dut, dtype=np.int64)
    if not np.all(np.isfinite(np.asarray(ref_floats, dtype=float))):
        return CompareResult(False, metric, reason="NaN/Inf in reference")

    ref_q15 = _saturate_ref_q15(ref_floats)

    # --- align by the PREDICTED delay --------------------------------------
    # y[n] corresponds to ref[n-delay]; drop the first `delay` ref samples and
    # the trailing DUT samples that have no reference.
    if delay < 0:
        return CompareResult(False, metric, reason=f"negative delay {delay}")
    ref_al = ref_q15[delay:]
    n = min(len(dut_arr), len(ref_al))
    if n < min_samples:
        return CompareResult(
            False, metric, n_compared=n, delay_used=delay,
            reason=f"only {n} samples comparable (< min {min_samples}); "
                   f"delay={delay} may be wrong or DUT truncated")
    a = dut_arr[:n].astype(np.int64)
    b = ref_al[:n].astype(np.int64)

    if polarity_dont_care:
        # choose the polarity with the smaller max error (contract-gated only)
        if np.max(np.abs(a - b)) > np.max(np.abs(a + b)):
            b = -b

    # --- diagnostics (always computed) -------------------------------------
    err = a - b
    max_abs = int(np.max(np.abs(err))) if n else 0
    sig_pow = float(np.mean(b.astype(float) ** 2))
    err_pow = float(np.mean(err.astype(float) ** 2))
    nmse_db = (10.0 * np.log10(err_pow / sig_pow)
               if sig_pow > 1.0 and err_pow > 0 else float("nan"))
    corr = (float(np.corrcoef(a, b)[0, 1])
            if n > 1 and np.std(a) > 0 and np.std(b) > 0 else float("nan"))
    worst_idx = np.argsort(-np.abs(err))[:5]
    worst = [(int(i), int(a[i]), int(b[i]), int(err[i])) for i in worst_idx]

    res = CompareResult(
        passed=False, metric=metric, n_compared=n, max_abs_err=max_abs,
        nmse_db=nmse_db, correlation=corr, delay_used=delay, worst=worst)

    # --- the gate, per metric class ----------------------------------------
    if metric is Metric.EXACT:
        res.passed = bool(np.array_equal(a, b))
        if not res.passed:
            res.reason = f"{int(np.count_nonzero(err))} of {n} samples differ"
        return res

    if metric is Metric.DECISION:
        # bits/symbols: compare the low bit (slicer decisions are packed LSB).
        ad = a & 1
        bd = b & 1
        be = int(np.count_nonzero(ad != bd))
        res.bit_errors = be
        res.passed = (be == 0)
        if not res.passed:
            res.reason = f"{be}/{n} bit errors"
        return res

    # AMPLITUDE: max-abs within the derived (or explicit) Q15 floor.
    tol = (tolerance if tolerance is not None
           else q15_quant_floor(op_count, head_shift))
    res.tolerance = tol
    res.passed = (max_abs <= tol)
    if not res.passed:
        res.reason = (f"max_abs_err {max_abs} LSB exceeds tolerance {tol} "
                      f"(worst: {worst[:3]})")
    return res


# =============================================================================
# Complex (I/Q) and LLR (soft-decision) comparison
# =============================================================================

@dataclass
class ComplexCompareResult:
    """Outcome of a complex (I/Q) DUT-vs-reference comparison.

    A complex block passes ONLY if BOTH channels pass — an I-only check would miss
    a swapped or sign-flipped Q. Each channel is gated by the same per-channel
    amplitude metric and derived Q15 floor as the real path (so I/Q parity is held
    to the same quantization bound, not a looser one)."""

    passed: bool
    i: CompareResult
    q: CompareResult
    reason: str = ""

    def summary(self) -> str:
        head = "PASS" if self.passed else "FAIL"
        tail = f" — {self.reason}" if self.reason else ""
        return (f"[{head}] complex: I={self.i.summary()} | "
                f"Q={self.q.summary()}{tail}")


def compare_complex_against_grc(
    dut_i_q15: list,
    dut_q_q15: list,
    ref_i_floats: list,
    ref_q_floats: list,
    *,
    metric: Metric = Metric.AMPLITUDE,
    delay: int = 0,
    op_count: int = 1,
    head_shift: int = 0,
    tolerance: int | None = None,
    min_samples: int = 4,
) -> ComplexCompareResult:
    """Compare a COMPLEX DUT output (I and Q channels) against a complex GR
    reference. Each channel is run through :func:`compare_against_grc` with the
    SAME predicted delay / op-count / derived tolerance; the block passes only if
    BOTH channels pass.

    A swapped I/Q, a negated Q, or a Q-channel latency error all fail here — the
    I channel alone would not catch them, which is why both are gated. The two
    channels share one group delay (a complex matched filter / mixer delays I and
    Q identically), so a single ``delay`` is stated for both."""
    ri = compare_against_grc(
        dut_i_q15, ref_i_floats, metric=metric, delay=delay,
        op_count=op_count, head_shift=head_shift, tolerance=tolerance,
        min_samples=min_samples)
    rq = compare_against_grc(
        dut_q_q15, ref_q_floats, metric=metric, delay=delay,
        op_count=op_count, head_shift=head_shift, tolerance=tolerance,
        min_samples=min_samples)
    passed = ri.passed and rq.passed
    reason = ""
    if not passed:
        bad = []
        if not ri.passed:
            bad.append(f"I: {ri.reason}")
        if not rq.passed:
            bad.append(f"Q: {rq.reason}")
        reason = "; ".join(bad)
    return ComplexCompareResult(passed=passed, i=ri, q=rq, reason=reason)


@dataclass
class LLRCompareResult:
    """Outcome of a SOFT-DECISION (LLR) DUT-vs-reference comparison.

    Soft (LLR) outputs are NOT bit-exact: a Q15 fixed-point LLR carries
    quantization + scaling error vs the GR float LLR. But the SIGN of an LLR is
    the hard decision the FEC decoder acts on, so sign agreement must be (near)
    perfect even where the magnitudes differ. This result therefore gates on BOTH:

      * ``magnitude`` — the soft values within a derived Q15 tolerance (after the
        block's known LLR scaling is applied to the reference), and
      * ``sign_mismatches`` — the count of samples whose hard decision (sign)
        disagrees. A sign flip is a wrong bit; near the decision boundary
        (|ref| ~ 0) a flip is quantization-benign and is EXCLUDED from the count
        via a small dead-zone.
    """

    passed: bool
    n_compared: int = 0
    sign_mismatches: int = 0
    max_abs_err: int = 0
    tolerance: int = 0
    correlation: float = float("nan")
    delay_used: int = 0
    reason: str = ""

    def summary(self) -> str:
        head = "PASS" if self.passed else "FAIL"
        tail = f" — {self.reason}" if self.reason else ""
        return (f"[{head}] llr: n={self.n_compared}, "
                f"sign_mismatch={self.sign_mismatches}, "
                f"max_abs_err={self.max_abs_err} LSB (tol {self.tolerance}), "
                f"corr={self.correlation:.4f}{tail}")


def compare_llr_against_grc(
    dut_q15: list,
    ref_floats: list,
    *,
    delay: int = 0,
    tolerance: int | None = None,
    op_count: int = 1,
    llr_scale: float = 1.0,
    sign_dead_zone: float = 0.02,
    max_sign_mismatch: int = 0,
    min_samples: int = 4,
) -> LLRCompareResult:
    """Compare a soft-decision (LLR) DUT output against a GR float LLR reference.

    The decision-relevant property is the SIGN (the hard bit); the magnitude is a
    confidence the decoder weights. So this gate has two parts:

      1. SIGN AGREEMENT — every sample's sign must match the reference's, EXCEPT
         where the reference LLR is within ``sign_dead_zone`` of zero (a
         decision-boundary sample where a flip is pure quantization noise, not a
         real bit error). At most ``max_sign_mismatch`` (default 0) mismatches are
         allowed outside the dead zone.
      2. MAGNITUDE — the soft values within a derived Q15 tolerance. The GR float
         LLR is first scaled by ``llr_scale`` (the block's LLR coefficient maps
         the theoretical 2I/σ² LLR into the Q15-representable range; the reference
         must be scaled the same way before diffing), then Q15-saturated.

    Args:
        dut_q15: DUT LLR output as uint16 Q15 words (``None`` = missing).
        ref_floats: the GR float LLR reference (same stimulus, theoretical scale).
        delay: predicted group delay (memoryless demod = 0).
        tolerance: explicit Q15 LSB magnitude bound; derived from ``op_count`` if
            None (a single MULQ = ~1 LSB).
        op_count: Q15 op count for the derived magnitude floor.
        llr_scale: scale applied to ``ref_floats`` so its range matches the DUT's
            Q15-clamped LLR (the block's LLR coefficient, e.g. 0.5 for the BPSK
            soft demod).
        sign_dead_zone: |ref·llr_scale| below this is a boundary sample, excluded
            from the sign count.
        max_sign_mismatch: tolerated sign mismatches outside the dead zone.
        min_samples: fail if fewer than this many samples are comparable.
    """
    dut = _to_signed(dut_q15)
    if not dut:
        return LLRCompareResult(False, reason="DUT produced no output")
    if any(d is None for d in dut):
        n_missing = sum(1 for d in dut if d is None)
        return LLRCompareResult(
            False, reason=f"{n_missing} DUT outputs missing (no egress)")
    if not ref_floats:
        return LLRCompareResult(False, reason="empty reference")
    if delay < 0:
        return LLRCompareResult(False, reason=f"negative delay {delay}")
    ref_arr = np.asarray(ref_floats, dtype=float)
    if not np.all(np.isfinite(ref_arr)):
        return LLRCompareResult(False, reason="NaN/Inf in reference")

    # Scale the reference LLR to the DUT's representable range, then Q15-saturate.
    ref_scaled = ref_arr * float(llr_scale)
    ref_q15 = _saturate_ref_q15(ref_scaled)

    dut_arr = np.asarray(dut, dtype=np.int64)
    ref_al = ref_q15[delay:]
    n = min(len(dut_arr), len(ref_al))
    if n < min_samples:
        return LLRCompareResult(
            False, n_compared=n, delay_used=delay,
            reason=f"only {n} samples comparable (< min {min_samples})")
    a = dut_arr[:n].astype(np.int64)
    b = ref_al[:n].astype(np.int64)
    ref_floats_al = ref_scaled[delay:delay + n]

    # 1) sign agreement outside the dead zone (a sign = the hard bit decision).
    # ``ref_floats_al`` is the SCALED reference LLR in float units ([-1, 1)-ish, the
    # same scale as a Q15 value / 32768), so the dead zone is a direct float
    # threshold — NOT multiplied by 32768.
    confident = np.abs(ref_floats_al) >= sign_dead_zone
    sign_a = np.sign(a)
    sign_b = np.sign(b)
    mism = int(np.count_nonzero((sign_a != sign_b) & confident))

    # 2) magnitude within the derived Q15 floor
    err = a - b
    max_abs = int(np.max(np.abs(err))) if n else 0
    tol = tolerance if tolerance is not None else q15_quant_floor(op_count)
    corr = (float(np.corrcoef(a, b)[0, 1])
            if n > 1 and np.std(a) > 0 and np.std(b) > 0 else float("nan"))

    passed = (mism <= max_sign_mismatch) and (max_abs <= tol)
    reason = ""
    if mism > max_sign_mismatch:
        reason = (f"{mism} hard-decision sign mismatch(es) > "
                  f"{max_sign_mismatch} allowed")
    elif max_abs > tol:
        reason = f"LLR magnitude max_abs_err {max_abs} LSB exceeds tolerance {tol}"
    return LLRCompareResult(
        passed=passed, n_compared=n, sign_mismatches=mism, max_abs_err=max_abs,
        tolerance=tol, correlation=corr, delay_used=delay, reason=reason)
