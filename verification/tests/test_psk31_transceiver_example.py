# SPDX-License-Identifier: GPL-3.0-or-later
"""PSK31 FULL TRANSCEIVER — WHOLE-CHAIN end-to-end gate (AGENTS.md §5b).

TX (chars → SRAM Varicode encoder [table at addr_base 1024] → diff → BPSK →
hold ×8 → envelope) and RX (symbols → slicer → diff decode → SRAM Varicode
DECODER [reverse map 1..955]) duplex on ONE chip, SHARING ONE panel — the
first two-client shared-panel design (per-read R3/R4 descriptors, the duplex
corridor template with the three-track crossover).

Gates: TX SAMPLE-EXACT vs the psk31 golden while RX runs interleaved; RX
decodes the sent text EXACTLY; shipped-.kyt parity; disjoint panel regions
enforced (an overlapping addr_base must raise, and a ZERO addr_base — both
tables at 0..127 — is the overlap case); mutation: a corrupted decoder panel
word must corrupt the decode (the gate sees the reverse map).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[2]
_EX = _ROOT / "examples" / "psk31_transceiver"
for _p in (str(_ROOT / "runtime" / "python"), str(_ROOT / "placekyt"),
           str(_EX)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from psk31_transceiver_demo import (  # noqa: E402
    CHIP_YAML, KYT_PATH, RX_TEXT, TX_TEXT, import_and_pnr, run_duplex)


@pytest.fixture(scope="module")
def built():
    return import_and_pnr()


def test_import_pnr_build_ok(built):
    project, bres, cat, ct = built
    assert bres.ok
    assert len(project.panels) == 1
    # BOTH tables merged into the one panel: 128 encoder words at 1024+ and
    # 128 decoder words in 1..955.
    img = project.panels[0].image
    assert sum(1 for a in img if a >= 1024) == 128
    assert sum(1 for a in img if a < 1024) == 128


def test_duplex_tx_exact_and_rx_decodes(built):
    project, bres, cat, ct = built
    tx, rx = run_duplex(project, bres, TX_TEXT, RX_TEXT)
    from psk31_tx_golden import golden_tx_q15
    gold = golden_tx_q15(TX_TEXT, sps=8, amplitude=1.0)
    assert tx == gold, f"TX not sample-exact ({len(tx)} vs {len(gold)})"
    assert rx == RX_TEXT, f"RX decoded {rx!r}"


def test_full_ascii_rx_roundtrip(built):
    """Every printable ASCII char decodes through the shared panel while the
    TX side idles-then-sends — the whole reverse map exercised in situ."""
    project, bres, cat, ct = built
    text = "".join(chr(c) for c in range(32, 91))
    tx, rx = run_duplex(project, bres, "E", text)
    assert rx == text


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
    tx, rx = run_duplex(project, bres, TX_TEXT, RX_TEXT)
    from psk31_tx_golden import golden_tx_q15
    assert tx == golden_tx_q15(TX_TEXT, sps=8, amplitude=1.0)
    assert rx == RX_TEXT


def test_overlapping_tables_raise():
    """addr_base 0 puts the encoder table INSIDE the decoder's address space —
    the shared-panel synthesis must REFUSE it with a named error."""
    from engine.catalog import BlockCatalog
    from engine.errors import PlacementError
    from engine.grc_import import import_grc

    grc = (_EX / "psk31_transceiver.grc").read_text()
    bad = grc.replace("addr_base: '1024'", "addr_base: '0'")
    tmp = Path(os.environ.get("TMPDIR", "/tmp")) / "trx_overlap.grc"
    tmp.write_text(bad)
    cat = BlockCatalog.from_gr_kyttar()
    with pytest.raises(PlacementError, match="OVERLAP"):
        import_grc(str(tmp), cat)


# ------------------------------------------------------------- MUTATION (INV-4)
def test_mutation_corrupt_reverse_map_FAILS(built):
    """Corrupting ONE decoder reverse-map word in the shared panel must corrupt
    the decode — the gate sees the panel contents, not just the plumbing."""
    project, bres, cat, ct = built
    img = dict(project.panels[0].image)
    # 'K' = 75: its codeword address holds 75+1; swap it for 'X'+1.
    from gr_kyttar.placement.blocks.varicode_decoder_block import VARICODE
    addr = int(VARICODE[ord("K")], 2)
    assert img.get(addr) == ord("K") + 1
    img[addr] = ord("X") + 1
    tx, rx = run_duplex(project, bres, "E", "OK", panel_image=img)
    assert rx == "OX", f"gate blind to a corrupted reverse-map word ({rx!r})"
