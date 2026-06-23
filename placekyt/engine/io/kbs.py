"""``.kbs`` bitstream container — writer and hardened reader (the architecture notes §5.1).

Binary layout::

    Header (16 bytes):
      Magic           "KYTBS\\x00\\x00\\x00"  (8 bytes)
      Format version  uint16  (currently 1)
      Chip count      uint16
      Reserved        uint32

    Metadata section:
      Metadata length uint32  (byte count of JSON payload; 0 = none)
      Metadata JSON   UTF-8

    Per-chip section (repeated chip_count times):
      Chip type hash  uint32  (CRC32 of chip type name)
      Word count      uint32
      Bitstream       uint16[word_count]  (little-endian)
      Checksum        uint32  (CRC32 of the bitstream words, little-endian bytes)

All integers little-endian. The reader is hardened against malicious/corrupt
files: magic is checked first, every length is bounds-checked against a hard
cap AND the remaining file size BEFORE allocation, and CRC32 is treated as a
corruption check only (NOT cryptographic authenticity).
"""

from __future__ import annotations

import json
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ProjectFileError

MAGIC = b"KYTBS\x00\x00\x00"
FORMAT_VERSION = 1

# Hard caps applied BEFORE any allocation (§5.1 parser safety).
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_METADATA_BYTES = 64 * 1024
MAX_WORDS_PER_CHIP = 8192  # a full chip is ~4500 words; 2x headroom
MAX_CHIPS = 32


class KbsError(ProjectFileError):
    """Malformed or corrupt ``.kbs`` file (CLI exit code 3, §11.4)."""


@dataclass
class KbsChip:
    """One chip's bitstream within a ``.kbs`` container."""

    chip_type_hash: int
    words: list[int] = field(default_factory=list)


@dataclass
class Kbs:
    """A parsed/constructed ``.kbs`` container."""

    chips: list[KbsChip] = field(default_factory=list)
    metadata: dict | None = None
    format_version: int = FORMAT_VERSION


def chip_type_hash(chip_type_name: str) -> int:
    """CRC32 of the chip-type name (the per-chip ``chip_type_hash`` field)."""
    return zlib.crc32(chip_type_name.encode("utf-8")) & 0xFFFFFFFF


# --------------------------------------------------------------------------- #
# Write
# --------------------------------------------------------------------------- #


def write_kbs(kbs: Kbs, path: str | Path) -> None:
    """Serialize ``kbs`` to ``path``."""
    Path(path).write_bytes(dumps_kbs(kbs))


def dumps_kbs(kbs: Kbs) -> bytes:
    """Serialize ``kbs`` to bytes."""
    if len(kbs.chips) > MAX_CHIPS:
        raise KbsError(f"too many chips ({len(kbs.chips)} > {MAX_CHIPS}).")

    out = bytearray()
    out += MAGIC
    out += struct.pack("<HHI", kbs.format_version, len(kbs.chips), 0)

    if kbs.metadata:
        meta_bytes = json.dumps(kbs.metadata).encode("utf-8")
        if len(meta_bytes) > MAX_METADATA_BYTES:
            raise KbsError(
                f"metadata is {len(meta_bytes)} bytes, over {MAX_METADATA_BYTES}."
            )
    else:
        meta_bytes = b""
    out += struct.pack("<I", len(meta_bytes))
    out += meta_bytes

    for chip in kbs.chips:
        if len(chip.words) > MAX_WORDS_PER_CHIP:
            raise KbsError(
                f"chip has {len(chip.words)} words, over {MAX_WORDS_PER_CHIP}."
            )
        word_bytes = struct.pack(f"<{len(chip.words)}H", *chip.words)
        out += struct.pack("<II", chip.chip_type_hash, len(chip.words))
        out += word_bytes
        out += struct.pack("<I", zlib.crc32(word_bytes) & 0xFFFFFFFF)

    return bytes(out)


# --------------------------------------------------------------------------- #
# Read (hardened)
# --------------------------------------------------------------------------- #


def read_kbs(path: str | Path) -> Kbs:
    """Read and validate a ``.kbs`` file."""
    p = Path(path)
    try:
        size = p.stat().st_size
    except OSError as exc:
        raise KbsError(f"cannot stat {p}: {exc}") from exc
    if size > MAX_FILE_BYTES:
        raise KbsError(f"{p} is {size} bytes, over the {MAX_FILE_BYTES}-byte limit.")
    try:
        data = p.read_bytes()
    except OSError as exc:
        raise KbsError(f"cannot read {p}: {exc}") from exc
    return loads_kbs(data, source=str(p))


