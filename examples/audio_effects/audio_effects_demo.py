# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless audio-effects demo — THREE placed effects, each END TO END.

Every effect is a placed dataflow JOIN — two independent arms recombining into
a multi-input block — the topology this repo's build engine gained single-fire
support for (join blocks' data-only ``sink`` entry + the importer's
trigger-arm election, see grc_import._elect_join_triggers):

  echo:    x →2 [Add.a0 | Delay(8) → Gain(0.5) → Add.a1]
           → Gain(0.5) → IIRBiquad(butter(2, 0.15)) → KeepOneInN(2) → out
  tremolo: x →2 [Multiply.a0 | NCO(250 Hz, 0.45) → ComplexToReal
           → AddConst(0.5) → Multiply.a1] → out       (gain swings 0.05..0.95)
  comb:    x →2 [Subtract.a0 | Delay(5) → Gain(0.3) → Subtract.a1] → out

The NCO is trigger-driven (one cos/sin per input, value ignored) and 250 Hz is
ON its 16-bit phase grid (250/8000·65536 = 2048), so no freq-word drift enters
the comparison. Both fan-outs originate at the INPUT PORT — the supported join
topology (each arm is its own host-injection landing; the demo injects every
landing per sample). ENGINE LIMITS (documented, this session): a port supports
~2 fan-out arms (a 3rd corridor fails placement), and a BLOCK's output cell
cannot fan out at all (no splitter block yet) — which is why this is a rack of
three 2-arm effects rather than one deep chain.

Golden: the IDENTICAL stock-GNU-Radio chains under the real GR interpreter.
Analog Q15 chains are NOT bit-exact vs float GR; each bound is DERIVED from
the per-block verified error reports (never tuned):

  echo:    Gain 2 + Add 2 (join) → ·0.5 + Gain 2 → + IIRBiquad 21
           (passband gain ≤ 1) = 25; KeepOneInN exact       → 25 LSB
  tremolo: rail NCO 12 + C2R 0 + AddConst 2 = 14;
           |g|≤0.95·(x exact) + |x|≤1·14 + Multiply 2       → 16 LSB
  comb:    Gain 2 + Subtract 2                              → 4 LSB

Run::

    PYTHONPATH=runtime/python:placekyt QT_QPA_PLATFORM=offscreen \
        .venv/bin/python examples/audio_effects/audio_effects_demo.py
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
GR_PYTHON = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")

SIG = [0.5 * math.sin(2 * math.pi * 330 * t / 8000)
       + 0.05 * math.sin(2 * math.pi * 2200 * t / 8000)
       for t in range(400)]
IIR_B = [0.04125353724172031, 0.08250707448344062, 0.04125353724172031]
IIR_A_FULL = [1.0, -1.3489677452527948, 0.5139818942196759]

