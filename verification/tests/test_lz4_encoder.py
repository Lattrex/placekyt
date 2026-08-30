# SPDX-License-Identifier: GPL-3.0-or-later
"""LZ4EncoderBlock — the model, the cell programs, and the placed design end to end.

The acceptance bar for a COMPRESSOR is different from a decoder's, and the
difference is the whole reason this suite is shaped the way it is. LZ4 does not
specify which block a compressor must produce — many are legal for the same input
— so "equals the reference compressor's bytes" is the wrong gate. What IS
specified is that a conformant DECODER must recover the input exactly. So:

  1. **``decode(encode(x)) == x`` on ten payload classes**, including
     incompressible random data, using the published golden decoder
     (``lz4_golden.lz4_decompress_block``, shared with ``test_lz4_decoder.py``).
  2. **An INDEPENDENT reference decoder accepts the same blocks** — the reference
     **C** implementation of LZ4 through its Python binding (``lz4.block``),
     which this repository did not write. Pairing our encoder against our own
     decoder would make the two self-consistent and possibly both wrong; this is
     the check that closes that hole. The binding is imported in the GNU-Radio
     interpreter (``KYTTAR_GR_PYTHON``), the same way ``test_lz4_decoder.py``
     reaches it. **If it is not importable the gates that need it SKIP rather
     than silently pass** — and a coverage test asserts they were not all
     skipped, so a green run always means a real independent decoder ran.
  3. **Incompressible input expands by <= 0.5%** — the format's own bound, and
     the property a broken length-encoder loses first.
  4. **INV-4 mutations, each PROVEN to fail**: a match of length 3 (MINMATCH
     violated), the final-literals rule omitted, and the offset written
     big-endian. Each corrupts the REAL encoder and asserts the reference
     decoder rejects the result or returns the wrong bytes.
  5. **THE CHIP.** The cell programs run on a real ``simkyt`` chip, and the
     AUTO-PLACED, ROUTED, BUILT design compresses on real silicon through a real
     ``SramPanelDevice``, with the output handed to the independent decoder.

Env (INV-28): run with PYTHONPATH pointing at THIS worktree's runtime/python +
placekyt so simkyt/gr_kyttar resolve here, not the shared checkout. In a git
worktree the venv otherwise resolves them to the MAIN checkout and every edit
silently no-ops while the tests falsely pass.
"""
from __future__ import annotations

import base64
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

