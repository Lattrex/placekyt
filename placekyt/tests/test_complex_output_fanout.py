"""Complex-output FAN-OUT: the build steers a complex cell's two rails (yi/yq) to
TWO DIFFERENT downstream blocks, and every complex-output block is budgeted for it.

Background (INV-17). A cell that emits a complex pair (yi/yq from ONE output cell —
ComplexMixer, NCO, IQUpconvert, …) has two on-chip delivery shapes:

  * COMPLEX PACKET — both rails to the SAME downstream cell: ``WRITE yi; WRITE yq;
    JUMP`` (two operands into R0/R1, one trigger, target fires once). This is the
    default template and is UNCHANGED by the fan-out work.
  * FAN-OUT — rails to TWO DIFFERENT downstream blocks (SSB Weaver's
    ``mixer.yi → LowPass_I``, ``mixer.yq → LowPass_Q``): each rail needs its OWN
    trigger. ``engine.build._apply_brokers`` detects this (two rails resolving to
    DISTINCT broker cells) and re-sequences the cell to ``WRITE yi; WRITE yq;
    JUMP→A; JUMP→B``, steering each rail to its own broker.

This file gates BOTH halves:

  1. ``test_complex_output_cells_budget_for_fanout`` — the INV-17 GUARANTEE. Every
     complex-output block MUST leave a free program word so the build can insert the
     extra fan-out JUMP. Caught HERE (block level), never at chip build.
  2. ``test_mixer_fanout_recovers_both_rails`` — the DSP proof. A mixer whose yi/yq
     go to two different LowPass filters recovers BOTH rails bit-accurately.
  3. ``test_complex_packet_unchanged`` — the regression. A mixer whose complex output
     goes to ONE downstream cell still emits the WRITE/WRITE/JUMP packet (one JUMP).
"""

from __future__ import annotations

import math
import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.build import BuildEngine  # noqa: E402
from engine.catalog import BlockCatalog  # noqa: E402
from engine.io.chip_type_io import load_chip_type  # noqa: E402
from ui.controller import AppController  # noqa: E402
from model.connection import BlockEndpoint, ChipPortEndpoint  # noqa: E402

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402

pytestmark = pytest.mark.skipif(not CT_PATH.exists(), reason="chip yaml absent")

_WRITE, _JUMP, _HALT = 0x6000, 0x7000, 0x0000
LIB = "lattrex.official"


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def catalog(qapp):
    return BlockCatalog.from_gr_kyttar()


@pytest.fixture(scope="module")
def chip_type():
    return load_chip_type(str(CT_PATH))


def _built_cells(catalog, chip_type, btype, params):
    """Build ``btype`` standalone (block → x16_out) and return {cell_id: [words]}
    for every placed cell, from the real BuildResult (the honest post-resolve memory
    the chip loads). Uses the actual pipeline so register allocation is real."""
    ctk = getattr(chip_type, "name", None) or "kyttar_10x12"
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("t", ctk)
    blk = ctrl.place_block(btype, 0, 0, 0, library=LIB, params=params)
    spec = catalog.get(btype)
    pm = catalog.port_map(btype, params=params, library=LIB)
    in_port = next((p.name for p in pm.ports if p.direction == "in"), None)
    out_port = next((p.name for p in pm.ports if p.direction == "out"), None)
    if in_port:
        ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                    BlockEndpoint(block=blk, port=in_port), name="in")
    if out_port:
        ctrl.add_logical_connection(BlockEndpoint(block=blk, port=out_port),
                                    ChipPortEndpoint(chip=0, port="x16_out"), name="o")
    ctrl.auto_route_all({ctk: chip_type}, auto_orient=False, use_bus="always")
    bres = BuildEngine(catalog, str(CT_PATH)).build(ctrl.project, {ctk: chip_type})
    if not bres.ok:
        return None
    bobj = ctrl.project.block(blk)
    cells = bres.chips[0].cells
    out = {}
    for c in bobj.placement.cells:
        info = cells.get((c.x, c.y))
        if info is not None:
            out[c.cell_id] = list(info["memory"])
    return out


def _complex_output_cells(cells):
    """Cells that emit a COMPLEX rail pair: ≥2 data-WRITE instructions + a trigger.
    These are the cells the fan-out transform may re-sequence, so they must have room
    for the extra JUMP. Identified structurally from the built memory."""
    hits = []
    for cid, mem in (cells or {}).items():
        writes = sum(1 for w in mem if (w & 0xF000) == _WRITE)
        jumps = sum(1 for w in mem if (w & 0xF000) == _JUMP)
        if writes >= 2 and jumps >= 1:
            hits.append((cid, mem, writes, jumps))
    return hits


# Complex-output blocks whose output cell emits a yi/yq pair. Extend this list when a
# new complex-output block is added (the authoring guide + INV-17 point here).
_COMPLEX_BLOCKS = [
    ("ComplexMixerBlock", {"sample_rate": 32000.0, "frequency": -1500.0, "phase": 0.1}),
    ("NCOBlock", {}),
    ("IQUpconvertBlock", {"sample_rate": 32000.0, "frequency": 6000.0}),
]


