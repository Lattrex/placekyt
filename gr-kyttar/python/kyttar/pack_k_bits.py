# SPDX-License-Identifier: GPL-3.0-or-later
"""
Kyttar Pack K Bits GRC Block — GNU Radio ``blocks.pack_k_bits_bb``.

Consumes ``k`` input bytes (each carrying one bit in its LSB) and packs them
MSB-first (GR's fixed convention) into one output byte. Rate-reducing (k in ->
1 out); a trailing partial group of < k bits is dropped, and only the LOW bit of
each input item is used. Verified BIT-EXACT vs ``blocks.pack_k_bits_bb`` on the
placeKYT-hosted chip.

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the exact
GR interface (class name, params, ports) so it places/wires identically in GRC, but
does NO in-process placement and streams pure pass-through.
"""

import numpy as np

from .dsp_markers import _PassThrough


class pack_k_bits(_PassThrough):
    """
    Pack K Bits — GNU Radio ``blocks.pack_k_bits_bb``.

    Parameters (mirror GR ``pack_k_bits_bb`` VERBATIM):
        device_id: Device ID to register with.
        k: number of input bits packed into each output byte (MSB-first). 1..8.

    Input: Data bits (0/1 as float, one bit per item).
    Output: Packed byte (one per k input bits).
    """

    def __init__(self, device_id: str = "kyttar_0", k: int = 8):
        # blocks.pack_k_bits_bb is a BYTE block (uint8 in/out) — declare the byte itemsize
        # so GRC stream connections match (a _bb block, not _ff).
        super().__init__(name="Kyttar Pack K Bits", n_in=1, n_out=1,
                         in_dtype=np.uint8, out_dtype=np.uint8)
        self._device_id = device_id
        self._k = int(k)
        # Advertise params for GRC↔placeKYT sync detection (see dsp_markers). Name
        # matches PackKBitsBlock's constructor kwarg verbatim.
        self._advertise_grc_params(
            device_id, "PackKBitsBlock", {"k": self._k})

    @property
    def k(self) -> int:
        return self._k

    @property
    def cell_count(self) -> int:
        """Number of cells used."""
        return 1
