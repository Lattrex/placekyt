# SPDX-License-Identifier: GPL-3.0-or-later
"""
Kyttar ChaCha20 keystream GRC marker — placeKYT ``ChaCha20KeystreamBlock``.

The ChaCha20 block function (RFC 8439 §2.3): twenty rounds over the 16-word
state, then the original state added back mod 2**32. One trigger word in, one
64-byte keystream block out — the sixteen state words in §2.3.2 order, each as
a hi/lo pair of raw 16-bit words. No stock GNU Radio counterpart, so this is a
placeKYT-native ([Kyttar]) block — still fully placeable in GRC.

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact interface (class name, params, ports) so it places/wires identically in
GRC, but does NO in-process placement and streams pure pass-through.
"""

import numpy as np

from .dsp_markers import _PassThrough


def _to_bytes(value, n: int, what: str) -> bytes:
    """Coerce a GRC param — a hex string or a bytes-like — to exactly n bytes."""
    if isinstance(value, (bytes, bytearray)):
        b = bytes(value)
    else:
        b = bytes.fromhex(str(value).strip())
    if len(b) != n:
        raise ValueError(f"a ChaCha20 {what} is {n} bytes; got {len(b)}")
    return b


class chacha20_keystream(_PassThrough):
    """
    ChaCha20 block function — placeKYT ``ChaCha20KeystreamBlock``
    (no GR counterpart).

    Parameters (mirror ``ChaCha20KeystreamBlock`` VERBATIM):
        device_id: Device ID to register with.
        key:       the 256-bit key — 32 bytes, given as 64 hex digits (or a
                   bytes object), parsed little-endian per RFC 8439 §2.3.
        nonce:     the 96-bit nonce — 12 bytes / 24 hex digits.
        counter:   the 32-bit initial block counter.
        counter_mode: "fixed" (default — every trigger recomputes block
                   ``counter``) or "increment" (the counter persists across
                   batches and advances by one per batch, so consecutive
                   triggers emit CONSECUTIVE keystream blocks — RFC 8439
                   §2.4's consumption, what multi-block encryption needs).

    Input:  trigger words — one keystream block per trigger.
    Output: raw 16-bit words, 32 per trigger (the sixteen §2.3.2 state words
            as hi/lo pairs, in §2.3.2 order).

    These are EXACT 32-bit integers carried as hi/lo halves, not Q15 samples:
    read the stream raw.
    """

    def __init__(self, device_id: str = "kyttar_0",
                 key="000102030405060708090a0b0c0d0e0f"
                     "101112131415161718191a1b1c1d1e1f",
                 nonce="000000090000004a00000000",
                 counter: int = 1,
                 counter_mode: str = "fixed"):
        super().__init__(name="Kyttar ChaCha20 Keystream", n_in=1, n_out=1,
                         in_dtype=np.int16, out_dtype=np.int16)
        if counter_mode not in ("fixed", "increment"):
            raise ValueError(
                f"counter_mode is 'fixed' or 'increment'; got {counter_mode!r}")
        self._device_id = device_id
        self._key = _to_bytes(key, 32, "key")
        self._nonce = _to_bytes(nonce, 12, "nonce")
        self._counter = int(counter) & 0xFFFFFFFF
        self._counter_mode = counter_mode
        self._advertise_grc_params(
            device_id, "ChaCha20KeystreamBlock",
            {"key": self._key, "nonce": self._nonce,
             "counter": self._counter, "counter_mode": counter_mode})

    @property
    def block_words(self) -> int:
        """Output words per trigger (16 state words as hi/lo pairs)."""
        return 32

    @property
    def cell_count(self) -> int:
        """Number of cells used."""
        return 51