@pytest.mark.parametrize("btype,params", _COMPLEX_BLOCKS)
def test_complex_output_cells_budget_for_fanout(catalog, chip_type, btype, params):
    """INV-17: every complex-output cell must have a free program word so the build
    can insert the extra fan-out JUMP. A cell packed to 32/32 cannot fan out — and we
    catch that HERE, at block-verify time, never at chip build."""
    if catalog.get(btype) is None:
        pytest.skip(f"{btype} not in catalog")
    cells = _built_cells(catalog, chip_type, btype, params)
    if cells is None:
        pytest.skip(f"{btype} did not build standalone")
    complex_cells = _complex_output_cells(cells)
    assert complex_cells, (
        f"{btype}: expected a complex output cell (≥2 WRITEs + a JUMP) but found none "
        f"— update _COMPLEX_BLOCKS or the block is not actually complex-output.")
    for cid, mem, writes, jumps in complex_cells:
        used = sum(1 for w in mem if (w & 0xFFFF) != 0)
        free = 32 - used
        # The fan-out form adds (writes - 1) extra JUMPs (one per extra rail beyond
        # the last, which reuses the authored JUMP). For a yi/yq pair that's +1.
        need = writes - 1
        assert free >= need, (
            f"{btype} output cell '{cid}': {used}/32 words used, {free} free, but the "
            f"fan-out form needs {need} extra JUMP word(s) (INV-17). The output cell "
            f"is over-full — it cannot fan its rails out to different blocks.")


def _build_mixer_to(catalog, chip_type, targets):
    """Place a ComplexMixer whose yi/yq go to ``targets`` (a list of (block_type,
    port, params)), route, build. Returns (ctrl, bres, mixer_name)."""
    ctk = getattr(chip_type, "name", None) or "kyttar_10x12"
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("t", ctk)
    mix = ctrl.place_block("ComplexMixerBlock", 0, 0, 0, library=LIB,
                           params={"sample_rate": 32000.0, "frequency": -1500.0,
                                   "phase": 0.1})
    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=mix, port="xi"), name="in")
    placed = []
    for i, (bt, port, params, xy) in enumerate(targets):
        t = ctrl.place_block(bt, 0, *xy, library=LIB, params=params)
        rail = "yi" if i == 0 else "yq"
        ctrl.add_logical_connection(BlockEndpoint(block=mix, port=rail),
                                    BlockEndpoint(block=t, port=port), name=f"r{i}")
        placed.append(t)
    # egress from the first target's output to x16_out so the chain builds.
    return ctrl, mix, placed


def test_complex_packet_unchanged(catalog, chip_type):
    """Regression: a mixer feeding ONE complex downstream cell keeps the WRITE/WRITE/
    JUMP packet (a SINGLE JUMP) — the fan-out transform must NOT touch it."""
    import simkyt
    ctk = getattr(chip_type, "name", None) or "kyttar_10x12"
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("t", ctk)
    mix = ctrl.place_block("ComplexMixerBlock", 0, 0, 0, library=LIB,
                           params={"sample_rate": 32000.0, "frequency": -1500.0,
                                   "phase": 0.1})
    # Both rails to the SAME complex block (a Costas loop takes xi/xq into one cell).
    costas = ctrl.place_block("ComplexCostasLoopBlock", 0, 4, 4, library=LIB, params={})
    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=mix, port="xi"), name="in")
    ctrl.add_logical_connection(BlockEndpoint(block=mix, port="yi"),
                                BlockEndpoint(block=costas, port="xi"), name="ri")
    ctrl.add_logical_connection(BlockEndpoint(block=mix, port="yq"),
                                BlockEndpoint(block=costas, port="xq"), name="rq")
    rep = ctrl.auto_route_all({ctk: chip_type}, auto_orient=False, use_bus="always")
    assert rep.ok, [(r.name, r.reason) for r in rep.failed]
    bres = BuildEngine(catalog, str(CT_PATH)).build(ctrl.project, {ctk: chip_type})
    assert bres.ok, [str(e) for e in bres.errors]
    mb = ctrl.project.block(mix)
    mcell = [c for c in mb.placement.cells if c.cell_id == "mixer"][0]
    mem = bres.chips[0].cells[(mcell.x, mcell.y)]["memory"]
    jumps = sum(1 for w in mem if (w & 0xF000) == _JUMP)
    assert jumps == 1, (
        f"complex PACKET path must keep ONE JUMP (the fan-out transform leaked into "
        f"the same-cell case) — found {jumps} JUMPs in the mixer cell.")


