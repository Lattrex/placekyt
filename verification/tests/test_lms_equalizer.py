# SPDX-License-Identifier: GPL-3.0-or-later
"""LMSEqualizerBlock — decision-directed complex LMS equalizer vs GNU Radio
``digital.linear_equalizer`` (adaptive_algorithm_lms).

Gate structure (two half-proofs that compose transitively):

  (a) CHIP == REFERENCE (bit-exact): the placed+routed+built block on simKYT
      reproduces ``process_reference`` word-for-word, including the adaptation
      dynamics, across parameter sweeps and seeds.
  (b) REFERENCE == alpha * GR (scale-covariant steady state): the same Q15
      reference tracks ``digital.linear_equalizer`` scaled by alpha = 1/2 (the
      unit-circle decision constellation vs GR's +-1.414-component QPSK — LMS
      is scale-covariant, so the whole trajectory scales). Post-convergence:
      RMS output deviation, 100% decision agreement, BER 0, tap distance.

  (a) + (b) => the CHIP is GR-equivalent. (b) runs at full length in python
  (the event-accurate chip sim is ~seconds/sample for this 14-cell block; (a)
  covers the chip at burst length, (b) covers the DSP at statistical length.)

MUTATIONS (INV-4): wrong step size, sign-flipped update, +1 delay, and a
frozen-adaptation corruption each FAIL the gate.

INV-19 KNOWN-LIMIT (recorded, guarded): SATURATED (pipelined) drive does NOT
quiesce — the backward gradient broadcast races the next sample's forward pass
(EventLimit). The block's contract is PER-SAMPLE drive (every GRC batch uses
it); the serialize-LOCK choreography (Costas pipeline_lock pattern: IN locks
until BCAST's unlock) is the recorded follow-up. The guard test asserts the
limit still exists so a silent behavioural change is flagged.
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
ALPHA = 0.5

pytestmark = pytest.mark.skipif(not Path(CHIP_YAML).exists(),
                                reason="chip yaml absent")


def _s16(v):
    v = int(v) & 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _q15(x):
    return int(round(max(-1.0, min(0.9999695, float(x))) * 32768)) & 0xFFFF


def _ref(num_taps, mu):
    from gr_kyttar.placement.blocks import LMSEqualizerBlock
    return LMSEqualizerBlock("ref", num_taps=num_taps, step_size=mu)


def _run_chip(iq, num_taps, mu):
    d = run_block_dut_complex("LMSEqualizerBlock", iq,
                              params={"num_taps": num_taps, "step_size": mu},
                              chip_yaml=CHIP_YAML, words_per_sample=2)
    assert d.ok, f"DUT failed: {d.reason}"
    return [None if w is None else int(w) & 0xFFFF
            for pair in zip(d.i_q15, d.q_q15) for w in pair]


# --------------------------------------------------------------------------- #
# (a) CHIP == REFERENCE, bit-exact
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("num_taps,mu,seed", [
    (5, 0.03, 3),
    (5, 0.01, 11),
    (3, 0.05, 7),
    (2, 0.03, 5),
])
def test_chip_bit_exact_to_reference(num_taps, mu, seed):
    rng = np.random.default_rng(seed)
    iq = [complex(a, b) for a, b in rng.uniform(-0.6, 0.6, (30, 2))]
    got = _run_chip(iq, num_taps, mu)
    exp = list(_ref(num_taps, mu).process_reference(
        [(_q15(c.real), _q15(c.imag)) for c in iq]))
    assert None not in got, f"missing output words: {got.count(None)}"
    assert got == [int(w) for w in exp], (
        "chip diverges from process_reference at word "
        f"{next(i for i, (a, b) in enumerate(zip(got, exp)) if a != b)}")


def test_chip_bit_exact_edge_values():
    """Rails, zeros and sign boundaries through the saturating datapath."""
    iq = [complex(0.999, -0.999), complex(-1.0, 1.0) * 0.99,
          complex(0.0, 0.0), complex(1e-4, -1e-4),
          complex(0.7, 0.7), complex(-0.7, -0.7),
          complex(0.999, 0.0), complex(0.0, -0.999)]
    got = _run_chip(iq, 5, 0.03)
    exp = list(_ref(5, 0.03).process_reference(
        [(_q15(c.real), _q15(c.imag)) for c in iq]))
    assert got == [int(w) for w in exp]


# --------------------------------------------------------------------------- #
# (b) REFERENCE == alpha * GR, scale-covariant steady state
# --------------------------------------------------------------------------- #

_GR_GOLDEN_SCRIPT = r"""
import json, sys
import numpy as np
from gnuradio import gr, blocks, digital
import pmt

cfg = json.loads(sys.argv[1])
cons = digital.constellation_qpsk()
pts = np.array(cons.points())
rng = np.random.default_rng(cfg["seed"])
syms = pts[rng.integers(0, 4, cfg["n"])]
chan = np.array(cfg["chan"])
x = (np.convolve(syms, chan)[:cfg["n"]] / cfg["norm"]).astype(np.complex64)
training = syms[:cfg["ntrain"]]
alg = digital.adaptive_algorithm_lms(cons.base(), cfg["mu"]).base()
eq = digital.linear_equalizer(cfg["num_taps"], 1, alg, True,
                              list(training), 'corr_est')
