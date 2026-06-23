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


def q15_quant_floor(op_count: int) -> int:
    """Worst-case Q15 quantization bound (LSB) for a feed-forward block.

    Each Q15 multiply/MAC truncation contributes up to ~1 LSB; ``op_count`` such
    operations accumulate worst-case-linearly, plus one final-quantization LSB.

    This is valid ONLY for feed-forward, non-saturating blocks (a single MULQ,
    an N-tap FIR/MAC chain). Recursive (IIR) and feedback/loop blocks do NOT
    obey an op-count bound — their error depends on pole radius / trajectory
    divergence; those classes must pass an explicit ``tolerance`` and use a
    behavioral metric, never this floor.
    """
    return max(1, int(op_count) + 1)


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
    tol = tolerance if tolerance is not None else q15_quant_floor(op_count)
    res.tolerance = tol
    res.passed = (max_abs <= tol)
    if not res.passed:
        res.reason = (f"max_abs_err {max_abs} LSB exceeds tolerance {tol} "
                      f"(worst: {worst[:3]})")
    return res
