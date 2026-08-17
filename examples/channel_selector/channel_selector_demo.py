# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless complex channel-selector demo — END TO END on one array.

The chain (every stage a placed Kyttar block):

  real multi-channel input → FloatToComplex → FreqXlatingFIR(9 taps, −9 kHz
  down-shift) → ComplexLowPassFilter(firdes, gain 0.9, cutoff 1.2 kHz)
  → MultiplyConstComplex(0.6+0.35j) → Conjugate → ComplexToImag → out

(The Conjugate stage was absent in the first shipped revision: a single-cell
complex-in→complex-out block used to mis-deliver its rails under the
auto-router's handoff — lessons_log 2026-08-09. The complex-handoff engine
fixes resolved it; test_conjugate_chain.py pins both the abutment and the
routed topology, and the stage is restored here in situ.)

The input carries two in-channel tones (8.6/9.4 kHz, ±400 Hz around the 9 kHz
channel center) and two interferers (4/14 kHz, landing at ∓5 kHz after the
down-shift, killed by the low-pass).

Golden: the IDENTICAL stock-GNU-Radio chain under the real GR interpreter
(float_to_complex, freq_xlating_fir_filter_ccf, fir_filter_ccf fed
firdes.low_pass — the demo ASSERTS the chip block's design_taps equal stock
firdes taps — multiply_const_cc, conjugate_cc, complex_to_imag). Analog Q15
chains are NOT bit-exact vs float GR; the acceptance bound is DERIVED from the
per-block verified error reports (never tuned): FloatToComplex 0 + FXF 16 +
ComplexLowPass 32 + MultiplyConstComplex 13 + Conjugate 0 + ComplexToImag 0
= 61 LSB (an upper bound assuming no cancellation; every stage gain ≤ 1).
All stages are feed-forward with the per-block delay=0 alignment convention,
so no transient trim applies.

NOTE the FreqXlatingFIR is SATURATION-BESPOKE (per-sample drive only — its
mid-chain mixer has no functional serialize-LOCK); this demo paces one sample
at a time, the same discipline the server's per-sample drive enforces.

Run::

    PYTHONPATH=runtime/python:placekyt QT_QPA_PLATFORM=offscreen \
        .venv/bin/python examples/channel_selector/channel_selector_demo.py
"""
from __future__ import annotations

import math
import os
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for _p in (str(ROOT / "runtime" / "python"), str(ROOT / "placekyt"), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

CHIP_YAML = str(ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml")
GRC_PATH = HERE / "channel_selector.grc"
KYT_PATH = HERE / "channel_selector.kyt"
GR_PYTHON = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")

# == the .grc variables (kept literally in sync)
FXF_TAPS = [0.0, 0.018715, 0.099838, 0.226239, 0.290416, 0.226239, 0.099838,
            0.018715, 0.0]
SIG = [0.25 * math.sin(2 * math.pi * 8600 * t / 32000)
       + 0.25 * math.cos(2 * math.pi * 9400 * t / 32000)
       + 0.2 * math.sin(2 * math.pi * 4000 * t / 32000)
       + 0.2 * math.sin(2 * math.pi * 14000 * t / 32000)
       for t in range(320)]

# Derived acceptance bound (see module docstring; Conjugate is exact → +0).
TOL_LSB = 0 + 16 + 32 + 13 + 0 + 0            # = 61

_GR_GOLDEN = r"""
import sys
from gnuradio import gr, blocks, filter as gfilter
from gnuradio.filter import firdes

sig = [float(x) for x in sys.argv[1].split(",")]
lpf_taps = [float(x) for x in sys.argv[2].split(",")]   # chip design_taps
fxf_taps = [float(x) for x in sys.argv[3].split(",")]
# The chip ComplexLowPassFilter designs firdes taps; assert stock firdes agrees
# so the golden is STOCK GR, not the block's own design echoed back.
stock = list(firdes.low_pass(0.9, 32000.0, 1200.0, 2500.0, 0, 6.76))
assert len(stock) == len(lpf_taps) and all(
    abs(a - b) < 1e-6 for a, b in zip(stock, lpf_taps)), "firdes tap mismatch"  # firdes is float32
