"""placeKYT-bitstream Device Block for GNURadio.

Unlike :class:`kyttar_device` (which AUTO-places GNURadio-discovered DSP
blocks), this block loads a HAND-PLACED design built by placeKYT — a ``.kbs``
bitstream that carries both the per-chip words AND the host-side port config
(input port + entry_addr/hop_count/data_addr, output port) in its metadata.

It programs a fresh chip via ``load_bitstream_physical`` and configures the
input port, then registers the chip so the existing :class:`kyttar_source` /
:class:`kyttar_sink` blocks stream through it UNCHANGED (chip.write_port /
read_port / run_until_output).

Reads the ``.kbs`` with a small self-contained parser (the documented binary
format) so this block does NOT import placeKYT — keeping it clean across the
GNURadio (NumPy 1.x) / placeKYT-venv (NumPy 2.x) split.

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

import json
import struct
from pathlib import Path
from typing import Any, List, Optional, Tuple

from gnuradio import gr

from .registry import DeviceType, get_registry

_KBS_MAGIC = b"KYTBS\x00\x00\x00"


def _read_kbs(path: str) -> Tuple[List[List[int]], dict]:
    """Minimal pure-Python reader for placeKYT's .kbs (see placekyt/engine/io/
    kbs.py for the authoritative format). Returns (per_chip_words, metadata).
    Validates magic + lengths; ignores the trailing per-chip CRC (corruption
    check only)."""
    data = Path(path).read_bytes()
    if data[:8] != _KBS_MAGIC:
        raise ValueError(f"{path}: not a .kbs file (bad magic)")
    version, chip_count = struct.unpack_from("<HH", data, 8)
    off = 16  # header is 16 bytes
    meta_len = struct.unpack_from("<I", data, off)[0]
    off += 4
    metadata = {}
    if meta_len:
        metadata = json.loads(data[off:off + meta_len].decode("utf-8"))
        off += meta_len
    chips: List[List[int]] = []
    for _ in range(chip_count):
        _type_hash, word_count = struct.unpack_from("<II", data, off)
        off += 8
        words = list(struct.unpack_from(f"<{word_count}H", data, off))
        off += word_count * 2
        off += 4  # skip the per-chip CRC32
        chips.append(words)
    return chips, metadata


class placekyt_device(gr.basic_block):
    """Loads a placeKYT ``.kbs`` build and exposes it as a Kyttar device.

    Parameters:
        device_id:   unique id shared with the source/sink blocks
        kbs_path:    path to the placeKYT-built .kbs bitstream
        chip_config: chip-type YAML (for Chip.from_chip_type); must match the
                     type the .kbs was built for
    """

    def __init__(
        self,
        device_id: str = "kyttar_0",
        kbs_path: str = "",
        chip_config: str = "",
    ):
        gr.basic_block.__init__(
            self, name="placeKYT Device", in_sig=[], out_sig=[])
        self._device_id = device_id
        self._kbs_path = kbs_path
        self._chip_config = chip_config
        self._initialized = False
        self._io: dict = {}

        registry = get_registry()
        registry.register_device(
            device_id=device_id,
            chip_config=chip_config,
            device_type=DeviceType.SIMULATOR,
        )
        print(f"[placeKYT-Device] Registered '{device_id}' kbs='{kbs_path}'")
        # Program the chip eagerly at construction. A no-signal-port block is
        # NOT guaranteed to be in the flowgraph graph, so GNURadio may never call
        # its start() — programming here ensures the chip is ready before the
        # source/sink work() loops run. (Re-programs on start() too, for restart.)
        try:
            self._initialize()
        except Exception as e:  # noqa: BLE001
            print(f"[placeKYT-Device] WARN: deferred init (will retry on start): {e}")

    def start(self) -> bool:
        try:
            if not self._initialized:
                self._initialize()
            return True
        except Exception as e:  # noqa: BLE001
            print(f"[placeKYT-Device] ERROR during init: {e}")
            import traceback
            traceback.print_exc()
            return False

    def stop(self) -> bool:
        self._initialized = False
        return True

    def _initialize(self) -> None:
        import simkyt

        chips, metadata = _read_kbs(self._kbs_path)
        if not chips:
            raise RuntimeError(f"{self._kbs_path}: no chips in bitstream")
        self._io = metadata.get("io", {})
        print(f"[placeKYT-Device] Loaded {len(chips)} chip(s), "
              f"{len(chips[0])} words; io={self._io}")

        chip_type_obj = simkyt.ChipType.from_yaml(self._chip_config)
        chip = simkyt.Chip.from_chip_type(chip_type_obj)
        chip.load_bitstream_physical(chips[0])  # single-chip for now

        # Configure the input port from the .kbs io metadata so injected samples
        # reach the first block (the same config placeKYT's own sim uses).
        in_port = self._io.get("input_port")
        if in_port is not None:
            chip.set_port_entry_address(in_port, int(self._io["entry_addr"]))
            chip.set_port_target_hop_count(in_port, int(self._io["hop_count"]))
            chip.set_port_target_data_address(in_port, int(self._io["data_addr"]))
            print(f"[placeKYT-Device] input '{in_port}': entry="
                  f"{self._io['entry_addr']} hop={self._io['hop_count']} "
                  f"data_addr={self._io['data_addr']}")

        get_registry().set_chip(self._device_id, chip)
        self._initialized = True
        print("[placeKYT-Device] Initialization complete.")

    def general_work(self, input_items, output_items):
        return 0

    # Accessors mirroring kyttar_device.
    def get_chip(self) -> Optional[Any]:
        return get_registry().get_chip(self._device_id)

    def get_device_id(self) -> str:
        return self._device_id

    def is_initialized(self) -> bool:
        return self._initialized

    def io_config(self) -> dict:
        """The host-side port config from the .kbs (input/output port names etc.)."""
        return dict(self._io)
