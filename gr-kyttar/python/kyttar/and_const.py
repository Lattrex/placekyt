"""
Kyttar AND-Const Block for GNURadio.

GR front-end for the placeKYT AndConstBlock — bitwise AND of a byte stream with
an immediate constant (GNU Radio's blocks.and_const_bb). The real DSP runs on the
placeKYT-hosted chip; this block keeps the exact GR interface (byte in, byte out,
the `constant` parameter mirrored verbatim) and computes the result in work() for a
faithful host-side preview.

    out[n] = in[n] & constant        # masking, e.g. &1 takes the LSB

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

import numpy as np
from gnuradio import gr


class and_const(gr.sync_block):
    """Kyttar AND-Const — ``out = in & constant`` on a byte stream
    (blocks.and_const_bb → AndConstBlock).

    Parameters:
        device_id: which Kyttar device to register with.
        constant: the immediate AND mask (0..255), mirrored VERBATIM from
            GNU Radio's ``and_const_bb`` and applied as an 8-bit byte mask.
    """

    _NAME = "Kyttar AND Const"
    _PLACEKYT = "AndConstBlock"

    def __init__(self, device_id: str = "kyttar_0", constant: int = 1):
        gr.sync_block.__init__(
            self, name=self._NAME,
            in_sig=[np.uint8], out_sig=[np.uint8])
        self._device_id = device_id
        self._constant = int(constant)
        self._mask = self._constant & 0xFF
        self._grc_advert = (str(device_id), self._PLACEKYT,
                            {"constant": self._constant})

    @property
    def constant(self) -> int:
        return self._constant

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
        out[:] = (input_items[0].astype(np.uint16) & self._mask).astype(np.uint8)
        return len(out)
