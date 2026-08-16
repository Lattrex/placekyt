# SPDX-License-Identifier: GPL-3.0-or-later
"""
Kyttar Additive LFSR Scrambler GRC Block — GNU Radio ``digital.additive_scrambler_bb``.

Additive scrambler: XORs the input bit stream with the free-running output of a
Fibonacci LFSR defined by ``mask`` (polynomial), ``seed`` (initial register) and
``len`` (register length), with an optional ``count`` fixed-vector reseed. Because
the LFSR runs independently of the data, the block is deterministic and self-inverse
— an identically-configured block descrambles. Verified BIT-EXACT vs
``digital.additive_scrambler_bb`` on the placeKYT-hosted chip.

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the exact
GR interface (class name, params, ports) so it places/wires identically in GRC, but
does NO in-process placement and streams pure pass-through.
"""

import numpy as np

from .dsp_markers import _PassThrough


class lfsr_scrambler(_PassThrough):
    """
    Additive LFSR Scrambler — GNU Radio ``digital.additive_scrambler_bb``.

    Parameters (mirror GR ``additive_scrambler_bb`` VERBATIM):
        device_id: Device ID to register with.
        mask: polynomial mask (feedback tap positions).
        seed: initial shift-register contents.
        len: shift-register length (feedback bit position). Kyttar: <= 15.
        count: reset the register to ``seed`` after this many items (0 = never).
        bits_per_byte: Kyttar requires 1 (bit-serial fabric).

    Input: Data bits (0/1 as float).
    Output: Scrambled/descrambled bits (0/1 as float).
    """

    def __init__(
        self,
        device_id: str = "kyttar_0",
        mask: int = 0x8A,
        seed: int = 0x7F,
        len: int = 7,
        count: int = 0,
        bits_per_byte: int = 1,
    ):
        # digital.additive_scrambler_bb is a BYTE block (uint8 in/out) — declare the byte
        # itemsize so GRC stream connections match (a _bb block, not _ff).
        super().__init__(name="Kyttar LFSR Scrambler", n_in=1, n_out=1,
                         in_dtype=np.uint8, out_dtype=np.uint8)
        self._device_id = device_id
        self._mask = int(mask)
        self._seed = int(seed)
        self._len = int(len)
        self._count = int(count)
        self._bits_per_byte = int(bits_per_byte)
        # Advertise params for GRC↔placeKYT sync detection (see dsp_markers). Names
        # match LFSRScramblerBlock's constructor kwargs verbatim.
        self._advertise_grc_params(
            device_id, "LFSRScramblerBlock",
            {"mask": self._mask, "seed": self._seed, "len": self._len,
             "count": self._count, "bits_per_byte": self._bits_per_byte})

    @property
    def mask(self) -> int:
        return self._mask

    @property
    def seed(self) -> int:
        return self._seed

    @property
    def len(self) -> int:
        return self._len

    @property
    def count(self) -> int:
        return self._count

    @property
    def bits_per_byte(self) -> int:
        return self._bits_per_byte

    @property
    def cell_count(self) -> int:
        """Number of cells used."""
        return 1
