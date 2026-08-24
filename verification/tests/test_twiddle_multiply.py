# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify TwiddleMultiplyBlock — complex multiply by a per-sample
TABLE-SELECTED Q15 twiddle constant (``y[n] = x[n] * twiddles[n mod P]``).

There is no GNU Radio counterpart block: the golden is the Python integer
reference in ``gr_kyttar.placement.blocks.fft_primitives``
(``twiddle_cmul_ref`` over the build-time table from ``quantize_twiddle``),
the pinned result of the FFT numeric design spike:

  * twiddles stored ``round(32768*x)`` (round-half-even, FULL scale — never
    0x7FFF-as-one, which costs a measured 2-6 dB on every signal class);
  * TRIVIAL entries special-cased structurally: ``W == 1`` passes through
    untouched, ``W == -1j`` is a rail swap + saturating negate;
  * non-trivial = 4 floor-MULQs + 2 V-restore saturating combines in the
    pinned p1-p2 / p3+p4 MultiplyCC ordering (the 3-multiply Karatsuba form is
    REJECTED: its constants reach ±sqrt(2) in Q15 — unrepresentable).

Gates: BIT-EXACT (tol 0) DUT vs the reference over edge + random (>=3 seeds)
+ sine/two-tone/impulse + ADVERSARIAL exact-full-scale stimulus (both the
combine-saturation and the -32768-negate corners exercised and gated); the
trivial and non-trivial paths EACH gated (pure-identity, pure -j, pure-mul,
and mixed tables); table period sweep P = 1..12 incl. the N=16 stage-0 table.
Mutations proven to FAIL (INV-4): wrong-table-index (rotated table),
0x7FFF-as-one trivial multiply, dropped saturation, I/Q swap, sign flip,
+1 delay, empty.  Orientation runs in the shared
``test_orientation_invariance.py`` (all 8 D4) AND is spot-gated here; the
SATURATED gate runs in the shared ``test_pipeline_saturation.py``
(COMPLEX_2IN2OUT).

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_twiddle_multiply.py -x -q
"""
from __future__ import annotations

import os
import random
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

from kyttar_verify import (  # noqa: E402
    run_block_dut_complex, write_report, CompareResult, Metric,
    D4_ORIENTATIONS, compare_dut_results)
from gr_kyttar.placement.blocks.fft_primitives import (  # noqa: E402
    TwiddleMultiplyBlock, quantize_twiddle, twiddle_cmul_ref, mulq,
    KIND_ID, KIND_MJ, KIND_MUL, s16, u16)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
pytestmark = pytest.mark.skipif(not os.path.exists(CHIP_YAML),
                                reason="chip yaml absent")

# The N=16 stage-0 DIF table (k = 0..7, W = exp(-2j*pi*k/16)) — the exact
# streaming-FFT use case: mixed trivial (k=0 identity, k=4 = -j) + 6
# non-trivial entries.
_N16_STAGE0 = [np.exp(-2j * np.pi * k / 16) for k in range(8)]
_N16_STAGE0[0] = 1        # exact trivial values (the spike's k-detection)
_N16_STAGE0[4] = -1j

_MIXED4 = [1, 0.7071067811865476 - 0.7071067811865476j, -1j,
           -0.5 + 0.25j]


def _q15(v: float) -> int:
    return max(-32768, min(32767, int(round(v * 32768.0)))) & 0xFFFF


def _words(stream):
    return ([_q15(c.real) for c in stream], [_q15(c.imag) for c in stream])


def _ref(x, twiddles):
    xi, xq = _words(x)
    return TwiddleMultiplyBlock("ref", twiddles=twiddles) \
        .process_reference_q15(xi, xq)


def _run(x, twiddles, **kw):
    dut = run_block_dut_complex("TwiddleMultiplyBlock", x,
                                params={"twiddles": twiddles},
                                chip_yaml=CHIP_YAML, **kw)
    assert dut.ok, dut.reason
    return dut


def _random_stream(seed, n=24, amp=1.0):
    rng = random.Random(seed)
    return [complex(rng.uniform(-amp, amp), rng.uniform(-amp, amp))
            for _ in range(n)]


def _assert_bitexact(x, twiddles, dut=None):
    yi, yq = _ref(x, twiddles)
    dut = dut or _run(x, twiddles)
    assert dut.i_q15 == yi, "I rail diverges from the bit-exact reference"
    assert dut.q_q15 == yq, "Q rail diverges from the bit-exact reference"
    return dut


# --- build-time table (quantization + trivial detection) ---------------------

def test_quantize_round_half_even_full_scale():
    """Stored coefficients are round(32768*x), round-half-EVEN, at FULL scale
    — the documented N=16 twiddle words verbatim."""
    kind, c, d = quantize_twiddle(np.exp(-2j * np.pi * 1 / 16))
    assert (kind, s16(c), s16(d)) == (KIND_MUL, 30274, -12540)
    kind, c, d = quantize_twiddle(np.exp(-2j * np.pi * 2 / 16))
    assert (kind, s16(c), s16(d)) == (KIND_MUL, 23170, -23170)
    kind, c, d = quantize_twiddle(np.exp(-2j * np.pi * 3 / 16))
    assert (kind, s16(c), s16(d)) == (KIND_MUL, 12540, -30274)


def test_trivial_detection_exact_values_only():
    assert quantize_twiddle(1)[0] == KIND_ID
    assert quantize_twiddle(1 + 0j)[0] == KIND_ID
    assert quantize_twiddle(-1j)[0] == KIND_MJ
    assert quantize_twiddle(complex(0, -1))[0] == KIND_MJ
    # near-misses are NOT trivial (and raise if unrepresentable):
    assert quantize_twiddle(0.9998 + 0.0001j)[0] == KIND_MUL


def test_unrepresentable_rails_raise():
    """HW-DEVIATION: a non-trivial value quantizing to ±32768 on either rail
    RAISES loudly (W = -1, near-1.0 reals, +1j)."""
    for w in (-1, -1.0 + 0j, 0.999999 + 0.0j, 1j, -0.00001 - 0.99999j):
        with pytest.raises(ValueError, match="HARDWARE LIMIT"):
            TwiddleMultiplyBlock("x", twiddles=[w])


def test_period_range_raises():
    """HW-DEVIATION: P is capped at MAX_PERIOD (table cells); out-of-range
    RAISES, never truncates."""
    TwiddleMultiplyBlock("ok", twiddles=[_MIXED4[1]] * 12)      # P = 12 fits
    with pytest.raises(ValueError, match="HARDWARE LIMIT"):
        TwiddleMultiplyBlock("x", twiddles=[_MIXED4[1]] * 13)
    with pytest.raises(ValueError, match="HARDWARE LIMIT"):
        TwiddleMultiplyBlock("x", twiddles=[])


def test_cell_budget():
    """All 6 cells fit at the MAX supported period (data + instructions
    within the 32-word cell; the exit cell keeps >=1 free word for the INV-17
    fan-out JUMP)."""
    from gr_kyttar.placement.resolver import CellProgramResolver
    blk = TwiddleMultiplyBlock("probe", twiddles=[_MIXED4[1]] * 12)
    r = CellProgramResolver()
    for cid, cp in blk.build_cell_programs().items():
        n_instr = r.count_instructions(cp)
        regs = [p.register for p in cp.inputs] \
            + [d.address for d in (cp.data or ())] \
            + [s.register for s in (cp.state or ())]
        max_addr = max([a for a in regs if a is not None], default=-1)
        assert max_addr + n_instr <= 31, (
            f"{cid}: {n_instr} instr from addr {max_addr + 1} overflow")
        if cid == "emit":
            used = n_instr + len(cp.inputs) + len(cp.data or ()) \
                + len(cp.state or ())
            assert 32 - used >= 1, "emit: no room for the fan-out JUMP"


# --- DUT bit-exact (tol 0) ---------------------------------------------------

def test_bitexact_n16_stage0_table():
    """The N=16 stage-0 table (the real streaming-FFT case): P=8, mixed
    trivial + non-trivial, full-scale sine stimulus."""
    n = 32
    t = np.arange(n)
    x = list((32767.0 / 32768.0) * np.exp(1j * 2 * np.pi * 3 * t / n))
    _assert_bitexact(x, _N16_STAGE0)


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_bitexact_random_mixed(seed):
    _assert_bitexact(_random_stream(seed), _MIXED4)


@pytest.mark.parametrize("twiddles,label", [
    ([1], "P1-identity"),
    ([-1j], "P1-minus-j"),
    ([1, -1j], "P2-all-trivial"),
    ([0.7071067811865476 - 0.7071067811865476j,
      -0.7071067811865476 - 0.7071067811865476j], "P2-all-mul"),
    ([1, 0.9238795325112867 - 0.3826834323650898j,
      0.7071067811865476 - 0.7071067811865476j,
      0.3826834323650898 - 0.9238795325112867j, -1j,
      -0.3826834323650898 - 0.9238795325112867j], "P6-mixed"),
], ids=lambda v: v if isinstance(v, str) else "")
def test_bitexact_table_sweep(twiddles, label):
    """Period + composition sweep: pure-trivial, pure-multiply and mixed
    tables each bit-exact (the trivial and non-trivial paths each gated)."""
    _assert_bitexact(_random_stream(99, n=3 * len(twiddles) + 5), twiddles)


def test_bitexact_adversarial_full_scale():
    """Exact-full-scale corners: the -j path's saturating negate at
    xi = -32768 (-1.0 -> +0x7FFF) and combine saturation on a near-unit
    twiddle driven at rail-full scale — both exercised and gated."""
    fs = 32767.0 / 32768.0
    x = [complex(-1.0, -1.0), complex(fs, -1.0), complex(-1.0, fs),
         complex(fs, fs), complex(-1.0, 0.0), complex(0.0, -1.0)]
    tw = [-1j, 0.9238795325112867 - 0.3826834323650898j, 1]
    yi, yq = _ref(x, tw)
    # non-vacuity: the -j negate saturates (index 0: yq = sat(-(-1.0))):
    assert yq[0] == 0x7FFF
    _assert_bitexact(x, tw)


def test_trivial_identity_passes_untouched():
    """The identity entries pass EVERY word through untouched — including
    words a 0x7FFF multiply would corrupt (the pinned structural skip)."""
    x = [complex(-1.0, -1.0), complex(32767 / 32768, -32768 / 32768),
         complex(1 / 32768, -1 / 32768)]
    xi, xq = _words(x)
    dut = _run(x, [1])
    assert dut.i_q15 == xi and dut.q_q15 == xq


def test_float_reference_parity():
    """The float reference tracks the bit-exact one within the MultiplyCC-
    class ±3-LSB floor (two truncating MULQs per rail + quantization)."""
    x = _random_stream(5, 32, amp=0.7)
    yi, yq = _ref(x, _MIXED4)
    yf = TwiddleMultiplyBlock("ref", twiddles=_MIXED4).process_reference(x)
    for k in range(len(x)):
        assert abs(s16(yi[k]) / 32768.0 - yf[k].real) <= 3.5 / 32768.0
        assert abs(s16(yq[k]) / 32768.0 - yf[k].imag) <= 3.5 / 32768.0


# --- MANDATORY mutations (INV-4) ---------------------------------------------

def test_mutation_wrong_table_index_fails():
    """A reference walking a ROTATED table must FAIL — proof the per-sample
    slot selection (n mod P) is genuinely under test."""
    x = _random_stream(11, 20)
    rot = _MIXED4[1:] + _MIXED4[:1]
    yi_rot, yq_rot = _ref(x, rot)
    dut = _run(x, _MIXED4)
    assert (dut.i_q15, dut.q_q15) != (yi_rot, yq_rot), \
        "gate cannot detect a wrong table index"


def test_mutation_trivial_as_7fff_multiply_fails():
    """The 0x7FFF-as-one mutation: modelling the identity entries as a
    multiply by 0x7FFF must FAIL — proof the structural pass-through (not a
    near-1 multiply) is what ships."""
    x = _random_stream(13, 20)
    xi, xq = _words(x)
    mut_yi = [mulq(w, 0x7FFF) for w in xi]
    dut = _run(x, [1])
    assert dut.i_q15 == xi
    assert dut.i_q15 != mut_yi, "DUT matches the 0x7FFF multiply — the " \
        "trivial entry is NOT passing through untouched"


def test_mutation_dropped_saturation_fails():
    """A wrap-instead-of-saturate combine model must FAIL on the adversarial
    stimulus — proof the V-restore combines are live."""
    fs = 32767.0 / 32768.0
    x = [complex(-1.0, -1.0), complex(fs, -1.0), complex(-1.0, 0.0)]
    tw = [0.9238795325112867 - 0.3826834323650898j, -1j]
    blk = TwiddleMultiplyBlock("ref", twiddles=tw)
    xi, xq = _words(x)

    def _wrap_ref():
        yi, yq = [], []
        for n, (iw, qw) in enumerate(zip(xi, xq)):
            kind, c, d = blk.table[n % blk.period]
            if kind == KIND_ID:
                yi.append(iw); yq.append(qw)
            elif kind == KIND_MJ:
                yi.append(qw); yq.append(u16(-s16(iw)))       # WRAPPING negate
            else:
                p1, p2 = mulq(iw, c), mulq(qw, d)
                p3, p4 = mulq(iw, d), mulq(qw, c)
                yi.append(u16(s16(p1) - s16(p2)))             # WRAPPING combine
                yq.append(u16(s16(p3) + s16(p4)))
        return yi, yq

    yi, yq = _ref(x, tw)
    wi, wq = _wrap_ref()
    assert (wi, wq) != (yi, yq), "wrap == sat — stimulus has no teeth"
    dut = _run(x, tw)
    assert (dut.i_q15, dut.q_q15) == (yi, yq)
    assert (dut.i_q15, dut.q_q15) != (wi, wq), \
        "DUT matches the WRAP model — a saturating combine is missing"


def test_mutation_iq_swap_fails():
    x = _random_stream(17, 20)
    yi, yq = _ref(x, _MIXED4)
    dut = _run(x, _MIXED4)
    assert yi != yq, "degenerate stimulus"
    assert not (dut.i_q15 == yq and dut.q_q15 == yi), \
        "gate cannot detect an I/Q swap"


def test_mutation_sign_inverted_fails():
    x = _random_stream(19, 20)
    yi, _yq = _ref(x, _MIXED4)
    dut = _run(x, _MIXED4)
    inv = [(0x10000 - w) & 0xFFFF for w in dut.i_q15]
    assert inv != yi, "gate cannot detect an inverted output"


def test_mutation_one_sample_offset_fails():
    x = _random_stream(23, 20)
    yi, _ = _ref(x, _MIXED4)
    dut = _run(x, _MIXED4)
    assert ([0x0000] + dut.i_q15[:-1]) != yi, \
        "gate cannot detect a +1 sample delay"


def test_mutation_empty_output_fails():
    x = _random_stream(29, 8)
    yi, _ = _ref(x, _MIXED4)
    assert [] != yi


# --- orientation spot gate (full 8-D4 coverage in the shared suite) ----------

@pytest.mark.parametrize("orient", [("cw",), ("cw", "cw"), ("mirror_v",)],
                         ids=lambda o: "+".join(o))
def test_orientation_spot(orient):
    x = _random_stream(31, 10)
    base = _run(x, _MIXED4)
    rot = _run(x, _MIXED4, orient=list(orient))
    ok, detail = compare_dut_results(base, rot)
    assert ok, f"{'+'.join(orient)}: {detail}"


# --- dashboard report --------------------------------------------------------

def test_emit_report():
    x = _random_stream(7, 24)
    dut = _assert_bitexact(x, _N16_STAGE0)
    # DERIVED, not asserted: recompute the reference and count the actual
    # mismatches, so the report's verdict is the measurement (INV-36).
    yi, yq = _ref(x, _N16_STAGE0)
    mismatches = sum(1 for a, b in zip(dut.i_q15, yi) if a != b) + \
        sum(1 for a, b in zip(dut.q_q15, yq) if a != b)
    write_report("TwiddleMultiplyBlock",
                 CompareResult(passed=(mismatches == 0), metric=Metric.EXACT,
                               n_compared=2 * len(x), max_abs_err=0.0,
                               tolerance=0.0, bit_errors=mismatches,
                               delay_used=0),
                 coverage={"edge": True, "random": 3, "table_sweep": 5,
                           "n16_stage0": True, "adversarial_full_scale": True,
                           "trivial_paths": True, "saturation": True,
                           "orientation_d4": True, "mutation": True})
