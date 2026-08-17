# SPDX-License-Identifier: GPL-3.0-or-later
"""robust_rx example — WHOLE-CHAIN end-to-end gate (AGENTS.md §5b).

Two receiver chains duplexed on ONE chip, driven by the SAME raised-cosine
BPSK burst at foff = 0.18 cyc/sample (far beyond Costas pull-in):

  'rx'  : FLLBandEdge(2, 0.35, 17, 0.1) -> Costas(0.05, order 2) -> slicer
  'ctl' : Costas(0.05, order 2) -> slicer  (NO FLL — the negative control)

The chain topology/params/operating point are test_fll_band_edge.py tier 5
verbatim (which also pins the GR golden competence at GR's own operating
point, INV-26). THIS gate's job is the SHIPPED EXAMPLE: the .grc imports,
auto-places+routes and builds; the SHIPPED .kyt recovers BER 0 on the 'rx'
chain while the 'ctl' chain fails (INV-4: the story's claim has an on-chip
control that CAN fail); and the classic coherent_bpsk_rx receiver fed the
same offset burst also fails (the "old chain dies" claim against the real
shipped artifact it names).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[2]
_EX = _ROOT / "examples" / "robust_rx"
for _p in (str(_ROOT / "runtime" / "python"), str(_ROOT / "placekyt"),
           str(_EX)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from robust_rx_demo import (  # noqa: E402
    CHIP_YAML, CTL_FAIL_BER, KYT_PATH, _jp, _q15, _s16, _wr, chain_ber,
    import_and_pnr, run_streams, stim)

COHERENT_KYT = _ROOT / "examples" / "coherent_bpsk_rx" / "coherent_bpsk_rx.kyt"


@pytest.fixture(scope="module")
def built():
    return import_and_pnr()


@pytest.fixture(scope="module")
def recovered(built):
    project, bres, cat, ctrl = built
    return run_streams(project, bres, cat, ctrl)


def test_import_pnr_build_ok(built):
    project, bres, cat, ctrl = built
    assert bres.ok
    types = sorted(b.type for b in project.blocks)
    assert types == ["BPSKSlicerBlock", "BPSKSlicerBlock",
                     "ComplexCostasLoopBlock", "ComplexCostasLoopBlock",
                     "FLLBandEdgeBlock"]
    from model.connection import ChipPortEndpoint
    sids = {c.stream_id for c in project.connections
            if isinstance(c.source, ChipPortEndpoint)
            and getattr(c, "stream_id", None)}
    assert sids == {"rx", "ctl"}
    # import_and_pnr itself asserts no corridor transits a port cell (the
    # FLL ring port-pinch hazard).


def test_fll_chain_recovers_ber0(built, recovered):
    """The headline: BER 0 through the placed+routed FLL->Costas->slicer at a
    frequency offset 50%% beyond the chip Costas' own pull-in."""
    bits = stim.tx_bits()
    n_want = stim.n_rx_bits()
    assert len(recovered["rx"]) >= n_want - 4, \
        f"short egress {len(recovered['rx'])}/{n_want}"
    assert chain_ber(recovered["rx"], bits) == 0.0


def test_negative_control_costas_only_fails(recovered):
    """INV-4: the same stimulus into the same Costas WITHOUT the FLL must
    fail — proof the BER-0 gate cannot be satisfied by the bounded
    phase/lag/polarity search alone (measured ~0.17 at foff=0.18)."""
    bits = stim.tx_bits()
    n_want = stim.n_rx_bits()
    assert len(recovered["ctl"]) >= n_want - 4, \
        f"short egress {len(recovered['ctl'])}/{n_want}"
    ber = chain_ber(recovered["ctl"], bits)
    assert ber > CTL_FAIL_BER, (
        f"negative control void: Costas-only locks at foff={stim.FOFF} "
        f"(BER {ber})")


def test_shipped_kyt_runs_end_to_end():
    """Drive the SHIPPED .kyt (not a reconstruction): build it and hold the
    same BER-0 + failed-control verdicts."""
    from engine.build import BuildEngine
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from engine.io.project_io import load_project

    project = load_project(KYT_PATH)
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    bres = BuildEngine(cat, CHIP_YAML).build(project, {project.chip_type: ct})
    assert bres.ok, [str(e) for e in bres.errors[:3]]
    out = run_streams(project, bres, cat)
    bits = stim.tx_bits()
    n_want = stim.n_rx_bits()
    assert len(out["rx"]) >= n_want - 4 and len(out["ctl"]) >= n_want - 4
    assert chain_ber(out["rx"], bits) == 0.0
    assert chain_ber(out["ctl"], bits) > CTL_FAIL_BER


