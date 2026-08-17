# SPDX-License-Identifier: GPL-3.0-or-later
"""Serialize-LOCKED NCO/FM in an AUTO-P&R'd CHAIN — placement invariance (INV-20).

Closes the long-standing INV-20 KNOWN LIMIT ("the unlock corridor is proven
hand-placed but not yet placement-invariant in an auto-routed chain"). Two facts,
both pinned here:

1. The unlock corridor itself IS placement-invariant: the block's cells (incl.
   ``transit_unlock``) transform RIGIDLY under auto-place/orient, and the unlock
   ``WRITE.CFG`` hop is re-derived from the placed geometry by
   ``_apply_internal_feedback`` (config-only branch). The original adjacency-loss
   sightings were the re-fold SET-dedup self-overlap bugs, fixed 2026-07-22
   (``_collides`` cell-count check). Gate: a locked-FM chain runs SATURATED
   bit-exact across ≥3 sampled placements AND a full ``auto_pnr`` pack.

2. The REAL placement-independent residual (found building this gate,
   2026-08-16): a locked block feeding a DOWNSTREAM BLOCK shipped SHIFTED rails —
   ``_patch_complex_packet_last_handoff`` counted the emit cell's lock-clear
   ``WRITE.CFG`` (which sits AFTER the yi/yq rails) as one of the "last N
   WRITEs", steering the CFG word down the data corridor, leaving yi unpatched
   and delivering (yq, 0) to the consumer — silently, in EVERY placement. Fixed
   by skipping config WRITEs in the tail selection (the same skip
   ``_patch_last_write_handoff`` / ``_patch_complex_output_port_handoff``
   already had). The block-consumer cases below FAIL pre-fix (INV-4 proven).
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_HERE = Path(__file__).resolve()
CHIP_YAML = str(_HERE.parents[2] / "placekyt" / "resources" / "chips"
                / "kyttar_10x12.yaml")
_CHIP_OK = Path(CHIP_YAML).exists()

pytestmark = pytest.mark.skipif(not _CHIP_OK, reason="chip yaml absent")

SENS = 1.5707963267948966
GAIN = 0.5
CGAIN = 0.75
_GAIN_Q15 = int(round(GAIN * 32768))

_rng = np.random.default_rng(7)
_XS = _rng.uniform(-0.9, 0.9, 40)
_XS_Q15 = [int(round(v * 32768)) & 0xFFFF for v in _XS]


def _s16(u):
    u = int(u) & 0xFFFF
    return u - 0x10000 if u >= 0x8000 else u


def _fm_ref_pairs():
    """Gain(0.5, MULQ-truncating) -> FM reference, as signed (yi, yq) pairs."""
    from gr_kyttar.placement.blocks.frequency_modulator_block import (  # noqa: PLC0415
        FrequencyModulatorBlock)
    g = [((_s16(w) * _GAIN_Q15) >> 15) & 0xFFFF for w in _XS_Q15]
    fm = FrequencyModulatorBlock("ref", sensitivity=SENS)
    return [(FrequencyModulatorBlock._s16(a), FrequencyModulatorBlock._s16(b))
            for (a, b) in fm.process_reference_q15([_s16(w) / 32768.0
                                                    for w in g])]


def _cgain_ref_pairs(pairs):
    """ComplexGain(0.75) applied to signed (i, q) pairs via the block's own
    Q15-exact reference (MULQ gain/4 + saturating <<2)."""
    from gr_kyttar.placement.blocks.complex_gain_block import (  # noqa: PLC0415
        ComplexGainBlock)
    cg = ComplexGainBlock("ref", gain=CGAIN)
    arr = np.array([(a & 0xFFFF, b & 0xFFFF) for (a, b) in pairs],
                   dtype=np.int64)
    out = cg.process_reference(arr)
    return [(_s16(a), _s16(b)) for (a, b) in out]


def _run_chain_saturated(*, anchors, with_consumer, mode="route"):
    """Place Gain -> FM(locked) [-> ComplexGain] -> x16_out, auto-orient +
    auto-route (or full auto_pnr), build, drive the whole burst SATURATED
    (queue_words_physical), and return the signed x16_out word list."""
    import simkyt  # noqa: PLC0415
    from PySide6.QtWidgets import QApplication  # noqa: PLC0415
    from engine.build import BuildEngine  # noqa: PLC0415
    from engine.catalog import BlockCatalog  # noqa: PLC0415
    from engine.io.chip_type_io import load_chip_type  # noqa: PLC0415
    from engine.port_config import stream_targets  # noqa: PLC0415
    from engine.registry import ChipTypeRegistry  # noqa: PLC0415
    from model.connection import BlockEndpoint, ChipPortEndpoint  # noqa: PLC0415
    from ui.controller import AppController  # noqa: PLC0415

    QApplication.instance() or QApplication([])
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    key = getattr(ct, "name", None) or "kyttar_10x12"
    ctrl = AppController(catalog=cat)
    ctrl.new_project("locked_chain", key)
    (gx, gy), (fx, fy) = anchors[0], anchors[1]
    g = ctrl.place_block("GainBlock", 0, gx, gy, params={"gain": GAIN},
                         library="lattrex.official")
    fm = ctrl.place_block("FrequencyModulatorBlock", 0, fx, fy,
                          library="lattrex.official",
                          params={"sensitivity": SENS, "pipeline_lock": True})
    R = ctrl.add_route
    R(ChipPortEndpoint(chip=0, port="x16_in"),
      BlockEndpoint(block=g, port="sample"), [])
    R(BlockEndpoint(block=g, port="out"), BlockEndpoint(block=fm, port="x"), [])
    if with_consumer:
        (cx, cy) = anchors[2]
        cg = ctrl.place_block("ComplexGainBlock", 0, cx, cy,
                              params={"gain": CGAIN},
                              library="lattrex.official")
        # ONE complex link — the controller synthesises the yq sibling.
        ctrl.add_logical_connection(BlockEndpoint(block=fm, port="yi"),
                                    BlockEndpoint(block=cg, port="xi"),
                                    name="fm2cg")
        R(BlockEndpoint(block=cg, port="yi"),
          ChipPortEndpoint(chip=0, port="x16_out"), [])
        R(BlockEndpoint(block=cg, port="yq"),
          ChipPortEndpoint(chip=0, port="x16_out"), [])
    else:
        R(BlockEndpoint(block=fm, port="yi"),
          ChipPortEndpoint(chip=0, port="x16_out"), [])
        R(BlockEndpoint(block=fm, port="yq"),
          ChipPortEndpoint(chip=0, port="x16_out"), [])
    if mode == "auto_pnr":
        rep = ctrl.auto_pnr({key: ct}, register=False)
    else:
        rep = ctrl.auto_route_all({key: ct}, auto_orient=True,
                                  use_bus="always")
    assert rep.ok, ("chain route failed: "
                    + "; ".join(f"{r.name}:{r.reason}" for r in rep.failed))
    bres = BuildEngine(cat, CHIP_YAML).build(ctrl.project, {key: ct})
    assert bres.ok, [str(e) for e in getattr(bres, "errors", [])][:4]
    for conn in ctrl.project.connections:
        s = getattr(conn, "source", None)
        if s is not None and getattr(s, "port", None) == "x16_in":
            conn.stream_id = "tx"
    reg = ChipTypeRegistry()
    reg.register_file(CHIP_YAML)
    tg = stream_targets(ctrl.project, reg, cat, 0, build_result=bres)["tx"]
    entry, hop, a0 = tg["entry_addr"], tg["hop_count"], tg["data_addrs"][0]
    stream = []
    for w in _XS_Q15:
        stream += [(0x6 << 12) | ((hop & 0x1F) << 5) | (int(a0) & 0x1F),
                   int(w) & 0xFFFF,
                   (0x7 << 12) | ((hop & 0x1F) << 5) | (int(entry) & 0x1F)]
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(bres.words(0))
    chip.queue_words_physical("x16_in", stream)
    # HARNESS SAFETY (INV-19): bounded run, never None.
    chip.run(max_events=max(300000, 20000 * len(stream)))
    return [_s16(v) for (v, _d, _t) in chip.read_port_words_timed("x16_out")]


def _assert_pairs(out, ref, tag):
    n = len(ref)
    assert len(out) >= 2 * n, (
        f"{tag}: {len(out)} words for {n} inputs — the pipeline DROPPED "
        "samples (unlock corridor broken / fan-in starved)")
    got = [(out[2 * k], out[2 * k + 1]) for k in range(n)]
    bad = [k for k in range(n) if got[k] != ref[k]]
    assert not bad, (f"{tag}: saturated chain diverges at pair {bad[0]}: "
                     f"got {got[bad[0]]}, ref {ref[bad[0]]}")


# ≥3 sampled placements + the full auto_pnr pack (the INV-20 gate statement).
_EGRESS_CASES = [
    ("hand_a", [(0, 0), (3, 1)], "route"),
    ("hand_b", [(0, 3), (4, 4)], "route"),
    ("hand_c", [(2, 8), (5, 2)], "route"),
    ("auto_pnr", [(0, 0), (3, 1)], "auto_pnr"),
]


@pytest.mark.parametrize("tag,anchors,mode",
                         _EGRESS_CASES, ids=[c[0] for c in _EGRESS_CASES])
def test_locked_fm_chain_port_egress_saturated(tag, anchors, mode):
    """Gain -> locked FM -> x16_out, saturated, bit-exact vs the composed
    references — across sampled placements and a full auto_pnr pack (the
    placement-invariance half of the gate)."""
    out = _run_chain_saturated(anchors=anchors, with_consumer=False, mode=mode)
    _assert_pairs(out, _fm_ref_pairs(), tag)


_CONSUMER_CASES = [
    ("hand_a", [(0, 0), (2, 1), (6, 3)], "route"),
    ("hand_b", [(0, 3), (3, 4), (7, 5)], "route"),
    ("auto_pnr", [(0, 0), (2, 1), (6, 3)], "auto_pnr"),
]


@pytest.mark.parametrize("tag,anchors,mode",
                         _CONSUMER_CASES, ids=[c[0] for c in _CONSUMER_CASES])
def test_locked_fm_chain_block_consumer_saturated(tag, anchors, mode):
    """Gain -> locked FM -> ComplexGain -> x16_out, saturated, bit-exact vs the
    composed references. THE INV-4 PIN for the WRITE.CFG tail-selection bug:
    pre-fix every one of these delivered (yq, 0) to the consumer (rails shifted
    by the lock-clear WRITE.CFG counted as a data WRITE) — in every placement."""
    out = _run_chain_saturated(anchors=anchors, with_consumer=True, mode=mode)
    _assert_pairs(out, _cgain_ref_pairs(_fm_ref_pairs()), tag)


def test_locked_nco_block_consumer_saturated():
    """Locked NCO -> ComplexGain -> x16_out (the NCO variant of the same emit
    structure), saturated, bit-exact vs the composed references — pins the fix
    for the OTHER block named in the INV-20 gate."""
    import simkyt  # noqa: PLC0415
    from PySide6.QtWidgets import QApplication  # noqa: PLC0415
    from engine.build import BuildEngine  # noqa: PLC0415
    from engine.catalog import BlockCatalog  # noqa: PLC0415
    from engine.io.chip_type_io import load_chip_type  # noqa: PLC0415
    from engine.port_config import stream_targets  # noqa: PLC0415
    from engine.registry import ChipTypeRegistry  # noqa: PLC0415
    from model.connection import BlockEndpoint, ChipPortEndpoint  # noqa: PLC0415
    from ui.controller import AppController  # noqa: PLC0415
    from gr_kyttar.placement.blocks.nco_block import NCOBlock  # noqa: PLC0415

    QApplication.instance() or QApplication([])
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    key = getattr(ct, "name", None) or "kyttar_10x12"
    ctrl = AppController(catalog=cat)
    ctrl.new_project("locked_nco_chain", key)
    params = {"frequency": 0.05, "amplitude": 0.8, "pipeline_lock": True}
    nco = ctrl.place_block("NCOBlock", 0, 2, 1, library="lattrex.official",
                           params=params)
    cg = ctrl.place_block("ComplexGainBlock", 0, 6, 3,
                          params={"gain": CGAIN}, library="lattrex.official")
    R = ctrl.add_route
    R(ChipPortEndpoint(chip=0, port="x16_in"),
      BlockEndpoint(block=nco, port="xi"), [])
    R(ChipPortEndpoint(chip=0, port="x16_in"),
      BlockEndpoint(block=nco, port="xq"), [])
    ctrl.add_logical_connection(BlockEndpoint(block=nco, port="yi"),
                                BlockEndpoint(block=cg, port="xi"),
                                name="nco2cg")
    R(BlockEndpoint(block=cg, port="yi"),
      ChipPortEndpoint(chip=0, port="x16_out"), [])
    R(BlockEndpoint(block=cg, port="yq"),
      ChipPortEndpoint(chip=0, port="x16_out"), [])
    rep = ctrl.auto_route_all({key: ct}, auto_orient=True, use_bus="always")
    assert rep.ok, ("route failed: "
                    + "; ".join(f"{r.name}:{r.reason}" for r in rep.failed))
    bres = BuildEngine(cat, CHIP_YAML).build(ctrl.project, {key: ct})
    assert bres.ok, [str(e) for e in getattr(bres, "errors", [])][:4]
    for conn in ctrl.project.connections:
        s = getattr(conn, "source", None)
        if s is not None and getattr(s, "port", None) == "x16_in":
            conn.stream_id = "tx"
    reg = ChipTypeRegistry()
    reg.register_file(CHIP_YAML)
    tg = stream_targets(ctrl.project, reg, cat, 0, build_result=bres)["tx"]
    entry, hop = tg["entry_addr"], tg["hop_count"]
    a0 = tg["data_addrs"][0]
    a1 = tg["data_addrs"][1] if len(tg["data_addrs"]) > 1 else a0
    n = 32
    stream = []
    for _ in range(n):
        stream += [(0x6 << 12) | ((hop & 0x1F) << 5) | (int(a0) & 0x1F), 0,
                   (0x6 << 12) | ((hop & 0x1F) << 5) | (int(a1) & 0x1F), 0,
                   (0x7 << 12) | ((hop & 0x1F) << 5) | (int(entry) & 0x1F)]
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(bres.words(0))
    chip.queue_words_physical("x16_in", stream)
    chip.run(max_events=max(300000, 20000 * len(stream)))
    out = [_s16(v) for (v, _d, _t) in chip.read_port_words_timed("x16_out")]
    nref = NCOBlock("ref", **{k: v for k, v in params.items()
                              if k != "pipeline_lock"})
    pairs = [( _s16(a), _s16(b)) for (a, b) in
             nref.process_reference_q15([0] * n)]
    _assert_pairs(out, _cgain_ref_pairs(pairs), "nco_consumer")
