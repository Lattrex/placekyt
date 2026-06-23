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
import os
import sys

# Add path to gr_kyttar placement module
_kyttar_path = os.environ.get('KYTTAR_PATH')
if _kyttar_path:
    if _kyttar_path not in sys.path:
        sys.path.insert(0, _kyttar_path)
else:
    _default_paths = [
        str(Path(__file__).resolve().parents[3] / 'python'),
        os.path.expanduser('~/kyttar_sim/python'),
    ]
    for _path in _default_paths:
        if os.path.isdir(os.path.join(_path, 'gr_kyttar')):
            if _path not in sys.path:
                sys.path.insert(0, _path)
            break

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
        """
        Called when flowgraph starts.

        This is where we:
        1. Discover the topology from the generated Python file
        2. Run placement and routing
        3. Program the simulator
        """
        print(f"[kyttar.device] Starting device '{self._device_id}'...")

        try:
            self._initialize()
            return True
        except Exception as e:
            print(f"[kyttar.device] ERROR during initialization: {e}")
            import traceback
            traceback.print_exc()
            return False

    def stop(self) -> bool:
        """Called when flowgraph stops."""
        print(f"[kyttar.device] Stopping device '{self._device_id}'")
        self._initialized = False
        return True

    def _initialize(self) -> None:
        """
        Full initialization: discover topology, place, route, program.
        """
        from gr_kyttar.placement import (
            ArrayConfig, Placer, Router, get_block_metrics
        )
        from gr_kyttar.bitstream import BitstreamGenerator

        registry = get_registry()
        device = registry.get_device(self._device_id)

        if device is None:
            raise RuntimeError(f"Device not found in registry: {self._device_id}")

        # Get the DSP blocks registered with this device
        dsp_blocks = device.dsp_blocks

        if not dsp_blocks:
            print(f"[kyttar.device] WARNING: No DSP blocks registered. Nothing to program.")
            return

        print(f"[kyttar.device] Found {len(dsp_blocks)} DSP blocks to place")

        # === STEP 1: Load config ===
        config = ArrayConfig.from_yaml(self._config_file)
        print(f"[kyttar.device] Array: {config.width}x{config.height}")
        print(f"[kyttar.device] Ports: {list(config.ports.keys())}")

        # Determine which ports to use
        # For now, use first input and output ports
        # TODO: Get from source/sink block registrations
        input_ports = config.get_input_ports()
        output_ports = config.get_output_ports()

        if not input_ports:
            raise RuntimeError("No input ports defined in chip config")
        if not output_ports:
            raise RuntimeError("No output ports defined in chip config")

        input_port_name = input_ports[0].name
        output_port_name = output_ports[0].name

        print(f"[kyttar.device] Using input port: {input_port_name}")
        print(f"[kyttar.device] Using output port: {output_port_name}")

        # === STEP 2: Get block definitions ===
        block_defs = [b.get_block_definition() for b in dsp_blocks]

        # === STEP 3: Place ===
        print("[kyttar.device] Running placement...")
        metrics = get_block_metrics(dsp_blocks)
        placer = Placer(config, input_port=input_port_name, output_port=output_port_name)
        placement = placer.place(block_defs, metrics)

        for name, placed in placement.placed_blocks.items():
            print(f"[kyttar.device]   {name} placed at {placed.anchor}")

        # === STEP 4: Route ===
        print("[kyttar.device] Running routing...")
        router = Router(config, input_port=input_port_name, output_port=output_port_name)
        cell_map = router.route(placement, block_defs)
        print(f"[kyttar.device]   Total cells: {cell_map.cell_count()}")

        # === STEP 5: Generate bitstream ===
        print("[kyttar.device] Generating bitstream...")
        gen = BitstreamGenerator(self._config_file)
        gen.load_cell_map(cell_map)
        bitstream = gen.generate(custom_row0=False)
        print(f"[kyttar.device]   Bitstream words: {len(bitstream.words)}")

        # === STEP 6: Create and program simulator ===
        print("[kyttar.device] Programming simulator...")

        import simkyt

        chip_type_obj = simkyt.ChipType.from_yaml(self._config_file)
        chip = simkyt.Chip.from_chip_type(chip_type_obj)

        # Program cells from cell_map directly using write_cell_memory
        cells_programmed = 0
        # cell_map.Face enum: SOUTH=0, EAST=1, WEST=2, NORTH=3
        face_names = {0: "south", 1: "east", 2: "west", 3: "north"}
        for (col, row), cell_config in cell_map.cells.items():
            cell_id = row * config.width + col
            # Write memory contents
            for addr, value in cell_config.memory.items():
                chip.write_cell_memory(cell_id, addr, value)
            # Set forward face
            if cell_config.fwd_face is not None:
                face_name = face_names.get(cell_config.fwd_face.value, "south")
                chip.set_fwd_face(cell_id, face_name)
            cells_programmed += 1

        print(f"[kyttar.device]   Programmed {cells_programmed} cells")

        # Set the input port entry address and target hop count
        # When data arrives at the input port:
        # 1. A WRITE instruction is injected with target_hop_count to route to the first block
        # 2. A JUMP instruction follows to start execution at entry_addr
        if placement.placed_blocks:
            first_block = next(iter(placement.placed_blocks.values()))
            first_cell_config = cell_map.get_cell(*first_block.entry_cell)
            if first_cell_config and first_cell_config.entry_addr is not None:
                entry_addr = first_cell_config.entry_addr
            else:
                entry_addr = 0  # Default entry point

            # Calculate target hop count for routing from port to first block
            # Get input port position from config
            input_port_pos = config.get_port_position(input_port_name)
            first_block_entry = first_block.entry_cell

            # Manhattan distance from port cell to first block's entry cell
            distance = abs(first_block_entry[0] - input_port_pos[0]) + \
                       abs(first_block_entry[1] - input_port_pos[1])

            # HOP_CNT is incremented BEFORE the check at each cell.
            # For the instruction to execute at distance d:
            #   - It visits d cells (including the port cell)
            #   - Each cell increments HOP_CNT
            #   - After d increments, HOP_CNT should equal 31
            #   - So: target_hop_count + d = 31, therefore target_hop_count = 31 - d
            # BUT: the port cell also increments HOP_CNT before checking!
            # So actually: target_hop_count + d + 1 = 31, giving target_hop_count = 30 - d
            target_hop_count = 30 - distance

            chip.set_port_entry_address(input_port_name, entry_addr)
            chip.set_port_target_hop_count(input_port_name, target_hop_count)

            print(f"[kyttar.device]   Input port '{input_port_name}' -> block at {first_block_entry}")
            print(f"[kyttar.device]   Distance: {distance} hops, target_hop_count: {target_hop_count}")
            print(f"[kyttar.device]   Entry address: {entry_addr}")

        # Store chip in registry
        registry.set_chip(self._device_id, chip)

        # Store additional state for source/sink blocks
        self._placement = placement
        self._block_defs = block_defs
        self._config = config
        self._input_port_name = input_port_name
        self._output_port_name = output_port_name

        self._initialized = True
        print("[kyttar.device] Initialization complete!")

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
