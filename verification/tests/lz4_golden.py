# SPDX-License-Identifier: GPL-3.0-or-later
"""GOLDEN model of the LZ4 **block format** decoder (there is NO stock GNU Radio block).

Spec: the published *LZ4 Block Format Description* (``doc/lz4_Block_format.md`` in the
lz4/lz4 reference repository, maintained by Yann Collet). Transcribed from that document
and cross-checked byte-for-byte against the reference C implementation through its
Python binding (``lz4.block``) in ``verification/tests/test_lz4_decoder.py``.

An LZ4 block is a sequence of **sequences**. Each sequence is::

    [token] [literal-length extra bytes] [literals] [offset (2B LE)] [match-length extra]

* ``token`` is ONE byte, split into two 4-bit fields: the HIGH nibble is the literal
  length, the LOW nibble is the match length.
* A nibble value of **15** means "read more": consume additional bytes, **summing** each
  into the length, and keep going while the byte read is ``255``. (A byte != 255 ends the
  continuation, and is still added.)
* ``literals`` are ``literal_length`` bytes copied verbatim to the output.
* ``offset`` is a **2-byte LITTLE-ENDIAN** value (byte 1 = low, byte 2 = high). It is a
  *backward distance* from the current output position. ``offset == 0`` is INVALID.
* the match copies ``match_length + 4`` bytes (the low nibble encodes the match length
  MINUS the 4-byte MINMATCH; a low nibble of 0 is a 4-byte match) from
  ``output[pos - offset]``. When ``match_length + 4 > offset`` the copy OVERLAPS: the
  later source bytes do not exist yet and are produced by the copy itself, so the copy
  MUST be byte-by-byte, never a block move. ``offset == 1`` is the degenerate case — a
  run of one repeated byte.

End-of-block rules (all three are properties of a *well-formed* block; the decoder
observes rather than enforces them):

1. the last sequence contains ONLY literals — the block ends right after them,
2. the last 5 bytes of input are always literals,
3. the last match starts at least 12 bytes before the end of the block.

This module is the independent GOLDEN the Kyttar block reference is gated exact against.
It is deliberately written as the plain, un-optimised transcription of the spec — the
on-chip block is what has to be clever.
"""
from __future__ import annotations

from typing import List, Tuple

#: The LZ4 minimum match length. A match-length nibble of 0 encodes a 4-byte match.
MINMATCH = 4

#: The value of a 4-bit length nibble that means "read continuation bytes".
NIBBLE_ESCAPE = 15

#: A continuation byte equal to this means "another continuation byte follows".
CONT_ESCAPE = 255


class LZ4FormatError(ValueError):
    """The compressed block violates the LZ4 block format."""


def _read_extra_length(src: bytes, pos: int) -> Tuple[int, int]:
    """Read a 15-nibble continuation run starting at ``pos``.

    Returns ``(extra, new_pos)``. Every byte read is ADDED to ``extra``; the run
    continues while the byte read is exactly 255. Note the terminating (non-255) byte
    is itself summed in — this is the classic transcription trap.
    """
    extra = 0
    while True:
        if pos >= len(src):
            raise LZ4FormatError("truncated length continuation")
        b = src[pos]
        pos += 1
        extra += b
        if b != CONT_ESCAPE:
            return extra, pos


def lz4_decompress_block(src) -> bytes:
    """GOLDEN: decode one LZ4 **block** (no frame header, no checksum) to raw bytes.

    Pure transcription of the published block format. Raises :class:`LZ4FormatError`
    on a malformed block (truncated fields, ``offset == 0``, an offset that reaches
    before the start of the output).
    """
    src = bytes(src)
    out = bytearray()
    pos = 0
    n = len(src)
    while pos < n:
        token = src[pos]
        pos += 1

        # --- literals -------------------------------------------------------
        lit_len = token >> 4
        if lit_len == NIBBLE_ESCAPE:
            extra, pos = _read_extra_length(src, pos)
            lit_len += extra
        if pos + lit_len > n:
            raise LZ4FormatError("truncated literals")
        out += src[pos:pos + lit_len]
        pos += lit_len

        # The last sequence is literals-only: the block ends right after them.
        if pos == n:
            break

        # --- match ----------------------------------------------------------
        if pos + 2 > n:
            raise LZ4FormatError("truncated offset")
        offset = src[pos] | (src[pos + 1] << 8)      # 2 bytes, LITTLE endian
        pos += 2
        if offset == 0:
            raise LZ4FormatError("offset 0 is invalid")
        if offset > len(out):
            raise LZ4FormatError("offset reaches before the start of the block")

        match_len = token & 0x0F
        if match_len == NIBBLE_ESCAPE:
            extra, pos = _read_extra_length(src, pos)
            match_len += extra
        match_len += MINMATCH                        # the +4 MINMATCH

        # BYTE-BY-BYTE copy: with match_len > offset the source overlaps the
        # destination and the later bytes are produced by the copy itself.
        start = len(out) - offset
        for i in range(match_len):
            out.append(out[start + i])
    return bytes(out)


