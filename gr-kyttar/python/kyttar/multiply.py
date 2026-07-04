"""
Kyttar Multiply Block for GNURadio

GR front-end for the placeKYT MultiplyBlock — the element-wise product of N real
input streams (GNU Radio's blocks.multiply_ff). The real DSP runs on the
placeKYT-hosted chip; this marker keeps the exact GR interface (N real inputs, one
real output) so it places/wires identically in GRC, and computes the product in
work() so a host-side preview scope shows the right thing.

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

import numpy as np
from gnuradio import gr


class multiply(gr.sync_block):
    """Kyttar Multiply — element-wise product of ``num_inputs`` real streams.

    Drop-in for ``blocks.multiply_ff``: ``out = in0 * in1 * … * in(N-1)``. On the
    Kyttar chip this maps to MultiplyBlock (a chain of Q15 MULQ). Place it between a
    KyttarSource and KyttarSink; ensure device_id matches your KyttarDevice.

    Parameters:
        device_id:  which Kyttar device to use
        num_inputs: number of real streams to multiply (default 2)
    """

    def __init__(self, device_id: str = "kyttar_0", num_inputs: int = 2):
        n = int(num_inputs)
        gr.sync_block.__init__(
            self, name="Kyttar Multiply",
            in_sig=[np.float32] * n, out_sig=[np.float32])
        self._device_id = device_id
        self._num_inputs = n
        self._advertise_grc_params(device_id, "MultiplyBlock",
                                   {"num_inputs": n})

    def _advertise_grc_params(self, device_id, placekyt_type, params):
        self._grc_advert = (str(device_id), str(placekyt_type), dict(params or {}))

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
        prod = input_items[0].astype(np.float32).copy()
        for i in range(1, len(input_items)):
            prod = prod * input_items[i].astype(np.float32)
        out[:] = prod
        return len(out)
