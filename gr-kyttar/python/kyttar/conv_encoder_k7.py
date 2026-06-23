"""
Kyttar Convolutional Encoder K=7 GRC Block.

Rate 1/2 K=7 convolutional encoder using the NASA standard polynomials:
  G1 = 0x79 (171 octal) = 1111001
  G2 = 0x5B (133 octal) = 1011011

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact GR interface (class name, params, ports) so it places/wires identically in
GRC, but does NO in-process placement and streams pure pass-through.
"""

import numpy as np
from gnuradio import gr


class conv_encoder_k7(gr.sync_block):
    """
    Convolutional Encoder K=7 Rate 1/2 Block.

    Encodes input bits using the NASA standard K=7 convolutional code on the
    chip. GR marker; the real DSP runs on the placeKYT-hosted chip.

    Input: Data bits (0/1 as float)
    Output: Encoded bits (0/1 as float), 2x input rate

    Parameters:
        device_id: Device ID to register with
    """

    def __init__(
        self,
        device_id: str = "kyttar_0",
    ):
        gr.sync_block.__init__(
            self,
            name="Kyttar Conv Encoder K=7",
            in_sig=[np.float32],
            out_sig=[np.float32],
        )

        # Set output multiple to 2 (rate 1/2 encoder)
        self.set_output_multiple(2)

        self._device_id = device_id

    def forecast(self, noutput_items, ninputs):
        """Tell scheduler how many input items needed for noutput_items output."""
        # For rate 1/2: need noutput_items/2 input items
        ninput_items_required = [noutput_items // 2]
        return ninput_items_required

    def general_work(self, input_items, output_items):
        """Pass through - the real DSP runs on the placeKYT-hosted chip."""
        inp = input_items[0]
        out = output_items[0]
        n = min(len(inp), len(out))
        out[:n] = inp[:n]
        self.consume(0, n)
        return n

    def work(self, input_items, output_items):
        """Pass through - the real DSP runs on the placeKYT-hosted chip."""
        inp = input_items[0]
        out = output_items[0]
        n = min(len(inp), len(out))
        out[:n] = inp[:n]
        return n

    @property
    def g1(self) -> int:
        """Generator polynomial 1."""
        return 0x79

    @property
    def g2(self) -> int:
        """Generator polynomial 2."""
        return 0x5B

    @property
    def cell_count(self) -> int:
        """Number of cells used."""
        return 1
