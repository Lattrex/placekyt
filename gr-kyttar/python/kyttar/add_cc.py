"""
Kyttar Add CC / Subtract CC Blocks for GNURadio

GR front-ends for the placeKYT AddCCBlock / SubCCBlock — the elementwise sum /
difference of TWO complex streams (GNU Radio's blocks.add_cc / blocks.sub_cc).
The real DSP runs on the placeKYT-hosted chip; these markers keep the exact GR
interface (2 complex inputs, one complex output) and compute the result in
work() for a faithful host-side preview.

    add_cc:  out = in0 + in1
    sub_cc:  out = in0 - in1

num_inputs is pinned to 2 (a placeKYT hardware limit: a third complex stream
does not fit the 32-word landing cell).

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

import numpy as np
from gnuradio import gr


class _AddSubCC(gr.sync_block):
    _SIGN = +1        # +1 add, -1 subtract
    _NAME = "Kyttar Add CC"
    _PLACEKYT = "AddCCBlock"

    def __init__(self, device_id: str = "kyttar_0", num_inputs: int = 2):
        n = int(num_inputs)
        if n != 2:
            raise ValueError(
                f"HARDWARE LIMIT: num_inputs={n} unsupported — the Kyttar "
                f"two-complex-stream combiner is pinned to 2 streams.")
        gr.sync_block.__init__(
            self, name=self._NAME,
            in_sig=[np.complex64] * n, out_sig=[np.complex64])
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
        acc = input_items[0].astype(np.complex64).copy()
        for i in range(1, len(input_items)):
            acc = acc + self._SIGN * input_items[i].astype(np.complex64)
        out[:] = acc
        return len(out)


class add_cc(_AddSubCC):
    """Kyttar Add CC — sum of two complex streams (blocks.add_cc → AddCCBlock)."""
    _SIGN = +1
    _NAME = "Kyttar Add CC"
    _PLACEKYT = "AddCCBlock"


class sub_cc(_AddSubCC):
    """Kyttar Subtract CC — in0 - in1 (blocks.sub_cc → SubCCBlock)."""
    _SIGN = -1
    _NAME = "Kyttar Subtract CC"
    _PLACEKYT = "SubCCBlock"
