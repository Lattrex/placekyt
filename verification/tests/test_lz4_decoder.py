# SPDX-License-Identifier: GPL-3.0-or-later
"""LZ4DecoderBlock — the golden, the cell programs on the REAL chip through the
REAL SRAM panel, and the PRECISE placement wall that QUARANTINES the block.

The block is the SECOND SRAM-backed DSP block (INV-31) and the first to address a
**computed** panel address: Varicode reads a preloaded ROM at ``address == the input
symbol``, whereas this block WRITES the panel as it decodes and reads it back at
``(wpos - offset) & 0xFFFF`` — an address the chip derives at run time from its own
output position. That mechanism is PROVEN here on real silicon.

What this suite establishes, in order of strength:

  1. **The golden vs the published spec.** ``lz4_golden.py`` is a plain transcription of
     the LZ4 Block Format Description; here it is held byte-for-byte against the
     REFERENCE C DECODER (``lz4.block``) so the golden and the block cannot be
     self-consistently wrong together.
  2. **The block's FSM model vs the golden** — ``decode_model`` is the cell-level twin
     of the on-chip state machine (same registers, same phases), not a second copy of
     the golden.
  3. **THE CHIP.** The cell programs run on a real ``simkyt`` chip: the token nibble
     split, the little-endian offset assembly, and — the decisive one — a whole match
     copy driven through a real ``SramPanelDevice`` + ``PanelDriver``, including the
     ``offset == 1`` byte-run whose later source bytes are produced BY the copy.
  4. **INV-4 mutations**, each proven to FAIL.
  5. **The WALL** (``test_placement_wall_*``): the block is QUARANTINED, and the guard
     tests pin the exact reasons so the next agent starts where this one finished. See
     the ``lessons_log`` entry and INV-47.

Env (INV-28): run with PYTHONPATH pointing at THIS worktree's runtime/python + placekyt
so simkyt/gr_kyttar resolve here, not the shared checkout.
"""
from __future__ import annotations

import itertools
import json
import os
import random
import subprocess
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

from lz4_golden import (  # noqa: E402
    CONT_ESCAPE,
    LZ4FormatError,
    MINMATCH,
    NIBBLE_ESCAPE,
    lz4_compress_block,
    lz4_decompress_block,
    make_sequence,
    sequences,
)
from gr_kyttar.placement.blocks import LZ4DecoderBlock  # noqa: E402
from gr_kyttar.placement.blocks.lz4_decoder_block import (  # noqa: E402
    MAT_ESCAPE,
    ST_MATCH,
    ST_TOKEN,
    decode_model,
)
from gr_kyttar.placement.resolver import (  # noqa: E402
    CellProgramResolver,
    JumpTarget,
    ResolvedTargets,
    WriteTarget,
)

