# SPDX-License-Identifier: GPL-3.0-or-later
"""CORDIC vectoring blocks — ComplexToMagBlock / ComplexToArgBlock vs GNU Radio
``blocks.complex_to_mag`` / ``blocks.complex_to_arg``.

Gate structure (the LMS two-half-proof pattern):

  (a) CHIP == REFERENCE (bit-exact): the placed+routed+built chain on simKYT
      reproduces ``process_reference`` word-for-word — per-sample AND under
      SATURATED pipelined drive (the blocks are fully feed-forward/stateless,
      so the pipeline-saturation oracle passes bit-exact; no INV-19 limit).
  (b) REFERENCE == GR (within derived tolerances): CORDIC is an approximation
      by construction — tolerances are LOCKED from the design-spike + live-GR
      measurement with ~2x margin, not tuned to pass (INV-4):
      magnitude max 19.7 LSB measured -> gate 40 LSB (vs SATURATED float truth
      — |v|>1 clamps to 0x7FFF by design); angle max 0.0026 rad measured for
      |v|>=0.1 -> gate 0.006 rad (input-quantization-limited below that:
      1 input LSB subtends ~1/(|v|*pi) half-turns).

MUTATIONS (INV-4): a corrupted ATAN table entry, a wrong CORDIC gain, and a
missing prescale-restore each FAIL the corresponding gate; a chip compared
against a corrupted reference MISMATCHES bit-level.

The angle convention is HALF-TURN Q15 (word/32768 * pi radians): 16-bit wrap
IS mod 2pi, so the +-pi seam needs no special case — the seam inputs are in
the edge-case sweep.
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
pytest.importorskip("PySide6")

_ROOT = Path(__file__).resolve().parents[2]
for p in (str(_ROOT / "placekyt"), str(_ROOT / "verification"),
          str(_ROOT / "runtime" / "python")):
    if p not in sys.path:
        sys.path.insert(0, p)

from kyttar_verify import write_report, CompareResult, Metric  # noqa: E402
from kyttar_verify.dut_runner import (  # noqa: E402
    run_block_dut_complex, run_block_dut_pipelined)

CHIP_YAML = str(_ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml")
GR_PYTHON = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")

pytestmark = pytest.mark.skipif(not Path(CHIP_YAML).exists(),
                                reason="chip yaml absent")

# Tolerances DERIVED from the design spike + live GR measurement (2026-08-13,
# 3000 uniform points): mag max 19.7 LSB / mean 8.6; arg max 0.0026 rad /
# mean 0.0002 for |v| >= 0.1. Locked with ~2x margin — NOT tunable (INV-4).
MAG_MAX_LSB = 40
MAG_MEAN_LSB = 17
ARG_MAX_RAD = 0.006      # |v| >= 0.1
ARG_MEAN_RAD = 0.0006
ARG_GATE_MAG = 0.1


def _s16(v):
    v = int(v) & 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _q15(x):
    return int(round(max(-1.0, min(0.9999695, float(x))) * 32768)) & 0xFFFF


def _mag_ref():
    from gr_kyttar.placement.blocks import ComplexToMagBlock
    return ComplexToMagBlock("ref")


def _arg_ref():
    from gr_kyttar.placement.blocks import ComplexToArgBlock
    return ComplexToArgBlock("ref")


def _run_chip(block, out_port, iq):
    d = run_block_dut_complex(block, iq, params={}, chip_yaml=CHIP_YAML,
                              words_per_sample=1, out_port=out_port,
                              place_xy=(0, 1))
    assert d.ok, f"DUT failed: {d.reason}"
    got = [None if w is None else int(w) & 0xFFFF for w in d.i_q15]
    assert None not in got, f"missing output words: {got.count(None)}"
    return got


_EDGE_IQ = [complex(0.9999695, 0.9999695), complex(-1.0, -1.0),
            complex(0.9999695, 0.0), complex(0.0, -1.0),
            complex(-0.9, 1e-4), complex(-0.9, -1e-4),   # +-pi seam
            complex(0.0, 0.0), complex(1e-4, -1e-4),
            complex(-0.7, 0.7), complex(0.7, -0.7), complex(-0.7, -0.7),
            complex(0.5, 0.0), complex(0.0, 0.5)]


# --------------------------------------------------------------------------- #
# (a) CHIP == REFERENCE, bit-exact — per-sample and saturated
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("block,out_port,ref", [
    ("ComplexToMagBlock", "mag", _mag_ref),
    ("ComplexToArgBlock", "z", _arg_ref),
])
def test_chip_bit_exact_to_reference(block, out_port, ref):
    rng = np.random.default_rng(7)
    iq = [complex(a, b) for a, b in rng.uniform(-0.95, 0.95, (24, 2))]
    got = _run_chip(block, out_port, iq)
    exp = list(ref().process_reference(
        [(_q15(c.real), _q15(c.imag)) for c in iq]))
    assert got == [int(w) for w in exp], (
        "chip diverges from process_reference at sample "
        f"{next(i for i, (a, b) in enumerate(zip(got, exp)) if a != b)}")


@pytest.mark.parametrize("block,out_port,ref", [
    ("ComplexToMagBlock", "mag", _mag_ref),
    ("ComplexToArgBlock", "z", _arg_ref),
])
def test_chip_bit_exact_edge_values(block, out_port, ref):
    """Rails, axes, the +-pi seam, zero and subnormal-ish inputs."""
    got = _run_chip(block, out_port, _EDGE_IQ)
    exp = list(ref().process_reference(
        [(_q15(c.real), _q15(c.imag)) for c in _EDGE_IQ]))
    assert got == [int(w) for w in exp]


@pytest.mark.parametrize("block,out_port,ref", [
    ("ComplexToMagBlock", "mag", _mag_ref),
    ("ComplexToArgBlock", "z", _arg_ref),
])
def test_chip_saturated_drive_bit_exact(block, out_port, ref):
    """SATURATED pipelined drive == per-sample output, bit-exact (positive
    gate — the chain is feed-forward/stateless; if this ever fails, a hazard
    was introduced)."""
    rng = np.random.default_rng(3)
    samples = [(_q15(a), _q15(b)) for a, b in rng.uniform(-0.9, 0.9, (16, 2))]
    exp = [int(w) for w in ref().process_reference(samples)]
    d = run_block_dut_pipelined(block, samples, params={}, chip_yaml=CHIP_YAML,
                                in_ports=("xi", "xq"), out_port=out_port,
                                place_xy=(0, 1))
    assert d.ok, f"saturated drive failed: {d.reason}"
    assert [int(w) & 0xFFFF for w in d.outputs_q15] == exp


# --------------------------------------------------------------------------- #
# (b) REFERENCE == GR, derived tolerances
# --------------------------------------------------------------------------- #

_GR_GOLDEN_SCRIPT = r"""
import json, sys
import numpy as np
from gnuradio import gr, blocks