tag = gr.tag_utils.python_to_tag((0, pmt.intern("corr_est"), pmt.PMT_NIL,
                                  pmt.intern("s")))
src = blocks.vector_source_c(list(x), False, 1, [tag])
snk = blocks.vector_sink_c()
tb = gr.top_block()
tb.connect(src, eq, snk)
tb.run()
out = np.array(snk.data())
print("GOLDEN " + json.dumps({
    "x_re": [float(v) for v in x.real], "x_im": [float(v) for v in x.imag],
    "sym_re": [float(v) for v in syms.real],
    "sym_im": [float(v) for v in syms.imag],
    "y_re": [float(v) for v in out.real], "y_im": [float(v) for v in out.imag],
    "taps_re": [float(v) for v in np.real(eq.taps())[::-1]],
    "taps_im": [float(v) for v in np.imag(eq.taps())[::-1]],
}))
"""


def _gr_available():
    try:
        r = subprocess.run([GR_PYTHON, "-c",
                            "from gnuradio import digital; "
                            "digital.adaptive_algorithm_lms"],
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


def _run_reference_stream(golden, num_taps, mu):
    ref = _ref(num_taps, mu)
    out = ref.process_reference(
        [(_q15(a), _q15(b)) for a, b in zip(golden["x_re"], golden["x_im"])])
    y = np.array([complex(_s16(out[2 * i]), _s16(out[2 * i + 1])) / 32768.0
                  for i in range(len(out) // 2)])
    taps = 2 * np.array([complex(_s16(a), _s16(b))
                         for a, b in zip(ref._wr, ref._wi)]) / 32768.0
    return y, taps


def _dec(v):
    return (np.real(v) >= 0).astype(int) * 2 + (np.imag(v) >= 0).astype(int)


_CFG = {"seed": 7, "n": 4000, "ntrain": 96, "mu": 0.03,
        "chan": [1.0, 0.35, -0.15], "norm": 3.2, "num_taps": 5}


@pytest.mark.skipif(not _gr_available(), reason="GR python unavailable")
def test_reference_matches_gr_scale_covariant():
    g = _golden(_CFG)
    y, taps = _run_reference_stream(g, _CFG["num_taps"], _CFG["mu"])
    gy = np.array(g["y_re"]) + 1j * np.array(g["y_im"])
    syms = np.array(g["sym_re"]) + 1j * np.array(g["sym_im"])
    gtaps = np.array(g["taps_re"]) + 1j * np.array(g["taps_im"])
    n = min(len(y), len(gy))
    tail = slice(600, n)

    dev = float(np.sqrt(np.mean(np.abs(y[tail] - ALPHA * gy[tail]) ** 2)))
    agree = float(np.mean(_dec(gy[tail]) == _dec(y[tail])))
    ber_q = float(np.mean(_dec(y[tail]) != _dec(syms[tail])))
    ber_g = float(np.mean(_dec(gy[tail]) != _dec(syms[tail])))
    tapdist = float(np.max(np.abs(taps - ALPHA * gtaps)))

    # Tolerances DERIVED from the design study (spike4): RMS deviation from
    # alpha*GR was 0.006 at mu=0.03 — locked with 3x margin. Tap distance was
    # 0.005 — locked with 4x margin. NOT tunable to pass (INV-4).
    assert ber_g == 0.0, "golden itself failed to converge — bad stimulus"
    assert ber_q == 0.0, f"Q15 reference BER {ber_q}"
    assert agree == 1.0, f"decision agreement {agree * 100:.2f}%"
    assert dev < 0.02, f"RMS |ref - alpha*GR| = {dev:.4f}"
    assert tapdist < 0.02, f"max tap deviation from alpha*GR = {tapdist:.4f}"


@pytest.mark.skipif(not _gr_available(), reason="GR python unavailable")
def test_dd_cold_start_reaches_gr_steady_state():
    """The chip contract (no on-chip training memory): DD-from-spike reaches
    the SAME steady state as GR-with-training — the reference runs with its
    built-in spike cold start against the trained golden."""
    g = _golden(dict(_CFG, seed=19))
    y, taps = _run_reference_stream(g, _CFG["num_taps"], _CFG["mu"])
    gy = np.array(g["y_re"]) + 1j * np.array(g["y_im"])
    n = min(len(y), len(gy))
    tail = slice(1200, n)
    dev = float(np.sqrt(np.mean(np.abs(y[tail] - ALPHA * gy[tail]) ** 2)))
    agree = float(np.mean(_dec(gy[tail]) == _dec(y[tail])))
    assert agree == 1.0 and dev < 0.02, (dev, agree)


# --------------------------------------------------------------------------- #
# Mutations (INV-4): the gate must FAIL on a corrupted DUT
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(not _gr_available(), reason="GR python unavailable")
@pytest.mark.parametrize("corrupt", ["wrong_mu", "flipped_update",
                                     "one_delay", "frozen"])
def test_mutations_fail(corrupt):
    g = _golden(_CFG)
    num_taps, mu = _CFG["num_taps"], _CFG["mu"]
    if corrupt == "wrong_mu":
        y, taps = _run_reference_stream(g, num_taps, mu * 8)
    elif corrupt == "frozen":
        ref = _ref(num_taps, 0.0)          # adaptation dead
        out = ref.process_reference(
            [(_q15(a), _q15(b)) for a, b in zip(g["x_re"], g["x_im"])])
        y = np.array([complex(_s16(out[2 * i]), _s16(out[2 * i + 1])) / 32768.0
                      for i in range(len(out) // 2)])
        taps = 2 * np.array([complex(_s16(a), _s16(b))
                             for a, b in zip(ref._wr, ref._wi)]) / 32768.0
    elif corrupt == "one_delay":
        y0, taps = _run_reference_stream(g, num_taps, mu)
        y = np.concatenate([[0j], y0[:-1]])
    else:  # flipped_update: negate mu (gradient ascent)
        y, taps = _run_reference_stream(g, num_taps, -mu)

    gy = np.array(g["y_re"]) + 1j * np.array(g["y_im"])
    gtaps = np.array(g["taps_re"]) + 1j * np.array(g["taps_im"])
    n = min(len(y), len(gy))
    tail = slice(600, n)
    dev = float(np.sqrt(np.mean(np.abs(y[tail] - ALPHA * gy[tail]) ** 2)))
    tapdist = float(np.max(np.abs(taps - ALPHA * gtaps)))
    assert dev >= 0.02 or tapdist >= 0.02, (
        f"mutation {corrupt} NOT caught (dev={dev:.4f}, taps={tapdist:.4f}) "
        f"— the gate certifies nothing")


def test_chip_mutation_param_mismatch_fails():
    """A chip built with the WRONG step size must NOT match the reference."""
    rng = np.random.default_rng(3)
    iq = [complex(a, b) for a, b in rng.uniform(-0.6, 0.6, (30, 2))]
    got = _run_chip(iq, 5, 0.03)
    exp = list(_ref(5, 0.12).process_reference(
        [(_q15(c.real), _q15(c.imag)) for c in iq]))
    assert got != [int(w) for w in exp], "wrong-mu reference matched the chip"


# --------------------------------------------------------------------------- #
# INV-19 known-limit guard: saturated drive is NOT supported (yet)
# --------------------------------------------------------------------------- #

def test_saturated_drive_known_limit_guard():
    """PER-SAMPLE CONTRACT (recorded limit): under saturated (pipelined) drive
    the backward gradient broadcast races the next forward pass and the design
    does not quiesce. This guard asserts the limit STILL EXISTS — if the block
    ever becomes saturation-safe (the serialize-LOCK follow-up), this test
    fails on purpose so the contract and the docs get re-evaluated."""
    rng = np.random.default_rng(3)
    samples = [(_q15(a), _q15(b)) for a, b in rng.uniform(-0.6, 0.6, (16, 2))]
    pipe = run_block_dut_pipelined("LMSEqualizerBlock", samples,
                                   params={"num_taps": 5, "step_size": 0.03},
                                   chip_yaml=CHIP_YAML,
                                   in_ports=("xi", "xq"), out_port="yi")
    assert not pipe.ok, (
        "saturated drive now SUCCEEDS — the INV-19 limit no longer holds; "
        "promote the block to saturation-safe (update docs + this guard)")


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(not _gr_available(), reason="GR python unavailable")
def test_emit_report():
    g = _golden(_CFG)
    y, _taps = _run_reference_stream(g, _CFG["num_taps"], _CFG["mu"])
    syms = np.array(g["sym_re"]) + 1j * np.array(g["sym_im"])
    n = min(len(y), len(syms))
    tail = slice(600, n)
    errs = int(np.sum(_dec(y[tail]) != _dec(syms[tail])))
    res = CompareResult(passed=(errs == 0), metric=Metric.DECISION,
                        n_compared=int(n - 600), bit_errors=errs, delay_used=0)
    write_report("LMSEqualizerBlock", res, coverage={
        "gr_equiv": "digital.linear_equalizer(num_taps, 1, "
                    "adaptive_algorithm_lms(constellation_qpsk(), mu)) — "
                    "scale-covariant (alpha=1/2 unit-circle constellation)",
        "patterns": "4000-symbol QPSK through [1, 0.35, -0.15] multipath; "
                    "chip bit-exact to reference over param sweep + edges; "
                    "reference == alpha*GR steady state (RMS<0.02, decisions "
                    "100%, BER 0, taps within 0.02)",
        "mutation": True,
        "note": "DD-only spike cold start (no on-chip training memory); "
                "taps stored halved (envelope sum|w|<=2); PER-SAMPLE drive "
                "contract (INV-19 known limit, guarded)",
    })
    assert errs == 0
