# SPDX-License-Identifier: GPL-3.0-or-later
"""VaricodeDecoderBlock — SRAM-backed, verified BIT-EXACT through the REAL panel.

The SRAM-backed PSK31 Varicode DECODER (INV-31), built with the SAME topology the
SRAM VaricodeEncoder proved. The 1024-address reverse code->char map that
QUARANTINED the single-cell design (INV-29: needs ~1024 entries vs the 21-entry
LOAD-table ceiling) now lives in the SRAM PANEL (address == codeword INTEGER value,
stored word == char + CHAR_OFFSET). This suite is the PROOF the SRAM path unblocks
the decoder:

  1. ACCUMULATE cell on-chip — the bit-accumulator + "00"-delimiter state machine
     runs on REAL simkyt: fed a bit stream one bit at a time, on each "00" boundary
     it forms the accumulated codeword INTEGER into the panel read ADDRESS and pulls
     the read trigger (WRITE cur->panel R5, JUMP->panel R1). The addresses captured
     at the panel equal the golden codeword integers, in order.
  2. LOAD PHASE — the persistent placed ``SramControllerBlock`` streams the reverse
     map into the panel (each pair sets its own sparse address), real ``PanelDriver``
     pump, held-ack port (mirrors the encoder's load-phase test).
  3. LOOKUP PHASE / FULL CHAIN — the accumulate cell drives the reads on chip A; the
     panel PUSH-READs ``sram[cur]`` (char + CHAR_OFFSET) and delivers it to the
     EMIT cell on chip B through REAL routing (mirrors
     ``test_write_then_read_back_out_port``); the emit cell subtracts CHAR_OFFSET and
     writes the ASCII char out x16_out. Asserted BIT-EXACT vs the golden decoder over
     the FULL ASCII 0..127 set + a message + random, AND ROUND-TRIP (golden encoder
     bits -> this SRAM decoder -> original chars) through the real panel push-read.
  4. INV-4 mutation gates proven to FAIL (wrong map word in SRAM, wrong delimiter,
     off-by-one bit accumulation).

Env (INV-28): run with PYTHONPATH pointing at THIS worktree's runtime/python +
placekyt + verification so simkyt/gr_kyttar resolve here, not the shared checkout.
"""
from __future__ import annotations

