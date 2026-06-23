"""
Kyttar Demux Block for GNURadio

Routes incoming data to 1 of up to 3 output channels. This is a fundamental
routing primitive for I/Q and multi-channel processing.

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact GR interface (class name, params, ports) so it places/wires identically in
GRC, but does NO in-process placement and streams pure pass-through.

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

from .dsp_markers import _PassThrough

# Channel entry addresses (must match placement/demux_block.py)
CHANNEL_ENTRY_ADDRESSES = [1, 11, 21]


class demux(_PassThrough):
    """
    Kyttar Demux - Channel Splitter

    Routes incoming interleaved data to separate output ports on the chip.
    GR marker; the real DSP runs on the placeKYT-hosted chip.

    This block has 1 input and num_channels outputs.

    Parameters:
        device_id: ID of the kyttar.device to use
        num_channels: Number of output channels (2 for I/Q, up to 3)
    """

    def __init__(self, device_id: str = "kyttar_0", num_channels: int = 2):
        if num_channels < 2 or num_channels > 3:
            raise ValueError("num_channels must be 2 or 3")

        super().__init__(name="Kyttar Demux", n_in=1, n_out=num_channels)

        self._device_id = device_id
        self._num_channels = num_channels

    def get_num_channels(self) -> int:
        """Get number of output channels."""
        return self._num_channels
