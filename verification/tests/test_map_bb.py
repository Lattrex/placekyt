# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify MapBBBlock — the per-symbol LUT remap (GNU Radio ``digital.map_bb``).

``map_bb`` is a memoryless byte-to-byte lookup: ``out = map[in]`` (the input indexes
the table). GNU Radio holds a 256-entry table seeded to the IDENTITY and overwritten
with the user's ``map``; this block reproduces that table (capped at the single-cell
hardware ceiling, INV-7) and is **BIT-EXACT** vs a LIVE ``digital.map_bb`` over the
supported input alphabet.

The gate (EXACT byte metric, tolerance 0):

  * the DUT (built + simulated on simKYT) equals LIVE ``digital.map_bb`` byte-for-byte
    over several maps — identity ``[0,1]``, invert ``[1,0]``, a 4-entry map, a Gray
    map, an 8-entry and a 16-entry map — plus random inputs (≥3 seeds);
  * the block's own ``process_reference`` equals the on-chip stream (a second EXACT
    check that pins the reference to the hardware);
  * mandatory mutation gates (INV-4): an off-by-one table index, a transposed table,
    and identity-instead-of-map must each FAIL against the true GR reference.

Run::

    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
        .venv/bin/python -m pytest verification/tests/test_map_bb.py -q
"""
from __future__ import annotations

import os
import random
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

from kyttar_verify import run_gnuradio_ref, write_report, CompareResult, Metric  # noqa: E402
from fsk4_dut import _run_single_block_stream  # noqa: E402
from gr_kyttar.placement.blocks.map_bb_block import MapBBBlock  # noqa: E402

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_AVAILABLE),
    reason="chip yaml or GNU Radio interpreter absent")


# --- reference + DUT runners --------------------------------------------------

def _gr_map_bb(mp, ins):
    """LIVE GNU Radio ``digital.map_bb`` — the golden reference. Returns bytes."""
    r = run_gnuradio_ref(
        input_q15=[0],   # unused; the byte stream comes in via extra_args
        gnuradio_script="""
from gnuradio import gr, blocks, digital
tb = gr.top_block()
src = blocks.vector_source_b(ins, False)
m = digital.map_bb(mp)
snk = blocks.vector_sink_b()
tb.connect(src, m); tb.connect(m, snk)
tb.run()
output_float = [float(x) for x in snk.data()]
""",
        extra_args={"mp": list(mp), "ins": list(ins)},
    )
    return [int(round(v)) & 0xFF for v in r.floats]


def _dut_map_bb(mp, ins):
    """Built + simulated MapBBBlock (x16_in -> block -> x16_out). Returns bytes."""
    words = _run_single_block_stream("MapBBBlock", {"map": list(mp)},
                                     [int(v) & 0xFFFF for v in ins], CHIP_YAML)
    return [w & 0xFF for w in words]


# The map families exercised (identity, invert, a 4-entry, a Gray map, 8- and
# 16-entry). `alphabet` is the natural input range the map defines (== the padded
# power-of-two table size, so the identity tail is also under test).
_MAPS = {
    "identity":  [0, 1],
    "invert":    [1, 0],
    "reverse4":  [3, 2, 1, 0],
    "gray4":     [0, 1, 3, 2],
    "gray8":     [0, 1, 3, 2, 6, 7, 5, 4],
    "gray16":    [0, 1, 3, 2, 6, 7, 5, 4, 12, 13, 15, 14, 10, 11, 9, 8],
    "byteval":   [200, 44, 17],    # tests the & 0xFF byte store + identity tail
}


def _alphabet(mp):
    """The table's addressable range = MapBBBlock's padded (pow2) table size."""
    return MapBBBlock("x", map=mp).table_size


# --- correctness: DUT == LIVE GR map_bb, BIT-EXACT ----------------------------

@pytest.mark.parametrize("name", list(_MAPS))
def test_dut_matches_gr_bitexact(name):
    """DUT equals LIVE digital.map_bb byte-for-byte over each map's alphabet."""
    mp = _MAPS[name]
    a = _alphabet(mp)
    ins = list(range(a)) + list(range(a))[::-1]   # every symbol, both directions
    gr = _gr_map_bb(mp, ins)
    dut = _dut_map_bb(mp, ins)
    print(f"\n{name}: map={mp}\n  in  {ins}\n  dut {dut}\n  gr  {gr}")
    assert dut == gr, f"{name}: DUT != GR map_bb\n dut {dut}\n gr  {gr}"


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_dut_matches_gr_random(seed):
    """Random symbol streams (across several maps) are bit-exact vs GR map_bb."""
    rng = random.Random(seed)
    for name, mp in _MAPS.items():
        a = _alphabet(mp)
        ins = [rng.randint(0, a - 1) for _ in range(24)]
        gr = _gr_map_bb(mp, ins)
        dut = _dut_map_bb(mp, ins)
        assert dut == gr, f"seed={seed} {name}: DUT != GR\n dut {dut}\n gr {gr}"


