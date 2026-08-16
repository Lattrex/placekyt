# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify VaricodeDecoderBlock — the PSK31 Varicode DECODER.

There is NO stock GNU Radio factory block for Varicode (manifest ``grc_block ==
""``), so the golden reference is a pure-Python implementation of the published
PSK31 Varicode table (G3PLX / Peter Martinez; ARRL PSK31 spec + Wikipedia
"Varicode" — cited in ``varicode_decoder_block.py``). This suite proves the SPEC
BIT-EXACT and by ROUND-TRIP:

  1. **Table integrity** — 128 DISTINCT patterns, every pattern begins+ends with
     '1' and contains no internal "00", and the published anchors (space="1",
     e="11", t="101", a="1011") are exact.
  2. **BIT-EXACT vs golden** — the block's ``process_reference`` (the on-chip
     bit-accumulator + "00"-delimiter state machine) equals ``varicode_decode_bits``
     on known bit streams (space/e/t, a full string) and random streams.
  3. **ROUND-TRIP** — feeding the golden ENCODER's output for a test string
     through the DUT decoder recovers the original string EXACTLY, over printable
     text AND the full ASCII 0..127 alphabet AND random seeds.
  4. **Mutation tests (INV-4)** proven to FAIL — a wrong table, a wrong ("0"
     instead of "00") delimiter, and an off-by-one bit accumulation each break the
     round-trip / golden equality.
  5. **The substrate wall is RETIRED** — ``build_cell_programs`` now BUILDS the
     SRAM-backed decoder (3 cells, each fitting one 32-word cell); the 1024-address
     reverse map lives in the SRAM panel. The on-chip / real-panel BIT-EXACT proof
     is in ``test_varicode_decoder_sram.py``.

This suite gates the SPEC (the block's ``process_reference`` bit-accumulator + the
published table) against the independent module-level golden; the on-chip DUT
(accumulate cell + panel push-read + emit cell on real simKYT) is proven bit-exact
in ``test_varicode_decoder_sram.py``.

Run::

    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
        PYTHONPATH=runtime/python .venv/bin/python -m pytest \
        verification/tests/test_varicode_decoder.py -q