def test_old_coherent_rx_fails_on_this_offset():
    """The on-screen story's other half, proven against the REAL artifact it
    names: the shipped coherent_bpsk_rx receiver (MF -> Costas -> Gardner ->
    slicer) fed the SAME foff=0.18 burst cannot recover the bits (its Costas
    is the same loop the 'ctl' chain isolates; at 0.18 nothing downstream can
    fix the unstripped carrier)."""
    import simkyt
    from engine.build import BuildEngine
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from engine.io.project_io import load_project
    from engine.port_config import input_port_config, stream_targets
    from ui.controller import AppController

    if not COHERENT_KYT.exists():
        pytest.skip("coherent_bpsk_rx.kyt absent")
    project = load_project(COHERENT_KYT)
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    bres = BuildEngine(cat, CHIP_YAML).build(project, {project.chip_type: ct})
    assert bres.ok, [str(e) for e in bres.errors[:3]]
    ctrl = AppController(catalog=cat)
    ctrl.project = project
    cfgs = stream_targets(project, ctrl.registry, cat, 0, build_result=bres)
    if cfgs:
        cfg = next(iter(cfgs.values()))
    else:
        _port, cfg1 = input_port_config(project, ctrl.registry, cat, 0,
                                        build_result=bres)
        cfg = {"hop_count": cfg1["hop_count"],
               "entry_addr": cfg1["entry_addr"],
               "data_addrs": [cfg1["data_addr"], cfg1["data_addr"] + 1],
               "out_tag": None}
    x = stim.rx_burst()
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(bres.words(0))
    out = []

    def drain():
        got = chip.read_port_words_timed("x16_out")
        for v, _d, _t in got:
            out.append(_s16(int(v)))
        return bool(got)

    h = int(cfg["hop_count"])
    da = [int(a) for a in cfg["data_addrs"]]
    for c in x:
        chip.queue_words_physical("x16_in", [
            _wr(h, da[0]), _q15(c.real), _wr(h, da[1]), _q15(c.imag),
            _jp(h, int(cfg["entry_addr"]))])
        idle = 0
        for _ in range(120000):
            chip.run(max_events=256)
            idle = 0 if drain() else idle + 1
            if idle > 40:
                break
    idle = 0
    for _ in range(120000):
        chip.run(max_events=256)
        idle = 0 if drain() else idle + 1
        if idle > 400:
            break
    assert len(out) > 200, f"coherent RX produced only {len(out)} words"
    # Gardner decimates to ~1 word/symbol; search both 1- and 2-sps framings
    # with a generous lag window — an unlocked chain fails them all.
    bits = stim.tx_bits()
    ber = min(chain_ber(out, bits, sps=1, max_lag=24),
              chain_ber(out, bits, sps=2, max_lag=24))
    assert ber > CTL_FAIL_BER, (
        f"coherent_bpsk_rx unexpectedly recovers foff={stim.FOFF} "
        f"(BER {ber}) — the robust_rx story would be false")


def test_grc_stimulus_matches_chain_gate_class():
    """The shipped stimulus IS the FLL chain gate's class: full-RC shaping
    (zero ISI at symbol instants), 2 sps, foff 0.18, 600 symbols — pinned so
    a drive-by edit of the .grc stimulus cannot silently change the story."""
    assert (stim.N_SYMS, stim.SPS, stim.ROLLOFF, stim.FOFF, stim.SEED) == \
        (600, 2, 0.35, 0.18, 5)
    x = stim.rx_burst()
    assert len(x) == 1200
    import numpy as np
    arr = np.array(x)
    assert np.max(np.abs(arr)) <= 0.9 + 1e-9
    # carrier really present: the spectrum's center of mass sits near foff
    f = np.fft.fftfreq(len(arr))
    p = np.abs(np.fft.fft(arr)) ** 2
    centroid = float(np.sum(f * p) / np.sum(p))
    assert abs(centroid - stim.FOFF) < 0.03
