# SPDX-License-Identifier: GPL-3.0-or-later
"""GOLDEN model of the PSK31 Varicode encoder (there is NO stock GNU Radio block).

Spec: PSK31 Varicode (G3PLX / Peter Martinez). Canonical 128-entry ASCII->code table,
transcribed EXACTLY from the fldigi ``src/psk/pskvaricode.cxx`` table (via the
``ckoval7/pydigi`` reimplementation ``pydigi/varicode/psk_varicode.py``) and
cross-checked against the ARRL PSK31 spec (http://www.arrl.org/psk31-spec) and the
Wikipedia "Varicode" article. Each character's code:

  * starts AND ends with '1',
  * contains NO two consecutive '0's,
  * characters are separated by a '00' gap.

The encoder output for a byte stream is: for each byte, its code bits (MSB..LSB as
written, one bit per output word) followed by '00'. This module is the independent
GOLDEN the DUT / block reference is gated bit-exact against.
"""
from __future__ import annotations

from typing import List

# The canonical 128-entry PSK31 Varicode table (ASCII 0..127). DO NOT reorder.
GOLDEN_VARICODE: List[str] = [
    "1010101011", "1011011011", "1011101101", "1101110111", "1011101011",  # 0-4
    "1101011111", "1011101111", "1011111101", "1011111111", "11101111",    # 5-9
    "11101", "1101101111", "1011011101", "11111", "1101110101",            # 10-14
    "1110101011", "1011110111", "1011110101", "1110101101", "1110101111",  # 15-19
    "1101011011", "1101101011", "1101101101", "1101010111", "1101111011",  # 20-24
    "1101111101", "1110110111", "1101010101", "1101011101", "1110111011",  # 25-29
    "1011111011", "1101111111", "1", "111111111", "101011111",             # 30-34
    "111110101", "111011011", "1011010101", "1010111011", "101111111",     # 35-39
    "11111011", "11110111", "101101111", "111011111", "1110101",           # 40-44
    "110101", "1010111", "110101111", "10110111", "10111101",              # 45-49
    "11101101", "11111111", "101110111", "101011011", "101101011",         # 50-54
    "110101101", "110101011", "110110111", "11110101", "110111101",        # 55-59
    "111101101", "1010101", "111010111", "1010101111", "1010111101",       # 60-64
    "1111101", "11101011", "10101101", "10110101", "1110111",              # 65-69
    "11011011", "11111101", "101010101", "1111111", "111111101",           # 70-74
    "101111101", "11010111", "10111011", "11011101", "10101011",           # 75-79
    "11010101", "111011101", "10101111", "1101111", "1101101",             # 80-84
    "101010111", "110110101", "101011101", "101110101", "101111011",       # 85-89
    "1010101101", "111110111", "111101111", "111111011", "1010111111",     # 90-94
    "101101101", "1011011111", "1011", "1011111", "101111",                # 95-99
    "101101", "11", "111101", "1011011", "101011",                         # 100-104
    "1101", "111101011", "10111111", "11011", "111011",                    # 105-109
    "1111", "111", "111111", "110111111", "10101",                         # 110-114
    "10111", "101", "110111", "1111011", "1101011",                        # 115-119
    "11011111", "1011101", "111010101", "1010110111", "110111011",         # 120-124
    "1010110101", "1011010111", "1110110101",                              # 125-127
]


def golden_bits(text_bytes) -> List[int]:
    """The GOLDEN Varicode bit stream (list of 0/1 ints) for a byte sequence.

    For each byte: its ``GOLDEN_VARICODE`` code bits, one int per bit, then ``0,0``.
    """
    out: List[int] = []
    for b in text_bytes:
        for ch in GOLDEN_VARICODE[int(b) & 0x7F]:
            out.append(1 if ch == "1" else 0)
        out.append(0)
        out.append(0)
    return out


def golden_bits_str(text: str) -> str:
    """Convenience: the golden bit stream for an ASCII string, as a '0'/'1' string."""
    return "".join(str(b) for b in golden_bits([ord(c) for c in text]))
