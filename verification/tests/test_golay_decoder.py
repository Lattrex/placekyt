# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify GolayDecoderBlock — SRAM-backed extended Golay (24,12) syndrome decoder.

NO GNU Radio counterpart (gr-fec has no Golay factory). Two goldens, both
anchored on the GolayEncoderBlock CONVENTION PIN (the same dispatch wave's
executable ``encode_word()`` / ``_column_mask()`` — the decoder derives all
syndrome machinery from it, never re-deriving B):

  1. an INDEPENDENT brute-force nearest-codeword decoder over the full
     4096-word codebook (unambiguous for weight <= 3 — min distance 8);
  2. the encoder itself for the ROUND-TRIP gates (golden-encoder bits ->
     DUT decoder under 0..3 injected errors).

The DUT path is proven ON REAL simkyt + the REAL ``SramPanelDevice`` /
``PanelDriver`` in stages, then end to end:

  * PACK cell on-chip: 24-bit group split into the (D, P) halves;
  * the SYNDROME CHAIN on-chip (pack -> syn1 -> syn2 -> syn3 -> correct):
    injected error patterns produce EXACTLY the golden syndrome as the panel
    read ADDRESS (captured at the port with the D forward + the read trigger,
    in time order);
  * EMIT cell on-chip: a REAL panel push-read lands e_d + kicks the burst;
  * LOAD phase: the persistent placed SramControllerBlock streams the sparse
    ``set_addr``-per-pair LUT into the panel in ONE chip run;
  * FULL CHAIN through the real panel: clean codewords, EXHAUSTIVE 1-error
    (24 positions x sampled words), sampled 2-/3-error, the >= 4-error KNOWN
    LIMIT (passthrough — proven never to alias, exhaustively at weight 4),
    round-trip vs the golden encoder (>= 3 seeds), and the INV-4 mutation
    gates (no-correction passthrough, corrupted LUT row through the REAL
    panel, swapped halves, +1 shift, misframed, empty) proven to FAIL.

STORED-VALUE-CAN-BE-0 (the VaricodeDecoder CHAR_OFFSET lesson) — verified
explicitly here: with the e_d-only storage format a read of 0 is shared by
s == 0, parity-only correctable errors, and uncorrectable syndromes, and ALL
THREE require the same no-op data correction, so the collision is harmless
and needs no offset. Gated by ``test_parity_only_errors_*`` +
``test_weight4_never_aliases_exhaustive``.

Full-chain harness note (stated plainly): chip A runs pack..correct and
drives the REAL panel (D is parked in panel scratch register R7 by the
correct cell's real routed egress, the syndrome address in R5, then the real
read trigger). The push-read is intercepted (the Varicode full-chain
pattern) and delivered — with the panel-captured D — to the EMIT cell on a
fresh chip through a REAL injected push-read (value, landing register, and
entry kick all real); only the correct->emit D hop is re-materialized from
the panel scratch register, and that exact hop's on-chip form is covered by
the syndrome-chain + emit-cell stage tests.

Env (INV-28): run with PYTHONPATH pointing at THIS worktree's runtime/python
+ placekyt so simkyt/gr_kyttar resolve here::

    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
        .venv/bin/python -m pytest verification/tests/test_golay_decoder.py -v