# (grc file, derived LSB bound, expected output count for len(SIG) inputs)
EFFECTS = {
    "echo": ("effect_echo.grc", 25, len(SIG) // 2),
    "tremolo": ("effect_tremolo.grc", 16, len(SIG)),
    "comb": ("effect_comb.grc", 4, len(SIG)),
}

_GR_GOLDEN = r"""
import sys
from gnuradio import gr, blocks, analog, filter as gfilter

which = sys.argv[1]
sig = [float(x) for x in sys.argv[2].split(",")]
b = [float(x) for x in sys.argv[3].split(",")]
a = [float(x) for x in sys.argv[4].split(",")]
tb = gr.top_block()
src = blocks.vector_source_f(sig, False, 1, [])
snk = blocks.vector_sink_f()
if which == "echo":
    d8 = blocks.delay(gr.sizeof_float, 8)
    g1 = blocks.multiply_const_ff(0.5)
    add = blocks.add_ff()
    g2 = blocks.multiply_const_ff(0.5)
    iir = gfilter.iir_filter_ffd(b, a, False)
    keep = blocks.keep_one_in_n(gr.sizeof_float, 2)
    tb.connect(src, (add, 0))
    tb.connect(src, d8, g1, (add, 1))
    tb.connect(add, g2, iir, keep, snk)
elif which == "tremolo":
    nco = analog.sig_source_f(8000.0, analog.GR_COS_WAVE, 250.0, 0.45, 0.0)
    head = blocks.head(gr.sizeof_float, len(sig))
    bias = blocks.add_const_ff(0.5)
    mul = blocks.multiply_ff()
    tb.connect(src, (mul, 0))
    tb.connect(nco, head, bias, (mul, 1))
    tb.connect(mul, snk)
elif which == "comb":
    d5 = blocks.delay(gr.sizeof_float, 5)
    g = blocks.multiply_const_ff(0.3)
    sub = blocks.sub_ff()
    tb.connect(src, (sub, 0))
    tb.connect(src, d5, g, (sub, 1))
    tb.connect(sub, snk)
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


def import_and_pnr(grc_name: str):
    from engine.build import BuildEngine
    from engine.catalog import BlockCatalog
    from engine.grc_import import import_grc
    from engine.io.chip_type_io import load_chip_type
    from ui.controller import AppController

    cat = BlockCatalog.from_gr_kyttar()
    res = import_grc(str(HERE / grc_name), cat)
    if not res.ok:
        raise RuntimeError(f"GRC import failed: unknown blocks {res.unknown}")
    ct = load_chip_type(CHIP_YAML)
    ctrl = AppController(catalog=cat)
    ctrl.project = res.project
    rep = ctrl.auto_pnr({res.project.chip_type: ct}, time_budget_s=120.0)
    if not rep.ok:
        raise RuntimeError(f"auto_pnr failed: {rep.reason}")
    bres = BuildEngine(cat, CHIP_YAML).build(res.project,
                                             {res.project.chip_type: ct})
    if not bres.ok:
        raise RuntimeError(
            "build failed: " + "; ".join(str(e) for e in bres.errors[:5]))
    return res.project, bres, cat, ct


def gr_golden(which: str, sig):
    script = Path(os.environ.get("TMPDIR", "/tmp")) / "audio_effects_golden.py"
    script.write_text(_GR_GOLDEN)
    r = subprocess.run(
        [GR_PYTHON, str(script), which,
         ",".join(repr(float(v)) for v in sig),
         ",".join(repr(v) for v in IIR_B),
         ",".join(repr(v) for v in IIR_A_FULL)],
        capture_output=True, text=True, timeout=300)
    line = next((ln for ln in r.stdout.splitlines()
                 if ln.startswith("GOLDEN")), None)
    if r.returncode != 0 or line is None:
        raise RuntimeError(f"GR golden failed: {r.stderr[-800:]}")
    return [float(x) for x in line.split()[1:]]


def run_chain(build_result, sig):
    """Inject every host landing per sample (each fan-out arm is its own
    landing) and return the signed Q15 output words."""
    import simkyt

    lands = list(build_result.chips[0].input_landings.values())
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(build_result.words(0))
    out = []
    for v in sig:
        for lin in lands:
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


def run_effect(which: str):
    grc_name, tol, n_expect = EFFECTS[which]
    project, bres, cat, ct = import_and_pnr(grc_name)
    used = sum(c.cell_count for c in bres.chips.values())
    got = run_chain(bres, SIG)
    gold = gr_golden(which, SIG)
    n = min(len(got), len(gold))
    worst = max(abs(got[i] - _s16(_q15(gold[i]))) for i in range(n))
    ok = (len(got) == len(gold) == n_expect and worst <= tol)
    print(f"   {which}: {used}/120 cells, {len(got)}/{len(gold)} samples, "
          f"worst |err| {worst} LSB (bound {tol}) -> "
          f"{'OK' if ok else 'OUT OF BOUNDS'}")
    return ok


def main():
    print("import each effect .grc -> auto place-and-route -> build -> run on "
          "real simKYT vs the stock-GR golden ...")
    results = [run_effect(w) for w in EFFECTS]
    ok = all(results)
    print("RESULT:", "WITHIN DERIVED BOUNDS — all three placed effects match "
          "stock GNU Radio" if ok else "OUT OF BOUNDS")
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