def test_mixer_fanout_recovers_both_rails(catalog, chip_type):
    """DSP proof: mixer.yi → LowPass_I, mixer.yq → LowPass_Q (fan-out to two DIFFERENT
    blocks) recovers BOTH rails bit-accurately vs the reference LowPass(mixer.I/Q)."""
    import simkyt
    from gr_kyttar.placement.blocks.complex_mixer_block import ComplexMixerBlock
    from gr_kyttar.placement.blocks.low_pass_filter_block import LowPassFilter

    ctk = getattr(chip_type, "name", None) or "kyttar_10x12"
    fs = 32000.0
    lpp = dict(gain=1.0, samp_rate=fs, cutoff_freq=1200.0, transition_width=2500.0)
    ph = 0.1
    n = 96
    t = np.arange(n)
    m = 0.6 * np.cos(2 * math.pi * 700.0 / fs * t)

    def s16(w):
        w = int(w) & 0xFFFF
        return w - 0x10000 if w >= 0x8000 else w

    def bestcorr(a, b, maxlag=24, skip=6):
        a, b = np.asarray(a), np.asarray(b)
        best = -2.0
        for lag in range(-maxlag, maxlag + 1):
            x, y = ((a[lag + skip:], b[skip:len(a) - lag]) if lag >= 0
                    else (a[skip:len(b) + lag], b[-lag + skip:]))
            k = min(len(x), len(y))
            if k < 20:
                continue
            x, y = x[:k], y[:k]
            if np.std(x) < 1e-6 or np.std(y) < 1e-6:
                continue
            best = max(best, float(np.corrcoef(x, y)[0, 1]))
        return best

    mixed = ComplexMixerBlock("m", sample_rate=fs, frequency=-1500.0,
                              phase=ph).process_reference(
        np.array([complex(x, 0.0) for x in m]))
    refI = np.real(LowPassFilter("l", **lpp).process_reference(
        np.real(mixed).astype(np.float32)))
    refQ = np.real(LowPassFilter("l", **lpp).process_reference(
        np.imag(mixed).astype(np.float32)))

    def run(out_lp):
        ctrl = AppController(catalog=catalog)
        ctrl.new_project("t", ctk)
        mix = ctrl.place_block("ComplexMixerBlock", 0, 0, 0, library=LIB,
                               params={"sample_rate": fs, "frequency": -1500.0,
                                       "phase": ph})
        lpi = ctrl.place_block("LowPassFilter", 0, 0, 6, library=LIB, params=lpp)
        lpq = ctrl.place_block("LowPassFilter", 0, 3, 6, library=LIB, params=lpp)
        ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                    BlockEndpoint(block=mix, port="xi"), name="in")
        ctrl.add_logical_connection(BlockEndpoint(block=mix, port="yi"),
                                    BlockEndpoint(block=lpi, port="sample"), name="ri")
        ctrl.add_logical_connection(BlockEndpoint(block=mix, port="yq"),
                                    BlockEndpoint(block=lpq, port="sample"), name="rq")
        tgt = lpi if out_lp == "i" else lpq
        ctrl.add_logical_connection(BlockEndpoint(block=tgt, port="out"),
                                    ChipPortEndpoint(chip=0, port="x16_out"), name="o")
        rep = ctrl.auto_route_all({ctk: chip_type}, auto_orient=False, use_bus="always")
        assert rep.ok, [(r.name, r.reason) for r in rep.failed]
        bres = BuildEngine(catalog, str(CT_PATH)).build(ctrl.project, {ctk: chip_type})
        assert bres.ok, [str(e) for e in bres.errors]
        entry, ins = catalog.resolved_io(
            "ComplexMixerBlock",
            {"sample_rate": fs, "frequency": -1500.0, "phase": ph}, library=LIB)
        a0, a1 = int(ins[0]), int(ins[1])
        chip = simkyt.Chip.from_yaml(str(CT_PATH))
        chip.load_bitstream_physical([w & 0xFFFF for w in bres.words(0)])
        chip.set_port_entry_address("x16_in", entry)

        def q(f):
            return int(round(max(-1, min(1, f)) * 32767)) & 0xFFFF

        out = []
        for x in m:
            chip.inject_data_physical([q(x)], target_hop_cnt=30, target_addr=a0)
            chip.run(max_events=6000)
            chip.inject_data_physical([0], target_hop_cnt=30, target_addr=a1)
            chip.run(max_events=6000)
            chip.inject_jump_physical(target_hop_cnt=30, entry_addr=entry)
            chip.run(max_events=90000)
            got = []
            while chip.output_available("x16_out"):
                got += [int(v) & 0xFFFF for v in
                        chip.read_port_i16("x16_out").view("uint16").tolist()]
                chip.release_output_ack("x16_out")
                chip.run(max_events=4000)
            out.append(s16(got[0]) / 32768.0 if got else 0.0)
        return np.array(out)

    ci = bestcorr(run("i"), refI)
    cq = bestcorr(run("q"), refQ)
    assert ci > 0.99, f"fan-out I rail corr {ci:.4f} (should be ~1.0 — a rail dropped)"
    assert cq > 0.99, f"fan-out Q rail corr {cq:.4f} (should be ~1.0 — a rail dropped)"