tb = gr.top_block()
src = blocks.vector_source_f(sig, False, 1, [])
zero = blocks.vector_source_f([0.0] * len(sig), False, 1, [])
f2c = blocks.float_to_complex(1)
fxf = gfilter.freq_xlating_fir_filter_ccf(1, fxf_taps, 9000.0, 32000.0)
clpf = gfilter.fir_filter_ccf(1, stock)
rot = blocks.multiply_const_cc(0.6 + 0.35j)
conj = blocks.conjugate_cc()
c2i = blocks.complex_to_imag(1)
snk = blocks.vector_sink_f()
tb.connect(src, (f2c, 0))
tb.connect(zero, (f2c, 1))
tb.connect(f2c, fxf, clpf, rot, conj, c2i, snk)
tb.run()
print("GOLDEN", " ".join(repr(float(v)) for v in snk.data()))
"""


def _wr(h, d):
    return (0x6 << 12) | ((h & 0x1F) << 5) | (d & 0x1F)


def _jp(h, e):
    return (0x7 << 12) | ((h & 0x1F) << 5) | (e & 0x1F)


def _q15(f):
    return max(-32768, min(32767, int(round(f * 32768.0)))) & 0xFFFF


def _s16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def import_and_pnr():
    from engine.build import BuildEngine
    from engine.catalog import BlockCatalog
    from engine.grc_import import import_grc
    from engine.io.chip_type_io import load_chip_type
    from ui.controller import AppController

    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    # The auto-P&R sweep is STOCHASTIC (seeded CP-SAT packings): an unlucky
    # seed run can exhaust its attempt pool on layouts the acceptance gates
    # reject and surface a partial (unroutable nets / a single_cell_inout
    # build error). Retry the whole import→pnr→build a few times — a genuine
    # failure still raises, with the LAST attempt's named reason.
    last_err = None
    for _attempt in range(3):
        res = import_grc(str(GRC_PATH), cat)
        if not res.ok:
            raise RuntimeError(f"GRC import failed: unknown blocks {res.unknown}")
        ctrl = AppController(catalog=cat)
        ctrl.project = res.project
        rep = ctrl.auto_pnr({res.project.chip_type: ct})
        if not rep.ok:
            last_err = f"auto_pnr failed: {rep.reason}"
            continue
        bres = BuildEngine(cat, CHIP_YAML).build(res.project,
                                                 {res.project.chip_type: ct})
        if not bres.ok:
            last_err = ("build failed: "
                        + "; ".join(str(e) for e in bres.errors[:5]))
            continue
        return res.project, bres, cat, ct
    raise RuntimeError(last_err or "auto_pnr failed")


def _design_taps():
    from engine.catalog import BlockCatalog
    cat = BlockCatalog.from_gr_kyttar()
    blk = cat.instantiate("ComplexLowPassFilter", "ref", {
        "gain": 0.9, "samp_rate": 32000.0, "cutoff_freq": 1200.0,
        "transition_width": 2500.0, "window": "hamming", "beta": 6.76})
    return list(blk.design_taps)


def gr_golden(sig):
    script = Path(os.environ.get("TMPDIR", "/tmp")) / "channel_selector_golden.py"
    script.write_text(_GR_GOLDEN)
    r = subprocess.run(
        [GR_PYTHON, str(script),
         ",".join(repr(float(v)) for v in sig),
         ",".join(repr(float(t)) for t in _design_taps()),
         ",".join(repr(float(t)) for t in FXF_TAPS)],
        capture_output=True, text=True, timeout=300)
    line = next((ln for ln in r.stdout.splitlines()
                 if ln.startswith("GOLDEN")), None)
    if r.returncode != 0 or line is None:
        raise RuntimeError(f"GR golden failed: {r.stderr[-800:]}")
    return [float(x) for x in line.split()[1:]]


def run_chain(project, build_result, sig):
    """Inject the signal (paced per sample — the FXF is saturation-bespoke)
    and return the signed Q15 output words."""
    import simkyt

    landings = build_result.chips[0].input_landings
    lin = next(iter(landings.values()))
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(build_result.words(0))
    out = []
    for v in sig:
        chip.queue_words_physical("x16_in", [
            _wr(lin["hop"], lin["data_addrs"][0]), _q15(v),
            _jp(lin["hop"], lin["entry"])])
        idle = 0
        for _ in range(120000):
            chip.run(max_events=64)
            got = chip.read_port_words_timed("x16_out")
            if got:
                idle = 0
                out.extend(_s16(w) for w, _d, _t in got)
            else:
                idle += 1
            if idle > 200:
                break
    return out


def main():
    print("1. import channel_selector.grc -> auto place-and-route -> build ...")
    project, bres, cat, ct = import_and_pnr()
    used = sum(c.cell_count for c in bres.chips.values())
    print(f"   build OK — {used}/120 cells, {len(project.blocks)} blocks")
    print("2. run the multi-channel signal on real simKYT (per-sample paced) ...")
    got = run_chain(project, bres, SIG)
    gold = gr_golden(SIG)
    n = min(len(got), len(gold))
    worst = max(abs(got[i] - _s16(_q15(gold[i]))) for i in range(n))
    print(f"   chip: {len(got)}/{len(gold)} samples, worst |err| {worst} LSB "
          f"(bound {TOL_LSB})")
    ok = len(got) == len(gold) and worst <= TOL_LSB
    print("RESULT:", "WITHIN DERIVED BOUNDS — placed chain matches stock "
          "GNU Radio" if ok else "OUT OF BOUNDS")
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