# --------------------------------------------------------------------------- encoder
# A minimal, format-legal compressor, used ONLY to MANUFACTURE test blocks (the gate
# decodes them and compares against the reference C decoder). It is not the golden for
# LZ4EncoderBlock and makes no claim to LZ4 compression ratios.

def _emit_length(out: bytearray, value: int) -> int:
    """Append the continuation bytes for ``value`` and return the nibble to store."""
    if value < NIBBLE_ESCAPE:
        return value
    rest = value - NIBBLE_ESCAPE
    while rest >= CONT_ESCAPE:
        out.append(CONT_ESCAPE)
        rest -= CONT_ESCAPE
    out.append(rest)
    return NIBBLE_ESCAPE


def make_sequence(literals: bytes, offset: int = 0, match_len: int = 0) -> bytes:
    """Build ONE format-legal LZ4 sequence.

    ``offset == 0`` makes it a literals-only (final) sequence. Otherwise ``match_len``
    is the DECODED match length in bytes and must be >= :data:`MINMATCH`.
    """
    literals = bytes(literals)
    body = bytearray()
    lit_nib = _emit_length(body, len(literals))
    body += literals
    if offset == 0:
        if match_len:
            raise ValueError("a literals-only sequence cannot carry a match")
        return bytes([lit_nib << 4]) + bytes(body)
    if match_len < MINMATCH:
        raise ValueError(f"match_len {match_len} < MINMATCH {MINMATCH}")
    body.append(offset & 0xFF)                       # little endian
    body.append((offset >> 8) & 0xFF)
    tail = bytearray()
    match_nib = _emit_length(tail, match_len - MINMATCH)
    return bytes([(lit_nib << 4) | match_nib]) + bytes(body) + bytes(tail)


def lz4_compress_block(data, window: int = 65535) -> bytes:
    """A simple format-legal LZ4 compressor (greedy longest match, honest end rules).

    Emits a block that a REFERENCE LZ4 decoder accepts: matches are at least
    :data:`MINMATCH` bytes, offsets are 1..``window``, the last 5 bytes are literals
    and the last match starts at least 12 bytes before the end.
    """
    data = bytes(data)
    n = len(data)
    out = bytearray()
    lit_start = 0
    i = 0
    # Rule 2/3: nothing may match into the last 5 bytes, and the last match must start
    # >= 12 bytes before the end.
    limit = n - 12
    while i < limit:
        best_len = 0
        best_off = 0
        lo = max(0, i - window)
        for j in range(i - 1, lo - 1, -1):
            k = 0
            # Matches may overlap forward but must not read the final 5 literals.
            while (i + k < n - 5) and data[j + k] == data[i + k] and k < 65535:
                k += 1
            if k > best_len:
                best_len, best_off = k, i - j
                if k >= 64:
                    break
        if best_len >= MINMATCH:
            out += make_sequence(data[lit_start:i], best_off, best_len)
            i += best_len
            lit_start = i
        else:
            i += 1
    out += make_sequence(data[lit_start:])           # literals-only tail
    return bytes(out)


def sequences(src) -> List[dict]:
    """Parse a block into its sequence records — a debugging/inspection aid.

    Each record: ``{'token', 'lit_len', 'literals', 'offset', 'match_len'}``. The final
    literals-only sequence has ``offset`` and ``match_len`` of ``None``.
    """
    src = bytes(src)
    recs: List[dict] = []
    pos = 0
    n = len(src)
    while pos < n:
        token = src[pos]
        pos += 1
        lit_len = token >> 4
        if lit_len == NIBBLE_ESCAPE:
            extra, pos = _read_extra_length(src, pos)
            lit_len += extra
        lits = src[pos:pos + lit_len]
        pos += lit_len
        if pos == n:
            recs.append({"token": token, "lit_len": lit_len, "literals": lits,
                         "offset": None, "match_len": None})
            break
        offset = src[pos] | (src[pos + 1] << 8)
        pos += 2
        match_len = token & 0x0F
        if match_len == NIBBLE_ESCAPE:
            extra, pos = _read_extra_length(src, pos)
            match_len += extra
        match_len += MINMATCH
        recs.append({"token": token, "lit_len": lit_len, "literals": lits,
                     "offset": offset, "match_len": match_len})
    return recs
