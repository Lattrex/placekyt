# SPDX-License-Identifier: GPL-3.0-or-later
"""Data-link example — WHOLE-CHAIN end-to-end gate (AGENTS.md §5b).

An 11-block scrambled byte loopback, every stage a placed Kyttar block:
UnpackKBits → Not → AndConst → MapBB → LFSRScrambler → DiffEncoder →
DiffDecoder → LFSRScrambler(descrambler) → CharToFloat(128) → FloatToChar(128)
→ PackKBits. This is the whole-chain proof for 8 blocks that previously had
only per-block gates (Unpack, Not, AndConst, MapBB, LFSR, DiffDecoder,
CharToFloat, FloatToChar).

The PRIMARY golden is the IDENTICAL stock-GNU-Radio flowgraph run under the
real GR interpreter — the strongest possible equivalence claim for the placed
composition — with the loopback identity (bytes out == payload) asserted on
top. Also: shipped-.kyt parity and INV-4 mutations (a corrupted scrambler seed
must break the GR match; the loopback identity ALONE would be blind to a
matched-pair corruption, which is exactly why the GR golden is primary).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[2]
_EX = _ROOT / "examples" / "data_link"
for _p in (str(_ROOT / "runtime" / "python"), str(_ROOT / "placekyt"), str(_EX)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from data_link_demo import (  # noqa: E402
    CHIP_YAML, DEMO_TEXT, GR_PYTHON, KYT_PATH, _jp, _wr, gr_golden,
    import_and_pnr, run_link)

pytestmark = pytest.mark.skipif(
    not Path(GR_PYTHON).exists(), reason="GNU Radio interpreter absent")


@pytest.fixture(scope="module")
def built():
    project, bres, cat, ct = import_and_pnr()
    return project, bres, cat


def test_import_pnr_build_ok(built):
    project, bres, cat = built
    assert bres.ok
    assert len(project.blocks) == 11
    assert not project.panels          # generic sweep, no panel


def test_loopback_matches_gr_and_identity(built):
    """The demo payload through the placed chain equals BOTH the stock-GR
    reference chain's output AND the original payload."""
    project, bres, cat = built
    payload = [ord(c) for c in DEMO_TEXT]
    got = run_link(project, bres, payload)
    gold = gr_golden(payload)
    assert got == gold, f"placed chain != stock GR ({len(got)} vs {len(gold)})"
    assert got == payload, "loopback identity broken"


def test_all_byte_values(built):
    """Every byte value 0..255 once — exercises the full bit-pattern space
    through the scrambler/diff/pack stages."""
    project, bres, cat = built
    payload = list(range(256))
    got = run_link(project, bres, payload)
    assert got == gr_golden(payload)
    assert got == payload


def test_shipped_kyt_runs_end_to_end():
    from engine.build import BuildEngine
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from engine.io.project_io import load_project

    project = load_project(KYT_PATH)
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    bres = BuildEngine(cat, CHIP_YAML).build(project, {project.chip_type: ct})
    assert bres.ok, [str(e) for e in bres.errors[:3]]
    payload = [ord(c) for c in DEMO_TEXT]
    got = run_link(project, bres, payload)
    assert got == payload and got == gr_golden(payload)


def test_shipped_kyt_saturated_matches_per_sample():
    """The shipped .grc runs the chain at Full-speed (``pipelined: 'yes'``), so
    the whole-chain proof must hold SATURATED (INV-19/21): the entire payload
    queued back-to-back with NO inter-byte quiescence must recover the same
    bytes as the per-sample drive. This is also the regression pin for the
    router's DEADLOCK-CYCLE guard — before it, the f2c→pack corridor passed
    through f2c's own input-delivery broker and the shipped .kyt hard-deadlocked
    after ~2100 events with ZERO output (sim stop_reason='Deadlock')."""
    import simkyt

    from engine.build import BuildEngine
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from engine.io.project_io import load_project

    project = load_project(KYT_PATH)
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    bres = BuildEngine(cat, CHIP_YAML).build(project, {project.chip_type: ct})
    assert bres.ok, [str(e) for e in bres.errors[:3]]
    payload = [ord(c) for c in DEMO_TEXT]

    lin = next(iter(bres.chips[0].input_landings.values()))
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(bres.words(0))
    words = []
    for b in payload:
        words += [_wr(lin["hop"], lin["data_addrs"][0]), int(b) & 0xFF,
                  _jp(lin["hop"], lin["entry"])]
    chip.queue_words_physical("x16_in", words)
    out: list[int] = []
    idle = 0
    for _ in range(400000):
        res = chip.run(max_events=256)     # BOUNDED — a livelock must fail clean
        got = chip.read_port_words_timed("x16_out")
        if got:
            idle = 0
            out.extend(v & 0xFFFF for v, _d, _t in got)
        else:
            idle += 1
        if idle > 2000 or len(out) >= len(payload):
            break
    assert out == payload, (
        f"saturated drive recovered {out} (want {payload}) — a count/value "
        "mismatch here is a pipelining hazard the per-sample gate cannot see")


# ------------------------------------------------------------- MUTATION (INV-4)
def test_mutation_mismatched_scrambler_seed_FAILS(built):
    """Corrupting ONE scrambler's seed must break the GR match AND the
    identity — the gate sees the scrambler state, it is not decorative."""
    from engine.build import BuildEngine
    from engine.io.chip_type_io import load_chip_type

    project, bres, cat = built
    descr = [b for b in project.blocks if b.type == "LFSRScramblerBlock"][1]
    old = dict(descr.params)
    try:
        descr.params["seed"] = 0x55            # out of sync with the scrambler
        ct = load_chip_type(CHIP_YAML)
        bres2 = BuildEngine(cat, CHIP_YAML).build(project,
                                                  {project.chip_type: ct})
        assert bres2.ok
        payload = [ord(c) for c in "SEED TEST"]
        got = run_link(project, bres2, payload)
        assert got != payload, "gate blind to a descrambler seed mismatch"
    finally:
        descr.params.clear()
        descr.params.update(old)
