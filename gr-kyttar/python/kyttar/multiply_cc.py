"""
Kyttar Multiply CC Block for GNURadio

GR front-end for the placeKYT MultiplyCCBlock — the elementwise product of
TWO complex streams (GNU Radio's blocks.multiply_cc). The real DSP runs on
the placeKYT-hosted chip; this marker keeps the exact GR interface (2 complex
inputs, one complex output) and computes the result in work() for a faithful
host-side preview.

    multiply_cc:  out = in0 * in1

num_inputs is pinned to 2 (a placeKYT hardware limit: a third complex stream
is a chained complex multiply — a whole second stage that does not fit).

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

import numpy as np
from gnuradio import gr


class multiply_cc(gr.sync_block):
    """Kyttar Multiply CC — product of two complex streams
    (blocks.multiply_cc → MultiplyCCBlock)."""

    def __init__(self, device_id: str = "kyttar_0", num_inputs: int = 2):
        n = int(num_inputs)
        if n != 2:
            raise ValueError(
                f"HARDWARE LIMIT: num_inputs={n} unsupported — the Kyttar "
                f"two-complex-stream product is pinned to 2 streams.")
        gr.sync_block.__init__(
            self, name="Kyttar Multiply CC",
            in_sig=[np.complex64] * n, out_sig=[np.complex64])
        self._device_id = device_id
        self._num_inputs = n
        self._grc_advert = (str(device_id), "MultiplyCCBlock",
                            {"num_inputs": n})

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
            acc = acc * input_items[i].astype(np.complex64)
        out[:] = acc
        return len(out)
