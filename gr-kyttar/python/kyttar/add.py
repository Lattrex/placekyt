"""
Kyttar Add / Subtract Blocks for GNURadio

GR front-ends for the placeKYT AddBlock / SubtractBlock — the sum / difference of N
real input streams (GNU Radio's blocks.add_ff / blocks.sub_ff). The real DSP runs on
the placeKYT-hosted chip; these markers keep the exact GR interface (N real inputs,
one real output) and compute the result in work() for a faithful host-side preview.

    add:      out = in0 + in1 + … + in(N-1)
    subtract: out = in0 - in1 - … - in(N-1)

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

import numpy as np
from gnuradio import gr


class _AddSub(gr.sync_block):
    _SIGN = +1        # +1 add, -1 subtract
    _NAME = "Kyttar Add"
    _PLACEKYT = "AddBlock"

    def __init__(self, device_id: str = "kyttar_0", num_inputs: int = 2):
        n = int(num_inputs)
        gr.sync_block.__init__(
            self, name=self._NAME,
            in_sig=[np.float32] * n, out_sig=[np.float32])
        self._device_id = device_id
        self._num_inputs = n
        self._grc_advert = (str(device_id), self._PLACEKYT, {"num_inputs": n})

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
        acc = input_items[0].astype(np.float32).copy()
        for i in range(1, len(input_items)):
            acc = acc + self._SIGN * input_items[i].astype(np.float32)
        out[:] = acc
        return len(out)


class add(_AddSub):
    """Kyttar Add — sum of ``num_inputs`` real streams (blocks.add_ff → AddBlock)."""
    _SIGN = +1
    _NAME = "Kyttar Add"
    _PLACEKYT = "AddBlock"


class subtract(_AddSub):
    """Kyttar Subtract — in0 - in1 - … (blocks.sub_ff → SubtractBlock)."""
    _SIGN = -1
    _NAME = "Kyttar Subtract"
    _PLACEKYT = "SubtractBlock"