def loads_kbs(data: bytes, *, source: str = "<bytes>") -> Kbs:
    """Parse ``.kbs`` bytes with all §5.1 hardening checks."""
    if len(data) > MAX_FILE_BYTES:
        raise KbsError(f"{source}: {len(data)} bytes, over the file limit.")

    r = _Reader(data, source)

    # Magic FIRST, before reading any length field.
    if r.take(8) != MAGIC:
        raise KbsError(f"{source}: bad magic (not a .kbs file).")

    version, chip_count, _reserved = struct.unpack("<HHI", r.take(8))
    if version > FORMAT_VERSION:
        raise KbsError(
            f"{source}: format version {version} newer than supported "
            f"({FORMAT_VERSION})."
        )
    if chip_count > MAX_CHIPS:
        raise KbsError(f"{source}: chip_count {chip_count} over {MAX_CHIPS}.")

    (meta_len,) = struct.unpack("<I", r.take(4))
    if meta_len > MAX_METADATA_BYTES:
        raise KbsError(f"{source}: metadata_length {meta_len} over cap.")
    r.ensure(meta_len)
    metadata = None
    if meta_len:
        try:
            metadata = json.loads(r.take(meta_len).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise KbsError(f"{source}: invalid metadata JSON: {exc}") from exc

    chips: list[KbsChip] = []
    for i in range(chip_count):
        type_hash, word_count = struct.unpack("<II", r.take(8))
        if word_count > MAX_WORDS_PER_CHIP:
            raise KbsError(
                f"{source}: chip {i} word_count {word_count} over "
                f"{MAX_WORDS_PER_CHIP}."
            )
        byte_count = word_count * 2
        r.ensure(byte_count + 4)  # words + trailing CRC
        word_bytes = r.take(byte_count)
        (stored_crc,) = struct.unpack("<I", r.take(4))
        actual_crc = zlib.crc32(word_bytes) & 0xFFFFFFFF
        if stored_crc != actual_crc:
            raise KbsError(
                f"{source}: chip {i} CRC mismatch "
                f"(stored {stored_crc:#010x}, computed {actual_crc:#010x}) "
                "— file is corrupt."
            )
        words = list(struct.unpack(f"<{word_count}H", word_bytes))
        chips.append(KbsChip(chip_type_hash=type_hash, words=words))

    return Kbs(chips=chips, metadata=metadata, format_version=version)


# --------------------------------------------------------------------------- #
# Stimulus convenience (a stimulus is a single-chip .kbs of raw burst words)
# --------------------------------------------------------------------------- #

# Marks a .kbs whose words are an input-port STIMULUS bitstream (a sequence of
# self-contained WRITE+DATA+JUMP bursts) rather than a chip PROGRAM. The chip
# type hash is 0 (a stimulus is not tied to a placed chip's program layout).
STIMULUS_KIND = "stimulus"


def write_stimulus_kbs(words: list[int], path: str | Path,
                       *, name: str | None = None) -> None:
    """Write a stimulus bitstream (raw 16-bit WRITE/DATA/JUMP words) as a
    single-chip ``.kbs`` tagged as a stimulus (§stimulus)."""
    meta = {"kind": STIMULUS_KIND}
    if name:
        meta["name"] = name
    write_kbs(
        Kbs(chips=[KbsChip(chip_type_hash=0, words=[w & 0xFFFF for w in words])],
            metadata=meta),
        path)


def read_stimulus_kbs(path: str | Path) -> list[int]:
    """Read a stimulus ``.kbs`` and return its raw words. Accepts any single-chip
    ``.kbs`` (the ``stimulus`` metadata tag is informational, not required)."""
    kbs = read_kbs(path)
    if not kbs.chips:
        raise KbsError(f"{path}: stimulus .kbs has no bitstream.")
    return list(kbs.chips[0].words)


# A .kbs whose words are a captured-OUTPUT / GOLDEN bitstream (WRITE(dest)+DATA
# words, preserving the WRITE-descriptor tag) rather than a chip program or an
# input stimulus. See engine/io/output_bitstream.py.
GOLDEN_KIND = "golden"


def write_golden_kbs(words: list[int], path: str | Path,
                     *, name: str | None = None) -> None:
    """Write an output/golden bitstream (raw 16-bit WRITE(dest)+DATA / JUMP words
    from ``output_bitstream.encode_output``) as a single-chip ``.kbs`` tagged as
    golden output (§185)."""
    meta = {"kind": GOLDEN_KIND}
    if name:
        meta["name"] = name
    write_kbs(
        Kbs(chips=[KbsChip(chip_type_hash=0, words=[w & 0xFFFF for w in words])],
            metadata=meta),
        path)


def read_golden_kbs(path: str | Path) -> list[int]:
    """Read a golden ``.kbs`` and return its raw words."""
    kbs = read_kbs(path)
    if not kbs.chips:
        raise KbsError(f"{path}: golden .kbs has no bitstream.")
    return list(kbs.chips[0].words)


class _Reader:
    """Bounds-checked sequential byte reader — never reads past the buffer."""

    def __init__(self, data: bytes, source: str):
        self._data = data
        self._pos = 0
        self._source = source

    def ensure(self, n: int) -> None:
        if n < 0 or self._pos + n > len(self._data):
            raise KbsError(
                f"{self._source}: truncated file (needed {n} bytes at offset "
                f"{self._pos}, only {len(self._data) - self._pos} remain)."
            )

    def take(self, n: int) -> bytes:
        self.ensure(n)
        chunk = self._data[self._pos : self._pos + n]
        self._pos += n
        return chunk
