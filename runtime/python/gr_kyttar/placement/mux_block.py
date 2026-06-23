"""
Kyttar Mux (Combiner) Block

Collects data from multiple channels and forwards to a single output,
preserving channel identity via WRITE entry addresses.

This is the counterpart to DemuxBlock - it recombines split data streams
back into a single output path.

The mux uses MULTIPLE ENTRY POINTS to identify channels:
- Entry R1 (ch0, I channel) → WRITE with addr=1
- Entry R11 (ch1, Q channel) → WRITE with addr=11
- Entry R21 (ch2) → WRITE with addr=21

Each entry point has a short program that:
1. Sets the output face
2. Gets data from input register (R31)
3. WRITEs data with channel-specific address to output
4. HALTs

The channel tag is carried in the WRITE instruction's dest field,
which is captured at the output port for channel identification by the sink.

This is essential for async operation where I and Q paths may complete
in different order - the sink needs to know which channel each sample
belongs to.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional
import numpy as np

from .kyttar_block import KyttarBlock, BlockInterface, assemble_to_words
from .block import CellProgram


# Entry addresses for each channel (matches demux)
CHANNEL_ENTRY_ADDRESSES = [1, 11, 21]

# Face encoding
FACE_SOUTH = 0
FACE_EAST = 1
FACE_WEST = 2
FACE_NORTH = 3


class MuxInterface(BlockInterface):
    """
    Interface for mux block.

    Multiple entry points - one per channel. The entry address itself identifies
    the channel, so no IN_FACE checking is needed.

    Entry addresses (matching CHANNEL_ENTRY_ADDRESSES):
    - R1: Channel 0 (I)
    - R11: Channel 1 (Q)
    - R21: Channel 2 (if 3-channel mode)
    """

    def __init__(
        self,
        input_registers: Optional[List[int]] = None,
        output_registers: Optional[List[int]] = None,
        output_face: Optional[int] = None,
        output_hop: int = 1,
        target_interface: Optional[BlockInterface] = None,
        num_channels: int = 2,
    ):
        self.input_registers = input_registers if input_registers is not None else [31]
        self.output_registers = output_registers if output_registers is not None else [31]

        # Output face (set by placer)
        self.output_face = output_face

        # Output hop count
        self.output_hop = output_hop

        # Target interface for output
        self.target_interface = target_interface

        # Number of channels
        self._num_channels = num_channels

    @property
    def entry_address(self) -> int:
        """Primary entry address (channel 0)."""
        return CHANNEL_ENTRY_ADDRESSES[0]

    @property
    def channel_entry_addresses(self) -> List[int]:
        """Entry addresses for each channel."""
        return CHANNEL_ENTRY_ADDRESSES[:self._num_channels]

    @property
    def channel_output_entries(self) -> List[int]:
        """Entry addresses for each channel output (same as input for mux)."""
        return CHANNEL_ENTRY_ADDRESSES[:self._num_channels]

    def get_channel_interface(self, channel: int) -> 'ChannelInterface':
        """
        Get a virtual interface for a specific channel.

        This returns an interface object that upstream blocks can use as their
        target_interface. The key difference is that entry_address will be
        the channel-specific entry (R1, R11, or R21) instead of just R1.

        This is essential for I/Q routing: when a DSP block JUMPs to the mux,
        it needs to JUMP to the correct channel entry so the mux can identify
        the channel.
        """
        if channel >= self._num_channels:
            raise ValueError(f"Channel {channel} out of range for {self._num_channels}-channel mux")
        return ChannelInterface(
            entry_address=CHANNEL_ENTRY_ADDRESSES[channel],
            input_registers=self.input_registers,
            output_registers=self.output_registers,
        )


class ChannelInterface(BlockInterface):
    """
    Virtual interface representing a single channel of a multi-channel block.

    Used to provide channel-specific entry addresses to upstream blocks.
    """

    def __init__(
        self,
        entry_address: int,
        input_registers: Optional[List[int]] = None,
        output_registers: Optional[List[int]] = None,
    ):
        self._entry_address = entry_address
        self.input_registers = input_registers if input_registers is not None else [31]
        self.output_registers = output_registers if output_registers is not None else [31]

    @property
    def entry_address(self) -> int:
        return self._entry_address


class MuxBlock(KyttarBlock):
    """
    Multiplexer block - collects data from multiple channels, outputs to one.

    Uses MULTIPLE ENTRY POINTS to identify channels:
    - Data arriving at R1 → channel 0 (I) → WRITE with addr=1
    - Data arriving at R11 → channel 1 (Q) → WRITE with addr=11
    - Data arriving at R21 → channel 2 → WRITE with addr=21

    Each entry point has a simple 4-instruction program:
    1. Set output FACE
    2. Get data from input register (R31)
    3. WRITE data with channel-specific address
    4. HALT

    The channel tag is carried in the WRITE instruction's dest field,
    which is captured at the output port for channel identification by the sink.
    """

    def __init__(self, name: str, num_channels: int = 2):
        """
        Initialize mux block.

        Args:
            name: Block name
            num_channels: Number of input channels (2 for I/Q, up to 3)
        """
        if num_channels < 2 or num_channels > 3:
            raise ValueError("num_channels must be 2 or 3")

        super().__init__(name, num_channels=num_channels)
        self._num_channels = num_channels

        self._interface = MuxInterface(
            num_channels=num_channels,
        )

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def cells_used(self) -> int:
        """Alias for cell_count for API compatibility."""
        return self.cell_count

    @property
    def num_channels(self) -> int:
        return self._num_channels

    @property
    def interface(self) -> MuxInterface:
        return self._interface

    def set_output_routing(
        self,
        face: int,
        hop_count: int,
        target_interface: Optional[BlockInterface] = None,
    ):
        """
        Set output routing (called by placer).

        Args:
            face: Output face (0=S, 1=E, 2=W, 3=N)
            hop_count: Hop count to reach target
            target_interface: Interface of target block
        """
        self._interface.output_face = face
        self._interface.output_hop = hop_count
        self._interface.target_interface = target_interface

    def build_cell_programs(
        self,
        output_hop: int = 1,
        target_interface: Optional[BlockInterface] = None,
    ) -> Dict[int, CellProgram]:
        """
        Build the mux cell program with multiple entry points.

        Each channel has its own entry point, so no IN_FACE checking is needed.
        The entry address itself identifies the channel.

        Memory layout:
        - R1-R4: Channel 0 (I) handler
        - R11-R14: Channel 1 (Q) handler
        - R21-R24: Channel 2 handler (if 3-channel mode)
        - R30: Output face constant (shared)

        Each channel handler (4 instructions):
        entry+0: MOVE R0, R30            ; Load output face constant
        entry+1: MOVE [FACE], R0         ; Set OUT_FACE
        entry+2: MOVE R0, R31            ; Get data from input register
        entry+3: WRITE @hop, addr        ; WRITE with channel address (addr=1, 11, or 21)
        entry+4: HALT

        The WRITE instruction's dest field carries the channel tag,
        which is captured at the output port for channel identification by the sink.
        """
        prog = CellProgram()

        # Use instance settings if not overridden
        if self._interface.output_face is not None:
            out_face = self._interface.output_face
            out_hop = self._interface.output_hop
        else:
            # Fallback to parameters (for simple cases)
            out_face = FACE_EAST  # Default
            out_hop = output_hop

        input_reg = self._interface.input_registers[0]  # R31
        const_face_reg = 30  # Shared output face constant

        # Store output face constant at R30
        prog.memory[const_face_reg] = out_face

        # Build handler for each channel
        for channel in range(self._num_channels):
            entry = CHANNEL_ENTRY_ADDRESSES[channel]  # 1, 11, or 21
            channel_addr = entry  # Use same address for WRITE tag

            # Build assembly for this channel's handler
            lines = [
                f"; Mux channel {channel} handler at R{entry}",
                f"; Output face: {out_face}, hop: {out_hop}",
                f"; WRITE address (channel tag): {channel_addr}",
                "",
                "start:",
                f"    MOVE R0, R{const_face_reg}",    # Load output face
                f"    MOVE [FACE], R0",               # Set OUT_FACE
                f"    MOVE R0, R{input_reg}",         # Get input data
                f"    WRITE @{out_hop}, {channel_addr}",  # WRITE with channel address
                f"    HALT",
            ]

            assembly = "\n".join(lines)
            words = assemble_to_words(assembly, base_addr=entry)

            # Store the assembled code
            for offset, word in enumerate(words):
                prog.memory[entry + offset] = word

        return {0: prog}

    def process_reference(self, input_samples: np.ndarray) -> np.ndarray:
        """
        Reference implementation - just passes through.

        In actual hardware, this preserves channel tags. For testing,
        we just pass through as the tagging is tested separately.
        """
        return input_samples.copy()


# Convenience function to create I/Q mux
def create_iq_mux(name: str = "iq_mux") -> MuxBlock:
    """Create a 2-channel mux for I/Q combining."""
    return MuxBlock(name=name, num_channels=2)
