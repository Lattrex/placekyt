"""Flagship: the REAL 4-block coherent BPSK RX through auto-P&R.

Import ``coherent_bpsk_rx.grc`` (x16_in → ComplexRRCMatchedFilter →
ComplexCostasLoop → GardnerTimingRecovery → BPSKSlicer → x16_out — FOUR SEPARATE
catalog blocks, the production receiver with the on-chip matched-filter front end)
→ auto-place (serpentine, lead-block input-cell anchor puts the MF's ``head`` cell
ON x16_in) → auto-route (bus/broker, ``use_bus="always"``) → build → drive through
simkyt.

This is the production flagship: the chain that recovers bits from a real ADC-grade
I/Q stream. The lead input-fed block is the RRC MATCHED FILTER (not Costas — the MF
front end is on-chip here), so its ``head`` input cell anchors on the port and the
MF→Costas yi/yq complex tap fans into the Costas phase cell through the bus broker.

What this pins:
  * import + auto-P&R routes ALL SEVEN nets (I/Q ingress, MF→Costas yi/yq,
    Costas→Gardner, Gardner→Slicer, Slicer→egress);
  * the design builds into a loadable bitstream and the Gardner feedback survives;
  * simkyt END-TO-END BER 0 (the acceptance gate) in ``test_flagship_ber``.

(The same chain is also gated programmatically — place_block + add_route instead of
GRC import — in ``test_production_rx_mf_ber.py``; this test pins the IMPORT path.)
"""

from __future__ import annotations

import math
import os
import random
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.build import BuildEngine  # noqa: E402
from engine.catalog import BlockCatalog  # noqa: E402
from engine.io.chip_type_io import load_chip_type  # noqa: E402
from ui.controller import AppController  # noqa: E402

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402
from tests.conftest import EXAMPLES_DIR  # noqa: E402
GRC = EXAMPLES_DIR / "coherent_bpsk_rx.grc"
pytestmark = pytest.mark.skipif(
    not (CT_PATH.exists() and GRC.exists()), reason="chip yaml / .grc absent")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def catalog(qapp):
    return BlockCatalog.from_gr_kyttar()


@pytest.fixture(scope="module")
def chip_type():
    return load_chip_type(str(CT_PATH))


# --- RRC BPSK burst (carrier + timing offset) — copied from the batch demo ---
def _make_rrc(beta, sps, span):
    n = span * sps
    taps = []
    for i in range(n + 1):
        t = (i - n / 2) / sps
        if abs(t) < 1e-8:
            v = 1 - beta + 4 * beta / math.pi
        elif abs(abs(4 * beta * t) - 1.0) < 1e-8:
            v = (beta / math.sqrt(2)) * (
                (1 + 2 / math.pi) * math.sin(math.pi / (4 * beta))
                + (1 - 2 / math.pi) * math.cos(math.pi / (4 * beta)))
        else:
            num = (math.sin(math.pi * t * (1 - beta))
                   + 4 * beta * t * math.cos(math.pi * t * (1 + beta)))
            den = math.pi * t * (1 - (4 * beta * t) ** 2)
            v = num / den
        taps.append(v)
    e = math.sqrt(sum(v * v for v in taps))
    return [v / e for v in taps]


def _tx_signal(bits, sps=2, beta=0.35, span=6, timing_offset=0.0, amp=0.9):
    syms = [1.0 if b == 0 else -1.0 for b in bits]
    taps = _make_rrc(beta, sps, span)
    up = []
    for s in syms:
        up.append(s)
        up.extend([0.0] * (sps - 1))
    shaped = []
    L = len(taps)
    for n in range(len(up)):
        acc = 0.0
        for k in range(L):
            if 0 <= n - k < len(up):
                acc += taps[k] * up[n - k]
        shaped.append(acc)
    out = []
    for n in range(len(shaped) - 1):
        i = n + int(math.floor(timing_offset))
        frac = timing_offset - math.floor(timing_offset)
        if 0 <= i < len(shaped) - 1:
            out.append(shaped[i] * (1 - frac) + shaped[i + 1] * frac)
        else:
            out.append(shaped[n])
    # Full-scale ADC-grade drive: the on-chip RRC matched filter needs real signal
    # energy (un-normalised RRC samples are tiny and vanish in Q15). Matches the
    # programmatic production-MF BER test.
    pk = max(abs(b) for b in out) or 1.0
    out = [amp * b / pk for b in out]
    return out, syms


