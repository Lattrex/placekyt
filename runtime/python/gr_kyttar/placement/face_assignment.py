"""
Face Assignment for Kyttar Placement/Routing

Assigns output faces to demux channels and input faces to mux channels
based on the relative positions of connected blocks.

This module handles the spatial routing decisions for branching topologies.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional, Set

from .graph import (
    BlockGraph, BlockEdge,
    is_demux_block, is_mux_block,
    get_face_direction, OPPOSITE_FACE,
    FACE_SOUTH, FACE_EAST, FACE_WEST, FACE_NORTH,
)


class FaceAssignmentError(Exception):
    """Raised when face assignment fails."""
    pass


@dataclass
class FaceAssignment:
    """
    Stores face assignments for demux/mux blocks and routing cells.

    Attributes:
        demux_faces: block_name -> {channel -> output_face}
        mux_input_faces: block_name -> {input_face -> channel}
        mux_output_faces: block_name -> output_face
        routing_faces: (col, row) -> forward_face
    """
    demux_faces: Dict[str, Dict[int, int]] = field(default_factory=dict)
    mux_input_faces: Dict[str, Dict[int, int]] = field(default_factory=dict)
    mux_output_faces: Dict[str, int] = field(default_factory=dict)
    routing_faces: Dict[Tuple[int, int], int] = field(default_factory=dict)

    def set_demux_channel_face(self, block_name: str, channel: int, face: int):
        """Set the output face for a demux channel."""
        if block_name not in self.demux_faces:
            self.demux_faces[block_name] = {}
        self.demux_faces[block_name][channel] = face

    def get_demux_channel_face(self, block_name: str, channel: int) -> Optional[int]:
        """Get the output face for a demux channel."""
        return self.demux_faces.get(block_name, {}).get(channel)

    def set_mux_face_channel(self, block_name: str, face: int, channel: int):
        """Set the input face for a mux channel."""
        if block_name not in self.mux_input_faces:
            self.mux_input_faces[block_name] = {}
        self.mux_input_faces[block_name][face] = channel

    def get_mux_face_channels(self, block_name: str) -> List[Tuple[int, int]]:
        """Get all (face, channel) pairs for a mux."""
        return list(self.mux_input_faces.get(block_name, {}).items())

    def set_mux_output_face(self, block_name: str, face: int):
        """Set the output face for a mux."""
        self.mux_output_faces[block_name] = face

    def get_mux_output_face(self, block_name: str) -> Optional[int]:
        """Get the output face for a mux."""
        return self.mux_output_faces.get(block_name)

    def set_routing_face(self, pos: Tuple[int, int], face: int):
        """Set the forward face for a routing cell."""
        self.routing_faces[pos] = face

    def get_routing_face(self, pos: Tuple[int, int]) -> Optional[int]:
        """Get the forward face for a routing cell."""
        return self.routing_faces.get(pos)


def get_input_face_from_position(
    block_pos: Tuple[int, int],
    src_pos: Tuple[int, int],
) -> int:
    """
    Get the face from which data arrives at a block from a source position.

    The input face is opposite to the direction from src to dst.

    Args:
        block_pos: Position of the receiving block
        src_pos: Position of the sending block

    Returns:
        Input face at block_pos
    """
    direction = get_face_direction(src_pos, block_pos)
    if direction is None:
        # Same position - this shouldn't happen, default to South
        return FACE_SOUTH
    # Input face is opposite to the direction data travels
    return OPPOSITE_FACE[direction]


def assign_faces(
    block_graph: BlockGraph,
    block_positions: Dict[str, Tuple[int, int]],
    input_port_pos: Optional[Tuple[int, int]] = None,
    output_port_pos: Optional[Tuple[int, int]] = None,
    blocked_cells: Optional[Set[Tuple[int, int]]] = None,
) -> FaceAssignment:
    """
    Assign faces for all demux/mux blocks based on block positions.

    For demux blocks:
    - Determine input face from upstream block position
    - Assign output faces for each channel based on downstream block positions
    - Verify no output face conflicts with input face

    For mux blocks:
    - Determine output face from downstream block position
    - Assign input faces for each channel based on upstream block positions
    - Verify no input face conflicts with output face

    Args:
        block_graph: Block connectivity graph with channel information
        block_positions: Map of block name to (col, row) position
        input_port_pos: Position of the chip input port (used when demux has no
            upstream block in the graph to determine input face correctly)
        output_port_pos: Position of the chip output port (used when mux has no
            downstream block in the graph to determine output face correctly)

    Returns:
        FaceAssignment with all face mappings

    Raises:
        FaceAssignmentError: If faces cannot be assigned (e.g., conflict)
    """
    assignment = FaceAssignment()

    for block_name, block in block_graph.nodes.items():
        pos = block_positions.get(block_name)
        if pos is None:
            continue

        if is_demux_block(block):
            _assign_demux_faces(
                block_name, block, pos,
                block_graph, block_positions, assignment,
                input_port_pos=input_port_pos,
                blocked_cells=blocked_cells,
            )
        elif is_mux_block(block):
            _assign_mux_faces(
                block_name, block, pos,
                block_graph, block_positions, assignment,
                output_port_pos=output_port_pos,
            )

    return assignment


def _assign_demux_faces(
    block_name: str,
    block,
    pos: Tuple[int, int],
    block_graph: BlockGraph,
    block_positions: Dict[str, Tuple[int, int]],
    assignment: FaceAssignment,
    input_port_pos: Optional[Tuple[int, int]] = None,
    blocked_cells: Optional[Set[Tuple[int, int]]] = None,
):
    """
    Assign output faces for a demux block.

    The demux routes different channels to different output faces.
    We determine which face based on where each downstream block is located.

    Uses a two-phase approach like mux assignment to handle conflicts properly.

    Args:
        blocked_cells: Optional set of cell positions that cannot be used as outputs
            (e.g., input_route cells). If a face leads to a blocked cell, that
            face will not be selected as an alternative.
    """
    # Compute which faces lead to blocked cells
    blocked_faces: Set[int] = set()
    if blocked_cells:
        face_deltas = {
            FACE_SOUTH: (0, 1),
            FACE_EAST: (1, 0),
            FACE_WEST: (-1, 0),
            FACE_NORTH: (0, -1),
        }
        for face, (dx, dy) in face_deltas.items():
            adj_pos = (pos[0] + dx, pos[1] + dy)
            if adj_pos in blocked_cells:
                blocked_faces.add(face)
                print(f"[face_assignment] Demux '{block_name}': face {face} blocked (leads to {adj_pos})")
    # Determine input face (from upstream block).
    # The input face is reserved to prevent output face conflicts.
    inputs = block_graph.get_inputs(block_name)
    input_face = None
    if inputs:
        # Get the first (should be only) upstream block
        upstream = inputs[0]
        upstream_pos = block_positions.get(upstream.src_block)
        if upstream_pos:
            input_face = get_input_face_from_position(pos, upstream_pos)
    # When the demux is the entry point (no upstream block), we do NOT
    # reserve an input face. Data enters via hop_cnt-based routing which
    # may arrive from any face depending on the actual routing path.
    # The input face from the port position would be unreliable since
    # the routing path may detour around other blocks.

    print(f"[face_assignment] Demux '{block_name}' at {pos}: input_face={input_face}")

    # Get all outgoing edges
    outputs = block_graph.get_outputs(block_name)
    print(f"[face_assignment] Demux '{block_name}' outputs: {[(e.dst_block, e.channel) for e in outputs]}")

    # Phase 1: Collect natural face assignments for all outputs
    natural_faces: List[Tuple[BlockEdge, int]] = []  # [(edge, natural_face), ...]
    for edge in outputs:
        dst_pos = block_positions.get(edge.dst_block)
        if dst_pos is None:
            print(f"[face_assignment]   Edge to '{edge.dst_block}' ch{edge.channel}: NO POSITION")
            continue

        # Determine the natural direction to the downstream block
        output_face = get_face_direction(pos, dst_pos)
        if output_face is None:
            raise FaceAssignmentError(
                f"Demux '{block_name}' channel {edge.channel}: "
                f"destination '{edge.dst_block}' at same position"
            )
        print(f"[face_assignment]   Edge to '{edge.dst_block}' at {dst_pos} ch{edge.channel}: natural_face={output_face}")
        natural_faces.append((edge, output_face))

    # Phase 2: Assign faces, resolving conflicts
    assigned_faces: Dict[int, int] = {}  # channel -> face
    reserved_for_input = {input_face}

    # Collect all natural faces that don't conflict with input
    natural_face_set = set()
    for edge, natural_face in natural_faces:
        if natural_face != input_face:
            natural_face_set.add(natural_face)

    # First pass: assign outputs that can use their natural face
    remaining = []
    for edge, natural_face in natural_faces:
        if natural_face == input_face:
            # This output conflicts with input, needs reassignment
            remaining.append((edge, natural_face))
        elif natural_face in assigned_faces.values():
            # This face was already assigned to another channel
            remaining.append((edge, natural_face))
        else:
            # Can use natural face
            assigned_faces[edge.channel] = natural_face
            print(f"[face_assignment]   Channel {edge.channel}: assigned natural face {natural_face}")

    # Second pass: find alternatives for conflicting outputs
    used_faces = reserved_for_input | set(assigned_faces.values()) | blocked_faces

    for edge, natural_face in remaining:
        dst_pos = block_positions.get(edge.dst_block)
        # Find an alternative that doesn't conflict with any used/blocked face
        reserved = used_faces | natural_face_set | blocked_faces
        alt_face = _find_alternative_face(pos, dst_pos, reserved)
        if alt_face is None:
            # Try with just used_faces (allow taking another's natural face, but not blocked)
            alt_face = _find_alternative_face(pos, dst_pos, used_faces | blocked_faces)
        if alt_face is None:
            raise FaceAssignmentError(
                f"Demux '{block_name}' channel {edge.channel}: "
                f"cannot find available face for output to '{edge.dst_block}' "
                f"(natural={natural_face}, input={input_face}, used={used_faces})"
            )
        assigned_faces[edge.channel] = alt_face
        used_faces.add(alt_face)
        print(f"[face_assignment]   Channel {edge.channel}: assigned alternative face {alt_face} (natural was {natural_face})")

    # Store assignments
    for channel, face in assigned_faces.items():
        assignment.set_demux_channel_face(block_name, channel, face)


def _assign_mux_faces(
    block_name: str,
    block,
    pos: Tuple[int, int],
    block_graph: BlockGraph,
    block_positions: Dict[str, Tuple[int, int]],
    assignment: FaceAssignment,
    output_port_pos: Optional[Tuple[int, int]] = None,
):
    """
    Assign input faces for a mux block.

    The mux receives different channels from different input faces.
    We determine which face based on where each upstream block is located.

    Uses a two-phase approach:
    1. Collect all natural face assignments
    2. Resolve conflicts by reassigning inputs that conflict with output or other inputs
    """
    # First, determine output face (to downstream block or output port)
    outputs = block_graph.get_outputs(block_name)
    if outputs:
        downstream = outputs[0]
        downstream_pos = block_positions.get(downstream.dst_block)
        if downstream_pos:
            output_face = get_face_direction(pos, downstream_pos)
            if output_face is None:
                output_face = FACE_EAST  # Default: output to East
        else:
            # No downstream block position - use output port if available
            if output_port_pos is not None:
                output_face = get_face_direction(pos, output_port_pos)
                if output_face is None:
                    output_face = FACE_EAST
            else:
                output_face = FACE_EAST
    elif output_port_pos is not None:
        # No downstream block in graph - route to output port
        output_face = get_face_direction(pos, output_port_pos)
        if output_face is None:
            output_face = FACE_EAST
    else:
        output_face = FACE_EAST  # Default: output to East

    assignment.set_mux_output_face(block_name, output_face)
    print(f"[face_assignment] Mux '{block_name}' at {pos}: output_face={output_face}")

    # Get all incoming edges
    inputs = block_graph.get_inputs(block_name)
    print(f"[face_assignment] Mux '{block_name}' inputs: {[(e.src_block, e.channel) for e in inputs]}")

    # Phase 1: Collect natural face assignments for all inputs
    natural_faces: List[Tuple[BlockEdge, int]] = []  # [(edge, natural_face), ...]
    for edge in inputs:
        src_pos = block_positions.get(edge.src_block)
        if src_pos is None:
            print(f"[face_assignment]   Edge from '{edge.src_block}' ch{edge.channel}: NO POSITION")
            continue

        # Determine the natural face from which data arrives
        input_face = get_input_face_from_position(pos, src_pos)
        print(f"[face_assignment]   Edge from '{edge.src_block}' at {src_pos} ch{edge.channel}: natural_face={input_face}")
        natural_faces.append((edge, input_face))

    # Phase 2: Assign faces, resolving conflicts
    # Reserved faces: output face + all natural faces that don't need reassignment
    assigned_faces: Dict[int, int] = {}  # channel -> face
    reserved_for_output = {output_face}

    # First pass: assign inputs that can use their natural face
    # (not conflicting with output face)
    remaining = []
    natural_face_set = set()
    for edge, natural_face in natural_faces:
        if natural_face != output_face:
            natural_face_set.add(natural_face)

    for edge, natural_face in natural_faces:
        if natural_face == output_face:
            # This input conflicts with output, needs reassignment
            remaining.append((edge, natural_face))
        elif natural_face in assigned_faces.values():
            # This face was already assigned to another input
            remaining.append((edge, natural_face))
        else:
            # Can use natural face
            assigned_faces[edge.channel] = natural_face
            print(f"[face_assignment]   Channel {edge.channel}: assigned natural face {natural_face}")

    # Second pass: find alternatives for conflicting inputs
    used_faces = reserved_for_output | set(assigned_faces.values())

    for edge, natural_face in remaining:
        src_pos = block_positions.get(edge.src_block)
        # Find an alternative that doesn't conflict with any used face
        # AND doesn't conflict with any remaining natural face we haven't assigned yet
        reserved = used_faces | natural_face_set
        alt_face = _find_alternative_input_face(pos, src_pos, reserved)
        if alt_face is None:
            # Try with just used_faces (allow taking another's natural face)
            alt_face = _find_alternative_input_face(pos, src_pos, used_faces)
        if alt_face is None:
            raise FaceAssignmentError(
                f"Mux '{block_name}' channel {edge.channel}: "
                f"cannot find available face for input from '{edge.src_block}' "
                f"(natural={natural_face}, output={output_face}, used={used_faces})"
            )
        assigned_faces[edge.channel] = alt_face
        used_faces.add(alt_face)
        print(f"[face_assignment]   Channel {edge.channel}: assigned alternative face {alt_face} (natural was {natural_face})")

    # Store assignments
    for channel, face in assigned_faces.items():
        assignment.set_mux_face_channel(block_name, face, channel)


def _find_alternative_face(
    from_pos: Tuple[int, int],
    to_pos: Tuple[int, int],
    used_faces: Set[int],
) -> Optional[int]:
    """
    Find an alternative face when the direct path is blocked.

    For demux output: we need a face that can eventually route to to_pos.
    We prefer faces that are in the general direction of the target.
    """
    dx = to_pos[0] - from_pos[0]
    dy = to_pos[1] - from_pos[1]

    # Order of preference based on destination direction
    if abs(dx) >= abs(dy):
        # Primarily horizontal
        if dx > 0:
            candidates = [FACE_EAST, FACE_SOUTH, FACE_NORTH, FACE_WEST]
        else:
            candidates = [FACE_WEST, FACE_SOUTH, FACE_NORTH, FACE_EAST]
    else:
        # Primarily vertical
        if dy > 0:
            candidates = [FACE_SOUTH, FACE_EAST, FACE_WEST, FACE_NORTH]
        else:
            candidates = [FACE_NORTH, FACE_EAST, FACE_WEST, FACE_SOUTH]

    for face in candidates:
        if face not in used_faces:
            return face

    return None


def _find_alternative_input_face(
    to_pos: Tuple[int, int],
    from_pos: Tuple[int, int],
    used_faces: Set[int],
) -> Optional[int]:
    """
    Find an alternative input face when the direct path is blocked.

    For mux input: we need a face that data can arrive from.
    """
    dx = from_pos[0] - to_pos[0]
    dy = from_pos[1] - to_pos[1]

    # Order of preference based on source direction
    if abs(dx) >= abs(dy):
        if dx > 0:
            candidates = [FACE_EAST, FACE_SOUTH, FACE_NORTH, FACE_WEST]
        else:
            candidates = [FACE_WEST, FACE_SOUTH, FACE_NORTH, FACE_EAST]
    else:
        if dy > 0:
            candidates = [FACE_SOUTH, FACE_EAST, FACE_WEST, FACE_NORTH]
        else:
            candidates = [FACE_NORTH, FACE_EAST, FACE_WEST, FACE_SOUTH]

    for face in candidates:
        if face not in used_faces:
            return face

    return None


def configure_demux_block(
    block,
    assignment: FaceAssignment,
    block_name: str,
    hop_counts: Dict[int, int],  # channel -> hop count
    target_interfaces: Dict[int, 'BlockInterface'],  # channel -> target interface
):
    """
    Configure a demux block with its face assignments.

    Args:
        block: The DemuxBlock instance
        assignment: Face assignments
        block_name: Name of this block in the graph
        hop_counts: Hop count to reach each channel's target
        target_interfaces: Interface of each channel's target block
    """
    demux_faces = assignment.demux_faces.get(block_name, {})

    for channel, face in demux_faces.items():
        hop = hop_counts.get(channel, 1)
        target = target_interfaces.get(channel)
        block.set_channel_routing(channel, face, hop, target)


def configure_mux_block(
    block,
    assignment: FaceAssignment,
    block_name: str,
    output_hop: int,
    output_target: Optional['BlockInterface'],
):
    """
    Configure a mux block with its face assignments.

    NOTE: The mux uses entry-address-based channel detection, not input face mapping.
    Each channel has a dedicated entry point (R1, R11, R21) that identifies it.
    The face assignment info is still useful for routing but not for the mux program.

    Args:
        block: The MuxBlock instance
        assignment: Face assignments
        block_name: Name of this block in the graph
        output_hop: Hop count to reach output target
        output_target: Interface of output target block
    """
    # Note: No input face mapping needed - mux uses entry addresses for channel detection

    # Set output routing
    output_face = assignment.get_mux_output_face(block_name)
    if output_face is not None:
        block.set_output_routing(output_face, output_hop, output_target)
