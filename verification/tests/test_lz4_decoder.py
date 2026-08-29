# SPDX-License-Identifier: GPL-3.0-or-later
"""LZ4DecoderBlock — the golden, the cell programs on the REAL chip through the
REAL SRAM panel, and the auto-placed design end to end.

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
  3. **THE CHIP, per cell.** The cell programs run on a real ``simkyt`` chip: the
     token nibble split, the little-endian offset assembly, the match-length
     continuation relay, and a whole match copy driven through a real
     ``SramPanelDevice`` + ``PanelDriver``, including the ``offset == 1`` byte-run
     whose later source bytes are produced BY the copy.
  4. **INV-4 mutations**, each proven to FAIL — including three that corrupt the
     REAL block and rebuild it on chip.
  5. **PLACEMENT**: the block goes through the panel template, all EIGHT of its cells
     land on the fabric, the three corridors are drawn, DRC is clean, and the build
     binds each program to its placed cell with the panel hand-offs intact.
  6. **THE WHOLE DESIGN.** The AUTO-PLACED, ROUTED, BUILT chip decodes 8 payload
     classes byte-exact, and decodes blocks the REFERENCE C COMPRESSOR produced.
     This is the one that matters: three defects passed every layer above it and
     were caught only here.
  7. **INV-23 ORIENTATION**: the fold is a rigid unit in all 8 D4 orientations,
     and its in-program face constants rotate with it.

This suite previously ended in three ``test_placement_wall_*`` gates pinning a
QUARANTINE, and then in two gates pinning the placement GAP. All five are gone.
The header of LAYER 5 records what the wall gates got wrong; the gap gates were
INVERTED rather than deleted (an assertion that a limit HOLDS passes precisely
while the block is broken), and are now
``test_every_internal_edge_DELIVERS_under_the_real_forwarding_rule`` and
``test_the_emit_cell_can_afford_the_ONE_flip_the_layout_asks_of_it``. See INV-48
and INV-50.

Env (INV-28): run with PYTHONPATH pointing at THIS worktree's runtime/python + placekyt
so simkyt/gr_kyttar resolve here, not the shared checkout.
"""
from __future__ import annotations

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
    """Each of the eight cells resolves inside its 32-word budget, with its state
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


def _dummy_targets(cp, distance=1):
    """Every output aimed at ``distance`` hops. With ``distance=10`` on a cell at
    (0,0) of an all-east row, every hand-off the cell makes EXITS the x16 port and
    is observable — which is how the per-cell gates read a cell's traffic."""
    tg = ResolvedTargets()
    for p in cp.outputs:
        tg.writes[p.name] = WriteTarget(distance=distance, target_addr=25)
        tg.jumps[p.name] = JumpTarget(distance=distance, target_addr=1)
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
    res = R.resolve(progs[3], _dummy_targets(progs[3], distance=10))
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


def test_onchip_offset_cell_relays_a_match_length_continuation_byte():
    """ON CHIP: once both offset bytes are in (``nb == 0``), the OFFSET cell is
    still the match phase's landing cell — the router steers EVERY ``ST_MATCH``
    byte here — so a match-length CONTINUATION byte must be RELAYED to the
    MATCHLEN cell, not swallowed.

    This is the path a token with match nibble 15 takes, i.e. every match longer
    than 18 bytes. Without the relay the byte is dropped, the run never fires,
    and the decoder stalls on ordinary English text (measured: `b'Q'*40`,
    `b'abc'*12` and the reference `repetitive` payload all stopped at the first
    match).
    """
    _need_chip()
    import simkyt
    b, progs, M = _block_maps()
    names, entries = M[3]
    res = R.resolve(progs[3], _dummy_targets(progs[3], distance=10))
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
    kick("feed", 0x08)                    # the two offset bytes
    kick("feed", 0x00)
    assert chip.read_cell_memory(_cid(0, 0), names["nb"]) == 0
    out = kick("feed", 0x2A)              # a match-length continuation byte
    assert out and out[0] == 0x2A, (
        f"a continuation byte arriving with nb == 0 must be relayed on "
        f"unchanged, got {out}")