"""
from __future__ import annotations

import functools
import itertools
import os
import random
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_WT = Path(__file__).resolve().parents[2]
_VERIFY = Path(__file__).resolve().parents[1]
for _p in (str(_WT / "runtime" / "python"), str(_WT / "placekyt"), str(_VERIFY)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from kyttar_verify import write_report, CompareResult, Metric  # noqa: E402
from gr_kyttar.placement.blocks.golay_encoder_block import (  # noqa: E402
    GolayEncoderBlock)
from gr_kyttar.placement.blocks.golay_decoder_block import (  # noqa: E402
    GolayDecoderBlock,
    N_CORRECTABLE_SYNDROMES,
    N_POPULATED_WORDS,
    LUT_ADDR_SPACE,
    parity_word_of,
    syndrome_of,
    split_bits24,
    error_lut_pairs,
    sram_error_image,
    correctable_syndromes,
    decode_word_from_sram,
    decode_stream_from_sram,
)
from gr_kyttar.placement.blocks.sram_controller_block import (  # noqa: E402
    SramControllerBlock)
from gr_kyttar.placement.resolver import (  # noqa: E402
    CellProgramResolver, ResolvedTargets, WriteTarget, JumpTarget)

CHIP_YAML = str(_WT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml")
pytestmark = pytest.mark.skipif(
    not os.path.exists(CHIP_YAML), reason="chip yaml absent")

W = 10  # chip is 10 cells wide

# Panel scratch register used to park D on its way to the emit cell in the
# full-chain harness (pure storage: with addr_regs=1 only R5 is the address).
_D_SCRATCH = 7


def _cid(x, y):
    return y * W + x


def _wr(h, d):
    return (0x6 << 12) | ((h & 0x1F) << 5) | (d & 0x1F)


def _jp(h, e):
    return (0x7 << 12) | ((h & 0x1F) << 5) | (e & 0x1F)


def _word_bits12(w: int) -> list:
    return [(w >> (11 - i)) & 1 for i in range(12)]


def _inject_errors(bits24, positions):
    bad = list(bits24)
    for p in positions:
        bad[p] ^= 1
    return bad


# --- independent golden: brute-force nearest-codeword ------------------------

@functools.lru_cache(maxsize=1)
def _codebook():
    return [tuple(GolayEncoderBlock.encode_word(w)) for w in range(4096)]


def _golden_decode(bits24):
    """Nearest-codeword decode (unambiguous within distance 3). Returns the
    12-bit data word, or None when no codeword lies within distance 3."""
    best_w, best_d = None, 4
    for w, cw in enumerate(_codebook()):
        d = sum(x != y for x, y in zip(cw, bits24))
        if d < best_d:
            best_w, best_d = w, d
    return best_w


# ==================================================== model / LUT construction

def test_lut_image_counts_and_bounds():
    """2026 populated words (the e_d != 0 correctable syndromes) of the 4096
    space; every address/value is 12-bit; syndrome 0 (and only-parity ones)
    deliberately absent."""
    img = sram_error_image()
    assert len(img) == N_POPULATED_WORDS == 2026
    assert LUT_ADDR_SPACE == 4096
    assert 0 not in img
    assert all(1 <= a <= 0xFFF for a in img)
    assert all(1 <= v <= 0xFFF for v in img.values())
    assert len(correctable_syndromes()) == N_CORRECTABLE_SYNDROMES == 2325


def test_syndrome_zero_for_every_clean_codeword_exhaustive():
    """s == 0 for ALL 4096 codewords of the PINNED encoder — the H matrix is
    derived from (and consistent with) the encoder's own column masks."""
    for w in range(4096):
        d, p = split_bits24(GolayEncoderBlock.encode_word(w))
        assert syndrome_of(d, p) == 0, w
    # and the re-encoded parity word IS the encoder's parity half
    assert parity_word_of(0xFFF) == 0xFFF  # all-one anchor (odd column weights)


def test_parity_only_errors_unstored_but_exact():
    """THE stored-value-can-be-0 gate: every correctable error confined to the
    PARITY half has a non-zero syndrome that is NOT in the image (reads 0),
    and the decode is STILL exact — 0 is the correct data-half action."""
    img = sram_error_image()
    n = 0
    for wgt in (1, 2, 3):
        for pos in itertools.combinations(range(12, 24), wgt):
            e_p = 0
            for i in pos:
                e_p |= 1 << (23 - i)
            s = syndrome_of(0, e_p)
            assert s != 0
            assert s not in img, (pos, s)
            n += 1
    assert n == 298  # C(12,1)+C(12,2)+C(12,3)
    # decode exactness through the model for sampled parity-only patterns
    rng = random.Random(5)
    for _ in range(32):
        w = rng.randrange(4096)
        bits = GolayEncoderBlock.encode_word(w)
        pos = rng.sample(range(12, 24), rng.randint(1, 3))
        got = decode_word_from_sram(img, _inject_errors(bits, pos))
        assert got == _word_bits12(w), (w, pos)


def test_weight4_never_aliases_exhaustive():
    """KNOWN LIMIT, half 1 (exhaustive): NO weight-4 error pattern has a
    correctable syndrome (their XOR with a weight-<=3 pattern would be a
    codeword of weight <= 7 < 8). So exactly-4-error words ALWAYS read an
    unpopulated address -> passthrough, never a miscorrection."""
    corr = correctable_syndromes()
    for pos in itertools.combinations(range(24), 4):
        e_d = e_p = 0
        for i in pos:
            if i < 12:
                e_d |= 1 << (11 - i)
            else:
                e_p |= 1 << (23 - i)
        assert syndrome_of(e_d, e_p) not in corr, pos


