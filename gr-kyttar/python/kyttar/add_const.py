"""
Kyttar Add-Const Block for GNURadio

GR front-end for the placeKYT AddConstBlock — adds a real constant to a real
input stream (GNU Radio's blocks.add_const_ff): out = in + const. The real DSP
runs on the placeKYT-hosted chip; this marker keeps the exact GR interface (one
real input, one real output, a ``const`` parameter matching GR verbatim) and
computes the result in work() for a faithful host-side preview.

    out = in + const     (Q15-SATURATED on chip)

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

import numpy as np
from gnuradio import gr


class add_const(gr.sync_block):
    """Kyttar Add-Const — out = in + const (blocks.add_const_ff → AddConstBlock)."""

    def __init__(self, device_id: str = "kyttar_0", const: float = 0.0):
        gr.sync_block.__init__(
            self, name="Kyttar Add Const",
            in_sig=[np.float32], out_sig=[np.float32])
        self._device_id = device_id
        self._const = float(const)
        self._grc_advert = (str(device_id), "AddConstBlock", {"const": float(const)})

    def set_const(self, const: float) -> None:
        self._const = float(const)

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
        # Q15-saturated preview to match the on-chip datapath (GR float would not
        # clip; the chip does — see AddConstBlock).
        s = input_items[0].astype(np.float32) + np.float32(self._const)
        out[:] = np.clip(s, -1.0, 32767.0 / 32768.0).astype(np.float32)
        return len(out)
