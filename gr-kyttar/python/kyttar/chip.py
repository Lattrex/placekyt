"""
Kyttar Chip - Hierarchical Container Block for GNURadio

Hierarchical container for Kyttar DSP blocks for a single chip. Users drag DSP
blocks INSIDE this container in GRC.

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact GR interface (class name, params, ports) so it places/wires identically in
GRC, but does NO in-process placement and streams pure pass-through.

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

import numpy as np
from gnuradio import gr
from typing import List

__all__ = ['kyttar_chip']


# Chip type configurations
CHIP_CONFIGS = {
    '12x12_dev': {
        'width': 12,
        'height': 12,
        'input_port_edge': 3,   # West
        'input_port_offset': 0,
        'output_port_edge': 1,  # East
        'output_port_offset': 11,
        'config_file': 'dev_12x12.yaml',
    },
}


class kyttar_chip(gr.hier_block2):
    """
    Kyttar Chip - Container for DSP Blocks

    This hierarchical block represents a single Kyttar chip. Place DSP blocks
    (AGC, FIR, NCO, etc.) inside this container. GR marker; the real DSP runs on
    the placeKYT-hosted chip, so this container only carries the float stream
    through unchanged.

    Parameters:
        chip_type: Preset chip configuration ('12x12_dev', 'custom')
        chip_config: Path to custom YAML config (only used if chip_type='custom')
        interface: Communication interface ('simulator', 'usb', 'spi')
    """

    def __init__(
        self,
        chip_type: str = '12x12_dev',
        chip_config: str = '',
        interface: str = 'simulator',
    ):
        gr.hier_block2.__init__(
            self,
            "Kyttar Chip",
            gr.io_signature(1, 1, gr.sizeof_float),  # 1 float input
            gr.io_signature(1, 1, gr.sizeof_float),  # 1 float output
        )

        self._chip_type = chip_type
        self._chip_config_path = chip_config
        self._interface = interface

        # Registered DSP blocks (populated by register_block)
        self._blocks: List = []

        # Create internal processing block and pass the stream through unchanged
        self._processor = _chip_processor(self)
        self.connect(self, self._processor, self)

    def register_block(self, block):
        """Register a DSP block to run on this chip."""
        self._blocks.append(block)

    def get_blocks(self) -> List:
        """Get all registered blocks."""
        return self._blocks.copy()

    def process_sample(self, sample: float) -> float:
        """Process a single sample - pass through (the real DSP runs on chip)."""
        return sample


class _chip_processor(gr.sync_block):
    """
    Internal processor block - a pass-through carrier inside the hier block.

    The real DSP runs on the placeKYT-hosted chip; this just carries the stream.
    """

    def __init__(self, chip):
        gr.sync_block.__init__(
            self,
            name="Kyttar Chip Processor",
            in_sig=[np.float32],
            out_sig=[np.float32],
        )
        self._chip = chip

    def work(self, input_items, output_items):
        inp = input_items[0]
        out = output_items[0]
        n = min(len(inp), len(out))
        out[:n] = inp[:n]
        return n
