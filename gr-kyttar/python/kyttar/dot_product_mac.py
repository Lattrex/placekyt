# SPDX-License-Identifier: GPL-3.0-or-later
"""
Kyttar Dot Product MAC GRC marker — placeKYT ``DotProductMACBlock``.

Fixed-coefficient dot product over a K-element input vector: K consecutive
samples form one fresh vector, one weighted-sum word (``bias + sum c[i]*x[i]``)
is emitted per vector (rate-reducing K:1, no delay line / no sample aging —
the correlator pattern, not the FIR pattern). There is no stock GNU Radio
counterpart, so this is a placeKYT-native ([Kyttar]) block — still fully
placeable in GRC with its parameters.

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact interface (class name, params, ports) so it places/wires identically in
GRC, but does NO in-process placement and streams pure pass-through.
"""

from typing import List

from .dsp_markers import _PassThrough


class dot_product_mac(_PassThrough):
    """
    Kyttar Dot Product MAC — placeKYT ``DotProductMACBlock`` (no GR counterpart).

    Parameters (mirror ``DotProductMACBlock`` VERBATIM):
        device_id: Device ID to register with.
        coefficients: the K weights (floats, arbitrary magnitude — the block
            derives its 2^-S coefficient-headroom prescale internally).
        bias: additive constant preloaded into the accumulator (default 0.0).
        k: vector length K, 2..7; must equal len(coefficients) (default 4).
        mode: "raw" (emit y / 2^S; the derived S is exposed as the placed
            block's `scale_shift`) or "restored" (saturating << S).

    Input:  float stream (one Q15 sample per item).
    Output: one float word per K input samples (raw or restored).
    """

    def __init__(self, device_id: str = "kyttar_0",
                 coefficients: List[float] = None, bias: float = 0.0,
                 k: int = 4, mode: str = "raw"):
        super().__init__(name="Kyttar Dot Product MAC", n_in=1, n_out=1)
        self._device_id = device_id
        if coefficients is None:
            coefficients = [0.25, 0.25, 0.25, 0.25]
        self._coefficients = [float(c) for c in coefficients]
        self._bias = float(bias)
        self._k = int(k)
        self._mode = str(mode)
        # Advertise params for GRC<->placeKYT sync detection (see dsp_markers).
        # Names match DotProductMACBlock's constructor kwargs verbatim.
        self._advertise_grc_params(
            device_id, "DotProductMACBlock",
            {"coefficients": self._coefficients, "bias": self._bias,
             "k": self._k, "mode": self._mode})

    def get_coefficients(self) -> List[float]:
        return list(self._coefficients)

    @property
    def k(self) -> int:
        return self._k

    @property
    def mode(self) -> str:
        return self._mode
