# SPDX-License-Identifier: GPL-3.0-or-later
"""
Kyttar keystream-serializer GRC marker — placeKYT ``KeystreamSerializerBlock``.

Serializes a hi/lo 16-bit half-word stream (the ``ChaCha20KeystreamBlock``
wire convention: each 32-bit state word as its hi half then its lo half) into
RFC 8439 §2.3 keystream BYTES — each 32-bit word emitted as its four bytes
LITTLE-ENDIAN, one byte per 16-bit word (the data-link convention). Rate 1:2 —
2 input words -> 4 output words. There is no stock GNU Radio counterpart, so
this is a placeKYT-native ([Kyttar]) block — still fully placeable in GRC.

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact interface (class name, params, ports) so it places/wires identically in
GRC, but does NO in-process placement and streams pure pass-through.
"""

import numpy as np

from .dsp_markers import _PassThrough


class keystream_serializer(_PassThrough):
    """
    Keystream serializer — placeKYT ``KeystreamSerializerBlock`` (no GR
    counterpart).

    Parameters (mirror ``KeystreamSerializerBlock`` VERBATIM — it takes none
    beyond the device):
        device_id: Device ID to register with.

    Input:  raw 16-bit words, the hi/lo halves of each 32-bit keystream word
            (hi first — the ChaCha20KeystreamBlock output convention).
    Output: raw 16-bit words, ONE keystream byte per word, in RFC 8439 §2.3
            serialization order (each 32-bit word's four bytes little-endian).

    These are raw integer words, not Q15 samples: read the stream raw.
    """

    def __init__(self, device_id: str = "kyttar_0"):
        # Raw 16-bit words in, raw 16-bit byte-valued words out.
        super().__init__(name="Kyttar Keystream Serializer", n_in=1, n_out=1,
                         in_dtype=np.int16, out_dtype=np.int16)
        self._device_id = device_id
        # Advertise params for GRC<->placeKYT sync detection (see dsp_markers).
        # KeystreamSerializerBlock's constructor takes no params beyond the name.
        self._advertise_grc_params(device_id, "KeystreamSerializerBlock", {})

    @property
    def rate(self) -> tuple:
        """Words in : words out (2 half-words -> 4 keystream bytes)."""
        return (1, 2)

    @property
    def cell_count(self) -> int:
        """Number of cells used."""
        return 1