def _ber_with_lag(rx, tx, max_lag=20, min_overlap=40):
    best = (10 ** 9, 0, 0)
    for lag in range(0, max_lag + 1):
        a, b = rx[lag:], tx[: len(rx) - lag]
        m = min(len(a), len(b))
        if m < min_overlap:
            continue
        e = sum(1 for i in range(m) if a[i] != b[i])
        e = min(e, m - e)        # inversion tolerant (BPSK 180° ambiguity)
        if e < best[0]:
            best = (e, m, lag)
    return best


def _fq(f):
    return int(round(max(-1.0, min(0.999, f)) * 32768)) & 0xFFFF


def _autopnr(catalog, chip_type):
    """Import → auto-place → auto-route (bus) the flagship. Returns the ctrl + report."""
    ctrl = AppController(catalog=catalog)
    res = ctrl.import_grc(str(GRC), chip_type="kyttar_10x12")
    assert res.ok, res.unknown
    ctrl.auto_place(0)
    rep = ctrl.auto_route_all({"kyttar_10x12": chip_type}, auto_orient=False,
                              use_bus="always")
    return ctrl, rep


def test_flagship_imports_and_routes_all_nets(qapp, catalog, chip_type):
    """4 separate blocks, auto-placed + bus-routed: ALL SEVEN nets route and it
    builds (I/Q ingress ×2, MF→Costas yi/yq ×2, Costas→Gardner, Gardner→Slicer,
    Slicer→egress)."""
    ctrl, rep = _autopnr(catalog, chip_type)
    assert rep.ok, [(r.name, r.reason) for r in rep.failed]
    routed = {r.name for r in rep.routed}
    assert {f"net{i}" for i in range(1, 8)} <= routed, \
        f"all seven nets must route, got {sorted(routed)}"
    # 4 SEPARATE catalog blocks (no fused CoherentRXBlock).
    types = {b.type for b in ctrl.project.blocks}
    assert "ComplexRRCMatchedFilterBlock" in types
    assert "ComplexCostasLoopBlock" in types
    assert "GardnerTimingRecovery" in types
    assert "BPSKSlicerBlock" in types
    assert "CoherentRXBlock" not in types
    bres = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert bres.ok, [str(e) for e in bres.errors]
    assert len(bres.words(0)) > 0


def test_flagship_gui_import_keeps_mf_input_on_port(qapp, catalog, chip_type):
    """The GUI import path runs auto_place THEN auto_route_all(auto_orient=True)
    (the default). The flow-orient pass must NOT push the input-fed LEAD block (the
    RRC matched filter) off the port: the serpentine placer anchored its ``head``
    input cell ON x16_in, and an orientation that slides it off would break I/Q
    ingress and the input flyline. Regression guard for the lead-anchor (issue F)."""
    ctrl = AppController(catalog=catalog)
    res = ctrl.import_grc(str(GRC), chip_type="kyttar_10x12")
    assert res.ok, res.unknown
    ctrl.auto_place(0)
    # GUI default: auto_orient=True.
    rep = ctrl.auto_route_all({"kyttar_10x12": chip_type})  # auto_orient defaults True
    mf = ctrl.project.block("complexrrcmatchedfilter")
    head = mf.placement.cell("head")
    port = chip_type.port("x16_in")
    assert (head.x, head.y) == (port.cell_x, port.cell_y), \
        f"MF input cell must stay on x16_in {(port.cell_x, port.cell_y)}, " \
        f"got {(head.x, head.y)}"
    # And the I/Q ingress nets must route as a consequence (the two x16_in nets are
    # direct port injections, left unrouted; the MF→Costas yi/yq nets must route).
    routed = {r.name for r in rep.routed}
    assert len(routed) >= 5, \
        f"forward nets must route once the MF stays anchored, got {sorted(routed)}"