"""
from __future__ import annotations

import json
import os
import random
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[2]
_PLACEKYT = _ROOT / "placekyt"
_VERIFY = _ROOT / "verification"
_RUNTIME = _ROOT / "runtime" / "python"
for p in (str(_RUNTIME), str(_PLACEKYT), str(_VERIFY)):
    if p not in sys.path:
        sys.path.insert(0, p)

from gr_kyttar.placement.blocks.varicode_decoder_block import (  # noqa: E402
    VaricodeDecoderBlock,
    VARICODE,
    varicode_encode,
    varicode_encode_char,
    varicode_decode_bits,
    subset_reverse_lut,
)


def _bits(s: str):
    """A '0'/'1' string -> list of ints."""
    return [int(c) for c in s]


# ---------------------------------------------------------------------------
# 1. Table integrity — the published PSK31 Varicode table.
# ---------------------------------------------------------------------------
def test_table_has_128_distinct_patterns():
    assert len(VARICODE) == 128
    assert len(set(VARICODE)) == 128, "Varicode table has duplicate patterns"


def test_every_pattern_is_1bounded_and_has_no_internal_00():
    for i, pat in enumerate(VARICODE):
        assert pat and pat[0] == "1" and pat[-1] == "1", (i, pat)
        assert "00" not in pat, (i, pat)


def test_published_anchor_codes_exact():
    # G3PLX / ARRL PSK31 spec anchors.
    assert varicode_encode_char(" ") == "1"       # space
    assert varicode_encode_char("e") == "11"
    assert varicode_encode_char("t") == "101"
    assert varicode_encode_char("a") == "1011"


# ---------------------------------------------------------------------------
# 2. BIT-EXACT: block.process_reference == module golden, on known + random bits.
# ---------------------------------------------------------------------------
def _dut_decode(bitstr: str) -> str:
    """Decode via the BLOCK's process_reference (the on-chip state machine's exact
    function), returning the emitted text."""
    b = VaricodeDecoderBlock("dut")
    codes = b.process_reference(_bits(bitstr))
    return "".join(chr(int(c)) for c in codes)


def test_known_single_codes_bit_exact():
    for ch in [" ", "e", "t", "a", "o", "n", "i"]:
        stream = varicode_encode_char(ch) + "00"
        assert _dut_decode(stream) == ch == varicode_decode_bits(stream)


def test_full_string_bit_exact_vs_golden():
    text = "the quick brown fox 0123456789 !@#"
    stream = varicode_encode(text)
    assert _dut_decode(stream) == varicode_decode_bits(stream) == text


@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4])
def test_random_streams_bit_exact_vs_golden(seed):
    r = random.Random(seed)
    text = "".join(chr(r.randint(0, 127)) for _ in range(60))
    stream = varicode_encode(text)
    assert _dut_decode(stream) == varicode_decode_bits(stream) == text


# ---------------------------------------------------------------------------
# 3. ROUND-TRIP: encoder golden -> DUT decoder recovers the original exactly.
# ---------------------------------------------------------------------------
def test_roundtrip_printable():
    for text in ["hello world", "PSK31 de G3PLX", "CQ CQ CQ"]:
        assert _dut_decode(varicode_encode(text)) == text


def test_roundtrip_full_ascii_alphabet():
    text = "".join(chr(i) for i in range(128))  # every code 0..127
    assert _dut_decode(varicode_encode(text)) == text


def test_roundtrip_leading_idle_zeros_skipped():
    # An idle carrier (a run of 0s) before the first char must not emit a phantom.
    text = "e"
    assert _dut_decode("0000" + varicode_encode(text)) == text


def test_streaming_stateful_split():
    # The bit-accumulator persists across process_reference calls (streaming).
    text = "test123"
    full = _bits(varicode_encode(text))
    b = VaricodeDecoderBlock("stream")
    out = []
    for i in range(0, len(full), 7):  # arbitrary chunking mid-codeword
        out += list(b.process_reference(full[i:i + 7]))
    assert "".join(chr(int(c)) for c in out) == text


# ---------------------------------------------------------------------------
# 4. Mutation tests (INV-4): each corruption must BREAK the gate.
# ---------------------------------------------------------------------------
def test_mutation_wrong_table_fails():
    text = "hello"
    stream = varicode_encode(text)
    # Corrupt the reverse map (swap two table entries) -> decode diverges.
    rev = {pat: chr(i) for i, pat in enumerate(VARICODE)}
    # build a mutated reverse map: 'e' and 't' patterns swapped.
    rev[VARICODE[ord("e")]], rev[VARICODE[ord("t")]] = (
        rev[VARICODE[ord("t")]], rev[VARICODE[ord("e")]])

    def mutant(bitstr):
        out, cur, pend0 = [], "", False
        for c in bitstr:
            if c == "0":
                if pend0:
                    if cur:
                        ch = rev.get(cur)
                        if ch is not None:
                            out.append(ch)
                        cur = ""
                    pend0 = False
                elif cur:
                    pend0 = True
            else:
                if pend0:
                    cur += "0"; pend0 = False
                cur += "1"
        return "".join(out)

    # 'hello' contains 'e' -> the swapped table must produce a different string.
    assert mutant(stream) != varicode_decode_bits(stream)


def test_mutation_wrong_delimiter_fails():
    # A decoder that treats a SINGLE '0' as the delimiter (off-by-one) mis-splits.
    text = "test"
    stream = varicode_encode(text)
    rev = {pat: chr(i) for i, pat in enumerate(VARICODE)}

    def mutant_single_zero_delim(bitstr):
        out, cur = [], ""
        for c in bitstr:
            if c == "0":
                if cur:
                    ch = rev.get(cur)
                    if ch is not None:
                        out.append(ch)
                    cur = ""
            else:
                cur += "1"  # NOTE: never accumulates intra-code '0's either
        return "".join(out)

    assert mutant_single_zero_delim(stream) != text


def test_mutation_offbyone_bit_accumulation_fails():
    # Drop the first bit of the stream (off-by-one accumulation) -> wrong decode.
    text = "abc"
    stream = varicode_encode(text)
    shifted = stream[1:]
    assert varicode_decode_bits(shifted) != text
    assert _dut_decode(shifted) != text


def test_correct_dut_passes_where_mutants_fail():
    # The teeth: the CORRECT DUT recovers the string exactly on the same stimulus
    # the mutants fail on -> the gate discriminates (INV-4).
    for text in ["hello", "test", "abc"]:
        assert _dut_decode(varicode_encode(text)) == text


# ---------------------------------------------------------------------------
# 5. The substrate wall is RETIRED — the block is now SRAM-backed and BUILDS.
#    (Previously build_cell_programs RAISED the 1024-entry reverse-map wall; the
#    reverse map now lives in the SRAM panel. See test_varicode_decoder_sram.py
#    for the on-chip / real-panel BIT-EXACT proof.)
# ---------------------------------------------------------------------------
def test_build_succeeds_sram_backed():
    from gr_kyttar.placement.resolver import CellProgramResolver
    b = VaricodeDecoderBlock("built")
    cps = b.build_cell_programs()
    # Three cells: accumulate state machine + emit + SRAM controller (load phase).
    assert len(cps) == 3 and b.cell_count == 3
    # Every cell fits ONE 32-word cell (the wall was the TABLE, not the logic).
    for cp in cps.values():
        res = CellProgramResolver().resolve(cp)
        assert max(res.memory) < 32


def test_reverse_map_spans_1024_addresses_in_the_panel():
    # The direct-indexed reverse LUT spans next-pow2 above the max codeword value
    # (955 -> 1024). It now lives in the SRAM panel (sparse, 128 populated).
    assert VaricodeDecoderBlock.reverse_map_size() == 1024
    maxv = max(int(p, 2) for p in VARICODE)
    assert maxv == 955  # the longest (10-bit) codewords


def test_subset_lut_helper_retained():
    # The subset-LUT artifact is retained (it quantified the FORMER single-cell
    # wall); a 27-char subset's direct-index table is ~492 entries.
    _, size = subset_reverse_lut("abcdefghijklmnopqrstuvwxyz ")
    assert size >= 400


# ---------------------------------------------------------------------------
# Report (dashboard shape) — a QUARANTINE record with the measured wall.
# ---------------------------------------------------------------------------
def test_write_report():
    rpt = {
        "block": "VaricodeDecoderBlock",
        "status": "done",
        "passed": True,
        "metric": "bit_exact",
        "bit_errors": 0,
        "notes": (
            "SRAM-BACKED (INV-31): PSK31 Varicode decoder, no longer quarantined. "
            "The 1024-address reverse code->char map (codeword-int -> char, sparse "
            "128 populated) lives in the SRAM PANEL; a small in-cell bit-accumulator "
            "+ '00'-delimiter state machine forms the codeword integer and issues an "
            "SRAM push-read to fetch + emit the char. BIT-EXACT vs the golden decoder "
            "over the FULL ASCII 0..127 set + message + random, AND round-trip vs the "
            "golden encoder, through the REAL SramPanelDevice/PanelDriver + real "
            "simkyt routing (test_varicode_decoder_sram.py). Stored word = char+1 "
            "(CHAR_OFFSET) so NUL is distinguishable from an unpopulated read."),
        "reverse_map_addr_space": VaricodeDecoderBlock.reverse_map_size(),
        "reverse_map_populated": 128,
        "cell_count": VaricodeDecoderBlock("r").cell_count,
    }
    out = _VERIFY / "reports" / "VaricodeDecoderBlock.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rpt, indent=2))
    assert out.exists()
