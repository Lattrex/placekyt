"""
Kyttar Float→Char Block for GNURadio

GR front-end for the placeKYT FloatToCharBlock — the drop-in for GNU Radio
``blocks.float_to_char(scale)``: ``out = saturate_int8(round_half_even(in*scale))``,
a signed 8-bit integer in [-128, 127]. The real DSP runs on the placeKYT-hosted
chip on the Q15 datapath (input in [-1, 1)); this marker keeps the exact GR
interface (one real input, one signed-char output, the ``scale`` param) and
computes a faithful host-side preview in work().

HARDWARE DEVIATION (INV-0): on the Q15 fabric ``scale`` must be a non-negative
INTEGER; a non-integer/negative ``scale`` cannot be reproduced bit-exactly and the
placeKYT block RAISES. GR's own ``float_to_char`` accepts an arbitrary float scale.

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

import numpy as np
from gnuradio import gr


class float_to_char(gr.sync_block):
    """Kyttar Float→Char — blocks.float_to_char(scale) → FloatToCharBlock.

    Parameters:
        device_id: ID of the kyttar.device to use.
        scale: multiplies the input before rounding (GR ``scale``). Must be a
            non-negative integer on the Q15 fabric (HARDWARE DEVIATION).
    """
    _PLACEKYT = "FloatToCharBlock"

    def __init__(self, device_id: str = "kyttar_0", scale: float = 1.0):
        gr.sync_block.__init__(
            self, name="Kyttar Float To Char",
            in_sig=[np.float32], out_sig=[np.int8])
        self._device_id = device_id
        self._scale = scale
        self._grc_advert = (str(device_id), self._PLACEKYT, {"scale": scale})

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

    def set_scale(self, scale: float):
        self._scale = scale

    def get_scale(self) -> float:
        return self._scale

    def work(self, input_items, output_items):
        # Match GR float_to_char: out = saturate_int8(round-half-even(in*scale)).
        # numpy's np.rint is round-half-to-even (the same as lrintf's default mode).
        x = input_items[0].astype(np.float64) * float(self._scale)
        y = np.rint(x)
        y = np.clip(y, -128, 127).astype(np.int8)
        output_items[0][:] = y
        return len(output_items[0])
