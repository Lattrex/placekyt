# SPDX-License-Identifier: GPL-3.0-or-later
"""CW FULL TRANSCEIVER — WHOLE-CHAIN end-to-end gate (AGENTS.md §5b).

TX (chars → SRAM Morse keyer → ITU-R keyed envelope) and RX (keyed audio →
Abs → the STREAMING fixed-unit SRAM Morse decoder [LUT at addr_base 16384])
duplex on ONE chip, SHARING ONE panel (the kicker-form duplex template: the
TX crossover keeps its completion track_c; the RX egress rides col 1 + row 2
onto its own colxo crossing; taps/return are standard build brokers).

Gates: TX BIT-EXACT vs the keyer golden while RX runs interleaved; RX decodes
the sent letters EXACTLY (== the streaming golden); the alphabet round-trips;
shipped-.kyt parity; mutation (a corrupted LUT word must corrupt exactly that
character). DOCUMENTED v1 limits under test: no spaces (letters compared), an
EOT blip terminates an RX burst.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[2]
_EX = _ROOT / "examples" / "cw_transceiver"
for _p in (str(_ROOT / "runtime" / "python"), str(_ROOT / "placekyt"),
           str(_EX)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cw_transceiver_demo import (  # noqa: E402
    CHIP_YAML, KYT_PATH, RX_TEXT, TX_TEXT, UNIT, keyed_envelope,
    import_and_pnr, run_duplex)


@pytest.fixture(scope="module")
def built():
    return import_and_pnr()


def test_import_pnr_build_ok(built):
    project, bres, cat, ct = built
    assert bres.ok
    assert len(project.panels) == 1
    img = project.panels[0].image
    # the offset Morse LUT (36 chars + the space seed) + the keyer ROM
    assert sum(1 for a in img if a >= 16384) == 37
    assert sum(1 for a in img if a < 16384) > 100
    assert project.panels[0].auto_inc_read      # the keyer's record streaming


def test_duplex_tx_exact_and_rx_decodes(built):
    project, bres, cat, ct = built
    tx, rx = run_duplex(project, bres, TX_TEXT, RX_TEXT)
    assert tx == keyed_envelope(TX_TEXT), "TX not bit-exact vs the ITU golden"
    assert rx == RX_TEXT.replace(" ", ""), f"RX decoded {rx!r}"


def test_rx_matches_streaming_golden(built):
    """The chip's decode equals the block's streaming reference model on the
    same burst (golden agreement, not just message recovery)."""
    from engine.catalog import BlockCatalog
    from cw_transceiver_demo import rx_burst, _s16

    project, bres, cat, ct = built
    text = "HELLO 73"
    _tx, rx = run_duplex(project, bres, "E", text)
    envf = [_s16(v) / 32768.0 for v in rx_burst(text)]
    gold = BlockCatalog.from_gr_kyttar().instantiate(
        "CWDecoderBlock", "g",
        {"unit_samples": UNIT}).process_reference_streaming(envf)
    assert rx == gold == text.replace(" ", "")


def test_alphanumerics_roundtrip(built):
    """Every ITU-R M.1677 letter+digit keys and decodes through the shared
    panel — the whole reverse LUT exercised in situ."""
    project, bres, cat, ct = built
    text = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    _tx, rx = run_duplex(project, bres, "E", text)
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
    assert tx == keyed_envelope(TX_TEXT)
    assert rx == RX_TEXT.replace(" ", "")


# ------------------------------------------------------------- MUTATION (INV-4)
def test_mutation_corrupt_lut_word_FAILS(built):
    """Corrupting ONE reverse-LUT word in the shared panel must corrupt
    exactly that character's decode."""
    from gr_kyttar.placement.blocks.cw_decoder_block import element_id, MORSE

    project, bres, cat, ct = built
    img = dict(project.panels[0].image)
    addr = 16384 + element_id(MORSE["S"])
    assert img.get(addr) == ord("S")
    img[addr] = ord("Z")
    _tx, rx = run_duplex(project, bres, "E", "SOS", panel_image=img)
    assert rx == "ZOZ", f"gate blind to a corrupted LUT word ({rx!r})"
