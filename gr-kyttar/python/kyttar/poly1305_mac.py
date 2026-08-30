# SPDX-License-Identifier: GPL-3.0-or-later
"""
Kyttar Poly1305 MAC GRC marker — placeKYT ``Poly1305MACBlock``.

The Poly1305 one-time authenticator (RFC 8439 §2.5): the message, consumed as
raw little-endian 16-bit words, is authenticated under the one-time key
``(r, s)`` and the 16-byte tag emitted as 8 little-endian words after exactly
``msg_words`` inputs. No stock GNU Radio counterpart, so this is a
placeKYT-native ([Kyttar]) block — still fully placeable in GRC.

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact interface (class name, params, ports) so it places/wires identically in
GRC, but does NO in-process placement and streams pure pass-through.
"""

import numpy as np

from .dsp_markers import _PassThrough


def _hex16(value, what: str) -> str:
    """Coerce a GRC param — hex string or bytes-like — to 16 bytes of hex."""
    if isinstance(value, (bytes, bytearray)):
        b = bytes(value)
    else:
        b = bytes.fromhex(str(value).strip())
    if len(b) != 16:
        raise ValueError(f"a Poly1305 {what} half is 16 bytes; got {len(b)}")
    return b.hex()


class poly1305_mac(_PassThrough):
    """
    Poly1305 one-time authenticator — placeKYT ``Poly1305MACBlock``
    (no GR counterpart).

    Parameters (mirror ``Poly1305MACBlock`` VERBATIM):
        device_id: Device ID to register with.
        r_key:     the polynomial key half — 16 bytes as 32 hex digits,
                   little-endian per RFC 8439 §2.5.2; clamped on the way in.
        s_key:     the additive blind half — 16 bytes / 32 hex digits.
        msg_words: the message length in 16-bit little-endian words (>= 1).

    Input:  the message as raw 16-bit words (NOT Q15).
    Output: after word ``msg_words``, the 16-byte tag as 8 raw LE words.
    """

    def __init__(self, device_id: str = "kyttar_0",
                 r_key="85d6be7857556d337f4452fe42d506a8",
                 s_key="0103808afb0db2fd4abff6af4149f51b",
                 msg_words: int = 17):
        super().__init__(name="Kyttar Poly1305 MAC", n_in=1, n_out=1,
                         in_dtype=np.int16, out_dtype=np.int16)
        self._device_id = device_id
        self._r_key = _hex16(r_key, "r")
        self._s_key = _hex16(s_key, "s")
        self._msg_words = int(msg_words)
        if self._msg_words < 1:
            raise ValueError("msg_words must be >= 1")
        self._advertise_grc_params(
            device_id, "Poly1305MACBlock",
            {"r_key": self._r_key, "s_key": self._s_key,
             "msg_words": self._msg_words})

    @property
    def tag_words(self) -> int:
        """Output words in the emitted tag (16 bytes as 8 LE words)."""
        return 8