import os
import random
import sys
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_WT = Path(__file__).resolve().parents[2]
for _p in (str(_WT / "runtime" / "python"), str(_WT / "placekyt"),
           str(Path(__file__).resolve().parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from varicode_golden import GOLDEN_VARICODE  # noqa: E402
from gr_kyttar.placement import VaricodeDecoderBlock  # noqa: E402
from gr_kyttar.placement.blocks.varicode_decoder_block import (  # noqa: E402
    VARICODE,
    CHAR_OFFSET,
    reverse_pairs,
    sram_reverse_image,
    decode_from_sram,
    varicode_encode,
    varicode_decode_bits,
    subset_reverse_lut,
)
from gr_kyttar.placement.blocks.sram_controller_block import (  # noqa: E402
    SramControllerBlock,
)
from gr_kyttar.placement.resolver import CellProgramResolver  # noqa: E402

CHIP_YAML = Path(os.environ.get(
    "KYTTAR_CHIP_YAML",
    _WT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml"))

W = 10  # chip is 10 wide


def _cid(x, y):
    return y * W + x


def _wr(h, d):
    return (0x6 << 12) | ((h & 0x1F) << 5) | (d & 0x1F)


def _jp(h, e):
    return (0x7 << 12) | ((h & 0x1F) << 5) | (e & 0x1F)


def _need_chip():
    if not CHIP_YAML.exists():
        pytest.skip("chip-type yaml absent")


def _bits(text):
    """The golden Varicode wire bits for an ASCII string (list of 0/1)."""
    return [int(c) for c in varicode_encode(text)]


# ================================================= consistency: golden anchors + table
def test_varicode_table_matches_golden():
    """The decoder's table IS the encoder golden (same 128-entry PSK31 table)."""
    assert VARICODE == GOLDEN_VARICODE
    assert len(set(VARICODE)) == 128


def test_codeword_integers_are_distinct():
    """The reverse map is well-defined: the 128 codeword INTEGER values are
    distinct (leading '1' encodes the length in the magnitude)."""
    vals = [int(p, 2) for p in VARICODE]
    assert len(set(vals)) == 128
    assert min(vals) == 1 and max(vals) == 955


# ============================================================ pure reverse-map / model
def test_reverse_image_is_offset_by_char_offset():
    img = sram_reverse_image()
    assert len(img) == 128
    # space (' ')=code "1" (int 1) -> stores 32 + CHAR_OFFSET
    assert img[int("1", 2)] == ord(" ") + CHAR_OFFSET
    assert img[int("11", 2)] == ord("e") + CHAR_OFFSET
    assert img[int("101", 2)] == ord("t") + CHAR_OFFSET
    # NUL (char 0, code "1010101011") stores as 0 + CHAR_OFFSET == 1 (NOT 0)
    assert img[int(VARICODE[0], 2)] == 0 + CHAR_OFFSET == 1
    assert 0 not in img.values()               # nothing stores as the empty default


def test_decode_from_sram_matches_golden_full_ascii():
    """The SRAM-backed decode MODEL is bit-exact to the golden over ALL 128 codes
    (including NUL — the CHAR_OFFSET makes it distinguishable)."""
    img = sram_reverse_image()
    text = "".join(chr(i) for i in range(128))
    got = "".join(chr(c) for c in decode_from_sram(img, _bits(text)))
    assert got == text == varicode_decode_bits(varicode_encode(text))


@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4])
def test_decode_from_sram_matches_golden_random(seed):
    rng = random.Random(seed)
    text = "".join(chr(rng.randint(0, 127)) for _ in range(80))
    img = sram_reverse_image()
    got = "".join(chr(c) for c in decode_from_sram(img, _bits(text)))
    assert got == text


def test_process_reference_still_golden():
    b = VaricodeDecoderBlock("v")
    codes = b.process_reference(_bits("the quick brown fox")).tolist()
    assert "".join(chr(c) for c in codes) == "the quick brown fox"


# ===================================================== cell resolution / single-cell fit
def _accum_cell(read_addr_hop=10):
    b = VaricodeDecoderBlock("v", read_addr_hop=read_addr_hop)
    acc = b.build_cell_programs()[0]
    res = CellProgramResolver().resolve(acc)
    cls = CellProgramResolver().classify_addresses(acc)
    bit_reg = [a for a, v in cls.items() if v.get("name") == "bit"][0]
    ent = CellProgramResolver().compute_entry_addresses(acc)["accumulate"]
    return res, bit_reg, ent


def _emit_cell(emit_hop=10):
    b = VaricodeDecoderBlock("v", emit_hop=emit_hop)
    em = b.build_cell_programs()[1]
    res = CellProgramResolver().resolve(em)
    cls = CellProgramResolver().classify_addresses(em)
    char_reg = [a for a, v in cls.items() if v.get("name") == "char"][0]
    ent = CellProgramResolver().compute_entry_addresses(em)["emit"]
    return res, char_reg, ent


def test_all_cells_fit_single_cell():
    """The accumulate state-machine, the emit cell, and the controller each resolve
    into one 32-word cell (the wall the panel removed was the reverse MAP, not the
    bit-accumulator logic)."""
    b = VaricodeDecoderBlock("v")
    for cp in b.build_cell_programs().values():
        res = CellProgramResolver().resolve(cp)
        assert max(res.memory) < 32
        assert len(res.memory) <= 32


def test_block_reports_three_cells():
    assert VaricodeDecoderBlock("v").cell_count == 3


# ============================ ACCUMULATE cell on-chip: forms codeword -> read address
def _run_accum_onchip(bits):
    """Load the accumulate cell at (0,0), feed `bits` one at a time, and capture the
    sequence of panel READ ADDRESSES the cell emits on each "00" boundary. The panel
    R3 descriptor is left as the disabled sentinel so no push-read is delivered — we
    only observe the address (R5) + the read trigger count."""
    import simkyt
    from engine.sram_panel import SramPanelDevice, PanelDriver
    res, bit_reg, ent = _accum_cell()
    chip = simkyt.Chip.from_yaml(str(CHIP_YAML))
    for a in range(32):
        chip.write_cell_memory(_cid(0, 0), a, int(res.memory.get(a, 0)))
    for x in range(W):
        chip.set_fwd_face(_cid(x, 0), "east")
    chip.set_port_handshake("x16_out", True)
    dev = SramPanelDevice()
    # R3 disabled sentinel (@0) -> _push_read returns None (no delivery), we just
    # watch the address the accumulate cell wrote to R5 on each read trigger.
    dev.on_write(3, _wr(31, 0))
    drv = PanelDriver(dev, chip, "x16_out", chip, "x16_in")
    addrs = []
    prev_reads = 0
    for bit in bits:
        chip.set_port_entry_address("x16_in", ent)
        chip.set_port_target_hop_count("x16_in", 30)      # land locally at (0,0)
        chip.write_port_multi_i16("x16_in", [[(bit_reg, int(bit) & 1)]], ent)
        for _ in range(250):
            chip.run(max_events=16)
            drv.step()
        if dev.reads_issued > prev_reads:
            addrs.append(dev.reg(5))
            prev_reads = dev.reads_issued
    return addrs


@pytest.mark.parametrize("ch", list(" etaoinZQ!~") + [chr(0), chr(127)])
def test_accum_cell_forms_codeword_address_onchip(ch):
    """For a single char's wire bits, the on-chip accumulate cell issues EXACTLY one
    panel read, at the address == the char's codeword INTEGER value."""
    _need_chip()
    addrs = _run_accum_onchip(_bits(ch))
    assert addrs == [int(VARICODE[ord(ch)], 2)], (ch, addrs)


def test_accum_cell_address_sequence_onchip():
    """A multi-char stream produces the codeword-integer addresses in order."""
    _need_chip()
    text = "et a"
    addrs = _run_accum_onchip(_bits(text))
    assert addrs == [int(VARICODE[ord(c)], 2) for c in text]


def test_accum_cell_skips_leading_idle_zeros_onchip():
    """A run of idle zeros before the first '1' must NOT trigger a spurious read."""
    _need_chip()
    addrs = _run_accum_onchip([0, 0, 0, 0] + _bits("e"))
    assert addrs == [int(VARICODE[ord("e")], 2)]


# ================================================== EMIT cell on-chip (offset subtract)
def _run_emit_onchip(word):
    """Deliver `word` (== char + CHAR_OFFSET) to the emit cell via a REAL panel
    push-read and collect the emitted char out x16_out."""
    import simkyt
    from engine.sram_panel import SramPanelDevice, PanelDriver, PushRead
    res, char_reg, ent = _emit_cell()
    chip = simkyt.Chip.from_yaml(str(CHIP_YAML))
    for a in range(32):
        chip.write_cell_memory(_cid(0, 0), a, int(res.memory.get(a, 0)))
    for x in range(W):
        chip.set_fwd_face(_cid(x, 0), "east")
    dev = SramPanelDevice()
    drv = PanelDriver(dev, chip, "x16_out", chip, "x16_in")
    drv._inject(PushRead(value=word, dest=char_reg, write_hop=30,
                         jump_entry=ent, jump_hop=30))
    out = []
    for _ in range(400):
        chip.run(max_events=16)
        for v, _d, _t in chip.read_port_words_timed("x16_out"):
            out.append(v & 0xFFFF)
    return out


@pytest.mark.parametrize("ch", list(" etaZ~") + [chr(0), chr(127)])
def test_emit_cell_subtracts_offset_onchip(ch):
    """The emit cell subtracts CHAR_OFFSET from the panel-delivered word and emits
    the ASCII char (NUL included — proves the offset is load-bearing)."""
    _need_chip()
    out = _run_emit_onchip(ord(ch) + CHAR_OFFSET)
    assert out == [ord(ch)], (ch, out)


# ============================================= LOAD PHASE: persistent controller load
def _controller():
    ctl = SramControllerBlock("ctl", panel_hop=10)
    cp = ctl.build_cell_programs()[0]
    res = CellProgramResolver().resolve(cp)
    cls = CellProgramResolver().classify_addresses(cp)
    cin = [a for a, v in cls.items() if v.get("name") == "data"][0]
    ent = CellProgramResolver().compute_entry_addresses(cp)
    return res, cin, ent


def test_load_phase_streams_reverse_map_sparse_addressing():
    """The persistent placed SramController streams the reverse map into the panel
    in ONE chip run: for each (codeword_int, char+offset) pair it set_addrs the
    sparse codeword address then writes the word — landing every populated word at
    its codeword integer address (SRAM_PANEL.md §6 load phase)."""
    _need_chip()
    import simkyt
    from engine.sram_panel import SramPanelDevice, PanelDriver

    res, cin, ent = _controller()
    pairs = reverse_pairs()[:24]
    dev = SramPanelDevice()
    chip = simkyt.Chip.from_yaml(str(CHIP_YAML))
    for a in range(32):
        chip.write_cell_memory(_cid(0, 0), a, int(res.memory.get(a, 0)))
    for x in range(W):
        chip.set_fwd_face(_cid(x, 0), "east")
    chip.set_port_handshake("x16_out", True)
    drv = PanelDriver(dev, chip, "x16_out", chip, "x16_in")
    stim = []
    for (addr, word) in pairs:
        stim += [_wr(30, cin), addr, _jp(30, ent["set_addr"])]      # sparse address
        stim += [_wr(30, cin), word, _jp(30, ent["write"])]         # store the word
    chip.queue_words_physical("x16_in", stim)
    for _ in range(16000):
        chip.run(max_events=32)
        drv.step()
        if dev.writes_committed >= len(pairs):
            break
    assert dev.writes_committed == len(pairs)
    assert all(dev.mem.get(addr) == word for (addr, word) in pairs), \
        "sparse-addressed reverse-map load stored wrong words"


# ============================= FULL CHAIN: accumulate on-chip -> panel push-read -> emit
def _load_full_reverse_map(dev):
    """Load ALL 128 reverse-map words into the panel via the controller write
    PROTOCOL (device commit — the same on_write/on_jump the controller emits; the
    persistent streaming load is proven separately in
    test_load_phase_streams_reverse_map_...)."""
    for (addr, word) in reverse_pairs():
        dev.on_write(2, word)        # R2 payload
        dev.on_write(5, addr)        # R5 address == codeword integer
        dev.on_jump(0)               # commit


def _decode_onchip(bits):
    """The full per-symbol SRAM-backed decode through REAL routing:

      * chip A runs the ACCUMULATE cell; fed the bit stream it forms each codeword
        and pulls the panel read trigger on the "00" boundary;
      * the panel PUSH-READs sram[cur] (char + CHAR_OFFSET) and we deliver it to the
        EMIT cell on chip B (mirrors test_write_then_read_back_out_port), which
        subtracts the offset and writes the char out x16_out.

    Returns the emitted ASCII char codes (ints), in order.
    """
    import simkyt
    from engine.sram_panel import SramPanelDevice, PanelDriver
    dev = SramPanelDevice()
    _load_full_reverse_map(dev)

    res_a, bit_reg, ent_a = _accum_cell()
    chip_a = simkyt.Chip.from_yaml(str(CHIP_YAML))
    for a in range(32):
        chip_a.write_cell_memory(_cid(0, 0), a, int(res_a.memory.get(a, 0)))
    for x in range(W):
        chip_a.set_fwd_face(_cid(x, 0), "east")
    chip_a.set_port_handshake("x16_out", True)
    drv_a = PanelDriver(dev, chip_a, "x16_out", chip_a, "x16_in")

    res_e, char_reg, ent_e = _emit_cell()
    out_chars = []

    def _emit_push(push):
        # The panel produced this PushRead from the accumulate cell's read trigger;
        # route it to the EMIT cell (0,0) on a fresh chip and collect the char out.
        chip_b = simkyt.Chip.from_yaml(str(CHIP_YAML))
        for a in range(32):
            chip_b.write_cell_memory(_cid(0, 0), a, int(res_e.memory.get(a, 0)))
        for x in range(W):
            chip_b.set_fwd_face(_cid(x, 0), "east")
        drv_b = PanelDriver(dev, chip_b, "x16_out", chip_b, "x16_in")
        push.dest = char_reg
        push.write_hop = 30
        push.jump_entry = ent_e
        push.jump_hop = 30
        drv_b._inject(push)
        for _ in range(400):
            chip_b.run(max_events=16)
            for v, _d, _t in chip_b.read_port_words_timed("x16_out"):
                out_chars.append(v & 0xFFFF)

    # Intercept the accumulate chip's push-reads (the panel's read-out delivery) and
    # forward each to the emit chip. The panel STILL performs the real mem[cur] read
    # (push.value == sram[cur]); we only re-route where the value lands.
    drv_a._inject = _emit_push
    for bit in bits:
        chip_a.set_port_entry_address("x16_in", ent_a)
        chip_a.set_port_target_hop_count("x16_in", 30)
        chip_a.write_port_multi_i16("x16_in", [[(bit_reg, int(bit) & 1)]], ent_a)
        for _ in range(250):
            chip_a.run(max_events=16)
            drv_a.step()
    return out_chars


def test_full_chain_bit_exact_full_ascii():
    """EVERY ASCII code 0..127: its golden wire bits decoded through the REAL panel
    push-read (accumulate -> panel -> emit) recover the char BIT-EXACT vs the golden
    decoder — the whole reverse map exercised end-to-end."""
    _need_chip()
    for byte in range(128):
        got = _decode_onchip(_bits(chr(byte)))
        assert got == [byte], (byte, got)


def test_full_chain_bit_exact_message():
    """A streamed message decoded through the panel round-trip is BIT-EXACT to the
    golden text."""
    _need_chip()
    msg = "the quick brown fox jumps over the lazy dog " + "PSK31 de G3PLX\n\r"
    got = _decode_onchip(_bits(msg))
    assert "".join(chr(c) for c in got) == msg
    assert got == [ord(c) for c in varicode_decode_bits(varicode_encode(msg))]


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_full_chain_bit_exact_random(seed):
    """Random ASCII bytes through the panel round-trip are bit-exact vs the golden."""
    _need_chip()
    rng = random.Random(seed)
    text = "".join(chr(rng.randint(0, 127)) for _ in range(24))
    got = _decode_onchip(_bits(text))
    assert "".join(chr(c) for c in got) == text


def test_roundtrip_golden_encoder_through_sram_decoder():
    """ROUND-TRIP: the GOLDEN encoder's wire bits fed through the SRAM decoder (real
    panel push-read) recover the original chars EXACTLY."""
    _need_chip()
    for text in ["hello world", "CQ CQ CQ", "abc123"]:
        got = _decode_onchip(_bits(text))
        assert "".join(chr(c) for c in got) == text


# ------------------------------------------------------------- MUTATION gates (INV-4)
def test_mutation_wrong_map_word_in_sram_FAILS():
    """A WRONG reverse-map word loaded into the panel must make the decoded char
    disagree with the golden — the gate SEES a corrupted SRAM image."""
    _need_chip()
    import simkyt
    from engine.sram_panel import SramPanelDevice, PanelDriver

    dev = SramPanelDevice()
    _load_full_reverse_map(dev)
    # Corrupt the panel word for 'q''s codeword (store 'a'+offset there instead).
    q_addr = int(VARICODE[ord("q")], 2)
    dev.mem[q_addr] = ord("a") + CHAR_OFFSET

    # Decode 'q' with the corrupted panel (reuse the accumulate->emit chain, but with
    # this pre-corrupted device).
    res_a, bit_reg, ent_a = _accum_cell()
    chip_a = simkyt.Chip.from_yaml(str(CHIP_YAML))
    for a in range(32):
        chip_a.write_cell_memory(_cid(0, 0), a, int(res_a.memory.get(a, 0)))
    for x in range(W):
        chip_a.set_fwd_face(_cid(x, 0), "east")
    chip_a.set_port_handshake("x16_out", True)
    drv_a = PanelDriver(dev, chip_a, "x16_out", chip_a, "x16_in")
    res_e, char_reg, ent_e = _emit_cell()
    out = []

    def _emit_push(push):
        chip_b = simkyt.Chip.from_yaml(str(CHIP_YAML))
        for a in range(32):
            chip_b.write_cell_memory(_cid(0, 0), a, int(res_e.memory.get(a, 0)))
        for x in range(W):
            chip_b.set_fwd_face(_cid(x, 0), "east")
        drv_b = PanelDriver(dev, chip_b, "x16_out", chip_b, "x16_in")
        push.dest, push.write_hop = char_reg, 30
        push.jump_entry, push.jump_hop = ent_e, 30
        drv_b._inject(push)
        for _ in range(400):
            chip_b.run(max_events=16)
            for v, _d, _t in chip_b.read_port_words_timed("x16_out"):
                out.append(v & 0xFFFF)

    drv_a._inject = _emit_push
    for bit in _bits("q"):
        chip_a.set_port_entry_address("x16_in", ent_a)
        chip_a.set_port_target_hop_count("x16_in", 30)
        chip_a.write_port_multi_i16("x16_in", [[(bit_reg, bit & 1)]], ent_a)
        for _ in range(250):
            chip_a.run(max_events=16)
            drv_a.step()
    assert out != [ord("q")], "gate blind to a wrong reverse-map word in SRAM"
    assert out == [ord("a")], "corruption should decode 'q' as 'a'"


def test_mutation_wrong_delimiter_FAILS():
    """A decoder that treats a SINGLE '0' as the boundary (off-by-one delimiter)
    mis-splits — the golden requires the '00' delimiter."""
    text = "test"
    stream = _bits(text)
    img = sram_reverse_image()

    def mutant_single_zero(bits):
        out, cur = [], 0
        for raw in bits:
            b = int(raw) & 1
            if b == 0:
                if cur:
                    w = img.get(cur, 0)
                    if w:
                        out.append((w - CHAR_OFFSET) & 0xFFFF)
                    cur = 0
            else:
                cur = (cur << 1) | 1     # never accumulates intra-code '0's either
        return out

    assert mutant_single_zero(stream) != [ord(c) for c in text]


def test_mutation_offbyone_bit_accumulation_FAILS():
    """Dropping the first bit (off-by-one accumulation) breaks the decode."""
    text = "abc"
    stream = _bits(text)
    img = sram_reverse_image()
    good = [chr(c) for c in decode_from_sram(img, stream)]
    shifted = [chr(c) for c in decode_from_sram(img, stream[1:])]
    assert "".join(good) == text
    assert "".join(shifted) != text


# --------------------------------------------------------- historical wall is retired
def test_reverse_map_addr_space_is_1024():
    """The reverse map spans 1024 addresses (max codeword 955) — the very table that
    was the quarantine wall now lives in the panel (sparse, 128 populated)."""
    assert VaricodeDecoderBlock.reverse_map_size() == 1024
    assert VaricodeDecoderBlock.ADDR_SPACE == 1024
    assert len(sram_reverse_image()) == 128


def test_subset_lut_helper_still_available():
    """The subset-LUT artifact is retained (historical wall quantification)."""
    _, size = subset_reverse_lut("abcdefghijklmnopqrstuvwxyz ")
    assert size >= 400
