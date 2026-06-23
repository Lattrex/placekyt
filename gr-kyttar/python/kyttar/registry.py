"""
Kyttar Registry - Central manager for device/block coordination

This singleton registry:
1. Tracks registered devices (kyttar.device blocks)
2. Associates source/sink blocks with their devices
3. Coordinates initialization: topology discovery, placement, routing, programming
4. Manages runtime state (simulator instances)

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

import threading
from typing import Dict, List, Optional, Set, Tuple, Any
from dataclasses import dataclass, field
from enum import Enum


class DeviceType(Enum):
    """Type of Kyttar device."""
    SIMULATOR = "simulator"
    HARDWARE = "hardware"  # Future


@dataclass
class DeviceInfo:
    """Information about a registered device."""
    device_id: str
    chip_config: str  # Path to YAML config
    device_type: DeviceType

    # Runtime state
    chip: Any = None  # kyttar.Chip instance
    is_initialized: bool = False
    is_running: bool = False

    # Registered blocks for this device
    source_blocks: List[str] = field(default_factory=list)  # (block_id, port_name)
    sink_blocks: List[str] = field(default_factory=list)    # (block_id, port_name)
    dsp_blocks: List[Any] = field(default_factory=list)     # KyttarBlock instances

    # Map from GRC block symbol_name (e.g., "kyttar_dc_blocker0") to KyttarBlock
    # This is populated when blocks register and provides fast lookup by GR name
    block_by_symbol: Dict[str, Any] = field(default_factory=dict)

    # Map from KyttarBlock instance (by id) to GR wrapper block
    # This is used to find the GR block for a given impl during connection setup
    impl_to_gr_block: Dict[int, Any] = field(default_factory=dict)

    # Connection topology (discovered from GRC flowgraph edge_list)
    # Each entry: (from_symbol:port, to_symbol:port) as raw strings
    # Parsed during initialization to establish KyttarBlock connections
    gr_edge_list: str = ""

    # Connection topology (discovered from GRC flowgraph)
    # Each entry: (from_block_id, from_port, to_block_id, to_port)
    connections: List[Tuple[str, str, str, str]] = field(default_factory=list)

    # Placement results (set after placement runs)
    # Maps block name to (col, row) position
    placement: Dict[str, Tuple[int, int]] = field(default_factory=dict)

    # Array configuration
    array_width: int = 12
    array_height: int = 12


class KyttarRegistry:
    """
    Singleton registry for Kyttar devices and blocks.

    Thread-safe singleton pattern for use across GNURadio blocks.
    """

    _instance: Optional['KyttarRegistry'] = None
    _lock = threading.Lock()

    def __new__(cls) -> 'KyttarRegistry':
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._devices: Dict[str, DeviceInfo] = {}
        self._block_to_device: Dict[str, str] = {}  # block_id -> device_id
        # Pending registrations for devices that don't exist yet
        self._pending_sources: Dict[str, List[Tuple[str, str]]] = {}  # device_id -> [(block_id, port_name)]
        self._pending_sinks: Dict[str, List[Tuple[str, str]]] = {}    # device_id -> [(block_id, port_name)]
        self._pending_dsp_blocks: Dict[str, List[Tuple[str, Any]]] = {}  # device_id -> [(block_id, block)]
        self._init_lock = threading.Lock()
        self._initialized = True

    def register_device(
        self,
        device_id: str,
        chip_config: str,
        device_type: DeviceType = DeviceType.SIMULATOR,
    ) -> None:
        """
        Register a new device.

        Args:
            device_id: Unique identifier for this device
            chip_config: Path to chip configuration YAML
            device_type: Type of device (simulator or hardware)
        """
        with self._init_lock:
            if device_id in self._devices:
                # Re-registering is OK (flowgraph restart)
                # Reset state but keep config
                self._devices[device_id].is_initialized = False
                self._devices[device_id].is_running = False
                self._devices[device_id].chip = None
                self._devices[device_id].source_blocks.clear()
                self._devices[device_id].sink_blocks.clear()
                self._devices[device_id].dsp_blocks.clear()
                self._devices[device_id].connections.clear()
            else:
                self._devices[device_id] = DeviceInfo(
                    device_id=device_id,
                    chip_config=chip_config,
                    device_type=device_type,
                )

            # Process any pending registrations for this device
            device = self._devices[device_id]
            if device_id in self._pending_sources:
                for block_id, port_name in self._pending_sources[device_id]:
                    device.source_blocks.append(block_id)
                    self._block_to_device[block_id] = device_id
                del self._pending_sources[device_id]

            if device_id in self._pending_sinks:
                for block_id, port_name in self._pending_sinks[device_id]:
                    device.sink_blocks.append(block_id)
                    self._block_to_device[block_id] = device_id
                del self._pending_sinks[device_id]

            if device_id in self._pending_dsp_blocks:
                for item in self._pending_dsp_blocks[device_id]:
                    block_id, kyttar_block = item[0], item[1]
                    gr_block = item[2] if len(item) > 2 else None
                    device.dsp_blocks.append(kyttar_block)
                    if gr_block is not None:
                        device.impl_to_gr_block[id(kyttar_block)] = gr_block
                    self._block_to_device[block_id] = device_id
                del self._pending_dsp_blocks[device_id]

    def unregister_device(self, device_id: str) -> None:
        """Unregister a device and all associated blocks."""
        with self._init_lock:
            if device_id in self._devices:
                # Remove block-to-device mappings
                to_remove = [bid for bid, did in self._block_to_device.items()
                            if did == device_id]
                for bid in to_remove:
                    del self._block_to_device[bid]

                del self._devices[device_id]

    def register_source(
        self,
        block_id: str,
        device_id: str,
        port_name: str,
    ) -> None:
        """
        Register a source block with a device.

        Supports lazy registration - if the device doesn't exist yet,
        the registration is queued until the device is registered.

        Args:
            block_id: Unique ID of the source block
            device_id: Device to associate with
            port_name: Input port on the chip this source uses
        """
        with self._init_lock:
            if device_id not in self._devices:
                # Device doesn't exist yet - queue for later
                if device_id not in self._pending_sources:
                    self._pending_sources[device_id] = []
                self._pending_sources[device_id].append((block_id, port_name))
                self._block_to_device[block_id] = device_id
                return

            self._devices[device_id].source_blocks.append(block_id)
            self._block_to_device[block_id] = device_id

    def register_sink(
        self,
        block_id: str,
        device_id: str,
        port_name: str,
    ) -> None:
        """
        Register a sink block with a device.

        Supports lazy registration - if the device doesn't exist yet,
        the registration is queued until the device is registered.

        Args:
            block_id: Unique ID of the sink block
            device_id: Device to associate with
            port_name: Output port on the chip this sink uses
        """
        with self._init_lock:
            if device_id not in self._devices:
                # Device doesn't exist yet - queue for later
                if device_id not in self._pending_sinks:
                    self._pending_sinks[device_id] = []
                self._pending_sinks[device_id].append((block_id, port_name))
                self._block_to_device[block_id] = device_id
                return

            self._devices[device_id].sink_blocks.append(block_id)
            self._block_to_device[block_id] = device_id

    def register_dsp_block(
        self,
        block_id: str,
        device_id: str,
        kyttar_block: Any,
        gr_block: Any = None,
    ) -> None:
        """
        Register a DSP block (e.g., GainBlock) with a device.

        Supports lazy registration - if the device doesn't exist yet,
        the registration is queued until the device is registered.

        Args:
            block_id: Unique ID of the GRC block
            device_id: Device to associate with
            kyttar_block: KyttarBlock implementation instance
            gr_block: The GNURadio wrapper block (optional, for symbol lookup)
        """
        with self._init_lock:
            # Check if already registered
            if block_id in self._block_to_device:
                return  # Already registered

            if device_id not in self._devices:
                # Device doesn't exist yet - queue for later
                if device_id not in self._pending_dsp_blocks:
                    self._pending_dsp_blocks[device_id] = []
                self._pending_dsp_blocks[device_id].append((block_id, kyttar_block, gr_block))
                self._block_to_device[block_id] = device_id
                return

            self._devices[device_id].dsp_blocks.append(kyttar_block)
            if gr_block is not None:
                self._devices[device_id].impl_to_gr_block[id(kyttar_block)] = gr_block
            self._block_to_device[block_id] = device_id

    def register_block_symbol(
        self,
        device_id: str,
        gr_symbol: str,
        kyttar_block: Any,
    ) -> None:
        """
        Register a block's GNURadio symbol name for edge_list lookup.

        This is called by each DSP block during its first work() call,
        once symbol_name() returns a valid value.

        Args:
            device_id: Device ID
            gr_symbol: The GR symbol name (e.g., "kyttar_dc_blocker0")
            kyttar_block: The KyttarBlock implementation instance
        """
        with self._init_lock:
            if device_id in self._devices:
                self._devices[device_id].block_by_symbol[gr_symbol] = kyttar_block
                print(f"[registry] Registered symbol '{gr_symbol}' for device '{device_id}'")

    def set_edge_list(self, device_id: str, edge_list: str) -> None:
        """
        Store the GNURadio edge_list for topology discovery.

        The edge_list is obtained from top_block.edge_list() after start().

        Args:
            device_id: Device ID
            edge_list: The edge list string from GR (e.g., "block0:0->block1:0\\n...")
        """
        with self._init_lock:
            if device_id in self._devices:
                self._devices[device_id].gr_edge_list = edge_list
                print(f"[registry] Stored edge_list for device '{device_id}'")

    def establish_block_connections(self, device_id: str) -> bool:
        """
        Parse the GR edge_list and establish KyttarBlock connections.

        This uses the block_by_symbol map to convert GR block names to
        KyttarBlock instances, then calls connect_to() to establish
        the proper routing connections.

        Returns True if connections were successfully established.
        """
        with self._init_lock:
            if device_id not in self._devices:
                return False

            device = self._devices[device_id]
            if not device.gr_edge_list:
                print(f"[registry] No edge_list available for '{device_id}'")
                return False

            if not device.block_by_symbol:
                print(f"[registry] No block symbols registered for '{device_id}'")
                return False

            print(f"[registry] Parsing edge_list for '{device_id}':")
            print(f"  {device.gr_edge_list}")

            # Parse edge_list format: "block0:port->block1:port\n..."
            connections_made = 0
            for line in device.gr_edge_list.strip().split('\n'):
                if '->' not in line:
                    continue

                src_part, dst_part = line.split('->')
                src_symbol = src_part.split(':')[0]
                dst_symbol = dst_part.split(':')[0]

                # Look up in our symbol map
                src_block = device.block_by_symbol.get(src_symbol)
                dst_block = device.block_by_symbol.get(dst_symbol)

                if src_block is not None and dst_block is not None:
                    # Both are Kyttar blocks - establish connection
                    src_block.connect_to(dst_block)
                    print(f"[registry]   Connected: {src_block.name} -> {dst_block.name}")
                    connections_made += 1

            print(f"[registry] Established {connections_made} block connections")
            return connections_made > 0

    def set_connections(
        self,
        device_id: str,
        connections: List[Tuple[str, str, str, str]],
    ) -> None:
        """
        Set the connection topology for a device.

        Args:
            device_id: Device ID
            connections: List of (from_block, from_port, to_block, to_port) tuples
        """
        with self._init_lock:
            if device_id not in self._devices:
                raise ValueError(f"Unknown device: {device_id}")

            self._devices[device_id].connections = connections

    def get_device(self, device_id: str) -> Optional[DeviceInfo]:
        """Get device info by ID."""
        return self._devices.get(device_id)

    def get_device_for_block(self, block_id: str) -> Optional[DeviceInfo]:
        """Get the device that a block is associated with."""
        device_id = self._block_to_device.get(block_id)
        if device_id:
            return self._devices.get(device_id)
        return None

    def get_all_devices(self) -> List[DeviceInfo]:
        """Get all registered devices."""
        return list(self._devices.values())

    def set_chip(self, device_id: str, chip: Any) -> None:
        """Set the chip instance for a device."""
        with self._init_lock:
            if device_id in self._devices:
                self._devices[device_id].chip = chip
                self._devices[device_id].is_initialized = True

    def get_chip(self, device_id: str) -> Optional[Any]:
        """Get the chip instance for a device."""
        device = self._devices.get(device_id)
        if device:
            return device.chip
        return None

    def clear(self) -> None:
        """Clear all registrations (for testing or reset)."""
        with self._init_lock:
            self._devices.clear()
            self._block_to_device.clear()
            self._pending_sources.clear()
            self._pending_sinks.clear()
            self._pending_dsp_blocks.clear()

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton instance (for testing)."""
        with cls._lock:
            if cls._instance is not None:
                cls._instance.clear()
                cls._instance = None


# Global accessor
def get_registry() -> KyttarRegistry:
    """Get the global registry instance."""
    return KyttarRegistry()


def find_top_block_from_block(block) -> Optional[Any]:
    """
    Find the parent top_block by walking gc referrers.

    This is used to get access to the flowgraph's edge_list() method,
    which provides the connection topology between blocks.

    Args:
        block: A GNURadio block instance

    Returns:
        The parent top_block if found, None otherwise
    """
    import gc

    seen = set()
    to_check = [block]

    while to_check:
        obj = to_check.pop()
        if id(obj) in seen:
            continue
        seen.add(id(obj))

        # Check if this object has edge_list method (top_block signature)
        if hasattr(obj, 'edge_list') and callable(getattr(obj, 'edge_list')):
            return obj

        # Add referrers to check list
        for ref in gc.get_referrers(obj):
            if id(ref) not in seen:
                to_check.append(ref)

        # Safety limit to avoid infinite loops
        if len(seen) > 2000:
            break

    return None