cfg = json.loads(sys.argv[1])
rng = np.random.default_rng(cfg["seed"])
x = (rng.uniform(-0.95, 0.95, cfg["n"])
     + 1j * rng.uniform(-0.95, 0.95, cfg["n"])).astype(np.complex64)
src = blocks.vector_source_c(list(x))
m = blocks.complex_to_mag(1)
a = blocks.complex_to_arg(1)
sm = blocks.vector_sink_f()
sa = blocks.vector_sink_f()
tb = gr.top_block()
tb.connect(src, m, sm)
tb.connect(src, a, sa)
tb.run()
print("GOLDEN " + json.dumps({
    "x_re": [float(v) for v in x.real], "x_im": [float(v) for v in x.imag],
    "mag": [float(v) for v in sm.data()], "arg": [float(v) for v in sa.data()],
}))
"""


def _gr_available():
    try:
        r = subprocess.run([GR_PYTHON, "-c",
                            "from gnuradio import blocks; blocks.complex_to_mag"],
                           capture_output=True, timeout=30)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _golden(cfg):
    r = subprocess.run([GR_PYTHON, "-c", _GR_GOLDEN_SCRIPT, json.dumps(cfg)],
                       capture_output=True, text=True, timeout=300)
    for line in r.stdout.splitlines():
        if line.startswith("GOLDEN "):
            return json.loads(line[len("GOLDEN "):])
    raise AssertionError(f"GR golden failed:\n{r.stderr[-800:]}")


_CFG = {"seed": 11, "n": 3000}


def _ref_outputs(golden, mag_ref=None, arg_ref=None):
    pairs = [(_q15(a), _q15(b))
             for a, b in zip(golden["x_re"], golden["x_im"])]
    out = {}
    if mag_ref is not None:
        out["mag"] = np.array([_s16(w) / 32768.0
                               for w in mag_ref.process_reference(pairs)])
    if arg_ref is not None:
        out["arg"] = np.array([_s16(w) / 32768.0 * np.pi
                               for w in arg_ref.process_reference(pairs)])
    return out


@pytest.mark.skipif(not _gr_available(), reason="GR python unavailable")
def test_reference_matches_gr_magnitude():
    g = _golden(_CFG)
    ref = _ref_outputs(g, mag_ref=_mag_ref())["mag"]
    gm = np.minimum(np.array(g["mag"]), 32767 / 32768.0)   # |v|>1 saturates
    err = np.abs(ref - gm) * 32768.0
    assert err.max() <= MAG_MAX_LSB, f"mag max err {err.max():.1f} LSB"
    assert err.mean() <= MAG_MEAN_LSB, f"mag mean err {err.mean():.2f} LSB"


@pytest.mark.skipif(not _gr_available(), reason="GR python unavailable")
def test_reference_matches_gr_angle():
    g = _golden(_CFG)
    ref = _ref_outputs(g, arg_ref=_arg_ref())["arg"]
    ga = np.array(g["arg"])
    mag = np.abs(np.array(g["x_re"]) + 1j * np.array(g["x_im"]))
    sel = mag >= ARG_GATE_MAG
    da = np.abs((ref[sel] - ga[sel] + np.pi) % (2 * np.pi) - np.pi)
    assert da.max() <= ARG_MAX_RAD, f"arg max err {da.max():.5f} rad"
    assert da.mean() <= ARG_MEAN_RAD, f"arg mean err {da.mean():.6f} rad"


# --------------------------------------------------------------------------- #
# Mutations (INV-4): the gate must FAIL on a corrupted implementation
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(not _gr_available(), reason="GR python unavailable")
def test_mutation_corrupt_atan_entry_fails():
    """Flipping one mid-table ATAN entry must blow the angle gate."""
    import gr_kyttar.placement.blocks.cordic_blocks as cb
    g = _golden(_CFG)
    orig = cb.ATAN_Q15[3]
    cb.ATAN_Q15[3] = (orig + 700) & 0xFFFF
    try:
        ref = _ref_outputs(g, arg_ref=_arg_ref())["arg"]
    finally:
        cb.ATAN_Q15[3] = orig
    ga = np.array(g["arg"])
    mag = np.abs(np.array(g["x_re"]) + 1j * np.array(g["x_im"]))
    sel = mag >= ARG_GATE_MAG
    da = np.abs((ref[sel] - ga[sel] + np.pi) % (2 * np.pi) - np.pi)
    assert da.max() > ARG_MAX_RAD, (
        "corrupted ATAN table STILL passes — the gate certifies nothing")


@pytest.mark.skipif(not _gr_available(), reason="GR python unavailable")
def test_mutation_wrong_gain_fails():
    """A wrong CORDIC gain compensation must blow the magnitude gate."""
    import gr_kyttar.placement.blocks.cordic_blocks as cb
    g = _golden(_CFG)
    orig = cb.KINV_Q15
    cb.KINV_Q15 = int(orig * 0.97)
    try:
        ref = _ref_outputs(g, mag_ref=_mag_ref())["mag"]
    finally:
        cb.KINV_Q15 = orig
    gm = np.minimum(np.array(g["mag"]), 32767 / 32768.0)
    err = np.abs(ref - gm) * 32768.0
    assert err.max() > MAG_MAX_LSB, (
        "wrong-gain reference STILL passes — the gate certifies nothing")


def test_chip_mutation_corrupted_reference_mismatches():
    """The chip must NOT match a reference with a corrupted ATAN table
    (bit-level, no GR needed)."""
    import gr_kyttar.placement.blocks.cordic_blocks as cb
    rng = np.random.default_rng(5)
    iq = [complex(a, b) for a, b in rng.uniform(-0.9, 0.9, (10, 2))]
    pairs = [(_q15(c.real), _q15(c.imag)) for c in iq]
    got = _run_chip("ComplexToArgBlock", "z", iq)
    orig = cb.ATAN_Q15[2]
    cb.ATAN_Q15[2] = (orig + 500) & 0xFFFF
    try:
        exp = [int(w) for w in _arg_ref().process_reference(pairs)]
    finally:
        cb.ATAN_Q15[2] = orig
    assert got != exp, "chip matched a corrupted reference"


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(not _gr_available(), reason="GR python unavailable")
def test_emit_reports():
    g = _golden(_CFG)
    outs = _ref_outputs(g, mag_ref=_mag_ref(), arg_ref=_arg_ref())
    gm = np.minimum(np.array(g["mag"]), 32767 / 32768.0)
    ga = np.array(g["arg"])
    mag = np.abs(np.array(g["x_re"]) + 1j * np.array(g["x_im"]))
    sel = mag >= ARG_GATE_MAG

    em = np.abs(outs["mag"] - gm) * 32768.0
    ok_m = bool(em.max() <= MAG_MAX_LSB and em.mean() <= MAG_MEAN_LSB)
    write_report("ComplexToMagBlock",
                 CompareResult(passed=ok_m, metric=Metric.AMPLITUDE,
                               n_compared=len(gm),
                               max_abs_err=int(round(em.max())),
                               tolerance=MAG_MAX_LSB, delay_used=0),
                 coverage={
                     "gr_equiv": "blocks.complex_to_mag(1)",
                     "patterns": "3000 uniform complex points + rails/axes/"
                                 "seam edges; chip bit-exact to reference "
                                 "per-sample AND saturated-pipelined",
                     "mutation": True,
                     "note": "CORDIC vectoring, 14 iterations, prescale 1/4, "
                             f"max {em.max():.1f} LSB vs GR (gate "
                             f"{MAG_MAX_LSB}); |v|>1 saturates by design",
                 })
    da = np.abs((outs["arg"][sel] - ga[sel] + np.pi) % (2 * np.pi) - np.pi)
    ok_a = bool(da.max() <= ARG_MAX_RAD and da.mean() <= ARG_MEAN_RAD)
    write_report("ComplexToArgBlock",
                 CompareResult(passed=ok_a, metric=Metric.AMPLITUDE,
                               n_compared=int(sel.sum()),
                               max_abs_err=int(round(da.max() / np.pi * 32768)),
                               tolerance=int(round(ARG_MAX_RAD / np.pi * 32768)),
                               delay_used=0),
                 coverage={
                     "gr_equiv": "blocks.complex_to_arg(1)",
                     "patterns": "3000 uniform complex points + seam edges; "
                                 "chip bit-exact to reference per-sample AND "
                                 "saturated-pipelined",
                     "mutation": True,
                     "note": "half-turn Q15 angle (word/32768*pi rad); max "
                             f"{da.max():.5f} rad vs GR for |v|>=0.1 (gate "
                             f"{ARG_MAX_RAD}); input-quantization-limited "
                             "below |v|~0.1",
                 })
    assert ok_m and ok_a
