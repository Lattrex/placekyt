"""
Kyttar XOR Block for GNURadio

GR front-end for the placeKYT XorBlock — the bitwise XOR of two byte (unsigned
char) input streams (GNU Radio's blocks.xor_bb). The real DSP runs on the
placeKYT-hosted chip; this marker keeps the exact GR interface (byte inputs, one
byte output) and computes the result in work() for a faithful host-side preview.

    xor: out = in0 ^ in1

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

import numpy as np
from gnuradio import gr


class xor(gr.sync_block):
    """Kyttar XOR — bitwise XOR of two byte streams (blocks.xor_bb → XorBlock)."""

    _PLACEKYT = "XorBlock"

    def __init__(self, device_id: str = "kyttar_0", num_inputs: int = 2):
        n = int(num_inputs)
        if n != 2:
            # placeKYT's XorBlock builds the canonical two-input case (out = a ^ b);
            # GR's xor_bb allows 2+, but only 2 is mapped on-chip today.
            raise ValueError(
                f"Kyttar XOR maps the 2-input case only, got num_inputs={n}")
        gr.sync_block.__init__(
            self, name="Kyttar XOR",
            in_sig=[np.uint8] * n, out_sig=[np.uint8])
        self._device_id = device_id
        self._num_inputs = n
        self._grc_advert = (str(device_id), self._PLACEKYT, {})

    def start(self) -> bool:
        advert = getattr(self, "_grc_advert", None)
        if advert is not None:
            try:
                from ._batch_session import get_session
                device_id, placekyt_type, params = advert
                get_session(device_id).register_params(placekyt_type, params)
            except Exception:  # noqa: BLE001
                pass
        return True

    def work(self, input_items, output_items):
        out = output_items[0]
        acc = input_items[0].astype(np.uint8).copy()
        for i in range(1, len(input_items)):
            acc = np.bitwise_xor(acc, input_items[i].astype(np.uint8))
        out[:] = acc
        return len(out)
