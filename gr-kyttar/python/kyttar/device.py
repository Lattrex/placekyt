"""
Kyttar Device Block for GNURadio

This is a configuration-only block that defines a Kyttar chip.
It has no signal connections - it just registers the device with the registry
and triggers initialization when the flowgraph starts.

Usage:
1. Add kyttar.device to flowgraph (no connections)
2. Add kyttar.source block, select this device and input port
3. Add Kyttar DSP blocks
4. Add kyttar.sink block, select this device and output port
5. Connect: Source -> DSP blocks -> Sink

On start(), this block:
1. Discovers topology from connected blocks
2. Runs placement and routing
3. Generates bitstream
4. Programs the simulator

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

import numpy as np
from gnuradio import gr
from pathlib import Path
from typing import Optional, List, Dict, Any

# SOCKET-ONLY OOT: this configuration block is retained so existing GRC
# flowgraphs/.block.yml that reference kyttar.device still instantiate, but the
# heavy SELF-PLACING path (which imported gr_kyttar + simkyt to place/route/build
# a local chip) has been REMOVED. The supported model is server-batch: a
# placeKYT-hosted chip driven over a socket by kyttar.source/kyttar.sink. This
# block now does NO placement and imports gnuradio + numpy ONLY.

from .registry import get_registry, DeviceType


# Chip configurations
CHIP_CONFIGS = {
    '12x12_dev': {
        'config_file': 'dev_12x12.yaml',
        'description': '12x12 development chip (simulator)',
    },
}


class device(gr.basic_block):
    """
    Kyttar Device - Chip Configuration Block

    This block defines a Kyttar chip and manages its lifecycle.
    It has NO signal ports - it's configuration only.

    On flowgraph start:
    1. Collects all DSP blocks between kyttar.source and kyttar.sink
    2. Runs placement and routing
    3. Generates bitstream and programs simulator

    Parameters:
        device_id: Unique identifier for this device
        chip_type: Chip configuration to use ('12x12_dev')
    """

    def __init__(
        self,
        device_id: str = "kyttar_0",
        chip_type: str = "12x12_dev",
    ):
        gr.basic_block.__init__(
            self,
            name="Kyttar Device",
            in_sig=[],  # No signal ports
            out_sig=[],
        )

        self._device_id = device_id
        self._chip_type = chip_type
        self._initialized = False

        # Get config path
        if chip_type not in CHIP_CONFIGS:
            raise ValueError(
                f"Unknown chip type: {chip_type}. "
                f"Available: {list(CHIP_CONFIGS.keys())}"
            )

        configs_dir = Path(__file__).parent.parent.parent.parent / 'configs'
        if not configs_dir.exists():
            configs_dir = Path(str(Path(__file__).resolve().parents[3] / 'configs'))

        self._config_file = str(configs_dir / CHIP_CONFIGS[chip_type]['config_file'])

        # Register with registry
        registry = get_registry()
        registry.register_device(
            device_id=device_id,
            chip_config=self._config_file,
            device_type=DeviceType.SIMULATOR,
        )

        print(f"[kyttar.device] Registered device '{device_id}' with config: {self._config_file}")

    def start(self) -> bool:
        """Called when flowgraph starts. No-op: self-placing has been removed; the
        supported path is server-batch (placeKYT-hosted chip over a socket)."""
        print(f"[kyttar.device] '{self._device_id}' is configuration-only "
              f"(self-placing removed; use server-batch mode on source/sink)")
        return True

    def stop(self) -> bool:
        """Called when flowgraph stops."""
        self._initialized = False
        return True

    def general_work(self, input_items, output_items):
        """
        No-op work function - this block has no signal ports.
        """
        return 0

    # Accessors for source/sink blocks
    def get_chip(self) -> Optional[Any]:
        """Get the programmed chip instance."""
        registry = get_registry()
        return registry.get_chip(self._device_id)

    def get_device_id(self) -> str:
        """Get the device ID."""
        return self._device_id

    def is_initialized(self) -> bool:
        """Check if device is initialized."""
        return self._initialized
