"""
Kyttar Mux Block for GNURadio

Combines multiple input channels into a single interleaved output. This is the
counterpart to demux - use at the end of parallel I/Q processing to recombine
streams before the output port.

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact GR interface (class name, params, ports) so it places/wires identically in
GRC, but does NO in-process placement and streams pure pass-through.

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

from .dsp_markers import _PassThrough

# Channel entry addresses (must match placement/mux_block.py)
CHANNEL_ENTRY_ADDRESSES = [1, 11, 21]


class mux(_PassThrough):
    """
    Kyttar Mux - Channel Combiner

    Combines separate channel inputs into a single interleaved output on the
    chip. GR marker; the real DSP runs on the placeKYT-hosted chip.

    This block has num_channels inputs and 1 output.

    Parameters:
        device_id: ID of the kyttar.device to use
        num_channels: Number of input channels (2 for I/Q, up to 3)
    """

    def __init__(self, device_id: str = "kyttar_0", num_channels: int = 2):
        if num_channels < 2 or num_channels > 3:
            raise ValueError("num_channels must be 2 or 3")

        super().__init__(name="Kyttar Mux", n_in=num_channels, n_out=1)

        self._device_id = device_id
        self._num_channels = num_channels

    def get_num_channels(self) -> int:
        """Get number of input channels."""
        return self._num_channels