def test_flagship_gardner_feedback_survives_in_build(qapp, catalog, chip_type):
    """In the auto-P&R build, the Gardner loop_filter's `period_fb` WRITE survives
    with a non-trivial hop (the dual-face fix kept the feedback while `out`
    egresses to the slicer bus)."""
    ctrl, _ = _autopnr(catalog, chip_type)
    bres = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert bres.ok, [str(e) for e in bres.errors]
    g = ctrl.project.block("gardnertimingrecovery")
    lf = g.placement.cell("loop_filter")
    mem = bres.chips[0].cells[(lf.x, lf.y)]["memory"]
    # period_fb WRITE dest = resampler period reg; with a feedback hop in (0, 31).
    fbs = [(a, w) for a, w in enumerate(mem)
           if (w & 0xF000) == 0x6000]
    # The cell emits exactly two real WRITEs: period_fb (feedback) and out (egress).
    assert len(fbs) >= 2, "loop_filter must emit period_fb AND out"
    hop_cnts = [(w >> 5) & 0x1F for _a, w in fbs]
    assert all(h < 31 for h in hop_cnts), \
        f"a loop_filter WRITE was collapsed to @0: {hop_cnts}"


def test_flagship_ber(qapp, catalog, chip_type):
    """End-to-end acceptance: drive an RRC BPSK burst (carrier+timing offset) through
    the auto-P&R'd PRODUCTION chain and recover bits at BER 0 (lag-aligned,
    inversion-tolerant).

    The REAL 4-separate-block coherent RX (on-chip RRC matched filter →
    ComplexCostasLoop → GardnerTimingRecovery → BPSKSlicer), imported from GRC,
    auto-placed + bus/broker-routed + built, recovers bits at BER 0 through simkyt.
    The lead block is the RRC matched filter: the burst is injected at its ``head``
    cell (entry resolved from ComplexRRCMatchedFilterBlock), and the MF→Costas yi/yq
    complex tap is delivered as one multi-WRITE + single-trigger burst through the
    bus broker. (The same chain is gated programmatically in
    ``test_production_rx_mf_ber.py``; this pins the GRC-import path.)"""
    import simkyt

    ctrl, rep = _autopnr(catalog, chip_type)
    assert rep.ok, [(r.name, r.reason) for r in rep.failed]
    bres = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert bres.ok, [str(e) for e in bres.errors]

    # The lead block (the RRC matched filter) is the injection target on x16_in.
    entry, _ins = catalog.resolved_io("ComplexRRCMatchedFilterBlock")
    random.seed(5)
    nsym, foff, toff = 160, 0.008, 0.45
    bits = [random.randint(0, 1) for _ in range(nsym)]
    sig, syms = _tx_signal(bits, timing_offset=toff)  # full-scale ADC-grade drive
    k = np.arange(len(sig))
    iq = (np.asarray(sig) * np.exp(1j * 2 * np.pi * foff * k)).astype(np.complex64)

    chip = simkyt.Chip.from_yaml(str(CT_PATH))
    chip.load_bitstream_physical(bres.words(0))
    chip.set_port_entry_address("x16_in", entry)

    rx = []
    for n in range(len(sig)):
        chip.inject_data_physical([_fq(float(iq[n].real))], target_hop_cnt=30,
                                  target_addr=0)
        chip.run(max_events=6000)
        chip.inject_data_physical([_fq(float(iq[n].imag))], target_hop_cnt=30,
                                  target_addr=1)
        chip.run(max_events=6000)
        chip.inject_jump_physical(target_hop_cnt=30, entry_addr=entry)
        chip.run(max_events=90000)
        while chip.output_available("x16_out"):
            w = chip.read_port_i16("x16_out").view("uint16").tolist()
            rx.append(int(w[-1]) & 1)
            chip.release_output_ack("x16_out")
            chip.run(max_events=4000)

    tx = [0 if s > 0 else 1 for s in syms]
    e, m, lag = _ber_with_lag(rx, tx)
    ber = (e / m) if m else 1.0
    assert m and e == 0, f"BER={ber:.4f} ({e}/{m}, lag={lag}); {len(rx)} bits"