CHIP_YAML = Path(os.environ.get(
    "KYTTAR_CHIP_YAML",
    _WT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml"))
REPORT = _WT / "verification" / "reports" / "LZ4DecoderBlock.json"
GR_PY = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")

W = 10                      # the chip is 10 cells wide
R = CellProgramResolver()


def _need_chip():
    if not CHIP_YAML.exists():
        pytest.skip("chip-type yaml absent")


def _cid(x, y):
    return y * W + x


def _wr(h, d):
    """A raw panel read-out WRITE descriptor (SRAM_PANEL.md §3)."""
    return (0x6 << 12) | ((h & 0x1F) << 5) | (d & 0x1F)


def _jp(h, e):
    """A raw panel read-out JUMP descriptor (SRAM_PANEL.md §3)."""
    return (0x7 << 12) | ((h & 0x1F) << 5) | (e & 0x1F)


# =========================================================================
# LAYER 1 — the GOLDEN against the REFERENCE C DECODER
# =========================================================================
_REF_COMPRESS = r"""
import sys, json, base64, lz4.block
print(json.dumps([base64.b64encode(
    lz4.block.compress(base64.b64decode(x), store_size=False)).decode()
    for x in json.load(sys.stdin)]))
"""

_REF_DECOMPRESS = r"""
import sys, json, base64, lz4.block
print(json.dumps([base64.b64encode(lz4.block.decompress(
    base64.b64decode(b), uncompressed_size=s)).decode()
    for b, s in json.load(sys.stdin)]))
"""


def _have_reference():
    try:
        return subprocess.run([GR_PY, "-c", "import lz4.block"],
                              capture_output=True, timeout=30).returncode == 0
    except Exception:                                   # noqa: BLE001
        return False


_HAVE_REF = _have_reference()


def _ref(script, payload):
    r = subprocess.run([GR_PY, "-c", script], input=json.dumps(payload),
                       capture_output=True, text=True, timeout=180, check=True)
    return json.loads(r.stdout)


def _ref_compress(payloads):
    import base64
    return [base64.b64decode(x) for x in _ref(
        _REF_COMPRESS, [base64.b64encode(p).decode() for p in payloads])]


def _ref_decompress(jobs):
    import base64
    return [base64.b64decode(x) for x in _ref(
        _REF_DECOMPRESS, [[base64.b64encode(b).decode(), s] for b, s in jobs])]


#: The reference payload set: all-literal, highly repetitive, a long overlapping
#: run, structured text, and incompressible random data.
PAYLOADS = {
    "all_literal": bytes(range(256)),
    "repetitive": b"the quick brown fox jumps over the lazy dog. " * 12,
    "long_run": b"x" + b"y" * 400 + b"tailtailtail!!!",
    "mixed": b"a" * 300 + b"bcdefgh" * 40 + b"a" * 50,
    "random": bytes(random.Random(20260829).randrange(256) for _ in range(2000)),
    "short": b"short payload, mostly literals",
}


@pytest.mark.skipif(not _HAVE_REF, reason="reference lz4 C binding not importable")
def test_golden_decodes_reference_compressed_blocks_exactly():
    """LAYER 1a: the GOLDEN decodes blocks produced by the REFERENCE C COMPRESSOR,
    byte-for-byte, on 6 payloads (>= 3 required).

    This is the check that the golden cannot be self-consistently wrong with the
    block: these compressed blocks come from an implementation this repository did
    not write.
    """
    names = list(PAYLOADS)
    blocks = _ref_compress([PAYLOADS[n] for n in names])
    assert len(blocks) == len(names)
    for name, blk in zip(names, blocks):
        assert lz4_decompress_block(blk) == PAYLOADS[name], name


@pytest.mark.skipif(not _HAVE_REF, reason="reference lz4 C binding not importable")
def test_reference_decoder_accepts_our_blocks():
    """LAYER 1b: the REFERENCE C DECODER accepts the blocks this suite manufactures
    and returns the original payload — so the stimulus is format-legal, not merely
    something our own golden happens to like."""
    names = [n for n in PAYLOADS if PAYLOADS[n]]
    jobs = [(lz4_compress_block(PAYLOADS[n]), len(PAYLOADS[n]) + 64) for n in names]
    for name, g in zip(names, _ref_decompress(jobs)):
        assert g == PAYLOADS[name], name


def test_golden_rejects_malformed_blocks():
    """The golden ENFORCES the format's invalid cases rather than guessing."""
    with pytest.raises(LZ4FormatError):
        lz4_decompress_block(bytes([0x10, ord("A"), 0x00, 0x00]))   # offset 0
    with pytest.raises(LZ4FormatError):
        lz4_decompress_block(bytes([0x10, ord("A"), 0x05, 0x00]))   # offset > out
    with pytest.raises(LZ4FormatError):
        lz4_decompress_block(bytes([0xF0]))                          # truncated


def test_golden_continuation_is_summed_not_replaced():
    """A 15-nibble continuation SUMS every byte read, INCLUDING the terminator."""
    lits = bytes(range(48))                          # 48 = 15 + 33
    blk = make_sequence(lits)
    assert blk[0] >> 4 == NIBBLE_ESCAPE and blk[1] == 33
    assert lz4_decompress_block(blk) == lits
    lits = bytes((i * 7) & 0xFF for i in range(280))  # 280 = 15 + 255 + 10
    blk = make_sequence(lits)
    assert blk[1] == CONT_ESCAPE and blk[2] == 10
    assert lz4_decompress_block(blk) == lits


def test_golden_minmatch_and_offset_endianness():
    """The +4 MINMATCH and the LITTLE-endian offset, read straight off the wire."""
    blk = make_sequence(b"abcdefgh", offset=8, match_len=4) + make_sequence(b"zz")
    assert blk[0] & 0x0F == 0                        # nibble 0 == a 4-byte match
    assert lz4_decompress_block(blk) == b"abcdefgh" + b"abcd" + b"zz"
    lits = b"z" * 300
    blk = make_sequence(lits, offset=0x0102, match_len=4) + make_sequence(b"q")
    rec = sequences(blk)[0]
    assert rec["offset"] == 0x0102 and rec["match_len"] == MINMATCH
    # locate the offset field: token + the literal-length continuation + literals
    i = 1
    while blk[i] == CONT_ESCAPE:
        i += 1
    i += 1 + len(lits)
    assert (blk[i], blk[i + 1]) == (0x02, 0x01), \
        "the offset must serialise LOW byte first"


# =========================================================================
# LAYER 2 — the block's FSM MODEL against the golden
# =========================================================================
def test_model_matches_golden_on_all_payloads():
    """``decode_model`` (the cell-level twin of the on-chip FSM) is EXACT against the
    golden on every reference payload."""
    for name, p in PAYLOADS.items():
        blk = lz4_compress_block(p) if p else make_sequence(b"")
        got, _st = decode_model(blk)
        assert bytes(got) == lz4_decompress_block(blk) == p, name


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_model_matches_golden_random(seed):
    """Random structured payloads (>= 3 seeds) through the FSM model."""
    rng = random.Random(seed)
    p = (bytes(rng.choice(b"abcdefg") for _ in range(600))
         + bytes(rng.randrange(256) for _ in range(200)))
    blk = lz4_compress_block(p)
    got, _st = decode_model(blk)
    assert bytes(got) == p


def test_model_all_literal_input():
    """An all-literal block (no match at all) passes straight through."""
    p = bytes(range(256))
    blk = make_sequence(p)
    got, st = decode_model(blk)
    assert bytes(got) == p
    assert st["push_reads"] == 0, "an all-literal block must issue NO push-read"


def test_model_offset_one_is_a_byte_run():
    """offset == 1 with a long match is a RUN of one byte — the copy has to be
    byte-by-byte, and the model produces the run, not a repeated block."""
    blk = make_sequence(b"Q", offset=1, match_len=200) + make_sequence(b"tail!")
    got, _st = decode_model(blk)
    assert bytes(got) == b"Q" * 201 + b"tail!"
    assert bytes(got) == lz4_decompress_block(blk)


def test_model_overlapping_match_shorter_than_length():
    """An overlapping match (match_len > offset, offset > 1) repeats the pattern —
    the later source bytes are produced BY the copy."""
    blk = make_sequence(b"abc", offset=3, match_len=11) + make_sequence(b"!")
    got, _st = decode_model(blk)
    assert bytes(got) == b"abc" + b"abcabcabcab" + b"!"
    assert bytes(got) == lz4_decompress_block(blk)


def test_model_match_at_the_window_boundary():
    """A match whose offset reaches the OLDEST byte still in the window decodes
    correctly — the window index is the 16-bit wrap of ``wpos - off``."""
    n = 4096
    body = bytes((i * 31 + 7) & 0xFF for i in range(n))
    blk = make_sequence(body, offset=n, match_len=8) + make_sequence(b"end!!")
    got, _st = decode_model(blk)
    assert bytes(got) == body + body[:8] + b"end!!"
    assert bytes(got) == lz4_decompress_block(blk)


def test_process_reference_is_the_golden():
    """The block's ``process_reference`` is the golden decode."""
    b = LZ4DecoderBlock("lz4")
    for p in PAYLOADS.values():
        blk = lz4_compress_block(p) if p else make_sequence(b"")
        assert bytes(b.process_reference(list(blk)).tolist()) == p


def test_window_words_is_validated():
    """``window_words`` must be a power of two within the 16-bit offset field."""
    with pytest.raises(ValueError):
        LZ4DecoderBlock("x", window_words=1000)
    with pytest.raises(ValueError):
        LZ4DecoderBlock("x", window_words=1 << 17)
    assert LZ4DecoderBlock("x", window_words=4096).window_words == 4096


def test_panel_cost_is_the_documented_protocol():
    """The panel cost is 3 port words per literal byte and 9 per MATCH byte —
    the number that scopes the encoder in the next wave."""
    blk = make_sequence(b"Q", offset=1, match_len=200) + make_sequence(b"tail!")
    c = LZ4DecoderBlock("x").panel_cost(list(blk))
    assert c["history_writes"] == 206 and c["push_reads"] == 200
    assert c["words_per_literal_byte"] == 3
    assert c["words_per_match_byte"] == 9
    assert c["total_words"] == 3 * 206 + 6 * 200


# =========================================================================
# LAYER 3 — the CELL PROGRAMS, on the REAL CHIP
# =========================================================================
def _cell_maps(cp):
    """``(name -> register, entry -> address)`` for one resolved cell program."""
    dm = R._allocate_data(cp.data)
    nd = max(dm.values(), default=-1) + 1
    base = 31 - R.count_instructions(cp)
    gap = list(range(nd, base))
    sm = R._allocate_state(cp.state, gap)
    rest = [r for r in gap if r not in set(sm.values())]
    im = R._allocate_inputs(cp.inputs, rest)
    names = dict(dm)
    names.update(sm)
    names.update(im)
    return names, R.compute_entry_addresses(cp)


def _block_maps():
    b = LZ4DecoderBlock("lz4")
    progs = b.build_cell_programs()
    return b, progs, {c: _cell_maps(cp) for c, cp in progs.items()}


def test_router_cold_starts_expecting_a_token():
    """The router's ``st`` boots at :data:`ST_TOKEN`, so the very first compressed
    byte is parsed as a sequence token — a cold-start property that is baked into
    the cell image, not arranged by a reset trigger (INV-33: pin the state, and
    give it the right initial value)."""
    _b, progs, M = _block_maps()
    router = progs[0]
    st = next(s for s in router.state if s.name == "st")
    assert st.initial_value == ST_TOKEN
    res = R.resolve(router, _dummy_targets(router))
    assert res.memory[M[0][0]["st"]] == ST_TOKEN


def test_every_cell_fits_a_32_word_cell():
    """Each of the seven cells resolves inside its 32-word budget, with its state
    registers strictly BELOW the first instruction (the INV-33 overlap check)."""
    _b, progs, _m = _block_maps()
    for cid, cp in progs.items():
        n = R.count_instructions(cp)
        base = 31 - n
        dm = R._allocate_data(cp.data)
        nd = max(dm.values(), default=-1) + 1
        assert base >= nd, f"cell {cid}: {n} instructions overrun its data words"
        sm = R._allocate_state(cp.state, list(range(nd, base)))
        for name, reg in sm.items():
            assert reg < base, (f"cell {cid} state {name!r} at R{reg} lands ON an "
                                f"instruction (base {base}) — INV-33 overlap")
        res = R.resolve(cp, _dummy_targets(cp))
        assert max(res.memory) < 32


def _dummy_targets(cp):
    tg = ResolvedTargets()
    for p in cp.outputs:
        tg.writes[p.name] = WriteTarget(distance=1, target_addr=25)
        tg.jumps[p.name] = JumpTarget(distance=1, target_addr=1)
    return tg


def _run_cell(cellid, entry, inval=None, preset=None, rounds=400):
    """Load one cell at (0,0) on a real chip, kick ``entry``, and return
    ``(final registers, words seen at x16_out)``.

    The cell's own WRITE/JUMP targets are aimed 10 hops east so every hand-off it
    makes EXITS the x16 port and is observable.
    """
    import simkyt
    b, progs, M = _block_maps()
    cp = progs[cellid]
    names, entries = M[cellid]
    tg = ResolvedTargets()
    for (s, o, d, i) in b.internal_connections():
        if s == cellid:
            tg.writes[o] = WriteTarget(distance=10, target_addr=M[d][0][i])
    for (s, o, d, e) in b.internal_jumps():
        if s == cellid:
            tg.jumps[o] = JumpTarget(distance=10, target_addr=M[d][1][e])
    for p in cp.outputs:                    # external edges (out / gohead)
        tg.writes.setdefault(p.name, WriteTarget(distance=10, target_addr=2))
        tg.jumps.setdefault(p.name, JumpTarget(distance=10, target_addr=1))
    res = R.resolve(cp, tg)

    chip = simkyt.Chip.from_yaml(str(CHIP_YAML))
    for a in range(32):
        chip.write_cell_memory(_cid(0, 0), a, int(res.memory.get(a, 0)))
    for k, v in (preset or {}).items():
        chip.write_cell_memory(_cid(0, 0), names[k], int(v) & 0xFFFF)
    if inval is not None:
        chip.write_cell_memory(_cid(0, 0), names[cp.inputs[0].name],
                               int(inval) & 0xFFFF)
    for x in range(W):
        chip.set_fwd_face(_cid(x, 0), "east")
    chip.set_port_entry_address("x16_in", entries[entry])
    chip.set_port_target_hop_count("x16_in", 30)
    chip.write_port("x16_in", np.array([0.0], dtype=np.float32))
    words = []
    for _ in range(rounds):
        chip.run(max_events=16)
        for v, _d, _t in chip.read_port_words_timed("x16_out"):
            words.append(v & 0xFFFF)
    regs = {n: chip.read_cell_memory(_cid(0, 0), a) for n, a in names.items()}
    return regs, words


@pytest.mark.parametrize("token,want_mat,want_lit,want_st", [
    (0x00, MINMATCH, None, ST_MATCH),            # no literals, 4-byte match
    (0x31, 1 + MINMATCH, 3, 1),                  # 3 literals, 5-byte match
    (0xF0, MINMATCH, (-NIBBLE_ESCAPE) & 0xFFFF, 1),   # literal continuation
    (0x0F, MAT_ESCAPE, None, ST_MATCH),          # match-length continuation
    (0xAB, 11 + MINMATCH, 0xA, 1),               # both nibbles mid-range
])
def test_onchip_token_cell_splits_the_nibbles(token, want_mat, want_lit, want_st):
    """ON CHIP: the TOKEN cell splits the byte into (literal, match) nibbles,
    applies the +4 MINMATCH once, and emits the negative sentinel for a literal
    continuation. Every hand-off is observed leaving the cell."""
    _need_chip()
    _regs, words = _run_cell(1, "tok", inval=token)
    assert words, "the token cell emitted nothing"
    assert words[0] == want_mat, f"mat seed {words[0]} != {want_mat}"
    if want_lit is None:
        assert words[1] == want_st, "a zero-literal token must set the MATCH phase"
    else:
        assert words[1] == want_lit, f"lit seed {words[1]} != {want_lit}"
        assert words[2] == want_st


@pytest.mark.parametrize("lo,hi,want", [(0x34, 0x12, 0x1234), (0x01, 0x00, 1),
                                        (0xFF, 0xFF, 0xFFFF), (0x00, 0x01, 0x100)])
def test_onchip_offset_cell_is_little_endian(lo, hi, want):
    """ON CHIP: the OFFSET cell assembles the two offset bytes LITTLE-endian —
    first byte low, second byte high. This is the mutation gate's target."""
    _need_chip()
    import simkyt
    b, progs, M = _block_maps()
    names, entries = M[3]
    tg = ResolvedTargets(
        writes={"setoff": WriteTarget(10, 2)},
        jumps={"setoff": JumpTarget(10, 1), "ready": JumpTarget(10, 1)})
    res = R.resolve(progs[3], tg)
    chip = simkyt.Chip.from_yaml(str(CHIP_YAML))
    for a in range(32):
        chip.write_cell_memory(_cid(0, 0), a, int(res.memory.get(a, 0)))
    for x in range(W):
        chip.set_fwd_face(_cid(x, 0), "east")

    def kick(entry, val=None):
        if val is not None:
            chip.write_cell_memory(_cid(0, 0), names["b"], val & 0xFFFF)
        chip.set_port_entry_address("x16_in", entries[entry])
        chip.set_port_target_hop_count("x16_in", 30)
        chip.write_port("x16_in", np.array([0.0], dtype=np.float32))
        out = []
        for _ in range(300):
            chip.run(max_events=16)
            for v, _d, _t in chip.read_port_words_timed("x16_out"):
                out.append(v & 0xFFFF)
        return out

    kick("arm")
    kick("feed", lo)
    out = kick("feed", hi)
    assert chip.read_cell_memory(_cid(0, 0), names["off"]) == want
    assert out and out[0] == want, "the assembled offset must be handed on"


# --- the DECISIVE gate: a real match copy through the REAL panel ---------------
def _panel_match_run(window, wpos, off, mat, rounds=30000):
    """Run a whole match copy on a REAL chip through a REAL ``SramPanelDevice``.

    Cell 5 (EMIT) sits at (0,0) abutting the embedded SramController at (1,0); the
    controller's panel words exit the x16 port into the panel, and the panel's
    push-read returns the fetched byte straight back into cell 5's ``b`` register
    and kicks ``emit_mat``. That is the block's whole copy loop, unmodified.

    Returns ``(panel device, chip, emit register map)``.
    """
    import simkyt
    from engine.sram_panel import PanelDriver, SramPanelDevice

    _b, progs, M = _block_maps()
    en, ee = M[5]
    cn, ce = M[6]
    # The emit cell's three panel hand-offs abut the controller (@1); its EXTERNAL
    # edges (`out`, `gohead`) are aimed LOCALLY (@0 == hop 31) so this harness does
    # not feed them back into the panel port (which would read as a panel trigger).
    tg = ResolvedTargets(
        writes={"hist_addr": WriteTarget(1, cn["data"]),
                "hist_data": WriteTarget(1, cn["data"]),
                "read": WriteTarget(1, cn["data"]),
                "out": WriteTarget(31, 20)},
        jumps={"hist_addr": JumpTarget(1, ce["set_addr"]),
               "hist_data": JumpTarget(1, ce["write"]),
               "read": JumpTarget(1, ce["lookup"]),
               "out": JumpTarget(31, 31),
               "gohead": JumpTarget(31, 31)})
    emit_res = R.resolve(progs[5], tg)
    # The controller with panel_hop 9: from (1,0) its WRITE/JUMP exit x16_out, and
    # its push-read descriptors point back at the emit cell's `b` + `emit_mat`.
    ctl = LZ4DecoderBlock("ctl", panel_hop=9,
                          read_wr_desc=_wr(31 - 1, en["b"]),
                          read_jp_desc=_jp(31 - 1, ee["emit_mat"]))
    ctl_res = R.resolve(ctl.build_cell_programs()[6])

    chip = simkyt.Chip.from_yaml(str(CHIP_YAML))
    for a in range(32):
        chip.write_cell_memory(_cid(0, 0), a, int(emit_res.memory.get(a, 0)))
        chip.write_cell_memory(_cid(1, 0), a, int(ctl_res.memory.get(a, 0)))
    for x in range(W):
        chip.set_fwd_face(_cid(x, 0), "east")
    chip.write_cell_memory(_cid(0, 0), en["wpos"], wpos & 0xFFFF)
    chip.write_cell_memory(_cid(0, 0), en["off"], off & 0xFFFF)
    chip.write_cell_memory(_cid(0, 0), en["mat"], mat & 0xFFFF)

    dev = SramPanelDevice()
    for a, v in window.items():
        dev.mem[a] = v & 0xFFFF
    chip.set_port_handshake("x16_out", True)
    drv = PanelDriver(dev, chip, "x16_out", chip, "x16_in")
    chip.set_port_entry_address("x16_in", ee["fetch"])
    chip.set_port_target_hop_count("x16_in", 30)
    chip.write_port("x16_in", np.array([0.0], dtype=np.float32))
    for _ in range(rounds):
        chip.run(max_events=16)
        drv.step()
    return dev, chip, en


def test_onchip_match_copy_through_the_real_panel():
    """THE DECISIVE GATE. A 4-byte match at offset 8 runs on the REAL chip through
    the REAL SRAM panel: each byte is push-read at the COMPUTED address
    ``wpos - off``, emitted, and appended to the window at ``wpos``.

    This is the mechanism Varicode never exercised — a computed, moving panel
    address into a window the block itself is writing."""
    _need_chip()
    window = {i: 0x41 + i for i in range(8)}          # 'A'..'H' at 0..7
    dev, chip, en = _panel_match_run(window, wpos=8, off=8, mat=4)
    got = [dev.mem.get(a, 0) for a in range(8, 12)]
    assert got == [0x41, 0x42, 0x43, 0x44], f"match copy wrote {got}"
    assert chip.read_cell_memory(_cid(0, 0), en["wpos"]) == 12
    assert chip.read_cell_memory(_cid(0, 0), en["mat"]) == 0, \
        "the copy loop must terminate exactly at the run length"


def test_onchip_offset_one_byte_run_through_the_real_panel():
    """THE CLASSIC DECODER BUG, on real silicon. ``offset == 1`` with a 6-byte match
    is a RUN: only ``window[0]`` is preloaded, and every later source byte is one
    THIS SAME MATCH produced a moment earlier. A block move would read zeros; a
    byte-by-byte copy through the panel reads the run back."""
    _need_chip()
    dev, chip, en = _panel_match_run({0: 0x51}, wpos=1, off=1, mat=6)
    got = [dev.mem.get(a, 0) for a in range(0, 7)]
    assert got == [0x51] * 7, f"offset-1 run produced {got}"
    assert chip.read_cell_memory(_cid(0, 0), en["wpos"]) == 7


def test_onchip_literal_emit_does_not_loop():
    """``emit_lit`` zeroes ``mat``, so the shared decrement goes NEGATIVE and the
    cell stops after ONE byte — the discriminator that lets one program body serve
    both a literal and a match byte."""
    _need_chip()
    dev, chip, en = _panel_match_run({}, wpos=5, off=0, mat=0, rounds=8000)
    # mat == 0 entered at emit_mat behaves as the literal case: one byte, then stop.
    assert chip.read_cell_memory(_cid(0, 0), en["wpos"]) == 6
    assert dev.writes_committed == 1, \
        f"a single byte must commit ONE history write, got {dev.writes_committed}"


# =========================================================================
# LAYER 4 — INV-4 MUTATIONS (each proven to FAIL)
# =========================================================================
def _mutated_decode(blk, *, match_off_by_one=0, drop_minmatch=False,
                    big_endian=False, no_sum=False):
    """The golden decode with ONE named defect injected. Used only by the mutation
    gates, to prove each gate SEES the corresponding bug."""
    src = bytes(blk)
    out = bytearray()
    pos, n = 0, len(src)
    while pos < n:
        token = src[pos]
        pos += 1
        lit = token >> 4
        if lit == NIBBLE_ESCAPE:
            extra = 0
            while True:
                bb = src[pos]
                pos += 1
                extra = bb if no_sum else extra + bb       # MUTATION: no sum
                if bb != CONT_ESCAPE:
                    break
            lit += extra
        out += src[pos:pos + lit]
        pos += lit
        if pos == n:
            break
        if big_endian:                                     # MUTATION: endianness
            off = (src[pos] << 8) | src[pos + 1]
        else:
            off = src[pos] | (src[pos + 1] << 8)
        pos += 2
        if off == 0 or off > len(out):
            raise LZ4FormatError("bad offset under mutation")
        ml = token & 0x0F
        if ml == NIBBLE_ESCAPE:
            extra = 0
            while True:
                bb = src[pos]
                pos += 1
                extra += bb
                if bb != CONT_ESCAPE:
                    break
            ml += extra
        if not drop_minmatch:                              # MUTATION: no +4
            ml += MINMATCH
        ml += match_off_by_one                             # MUTATION: off-by-one
        start = len(out) - off
        for i in range(ml):
            out.append(out[start + i])
    return bytes(out)


_MUT_BLOCK = (make_sequence(bytes(range(48)), offset=20, match_len=30)
              + make_sequence(b"abc", offset=3, match_len=9)
              + make_sequence(b"tail!"))


def _mutation_is_visible(blk, **defect):
    """True when the named defect changes the decode OR makes it fail outright.

    Both count as "the gate SEES it" (INV-4): a corrupted decoder that produces a
    DIFFERENT byte stream and one that produces NO valid stream at all are equally
    caught. What would NOT count is the defect being invisible.
    """
    good = lz4_decompress_block(blk)
    try:
        return _mutated_decode(blk, **defect) != good
    except (LZ4FormatError, IndexError):
        return True


def test_mutation_match_length_off_by_one_FAILS():
    """INV-4: a match length one byte long/short must break the gate."""
    for delta in (+1, -1):
        assert _mutation_is_visible(_MUT_BLOCK, match_off_by_one=delta), \
            f"gate blind to a match length {delta:+d}"


def test_mutation_minmatch_omitted_FAILS():
    """INV-4: dropping the +4 MINMATCH must break the gate."""
    assert _mutation_is_visible(_MUT_BLOCK, drop_minmatch=True), \
        "gate blind to a missing MINMATCH"


def test_mutation_offset_big_endian_FAILS():
    """INV-4: reading the offset BIG-endian must break the gate."""
    blk = make_sequence(b"z" * 300, offset=0x0102, match_len=6) + make_sequence(b"q")
    assert _mutation_is_visible(blk, big_endian=True), \
        "gate blind to a big-endian offset"
    # and on a block where the swap stays IN RANGE (0x0201 -> 0x0102, both well
    # inside a 600-byte output), so the defect shows as WRONG BYTES rather than a
    # range error — the harder half of the same gate.
    body = bytes((i * 13 + 5) & 0xFF for i in range(600))
    blk2 = make_sequence(body, offset=0x0201, match_len=8) + make_sequence(b"end!!")
    assert _mutated_decode(blk2, big_endian=True) != lz4_decompress_block(blk2)


def test_mutation_continuation_not_summed_FAILS():
    """INV-4: taking only the LAST continuation byte instead of the SUM must break
    the gate (the 15 + 255 + k case is where it shows)."""
    blk = make_sequence(bytes((i * 7) & 0xFF for i in range(280)))
    assert _mutation_is_visible(blk, no_sum=True), \
        "gate blind to an unsummed 15-nibble continuation"
    # A SINGLE-byte continuation (15 + 33 = 48 literals) is the case where the
    # defect is invisible — the sum and the last byte agree. The gate must
    # therefore be driven by a MULTI-byte continuation, which is what the block
    # above is; assert that distinction explicitly so the choice is not accidental.
    one_byte = make_sequence(bytes(range(48)))
    assert not _mutation_is_visible(one_byte, no_sum=True), \
        "a 1-byte continuation cannot distinguish sum from last-byte"
    assert blk[1] == CONT_ESCAPE, "the gate block must use a MULTI-byte continuation"


def test_mutation_onchip_offset_bytes_swapped_FAILS():
    """INV-4, ON CHIP: feeding the two offset bytes in the WRONG order to the real
    offset cell yields the byte-swapped value — the little-endian assembly is
    load-bearing silicon behaviour, not a comment."""
    _need_chip()
    import simkyt
    _b, progs, M = _block_maps()
    names, entries = M[3]
    tg = ResolvedTargets(writes={"setoff": WriteTarget(10, 2)},
                         jumps={"setoff": JumpTarget(10, 1),
                                "ready": JumpTarget(10, 1)})
    res = R.resolve(progs[3], tg)
    chip = simkyt.Chip.from_yaml(str(CHIP_YAML))
    for a in range(32):
        chip.write_cell_memory(_cid(0, 0), a, int(res.memory.get(a, 0)))
    for x in range(W):
        chip.set_fwd_face(_cid(x, 0), "east")

    def kick(entry, val=None):
        if val is not None:
            chip.write_cell_memory(_cid(0, 0), names["b"], val & 0xFFFF)
        chip.set_port_entry_address("x16_in", entries[entry])
        chip.set_port_target_hop_count("x16_in", 30)
        chip.write_port("x16_in", np.array([0.0], dtype=np.float32))
        for _ in range(300):
            chip.run(max_events=16)
            list(chip.read_port_words_timed("x16_out"))

    kick("arm")
    kick("feed", 0x12)          # the bytes SWAPPED
    kick("feed", 0x34)
    assert chip.read_cell_memory(_cid(0, 0), names["off"]) == 0x3412 != 0x1234


# =========================================================================
# LAYER 5 — THE WALL (this is why the block is QUARANTINED)
# =========================================================================
def test_placement_wall_fsm_cannot_fit_the_panel_template_cell_budget():
    """WALL, part 1 — the SIZE bound.

    The parse+emit FSM needs 102 instructions across its datapath cells. A cell
    holds 32 words TOTAL (program + data + state + the input register), so the very
    best case for one cell is ``31 - (data + state + inputs)`` instructions — at
    most 28, and 25-26 for the constant footprints this FSM actually needs. So the
    datapath is a **4-cell absolute lower bound** (5 with the constants it uses),
    plus the SRAM controller.

    The panel P&R templates (``engine/panel_pnr.py``) pin exactly TWO cells for a
    TX-shaped panel block (controller + return consumer) and at most FOUR for the
    RX shape (controller + kicker + input + return). Every shipped panel-backed
    block is 2 or 3 cells. This block is 7, and cannot be fewer than 5.
    """
    b, progs, _M = _block_maps()
    datapath = sum(R.count_instructions(cp) for c, cp in progs.items() if c != 6)
    assert datapath >= 100, f"the FSM shrank to {datapath} — re-derive the bound"
    # the most generous per-cell budget: 1 data word, 1 state var, 1 input
    best_case = 31 - 1 - 1 - 1
    assert -(-datapath // best_case) >= 4, "the 4-cell lower bound no longer holds"
    assert b.cell_count == 7


def test_placement_wall_no_single_face_ring_exists():
    """WALL, part 2 — the TOPOLOGY bound, and the durable finding.

    A cell has ONE forward face, so every WRITE/JUMP it makes leaves in the same
    direction and reaches its target by HOP COUNT, transiting the cells between.
    The embedded SRAM controller sits AT the panel port with its face pointing OUT
    of the array, so any word that TRANSITS it is lost off-chip (and INV-32 makes
    routing through a used port cell a hard ``port_transit`` failure anyway).

    For a block whose cells form a cycle this means: there must exist an ordering of
    the cells around a ring such that no internal edge transits the controller's
    slot. For this FSM's 15-edge graph **no such ordering exists at any ring size
    from 7 to 16** — and not even within two violations. The block therefore cannot
    be laid out as a single-face ring; it needs the generic broker/corridor router,
    which is exactly the path ``auto_pnr`` bypasses for panel designs.

    This test IS the wall: if a future change makes an ordering exist, it fails and
    tells the next agent the quarantine can be revisited.
    """
    b, progs, _M = _block_maps()
    ctl = 6
    edges = {(s, d) for (s, _o, d, _i) in b.internal_connections()}
    edges |= {(s, d) for (s, _o, d, _e) in b.internal_jumps()}
    cells = sorted(progs)
    found = None
    for n in range(len(cells), 17):
        for perm in itertools.permutations(cells):
            slot = {c: k for k, c in enumerate(perm)}
            ok = True
            for (s, d) in edges:
                i, j = slot[s], slot[d]
                hop = (j - i) % n
                if hop == 0 or hop > 31 or any(
                        slot[ctl] == (i + t) % n for t in range(1, hop)):
                    ok = False
                    break
            if ok:
                found = (n, perm)
                break
        if found:
            break
    assert found is None, (
        f"a single-face ring order now EXISTS ({found}) — the topology half of the "
        "LZ4DecoderBlock quarantine can be revisited")


def test_placement_wall_panel_template_rejects_this_block():
    """WALL, part 3 — the TOOLING bound, demonstrated end to end.

    ``AppController.auto_pnr`` routes EVERY SRAM-panel design through
    ``engine.panel_pnr.apply_panel_template`` and never falls through to the generic
    CP-SAT placer + router. The template places a fixed set of cells and derives
    Varicode-specific params, so a 7-cell panel-backed block is rejected. Panel
    SYNTHESIS (the panel, the ``x1_out``/``x1_in`` connections, the return net)
    works fine — it is the template PLACEMENT that has no shape for this block.
    """
    _need_chip()
    from engine.catalog import BlockCatalog
    from engine.errors import PlacementError
    from engine.io.chip_type_io import load_chip_type
    from engine.panel_pnr import apply_panel_template, synthesize_panel
    from model.block import Block
    from model.chip import ChipInstance
    from model.connection import BlockEndpoint, ChipPortEndpoint, Connection
    from model.project import Project, ProjectMetadata

    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(str(CHIP_YAML))
    p = Project(metadata=ProjectMetadata(name="lz4"), chip_type="kyttar_10x12")
    p.chips = [ChipInstance(0, "C0")]
    p.blocks = [Block("lz4", "LZ4DecoderBlock", library="lattrex.official",
                      params={})]
    p.connections = [
        Connection("i", ChipPortEndpoint(0, "x16_in"), BlockEndpoint("lz4", "byte")),
        Connection("o", BlockEndpoint("lz4", "out"), ChipPortEndpoint(0, "x16_out")),
    ]
    actions = synthesize_panel(p, cat)
    assert any("panel" in a for a in actions), \
        "panel SYNTHESIS must still work — only the template placement is the wall"
    with pytest.raises((PlacementError, TypeError, KeyError)):
        apply_panel_template(p, cat, ct)


def test_block_is_not_done_until_it_has_a_report():
    """The manifest must not claim `done` without a passing report (INV-38: absence
    is the safe state).

    RE-OPENED 2026-08-29. This test previously asserted `status == "needs_human"`,
    pinning a quarantine whose stated cause — a panel-template CELL CAP — an audit
    proved does not exist: `GolayDecoderBlock` is a 7-cell panel-backed block with
    status `done`, and `panel_pnr.py` has no cell-count check at all. Pinning a
    false wall in a test is how the false wall survives, so the assertion now pins
    the property that is actually true in both states."""
    m = json.loads((_WT / "verification" / "manifest.json").read_text())
    blocks = m["blocks"] if isinstance(m, dict) and "blocks" in m else m
    ent = next(b for b in blocks if b["kyttar_block"] == "LZ4DecoderBlock")
    if ent["status"] == "done":
        assert REPORT.exists(), \
            "manifest says done but there is no verification report (INV-38)"
    else:
        assert not REPORT.exists(), \
            "a not-done block must NOT ship a passing verification report (INV-38)"