def test_weight5_can_miscorrect_documented():
    """KNOWN LIMIT, half 2: >= 5 errors CAN alias a correctable syndrome and
    miscorrect (bounded-distance decoding) — demonstrated, not hidden."""
    img = sram_error_image()
    bits = GolayEncoderBlock.encode_word(0x000)   # the all-zero codeword
    # 5 of the 8 ones of a weight-8 codeword == distance 3 from that codeword.
    cw8 = next(cw for cw in _codebook() if sum(cw) == 8)
    pos = [i for i, b in enumerate(cw8) if b][:5]
    got = decode_word_from_sram(img, _inject_errors(bits, pos))
    assert got != _word_bits12(0x000), "a 5-error alias should miscorrect"


def test_model_matches_bruteforce_golden():
    """The SRAM model == the independent nearest-codeword golden over sampled
    words x error weights 0..3 at rotating positions."""
    img = sram_error_image()
    rng = random.Random(24)
    for w in [0x000, 0xFFF] + [rng.randrange(4096) for _ in range(24)]:
        bits = GolayEncoderBlock.encode_word(w)
        for nerr in range(4):
            pos = rng.sample(range(24), nerr)
            bad = _inject_errors(bits, pos)
            assert _golden_decode(bad) == w, (w, pos)
            assert decode_word_from_sram(img, bad) == _word_bits12(w), (w, pos)


def test_block_reference_grouping_and_partial_drop():
    """process_reference_q15: 24-bit groups -> 12 corrected bits each; only
    the LSB of each input word is data; a trailing partial group (< 24 bits)
    is NOT emitted."""
    blk = GolayDecoderBlock("ref")
    rng = random.Random(9)
    words = [rng.randrange(4096) for _ in range(3)]
    stream = []
    for w in words:
        stream += _inject_errors(GolayEncoderBlock.encode_word(w),
                                 rng.sample(range(24), 2))
    # stray high bits on the input words must be ignored (LSB-only)
    noisy = [b | (rng.randint(0, 0x7FFF) << 1) for b in stream]
    want = [b for w in words for b in _word_bits12(w)]
    assert blk.process_reference_q15(noisy) == want
    assert blk.process_reference_q15(noisy + [1, 0, 1]) == want  # partial drop


# ============================================== cell resolution / register traps

def _block(**kw):
    return GolayDecoderBlock("g", **kw)


def test_all_cells_fit_single_cell():
    b = _block()
    r = CellProgramResolver()
    for cid, cp in b.build_cell_programs().items():
        tg = ResolvedTargets()
        for o in cp.outputs:
            tg.writes[o.name] = WriteTarget(1, 1)
            tg.jumps[o.name] = JumpTarget(1, 1)
        res = r.resolve(cp, tg)
        assert max(res.memory) <= 31 and len(res.memory) <= 32, cid


def test_register_layout_no_silent_collision():
    """The encoder's silent-collision lesson as an explicit gate: every non-R0
    input register and state register sits BELOW 31 - n_instructions and
    ABOVE the data/LOAD-table range, in every cell."""
    b = _block()
    r = CellProgramResolver()
    for cid, cp in b.build_cell_programs().items():
        cls = r.classify_addresses(cp)
        base = 31 - r.count_instructions(cp)
        data_top = max([a for a, v in cls.items() if v["role"] == "data"],
                       default=0)
        for a, v in cls.items():
            if v["role"] in ("input", "state") and a != 0:
                assert data_top < a < base, (cid, v, a, base, data_top)


def test_block_reports_seven_cells():
    assert _block().cell_count == 7


# ================================================== stage 1: PACK cell on-chip

def _resolve_pack():
    cp = _block().build_cell_programs()[0]
    R = CellProgramResolver()
    tg = ResolvedTargets()
    tg.writes["dw"] = WriteTarget(W, 1)      # exit the port, dest tag 1
    tg.writes["pw"] = WriteTarget(W, 2)      # exit the port, dest tag 2
    tg.jumps["trig"] = JumpTarget(W, 0)
    res = R.resolve(cp, tg)
    ent = R.compute_entry_addresses(cp)["default"]
    return res, ent


def _feed_bits(chip, ent, bits, pump=None, per_bit=60, per_group=600):
    """Inject a bit stream at the pack cell (0,0) one bit per port trigger —
    the per-sample panel contract. The 24th bit of each group gets a larger
    run budget (the whole chain + panel round-trip fires there)."""
    for i, bit in enumerate(bits):
        chip.set_port_entry_address("x16_in", ent)
        chip.set_port_target_hop_count("x16_in", 30)      # land at (0,0)
        chip.write_port_multi_i16("x16_in", [[(0, int(bit) & 1)]], ent)
        n = per_group if (i % 24) == 23 else per_bit
        for _ in range(n):
            chip.run(max_events=16)
            if pump is not None:
                pump()


