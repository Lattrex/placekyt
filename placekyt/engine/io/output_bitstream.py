"""Captured-output + golden as a ``.kbs`` BITSTREAM, with the WRITE descriptor
preserved as a generic tag (#185).

The old CSV output was lossy: it kept only the DATA value and threw away the
WRITE's destination — which is exactly the field that distinguishes one virtual
"channel" of an output stream from another (e.g. I vs Q routed by WRITE dest, or
any user-chosen tagging). Captured output is now a real bitstream of
``WRITE(dest) + DATA`` words (JUMP triggers as ``JUMP`` words), so the (hop,
dest) descriptor survives and the user can select/group output by it — a virtual
channel selector.

NOTE (#185 scope): the chip output port currently captures the WRITE's DEST but
not its HOP (hop is uniform at the output boundary), so the tag here is
dest-only; the ``hop`` slot is carried as 0 and can be wired through later.
"""

from __future__ import annotations

from dataclasses import dataclass

_OP_WRITE = 0x6
_OP_JUMP = 0x7


@dataclass(frozen=True)
class OutWord:
    """One captured output word: a WRITE carrying ``value`` tagged by ``dest``
    (its virtual channel), or a JUMP trigger carrying ``entry``."""

    is_jump: bool
    value: int          # data value (WRITE) — 0 for a JUMP
    dest: int           # WRITE dest (the tag) / JUMP entry
    hop: int = 0        # reserved (not captured at the output boundary yet)

    @property
    def tag(self) -> tuple[int, int]:
        """The generic ``(hop, dest)`` selector tag."""
        return (self.hop, self.dest)


def _wr(hop: int, dest: int) -> int:
    return (_OP_WRITE << 12) | ((hop & 0x1F) << 5) | (dest & 0x1F)


def _jp(hop: int, entry: int) -> int:
    return (_OP_JUMP << 12) | ((hop & 0x1F) << 5) | (entry & 0x1F)


def encode_output(words: list[OutWord]) -> list[int]:
    """Encode captured output words into a raw 16-bit bitstream: each WRITE
    becomes ``WRITE(dest) + DATA``; each JUMP becomes a single ``JUMP`` word."""
    out: list[int] = []
    for w in words:
        if w.is_jump:
            out.append(_jp(w.hop, w.dest))
        else:
            out.append(_wr(w.hop, w.dest))
            out.append(w.value & 0xFFFF)
    return out


def decode_output(stream: list[int]) -> list[OutWord]:
    """Decode a bitstream back into tagged output words (the inverse of
    :func:`encode_output`). A WRITE consumes the following DATA word; a JUMP
    stands alone; a bare data word (no preceding WRITE) is treated as an
    untagged value (dest 0)."""
    out: list[OutWord] = []
    i = 0
    n = len(stream)
    while i < n:
        w = stream[i] & 0xFFFF
        op = (w >> 12) & 0xF
        hop = (w >> 5) & 0x1F
        fld = w & 0x1F
        if op == _OP_WRITE:
            value = stream[i + 1] & 0xFFFF if i + 1 < n else 0
            out.append(OutWord(False, value, fld, hop))
            i += 2
        elif op == _OP_JUMP:
            out.append(OutWord(True, 0, fld, hop))
            i += 1
        else:
            out.append(OutWord(False, w, 0, 0))
            i += 1
    return out


@dataclass
class TaggedCompare:
    """Word-by-word golden compare result (value AND tag)."""

    passed: bool
    compared: int
    mismatches: int
    first_mismatch: int | None
    details: list[tuple]    # (index, exp, act) where each is a short repr


def compare_output(actual: list[OutWord], golden: list[OutWord], *,
                   tolerance: int = 0, max_details: int = 50) -> TaggedCompare:
    """Compare captured output to golden WORD-BY-WORD: each word must match in
    KIND (WRITE vs JUMP), TAG (hop, dest), and — for a WRITE — VALUE within
    ``tolerance``. A wrong tag is a mismatch (so virtual-channel routing
    correctness is enforced, not just the values)."""
    n = len(golden)
    mismatches = 0
    first: int | None = None
    details: list[tuple] = []
    for i in range(n):
        e = golden[i]
        a = actual[i] if i < len(actual) else None
        ok = False
        if a is not None and a.is_jump == e.is_jump and a.tag == e.tag:
            ok = e.is_jump or abs(_s16(a.value) - _s16(e.value)) <= tolerance
        if not ok:
            mismatches += 1
            if first is None:
                first = i
            if len(details) < max_details:
                details.append((i, _repr(e), _repr(a)))
    return TaggedCompare(
        passed=(mismatches == 0 and len(actual) >= n),
        compared=min(n, len(actual)),
        mismatches=mismatches,
        first_mismatch=first,
        details=details,
    )


def _s16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _repr(w: OutWord | None) -> str:
    if w is None:
        return "<missing>"
    if w.is_jump:
        return f"JUMP entry={w.dest}"
    return f"0x{w.value & 0xFFFF:04X}@dest{w.dest}"
