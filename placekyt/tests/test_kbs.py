"""Tests for the .kbs bitstream container (engine/io/kbs.py, §5.1)."""

from __future__ import annotations

import struct

import pytest

from engine.io.kbs import (
    MAGIC,
    MAX_CHIPS,
    MAX_METADATA_BYTES,
    MAX_WORDS_PER_CHIP,
    Kbs,
    KbsChip,
    KbsError,
    chip_type_hash,
    dumps_kbs,
    loads_kbs,
)


def _sample() -> Kbs:
    return Kbs(
        chips=[
            KbsChip(chip_type_hash("kyttar_10x12"), [0x67C1, 0x0001, 0xFFFF, 0x8000]),
            KbsChip(chip_type_hash("kyttar_10x12"), [0x1234]),
        ],
        metadata={"project_name": "test", "blocks": ["agc", "dfe"]},
    )


class TestRoundTrip:
    def test_basic(self):
        k = _sample()
        blob = dumps_kbs(k)
        assert blob[:8] == MAGIC
        k2 = loads_kbs(blob)
        assert len(k2.chips) == 2
        assert k2.chips[0].words == [0x67C1, 0x0001, 0xFFFF, 0x8000]
        assert k2.chips[1].words == [0x1234]
        assert k2.metadata == {"project_name": "test", "blocks": ["agc", "dfe"]}

    def test_idempotent(self):
        blob = dumps_kbs(_sample())
        assert dumps_kbs(loads_kbs(blob)) == blob

    def test_no_metadata(self):
        k = Kbs(chips=[KbsChip(0, [0x1])])
        k2 = loads_kbs(dumps_kbs(k))
        assert k2.metadata is None

    def test_empty_chip_words(self):
        k = Kbs(chips=[KbsChip(0, [])])
        k2 = loads_kbs(dumps_kbs(k))
        assert k2.chips[0].words == []

    def test_chip_type_hash_stable(self):
        assert chip_type_hash("kyttar_10x12") == chip_type_hash("kyttar_10x12")
        assert chip_type_hash("a") != chip_type_hash("b")


class TestHardenedReader:
    def test_bad_magic(self):
        blob = dumps_kbs(_sample())
        with pytest.raises(KbsError, match="bad magic"):
            loads_kbs(b"XXXXXXXX" + blob[8:])

    def test_empty(self):
        with pytest.raises(KbsError):
            loads_kbs(b"")

    def test_truncated_header(self):
        with pytest.raises(KbsError):
            loads_kbs(MAGIC + b"\x01\x00")  # cut off mid-header

    def test_truncated_body(self):
        blob = dumps_kbs(_sample())
        with pytest.raises(KbsError, match="truncated"):
            loads_kbs(blob[:20])

    def test_crc_mismatch(self):
        blob = bytearray(dumps_kbs(_sample()))
        blob[-6] ^= 0xFF  # flip a word byte in the last chip
        with pytest.raises(KbsError, match="CRC mismatch"):
            loads_kbs(bytes(blob))

    def test_newer_version_rejected(self):
        blob = bytearray(dumps_kbs(_sample()))
        struct.pack_into("<H", blob, 8, 99)  # version field at offset 8
        with pytest.raises(KbsError, match="newer than supported"):
            loads_kbs(bytes(blob))

    def test_chip_count_over_cap(self):
        # Forge a header claiming too many chips (before any allocation).
        forged = MAGIC + struct.pack("<HHI", 1, MAX_CHIPS + 1, 0) + struct.pack("<I", 0)
        with pytest.raises(KbsError, match="chip_count"):
            loads_kbs(forged)

    def test_word_count_over_cap(self):
        # Header: 1 chip, no metadata; chip declares an absurd word_count.
        forged = (
            MAGIC
            + struct.pack("<HHI", 1, 1, 0)
            + struct.pack("<I", 0)  # metadata length
            + struct.pack("<II", 0, MAX_WORDS_PER_CHIP + 1)  # type hash, word_count
        )
        with pytest.raises(KbsError, match="word_count"):
            loads_kbs(forged)

    def test_metadata_over_cap(self):
        forged = (
            MAGIC
            + struct.pack("<HHI", 1, 0, 0)
            + struct.pack("<I", MAX_METADATA_BYTES + 1)
        )
        with pytest.raises(KbsError, match="metadata_length"):
            loads_kbs(forged)


class TestWriterCaps:
    def test_too_many_chips(self):
        k = Kbs(chips=[KbsChip(0, [0]) for _ in range(MAX_CHIPS + 1)])
        with pytest.raises(KbsError, match="too many chips"):
            dumps_kbs(k)

    def test_too_many_words(self):
        k = Kbs(chips=[KbsChip(0, [0] * (MAX_WORDS_PER_CHIP + 1))])
        with pytest.raises(KbsError, match="words"):
            dumps_kbs(k)