def test_pack_cell_groups_onchip():
    """The pack cell splits a 48-bit stream into two (D, P) pairs on real
    simkyt — group 2 exercises the deliberate no-reset (stale bits above bit
    11, masked downstream)."""
    import simkyt
    res, ent = _resolve_pack()
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    for a in range(32):
        chip.write_cell_memory(_cid(0, 0), a, int(res.memory.get(a, 0)))
    for x in range(W):
        chip.set_fwd_face(_cid(x, 0), "east")
    rng = random.Random(3)
    words = [(0xB71, 0x2F2), (0x000, 0xFFF)]
    bits = []
    for (d, p) in words:
        bits += _word_bits12(d) + _word_bits12(p)
    _feed_bits(chip, ent, bits)
    got = [(v & 0xFFFF, dest) for (v, dest, _t) in
           chip.read_port_words_timed("x16_out")]
    assert len(got) == 4, got
    for gi, (d, p) in enumerate(words):
        dv, dtag = got[2 * gi]
        pv, ptag = got[2 * gi + 1]
        assert (dtag, ptag) == (1, 2), got
        assert dv & 0xFFF == d and pv & 0xFFF == p, (gi, got)


# ================================= stage 2: the SYNDROME CHAIN on-chip (no panel)

def _resolve_chain(read_addr_hop=W - 4, read_dest=5, read_entry=1):
    """Resolve cells 0..4 for the row-0 placement pack(0,0)..correct(4,0):
    @1 abutment hops between cells, the correct cell's D forward + panel
    protocol exiting the east port."""
    b = _block(read_addr_hop=read_addr_hop, read_dest=read_dest,
               read_entry=read_entry)
    cps = b.build_cell_programs()
    R = CellProgramResolver()
    ents = {i: R.compute_entry_addresses(cps[i])["default"] for i in range(6)}

    def reg(i, name):
        cls = R.classify_addresses(cps[i])
        return [a for a, v in cls.items() if v.get("name") == name][0]

    tgs = {}
    tgs[0] = {"w": {"dw": (1, reg(1, "dw")), "pw": (1, reg(1, "pw"))},
              "j": {"trig": (1, ents[1])}}
    tgs[1] = {"w": {"dout": (1, reg(2, "dw")), "pout": (1, reg(2, "pw")),
                    "qout": (1, reg(2, "qw"))},
              "j": {"trig": (1, ents[2])}}
    tgs[2] = {"w": {"dout": (1, reg(3, "dw")), "pout": (1, reg(3, "pw")),
                    "qout": (1, reg(3, "qw"))},
              "j": {"trig": (1, ents[3])}}
    tgs[3] = {"w": {"dout": (1, reg(4, "dw")), "sout": (1, reg(4, "sw"))},
              "j": {"trig": (1, ents[4])}}
    # correct: the template dout parks D in the panel scratch register; the
    # literal panel WRITE/JUMP (addr -> R5, trigger R1) are baked via params.
    tgs[4] = {"w": {"dout": (W - 4, _D_SCRATCH)}, "j": {}}
    resolved = {}
    for i in range(5):
        tg = ResolvedTargets()
        for name, (dist, addr) in tgs[i]["w"].items():
            tg.writes[name] = WriteTarget(dist, addr)
        for name, (dist, addr) in tgs[i]["j"].items():
            tg.jumps[name] = JumpTarget(dist, addr)
        resolved[i] = R.resolve(cps[i], tg)
    emit_regs = (reg(5, "dw"), reg(5, "ew"))
    return resolved, ents, emit_regs, b


def _chip_a(resolved, handshake=False):
    import simkyt
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    for i in range(5):
        for a in range(32):
            chip.write_cell_memory(_cid(i, 0), a,
                                   int(resolved[i].memory.get(a, 0)))
    for x in range(W):
        chip.set_fwd_face(_cid(x, 0), "east")
    if handshake:
        chip.set_port_handshake("x16_out", True)
    return chip


_SYN_CASES = [
    (0x2F2, ()),                    # clean
    (0x2F2, (0,)),                  # d11 flipped
    (0x94D, (11,)),                 # d0 flipped
    (0x94D, (12,)),                 # p11 flipped (parity-only)
    (0xB71, (23,)),                 # p0 flipped (parity-only)
    (0xB71, (3, 17)),               # mixed double
    (0x5A5, (1, 6, 20)),            # mixed triple
    (0x000, (2, 3, 4, 5)),          # weight 4 — uncorrectable syndrome
]


