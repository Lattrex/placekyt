"""
Kyttar Viterbi K=7 Decoder GRC Block.

Full K=7 Rate 1/2 Viterbi decoder for the NASA standard convolutional
code (G1=0x79, G2=0x5B).

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact GR interface (class name, params, ports) so it places/wires identically in
GRC, but does NO in-process placement and streams pure pass-through.
"""

from .dsp_markers import _PassThrough


class viterbi_k7(_PassThrough):
    """
    Viterbi K=7 Rate 1/2 Decoder Block.

    Soft-decision Viterbi decoder for the NASA standard K=7 convolutional
    code on the chip. GR marker; the real DSP runs on the placeKYT-hosted chip.

    Input: Soft bits (LLRs) from soft demodulator, 2 per encoded symbol
    Output: Decoded data bits

    Parameters:
        device_id: Device ID to register with
        traceback_depth: Survivor path memory depth (default 35 = 5*K)
    """

    def __init__(
        self,
        device_id: str = "kyttar_0",
        traceback_depth: int = 35,
    ):
        super().__init__(name="Kyttar Viterbi K=7", n_in=1, n_out=1)
        self._device_id = device_id
        self._traceback_depth = traceback_depth

    @property
    def traceback_depth(self) -> int:
        """Traceback depth."""
        return self._traceback_depth

    @property
    def cell_count(self) -> int:
        """Number of cells used (68: 1 BMU + 64 ACS + 2 DEC + 1 TRACE)."""
        return 68