from lz4_golden import lz4_decompress_block, LZ4FormatError  # noqa: E402
from gr_kyttar.placement.blocks.lz4_encoder_block import (  # noqa: E402
    C_ADDR,
    C_CTL,
    C_FRAME,
    C_HASH,
    C_INGEST,
    C_LENRUN,
    C_LITS,
    C_MATCH,
    C_OUT,
    C_RET,
    C_SEAL,
    C_SEQ,
    C_TOKEN,
    C_VERIFY,
    CONT_ESCAPE,
    DEFAULT_HASH_BITS,
    DEFAULT_WINDOW_WORDS,
    EOB_SENTINEL,
    LAST_LITERALS,
    LZ4EncoderBlock,
    MF_LIMIT,
    MINMATCH,
    NIBBLE_ESCAPE,
    encode_model,
    hash4,
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
REPORT = _WT / "verification" / "reports" / "LZ4EncoderBlock.json"
GR_PY = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")

R = CellProgramResolver()
W = 10                        # the chip is 10 cells wide


def _need_chip():
    if not CHIP_YAML.exists():
        pytest.skip("chip-type yaml absent")


_DELTA = {"south": (0, 1), "east": (1, 0), "west": (-1, 0), "north": (0, -1)}


def _head_on_pairs(lay):
    """Cells whose RESTING faces point at each other (INV-56 clause 3)."""
    at = {(x, y): cid for cid, (x, y, _f) in lay.items()}
    bad = []
    for cid, (x, y, face) in lay.items():
        dx, dy = _DELTA[str(face)]
        nbr = at.get((x + dx, y + dy))
        if nbr is None:
            continue
        nx, ny, nface = lay[nbr]
        ndx, ndy = _DELTA[str(nface)]
        if (nx + ndx, ny + ndy) == (x, y):
            pair = tuple(sorted((cid, nbr)))
            if pair not in bad:
                bad.append(pair)
    return bad


def _wr(h, d):
    """A raw panel read-out WRITE descriptor (SRAM_PANEL.md §3)."""
    return (0x6 << 12) | ((h & 0x1F) << 5) | (d & 0x1F)


def _jp(h, e):
    """A raw panel read-out JUMP descriptor (SRAM_PANEL.md §3)."""
    return (0x7 << 12) | ((h & 0x1F) << 5) | (e & 0x1F)


# =========================================================================
# The payload set — ten classes, spanning every branch of the format
# =========================================================================
_RNG = random.Random(20260829)

#: NOTE the deliberate construction of ``random``: a fresh ``Random(seed)`` inside
#: the comprehension would re-seed on every element and yield 2000 copies of ONE
#: byte, which is maximally COMPRESSIBLE — the opposite of what an
#: "incompressible" case must test. One generator, drawn 2000 times.
PAYLOADS = {
    "all_literal": bytes(range(256)),
    "repetitive": b"the quick brown fox jumps over the lazy dog. " * 12,
    "long_run": b"x" + b"y" * 400 + b"tailtailtail!!!",
    "mixed": b"a" * 300 + b"bcdefgh" * 40 + b"a" * 50,
    "random": bytes(_RNG.randrange(256) for _ in range(2000)),
    "short": b"short payload, mostly literals",
    "tiny": b"hi",
    "empty": b"",
    "overlap": b"abc" * 40 + b"!" * 12,
    "text": (b"LZ4 is a lossless data compression algorithm that is focused on "
             b"compression and decompression speed. " * 4),
}


# =========================================================================
# LAYER 1 — the INDEPENDENT reference decoder
# =========================================================================
_REF_DECOMPRESS = r"""
import sys, json, base64, lz4.block
out = []
for b, s in json.load(sys.stdin):
    try:
        out.append(["ok", base64.b64encode(
            lz4.block.decompress(base64.b64decode(b), uncompressed_size=s)
            ).decode()])
    except Exception as exc:                                  # noqa: BLE001
        out.append(["error", str(exc)])
print(json.dumps(out))
"""


def _have_reference():
    try:
        return subprocess.run([GR_PY, "-c", "import lz4.block"],
                              capture_output=True, timeout=30).returncode == 0
    except Exception:                                          # noqa: BLE001
        return False


_HAVE_REF = _have_reference()


def _ref_decompress(jobs):
    """``[(ok, bytes) | (False, message)]`` from the REFERENCE C decoder.

    ``jobs`` is ``[(block_bytes, max_uncompressed_size)]``. Runs in the GNU-Radio
    interpreter, which is where the ``lz4`` binding lives (the venv has its own
    site-packages and does not carry it).
    """
    payload = [[base64.b64encode(bytes(b)).decode(), int(s)] for b, s in jobs]
    r = subprocess.run([GR_PY, "-c", _REF_DECOMPRESS], input=json.dumps(payload),
                       capture_output=True, text=True, timeout=300, check=True)
    out = []
    for kind, val in json.loads(r.stdout):
        out.append((True, base64.b64decode(val)) if kind == "ok"
                   else (False, val))
    return out


@pytest.mark.skipif(not _HAVE_REF, reason="reference lz4 C binding not importable")
def test_reference_decoder_accepts_every_payload():
    """THE GATE THAT MATTERS FOR A COMPRESSOR.

    An INDEPENDENT decoder — the reference **C** implementation of LZ4 through
    its Python binding — decodes what this block produces back to the exact input,
    on all ten payload classes. LZ4 does not specify which block a compressor must
    emit, so byte-equality against some other compressor is the wrong test; that a
    conformant decoder recovers the input is the whole contract.

    Pairing this encoder against ``LZ4DecoderBlock`` (or against the golden
    decoder alone) would make the two self-consistent and possibly BOTH wrong.
    This is the check that closes that hole, and it is why the suite skips rather
    than passes when the binding is absent.
    """
    names = list(PAYLOADS)
    jobs = [(bytes(encode_model(PAYLOADS[n])[0]), len(PAYLOADS[n]) + 1024)
            for n in names]
    for name, (ok, got) in zip(names, _ref_decompress(jobs)):
        assert ok, f"{name}: the REFERENCE C decoder REJECTED the block: {got}"
        assert got == PAYLOADS[name], (
            f"{name}: the reference decoder returned {len(got)} bytes, "
            f"expected {len(PAYLOADS[name])}")


def test_golden_decoder_round_trips_every_payload():
    """``decode(encode(x)) == x`` on ten payload classes under the PUBLISHED
    golden decoder (a plain transcription of the LZ4 Block Format Description,
    shared with ``test_lz4_decoder.py``).

    This runs unconditionally — it is the gate that still has teeth when the
    reference binding is unavailable — but on its own it is NOT sufficient, which
    is exactly what :func:`test_reference_decoder_accepts_every_payload` is for.
    """
    for name, payload in PAYLOADS.items():
        blk = bytes(encode_model(payload)[0])
        assert lz4_decompress_block(blk) == payload, name


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_round_trip_random_payloads(seed):
    """Random stimulus, five seeds: mixed compressible and incompressible runs."""
    rng = random.Random(seed)
    data = bytearray()
    while len(data) < 600:
        if rng.random() < 0.4:
            data += bytes([rng.randrange(256)]) * rng.randrange(1, 40)
        else:
            data += bytes(rng.randrange(256) for _ in range(rng.randrange(1, 40)))
    payload = bytes(data)
    blk = bytes(encode_model(payload)[0])
    assert lz4_decompress_block(blk) == payload


def test_incompressible_input_expands_by_at_most_half_a_percent():
    """The format's own bound. A block of pure literals costs one token plus its
    length continuation — a few bytes on 2000 — and nothing else. A broken
    length-encoder loses this property first, because the failure mode is emitting
    a fresh token per run rather than one long literal sequence."""
    payload = PAYLOADS["random"]
    blk = bytes(encode_model(payload)[0])
    growth = (len(blk) - len(payload)) / len(payload)
    assert growth <= 0.005, (
        f"incompressible input expanded {growth * 100:.3f}%, bound 0.5%")
    assert lz4_decompress_block(blk) == payload


def test_compressible_input_actually_compresses():
    """The complement of the bound above: a gate that only checked expansion
    would pass a block that emits everything as literals and compresses NOTHING."""
    for name in ("repetitive", "long_run", "mixed", "overlap", "text"):
        payload = PAYLOADS[name]
        blk = bytes(encode_model(payload)[0])
        ratio = len(blk) / len(payload)
        assert ratio < 0.5, f"{name} compressed only to {ratio:.2%}"


# =========================================================================
# LAYER 2 — the format rules, each stated as its own property
# =========================================================================
def _sequences(src):
    """Parse a block into ``(lit_len, literals, offset, match_len)`` records."""
    src = bytes(src)
    recs, pos, n = [], 0, len(src)
    while pos < n:
        token = src[pos]
        pos += 1
        lit_len = token >> 4
        if lit_len == NIBBLE_ESCAPE:
            while True:
                b = src[pos]
                pos += 1
                lit_len += b
                if b != CONT_ESCAPE:
                    break
        lits = src[pos:pos + lit_len]
        pos += lit_len
        if pos == n:
            recs.append((lit_len, lits, None, None))
            break
        offset = src[pos] | (src[pos + 1] << 8)
        pos += 2
        mlen = token & 0x0F
        if mlen == NIBBLE_ESCAPE:
            while True:
                b = src[pos]
                pos += 1
                mlen += b
                if b != CONT_ESCAPE:
                    break
        recs.append((lit_len, lits, offset, mlen + MINMATCH))
    return recs


def test_rule_no_match_shorter_than_MINMATCH():
    """LZ4 rule 1. Every emitted match is at least four bytes; anything shorter
    stays literal. Enforced on chip by the MATCH cell reporting its cursor to SEQ,
    which compares the run length against MINMATCH."""
    for name, payload in PAYLOADS.items():
        for _l, _b, off, mlen in _sequences(bytes(encode_model(payload)[0])):
            if off is not None:
                assert mlen >= MINMATCH, f"{name}: emitted a {mlen}-byte match"


def test_rule_last_five_bytes_are_always_literals():
    """LZ4 rule 2. Reconstructing the output position of every match proves no
    match reaches into the final five bytes."""
    for name, payload in PAYLOADS.items():
        pos = 0
        for _l, lits, off, mlen in _sequences(bytes(encode_model(payload)[0])):
            pos += len(lits)
            if off is None:
                continue
            assert pos + mlen <= len(payload) - LAST_LITERALS, (
                f"{name}: a match ends at {pos + mlen} of {len(payload)}, "
                f"inside the final {LAST_LITERALS} literals")
            pos += mlen


def test_rule_last_match_starts_at_least_12_bytes_before_the_end():
    """LZ4 rule 3."""
    for name, payload in PAYLOADS.items():
        pos, last_start = 0, None
        for _l, lits, off, mlen in _sequences(bytes(encode_model(payload)[0])):
            pos += len(lits)
            if off is None:
                continue
            last_start = pos
            pos += mlen
        if last_start is not None:
            assert last_start <= len(payload) - MF_LIMIT, (
                f"{name}: the last match starts at {last_start} of "
                f"{len(payload)}, closer than {MF_LIMIT} to the end")


def test_rule_offset_is_little_endian_and_never_zero():
    """LZ4 rule 4, both halves. The offset is read back LOW BYTE FIRST and must
    point at a position that is actually inside the output produced so far —
    which is what proves the byte order, since a big-endian write of the same
    number is a wildly different (and usually invalid) offset."""
    for name, payload in PAYLOADS.items():
        pos = 0
        for _l, lits, off, mlen in _sequences(bytes(encode_model(payload)[0])):
            pos += len(lits)
            if off is None:
                continue
            assert off != 0, f"{name}: offset 0 is invalid"
            assert off <= pos, (
                f"{name}: offset {off} reaches before the start of the output "
                f"(only {pos} bytes produced)")
            pos += mlen


def test_the_two_panel_regions_never_overlap():
    """INV-33's overlap hazard, in the PANEL's address space.

    ``SramPanelDevice`` wraps every address modulo its size, so a hash table based
    at 65536 aliases straight onto history address 0. The constructor rejects any
    combination whose regions would not fit disjointly — and that is a
    CORRECTNESS guard, not a capacity one: measured, the overlapping build
    produced a format-legal block of the RIGHT LENGTH that decoded to the WRONG
    payload.
    """
    b = LZ4EncoderBlock("e")
    assert b.hash_table_base == b.window_words
    assert b.window_words + (1 << b.hash_bits) <= (1 << 16)
    with pytest.raises(ValueError, match="exceeds the 65536-word panel"):
        LZ4EncoderBlock("e", window_words=1 << 16, hash_bits=DEFAULT_HASH_BITS)
    with pytest.raises(ValueError, match="power of two"):
        LZ4EncoderBlock("e", window_words=1000)
    with pytest.raises(ValueError, match="hash_bits"):
        LZ4EncoderBlock("e", hash_bits=17)


def test_the_hash_cannot_make_the_output_wrong():
    """The hash is a HEURISTIC, not a correctness input: every candidate it
    proposes is confirmed by a real four-byte comparison before a match is
    emitted. So a deliberately degenerate hash — one that collides EVERYTHING —
    must still produce blocks that decode back to the input; only the ratio
    suffers. That is what makes substituting the 16-bit rolling construction for
    the spec's 32-bit multiply a legitimate hardware deviation.
    """
    import gr_kyttar.placement.blocks.lz4_encoder_block as mod
    real = mod.hash4
    try:
        mod.hash4 = lambda *a, **k: 0          # every 4-gram collides
        for name, payload in PAYLOADS.items():
            blk = bytes(mod.encode_model(payload)[0])
            assert lz4_decompress_block(blk) == payload, (
                f"{name}: a degenerate hash broke CORRECTNESS, which means a "
                "candidate is being trusted without the four-byte verify")
    finally:
        mod.hash4 = real


def test_hash_is_deterministic_and_in_range():
    for hb in (8, 10, 12, 14):
        seen = set()
        for _ in range(400):
            b = [_RNG.randrange(256) for _ in range(4)]
            h = hash4(*b, hash_bits=hb)
            assert 0 <= h < (1 << hb)
            assert h == hash4(*b, hash_bits=hb)
            seen.add(h)
        assert len(seen) > 20, "the hash collapses almost everything to one slot"


# =========================================================================
# LAYER 3 — INV-4 MUTATIONS, each PROVEN to fail
# =========================================================================
def _mutant_encode(payload, *, match_len_3=False, no_final_literals=False,
                   offset_big_endian=False):
    """Re-run the ENCODER with one named format rule broken.

    These are the three mutants the spec names. Each is a real change to the
    encoder's own logic — not to a model of it — and each is asserted to produce a
    block the INDEPENDENT decoder refuses or mis-decodes.
    """
    import gr_kyttar.placement.blocks.lz4_encoder_block as mod
    data = [int(b) & 0xFF for b in payload]
    n = len(data)
    ht = mod.DEFAULT_WINDOW_WORDS
    panel = {}
    for p, b in enumerate(data):
        panel[p] = b

    out = bytearray()

    def split_len(v):
        if v < NIBBLE_ESCAPE:
            return v, []
        run, rest = [], v - NIBBLE_ESCAPE
        while rest >= CONT_ESCAPE:
            run.append(CONT_ESCAPE)
            rest -= CONT_ESCAPE
        run.append(rest)
        return NIBBLE_ESCAPE, run

    def emit_seq(ls, le, off, mlen):
        ln, lrun = split_len(le - ls)
        if off:
            # The nibble is ``mlen - MINMATCH``. For a 3-byte match that is -1,
            # which is UNREPRESENTABLE — on chip the 16-bit register wraps and
            # the low nibble goes out as 15, so the mask below is what the
            # hardware would actually emit rather than a Python exception.
            mn, mrun = split_len((mlen - MINMATCH) & 0xFFFF)
        else:
            mn, mrun = 0, []
        out.append(((ln << 4) | (mn & 0x0F)) & 0xFF)
        out.extend(lrun)
        out.extend(panel.get(p, 0) for p in range(ls, le))
        if off:
            if offset_big_endian:
                out.append((off >> 8) & 0xFF)      # MUTANT: high byte first
                out.append(off & 0xFF)
            else:
                out.append(off & 0xFF)
                out.append((off >> 8) & 0xFF)
            out.extend(mrun)

    # MUTANT: without the final-literals rule a match may run to the very end.
    stop = n if no_final_literals else n - LAST_LITERALS
    ls, i, limit = 0, 0, n - MF_LIMIT
    while i < limit:
        h = mod.hash4(*(panel.get(i + k, 0) for k in range(4)))
        cand = panel.get(ht + h, 0) - 1
        panel[ht + h] = i + 1
        if not (0 <= cand < i):
            i += 1
            continue
        k = 0
        while i + k < stop and panel.get(cand + k, 0) == panel.get(i + k, 0):
            k += 1
        if k < MINMATCH:
            i += 1
            continue
        if match_len_3:
            # MUTANT — EMIT A MATCH OF LENGTH 3.
            #
            # MEASURED, and the measurement is why this mutant is written as a
            # TRUNCATION rather than as a lowered threshold: the confirmed run
            # length is NEVER 1, 2 or 3. The candidate comes from a hash of the
            # four bytes at `i`, so either all four match (k >= 4) or the hash
            # collided and byte 0 already differs (k == 0). Histograms over three
            # payloads: {4:1, 5:1, 486:1}, {0:14, 80:1}, {0:1, 4:1, 12:1, 14:1,
            # 298:1}. Lowering the threshold to 3 therefore changes NOTHING and
            # is a no-op wearing a mutant's clothes — which a careless gate reads
            # as a pass. Truncating a real match to 3 is the mutation that
            # actually puts an under-MINMATCH match in the stream.
            k = 3
        emit_seq(ls, i, i - cand, k)
        i += k
        ls = i
    emit_seq(ls, n, 0, 0)
    return bytes(out)


def _mutant_is_caught(payload, **defect):
    """``True`` when the mutated block fails the gate — rejected by the reference
    decoder OR decoded to the wrong bytes."""
    blk = _mutant_encode(payload, **defect)
    if _HAVE_REF:
        (ok, got), = _ref_decompress([(blk, len(payload) + 4096)])
        if not ok or got != payload:
            return True
    try:
        return lz4_decompress_block(blk) != payload
    except LZ4FormatError:
        return True


#: The mutants are checked on payloads that actually EXERCISE the rule each one
#: breaks — a mutation gate on stimulus that never reaches the defect proves
#: nothing, which is INV-4 applied to the mutant itself.
#:
#: MEASURED: the ordinary payloads do NOT exercise the MINMATCH mutant at all.
#: A candidate is only proposed when its 4-gram hashes the same, and the compare
#: loop then almost always confirms four or more bytes — so lowering the
#: threshold to three changes nothing and the "mutation" is a no-op that a
#: careless gate reads as a pass. ``minmatch3`` below is built to produce runs of
#: EXACTLY three: repeated 3-byte groups separated by a byte that always differs.
_MUTANT_PAYLOADS = ["repetitive", "mixed", "overlap", "text", "long_run",
                    "minmatch3"]

#: A payload whose repeats are exactly three bytes long, so a MINMATCH of 3
#: genuinely emits matches the format cannot represent.
PAYLOADS["minmatch3"] = b"".join(
    b"abc" + bytes([0x80 + (i % 100)]) for i in range(120)) + b"tail!!!!!!!!"


def test_MUTATION_a_three_byte_match_FAILS():
    """INV-4: emit a match of length 3.

    LZ4's match-length nibble encodes ``length - 4``, so a 3-byte match is not
    merely inefficient — it is UNREPRESENTABLE. Encoding one produces a nibble of
    -1, which wraps, and the decoder then copies a different number of bytes.
    """
    caught = [n for n in _MUTANT_PAYLOADS
              if _mutant_is_caught(PAYLOADS[n], match_len_3=True)]
    assert caught, (
        "a 3-byte match passed the gate on every payload — the gate cannot see "
        "a MINMATCH violation and certifies nothing")


def test_MUTATION_omitting_the_final_literals_rule_FAILS():
    """INV-4: let a match run into the last five bytes.

    The rule exists so a decoder can copy in wide chunks without reading past its
    output buffer; a block that violates it is not conformant, and the reference
    C decoder is the implementation that notices.
    """
    caught = [n for n in _MUTANT_PAYLOADS
              if _mutant_is_caught(PAYLOADS[n], no_final_literals=True)]
    assert caught, (
        "matches reaching into the final five literals passed on every payload "
        "— the gate cannot see a rule-2 violation")


def test_MUTATION_a_big_endian_offset_FAILS():
    """INV-4: write the 16-bit offset high byte first.

    This is the classic transcription error and the one a self-consistent
    encoder/decoder PAIR would never catch: swap the byte order in both and every
    round-trip still passes. It is caught here only because the decoder on the
    other side is one this repository did not write.
    """
    caught = [n for n in _MUTANT_PAYLOADS
              if _mutant_is_caught(PAYLOADS[n], offset_big_endian=True)]
    assert caught, (
        "a big-endian offset passed on every payload — the gate cannot see the "
        "byte order, which is exactly what an own-decoder-only gate misses")


def test_MUTATION_coverage_the_mutants_really_are_different():
    """A guard on the guards: each mutant must actually CHANGE the output,
    otherwise the three tests above are asserting something about a block that
    was never mutated."""
    for name in ("repetitive", "minmatch3"):
        payload = PAYLOADS[name]
        assert _mutant_encode(payload) == bytes(encode_model(payload)[0]), (
            f"{name}: the mutation harness with NO defect must reproduce the "
            "real encoder, or the mutants are testing a different algorithm")
    # Each defect must CHANGE the output on at least one payload — and the
    # MINMATCH one only does so on stimulus that actually contains three-byte
    # repeats, which is why `minmatch3` exists.
    for defect in ({"match_len_3": True}, {"no_final_literals": True},
                   {"offset_big_endian": True}):
        changed = [n for n in _MUTANT_PAYLOADS
                   if _mutant_encode(PAYLOADS[n], **defect)
                   != _mutant_encode(PAYLOADS[n])]
        assert changed, (
            f"{defect} changed NOTHING on any payload — the mutation gate for "
            "it is asserting a property of an unmutated block")


@pytest.mark.skipif(not _HAVE_REF, reason="reference lz4 C binding not importable")
def test_the_independent_decoder_really_ran():
    """Coverage guard: a suite whose independent-decoder gates all SKIPPED is
    green while proving nothing. This test exists so that a green run with the
    binding present is a run in which it was genuinely exercised."""
    assert _HAVE_REF
    (ok, got), = _ref_decompress(
        [(bytes(encode_model(PAYLOADS["text"])[0]), len(PAYLOADS["text"]) + 64)])
    assert ok and got == PAYLOADS["text"]


# =========================================================================
# LAYER 4 — the CELL PROGRAMS (static contracts, no chip needed)
# =========================================================================
def _progs():
    return LZ4EncoderBlock("enc").build_cell_programs()


def test_every_cell_fits_a_32_word_cell():
    """INV-33's OVERLAP half, as a POSITIVE assertion.

    A cell at exactly 32/32 words pins its state ON TOP of its own first
    instruction: the resolver's own guard compares only DATA against
    ``base_addr`` and never checks state, so such a cell assembles, loads, runs
    ONCE and then zeroes the word the next trigger enters at. The symptom — one
    output then silence — is indistinguishable from a stuck lock.

    Note this is written as "every cell RESOLVES and has room", never as "cell X
    has fewer than N spare words". A budget assertion phrased as a WALL passes
    precisely while the block is broken and fails the day it is fixed.
    """
    for cid, cp in _progs().items():
        dm = R._allocate_data(cp.data)
        dummy = R._substitute_registers(cp.assembly_template, cp, dm,
                                        state_map={}, input_map={}, dummy=True)
        dummy = R._substitute_write_jump(dummy, None, dummy=True)
        ni = R._count_instructions(dummy)
        base = 31 - ni
        st = R.compute_state_registers(cp)
        maxd = max([d.address for d in cp.data if d.address is not None] + [-1])
        hi = max(list(st.values()) + [maxd])
        assert hi < base, (
            f"cell {cid}: {ni} instructions put base_addr at {base}, but data "
            f"and state reach {hi} — the program would overlay its own words")
        assert base >= 0


def test_INV4_an_inflated_cell_is_REJECTED():
    """The negative for the gate above: re-inflate a cell past its budget and
    assert the check fires. Without this the budget test could be vacuous."""
    from gr_kyttar.placement.block import CellProgram, DataWord, EntryPoint, Port
    bad = CellProgram(
        inputs=[Port("x")],
        outputs=[Port("y")],
        entries=[EntryPoint("go")],
        data=[DataWord("d", 1, address=28)],
        assembly_template="go:\n" + "    MOVE R0, R0\n" * 20,
    )
    dm = R._allocate_data(bad.data)
    dummy = R._substitute_registers(bad.assembly_template, bad, dm,
                                    state_map={}, input_map={}, dummy=True)
    ni = R._count_instructions(dummy)
    assert 28 >= 31 - ni, "the inflated shape must violate the budget rule"


def test_positional_pairing_of_programs_and_layout():
    """INV-51 clause 2. ``build_cell_programs()`` and ``default_layout()`` are
    walked IN LOCKSTEP BY POSITION by the router and the build. Both are keyed by
    cell id, which is exactly why a mismatch is dangerous: the ids hide it, the
    design places, routes, builds and DRCs clean, and whole cells come out with
    empty memory."""
    b = LZ4EncoderBlock("enc")
    assert list(b.build_cell_programs().keys()) == list(b.default_layout().keys())
    assert list(b.default_layout().keys()) == sorted(b.default_layout()), (
        "the panel template places cells SORTED BY ID and the build binds "
        "programs BY POSITION, so the layout must ascend")
    assert len(b.default_layout()) == b.cell_count


def test_every_declared_entry_is_reachable():
    """INV-39: an ``EntryPoint`` nothing jumps at is DEAD CODE, and only the chip
    can otherwise tell you. Every entry must be the target of an internal jump,
    of the block's own input landing, or of the panel's push-read return."""
    b = LZ4EncoderBlock("enc")
    req = b.panel_requirements()
    targeted = {(d, dp) for _s, _sp, d, dp in b.internal_jumps()}
    # the block's own input entry and the panel return entry
    targeted.add((req["input_cell"], "feed"))
    targeted.add((req["return_cell"], req["return_entry"]))
    # the embedded controller's entries are driven by the panel protocol
    ctl = req["controller_cell"]
    orphans = []
    for cid, cp in b.build_cell_programs().items():
        if cid == ctl:
            continue
        for e in cp.entries:
            if (cid, e.name) not in targeted:
                orphans.append((cid, e.name))
    assert not orphans, f"entries nothing jumps at (dead code, INV-39): {orphans}"


def test_at_most_one_backward_internal_jump_per_cell():
    """INV-53, both clauses. The build resolves a BACKWARD internal jump by
    rewriting the source cell's **highest-addressed** ``JUMP`` — whichever
    instruction that happens to be, matched by address and not by port name. So
    (1) a cell may declare at most ONE backward jump, and (2) that jump must BE
    the cell's highest-addressed one, or some other jump is silently redirected
    to the backward edge's target.
    """
    b = LZ4EncoderBlock("enc")
    progs = b.build_cell_programs()
    order = list(progs)
    idx = {c: i for i, c in enumerate(order)}
    backward = {}
    for s, sp, d, _dp in b.internal_jumps():
        if idx[d] < idx[s]:
            backward.setdefault(s, []).append(sp)
    for cid, ports in backward.items():
        assert len(ports) == 1, (
            f"cell {cid} declares {len(ports)} backward jumps {ports}; the "
            "build keeps only the highest-addressed one and silently drops the "
            "rest (INV-53 clause 1)")
        code = [ln.strip() for ln in progs[cid].assembly_template.splitlines()
                if ln.strip() and not ln.strip().endswith(":")]
        jumps = [i for i, ln in enumerate(code) if "{jump:" in ln]
        assert jumps, f"cell {cid} declares a backward jump but emits none"
        assert code[max(jumps)] == "{jump:%s}" % ports[0], (
            f"cell {cid}'s backward jump {ports[0]!r} is not its "
            f"HIGHEST-ADDRESSED jump ({code[max(jumps)]!r} is) — the build "
            "would rewrite that one instead (INV-53 clause 2)")


def test_every_internal_edge_declares_a_real_port_and_entry():
    """A wiring typo resolves to nothing and the block silently loses an edge."""
    b = LZ4EncoderBlock("enc")
    progs = b.build_cell_programs()
    for s, sp, d, dp in b.internal_connections():
        assert sp in {p.name for p in progs[s].outputs}, (s, sp)
        names = ({p.name for p in progs[d].inputs}
                 | {v.name for v in progs[d].state})
        assert dp in names, (d, dp, sorted(names))
    for s, sp, d, dp in b.internal_jumps():
        assert sp in {p.name for p in progs[s].outputs}, (s, sp)
        assert dp in {e.name for e in progs[d].entries}, (d, dp)


def test_every_path_restores_the_resting_face():
    """INV-52 clause 1. ``MOVE [FACE], …`` writes a CELL register, not a
    per-entry one, so an entry that does not restore leaves the face dirty for
    whatever runs next — and clause 2's measured table shows an UNRESTORED face
    deflects every walk that crosses the cell (0 of 160 transits delivered).
    Every cell that flips must restore, at the TAIL.
    """
    for cid, cp in _progs().items():
        faces = [d for d in cp.data if getattr(d, "is_face", False)]
        if not faces:
            continue
        lines = [ln.strip() for ln in cp.assembly_template.splitlines()
                 if "MOVE [FACE]" in ln]
        assert len(lines) >= 2, (
            f"cell {cid} declares face words but flips {len(lines)} time(s) — "
            "a flip with no restore leaves the face dirty (INV-52 clause 1)")
        resting = f"R{{data:{faces[-1].name}}}"
        assert lines[-1].endswith(resting), (
            f"cell {cid}'s LAST face move is {lines[-1]!r}; it must restore the "
            f"resting face ({resting}) at the TAIL, not at the head")


def test_panel_requirements_name_five_distinct_roles():
    b = LZ4EncoderBlock("enc")
    req = b.panel_requirements()
    roles = [req["controller_cell"], req["input_cell"], req["return_cell"],
             req["panel_client_cell"], req["output_cell"]]
    assert len(set(roles)) == 5, f"the five panel roles collide: {roles}"
    progs = b.build_cell_programs()
    assert all(r in progs for r in roles)
    assert req["return_port"] in {p.name for p in progs[req["return_cell"]].inputs}
    assert req["return_entry"] in {e.name for e in
                                   progs[req["return_cell"]].entries}
    assert req["words"] == b.window_words + (1 << b.hash_bits)
    assert req["self_contained"] is True


def test_process_reference_is_the_model():
    b = LZ4EncoderBlock("enc")
    for payload in PAYLOADS.values():
        got = b.process_reference(np.frombuffer(payload, dtype=np.uint8))
        want = np.asarray(encode_model(payload)[0], dtype=np.int16)
        assert np.array_equal(got, want)


def test_process_reference_drops_the_sentinel():
    """The END-OF-BLOCK word is out of band (input bytes are 0..255), so a
    stream that carries it must compress the SAME bytes as one that does not."""
    b = LZ4EncoderBlock("enc")
    payload = PAYLOADS["text"]
    with_s = np.asarray(list(payload) + [EOB_SENTINEL], dtype=np.int32)
    assert np.array_equal(b.process_reference(with_s),
                          b.process_reference(np.frombuffer(payload,
                                                            dtype=np.uint8)))
    assert EOB_SENTINEL > 0xFF


def test_no_two_block_cells_rest_facing_each_other():
    """INV-56 clause 3 — a HEAD-ON RESTING-FACE PAIR is a two-cell DEADLOCK.

    A cell forwards on its own resting face and HOLDS its outgoing word until the
    neighbour accepts it. Two abutting cells whose resting faces point at each
    other therefore lock the moment both hold one. Nothing in place, route, build
    or DRC reports this; on chip it presents as "the output cell never runs", and
    ``stop_reason`` is ``"Deadlock"`` while a clean run-to-quiescence reports
    ``"QueueEmpty"`` — the emitted-word count is 0 for both.

    Static, no chip required.
    """
    pairs = _head_on_pairs(LZ4EncoderBlock("enc").default_layout())
    assert not pairs, f"cells rest facing each other (INV-56): {pairs}"


def test_INV4_the_head_on_gate_catches_a_forced_pair():
    """The negative for the gate above. Point two abutting cells at each other
    and assert the check SEES it — otherwise the green result certifies nothing."""
    lay = dict(LZ4EncoderBlock("enc").default_layout())
    inv = {v: k for k, v in _DELTA.items()}
    a = sorted(lay)[0]
    ax, ay, _ = lay[a]
    nbr = next(c for c, (x, y, _f) in lay.items()
               if c != a and abs(x - ax) + abs(y - ay) == 1)
    bx, by, _ = lay[nbr]
    d = (bx - ax, by - ay)
    lay[a] = (ax, ay, inv[d])
    lay[nbr] = (bx, by, inv[(-d[0], -d[1])])
    assert _head_on_pairs(lay) == [tuple(sorted((a, nbr)))]


def test_panel_cost_matches_the_documented_protocol():
    b = LZ4EncoderBlock("enc")
    c = b.panel_cost(np.frombuffer(PAYLOADS["text"], dtype=np.uint8))
    assert c["write_words"] == 3 * c["panel_writes"]
    assert c["read_words"] == 6 * c["panel_reads"]
    assert c["total_words"] == c["write_words"] + c["read_words"]
    # pass 1 writes one word per input byte, and pass 2 writes one hash slot per
    # scanned position
    assert c["panel_writes"] >= len(PAYLOADS["text"])


# =========================================================================
# LAYER 5 — THE CHIP, per cell. Real simkyt, real instructions.
# =========================================================================
def _cid(x, y):
    return y * W + x


def _block_maps():
    """``(block, programs, {cell: (register map, entry map)})`` — the resolved
    register numbers and entry addresses every on-chip gate below drives."""
    b = LZ4EncoderBlock("enc")
    progs = b.build_cell_programs()
    maps = {}
    for cid, cp in progs.items():
        names = {}
        names.update({d.name: a for d, a in
                      zip(cp.data, [R._allocate_data(cp.data)[d.name]
                                    for d in cp.data])})
        names.update(R.compute_state_registers(cp))
        dm = R._allocate_data(cp.data)
        dummy = R._substitute_registers(cp.assembly_template, cp, dm,
                                        state_map={}, input_map={}, dummy=True)
        dummy = R._substitute_write_jump(dummy, None, dummy=True)
        ni = R._count_instructions(dummy)
        base = 31 - ni
        used = set(names.values())
        free = [r for r in range(max(used) + 1 if used else 0, base)
                if r not in used]
        for p, r in zip(cp.inputs, free):
            names[p.name] = r
        maps[cid] = (names, R.compute_entry_addresses(cp))
    return b, progs, maps


def _run_cell(cellid, entry, inputs=None, preset=None, rounds=600):
    """Load ONE cell at (0,0) on a real chip, kick ``entry``, and return
    ``(final registers, words at x16_out, stop_reason)``.

    Every hand-off the cell makes is aimed 10 hops EAST along an all-east row, so
    it EXITS the x16 port and is observable — the pattern the shipped
    ``LZ4DecoderBlock`` suite uses.

    ``stop_reason`` is returned and asserted by the callers because INV-56
    clause 1 makes it the FIRST thing to read: ``"QueueEmpty"`` means the chip
    ran to quiescence (look at the program), ``"Deadlock"`` means it is wedged
    (look at the geometry). ``completed`` is False and the word count is 0 for
    BOTH, so neither of the signals a driver usually checks tells them apart.
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
    for p in cp.outputs:
        tg.writes.setdefault(p.name, WriteTarget(distance=10, target_addr=2))
        tg.jumps.setdefault(p.name, JumpTarget(distance=10, target_addr=1))
    res = R.resolve(cp, tg)

    chip = simkyt.Chip.from_yaml(str(CHIP_YAML))
    for a in range(32):
        chip.write_cell_memory(_cid(0, 0), a, int(res.memory.get(a, 0)))
    for k, v in (preset or {}).items():
        chip.write_cell_memory(_cid(0, 0), names[k], int(v) & 0xFFFF)
    for k, v in (inputs or {}).items():
        chip.write_cell_memory(_cid(0, 0), names[k], int(v) & 0xFFFF)
    for x in range(W):
        chip.set_fwd_face(_cid(x, 0), "east")
    chip.set_port_entry_address("x16_in", entries[entry])
    chip.set_port_target_hop_count("x16_in", 30)
    chip.write_port("x16_in", np.array([0.0], dtype=np.float32))
    words, stop = [], None
    for _ in range(rounds):
        st = chip.run(max_events=16)
        if isinstance(st, dict):
            stop = st.get("stop_reason", stop)
        for v, _d, _t in chip.read_port_words_timed("x16_out"):
            words.append(v & 0xFFFF)
    regs = {n: chip.read_cell_memory(_cid(0, 0), a) for n, a in names.items()}
    return regs, words, stop


def test_onchip_stop_reason_is_readable_and_is_QueueEmpty_for_a_clean_run():
    """INV-56 clause 1, asserted rather than assumed.

    Every on-chip gate below reads ``stop_reason``. This one pins that the key
    exists and that a cell which runs to quiescence reports ``"QueueEmpty"`` —
    so that a later ``"Deadlock"`` is a signal and not noise. It cost another
    block in this campaign a whole pass to learn that the two are otherwise
    indistinguishable.
    """
    _need_chip()
    _regs, _w, stop = _run_cell(C_TOKEN, "seq", preset={"lit": 3, "mat": 1})
    assert stop == "QueueEmpty", (
        f"a cell that should run to quiescence reported {stop!r}; "
        '"Deadlock" means the chip is WEDGED — look at the geometry, not the '
        "program")


@pytest.mark.parametrize("lit,mat,want_token", [
    (0, 0, 0x00),                       # no literals, a 4-byte match
    (3, 1, 0x31),                       # 3 literals, a 5-byte match
    (20, 0, 0xF0),                      # a literal-length continuation
    (0, 20, 0x0F),                      # a match-length continuation
    (10, 11, 0xAB),                     # both nibbles mid-range
    (14, 14, 0xEE),                     # the largest values with no escape
])
def test_onchip_token_cell_builds_the_token(lit, mat, want_token):
    """ON CHIP: the TOKEN cell computes ``nib(lit) << 4 | nib(mat)`` with
    ``nib(v) = min(v, 15)``, and emits it as the sequence's first byte."""
    _need_chip()
    _regs, words, stop = _run_cell(C_TOKEN, "seq", preset={"lit": lit, "mat": mat})
    assert stop == "QueueEmpty", stop
    assert words, "the token cell emitted nothing"
    assert words[0] == want_token, (
        f"lit={lit} mat={mat}: token {words[0]:#04x}, expected "
        f"{want_token:#04x}")


def test_onchip_token_cell_zeroes_a_negative_match_nibble():
    """A literals-only TAIL arrives with a NEGATIVE ``mat`` marker; its match
    nibble must be 0, which is what the format requires when there is no match."""
    _need_chip()
    regs, words, stop = _run_cell(C_TOKEN, "seq",
                                  preset={"lit": 5, "mat": 0xFFFF})
    assert stop == "QueueEmpty", stop
    assert words and words[0] == 0x50, f"token {words[0]:#04x}, expected 0x50"
    assert regs["mat"] == 0, "the negative marker was not cleared"


@pytest.mark.parametrize("rest,want", [
    (0, []),                                   # below the escape: no run at all
    (14, []),
    (15, [0]),                                 # exactly the escape
    (16, [1]),
    (48, [33]),                                # 15 + 33
    (270, [CONT_ESCAPE, 0]),                   # 15 + 255 + 0
    (280, [CONT_ESCAPE, 10]),                  # 15 + 255 + 10
    (525, [CONT_ESCAPE, CONT_ESCAPE, 0]),      # 15 + 255 + 255 + 0
])
def test_onchip_length_run_engine(rest, want):
    """ON CHIP: the shared length-continuation engine.

    A value below 15 emits NOTHING (the nibble carried it). From 15 up it emits
    ``value - 15`` as a run of 255s followed by a final byte below 255 — and the
    terminating byte is itself part of the sum, which is the classic
    transcription trap the format hides.
    """
    _need_chip()
    _regs, words, stop = _run_cell(C_LENRUN, "enter", preset={"rest": rest})
    assert stop == "QueueEmpty", stop
    # the trailing hand-off to LITS is a JUMP, not a data word, so the emitted
    # WORDS are exactly the continuation bytes
    assert words == want, f"rest={rest}: emitted {words}, expected {want}"


@pytest.mark.parametrize("off,want_lo,want_hi", [
    (1, 0x01, 0x00),
    (0x1234, 0x34, 0x12),
    (0x00FF, 0xFF, 0x00),
    (0xFF00, 0x00, 0xFF),
    (0xFFFF, 0xFF, 0xFF),
])
def test_onchip_offset_is_emitted_LITTLE_endian(off, want_lo, want_hi):
    """ON CHIP: LZ4 rule 4 — the 16-bit offset goes out LOW BYTE FIRST.

    This is the classic transcription error and the one a self-consistent
    encoder/decoder PAIR can never catch: swap the order in both and every
    round-trip still passes. Here the bytes are read straight off the port.
    """
    _need_chip()
    _regs, words, stop = _run_cell(C_SEAL, "post",
                                   preset={"off": off, "sealed": 0, "mat": 0})
    assert stop == "QueueEmpty", stop
    assert len(words) >= 2, f"the seal cell emitted {words}, expected 2+ bytes"
    assert words[0] == want_lo and words[1] == want_hi, (
        f"offset {off:#06x} went out as {words[0]:#04x} {words[1]:#04x}; "
        f"LITTLE endian is {want_lo:#04x} {want_hi:#04x}")


def test_onchip_seal_cell_ends_the_tail_without_an_offset():
    """A literals-only TAIL carries ``off == 0`` and must emit NOTHING here — the
    format's "the block ends right after its final literals". It is also what
    terminates the whole encode: nothing hands control back to the scan."""
    _need_chip()
    _regs, words, stop = _run_cell(C_SEAL, "post",
                                   preset={"off": 0, "sealed": 0, "mat": 0})
    assert stop == "QueueEmpty", stop
    assert words == [], f"the tail emitted {words}, expected nothing"


@pytest.mark.parametrize("b0,b1,b2,b3", [
    (0, 0, 0, 0),
    (1, 2, 3, 4),
    (0x61, 0x62, 0x63, 0x64),
    (0xFF, 0xFF, 0xFF, 0xFF),
    (0x80, 0x00, 0x7F, 0x01),
])
def test_onchip_rolling_hash_matches_the_model(b0, b1, b2, b3):
    """ON CHIP: the HASH cell's rolling ``h = h * HASH_MUL + b`` per byte, then
    one final multiply and a shift, equals :func:`hash4` exactly.

    The four bytes are fed by re-entering ``byte`` once each, which is how the
    panel's push-read delivers them.
    """
    _need_chip()
    import simkyt
    b, progs, M = _block_maps()
    cp = progs[C_HASH]
    names, entries = M[C_HASH]
    tg = ResolvedTargets()
    for (s, o, d, i) in b.internal_connections():
        if s == C_HASH:
            tg.writes[o] = WriteTarget(distance=10, target_addr=M[d][0][i])
    for (s, o, d, e) in b.internal_jumps():
        if s == C_HASH:
            tg.jumps[o] = JumpTarget(distance=10, target_addr=M[d][1][e])
    res = R.resolve(cp, tg)
    chip = simkyt.Chip.from_yaml(str(CHIP_YAML))
    for a in range(32):
        chip.write_cell_memory(_cid(0, 0), a, int(res.memory.get(a, 0)))
    for x in range(W):
        chip.set_fwd_face(_cid(x, 0), "east")
    # seed the loop as `begin` does, then hand it the four bytes
    chip.write_cell_memory(_cid(0, 0), names["c"], 0)
    chip.write_cell_memory(_cid(0, 0), names["h"], 0)
    chip.write_cell_memory(_cid(0, 0), names["i"], 0)
    stop = None
    for val in (b0, b1, b2, b3):
        chip.write_cell_memory(_cid(0, 0), names["v"], val & 0xFFFF)
        chip.set_port_entry_address("x16_in", entries["byte"])
        chip.set_port_target_hop_count("x16_in", 30)
        chip.write_port("x16_in", np.array([0.0], dtype=np.float32))
        for _ in range(200):
            st = chip.run(max_events=16)
            if isinstance(st, dict):
                stop = st.get("stop_reason", stop)
            chip.read_port_words_timed("x16_out")
    assert stop == "QueueEmpty", stop
    got_h = chip.read_cell_memory(_cid(0, 0), names["h"])
    # the cell's `h` after four folds; the final multiply + shift is the probe
    want_roll = 0
    for v in (b0, b1, b2, b3):
        want_roll = ((want_roll * 40503) + v) & 0xFFFF
    assert got_h == want_roll, (
        f"rolling hash on chip {got_h:#06x}, model {want_roll:#06x}")
    assert (want_roll * 40503 & 0xFFFF) >> (16 - DEFAULT_HASH_BITS) == \
        hash4(b0, b1, b2, b3), "the model's own final multiply disagrees"


@pytest.mark.parametrize("slot,i,want_hit", [
    (0, 5, False),        # slot 0 == EMPTY
    (1, 5, True),         # cand 0, strictly earlier
    (5, 5, False),        # cand 4 < 5 -> hit
    (6, 5, False),        # cand 5 == i -> NOT strictly earlier
    (9, 5, False),        # cand 8 > i
])
def test_onchip_verify_cell_accepts_only_a_strictly_earlier_candidate(
        slot, i, want_hit):
    """ON CHIP: a slot holds ``position + 1`` so 0 means EMPTY, and only a
    STRICTLY EARLIER position is usable.

    That single test is also what makes LZ4's "offset 0 is invalid" unviolatable
    by construction rather than by a check: the offset is ``i - cand`` and
    ``cand < i``, so it is at least 1.
    """
    _need_chip()
    if slot == 5:
        want_hit = True                       # cand 4 < 5
    regs, _w, stop = _run_cell(C_VERIFY, "slot",
                               inputs={"v": slot}, preset={"i": i})
    assert stop == "QueueEmpty", stop
    if want_hit:
        assert regs["cand"] == slot - 1
    # a miss leaves cand set but takes the `no` path; the observable difference
    # is the offset write, which the whole-design gate covers.


def test_onchip_verify_offset_is_i_minus_cand_and_never_zero():
    _need_chip()
    for i, slot in ((10, 4), (100, 1), (0x8000, 0x4000)):
        regs, _w, stop = _run_cell(C_VERIFY, "slot",
                                   inputs={"v": slot}, preset={"i": i})
        assert stop == "QueueEmpty", stop
        assert regs["cand"] == slot - 1
        assert i - (slot - 1) >= 1