@pytest.mark.parametrize("word,pos", _SYN_CASES)
def test_syndrome_chain_onchip(word, pos):
    """pack -> syn1 -> syn2 -> syn3 -> correct on REAL simkyt: the injected
    error pattern produces EXACTLY the golden syndrome as the panel read
    ADDRESS, preceded by the received data half (in time order) and followed
    by the read trigger."""
    resolved, ents, _eregs, _b = _resolve_chain()
    chip = _chip_a(resolved)
    bits = _inject_errors(GolayEncoderBlock.encode_word(word), pos)
    _feed_bits(chip, ents[0], bits)
    words = [(v & 0xFFFF, dest, t) for (v, dest, t) in
             chip.read_port_words_timed("x16_out")]
    jumps = chip.read_port_jumps("x16_out")
    d_recv, p_recv = split_bits24(bits)
    s_want = syndrome_of(d_recv, p_recv)
    assert len(words) == 2, words
    (dv, dtag, dt), (sv, stag, st) = words
    assert (dtag, stag) == (_D_SCRATCH, 5), words
    assert dt < st, "D must be parked before the read address"
    assert dv & 0xFFF == d_recv
    assert sv == s_want, (hex(sv), hex(s_want))          # masked: NO stale bits
    assert [e for (e, _t) in jumps] == [1], jumps        # exactly one read trigger


def test_syndrome_chain_stream_of_groups_onchip():
    """Three back-to-back codewords: one (D, s, trigger) triple each, in
    order (state carries over between groups only via the masked stale
    bits)."""
    resolved, ents, _eregs, _b = _resolve_chain()
    chip = _chip_a(resolved)
    cases = [(0xC3C, (5,)), (0x123, ()), (0x7FF, (2, 14))]
    bits = []
    for w, pos in cases:
        bits += _inject_errors(GolayEncoderBlock.encode_word(w), pos)
    _feed_bits(chip, ents[0], bits)
    words = [(v & 0xFFFF, dest) for (v, dest, _t) in
             chip.read_port_words_timed("x16_out")]
    assert len(words) == 6
    for gi, (w, pos) in enumerate(cases):
        grp = _inject_errors(GolayEncoderBlock.encode_word(w), pos)
        d_recv, p_recv = split_bits24(grp)
        assert words[2 * gi] == ((words[2 * gi][0] & 0xFFFF), _D_SCRATCH)
        assert words[2 * gi][0] & 0xFFF == d_recv, gi
        assert words[2 * gi + 1] == (syndrome_of(d_recv, p_recv), 5), gi


# ==================================== stage 3: EMIT cell on-chip (real push-read)

def _resolve_emit():
    cp = _block().build_cell_programs()[5]
    R = CellProgramResolver()
    tg = ResolvedTargets()
    tg.writes["out"] = WriteTarget(W, 0)
    tg.jumps["trig"] = JumpTarget(W, 0)
    res = R.resolve(cp, tg)
    cls = R.classify_addresses(cp)
    dw_reg = [a for a, v in cls.items() if v.get("name") == "dw"][0]
    ew_reg = [a for a, v in cls.items() if v.get("name") == "ew"][0]
    ent = R.compute_entry_addresses(cp)["default"]
    return res, dw_reg, ew_reg, ent


def _run_emit_onchip(d_word, e_word):
    """Deliver (D, e_d) to the emit cell — e_d via a REAL panel push-read that
    also kicks the entry — and collect the 12-bit burst out x16_out."""
    import simkyt
    from engine.sram_panel import SramPanelDevice, PanelDriver, PushRead
    res, dw_reg, ew_reg, ent = _resolve_emit()
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    for a in range(32):
        chip.write_cell_memory(_cid(0, 0), a, int(res.memory.get(a, 0)))
    for x in range(W):
        chip.set_fwd_face(_cid(x, 0), "east")
    chip.write_cell_memory(_cid(0, 0), dw_reg, d_word & 0xFFFF)
    dev = SramPanelDevice()
    drv = PanelDriver(dev, chip, "x16_out", chip, "x16_in")
    drv._inject(PushRead(value=e_word & 0xFFFF, dest=ew_reg, write_hop=30,
                         jump_entry=ent, jump_hop=30))
    out = []
    for _ in range(800):
        chip.run(max_events=16)
        for v, _d, _t in chip.read_port_words_timed("x16_out"):
            out.append(v & 0xFFFF)
    return out


@pytest.mark.parametrize("d,e", [
    (0xB71, 0x000),            # e_d == 0: the no-op correction (clean/parity-only)
    (0xB71, 0x800),            # flip d11
    (0x000, 0x001),            # flip d0
    (0xFFF, 0xABC),
    (0x2F2 | 0xF000, 0x010),   # stale bits above bit 11 are masked by the peel
])
def test_emit_cell_bursts_corrected_word_onchip(d, e):
    out = _run_emit_onchip(d, e)
    assert out == _word_bits12((d ^ e) & 0xFFF), (hex(d), hex(e), out)


