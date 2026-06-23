"""
Kyttar Demux (Splitter) Block

Routes incoming data to 1 of up to 3 output faces based on JUMP entry address.
This is a fundamental routing primitive for I/Q and multi-channel processing.

The input face cannot be used for output (would cause lockup), so maximum
3 output channels are supported.

Entry addresses for 3-way split:
- Channel 0: R1  (addresses 1-10)
- Channel 1: R11 (addresses 11-20)
- Channel 2: R21 (addresses 21-30)

For 2-way (I/Q) split:
- Channel 0 (I): R1
- Channel 1 (Q): R11

Face assignment is determined at placement time based on cell position and
where downstream blocks are located.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
import numpy as np

from .kyttar_block import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15
from .block import CellProgram


# Entry addresses for each channel (evenly split across 32 addresses)
CHANNEL_ENTRY_ADDRESSES = [1, 11, 21]

# Face encoding: South=0, East=1, West=2, North=3
FACE_SOUTH = 0
FACE_EAST = 1
FACE_WEST = 2
FACE_NORTH = 3


class DemuxInterface(BlockInterface):
    """
    Interface for demux block.

    Has multiple entry addresses (one per channel) and multiple output faces.
    Input register is shared across all channels.
    """

    def __init__(
        self,
        channel_entries: Optional[List[int]] = None,
        input_registers: Optional[List[int]] = None,
        output_registers: Optional[List[int]] = None,
        channel_faces: Optional[List[Optional[int]]] = None,
        channel_targets: Optional[List[Optional[BlockInterface]]] = None,
        channel_hops: Optional[List[int]] = None,
    ):
        # Don't call super().__init__() - we override all fields
        self.channel_entries = channel_entries if channel_entries is not None else [1, 11, 21]
        self.input_registers = input_registers if input_registers is not None else [31]
        self.output_registers = output_registers if output_registers is not None else [31]

        # Output faces for each channel (set by placer based on routing)
        # None means not yet assigned
        self.channel_faces = channel_faces if channel_faces is not None else [None, None, None]

        # Target interfaces for each channel output
        self.channel_targets = channel_targets if channel_targets is not None else [None, None, None]

        # Hop counts to reach each channel's target
        self.channel_hops = channel_hops if channel_hops is not None else [1, 1, 1]

    @property
    def entry_address(self) -> int:
        """Return first channel entry for compatibility."""
        return self.channel_entries[0]

    @property
    def entry_addresses(self) -> List[int]:
        """Return all channel entry addresses."""
        return self.channel_entries


class DemuxBlock(KyttarBlock):
    """
    Demultiplexer block - routes data to different output faces based on entry address.

    When a JUMP arrives at entry R1, data is routed to channel 0's output face.
    When a JUMP arrives at entry R11, data is routed to channel 1's output face.
    When a JUMP arrives at entry R21, data is routed to channel 2's output face.

    The output faces are determined at placement time based on where downstream
    blocks are located. The placer assigns faces to avoid routing back to the
    input face (which would cause lockup).

    For I/Q processing:
    - Source sends I samples with JUMP to R1
    - Source sends Q samples with JUMP to R11
    - Demux routes I to one face, Q to another face
    """

    def __init__(self, name: str, num_channels: int = 2):
        """
        Initialize demux block.

        Args:
            name: Block name
            num_channels: Number of output channels (2 for I/Q, up to 3)
        """
        if num_channels < 2 or num_channels > 3:
            raise ValueError("num_channels must be 2 or 3")

        super().__init__(name, num_channels=num_channels)
        self._num_channels = num_channels

        # Create interface with channel entries
        self._interface = DemuxInterface(
            channel_entries=CHANNEL_ENTRY_ADDRESSES[:num_channels],
            channel_faces=[None] * num_channels,
            channel_targets=[None] * num_channels,
            channel_hops=[1] * num_channels,
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
    def interface(self) -> DemuxInterface:
        return self._interface

    def set_channel_routing(
        self,
        channel: int,
        face: int,
        hop_count: int,
        target_interface: Optional[BlockInterface] = None,
    ):
        """
        Set routing for a channel (called by placer).

        Args:
            channel: Channel index (0, 1, or 2)
            face: Output face for this channel (0=S, 1=E, 2=W, 3=N)
            hop_count: Hop count to reach target
            target_interface: Interface of target block
        """
        if channel >= self._num_channels:
            raise ValueError(f"Channel {channel} out of range for {self._num_channels}-channel demux")

        self._interface.channel_faces[channel] = face
        self._interface.channel_hops[channel] = hop_count
        self._interface.channel_targets[channel] = target_interface

    def build_cell_programs(
        self,
        output_hop: int = 1,
        target_interface: Optional[BlockInterface] = None,
    ) -> Dict[int, CellProgram]:
        """
        Build the demux cell program.

        Note: output_hop and target_interface are ignored for demux - use
        set_channel_routing() to configure per-channel routing.
        """
        prog = CellProgram()

        input_reg = self._interface.input_registers[0]  # R31

        # Build assembly for each channel
        lines = [
            f"; Demux block - {self._num_channels} channel splitter",
            f"; Input: R{input_reg}",
            f"; Channel entries: {self._interface.channel_entries[:self._num_channels]}",
            "",
        ]

        for ch in range(self._num_channels):
            entry = self._interface.channel_entries[ch]
            face = self._interface.channel_faces[ch]
            hop = self._interface.channel_hops[ch]
            target = self._interface.channel_targets[ch]

            # Use default faces if not configured: East for ch0, South for ch1, West for ch2
            if face is None:
                default_faces = [FACE_EAST, FACE_SOUTH, FACE_WEST]
                face = default_faces[ch]
                self._interface.channel_faces[ch] = face

            # Get target interface or use defaults
            if target is None:
                target = BlockInterface()
            target_input = target.input_registers[0] if target.input_registers else 31
            target_entry = target.entry_address

            lines.extend([
                f"; === Channel {ch}: Entry R{entry}, Face={face}, Hop={hop} ===",
                f".org {entry}",
                f"ch{ch}_entry:",
                f"    MOVE R0, #{face}",       # Load face value
                f"    MOVE [FACE], R0",        # Set OUT_FACE register
                f"    MOVE R0, R{input_reg}",  # Get input data
                f"    WRITE @{hop}, {target_input}",  # Forward data
                f"    JUMP @{hop}, {target_entry}",   # Trigger downstream
                f"    HALT",
                "",
            ])

        assembly = "\n".join(lines)

        # Assemble each channel's code at its entry address
        # We need to assemble separately for each .org section
        #
        # Memory layout for each channel:
        # entry+0: MOVE R0, Rconst   ; Load face constant from Rconst
        # entry+1: MOVE [FACE], R0   ; Set output direction
        # entry+2: MOVE R0, R31      ; Get input data
        # entry+3: WRITE @hop, addr  ; Forward data
        # entry+4: JUMP @hop, addr   ; Trigger downstream
        # entry+5: HALT
        # entry+6: DW face           ; Face constant value (Rconst = entry+6)
        #
        # Note: Each channel uses 7 words. With entries at R1, R11, R21:
        # Channel 0: R1-R7 (entry=1, const at R7)
        # Channel 1: R11-R17 (entry=11, const at R17)
        # Channel 2: R21-R27 (entry=21, const at R27)

        for ch in range(self._num_channels):
            entry = self._interface.channel_entries[ch]
            face = self._interface.channel_faces[ch]
            hop = self._interface.channel_hops[ch]
            target = self._interface.channel_targets[ch]

            if target is None:
                target = BlockInterface()
            target_input = target.input_registers[0] if target.input_registers else 31
            target_entry = target.entry_address

            # Store face constant 6 words after entry
            const_reg = entry + 6

            ch_assembly = f"""; Channel {ch} code - Entry R{entry}, Face={face}
    MOVE R0, R{const_reg}
    MOVE [FACE], R0
    MOVE R0, R{input_reg}
    WRITE @{hop}, {target_input}
    JUMP @{hop}, {target_entry}
    HALT
    DW {face}
"""
            words = assemble_to_words(ch_assembly, base_addr=entry)
            # Don't use set_program for channels > 0 to avoid overwriting entry_addr
            if ch == 0:
                prog.set_program(entry, words)
            else:
                # Just load the memory without changing entry_addr
                for i, word in enumerate(words):
                    prog.memory[entry + i] = word

        return {0: prog}

    def process_reference(self, input_samples: np.ndarray) -> np.ndarray:
        """
        Reference implementation - just passes through.

        In actual hardware, this routes to different outputs based on entry.
        For testing, we just pass through as the routing is tested separately.
        """
        return input_samples.copy()


# Convenience function to create I/Q demux
def create_iq_demux(name: str = "iq_demux") -> DemuxBlock:
    """Create a 2-channel demux for I/Q splitting."""
    return DemuxBlock(name=name, num_channels=2)