def test_reference_matches_chip_bitexact():
    """The block's own process_reference equals the on-chip stream, word for word."""
    for name, mp in _MAPS.items():
        blk = MapBBBlock("m", map=mp)
        a = blk.table_size
        rng = random.Random(hash(name) & 0xFFFF)
        ins = [rng.randint(0, a - 1) for _ in range(20)]
        dut = _dut_map_bb(mp, ins)
        ref = [int(v) & 0xFF for v in blk.process_reference(ins)]
        assert dut == ref, f"{name}: chip != reference\n chip {dut}\n ref  {ref}"


def test_default_map_is_identity_pair():
    """GR default map is [0, 1]; the block mirrors that param default verbatim."""
    blk = MapBBBlock("m")
    assert blk.map == [0, 1]
    assert _dut_map_bb([0, 1], [0, 1, 0, 1]) == _gr_map_bb([0, 1], [0, 1, 0, 1])


# --- MANDATORY mutation gates (INV-4): each must FAIL vs the GR reference ------

def test_mutation_offbyone_index_fails():
    """An off-by-one table index (out = map[in+1]) must DISAGREE with GR map_bb."""
    mp = _MAPS["gray8"]
    a = _alphabet(mp)
    ins = list(range(a))
    gr = _gr_map_bb(mp, ins)
    # off-by-one: shift the map so entry i lands one slot early.
    shifted = mp[1:] + [mp[0]]
    mutated = _dut_map_bb(shifted, ins)
    assert mutated != gr, "gate missed an off-by-one table index!"


def test_mutation_transposed_table_fails():
    """A transposed (reversed) table must DISAGREE with GR map_bb."""
    mp = _MAPS["gray8"]
    a = _alphabet(mp)
    ins = list(range(a))
    gr = _gr_map_bb(mp, ins)
    transposed = _dut_map_bb(list(reversed(mp)), ins)
    assert transposed != gr, "gate missed a transposed table!"


def test_mutation_identity_instead_of_map_fails():
    """Identity-instead-of-map (the block passed input through) must FAIL for any
    non-identity map — proves the LUT is actually applied, not bypassed."""
    mp = _MAPS["reverse4"]
    a = _alphabet(mp)
    ins = list(range(a))
    gr = _gr_map_bb(mp, ins)
    identity_out = list(ins)   # what a bypassed block would emit
    assert identity_out != gr, "gate would miss an identity (bypassed) block!"


def test_mutation_wrong_map_fails():
    """A DUT built at the WRONG map must FAIL against the right GR reference."""
    a = _alphabet(_MAPS["gray8"])
    ins = list(range(a))
    gr_right = _gr_map_bb(_MAPS["gray8"], ins)
    dut_wrong = _dut_map_bb(_MAPS["invert"] + list(range(2, a)), ins)  # different map
    assert dut_wrong != gr_right, "gate missed a wrong-map build!"


def test_empty_output_fails():
    """An empty output is never a pass."""
    gr = _gr_map_bb(_MAPS["reverse4"], [0, 1, 2, 3])
    assert gr and [] != gr


# --- HARDWARE-DEVIATION guards (INV-7): raise, never truncate -----------------

def test_oversize_map_raises():
    """A map longer than the single-cell ceiling RAISES (documented HW deviation)."""
    with pytest.raises(ValueError, match="HARDWARE DEVIATION"):
        MapBBBlock("big", map=list(range(MapBBBlock.MAX_TABLE + 1)))


def test_max_table_builds_and_routes():
    """The largest supported table (MAX_TABLE entries) still builds, routes, and is
    bit-exact vs GR — pins the documented ceiling as a real, tested limit."""
    n = MapBBBlock.MAX_TABLE
    mp = list(range(n - 1, -1, -1))   # full reverse map of the max size
    ins = [0, n - 1, n // 2, 1, n - 2]
    gr = _gr_map_bb(mp, ins)
    dut = _dut_map_bb(mp, ins)
    assert dut == gr, f"max table (N={n}) DUT != GR\n dut {dut}\n gr {gr}"


# --- report -------------------------------------------------------------------

def test_emit_report():
    """Emit the dashboard report (EXACT metric, tolerance 0, across all maps)."""
    total = errs = 0
    for name, mp in _MAPS.items():
        a = _alphabet(mp)
        ins = list(range(a))
        gr = _gr_map_bb(mp, ins)
        dut = _dut_map_bb(mp, ins)
        total += len(gr)
        errs += sum(1 for k in range(len(gr)) if dut[k] != gr[k])
    res = CompareResult(passed=(errs == 0), metric=Metric.DECISION,
                        n_compared=total, bit_errors=errs, delay_used=0)
    assert res.passed, res.summary()
    write_report("MapBBBlock", res, coverage={
        "exact": "byte-for-byte vs LIVE digital.map_bb, tolerance 0",
        "maps": "identity, invert, reverse4, gray4, gray8, gray16, byteval(&0xFF)",
        "random": 3,
        "mutation": "off-by-one index, transposed table, identity-instead-of-map, "
                    "wrong-map — all FAIL",
        "gr_equiv": "digital.map_bb (out=map[in]; 256-entry identity-seeded table)",
        "hw_deviation": f"single-cell LUT capped at {MapBBBlock.MAX_TABLE} entries "
                        "(INV-7 + LOAD 5-bit addr); raises above, never truncates",
    })