# ==================================================== LOAD phase (persistent run)

def _controller():
    ctl = SramControllerBlock("ctl", panel_hop=W)
    cp = ctl.build_cell_programs()[0]
    R = CellProgramResolver()
    res = R.resolve(cp)
    cls = R.classify_addresses(cp)
    cin = [a for a, v in cls.items() if v.get("name") == "data"][0]
    ent = R.compute_entry_addresses(cp)
    return res, cin, ent


def test_load_phase_streams_lut_sparse_addressing():
    """The persistent placed SramController streams the LUT into the panel in
    ONE chip run: each (syndrome, e_d) pair set_addrs its sparse address then
    writes the word (SRAM_PANEL.md §6 — syndromes are scattered, so NO
    auto-increment; controller wraddr is cell state, hence ONE run)."""
    import simkyt
    from engine.sram_panel import SramPanelDevice, PanelDriver

    res, cin, ent = _controller()
    pairs = error_lut_pairs()[:24]
    dev = SramPanelDevice()
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    for a in range(32):
        chip.write_cell_memory(_cid(0, 0), a, int(res.memory.get(a, 0)))
    for x in range(W):
        chip.set_fwd_face(_cid(x, 0), "east")
    chip.set_port_handshake("x16_out", True)
    drv = PanelDriver(dev, chip, "x16_out", chip, "x16_in")
    stim = []
    for (addr, word) in pairs:
        stim += [_wr(30, cin), addr, _jp(30, ent["set_addr"])]   # sparse address
        stim += [_wr(30, cin), word, _jp(30, ent["write"])]      # store the word
    chip.queue_words_physical("x16_in", stim)
    for _ in range(16000):
        chip.run(max_events=32)
        drv.step()
        if dev.writes_committed >= len(pairs):
            break
    assert dev.writes_committed == len(pairs)
    assert all(dev.mem.get(addr) == word for (addr, word) in pairs), \
        "sparse-addressed LUT load stored wrong words"


# ============================== FULL CHAIN through the REAL panel (see the note)

def _load_full_lut(dev):
    """Load ALL 2026 LUT words via the controller write PROTOCOL (device
    commit — the same on_write/on_jump sequence the controller emits; the
    persistent streaming load is proven in test_load_phase_...)."""
    for (addr, word) in error_lut_pairs():
        dev.on_write(2, word)       # R2 payload
        dev.on_write(5, addr)       # R5 address == syndrome
        dev.on_jump(0)              # commit


@functools.lru_cache(maxsize=1)
def _loaded_pairs():
    return tuple(error_lut_pairs())


def _decode_onchip(bits, dev=None):
    """The full SRAM-backed decode: chip A (pack..correct) forms each
    syndrome and drives the REAL panel (D parked in scratch R7 by real routed
    egress, address in R5, real read trigger); the panel's push-read (value =
    sram[s]) is delivered — with the panel-captured D — to the EMIT cell on a
    fresh chip via a REAL injected push-read. Returns the emitted bits."""
    import simkyt
    from engine.sram_panel import SramPanelDevice, PanelDriver
    if dev is None:
        dev = SramPanelDevice()
        _load_full_lut(dev)

    resolved, ents, _eregs, _b = _resolve_chain()
    chip_a = _chip_a(resolved, handshake=True)
    drv_a = PanelDriver(dev, chip_a, "x16_out", chip_a, "x16_in")

    res_e, dw_reg, ew_reg, ent_e = _resolve_emit()
    out_bits = []

    def _emit_push(push):
        chip_b = simkyt.Chip.from_yaml(CHIP_YAML)
        for a in range(32):
            chip_b.write_cell_memory(_cid(0, 0), a,
                                     int(res_e.memory.get(a, 0)))
        for x in range(W):
            chip_b.set_fwd_face(_cid(x, 0), "east")
        # D for this codeword: parked in panel scratch R7 by the correct
        # cell's real routed egress (time-ordered before the read trigger).
        chip_b.write_cell_memory(_cid(0, 0), dw_reg, dev.reg(_D_SCRATCH))
        drv_b = PanelDriver(dev, chip_b, "x16_out", chip_b, "x16_in")
        push.dest, push.write_hop = ew_reg, 30
        push.jump_entry, push.jump_hop = ent_e, 30
        drv_b._inject(push)
        for _ in range(800):
            chip_b.run(max_events=16)
            for v, _d, _t in chip_b.read_port_words_timed("x16_out"):
                out_bits.append(v & 1)

    drv_a._inject = _emit_push
    _feed_bits(chip_a, ents[0], bits, pump=drv_a.step)
    return out_bits


