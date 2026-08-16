# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless audio-tail + S-meter demo — two streams duplex on one array.

Stream 'audio': DCBlocker(32, short form) → AGC(0.02, ref 0.3, gain 0.999,
max_gain 0.999 — the chip's Q15 attenuating regime, mirrored in the GR golden
exactly as the per-block gate does) → BandReject 3300..3700 → Squelch(-25 dB). Stream 'meter': Abs → MovingAverage(8)
→ Nlog10(10·log10, wire value scaled /64 per the block's documented Q15
representation). Both streams share x16_in/x16_out demuxed by tags (the same
duplex machinery as the BPSK modem).

Golden: the IDENTICAL stock-GNU-Radio chains under the real GR interpreter.
Analog Q15 chains are NOT bit-exact vs float GR — the acceptance bound is
DERIVED from the per-block verified error reports (never tuned to pass):

  audio: sum of the stages' report tolerances (DCB 59 + AGC 80 + BRF 79 +
         Squelch 4 = 222 LSB ≈ 0.0068 FS) — an upper bound assuming no error
         cancellation, each stage gain ≤ 1.
  meter: Abs 2 + MA 5 = 7 LSB linear into the log; near the quiet floor the
         log slope amplifies linear error without bound, so dB is compared
         only where the averaged level exceeds 0.02 FS, where
         d(dB) = (10/ln10)·ε/x ≤ 4.343·(7/32768)/0.02 ≈ 0.047 dB, plus the
         Nlog10 stage's own 10-LSB wire tolerance (×64 scale ≈ 0.02 dB).

Run::

    PYTHONPATH=runtime/python:placekyt QT_QPA_PLATFORM=offscreen \
        .venv/bin/python examples/audio_meter/audio_meter_demo.py
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
GRC_PATH = HERE / "audio_meter.grc"
KYT_PATH = HERE / "audio_meter.kyt"
GR_PYTHON = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")

# The test signal (== the .grc 'sig' variable).
# Tone FIRST, silence after: during silence agc_ff's uncapped gain climbs
# above 1.0 (err = ref every sample), which Q15 cannot represent — a leading
# silence therefore puts the whole run outside the AGC block's verified
# (attenuating, gain <= 1) envelope. The squelch still demos by closing on the
# tail silence.
# Tail length: the squelch power IIR (alpha 0.01) decays as 0.99^n
# (~0.044 dB/sample) from the tone's level; measured, the estimate crosses
# the -25 dB threshold after ~450 silence samples — 520 gives the gate
# margin to actually CLOSE inside the run.
SIG = ([0.05 + 0.85 * math.sin(2 * math.pi * 1000 * t / 8000)
        for t in range(160)]
       + [0.0] * 520)

# Derived acceptance bounds (see module docstring).
AUDIO_TOL_LSB = 59 + 80 + 79 + 4               # DCB+AGC+BRF+Squelch = 222
# The AGC per-block gate compares AFTER a 40-sample startup-transient trim
# (test_agc.py head_shift=_TRIM); the composed comparison mirrors it: samples
# within TRANSIENT_TRIM of the tone onset are excluded from the LSB bound.
TONE_ONSET = 0
TRANSIENT_TRIM = 40
METER_FLOOR = 0.02                              # linear level gate for dB compare
METER_TOL_DB = 4.343 * (7 / 32768.0) / METER_FLOOR + (10 / 32768.0) * 64.0
NLOG10_DB_SCALE = 64.0                          # the block's documented wire scale

