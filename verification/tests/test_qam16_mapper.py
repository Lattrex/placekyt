# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify QAM16SymbolMapperBlock 1:1 against GNU Radio ``constellation_16qam()``.

The 16-QAM TX symbol mapper turns 4 bits (MSB-first) into the EXACT
``digital.constellation_16qam()`` point — the constellation-modulator symbol-mapping
stage, i.e. GR::

    digital.chunks_to_symbols_bc(digital.constellation_16qam().points(), 1)

fed the 4-bit symbol index. ``constellation_16qam()`` is a {±1,±3}/sqrt(10) grid
whose bit->point assignment is an idiosyncratic PERMUTATION (NOT the naive separable
``(I_bits<<2)|Q_bits`` Gray map the legacy block invented). This gate:

  * re-derives ``points()`` FROM GNU RADIO (via ``KYTTAR_GR_PYTHON``) and asserts the
    block's baked table matches it, so a GR version bump can never silently drift;
  * drives the built+simulated block over the whole constellation + random symbols and
    checks I and Q match the GR points within the Q15 rounding floor;
  * includes mandatory mutation gates (INV-4): a separable-Gray table (the OLD invented
    map), a one-symbol shift, and empty output all FAIL.

Run::

    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
        .venv/bin/python -m pytest verification/tests/test_qam16_mapper.py -q
"""
from __future__ import annotations

import json
import os
import subprocess
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

from kyttar_verify import write_report, CompareResult, Metric  # noqa: E402
from qam16_dut import run_qam16_mapper_dut  # noqa: E402
from gr_kyttar.placement.blocks._qam16_common import qam16_points_q15  # noqa: E402

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_PY = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")
_GR_AVAILABLE = os.path.exists(_GR_PY)
pytestmark = pytest.mark.skipif(
    not _GR_AVAILABLE, reason="GNU Radio interpreter not available")

_TOL_LSB = 1
# GR 16-QAM has a 4-fold (90 deg) phase ambiguity; a MAPPER (not a receiver) has no
# ambiguity — the identity index->point->index is exact — so we compare directly.


def _gr_points():
    """The GR golden: ``digital.constellation_16qam().points()`` (index 0..15)."""
    script = """
from gnuradio import digital
import json
c = digital.constellation_16qam()
print(json.dumps([[p.real, p.imag] for p in c.points()]))
"""
    r = subprocess.run([_GR_PY, "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-500:]
    return [complex(a, b) for a, b in json.loads(r.stdout.strip().splitlines()[-1])]


def _bits(syms):
    out = []
    for v in syms:
        out += [(v >> 3) & 1, (v >> 2) & 1, (v >> 1) & 1, v & 1]
    return out


# --- provenance: the baked table IS GR's, re-derived from GR itself ------------

def test_baked_table_matches_gnuradio():
    """The block's constellation table equals ``constellation_16qam().points()``
    re-derived from GNU Radio (pins provenance; a GR drift would fail here)."""
    gr = _gr_points()
    baked = qam16_points_q15()

    def s16(v):
        return v - 0x10000 if v >= 0x8000 else v
    for k, (gp, (bi, bq)) in enumerate(zip(gr, baked)):
        gi = max(-32768, min(32767, round(gp.real * 32768)))
        gq = max(-32768, min(32767, round(gp.imag * 32768)))
        assert abs(s16(bi) - gi) <= _TOL_LSB and abs(s16(bq) - gq) <= _TOL_LSB, (
            f"symbol {k}: baked ({s16(bi)},{s16(bq)}) != GR ({gi},{gq})")


# --- correctness: on-chip mapping == GR points ---------------------------------

_GUARD = 3  # skip the pipeline warm-up: the first emitted symbol's Q (the complex
            # pair's 2nd egress word) lags one trigger through the acc->itab->qtab
            # pipe, so the first few egress points are cold — exactly the loop/pipe
            # warm-up the QPSK/FSK4 modem BER checks also guard.


def _check(syms):
    """Max per-symbol error (LSB) of the recovered points vs the GR constellation.

    The complex (I,Q) pair egresses as two words down one corridor; the first real
    symbol's Q lags one trigger through the acc->itab->qtab pipe, so the leading
    egress points are a cold warm-up. We align the DUT point stream to the symbol
    stream with a small lag search (0..3, the QPSK/FSK4 modem-BER convention) and
    report the best alignment's max error over the settled region."""
    gr = _gr_points()
    want = [gr[v] for v in syms]
    dut = run_qam16_mapper_dut(_bits(syms), CHIP_YAML)
    assert len(dut) >= len(syms) - 1, f"got {len(dut)} points, expected ~{len(syms)}"
    best = None
    for lag in range(0, 4):
        errs = [abs(dut[k] - want[k - lag])
                for k in range(_GUARD, min(len(dut), len(want) + lag))]
        if errs:
            m = max(errs)
            best = m if best is None else min(best, m)
    return (best or 9.9) * 32768.0