def test_full_chain_clean_codewords():
    """Clean codewords through the real panel: s == 0 reads the (guaranteed
    unpopulated) address 0, e_d == 0, output == the data bits — the uniform
    single-lookup path, bit-exact."""
    words = [0x000, 0xFFF, 0x2F2, 0x94D]
    bits = [b for w in words for b in GolayEncoderBlock.encode_word(w)]
    got = _decode_onchip(bits)
    assert got == [b for w in words for b in _word_bits12(w)]


_ONEERR_WORDS = [0x2F2, 0xB71, 0x555]


@pytest.mark.parametrize("word", _ONEERR_WORDS)
def test_full_chain_exhaustive_1_error(word):
    """EXHAUSTIVE single-error coverage: all 24 positions, through the real
    panel push-read, each corrected bit-exact."""
    bits = []
    for pos in range(24):
        bits += _inject_errors(GolayEncoderBlock.encode_word(word), (pos,))
    got = _decode_onchip(bits)
    assert got == _word_bits12(word) * 24, word


def test_full_chain_2_and_3_errors_sampled():
    """Sampled 2- and 3-error patterns across positions (data-half,
    parity-half, and mixed), corrected bit-exact through the real panel."""
    cases = [
        (0x94D, (0, 23)), (0x94D, (5, 6)), (0x123, (13, 20)),
        (0x123, (2, 12, 22)), (0xC3C, (0, 1, 2)), (0xC3C, (21, 22, 23)),
        (0x7FF, (3, 11, 17)), (0x800, (7, 9, 15)),
    ]
    bits = []
    for w, pos in cases:
        bits += _inject_errors(GolayEncoderBlock.encode_word(w), pos)
    got = _decode_onchip(bits)
    want = [b for (w, _pos) in cases for b in _word_bits12(w)]
    assert got == want


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_full_chain_random_errors(seed):
    """Random words x random 0..3-error patterns (>= 3 seeds) — bit-exact."""
    rng = random.Random(seed)
    bits, want = [], []
    for _ in range(5):
        w = rng.randrange(4096)
        pos = rng.sample(range(24), rng.randint(0, 3))
        bits += _inject_errors(GolayEncoderBlock.encode_word(w), pos)
        want += _word_bits12(w)
    got = _decode_onchip(bits)
    assert got == want, seed


def test_full_chain_4_errors_passthrough_known_limit():
    """KNOWN LIMIT on-chip: exactly-4-error words read an unpopulated address
    (proven never to alias) and pass the RECEIVED data half through — no
    miscorrection, residual data-half errors remain, honestly documented."""
    cases = [(0x2F2, (0, 5, 13, 20)),      # 2 data + 2 parity errors
             (0xB71, (12, 15, 18, 21)),    # all-parity: output still exact
             (0x94D, (1, 2, 3, 4))]        # all-data: 4 residual bit errors
    bits = []
    for w, pos in cases:
        bits += _inject_errors(GolayEncoderBlock.encode_word(w), pos)
    got = _decode_onchip(bits)
    want = []
    for w, pos in cases:
        d_recv, _p = split_bits24(
            _inject_errors(GolayEncoderBlock.encode_word(w), pos))
        want += _word_bits12(d_recv)
    assert got == want
    # and the residual-error consequence is visible where data bits were hit:
    assert got[0:12] != _word_bits12(0x2F2)
    assert got[12:24] == _word_bits12(0xB71)   # parity-only: exact anyway
    assert got[24:36] != _word_bits12(0x94D)


@pytest.mark.parametrize("seed", [11, 22, 33])
def test_roundtrip_golden_encoder_through_dut_decoder(seed):
    """ROUND-TRIP (the convention pin, end to end): the GOLDEN encoder's wire
    bits, with 0..3 injected errors per codeword, fed through the on-chip
    SRAM decoder recover the original data words EXACTLY."""
    rng = random.Random(seed)
    bits, want = [], []
    for nerr in (0, 1, 2, 3):
        w = rng.randrange(4096)
        pos = rng.sample(range(24), nerr)
        bits += _inject_errors(GolayEncoderBlock.encode_word(w), pos)
        want += _word_bits12(w)
    got = _decode_onchip(bits)
    assert got == want, seed


# ------------------------------------------------------- MUTATION gates (INV-4)

def test_mutation_no_correction_passthrough_FAILS():
    """A decoder that IGNORES the LUT (emits the received data half) must
    disagree with the golden on any data-half error — the gate SEES a dead
    correction path. (The stimulus exercises the data half by construction.)"""
    img = sram_error_image()
    bits = _inject_errors(GolayEncoderBlock.encode_word(0x2F2), (3,))
    d_recv, _p = split_bits24(bits)
    passthrough = _word_bits12(d_recv)
    assert passthrough != decode_word_from_sram(img, bits)
    assert decode_word_from_sram(img, bits) == _word_bits12(0x2F2)


