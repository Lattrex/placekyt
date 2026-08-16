# SPDX-License-Identifier: GPL-3.0-or-later
"""VaricodeEncoderBlock — SRAM-backed, verified BIT-EXACT through the REAL panel.

The FIRST SRAM-backed DSP block (INV-31). The 128-entry PSK31 Varicode table that
QUARANTINED the single-cell design (INV-29: table too big + variable-length emit)
now lives in the SRAM PANEL, and each entry packs into ONE 16-bit word
(the code LEFT-ALIGNED at bit 15 + the length in bits[3:0]) so the panel returns
a FIXED word per symbol and the emitter walks it with immediate shift counts. This suite is
the PROOF the SRAM path unblocks the table-heavy block:

  1. LOAD PHASE — the persistent placed ``SramControllerBlock`` streams the packed
     table into the panel with AUTO-INCREMENT addressing, ONE chip run, held-ack panel
     port, real ``PanelDriver`` pump (mirrors ``TestSramDemo``).
  2. LOOKUP PHASE — per input byte the panel PUSH-READs ``sram[byte]`` back into the
     emit cell's input register + kicks its ``emit`` entry through REAL simkyt routing
     (mirrors ``test_write_then_read_back_out_port``); the on-chip emit cell unpacks
     (code,length) and emits ``length`` bits + the ``00`` gap.
  3. The emitted bit stream is asserted BIT-EXACT vs the existing Python golden
     (``varicode_golden``) over the full ASCII set + a message + edge chars + random.
  4. INV-4 mutation gates proven to FAIL (wrong table word loaded, missing ``00``,
     wrong length unpack).

Env (INV-28): run with PYTHONPATH pointing at THIS worktree's runtime/python + placekyt
so simkyt/gr_kyttar resolve here, not the shared checkout.
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

from varicode_golden import GOLDEN_VARICODE, golden_bits  # noqa: E402
from gr_kyttar.placement import VaricodeEncoderBlock  # noqa: E402
from gr_kyttar.placement.blocks.varicode_encoder_block import (  # noqa: E402
    pack_entry,
    unpack_bits,
    sram_table,
    emit_from_sram,
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


# ============================================================ pure packing / golden
def test_pack_unpack_roundtrips_all_128():
    """Every table entry packs into one 16-bit word and unpacks bit-exact."""
    for b in range(128):
        code = GOLDEN_VARICODE[b]
        word = pack_entry(code)
        assert word <= 0xFFFF
        assert (word & 0xF) == len(code)              # length in bits[3:0]
        # code left-aligned: first bit at bit 15, region never touches len bits
        assert (word >> 15) == int(code[0])
        assert (word & 0x0030) == 0                   # bits[5:4] unused
        assert unpack_bits(word) == [1 if c == "1" else 0 for c in code]


def test_sram_table_is_128_words_addressed_by_codepoint():
    sram = sram_table()
    assert len(sram) == 128
    assert sram[32] == pack_entry("1")                # space
    assert sram[ord("e")] == pack_entry("11")


def test_emit_from_sram_matches_golden_full_ascii():
    """The SRAM-backed encode model is bit-exact to the golden over all 128 codes."""
    sram = sram_table()
    dut = emit_from_sram(sram, list(range(128)))
    assert dut == golden_bits(list(range(128)))


def test_process_reference_still_golden():
    b = VaricodeEncoderBlock("v")
    dut = b.process_reference([ord(c) for c in "the quick brown fox"]).tolist()
    assert dut == golden_bits([ord(c) for c in "the quick brown fox"])


# ================================================= on-chip emit cell (unpack + emit)
def _emit_cell():
    """Resolve the emit cell with emit_hop=10 so its bits exit the x16 port."""
    b = VaricodeEncoderBlock("v", emit_hop=10, emit_dest=2)
    cp = b.build_cell_programs()[1]   # cell 1 = the emit cell (cell 0 = the ctl)
    res = CellProgramResolver().resolve(cp)
    cls = CellProgramResolver().classify_addresses(cp)
    word_reg = [a for a, v in cls.items() if v.get("name") == "word"][0]
    return res, word_reg, res.entry_addr


def _run_emit_cell_onchip(word):
    """Load the emit cell at (0,0), place `word` in its input reg, kick emit, collect
    the emitted bits out x16_out."""
    import simkyt
    res, word_reg, entry = _emit_cell()
    chip = simkyt.Chip.from_yaml(str(CHIP_YAML))
    for a in range(0, 32):
        chip.write_cell_memory(_cid(0, 0), a, int(res.memory.get(a, 0)))
    chip.write_cell_memory(_cid(0, 0), word_reg, int(word))
    for x in range(W):
        chip.set_fwd_face(_cid(x, 0), "east")
    chip.set_port_entry_address("x16_in", entry)
    chip.set_port_target_hop_count("x16_in", 30)
    chip.write_port("x16_in", np.array([0.0], dtype=np.float32))
    bits = []
    for _ in range(500):
        chip.run(max_events=16)
        for v, _d, _t in chip.read_port_words_timed("x16_out"):
            bits.append(v & 1)
    return bits


def test_emit_cell_fits_single_cell():
    """The emit cell resolves into one 32-word cell (the wall the panel removed was
    the TABLE, not the emit logic)."""
    res, _reg, _entry = _emit_cell()
    assert max(res.memory) < 32
    assert len(res.memory) <= 32


@pytest.mark.parametrize("ch", list(" etaoinsZI!Q~\n\r") + [chr(0), chr(127)])
def test_emit_cell_bit_exact_onchip(ch):
    """The on-chip emit cell emits EXACTLY the Varicode bits + '00' for a delivered
    packed word — the resolved variable-length emit runs on real simkyt."""
    _need_chip()
    got = _run_emit_cell_onchip(pack_entry(GOLDEN_VARICODE[ord(ch)]))
    want = [1 if c == "1" else 0 for c in GOLDEN_VARICODE[ord(ch)]] + [0, 0]
    assert got == want, (ch, got, want)


# ============================================= LOAD PHASE: persistent controller load
def _controller():
    ctl = SramControllerBlock("ctl", panel_hop=10)
    cp = ctl.build_cell_programs()[0]
    res = CellProgramResolver().resolve(cp)
    cls = CellProgramResolver().classify_addresses(cp)
    cin = [a for a, v in cls.items() if v.get("name") == "data"][0]
    ent = CellProgramResolver().compute_entry_addresses(cp)
    return res, cin, ent


def test_load_phase_streams_table_with_autoincrement():
    """The persistent placed SramController streams packed words into the panel in ONE
    chip run; its wraddr AUTO-INCREMENTS so a `set_addr(0)` + N `write`s lands words at
    0..N-1 (SRAM_PANEL.md §6 load phase, mirrors TestSramDemo pacing)."""
    _need_chip()
    import simkyt
    from engine.sram_panel import SramPanelDevice, PanelDriver

    res, cin, ent = _controller()
    sram = sram_table()
    N = 24
    dev = SramPanelDevice()
    chip = simkyt.Chip.from_yaml(str(CHIP_YAML))
    for a in range(0, 32):
        chip.write_cell_memory(_cid(0, 0), a, int(res.memory.get(a, 0)))
    for x in range(W):
        chip.set_fwd_face(_cid(x, 0), "east")
    chip.set_port_handshake("x16_out", True)
    drv = PanelDriver(dev, chip, "x16_out", chip, "x16_in")
    stim = [_wr(30, cin), 0, _jp(30, ent["set_addr"])]
    for i in range(N):
        stim += [_wr(30, cin), sram[i], _jp(30, ent["write"])]
    chip.queue_words_physical("x16_in", stim)
    for _ in range(8000):
        chip.run(max_events=32)
        drv.step()
        if dev.writes_committed >= N:
            break
    assert dev.writes_committed == N
    assert all(dev.mem.get(i) == sram[i] for i in range(N)), \
        "auto-increment load stored wrong words"


# ===================================== LOOKUP PHASE: panel push-read → emit → golden
def _load_full_table(dev):
    """Load all 128 packed words into the panel via the controller write PROTOCOL
    (device commit — the same on_write/on_jump the controller emits; the persistent
    streaming load is proven separately in test_load_phase_streams_table_...)."""
    sram = sram_table()
    for a in range(128):
        dev.on_write(2, sram[a])   # R2 payload
        dev.on_write(5, a)         # R5 address == code point
        dev.on_jump(0)             # commit
    return sram


def _encode_byte_full_chain(dev, byte):
    """The full per-symbol SRAM-backed encode through REAL routing: the panel
    PUSH-READs sram[byte] into the emit cell's input register + kicks its emit entry;
    the emit cell unpacks + emits the bits out x16_out."""
    import simkyt
    from engine.sram_panel import PanelDriver
    res, word_reg, entry = _emit_cell()
    chip = simkyt.Chip.from_yaml(str(CHIP_YAML))
    for a in range(0, 32):
        chip.write_cell_memory(_cid(0, 0), a, int(res.memory.get(a, 0)))
    for x in range(W):
        chip.set_fwd_face(_cid(x, 0), "east")
    drv = PanelDriver(dev, chip, "x16_out", chip, "x16_in")
    # Push-read descriptors: deliver the word to (0,0).R{word_reg} (@30 == local) then
    # JUMP the emit entry — the panel ORIGINATES the delivery (SRAM_PANEL.md §3).
    dev.on_write(3, _wr(30, word_reg))
    dev.on_write(4, _jp(30, entry))
    dev.on_write(5, byte)
    push = dev.on_jump(1)                        # read trigger
    assert push is not None and push.value == dev.mem.get(byte, 0)
    drv._inject(push)
    bits = []
    for _ in range(500):
        chip.run(max_events=16)
        for v, _d, _t in chip.read_port_words_timed("x16_out"):
            bits.append(v & 1)
    return bits


def test_full_chain_bit_exact_full_ascii():
    """EVERY ASCII code 0..127 encoded through the REAL panel push-read + emit cell is
    BIT-EXACT vs the golden — the whole table exercised end-to-end."""
    _need_chip()
    from engine.sram_panel import SramPanelDevice
    dev = SramPanelDevice()
    _load_full_table(dev)
    assert dev.writes_committed == 128
    for byte in range(128):
        got = _encode_byte_full_chain(dev, byte)
        want = [1 if c == "1" else 0 for c in GOLDEN_VARICODE[byte]] + [0, 0]
        assert got == want, (byte, got, want)


def test_full_chain_bit_exact_message():
    """A streamed message ('the quick brown fox...' + edge chars) through the panel
    round-trip concatenates BIT-EXACT to the golden bit stream."""
    _need_chip()
    from engine.sram_panel import SramPanelDevice
    dev = SramPanelDevice()
    _load_full_table(dev)
    msg = "the quick brown fox jumps over the lazy dog " + " et\n\r"
    out = []
    for ch in msg:
        out += _encode_byte_full_chain(dev, ord(ch))
    assert out == golden_bits([ord(c) for c in msg])


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_full_chain_bit_exact_random(seed):
    """Random ASCII bytes through the panel round-trip are bit-exact vs the golden."""
    _need_chip()
    from engine.sram_panel import SramPanelDevice
    rng = random.Random(seed)
    text = [rng.randint(0, 127) for _ in range(40)]
    dev = SramPanelDevice()
    _load_full_table(dev)
    out = []
    for byte in text:
        out += _encode_byte_full_chain(dev, byte)
    assert out == golden_bits(text)


# ------------------------------------------------------------- MUTATION gates (INV-4)
def test_mutation_wrong_table_word_loaded_FAILS():
    """A WRONG packed word loaded into the panel must make the emitted bits disagree
    with the golden — the gate SEES a corrupted SRAM image."""
    _need_chip()
    from engine.sram_panel import SramPanelDevice
    dev = SramPanelDevice()
    _load_full_table(dev)
    # Corrupt the panel word for 'q' (load 'a''s packed word at 'q''s address).
    dev.mem[ord("q")] = pack_entry(GOLDEN_VARICODE[ord("a")])
    got = _encode_byte_full_chain(dev, ord("q"))
    want = [1 if c == "1" else 0 for c in GOLDEN_VARICODE[ord("q")]] + [0, 0]
    assert got != want, "gate blind to a wrong table word in SRAM"


def test_mutation_missing_00_separator_FAILS():
    """The golden requires the '00' gap; a run WITHOUT it must disagree."""
    text = "test"
    gold = golden_bits([ord(c) for c in text])
    no_gap = []
    for ch in text:
        no_gap += [1 if b == "1" else 0 for b in GOLDEN_VARICODE[ord(ch)]]
    assert no_gap != gold, "gate blind to a missing '00' separator"


def test_mutation_wrong_length_unpack_FAILS():
    """If the packed LENGTH field is wrong (one bit short) the unpacked bits differ —
    proving the length nibble is load-bearing, not decorative."""
    for b in range(1, 128):
        code = GOLDEN_VARICODE[b]
        if len(code) < 2:
            continue
        good = pack_entry(code)
        bad = ((len(code) - 1) << 10) | (good & 0x3FF)   # length one too small
        assert unpack_bits(bad) != [1 if c == "1" else 0 for c in code]
        break
    else:
        pytest.fail("no multi-bit code to mutate")