# --- the DECISIVE gate: a real match copy through the REAL panel ---------------
def _panel_match_run(window, wpos, off, mat, rounds=30000):
    """Run a whole match copy on a REAL chip through a REAL ``SramPanelDevice``.

    Cell 5 (EMIT) sits at (0,0) abutting the embedded SramController at (1,0); the
    controller's panel words exit the x16 port into the panel, and the panel's
    push-read returns the fetched byte straight back into cell 5's ``b`` register
    and kicks ``emit_mat``. That is the block's whole copy loop, unmodified.

    THE EMIT CELL RESTS ON ITS RING FACE and flips toward the panel for the burst,
    so this harness lays it on an all-EAST row and lets the flip do its work: the
    in-program ``MOVE [FACE]`` writes the hardware face, exactly as it does on the
    placed design.

    SEED BOTH COUNTERS. There is no per-byte ``set_addr`` any more — the
    controller's ``write`` entry auto-increments its OWN ``wraddr``, which boots at
    0. A test that presets ``wpos`` mid-stream must therefore preset ``wraddr`` to
    the same value, or the history bytes land at 0.. instead of at ``wpos``. (An
    earlier pass read that mismatch as a DSP bug and reverted a correct change.)

    Returns ``(panel device, chip, emit register map)``.
    """
    import simkyt
    from engine.sram_panel import PanelDriver, SramPanelDevice

    _b, progs, M = _block_maps()
    en, ee = M[5]
    cn, ce = M[6]
    on, oe = M[7]
    # The emit cell's panel hand-offs reach the controller at @2 (they transit the
    # OUT cell, which rests toward the controller — see the block's default_layout);
    # here the harness abuts them at @1 with the OUT cell aimed locally, because the
    # point of this gate is the PANEL protocol, not the fold. `gohead` is aimed
    # LOCALLY (@0 == hop 31) so it is not fed back into the panel port.
    tg = ResolvedTargets(
        writes={"hist_data": WriteTarget(1, cn["data"]),
                "read": WriteTarget(1, cn["data"]),
                "out": WriteTarget(31, 20)},
        jumps={"hist_data": JumpTarget(1, ce["write"]),
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
    # Seed the controller's OWN write counter to match (see the docstring).
    chip.write_cell_memory(_cid(1, 0), cn["wraddr"], wpos & 0xFFFF)

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
    res = R.resolve(progs[3], _dummy_targets(progs[3], distance=10))
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
# LAYER 5 — PLACEMENT: the block goes through auto_pnr and lands on the fabric
# =========================================================================
# This layer replaced three ``test_placement_wall_*`` gates that pinned a
# QUARANTINE. All three are gone, and it is worth recording why, because two of
# them looked rigorous and were not:
#
#   * "the panel template caps cell count" — there is NO cell-count check
#     anywhere in ``engine/panel_pnr.py``; its only count limit is
#     ``len(backed) > 2``, which caps how many BLOCKS may be panel-backed in one
#     design. ``GolayDecoderBlock`` had already shipped as a 7-cell panel block.
#   * "no single-face ring ordering exists" — true, and irrelevant: the search
#     only enumerated RINGS, where every edge must run the same way round a
#     cycle. The fabric is not a ring. On a LINE each cell picks its own face
#     independently, and ``test_layout_is_internally_routable`` below exhibits an
#     ordering that satisfies all 25 edges with zero violations.
#   * "the template rejects this block" — it asserted only that SOMETHING was
#     raised, with no message check. The same shape raised for the WORKING
#     3-cell Varicode decoder too, so it proved nothing about this block.
#
# The lesson is in the KB's evidence rule: measure the limit, check the shipped
# blocks, name the layer, and state the reach.


def _reachable_from(pos, face, start, W, H, limit=31):
    """The cells a word from ``start`` visits, under the REAL forwarding rule.

    Read off a simkyt trace: the word leaves ``start`` on ``start``'s face, and
    every cell it then arrives at forwards it on THAT CELL'S OWN face. So each
    cell has exactly ONE outgoing walk. (A straight-line "ray" model — the word
    keeps the sender's direction the whole way — is WRONG, and believing it is
    how this block got a layout that places and builds but ping-pongs at run
    time: the router's westward word hit the eastward-facing emit cell and came
    straight back.)
    """
    step = {"east": (1, 0), "west": (-1, 0), "north": (0, -1), "south": (0, 1)}
    occ = {p: c for c, p in pos.items()}
    seen = []
    x, y = pos[start]
    dx, dy = step[face[start]]
    for _ in range(limit):
        x, y = x + dx, y + dy
        if not (0 <= x < W and 0 <= y < H):
            break
        c = occ.get((x, y))
        seen.append(c)
        if c is not None:
            dx, dy = step[face[c]]
    return seen


def _flip_walks(pos, face, start, W, H):
    """Every walk out of ``start`` — the resting face first, then the three flip
    faces — as ``{face: [cells visited]}``."""
    return {f: _reachable_from(pos, {**face, start: f}, start, W, H)
            for f in ("east", "west", "north", "south")}


def test_the_merged_INV50_rule_keeps_BOTH_halves_of_the_walk():
    """INV-50 was closed by TWO passes that fixed OPPOSITE halves of one walk, and
    this pins that neither half can be dropped without a red test.

    A walk is "leave the source on face F, then turn at every occupied cell you
    cross". ``ChaCha20KeystreamBlock`` fixed F (``emit_faces()``, a neighbour CELL
    ID so it survives rotation); ``LZ4DecoderBlock`` fixed the turns
    (``BlockDefinition.cell_faces``, the block's authored faces, because at
    internal-resolution time the ``cell_map`` still holds the router's positional
    guesses). This test asserts both mechanisms are still WIRED — a merge that
    kept only one would leave the other's block silently mis-sized, which is
    exactly the failure mode neither pass's own suite would notice.
    """
    import inspect
    from gr_kyttar.placement.router import Router
    from gr_kyttar.placement.block import BlockDefinition

    sig = inspect.signature(Router._get_routing_distance).parameters
    assert "start_face" in sig, (
        "the FIRST-step half is gone: _get_routing_distance no longer takes "
        "start_face, so a flipped edge is sized along the resting walk again")
    assert "authored" in sig, (
        "the TRANSIT half is gone: _get_routing_distance no longer takes "
        "authored faces, so the walk crosses the fold on the router's guesses")
    assert "strict" in sig, (
        "the strict failure mode is gone — an internal edge that reaches "
        "nothing would silently take the Manhattan estimate again")
    assert hasattr(Router, "_declared_emit_face"), \
        "emit_faces() resolution (the ChaCha half) was dropped"
    assert hasattr(Router, "_authored_faces"), \
        "cell_faces resolution (the LZ4 half) was dropped"
    assert "cell_faces" in {f.name for f in
                            __import__("dataclasses").fields(BlockDefinition)}, \
        "BlockDefinition.cell_faces was dropped"
    assert "orientation" in {f.name for f in
                             __import__("dataclasses").fields(BlockDefinition)}, (
        "BlockDefinition.orientation was dropped — declared flip faces are "
        "stored UNROTATED, so without it a rotated block flips the wrong way")
    # A DECLARED emit face must WIN outright: inference knows which faces a cell
    # has, never which port uses which, so a declaration must not be second-guessed.
    src = inspect.getsource(Router._internal_distance)
    assert "declared_face is not None" in src, (
        "a declared emit face is no longer authoritative — the router would "
        "fall back to inference and could 'fix' a wrong declaration silently")


def test_every_internal_edge_DELIVERS_under_the_real_forwarding_rule():
    """THE ROUTABILITY GATE. Every one of the block's internal edges reaches its
    target under the REAL forwarding rule (:func:`_reachable_from`) — a word
    leaves on its SOURCE cell's face and turns at every occupied cell it crosses.

    This test used to pin a KNOWN GAP: an exact set of four edges the 7x1 row
    could not deliver (``0->1``, ``0->2``, ``0->3``, ``5->6``). The gap is CLOSED,
    so it now asserts the positive property, which is strictly stronger — a
    regression that breaks any edge fails here rather than being absorbed into a
    "known" set.

    It also pins WHERE the flips are: cell 5 is the only cell that needs one, and
    it needs exactly ONE. That is the whole point of the 8th cell (see the class
    docstring): with the egress still in cell 5, that cell needed THREE
    directions, which costs 6 words against the 5 it has.
    """
    b = LZ4DecoderBlock("lz4")
    lay = b.default_layout()
    pos = {c: (x, y) for c, (x, y, _f) in lay.items()}
    face = {c: f for c, (_x, _y, f) in lay.items()}
    ctl = b.panel_requirements()["controller_cell"]
    W = max(x for x, _y in pos.values()) + 1
    H = max(y for _x, y in pos.values()) + 1

    edges = ({(s, d) for s, _o, d, _i in b.internal_connections()}
             | {(s, d) for s, _o, d, _e in b.internal_jumps()})
    unroutable = set()
    flips = {c: set() for c in pos}
    for (s, d) in sorted(edges):
        walks = _flip_walks(pos, face, s, W, H)
        hit = None
        for f in (face[s], "east", "west", "north", "south"):
            seen = walks[f]
            if d in seen and (ctl not in seen[:seen.index(d)] or d == ctl):
                hit = f
                break
        if hit is None:
            unroutable.add((s, d))
        elif hit != face[s]:
            flips[s].add(hit)

    assert not unroutable, (
        f"internal edges that do NOT deliver: {sorted(unroutable)}. A word turns "
        "at every occupied cell it crosses (INV-48), so each target must lie on "
        "one of its source cell's four walks — and not behind the controller, "
        "which would push it into the SRAM port.")
    assert {c: sorted(v) for c, v in flips.items() if v} == {5: ["east"]}, (
        f"the FLIP set changed: {({c: sorted(v) for c, v in flips.items() if v})}. "
        "Exactly one cell (5, EMIT) may need exactly one flip — every other edge "
        "must ride its source cell's resting face. More flips than that means the "
        "fold regressed and some cell is paying 3 words it may not have.")


def test_the_emit_cell_can_afford_the_ONE_flip_the_layout_asks_of_it():
    """The budget arithmetic, as a POSITIVE assertion.

    This test used to assert the opposite — that the emit cell had FEWER free
    words than a flip costs — and it pinned the gap that stopped this block. Both
    halves of that gap are closed, and each is asserted here:

    * dropping the redundant per-byte ``set_addr`` freed 3 words (2 -> 5);
    * splitting the egress onto cell 7 dropped the emit cell from THREE
      directions to TWO, so it needs ONE flip (3 words), not two (6).

    A change that puts a third direction back on the emit cell, or that spends
    its words elsewhere, fails here with the arithmetic spelled out.
    """
    b = LZ4DecoderBlock("lz4")
    progs = b.build_cell_programs()

    def free_words(cid):
        cp = progs[cid]
        n = R.count_instructions(cp)
        dm = R._allocate_data(cp.data)
        nd = max(dm.values(), default=-1) + 1
        return (31 - n) - nd - len(cp.state) - len(cp.inputs)

    for cid in sorted(progs):
        assert free_words(cid) >= 0, (
            f"cell {cid} is over budget by {-free_words(cid)} words")
    # The emit cell's flip is AUTHORED (two `MOVE [FACE]` pairs + two is_face
    # DataWords are already inside the program), so what must hold is that the
    # program with the flip in it still resolves — which the loop above checks —
    # and that the flip is really there.
    emit = progs[b.panel_requirements()["panel_client_cell"]]
    faces = [d for d in emit.data if getattr(d, "is_face", False)]
    assert len(faces) == 2, (
        f"the emit cell must carry exactly two is_face DataWords (its resting "
        f"ring face and the panel face it flips to), found {len(faces)}")
    assert emit.assembly_template.count("MOVE [FACE]") == 4, (
        "the emit cell must flip and RESTORE on both of its bursts (the fetch "
        "and the emit body) — an unrestored flip leaves the cell facing the "
        "panel, and the ring words that transit it (cells 1 and 2 reaching the "
        "router's `st`) are then deflected into the controller")


def test_layout_leaves_an_egress_cell_on_the_output_cell_walk():
    """The OUTPUT cell's ``out`` WRITE rides one of its faces, so the layout must
    leave a FREE cell on that walk BEFORE the controller.

    Without it the output would transit the controller — whose face points at the
    panel — and be pushed into the SRAM port instead of the output corridor. The
    self-contained template raises a named PlacementError when this cell is
    missing, so this test pins the layout property the template depends on.

    Note WHICH cell: the egress belongs to ``output_cell`` (7), not to the return
    cell (5). Those were the same cell until this block needed them apart.
    """
    b = LZ4DecoderBlock("lz4")
    lay = b.default_layout()
    req = b.panel_requirements()
    out_c, ctl = req["output_cell"], req["controller_cell"]
    assert out_c != req["return_cell"], (
        "this block's egress is on its OWN cell — that split is what let the "
        "emit cell fit its one flip")
    pos = {c: (x, y) for c, (x, y, _f) in lay.items()}
    occ = {p: c for c, p in pos.items()}
    found = None
    for f in (lay[out_c][2], "north", "east", "west", "south"):
        dx, dy = {"east": (1, 0), "west": (-1, 0),
                  "north": (0, -1), "south": (0, 1)}[f]
        sx, sy = pos[out_c]
        for k in range(1, 32):
            p = (sx + dx * k, sy + dy * k)
            if occ.get(p) == ctl:
                break
            if p not in occ:
                found = (f, k, p)
                break
        if found:
            break
    assert found is not None, (
        "no free cell on any of the output cell's walks before the controller — "
        "the block's `out` WRITE has nowhere to land but the controller")


def test_auto_pnr_places_every_cell_and_routes_the_corridors():
    """THE PLACEMENT GATE. ``apply_panel_template`` places ALL EIGHT cells and
    draws the three corridors, and the result passes DRC with no errors.

    A role-named template places only the cells named in ``panel_requirements()``
    — 3 here — and the build binds programs to ``placement.cells`` BY INDEX, so a
    short placement is a silent-dead build twice over: the extra cells are absent
    AND the remaining programs land on the wrong positions. This asserts the
    whole placement, not just that something was produced.
    """
    _need_chip()
    from engine.catalog import BlockCatalog
    from engine.drc import check_project
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
    assert any("panel" in a for a in synthesize_panel(p, cat))
    results, _notes = apply_panel_template(p, cat, ct)

    blk = p.block("lz4")
    placed = {c.cell_id: (c.x, c.y) for c in blk.placement.cells}
    n_cells = LZ4DecoderBlock("probe").cell_count
    assert len(placed) == n_cells, (
        f"the template placed {len(placed)} of {n_cells} cells — the un-placed "
        "ones are silently absent from the build")
    assert sorted(placed) == list(range(n_cells)), placed
    # The build binds programs to placement.cells BY INDEX: the list must ascend.
    ids = [c.cell_id for c in blk.placement.cells]
    assert ids == sorted(ids), f"placement.cells is not in cell-id order: {ids}"
    # The controller sits ON the x1_out port cell, everything else off it.
    req = blk.placement.cells
    ctl = next(c for c in req if c.cell_id == 6)
    assert (ctl.x, ctl.y) == (9, 11), "the controller must sit on x1_out"
    assert len({(c.x, c.y) for c in req}) == n_cells, "cells overlap"
    # The return cell must land on the x1_in ROW — the push-read corridor runs
    # east along it and has to land ON that cell.
    ret = next(c for c in req if c.cell_id == 5)
    assert ret.y == 11, f"the return cell must sit on the x1_in row, got {ret.y}"
    # The OUT cell sits BETWEEN the emit cell and the controller and rests toward
    # the controller, so the emit cell's panel words transit it. That adjacency is
    # what lets ONE eastward flip serve both (cell 7 at hop 1, controller at 2).
    out = next(c for c in req if c.cell_id == 7)
    assert (out.x, out.y) == (ret.x + 1, ret.y) and (ctl.x, ctl.y) == (out.x + 1,
                                                                      out.y), (
        f"emit {(ret.x, ret.y)} -> out {(out.x, out.y)} -> ctl "
        f"{(ctl.x, ctl.y)} must be collinear and adjacent")
    assert out.face.name == "EAST", (
        "the OUT cell must REST facing the controller or the emit cell's panel "
        "words are deflected into the output corridor")
    # All three corridors were drawn.
    named = {r.name for r in results}
    assert {"in_to_block", "block_to_out"} <= named, named
    assert any("panel_return" in n for n in named), named
    assert all(r.ok for r in results)

    drc = check_project(p, {"kyttar_10x12": ct}, catalog=cat)
    errs = [f for f in drc.findings if getattr(f, "severity", "") == "error"]
    assert not errs, "DRC errors: " + "; ".join(
        f"{getattr(e, 'code', '?')}: {getattr(e, 'message', e)}" for e in errs)


def test_build_lands_each_program_on_its_placed_cell():
    """The BUILD binds each cell program to the position the template chose, and
    leaves the emit cell's panel hand-offs alone.

    The emit cell is both the block's egress AND the panel's client, so the
    build's exit-hop passes must honour ``RAW_OUTPUT_HOPS`` and not rewrite its
    WRITE/JUMP words to the output corridor — doing so aimed the panel protocol
    at ``x16_out`` and the history window was never written.
    """
    _need_chip()
    from engine.build import BuildEngine
    from engine.catalog import BlockCatalog
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
    synthesize_panel(p, cat)
    apply_panel_template(p, cat, ct)
    res = BuildEngine(cat, str(CHIP_YAML)).build(p, {"kyttar_10x12": ct})
    assert res.ok, [str(e) for e in res.errors]
    cb = res.chips[0]

    blk = p.block("lz4")
    pos = {c.cell_id: (c.x, c.y) for c in blk.placement.cells}
    inst = cat.instantiate("LZ4DecoderBlock", "probe", {},
                           library="lattrex.official")
    # Every cell's ENTRY on the fabric must be that cell program's own entry.
    for cid, xy in pos.items():
        built = cb.cells.get(xy)
        assert built is not None, f"cell {cid} at {xy} was not built"
        want = R.compute_entry_addresses(inst.build_cell_programs()[cid])
        assert built.get("entry") in want.values(), (
            f"cell {cid} at {xy}: built entry {built.get('entry')} is not one "
            f"of that program's entries {sorted(want.values())} — the programs "
            "are bound to the wrong cells")

    # The emit cell keeps its TWO distinct panel hand-offs: the controller's
    # `write` and `lookup` entries, each at the placed hop. A build that rewrote
    # them would collapse them onto one entry (the old defect).
    #
    # There are TWO, not three: the per-byte `set_addr` is gone. The controller's
    # `write` auto-increments its own `wraddr` and `lookup` drives a SEPARATE
    # `rdaddr`, so the two paths never collide — proven on silicon (see
    # test_onchip_match_copy_through_the_real_panel, which interleaves them) and
    # end to end (test_auto_placed_design_decodes_on_chip). Removing it freed the
    # three words the emit cell's face flip needed.
    emit_mem = cb.cells[pos[5]]["memory"]
    ctl_entries = R.compute_entry_addresses(inst.build_cell_programs()[6])
    jumps = {w & 0x1F for a, w in enumerate(emit_mem)
             if w and (w >> 12) & 0xF == 0x7}
    for name in ("write", "lookup"):
        assert ctl_entries[name] in jumps, (
            f"the emit cell no longer JUMPs the controller's {name!r} entry "
            f"(entries seen: {sorted(jumps)}) — the panel protocol was "
            "flattened by an exit-hop patch")
    assert ctl_entries["set_addr"] not in jumps, (
        "the emit cell JUMPs the controller's `set_addr` — the per-byte address "
        "latch is redundant (the controller auto-increments `wraddr`) and its "
        "three words are what pay for the emit cell's face flip")

    # The OUT cell issues the egress for the WHOLE corridor, not just the first
    # cell of it. The corridor is plain transit cells, and hardware forwards a
    # transiting word (HOP_CNT < 31) on the cell's face BEFORE any program runs —
    # only a landing word executes. So a WRITE aimed @1 parks the byte on the
    # first corridor cell and nothing ever leaves the port (measured: correct
    # decode inside the block, zero words at x16_out).
    eg = next(c for c in p.connections if c.name == "block_to_out")
    want_hop = len(eg.route) + 1                # transit every cell, then exit
    out_mem = cb.cells[pos[7]]["memory"]
    hops = {31 - ((w >> 5) & 0x1F) for w in out_mem
            if w and (w >> 12) & 0xF in (0x6, 0x7)}
    assert hops == {want_hop}, (
        f"the OUT cell's egress WRITE/JUMP carry hops {sorted(hops)}, expected "
        f"{want_hop} = the {len(eg.route)}-cell corridor plus the port exit")


# --- the whole thing, on the chip, from the auto-placed build ------------------
def _auto_build():
    """Synthesize + template-place + build the one-block LZ4 design.

    Returns ``(project, BuildResult)``. This is the same path ``auto_pnr`` takes
    for a panel design (``ui/controller.py`` delegates to ``apply_panel_template``
    and then routes the leftover block→block nets, of which this design has none).
    """
    from engine.build import BuildEngine
    from engine.catalog import BlockCatalog
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
    synthesize_panel(p, cat)
    apply_panel_template(p, cat, ct)
    res = BuildEngine(cat, str(CHIP_YAML)).build(p, {"kyttar_10x12": ct})
    assert res.ok, [str(e) for e in res.errors]
    return p, res


def _decode_on_chip(project, bres, src_bytes, idle_max=200):
    """Feed a compressed LZ4 block into the BUILT design on a real ``simkyt``
    chip with a real ``SramPanelDevice`` on the x1 pair, and return the bytes
    that leave ``x16_out``.

    Uses the current in-fabric panel API (``chip.register_panel``), which
    self-pumps inside ``run()``, and paces ONE 3-word transaction at a time —
    the panel link is single-outstanding (``SRAM_PANEL.md`` §5), so a bulk queue
    would starve on the held ack.
    """
    import simkyt
    from engine.sram_panel import SramPanelDevice

    lin = next(iter(bres.chips[0].input_landings.values()))
    panel = project.panels[0]
    dev = SramPanelDevice(size_words=panel.size_words,
                          addr_regs=panel.address_regs,
                          auto_inc_read=bool(getattr(panel, "auto_inc_read",
                                                     False)))
    dev.mem.update({int(a): int(w) & 0xFFFF
                    for a, w in (panel.image or {}).items()})
    chip = simkyt.Chip.from_yaml(str(CHIP_YAML))
    chip.load_bitstream_physical(bres.words(0))
    chip.register_panel("x1_out", "x1_in", dev)

    out = []

    def pump(limit):
        idle = 0
        for _ in range(120000):
            chip.run(max_events=64)
            got = chip.read_port_words_timed("x16_out")
            if got:
                idle = 0
                out.extend(w & 0xFFFF for w, _d, _t in got)
            else:
                idle += 1
                if idle > limit:
                    return

    for b in src_bytes:
        chip.queue_words_physical("x16_in", [
            _wr(lin["hop"], lin["data_addrs"][0]), int(b) & 0xFF,
            _jp(lin["hop"], lin["entry"])])
        pump(idle_max)
    pump(4 * idle_max)
    return out, dev


_BUILD_CACHE: list = []


def _auto_build_cached():
    """The auto-placed + built design, built ONCE for the whole module.

    Placement and build are deterministic here (the panel template lays the
    block's own ``default_layout`` down — there is no CP-SAT search), so every
    end-to-end case can share one build. Each case still gets a FRESH chip and a
    fresh panel device, so no state leaks between payloads.
    """
    if not _BUILD_CACHE:
        _BUILD_CACHE.append(_auto_build())
    return _BUILD_CACHE[0]


@pytest.mark.parametrize("name,payload", [
    ("all_literal", bytes(range(64))),
    ("repetitive", b"the quick brown fox jumps over the lazy dog. " * 3),
    ("byte_run", b"Q" * 40 + b"tail!"),
    ("overlap", b"abc" * 12 + b"!"),
    # The three cases each of which found a REAL defect that no per-cell gate saw:
    #   * a match-length CONTINUATION (nibble 15) — the OFFSET cell dropped the
    #     byte, so every match longer than 18 stalled;
    #   * offset == 1 over a long run — the copy has to be byte-by-byte;
    #   * a LITERAL continuation (15 + 255 + k) alongside matches.
    ("matchlen_continuation", b"z" * 4 + b"y" * 60 + b"end!"),
    ("offset_one_run", b"Q" + b"Q" * 200 + b"tail!"),
    ("literal_continuation", bytes((i * 7) & 0xFF for i in range(280))),
    ("mixed", b"a" * 80 + b"bcdefgh" * 10 + b"a" * 20),
])
def test_auto_placed_design_decodes_on_chip(name, payload):
    """THE END-TO-END GATE.

    The AUTO-PLACED, BUILT design decompresses a real LZ4 block byte-for-byte on a
    real chip through a real SRAM panel. Every layer below it is a component
    check; this is those checks composed — the parse FSM, the history window, the
    match copy at a computed panel address, and the placement, at once, from the
    same build path the GUI's auto-P&R produces.

    This gate was SKIPPED while the block could not be laid out (the emit cell
    needed a third face it could not afford). It is live now, and it is the test
    that matters: three defects survived every per-cell gate and every model check
    and were caught only here — the OFFSET cell dropping a match-length
    continuation byte, cell 4 kicking the RETURN entry instead of ``fetch``, and
    the egress WRITE aimed at the first corridor cell instead of through it.
    """
    _need_chip()
    blk = lz4_compress_block(payload)
    golden = lz4_decompress_block(blk)
    assert golden == payload, "the stimulus itself must round-trip"
    project, bres = _auto_build_cached()
    got, dev = _decode_on_chip(project, bres, blk)
    assert len(got) >= len(golden), (
        f"{name}: chip emitted {len(got)} of {len(golden)} bytes "
        f"(panel writes committed: {dev.writes_committed})")
    assert bytes(got[:len(golden)]) == golden, (
        f"{name}: decoded bytes differ from the golden\n"
        f"  golden[:24]={list(golden[:24])}\n"
        f"  chip  [:24]={got[:24]}")


def _mutated_chip_decode(payload, *, patch):
    """Build the design with ONE named defect patched into the BLOCK ITSELF, run
    it on a real chip, and return ``(decoded bytes, golden)``.

    The mutation is applied to the real ``LZ4DecoderBlock`` class and the design
    is re-placed, re-routed and re-built from it (INV-4 rule 5: mutate the BLOCK,
    not a model of it). ``patch`` is a callable given the class; it returns a
    restore callable.
    """
    from gr_kyttar.placement.blocks import lz4_decoder_block as _mod
    restore = patch(_mod.LZ4DecoderBlock)
    saved = list(_BUILD_CACHE)
    _BUILD_CACHE.clear()
    try:
        blk = lz4_compress_block(payload)
        project, bres = _auto_build()
        got, _dev = _decode_on_chip(project, bres, blk)
        return got, lz4_decompress_block(blk)
    finally:
        restore()
        _BUILD_CACHE[:] = saved


def test_mutation_onchip_first_fetch_kicks_the_RETURN_entry_FAILS():
    """INV-4, WHOLE CHIP: cell 4 kicking the emit cell's ``emit_mat`` (the panel
    RETURN door) instead of its ``fetch`` must break the decode.

    This mutation IS the bug the end-to-end gate found, restored: ``emit_mat``
    emits whatever ``b`` still holds — the previous literal — so the run starts
    one byte early against a window the spurious byte has already corrupted.
    Every per-cell gate and the whole FSM model pass with it in place, which is
    why this gate has to run the placed chip.
    """
    _need_chip()

    def patch(cls):
        orig = cls.internal_jumps
        cls.internal_jumps = lambda self: [
            (s, o, d, "emit_mat" if (s, o, d) == (4, "fetch", 5) else e)
            for (s, o, d, e) in orig(self)]

        def restore():
            cls.internal_jumps = orig
        return restore

    got, golden = _mutated_chip_decode(b"abc" * 12 + b"!", patch=patch)
    assert bytes(got[:len(golden)]) != golden, \
        "the end-to-end gate is BLIND to the first fetch kicking the return entry"


def test_mutation_onchip_dropping_the_matchlen_relay_FAILS():
    """INV-4, WHOLE CHIP: removing the OFFSET cell's relay of a match-length
    CONTINUATION byte must break the decode.

    The router steers every ``ST_MATCH`` byte to the OFFSET cell, so once the two
    offset bytes are in, a continuation byte lands there with ``nb == 0``. Without
    the relay to cell 4 it is swallowed and the match never fires — measured: a
    45-byte payload decoded to ONE byte. Any match longer than 18 takes this path,
    which is most real text.
    """
    _need_chip()

    def patch(cls):
        oc, oj = cls.internal_connections, cls.internal_jumps
        cls.internal_connections = lambda self: [
            e for e in oc(self) if e[:2] != (3, "ext")]
        cls.internal_jumps = lambda self: [
            e for e in oj(self) if e[:2] != (3, "ext")]

        def restore():
            cls.internal_connections, cls.internal_jumps = oc, oj
        return restore

    got, golden = _mutated_chip_decode(b"Q" * 40 + b"tail!", patch=patch)
    assert bytes(got[:len(golden)]) != golden, \
        "the end-to-end gate is BLIND to a dropped match-length continuation"


def test_mutation_onchip_egress_aimed_at_the_first_corridor_cell_FAILS():
    """INV-4, WHOLE CHIP: aiming the OUT cell's egress at the first corridor cell
    (``@1``) instead of through the whole corridor must produce NO output.

    A transiting word (HOP_CNT < 31) is forwarded by hardware on the cell's face
    before any program runs; only a LANDING word executes. So ``@1`` parks the
    byte on the egress cell and nothing reaches the port — while everything
    INSIDE the block still looks perfect (the panel window fills correctly). This
    gate pins the distinction.
    """
    _need_chip()
    project, bres = _auto_build()
    blk = project.block("lz4")
    good = int(blk.params["emit_hop"])
    assert good > 1, "the egress hop must span the corridor, not one cell"

    # Rebuild the block with the SHORT hop and run it: the panel window must
    # still fill (the block works) while the port stays silent.
    from engine.build import BuildEngine
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(str(CHIP_YAML))
    blk.params["emit_hop"] = 1
    bad = BuildEngine(cat, str(CHIP_YAML)).build(project, {"kyttar_10x12": ct})
    assert bad.ok, [str(e) for e in bad.errors]
    payload = bytes(range(32))
    got, dev = _decode_on_chip(project, bad, lz4_compress_block(payload))
    blk.params["emit_hop"] = good
    assert dev.writes_committed >= len(payload), (
        "the mutation must leave the BLOCK working — if the window did not fill, "
        "this gate is measuring something else")
    assert not got, (
        f"the port emitted {len(got)} words with the egress aimed one cell away; "
        "a transiting word is forwarded by hardware, so @1 must park it")


@pytest.mark.skipif(not _HAVE_REF, reason="reference lz4 C binding not importable")
def test_auto_placed_design_decodes_REFERENCE_C_blocks_on_chip():
    """THE STRONGEST GATE: the placed chip decodes blocks produced by the
    REFERENCE C COMPRESSOR, byte-for-byte.

    Everything else in this suite runs on blocks this repository manufactured. A
    block from ``lz4.block.compress`` is one nothing here shaped — it picks its
    own token nibbles, its own continuation lengths and its own offsets — so this
    is the check that the whole chain (golden, model, cell programs, placement,
    corridors) is not self-consistently wrong.

    Kept small on purpose: the panel link is single-outstanding, so a large
    payload is minutes of simulation for no extra coverage.
    """
    _need_chip()
    payloads = [PAYLOADS["short"],
                b"the quick brown fox jumps over the lazy dog. " * 3,
                b"Q" * 40 + b"tail!"]
    blocks = _ref_compress(payloads)
    project, bres = _auto_build_cached()
    for want, blk in zip(payloads, blocks):
        got, dev = _decode_on_chip(project, bres, blk)
        assert bytes(got[:len(want)]) == want and len(got) >= len(want), (
            f"a REFERENCE-C-compressed block did not decode on chip:\n"
            f"  want[:24]={list(want[:24])}\n  got [:24]={got[:24]}")


# =========================================================================
# LAYER 6 — INV-23 ORIENTATION: the fold is a RIGID unit
# =========================================================================
_D4 = [["cw"], ["cw", "cw"], ["cw", "cw", "cw"], ["mirror_h"], ["mirror_v"],
       ["mirror_h", "cw"], ["mirror_v", "cw"]]


@pytest.mark.parametrize("orient", _D4, ids=["+".join(o) for o in _D4])
def test_orientation_transforms_the_fold_and_its_face_words_together(orient):
    """INV-23: rotating/mirroring the placed block moves every cell AND turns
    every cell's resting face AND rewrites the in-program ``is_face`` constants
    through the SAME D4 map — so the fold stays a rigid unit and every internal
    edge still delivers.

    This block cannot use the shared orientation gate
    (``test_orientation_invariance.py``): that harness places a block anywhere and
    drives it from the chip port, whereas a panel-backed block is pinned by the
    panel template with its controller ON ``x1_out``. So the invariance is checked
    where it can be — on the transform itself — and it is checked on the property
    that actually matters here: **the walk**. A face constant that did not rotate
    with the cells would aim the emit cell's panel burst somewhere else, and the
    layout's one flip is exactly the thing at risk.
    """
    from model.enums import Face, face_code_after
    from model.placement import Placement, PlacedCell

    b = LZ4DecoderBlock("lz4")
    lay = b.default_layout()
    pl = Placement(0, [PlacedCell(cid, x, y, Face.from_str(f))
                       for cid, (x, y, f) in sorted(lay.items())])
    for k in orient:
        pl.transform(k)
    pos = {c.cell_id: (c.x, c.y) for c in pl.cells}
    face = {c.cell_id: c.face.value for c in pl.cells}

    # 1. RIGID: the footprint keeps its shape (same multiset of cell-to-cell
    #    offsets up to the transform) and nothing collides.
    assert len({(c.x, c.y) for c in pl.cells}) == b.cell_count, "cells collide"

    # 2. Every internal edge still delivers, on the transformed faces.
    W_ = max(x for x, _y in pos.values()) + 1
    H_ = max(y for _x, y in pos.values()) + 1
    minx = min(x for x, _y in pos.values())
    miny = min(y for _x, y in pos.values())
    npos = {c: (x - minx, y - miny) for c, (x, y) in pos.items()}
    W_ -= minx
    H_ -= miny
    ctl = b.panel_requirements()["controller_cell"]
    edges = ({(s, d) for s, _o, d, _i in b.internal_connections()}
             | {(s, d) for s, _o, d, _e in b.internal_jumps()})
    bad = []
    for (s, d) in sorted(edges):
        ok = False
        for f in ("east", "west", "north", "south"):
            seen = _reachable_from(npos, {**face, s: f}, s, W_, H_)
            if d in seen and (d == ctl or ctl not in seen[:seen.index(d)]):
                ok = True
                break
        if not ok:
            bad.append((s, d))
    assert not bad, f"orientation {orient} breaks internal edges {bad}"

    # 3. The in-program FACE constants transform by the SAME map. The emit cell
    #    rests on the ring face and flips to the panel face; after the transform
    #    both must still name the direction of the cell they serve.
    progs = b.build_cell_programs()
    emit_id = b.panel_requirements()["panel_client_cell"]
    out_id = b.panel_requirements()["output_cell"]
    got = {d.name: face_code_after(d.value, orient)
           for d in progs[emit_id].data if d.is_face}
    _CODE = {"south": 0, "east": 1, "west": 2, "north": 3}
    assert got["face_ring"] == _CODE[face[emit_id]], (
        f"orientation {orient}: the emit cell's resting `face_ring` constant "
        f"({got['face_ring']}) no longer matches its placed face "
        f"({face[emit_id]}) — the ring hand-off would leave the wrong way")
    # face_panel must point AT the OUT cell (which the panel burst reaches first).
    ex, ey = npos[emit_id]
    ox, oy = npos[out_id]
    want = {(1, 0): 1, (-1, 0): 2, (0, -1): 3, (0, 1): 0}[(ox - ex, oy - ey)]
    assert got["face_panel"] == want, (
        f"orientation {orient}: the emit cell's `face_panel` constant "
        f"({got['face_panel']}) does not point at the OUT cell (want {want}) — "
        "the panel burst and the block's own output would both go astray")


def test_orientation_is_a_noop_for_the_identity():
    """The D4 identity leaves the layout byte-identical — the control that makes
    the seven transform cases above mean something."""
    from model.enums import Face
    from model.placement import Placement, PlacedCell

    b = LZ4DecoderBlock("lz4")
    lay = b.default_layout()
    pl = Placement(0, [PlacedCell(cid, x, y, Face.from_str(f))
                       for cid, (x, y, f) in sorted(lay.items())])
    before = [(c.cell_id, c.x, c.y, c.face.value) for c in pl.cells]
    for k in ("cw", "cw", "cw", "cw"):
        pl.transform(k)
    assert [(c.cell_id, c.x, c.y, c.face.value) for c in pl.cells] == before


def test_emit_report():
    """Emit the verification report — from the AUTO-PLACED CHIP, not the model.

    The number that earns this block its `done` is the byte error count of the
    placed, routed, built design against the golden, so that is what is measured
    and reported here. ``write_report`` refuses to write unless the session
    itself passed (INV-36/INV-38), so the file cannot outlive a red run.
    """
    _need_chip()
    from kyttar_verify.compare import CompareResult, Metric, write_report

    payloads = [bytes(range(64)),
                b"the quick brown fox jumps over the lazy dog. " * 3,
                b"Q" * 40 + b"tail!",
                b"abc" * 12 + b"!",
                b"z" * 4 + b"y" * 60 + b"end!",
                bytes((i * 7) & 0xFF for i in range(280))]
    project, bres = _auto_build_cached()
    n = errs = 0
    for want in payloads:
        golden = lz4_decompress_block(lz4_compress_block(want))
        got, _dev = _decode_on_chip(project, bres, lz4_compress_block(want))
        n += len(golden)
        errs += sum(1 for i, g in enumerate(golden)
                    if i >= len(got) or (got[i] & 0xFF) != g)
    res = CompareResult(passed=(errs == 0), metric=Metric.EXACT,
                        n_compared=n, bit_errors=errs, delay_used=0)
    write_report("LZ4DecoderBlock", res, coverage={
        "gr_equiv": "no stock GR block; golden is a transcription of the "
                    "published LZ4 Block Format Description, cross-checked "
                    "BOTH WAYS against the reference C implementation "
                    "(lz4.block) — it decodes reference-compressed blocks and "
                    "the reference decodes ours",
        "patterns": f"{len(payloads)} payload classes, {n} bytes, decoded on "
                    "the AUTO-PLACED + ROUTED + BUILT design on a real simkyt "
                    "chip through a real SramPanelDevice: all-literal, "
                    "repetitive text, an offset==1 byte run, an overlapping "
                    "match, a match-length continuation and a literal "
                    "continuation; plus reference-C-compressed blocks on the "
                    "same placed design",
        "mutation": True,
        "note": "SRAM-backed (INV-31), and the first block to address a "
                "COMPUTED panel address (wpos - off) into a window it is "
                "itself writing. 8 cells: a 3x2 ring over the parse FSM with "
                "the egress cell and the controller on a tail — the split that "
                "let the emit cell fit its one face flip (INV-46/INV-48). "
                "Whole-chip mutations: the first fetch kicking the return "
                "entry, a dropped match-length continuation, and an egress hop "
                "aimed at the first corridor cell.",
    })
    assert errs == 0, f"{errs} byte errors over {n} decoded bytes"


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