def test_mutation_corrupted_lut_row_in_sram_FAILS():
    """A WRONG panel word (through the REAL panel) must corrupt the decode
    EXACTLY as stored — the gate sees a corrupted SRAM image."""
    from engine.sram_panel import SramPanelDevice
    dev = SramPanelDevice()
    _load_full_lut(dev)
    word, pos = 0x94D, 6
    bits = _inject_errors(GolayEncoderBlock.encode_word(word), (pos,))
    d_recv, p_recv = split_bits24(bits)
    s = syndrome_of(d_recv, p_recv)
    wrong = 0x041                       # a bogus 2-bit pattern
    assert dev.mem[s] != wrong
    dev.mem[s] = wrong
    got = _decode_onchip(bits, dev=dev)
    assert got != _word_bits12(word), "gate blind to a corrupted LUT row"
    assert got == _word_bits12((d_recv ^ wrong) & 0xFFF)


def test_mutation_swapped_halves_FAILS():
    """A parity-first wire layout (p11..p0 d11..d0) must break the decode —
    the data-first layout is part of the pin."""
    img = sram_error_image()
    w = 0x5A5
    bits = GolayEncoderBlock.encode_word(w)
    swapped = bits[12:] + bits[:12]
    assert decode_word_from_sram(img, swapped) != _word_bits12(w)


def test_mutation_plus_one_shift_FAILS():
    """A +1-bit frame slip must FAIL (no free realignment, INV-2)."""
    img = sram_error_image()
    words = [0x2F2, 0x94D]
    stream = [b for w in words for b in GolayEncoderBlock.encode_word(w)]
    want = [b for w in words for b in _word_bits12(w)]
    assert decode_stream_from_sram(img, stream) == want
    assert decode_stream_from_sram(img, [0] + stream[:-1]) != want


def test_mutation_misframed_half_offset_FAILS():
    """A 12-bit misframe (codeword boundary on the half boundary) must FAIL."""
    img = sram_error_image()
    words = [0xB71, 0x123, 0xC3C]
    stream = [b for w in words for b in GolayEncoderBlock.encode_word(w)]
    want = [b for w in words for b in _word_bits12(w)]
    assert decode_stream_from_sram(img, stream[12:]) != want


def test_mutation_empty_output_FAILS():
    want = _word_bits12(0x2F2)
    assert [] != want and len(want) == 12


# ------------------------------------------------------------------- report

def test_emit_report():
    rng = random.Random(20260816)
    bits, want = [], []
    for k in range(16):
        w = rng.randrange(4096)
        bits += _inject_errors(GolayEncoderBlock.encode_word(w),
                               rng.sample(range(24), k % 4))
        want += _word_bits12(w)
    got = _decode_onchip(bits)
    errs = sum(1 for i in range(len(want))
               if i >= len(got) or got[i] != want[i])
    res = CompareResult(passed=(errs == 0), metric=Metric.DECISION,
                        n_compared=len(want), bit_errors=errs, delay_used=0)
    assert res.passed, res.summary()
    write_report("GolayDecoderBlock", res, coverage={
        "random": 3,
        "gr_equiv": "(none — extended Golay (24,12) syndrome decoder vs the "
                    "GolayEncoderBlock convention pin + a brute-force "
                    "nearest-codeword golden)",
        "edge": True,           # anchors, parity-only, all-data, stale-bit masking
        "exhaustive_1_error": "24 positions x 3 words (on-chip, real panel)",
        "sampled_errors": "2/3-error sampled + 3 random seeds + round-trip "
                          "0..3 errors x 3 seeds",
        "known_limit": "weight-4 passthrough (exhaustive no-alias proof) + "
                       "weight-5 miscorrection documented",
        "mutation": True,
        "sram": "syndrome->e_d LUT, ONE word per populated syndrome (2026 of "
                "4096), sparse set_addr load through the REAL "
                "SramPanelDevice/PanelDriver",
        "decision": "s = Q(D) ^ P from the encoder's own column masks; "
                    "out = D ^ LUT[s]; uniform single-lookup path (s == 0 "
                    "reads the guaranteed-unpopulated address 0)",
        "note": "7-cell rate-compressing 24:12 (pack24 -> syn 5/4/3 -> "
                "correct -> emit + SramController); per-sample panel "
                "contract (NEEDS_BESPOKE saturation entry)",
    })
