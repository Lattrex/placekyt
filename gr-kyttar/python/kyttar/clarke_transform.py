"""
Kyttar Clarke Transform Block for GNURadio

GR front-end for the placeKYT ClarkeTransformBlock — the two-current Clarke
(abc -> alpha-beta) transform for 3-phase motor control (FOC). The real DSP
runs on the placeKYT-hosted chip; this marker keeps the exact GR interface
(two float phase-current inputs, one complex alpha-beta output) and computes
the result in work() for a faithful host-side preview.

    i_alpha = ia
    i_beta  = (ia + 2*ib) / sqrt(3)          (clamped to the Q15 range)
    out     = i_alpha + j*i_beta

The amplitude-invariant two-current form: with ia + ib + ic = 0 the third
phase current is redundant, so only ia and ib are sensed — the standard
two-shunt FOC front end. There is no stock GNU Radio counterpart; the on-chip
golden is a pinned host reference (see
verification/tests/test_clarke_transform.py).

On the chip the two currents are produced by two independent chains and are
told apart by their ARRIVAL FACE using the arbiter LOCK, which is why the
placed block requires its two inputs on two distinct faces. The
face_ia/face_ib placement knobs of the placed block are router-reconciled
internals (see ClarkeTransformBlock.GRC_UNSUPPORTED_PARAMS) and are
intentionally NOT exposed to GRC.

RATE: 1:1 — one complex sample per matched (ia, ib) pair — a genuine
sync_block that really computes the transform (the xor_join convention, not
the rate-expanding rendezvous markers).

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

import numpy as np
from gnuradio import gr

_INV_SQRT3 = 1.0 / np.sqrt(3.0)


class clarke_transform(gr.sync_block):
    """Kyttar Clarke Transform — (ia, ib) -> i_alpha + j*i_beta
    (placeKYT ClarkeTransformBlock)."""

    _PLACEKYT = "ClarkeTransformBlock"

    def __init__(self, device_id: str = "kyttar_0"):
        gr.sync_block.__init__(
            self, name="Kyttar Clarke Transform",
            in_sig=[np.float32, np.float32], out_sig=[np.complex64])
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
        ia = input_items[0][:n].astype(np.float64)
        ib = input_items[1][:n].astype(np.float64)
        beta = (ia + 2.0 * ib) * _INV_SQRT3
        # Mirror the chip's SATURATING Q15 adds: clamp to the Q15 rails
        # instead of letting the host preview exceed what the chip can emit.
        beta = np.clip(beta, -1.0, 32767.0 / 32768.0)
        out[:n] = (ia + 1j * beta).astype(np.complex64)
        return n
