# SPDX-License-Identifier: GPL-3.0-or-later
"""Audio-tail + S-meter + true-RMS example — WHOLE-CHAIN end-to-end gate
(AGENTS.md §5b).

Three analog streams duplex on one array (the BPSK-modem demux machinery):
  audio: DCBlocker(32, short) → AGC(0.02, ref 0.3, gain 0.999, max_gain 0.999)
         → BandRejectFilter(3300..3700) → Squelch(-25 dB)
  meter: Abs → MovingAverage(8, 1/8) → Nlog10(10·log10)
  rms:   RMSBlock (= blocks.rms_ff, alpha 0.0625 — exact in Q15)

Golden: the IDENTICAL stock-GNU-Radio chains under the real GR interpreter.
Analog Q15 chains are NOT bit-exact vs float GR; the acceptance bounds are
DERIVED from the per-block verified error reports (audio: sum of stage
tolerances = 222 LSB; meter: linear tolerance through the log slope above a
0.02 FS floor ≈ 0.066 dB; rms: the RMS block report's 16-LSB tolerance above
its 0.18-FS verified amplitude floor) — never tuned to pass. The AGC golden
runs with max_gain=0.999, the chip block's Q15 regime, exactly as the
per-block gate drives agc_ff (uncapped GR gain exceeds 1.0 near
zero-crossings and the trajectories split for the whole loop transient).

Whole-chain proof for 9 analog blocks that previously had only per-block
gates: DCBlocker, AGC, BandRejectFilter, Squelch, Abs, MovingAverage,
Nlog10, RMS (+ the duplex three-stream tagging path itself).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[2]
_EX = _ROOT / "examples" / "audio_meter"
for _p in (str(_ROOT / "runtime" / "python"), str(_ROOT / "placekyt"), str(_EX)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from audio_meter_demo import (  # noqa: E402
    AUDIO_TOL_LSB, CHIP_YAML, GR_PYTHON, KYT_PATH, METER_FLOOR, METER_TOL_DB,
    NLOG10_DB_SCALE, RMS_TOL_LSB, SIG, TONE_ONSET, TRANSIENT_TRIM, _jp, _q15,
    _s16, _wr, gr_golden, import_and_pnr, rms_worst, run_streams)

pytestmark = pytest.mark.skipif(
    not Path(GR_PYTHON).exists(), reason="GNU Radio interpreter absent")


@pytest.fixture(scope="module")
def built():
    project, bres, cat, ct = import_and_pnr()
    return project, bres, cat, ct


@pytest.fixture(scope="module")
def golden():
    return gr_golden(SIG)


def _audio_worst(a_chip, a_gold):
    n = min(len(a_chip), len(a_gold))
    return max((abs(a_chip[i] - _s16(_q15(a_gold[i]))) for i in range(n)
                if not (TONE_ONSET <= i < TONE_ONSET + TRANSIENT_TRIM)),
               default=0)


def _meter_worst(m_chip, m_gold):
    worst, compared = 0.0, 0
    for i in range(min(len(m_chip), len(m_gold))):
        if 10 ** (m_gold[i] / 10.0) < METER_FLOOR:
            continue
        worst = max(worst,
                    abs((m_chip[i] / 32768.0) * NLOG10_DB_SCALE - m_gold[i]))
        compared += 1
    return worst, compared


def test_import_pnr_build_ok(built):
    project, bres, cat, ct = built
    assert bres.ok
    assert len(project.blocks) == 8
    assert not project.panels               # generic duplex sweep, no panel
    # three ingress streams, three tagged egress nets
    from model.connection import ChipPortEndpoint
    sids = {c.stream_id for c in project.connections
            if isinstance(c.source, ChipPortEndpoint)
            and getattr(c, "stream_id", None)}
    assert sids == {"audio", "meter", "rms"}


def test_out_tags_are_5bit_and_unique(built):
    """Regression for the derived-tag engine bug: out_tags landed at 36/47,
    which a 5-bit DEST field silently wraps — every derived tag must sit in
    2..31 and be unique per chip."""
    project, bres, cat, ct = built
    from model.connection import ChipPortEndpoint
    tags = [c.out_tag for c in project.connections
            if isinstance(c.target, ChipPortEndpoint)
            and getattr(c, "out_tag", None) is not None]
    assert tags and len(set(tags)) == len(tags)
    assert all(2 <= t <= 31 for t in tags), tags


def test_all_streams_within_derived_bounds(built, golden):
    project, bres, cat, ct = built
    a_gold, m_gold, r_gold = golden
    a_chip, m_chip, r_chip = run_streams(project, bres, SIG)
    assert len(a_chip) == len(a_gold) and len(m_chip) == len(m_gold)
    assert len(r_chip) == len(r_gold)
    assert _audio_worst(a_chip, a_gold) <= AUDIO_TOL_LSB
    worst_db, compared = _meter_worst(m_chip, m_gold)
    assert compared > 50
    assert worst_db <= METER_TOL_DB
    worst_r, compared_r = rms_worst(r_chip, r_gold)
    assert compared_r > 100
    assert worst_r <= RMS_TOL_LSB


def test_squelch_actually_closes(built, golden):
    """The tail silence must close the squelch on chip AND in GR — the audio
    stream ends in a run of exact zeros (the squelch is not decorative)."""
    project, bres, cat, ct = built
    a_gold, _m, _r = golden
    a_chip, _mc, _rc = run_streams(project, bres, SIG)
    assert a_gold[-1] == 0.0
    assert all(v == 0 for v in a_chip[-20:])


def test_shipped_kyt_runs_end_to_end(golden):
    from engine.build import BuildEngine
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from engine.io.project_io import load_project

    project = load_project(KYT_PATH)
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    bres = BuildEngine(cat, CHIP_YAML).build(project, {project.chip_type: ct})
    assert bres.ok, [str(e) for e in bres.errors[:3]]
    a_gold, m_gold, r_gold = golden
    a_chip, m_chip, r_chip = run_streams(project, bres, SIG)
    assert _audio_worst(a_chip, a_gold) <= AUDIO_TOL_LSB
    worst_db, compared = _meter_worst(m_chip, m_gold)
    assert compared > 50 and worst_db <= METER_TOL_DB
    worst_r, compared_r = rms_worst(r_chip, r_gold)
    assert compared_r > 100 and worst_r <= RMS_TOL_LSB


def test_shipped_kyt_saturated_matches_per_sample():
    """The shipped .grc drives all three streams at Full-speed (``pipelined:
    'yes'``), so the whole-chain proof must hold SATURATED (INV-19/21): the
    three interleaved streams' whole bursts queued back-to-back with NO
    quiescence must recover BIT-EXACTLY what the per-sample drive recovers,
    on the audio, meter AND rms rails (every block in the chains is
    individually saturation-proven; RMSBlock is in the shared saturation
    registry)."""
    import simkyt

    from engine.build import BuildEngine
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from engine.io.project_io import load_project
    from model.connection import ChipPortEndpoint

    project = load_project(KYT_PATH)
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    bres = BuildEngine(cat, CHIP_YAML).build(project, {project.chip_type: ct})
    assert bres.ok, [str(e) for e in bres.errors[:3]]
    a_ps, m_ps, r_ps = run_streams(project, bres, SIG)

    landings = bres.chips[0].input_landings
    by_sid = {}
    for c in project.connections:
        if (isinstance(c.source, ChipPortEndpoint) and c.source.port == "x16_in"
                and getattr(c, "stream_id", None) and c.name in landings):
            by_sid[c.stream_id] = landings[c.name]
    tag_to_sid = {}
    for c in project.connections:
        if (isinstance(c.target, ChipPortEndpoint)
                and c.target.port == "x16_out"
                and getattr(c, "out_tag", None) is not None):
            src = c.source.block
            tag_to_sid[c.out_tag] = ("meter" if "nlog10" in src
                                     else "rms" if "rms" in src else "audio")
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(bres.words(0))
    words = []
    for k in range(len(SIG)):
        for sid in ("audio", "meter", "rms"):
            lin = by_sid[sid]
            words += [_wr(lin["hop"], lin["data_addrs"][0]), _q15(SIG[k]),
                      _jp(lin["hop"], lin["entry"])]
    chip.queue_words_physical("x16_in", words)
    out = {"audio": [], "meter": [], "rms": []}
    idle = 0
    for _ in range(800000):
        chip.run(max_events=256)   # BOUNDED — a livelock must fail clean
        got = chip.read_port_words_timed("x16_out")
        if got:
            idle = 0
            for v, d, _t in got:
                sid = tag_to_sid.get(int(d))
                if sid:
                    out[sid].append(_s16(v))
        else:
            idle += 1
        if idle > 3000 or (len(out["audio"]) >= len(a_ps)
                           and len(out["meter"]) >= len(m_ps)
                           and len(out["rms"]) >= len(r_ps)):
            break
    assert out["audio"] == a_ps, "audio rail diverges under saturated drive"
    assert out["meter"] == m_ps, "meter rail diverges under saturated drive"
    assert out["rms"] == r_ps, "rms rail diverges under saturated drive"


# ------------------------------------------------------------- MUTATION (INV-4)
def test_mutation_agc_reference_FAILS(built, golden):
    """Halving the AGC reference must push the audio stream far outside the
    derived bound — the bound sees the loop's operating point, it is not so
    wide as to pass anything."""
    from engine.build import BuildEngine
    from engine.io.chip_type_io import load_chip_type

    project, bres, cat, ct = built
    agc = next(b for b in project.blocks if b.type == "AGCBlock")
    old = dict(agc.params)
    try:
        agc.params["reference"] = 0.15
        ct2 = load_chip_type(CHIP_YAML)
        bres2 = BuildEngine(cat, CHIP_YAML).build(project,
                                                  {project.chip_type: ct2})
        assert bres2.ok
        a_gold, _m, _r = golden
        a_chip, _mc, _rc = run_streams(project, bres2, SIG)
        assert _audio_worst(a_chip, a_gold) > AUDIO_TOL_LSB, \
            "gate blind to a halved AGC reference"
    finally:
        agc.params.clear()
        agc.params.update(old)