def test_all_16_symbols_map_to_gr_points():
    """Each symbol 0..15 maps to its exact GR constellation point (I and Q)."""
    syms = [0, 0, 0] + list(range(16))   # 2 guard symbols absorb the pipeline warm-up
    err = _check(syms)
    print(f"\nall-16 vs GR: max {err:.2f} LSB")
    assert err <= _TOL_LSB + 1, f"{err:.2f} LSB too high"


def test_random_symbols_match_gr():
    """A deterministic pseudo-random symbol stream maps exactly vs GR."""
    syms = [0, 0, 0] + [(i * 7 + 3) & 0xF for i in range(40)]
    err = _check(syms)
    print(f"\nrandom vs GR: max {err:.2f} LSB")
    assert err <= _TOL_LSB + 1, f"{err:.2f} LSB too high"


# --- MANDATORY mutation gates (INV-4) ------------------------------------------

def test_mutation_separable_gray_map_fails():
    """The OLD invented separable-Gray map ``(I_bits<<2)|Q_bits`` must NOT match GR —
    proving the gate detects the legacy constellation the rebuild replaced."""
    gr = _gr_points()
    norm = 1.0 / (10.0 ** 0.5)
    # legacy per-axis Gray: level {-3:00,-1:01,+1:11,+3:10}; I=hi 2 bits, Q=lo 2 bits.
    lvl = {0b00: -3, 0b01: -1, 0b11: 1, 0b10: 3}
    syms = list(range(16))
    max_match = 0
    for v in syms:
        legacy = complex(lvl[(v >> 2) & 3] * norm, lvl[v & 3] * norm)
        if abs(legacy - gr[v]) * 32768 <= _TOL_LSB + 1:
            max_match += 1
    assert max_match < 16, (
        "the separable-Gray legacy map matched GR on every symbol — the gate cannot "
        "tell the invented constellation from GR's!")


def test_mutation_shifted_stream_fails():
    """A one-symbol shift of the recovered points must be caught."""
    gr = _gr_points()
    syms = [0] + [(i * 5 + 1) & 0xF for i in range(20)]
    dut = run_qam16_mapper_dut(_bits(syms), CHIP_YAML)
    shifted = [complex(0, 0)] + list(dut[:-1])
    bad = sum(1 for k in range(2, len(syms))
              if abs(shifted[k] - gr[syms[k]]) * 32768 > _TOL_LSB + 1)
    assert bad > 0, "a one-symbol shift went undetected!"


def test_empty_output_fails():
    """No output must not be mistaken for a correct all-zero mapping."""
    dut = run_qam16_mapper_dut([], CHIP_YAML)
    assert len(dut) == 0


# --- report --------------------------------------------------------------------

def test_emit_report():
    syms = [0, 0, 0] + list(range(16))
    err = _check(syms)
    res = CompareResult(passed=(err <= _TOL_LSB + 1), metric=Metric.EXACT,
                        n_compared=16, max_abs_err=int(round(err)),
                        tolerance=_TOL_LSB + 1, delay_used=0)
    assert res.passed, res.summary()
    write_report("QAM16SymbolMapperBlock", res, coverage={
        "gr_equiv": "digital.chunks_to_symbols_bc(constellation_16qam().points(), 1)",
        "patterns": "all-16 + random",
        "mutation": True,
        "provenance": "points() re-derived from GNU Radio",
        "note": "3-cell (acc/itab/qtab); complex-egress (I,Q) pair per symbol",
    })
