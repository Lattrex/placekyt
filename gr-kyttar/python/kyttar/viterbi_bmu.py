"""
Kyttar Viterbi Branch Metric Unit GRC Block.

Computes branch metrics for K=7 rate 1/2 Viterbi decoding.

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact GR interface (class name, params, ports) so it places/wires identically in
GRC, but does NO in-process placement and streams pure pass-through.
"""

from .dsp_markers import _PassThrough


class viterbi_bmu(_PassThrough):
    """
    Viterbi Branch Metric Unit Block.

    Computes branch metrics for K=7 rate 1/2 Viterbi decoder on the chip.
    GR marker; the real DSP runs on the placeKYT-hosted chip.

    Parameters:
        device_id: Device ID to register with
    """

    def __init__(
        self,
        device_id: str = "kyttar_0",
    ):
        super().__init__(name="Kyttar Viterbi BMU", n_in=1, n_out=1)
        self._device_id = device_id
