# SPDX-License-Identifier: GPL-3.0-or-later
"""2P2S with ROUTED head taps — the full multiplex-through-ports-across-chips proof.

The companion to test_2p2s_plumbing (which uses an AT-LANDING head at (0,0)). Here
each chip's head gain is placed AWAY from the input landing cell — a gain tapped
off the row-0 through-bus — so the input reaches it via a corridor. This exercises
the routed-input multichip path end to end:

    x16_in ─▶ (corridor) ─▶ [gain @ non-landing] ─▶ (row-0 bus) ─▶ x16_out
        ↑ the inter-chip relay must deliver the crossed value to a ROUTED landing
          (WRITE+JUMP over the configured hop), not a bare at-landing queue.

This is the topology the OFDM examples need (blocks placed for the DSP, not pinned
to the input cell) and the case that was completely unsupported before the
routed-input .so change (write_port_i16 / the relay's write_input_raw only reach an
at-landing block). It is gated by the per-port ROUTED flag
(MultiChipSimEngine.configure_input_port(routed=...)), derived here from the build's
input_landings: routed iff the landing cell != the port cell (0,0).

Two 0.5x gains in series per chain -> 0.25x at each tail; both chains run at once
with distinct stimulus and must not interfere.

Run:
    QT_QPA_PLATFORM=offscreen placekyt/.venv/bin/python -m pytest \
        placekyt/tests/test_2p2s_routed_tap.py -q
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.catalog import BlockCatalog  # noqa: E402
from ui.controller import AppController  # noqa: E402

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402

pytestmark = pytest.mark.skipif(not CT_PATH.exists(), reason="chip yaml absent")

_GRC = (Path(__file__).resolve().parents[2] / "examples" / "gain" / "gain.grc")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def catalog():
    return BlockCatalog.from_gr_kyttar()


def _auto_pnr_gain(catalog):
    """A single gain chip via the REAL path (import gain.grc -> auto-P&R -> build).
    Auto-P&R lands the gain at a ROUTED cell off the input port with a corridor the
    datapath actually executes (a hand-laid add_route corridor builds but does not
    drive — the router synthesizes the working landing). Returns (words, landing).
    All four 2P2S chips are identical, so one build serves every chip."""
    from engine.grc_import import import_grc
    from engine.io.chip_type_io import load_chip_type

    res = import_grc(str(_GRC), catalog, chip_type="kyttar_10x12")
    assert res.ok, res.unknown
    ctrl = AppController(catalog=catalog)
    ctrl.project = res.project
    ct = load_chip_type(str(CT_PATH))
    assert ctrl.auto_pnr({"kyttar_10x12": ct}).ok
    r = ctrl.build()
    assert r.ok, [getattr(e, "category", None) for e in r.errors]
    il = list(r.chips[0].input_landings.values())[0]
    return r.chips[0].words, il


def test_routed_head_lands_off_the_port_cell(qapp, catalog):
    """Sanity: the auto-P&R'd head really is ROUTED — its input landing cell is NOT
    the port cell (0,0). (If it were at (0,0) this would be the at-landing case.)"""
    _words, il = _auto_pnr_gain(catalog)
    assert tuple(il["cell"]) != (0, 0), il


def test_two_routed_chains_relay_independently(qapp, catalog):
    """Both chains, ROUTED head taps, run at once with distinct stimulus; each tail
    = 0.25x of its OWN input, no cross-chain interference. Requires the routed-input
    .so (skips cleanly on an older binary without set_port_input_routed)."""
    import simkyt
    if not hasattr(simkyt.MultiChipSimulation.new("probe", 5.0),
                   "set_port_input_routed"):
        pytest.skip("simkyt .so predates the routed-input relay flag")

    from engine.simulator import MultiChipSimEngine

    words, il = _auto_pnr_gain(catalog)
    hop, entry, a0 = il["hop"], il["entry"], il["data_addrs"][0]
    routed = tuple(il["cell"]) != (0, 0)
    assert routed, il

    ct = str(CT_PATH)
    eng = MultiChipSimEngine({0: ct, 1: ct, 2: ct, 3: ct})
    eng.connect(0, "x16_out", 1, "x16_in")   # chain A
    eng.connect(2, "x16_out", 3, "x16_in")   # chain B
    for cid in range(4):
        eng.load(cid, words, trace=True)
        eng.configure_input_port(cid, "x16_in", entry_addr=entry,
                                 hop_count=hop, data_addr=a0, routed=routed)

    eng.inject(0, "x16_in", [0x4000, 0x2000])   # chain A
    eng.inject(2, "x16_in", [0x6000, 0x1000])   # chain B
    eng.run(None, 600)

    out_a = eng.capture(1, "x16_out")
    out_b = eng.capture(3, "x16_out")
    assert out_a[:2] == [0x1000, 0x0800], out_a
    assert out_b[:2] == [0x1800, 0x0400], out_b