def test_flagship_no_stray_execution(qapp, catalog, chip_type):
    """No UNPROGRAMMED cell may execute during a run (regression for the (5,1) stray
    exec). A folded block's in-program face constant (e.g. Gardner loop_filter's
    `face_fb`) must be transformed by the block's orientation; if it isn't, a
    feedback WRITE fires the wrong way and lands on an EMPTY cell, which then
    `exec_tick`s on garbage. The build/sim still recovered BER 0 (the feedback was
    just lost), so only a trace check catches it.

    With the §1.4 UNIVERSAL routing-cell program (Reading B) EVERY routing cell is
    now programmed, so "every exec_tick cell is programmed" alone is weaker. So we
    ALSO assert the load-bearing pass-through property: a PLAIN TRANSIT spine cell
    (one carrying ONLY the universal program) must NEVER exec_tick — a transiting
    (HOP<31) word is forwarded on its fwd_face and must not fire any entry. If a
    transit cell mis-executes, the universal program broke pass-through (the core
    builds≠computes hazard) — this catches it directly."""
    import simkyt
    from engine.simulator import SimulationEngine
    from engine.build import _universal_routing_program

    ctrl, rep = _autopnr(catalog, chip_type)
    assert rep.ok
    bres = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert bres.ok

    # The set of cells that carry a real program (non-empty memory) in the build.
    width = chip_type.width
    cells = bres.chips[0].cells
    programmed = set()
    for (x, y), info in cells.items():
        if any(w for w in info["memory"]):
            programmed.add((x, y))

    # PLAIN TRANSIT cells = those whose memory EXACTLY equals the universal program
    # for some bus face (a broker/crossover appends extra relay/demux entries, so
    # it won't match). These forward transiting words and must NEVER exec_tick.
    univ_sigs = [[_m.get(a, 0) & 0xFFFF for a in range(32)]
                 for _e, _m in (_universal_routing_program(bf) for bf in range(4))]
    transit_cells = {(x, y) for (x, y), info in cells.items()
                     if [w & 0xFFFF for w in info["memory"]] in univ_sigs}
    assert transit_cells, "expected the universal program on the transit spine"

    entry, _ = catalog.resolved_io("ComplexRRCMatchedFilterBlock")
    eng = SimulationEngine(str(CT_PATH))
    eng.load(bres.words(0), trace=True)
    eng.configure_input_port("x16_in", entry_addr=entry, hop_count=30, data_addr=0)
    for _ in range(6):
        eng.chip.inject_data_physical([_fq(0.5)], target_hop_cnt=30, target_addr=0)
        eng.chip.run(max_events=4000)
        eng.chip.inject_data_physical([_fq(0.0)], target_hop_cnt=30, target_addr=1)
        eng.chip.run(max_events=4000)
        eng.chip.inject_jump_physical(target_hop_cnt=30, entry_addr=entry)
        eng.chip.run(max_events=60000)

    exec_cells = {(c % width, c // width)
                  for ev in eng.chip.get_trace()
                  if ev.get("kind") == "exec_tick"
                  and ev.get("cell_id") is not None
                  for c in [ev["cell_id"]]}
    stray = sorted(exec_cells - programmed)
    assert not stray, f"unprogrammed cells executed (stray emit): {stray}"
    # Pass-through: a plain transit cell must NOT consume/execute a transiting word.
    mis = sorted(exec_cells & transit_cells)
    assert not mis, \
        f"plain transit cells mis-executed (pass-through broken): {mis}"
