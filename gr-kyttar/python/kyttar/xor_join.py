"""
Kyttar XOR Join Block for GNURadio

GR front-end for the placeKYT XorJoinBlock — the bitwise XOR of two byte
(unsigned char) streams produced by TWO INDEPENDENT on-chip chains. The real DSP
runs on the placeKYT-hosted chip; this marker keeps the exact GR interface (two
byte inputs, one byte output) and computes the result in work() for a faithful
host-side preview.

    xor_join: out = a ^ b

WHY THIS IS NOT `kyttar.xor`. The FUNCTION is identical (and `kyttar.xor` is the
drop-in for GNU Radio's blocks.xor_bb). What differs is where the operands come
from: `kyttar.xor` takes both from ONE upstream block, while this one joins two
SEPARATE chains running on the chip at independent, asynchronous rates — the
stream-cipher case, plaintext XOR keystream. On the chip the two streams are
told apart by their ARRIVAL FACE using the arbiter LOCK, which is why the placed
block requires its two inputs on two distinct faces.

XOR is its own inverse, so the same block is both the encrypt and the decrypt
half of a stream cipher.

RATE: 1:1 — one output word per matched (a, b) pair — so unlike the rate-
EXPANDING rendezvous markers (feature_pair_join, tmr_voter) this one is a
genuine sync_block that really computes the XOR.

The face_a/face_b placement knobs of the placed block are router-reconciled
internals (see XorJoinBlock.GRC_UNSUPPORTED_PARAMS) and are intentionally NOT
exposed to GRC.

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

import numpy as np
from gnuradio import gr


class xor_join(gr.sync_block):
    """Kyttar XOR Join — bitwise XOR of two INDEPENDENT byte streams
    (placeKYT XorJoinBlock)."""

    _PLACEKYT = "XorJoinBlock"

    def __init__(self, device_id: str = "kyttar_0"):
        gr.sync_block.__init__(
            self, name="Kyttar Xor Join",
            in_sig=[np.uint8, np.uint8], out_sig=[np.uint8])
        self._device_id = device_id
        self._grc_advert = (str(device_id), self._PLACEKYT, {})

    def start(self) -> bool:
        advert = getattr(self, "_grc_advert", None)
        if advert is not None:
            try:
                from ._batch_session import get_session
                device_id, placekyt_type, params = advert
                get_session(device_id).register_params(placekyt_type, params)
            except Exception:  # noqa: BLE001 — advertising is best-effort
                pass
        return True

    def work(self, input_items, output_items):
        out = output_items[0]
        n = len(out)
        a = input_items[0][:n].astype(np.uint8)
        b = input_items[1][:n].astype(np.uint8)
        out[:n] = np.bitwise_xor(a, b)
        return n