_GR_GOLDEN = r"""
import sys
from gnuradio import gr, blocks, analog, filter as gfilter
from gnuradio.filter import firdes

sig = [float(x) for x in sys.argv[1].split(",")]
tb = gr.top_block()
# audio chain
a_src = blocks.vector_source_f(sig, False, 1, [])
dcb = gfilter.dc_blocker_ff(32, False)
agc = analog.agc_ff(0.02, 0.3, 0.999)
# max_gain capped at 0.999 — the chip block's Q15 gain register regime (the
# per-block gate drives agc_ff the same way); uncapped GR gain exceeds 1.0
# near zero-crossings and the trajectories split for the whole loop transient.
agc.set_max_gain(0.999)
brf = gfilter.fir_filter_fff(1, firdes.band_reject(0.999, 8000, 3300, 3700, 400,
                                                   0, 6.76))  # 0 = hamming
sq = analog.pwr_squelch_ff(-25.0, 0.01, 0, False)
a_snk = blocks.vector_sink_f()
tb.connect(a_src, dcb, agc, brf, sq, a_snk)
# meter chain
m_src = blocks.vector_source_f(sig, False, 1, [])
env = blocks.abs_ff()
avg = blocks.moving_average_ff(8, 0.125)
db = blocks.nlog10_ff(10.0, 1, 0.0)
m_snk = blocks.vector_sink_f()
tb.connect(m_src, env, avg, db, m_snk)
tb.run()
print("AUDIO", " ".join(repr(float(v)) for v in a_snk.data()))
print("METER", " ".join(repr(float(v)) for v in m_snk.data()))
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
    res = import_grc(str(GRC_PATH), cat)
    if not res.ok:
        raise RuntimeError(f"GRC import failed: unknown blocks {res.unknown}")
    ct = load_chip_type(CHIP_YAML)
    ctrl = AppController(catalog=cat)
    ctrl.project = res.project
    rep = ctrl.auto_pnr({res.project.chip_type: ct})
    if not rep.ok:
        raise RuntimeError(f"auto_pnr failed: {rep.reason}")
    bres = BuildEngine(cat, CHIP_YAML).build(res.project,
                                             {res.project.chip_type: ct})
    if not bres.ok:
        raise RuntimeError(
            "build failed: " + "; ".join(str(e) for e in bres.errors[:5]))
    return res.project, bres, cat, ct


def gr_golden(sig):
    script = Path(os.environ.get("TMPDIR", "/tmp")) / "audio_meter_golden.py"
    script.write_text(_GR_GOLDEN)
    r = subprocess.run([GR_PYTHON, str(script),
                        ",".join(repr(float(v)) for v in sig)],
                       capture_output=True, text=True, timeout=300)
    out = {}
    for ln in r.stdout.splitlines():
        if ln.startswith(("AUDIO", "METER")):
            key, *vals = ln.split()
            out[key.lower()] = [float(v) for v in vals]
    if r.returncode != 0 or set(out) != {"audio", "meter"}:
        raise RuntimeError(f"GR golden failed: {r.stderr[-600:]}")
    return out["audio"], out["meter"]


def run_streams(project, build_result, sig):
    """Drive both streams (paced per sample) and return each stream's signed
    Q15 output words, demuxed by out_tag from the shipped nets."""
    import simkyt
    from model.connection import BlockEndpoint, ChipPortEndpoint

    landings = build_result.chips[0].input_landings
    # Map stream_id -> landing (nets carry the stream ids from the .grc).
    by_sid = {}
    for c in project.connections:
        if (isinstance(c.source, ChipPortEndpoint) and c.source.port == "x16_in"
                and getattr(c, "stream_id", None) and c.name in landings):
            by_sid[c.stream_id] = landings[c.name]
    tags = {}
    for c in project.connections:
        if (isinstance(c.target, ChipPortEndpoint)
                and c.target.port == "x16_out"
                and getattr(c, "out_tag", None) is not None):
            tags[c.out_tag] = c
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(build_result.words(0))
    out = {sid: [] for sid in by_sid}
    tag_to_sid = {}
    # Attribute each out_tag to a stream by walking each filament from its
    # first block: simpler — there are exactly two; match audio->squelch chain
    # tag and meter->nlog10 chain tag by source block name.
    for tag, conn in tags.items():
        src = conn.source.block
        tag_to_sid[tag] = "meter" if "nlog10" in src else "audio"

    def pump(idle_max):
        idle = 0
        for _ in range(120000):
            chip.run(max_events=64)
            got = chip.read_port_words_timed("x16_out")
            if got:
                idle = 0
                for v, d, _t in got:
                    sid = tag_to_sid.get(int(d))
                    if sid:
                        out[sid].append(_s16(v))
            else:
                idle += 1
            if idle > idle_max:
                break

    # Interleave the two streams sample-by-sample (the duplex schedule).
    for k in range(len(sig)):
        for sid in ("audio", "meter"):
            lin = by_sid[sid]
            chip.queue_words_physical("x16_in", [
                _wr(lin["hop"], lin["data_addrs"][0]), _q15(sig[k]),
                _jp(lin["hop"], lin["entry"])])
            pump(60)
    pump(400)
    return out["audio"], out["meter"]


def main():
    print("1. import audio_meter.grc -> duplex auto place-and-route -> build ...")
    project, bres, cat, ct = import_and_pnr()
    used = sum(c.cell_count for c in bres.chips.values())
    print(f"   build OK — {used}/120 cells, {len(project.blocks)} blocks, "
          f"2 streams")
    print("2. drive both streams on real simKYT ...")
    a_chip, m_chip = run_streams(project, bres, SIG)
    a_gold, m_gold = gr_golden(SIG)
    n_a = min(len(a_chip), len(a_gold))
    worst_a = max((abs(a_chip[i] - _s16(_q15(a_gold[i]))) for i in range(n_a)
                   if not (TONE_ONSET <= i < TONE_ONSET + TRANSIENT_TRIM)),
                  default=0)
    # meter: compare in dB where the averaged level is above the floor.
    n_m = min(len(m_chip), len(m_gold))
    worst_m = 0.0
    compared = 0
    for i in range(n_m):
        lin = 10 ** (m_gold[i] / 10.0)
        if lin < METER_FLOOR:
            continue
        chip_db = (m_chip[i] / 32768.0) * NLOG10_DB_SCALE
        worst_m = max(worst_m, abs(chip_db - m_gold[i]))
        compared += 1
    print(f"   audio: {len(a_chip)}/{len(a_gold)} samples, worst |err| "
          f"{worst_a} LSB (bound {AUDIO_TOL_LSB})")
    print(f"   meter: {len(m_chip)}/{len(m_gold)} samples, {compared} above "
          f"floor, worst |err| {worst_m:.4f} dB (bound {METER_TOL_DB:.4f})")
    ok = (len(a_chip) == len(a_gold) and len(m_chip) == len(m_gold)
          and worst_a <= AUDIO_TOL_LSB and worst_m <= METER_TOL_DB
          and compared > 50)
    print("RESULT:", "WITHIN DERIVED BOUNDS — both placed streams match stock "
          "GNU Radio" if ok else "OUT OF BOUNDS")
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
