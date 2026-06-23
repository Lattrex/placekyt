"""
Kyttar Source Block for GNURadio

This block acts as the entry point into a Kyttar chip.
It writes data to the chip's INPUT PORT - the only valid way to get data in.

Usage:
    Source [GR] -> [kyttar.source] -> [kyttar.gain] -> [kyttar.sink] -> Sink [GR]

The Source block:
1. Receives float32 samples from the GNURadio domain
2. Writes them to the specified input port using chip.write_port()
3. Runs the simulation with TRUE PIPELINED operation

PIPELINING: Multiple samples can be in-flight simultaneously. The chip
processes data like a pipeline - sample N entering while sample N-1 is
mid-array and sample N-2 is exiting. We do NOT wait for each sample to
complete before injecting the next.

MULTI-CHANNEL MODE (num_channels > 1):
When num_channels is 2 (I/Q) or 3 (tri-channel), the source block expects
interleaved input and tags each sample with a channel-specific entry address.
This allows a demux block to route samples to different processing paths.

Channel entry addresses (from CHANNEL_ENTRY_ADDRESSES):
  - Channel 0 (I): R1
  - Channel 1 (Q): R11
  - Channel 2:     R21

IMPORTANT: This block triggers device initialization on first work() call,
since GNURadio doesn't call start() on blocks with no signal connections
(like the kyttar.device block).

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

import numpy as np
from gnuradio import gr
from typing import Optional, Any
import os
import sys
from pathlib import Path

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

from .registry import get_registry, find_top_block_from_block


class source(gr.sync_block):
    """
    Kyttar Source - Entry point into Kyttar chip via INPUT PORT.

    Data enters the chip ONLY through the configured input port.
    There is no other way to get data into the chip.

    This block implements TRUE PIPELINED operation:
    - All input samples are queued at once
    - Simulation runs until outputs are available
    - Multiple samples can be in-flight simultaneously

    Parameters:
        device_id: ID of the kyttar.device to use
        port_name: Name of the chip input port (e.g., 'x16_in')
        num_channels: Number of channels (1=simple, 2=I/Q, 3=tri-channel)
            - 1: All samples go to same entry address (default)
            - 2: Interleaved I/Q - alternates between R1 and R11
            - 3: Tri-channel - cycles through R1, R11, R21
    """

    # Channel entry addresses (must match CHANNEL_ENTRY_ADDRESSES in placement)
    CHANNEL_ENTRY_ADDRESSES = [1, 11, 21]  # R1, R11, R21

    def __init__(
        self,
        device_id: str = "kyttar_0",
        port_name: str = "x16_in",
        num_channels: int = 1,
        server_host: str = "",
        server_port: int = 0,
        complex_in: bool = False,
        burst_len: int = 0,
    ):
        # SERVER-BATCH MODE (server_port > 0): drive a placeKYT-hosted chip via ONE
        # process_batch RPC instead of building/owning a local chip. The input is
        # the whole complex burst; the matching kyttar_sink (same device_id) drains
        # the recovered words. This is the GRC-first demo path — the REAL DSP blocks
        # stay in the GR graph (so the flowgraph imports into placeKYT) while the
        # actual DSP runs on the hosted chip. `complex_in` accepts the I/Q burst.
        self._server_mode = int(server_port) > 0
        # In server mode the INPUT is the complex I/Q burst (the session carries it
        # to the chip), but the OUTPUT to the marker chain is FLOAT — the real DSP
        # blocks (costas/gardner/slicer) are float-stream markers, so the chain
        # source→costas→…→sink type-checks. The marker-chain data is unused; the
        # burst travels via the batch session, not the GR stream.
        in_dtype = np.complex64 if (complex_in or self._server_mode) else np.float32
        out_dtype = np.float32 if self._server_mode else in_dtype
        gr.sync_block.__init__(
            self,
            name="Kyttar Source",
            in_sig=[in_dtype],
            out_sig=[out_dtype],  # Pass through for GRC connection visualization
        )

        if num_channels < 1 or num_channels > 3:
            raise ValueError("num_channels must be 1, 2, or 3")

        self._device_id = device_id
        self._port_name = port_name
        self._num_channels = num_channels
        self._output_port_name = None  # Set during initialization
        self._initialized = False
        self._chip = None
        self._server_host = str(server_host) or "127.0.0.1"
        self._server_port = int(server_port)
        self._burst_len = int(burst_len)
        self._inbuf = []          # server mode: accumulated complex burst
        self._dispatched = False

        if self._server_mode:
            print(f"[kyttar.source] SERVER-BATCH mode -> "
                  f"{self._server_host}:{self._server_port} (device '{device_id}', "
                  f"port '{port_name}')")
            return

        # Register with device (lazy - device may not exist yet)
        registry = get_registry()
        self._block_id = f"source_{id(self)}"
        registry.register_source(self._block_id, device_id, port_name)

        mode = "single-channel" if num_channels == 1 else f"{num_channels}-channel"
        print(f"[kyttar.source] Registered with device '{device_id}', port '{port_name}', mode={mode}")

    def start(self) -> bool:
        """Called when flowgraph starts."""
        print(f"[kyttar.source] Starting, device='{self._device_id}', port='{self._port_name}'")
        # Defer initialization to work() since device may not be ready yet
        # GNURadio calls start() on blocks in undefined order
        self._initialized = False
        self._chip = None
        return True

    def _try_initialize(self) -> bool:
        """Try to initialize connection to the device.

        This method triggers device initialization if needed, since GNURadio
        doesn't call start() on blocks with no signal connections (like kyttar.device).
        """
        registry = get_registry()
        device = registry.get_device(self._device_id)

        if device is None:
            return False

        # If device not initialized, trigger initialization
        if not device.is_initialized:
            print(f"[kyttar.source] Device not initialized, triggering initialization...")
            try:
                self._initialize_device(device)
            except Exception as e:
                print(f"[kyttar.source] ERROR during device initialization: {e}")
                import traceback
                traceback.print_exc()
                return False

        self._chip = device.chip

        if self._chip is None:
            return False

        # Verify the port exists and is an input port
        if self._port_name not in self._chip.input_port_names:
            print(f"[kyttar.source] ERROR: '{self._port_name}' is not a valid input port")
            print(f"[kyttar.source] Available input ports: {self._chip.input_port_names}")
            return False

        print(f"[kyttar.source] Initialized, writing to input port '{self._port_name}'")
        return True

    def _initialize_device(self, device) -> None:
        """Initialize the device: placement, routing, programming.

        This does what kyttar.device.start() would do, but triggered from Source.

        For branching topologies (demux/mux), this:
        1. Uses BlockGraph to understand channel routing
        2. Assigns faces for demux outputs and mux inputs
        3. Configures demux/mux blocks before building cell programs
        4. Computes routes with proper hop counts for multi-hop paths
        """
        from gr_kyttar.placement import (
            ArrayConfig, Placer, Router, get_block_metrics,
            is_demux_block, is_mux_block,
            assign_faces, configure_demux_block, configure_mux_block,
            FaceAssignmentError,
        )
        from gr_kyttar.bitstream import BitstreamGenerator

        import simkyt

        registry = get_registry()

        # Get the DSP blocks registered with this device
        dsp_blocks = device.dsp_blocks

        if not dsp_blocks:
            raise RuntimeError("No DSP blocks registered. Add kyttar.gain or other DSP blocks.")

        print(f"[kyttar.source] Found {len(dsp_blocks)} DSP blocks to place")

        # === STEP 0: Establish block connections from GRC topology ===
        # This parses the GRC edge_list and builds BlockGraph with channel info
        self._establish_block_connections(device, dsp_blocks)

        # === STEP 1: Load config ===
        config = ArrayConfig.from_yaml(device.chip_config)
        print(f"[kyttar.source] Array: {config.width}x{config.height}")

        # Determine which ports to use
        input_ports = config.get_input_ports()
        output_ports = config.get_output_ports()

        if not input_ports:
            raise RuntimeError("No input ports defined in chip config")
        if not output_ports:
            raise RuntimeError("No output ports defined in chip config")

        input_port_name = self._port_name  # Use the port specified in this Source block
        output_port_name = output_ports[0].name  # Default to first output
        self._output_port_name = output_port_name  # Store for run_until_output

        print(f"[kyttar.source] Using input port: {input_port_name}")
        print(f"[kyttar.source] Using output port: {output_port_name}")

        # === STEP 2: Get block definitions (initial - for placement only) ===
        # Block names were already overridden with GRC symbols in Step 4b of
        # _establish_block_connections, so each block has a unique name.
        block_defs = [b.get_block_definition() for b in dsp_blocks]

        # === STEP 3: Place ===
        print("[kyttar.source] Running placement...")
        metrics = get_block_metrics(dsp_blocks)
        placer = Placer(config, input_port=input_port_name, output_port=output_port_name)
        placement = placer.place(block_defs, metrics)

        for name, placed in placement.placed_blocks.items():
            print(f"[kyttar.source]   {name} placed at {placed.anchor}")

        # === STEP 3.5: Configure demux/mux blocks based on placement ===
        # This is crucial for branching topologies!
        if hasattr(self, '_block_graph') and self._block_graph is not None:
            print("[kyttar.source] Configuring demux/mux blocks based on placement...")
            self._configure_branching_blocks(
                dsp_blocks, placement, config,
                input_port_name, output_port_name
            )

            # CRITICAL: Rebuild block definitions after configuring demux/mux routing.
            # The initial block_defs were built with default hop counts and faces.
            # Now that configure_branching_blocks has set the correct routing on
            # demux/mux blocks, we must rebuild to get the updated cell programs.
            #
            # Also configure non-demux/mux blocks (e.g., gain) with correct hop
            # counts to their targets based on placement positions.
            print("[kyttar.source] Configuring all block output routing...")
            self._configure_all_block_routing(dsp_blocks, placement, config)

            print("[kyttar.source] Rebuilding block definitions with updated routing...")
            block_defs = [b.get_block_definition() for b in dsp_blocks]

        # === STEP 4: Route ===
        print("[kyttar.source] Running routing...")
        router = Router(config, input_port=input_port_name, output_port=output_port_name)
        has_branching = hasattr(self, '_block_graph') and self._block_graph is not None
        # Skip the generic WRITE/JUMP fixup for branching topologies because:
        # 1. Block programs have already been rebuilt with correct hop counts
        # 2. The generic fixup overwrites ALL WRITE/JUMP in a cell with one hop count,
        #    which corrupts demux/mux programs that have per-channel hop counts
        cell_map = router.route(placement, block_defs, skip_write_fixup=has_branching)

        # Fix up routing for demux blocks with indirect paths
        if hasattr(self, '_block_graph') and self._block_graph is not None:
            self._fixup_demux_routing(cell_map, placement, dsp_blocks, config)

            # Fix up mux/block hop counts using actual routing paths
            self._fixup_hop_counts_from_routing(
                cell_map, placement, dsp_blocks, config,
                output_port_name
            )

        print(f"[kyttar.source]   Total cells: {cell_map.cell_count()}")

        # === STEP 5: Generate bitstream ===
        print("[kyttar.source] Generating bitstream...")
        gen = BitstreamGenerator(device.chip_config)
        gen.load_cell_map(cell_map)
        bitstream = gen.generate(custom_row0=False)
        print(f"[kyttar.source]   Bitstream words: {len(bitstream.words)}")

        # === STEP 6: Create and program simulator ===
        print("[kyttar.source] Programming simulator...")

        chip_type_obj = simkyt.ChipType.from_yaml(device.chip_config)
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

        print(f"[kyttar.source]   Programmed {cells_programmed} cells")

        # Set the input port entry address and target hop count
        # Find the source block (the one with no incoming connections)
        if placement.placed_blocks:
            # Build a set of all blocks that are targets of connections
            blocks_with_inputs = set()
            for b in block_defs:
                for conn in b.connections:
                    blocks_with_inputs.add(conn.target)

            # Find blocks with no inputs - these are the source blocks
            source_blocks = [
                pb for name, pb in placement.placed_blocks.items()
                if name not in blocks_with_inputs
            ]

            if source_blocks:
                first_block = source_blocks[0]
                print(f"[kyttar.source]   Source block (no inputs): {first_block.block.name}")
            else:
                # Fallback: just use first block in dict
                first_block = next(iter(placement.placed_blocks.values()))
                print(f"[kyttar.source]   WARNING: No source block found, using: {first_block.block.name}")
            first_cell_config = cell_map.get_cell(*first_block.entry_cell)
            if first_cell_config and first_cell_config.entry_addr is not None:
                entry_addr = first_cell_config.entry_addr
            else:
                entry_addr = 0  # Default entry point

            # Calculate target hop count by counting actual routing path length.
            # The actual path may be longer than Manhattan distance if the router
            # had to detour around placed blocks.
            input_port_pos = config.get_port_position(input_port_name)
            first_block_entry = first_block.entry_cell

            # Walk the routing path from input port to the source block
            distance = self._count_routing_path(cell_map, input_port_pos, first_block_entry)

            # HOP_CNT is incremented BEFORE the check at each cell.
            # target_hop_count + distance + 1 = 31, so target_hop_count = 30 - distance
            target_hop_count = 30 - distance

            chip.set_port_entry_address(input_port_name, entry_addr)
            chip.set_port_target_hop_count(input_port_name, target_hop_count)

            print(f"[kyttar.source]   Input port '{input_port_name}' -> block at {first_block_entry}")
            print(f"[kyttar.source]   Distance: {distance} hops (path), target_hop_count: {target_hop_count}")
            print(f"[kyttar.source]   Entry address: {entry_addr}")

        # === DEBUG: Dump complete cell map ===
        if os.environ.get('KYTTAR_DEBUG'):
            face_arrows = {0: "v(S)", 1: ">(E)", 2: "<(W)", 3: "^(N)"}
            # Opcode mapping from the Kyttar ISA reference (see PROGRAMMING_GUIDE.md)
            opcode_names = {
                0x0: "HALT", 0x1: "HALT", 0x2: "HALT", 0x3: "HALT",
                0x4: "MOVE", 0x5: "BR", 0x6: "WRITE", 0x7: "JUMP",
                0x8: "LOGIC", 0x9: "ARITH", 0xA: "SHL", 0xB: "SHR",
                0xC: "MUL", 0xD: "MAC", 0xE: "CMP", 0xF: "LOAD",
            }
            print(f"\n[kyttar.source] === CELL MAP DUMP ({cell_map.cell_count()} cells) ===")
            # Sort by (row, col) for readability
            sorted_cells = sorted(cell_map.cells.items(), key=lambda x: (x[0][1], x[0][0]))
            for (col, row), cc in sorted_cells:
                face_str = face_arrows.get(cc.fwd_face.value, "?") if cc.fwd_face is not None else "NONE"
                block_str = cc.block_name or "routing"
                entry_str = f"entry=R{cc.entry_addr}" if cc.entry_addr is not None else ""
                print(f"  ({col:2d},{row:2d}) {face_str:5s} [{block_str}] {entry_str}")
                # Decode memory contents
                for addr in sorted(cc.memory.keys()):
                    val = cc.memory[addr]
                    opcode = (val >> 12) & 0xF
                    op_name = opcode_names.get(opcode, f"?{opcode:X}")
                    if opcode == 0x6:  # WRITE
                        hop = (val >> 5) & 0x1F
                        tgt_addr = val & 0x1F
                        print(f"    R{addr:2d} = 0x{val:04X}  {op_name} @{31-hop} R{tgt_addr}")
                    elif opcode == 0x7:  # JUMP
                        hop = (val >> 5) & 0x1F
                        tgt_addr = val & 0x1F
                        print(f"    R{addr:2d} = 0x{val:04X}  {op_name} @{31-hop} R{tgt_addr}")
                    elif opcode in (0x0, 0x1, 0x2, 0x3):  # HALT (including reserved)
                        print(f"    R{addr:2d} = 0x{val:04X}  {op_name}")
                    else:
                        src = (val >> 5) & 0x1F
                        dst = val & 0x1F
                        print(f"    R{addr:2d} = 0x{val:04X}  {op_name} R{src} -> R{dst}")
            print(f"[kyttar.source] === END CELL MAP DUMP ===\n")

            # Also dump the routing path visually on a grid
            print(f"[kyttar.source] === ROUTING GRID ({config.width}x{config.height}) ===")
            cell_lookup = {}
            for (c, r), cc in cell_map.cells.items():
                abbrev = cc.block_name[:6] if cc.block_name else "route"
                face_ch = {0: "v", 1: ">", 2: "<", 3: "^"}.get(
                    cc.fwd_face.value if cc.fwd_face is not None else -1, "?")
                cell_lookup[(c, r)] = f"{face_ch}"
            # Print grid with just face arrows
            print("     " + "  ".join(f"{c:2d}" for c in range(config.width)))
            for r in range(config.height):
                row_str = f"{r:2d}  "
                for c in range(config.width):
                    if (c, r) in cell_lookup:
                        row_str += f" {cell_lookup[(c,r)]} "
                    else:
                        row_str += " . "
                print(row_str)
            # Legend
            print("\nLegend: v=South >=East <=West ^=North .=empty")
            # Print block positions
            for name, placed in placement.placed_blocks.items():
                print(f"  Block '{name}' at {placed.anchor}")
            input_port_pos_dbg = config.get_port_position(input_port_name)
            output_port_pos_dbg = config.get_port_position(output_port_name)
            print(f"  Input port '{input_port_name}' at {input_port_pos_dbg}")
            print(f"  Output port '{output_port_name}' at {output_port_pos_dbg}")
            print(f"[kyttar.source] === END ROUTING GRID ===\n")

        # Store chip in registry
        registry.set_chip(self._device_id, chip)

        print("[kyttar.source] Device initialization complete!")

    def _establish_block_connections(self, device, dsp_blocks) -> None:
        """
        Establish block connections using GRC's edge_list topology.

        This uses the new graph-based placement/routing infrastructure:
        1. Parse edge_list into ConnectionGraph with port numbers
        2. Build BlockGraph with channel information
        3. Store for later use in face assignment and routing

        The approach:
        1. Find the top_block via gc traversal
        2. Get the edge_list string
        3. Parse into ConnectionGraph (preserves port numbers)
        4. Build symbol->block mapping
        5. Build BlockGraph for demux/mux channel routing
        6. Call connect_to() for basic routing (existing system)
        """
        from gr_kyttar.placement import (
            parse_edge_list, build_block_graph, is_demux_block, is_mux_block,
        )

        print("[kyttar.source] Establishing block connections from GRC topology...")

        # Step 1: Find the top_block to get edge_list
        top_block = find_top_block_from_block(self)
        if top_block is None:
            print("[kyttar.source] WARNING: Could not find top_block, using linear chain")
            self._establish_linear_chain(dsp_blocks)
            self._block_graph = None
            self._symbol_to_block = {}
            return

        # Step 2: Get edge_list
        try:
            edge_list_str = top_block.edge_list()
            print(f"[kyttar.source] Edge list from GRC:\n{edge_list_str}")
        except Exception as e:
            print(f"[kyttar.source] WARNING: Could not get edge_list: {e}")
            self._establish_linear_chain(dsp_blocks)
            self._block_graph = None
            self._symbol_to_block = {}
            return

        if not edge_list_str:
            print("[kyttar.source] WARNING: Empty edge_list, using linear chain")
            self._establish_linear_chain(dsp_blocks)
            self._block_graph = None
            self._symbol_to_block = {}
            return

        # Step 3: Parse into ConnectionGraph (preserves port numbers!)
        conn_graph = parse_edge_list(edge_list_str)
        print(f"[kyttar.source] Parsed {len(conn_graph.edges)} edges with port numbers")

        # Step 4: Build a map from GR symbol name to KyttarBlock
        symbol_to_block = {}

        for impl in dsp_blocks:
            gr_block = device.impl_to_gr_block.get(id(impl))
            if gr_block is not None and hasattr(gr_block, 'symbol_name'):
                try:
                    symbol = gr_block.symbol_name()
                    if symbol:
                        symbol_to_block[symbol] = impl
                        block_type = "demux" if is_demux_block(impl) else "mux" if is_mux_block(impl) else "dsp"
                        print(f"[kyttar.source]   Found: {symbol} -> {impl.name} ({block_type})")
                except Exception as e:
                    print(f"[kyttar.source]   Error getting symbol for {impl.name}: {e}")

        if not symbol_to_block:
            print("[kyttar.source] WARNING: Could not map symbols to blocks, using linear chain")
            self._establish_linear_chain(dsp_blocks)
            self._block_graph = None
            self._symbol_to_block = {}
            return

        # Store for later use
        self._symbol_to_block = symbol_to_block
        self._conn_graph = conn_graph

        # Step 4b: Rename blocks to use unique GRC symbols.
        # This must happen BEFORE connect_to() calls (which capture target.name)
        # and BEFORE get_block_definition() (which uses self._name as BlockDef.name).
        # Without this, duplicate block types (e.g., two "Gain_0.50") get
        # deduplicated by the placement engine which uses name as key.
        for symbol, block in symbol_to_block.items():
            block._name = symbol

        # Step 5: Build BlockGraph with channel information
        block_graph = build_block_graph(conn_graph, symbol_to_block)
        self._block_graph = block_graph

        print(f"[kyttar.source] Built BlockGraph with {len(block_graph.nodes)} nodes, {len(block_graph.edges)} edges")
        for edge in block_graph.edges:
            print(f"[kyttar.source]   {edge.src_block}:{edge.src_port} -> {edge.dst_block}:{edge.dst_port} (ch={edge.channel})")

        # Step 6: Also call connect_to() for basic routing (existing system)
        # This is needed for blocks that don't use the new graph-based routing
        connections_made = 0
        for src_name, src_port, dst_name, dst_port in conn_graph.edges:
            src_block = symbol_to_block.get(src_name)
            dst_block = symbol_to_block.get(dst_name)

            if src_block is not None and dst_block is not None:
                src_block.connect_to(dst_block)
                connections_made += 1

        if connections_made == 0:
            print("[kyttar.source] WARNING: No connections found in edge_list, using linear chain")
            self._establish_linear_chain(dsp_blocks)
        else:
            print(f"[kyttar.source] Established {connections_made} block connections")

    def _establish_linear_chain(self, dsp_blocks) -> None:
        """
        Fallback: Connect blocks in registration order as a linear chain.

        This is used when we can't get GRC topology information.
        """
        if len(dsp_blocks) <= 1:
            return

        print(f"[kyttar.source] Creating linear chain of {len(dsp_blocks)} blocks")
        for i in range(len(dsp_blocks) - 1):
            src = dsp_blocks[i]
            dst = dsp_blocks[i + 1]
            src.connect_to(dst)
            print(f"[kyttar.source]   {src.name} -> {dst.name}")

    def _configure_branching_blocks(
        self,
        dsp_blocks,
        placement,
        config,
        input_port_name: str,
        output_port_name: str,
    ) -> None:
        """
        Configure demux/mux blocks based on placement positions.

        This assigns output faces to demux channels and input faces to mux channels
        based on where downstream/upstream blocks are placed. This must be done
        BEFORE build_cell_programs() is called on these blocks.
        """
        from gr_kyttar.placement import (
            is_demux_block, is_mux_block,
            assign_faces, configure_demux_block, configure_mux_block,
            FaceAssignmentError, manhattan_distance,
        )

        if not hasattr(self, '_block_graph') or self._block_graph is None:
            return

        block_graph = self._block_graph

        # Build a reverse map: impl -> GRC symbol name
        impl_to_symbol = {impl: symbol for symbol, impl in self._symbol_to_block.items()}

        # Build block positions from placement, using GRC symbol names (to match block_graph)
        block_positions = {}
        for def_name, placed in placement.placed_blocks.items():
            # Find the impl with this name, then get its GRC symbol
            for impl in dsp_blocks:
                if impl.name == def_name:
                    symbol = impl_to_symbol.get(impl)
                    if symbol:
                        block_positions[symbol] = placed.anchor
                    else:
                        # Fallback to definition name
                        block_positions[def_name] = placed.anchor
                    break

        print(f"[kyttar.source] Block positions for face assignment: {block_positions}")

        # Assign faces based on block positions
        try:
            input_port_pos = config.get_port_position(input_port_name)
            output_port_pos = config.get_port_position(output_port_name)

            # Compute blocked_cells for face assignment: cells on the input route path
            # that should not be used as demux output destinations
            blocked_cells = set()
            source_block_pos = None
            # Find demux from block_graph (which has block nodes we can check)
            for name, block in block_graph.nodes.items():
                if is_demux_block(block):
                    source_block_pos = block_positions.get(name)
                    break
            if source_block_pos is None and block_positions:
                # No demux - use first block as source
                source_block_pos = next(iter(block_positions.values()))

            if source_block_pos and input_port_pos:
                # Compute Manhattan path from input port to source block
                # This gives us the cells that will be input_route cells
                from gr_kyttar.placement import compute_manhattan_path
                # Blocked set for path: only block positions (not the destination)
                path_blocked = set(block_positions.values()) - {source_block_pos}
                path = compute_manhattan_path(
                    input_port_pos, source_block_pos, path_blocked,
                    config.width, config.height
                )
                if path:
                    # Add all path cells except source block to blocked_cells
                    blocked_cells = set(path) - {source_block_pos}
                    print(f"[kyttar.source] Input route path: {path}")
                    print(f"[kyttar.source] blocked_cells for face assignment: {sorted(blocked_cells)}")

            face_assignment = assign_faces(
                block_graph, block_positions,
                input_port_pos=input_port_pos,
                output_port_pos=output_port_pos,
                blocked_cells=blocked_cells,
            )
            self._face_assignment = face_assignment  # Store for _fixup_demux_routing
            print(f"[kyttar.source] Face assignments:")
            for name, faces in face_assignment.demux_faces.items():
                print(f"[kyttar.source]   Demux '{name}': {faces}")
            for name, faces in face_assignment.mux_input_faces.items():
                out_face = face_assignment.mux_output_faces.get(name)
                print(f"[kyttar.source]   Mux '{name}': inputs={faces}, output={out_face}")
        except FaceAssignmentError as e:
            print(f"[kyttar.source] WARNING: Face assignment failed: {e}")
            print("[kyttar.source] Falling back to default face assignments")
            self._face_assignment = None
            return

        # Now configure each demux/mux block
        for impl in dsp_blocks:
            # Find the symbol name for this block
            block_name = None
            for symbol, block in self._symbol_to_block.items():
                if block is impl:
                    block_name = symbol
                    break

            if block_name is None:
                continue

            if is_demux_block(impl):
                self._configure_demux(impl, block_name, face_assignment, block_positions)
            elif is_mux_block(impl):
                self._configure_mux(impl, block_name, face_assignment, block_positions,
                                    output_port_name=output_port_name, config=config)

    def _configure_demux(self, impl, block_name: str, face_assignment, block_positions) -> None:
        """Configure a demux block with face assignments and hop counts."""
        from gr_kyttar.placement import (
            manhattan_distance, get_face_direction,
            compute_manhattan_path,
            FACE_SOUTH, FACE_EAST, FACE_WEST, FACE_NORTH
        )

        if not hasattr(self, '_block_graph'):
            return

        block_graph = self._block_graph
        demux_faces = face_assignment.demux_faces.get(block_name, {})

        if not demux_faces:
            print(f"[kyttar.source] WARNING: No face assignment for demux '{block_name}'")
            return

        demux_pos = block_positions.get(block_name)
        if demux_pos is None:
            return

        # Collect all occupied positions (blocks) to avoid in routing
        blocked = set(block_positions.values())

        # Get output edges for each channel
        outputs = block_graph.get_outputs(block_name)

        for edge in outputs:
            channel = edge.channel
            face = demux_faces.get(channel)

            if face is None:
                print(f"[kyttar.source] WARNING: No face for demux '{block_name}' channel {channel}")
                continue

            dst_pos = block_positions.get(edge.dst_block)
            if dst_pos is None:
                continue

            # Check if the assigned face is the natural direction to the target
            natural_face = get_face_direction(demux_pos, dst_pos)

            if face == natural_face:
                # Direct path - use manhattan distance
                hop_count = manhattan_distance(demux_pos, dst_pos)
            else:
                # Indirect path - compute the actual path starting in 'face' direction
                # First, find the cell adjacent to demux in the 'face' direction
                face_deltas = {
                    FACE_SOUTH: (0, 1),
                    FACE_NORTH: (0, -1),
                    FACE_EAST: (1, 0),
                    FACE_WEST: (-1, 0),
                }
                dx, dy = face_deltas.get(face, (0, 0))
                first_cell = (demux_pos[0] + dx, demux_pos[1] + dy)

                # Now compute path from first_cell to dst_pos
                # Remove dst_pos from blocked so we can route to it
                blocked_for_route = blocked - {dst_pos}
                path = compute_manhattan_path(first_cell, dst_pos, blocked_for_route, 12, 12)

                # Total hop count = 1 (demux to first_cell) + path length
                hop_count = 1 + len(path) - 1  # path includes start, so subtract 1

            # Get target interface
            target_block = block_graph.get_block(edge.dst_block)
            target_interface = target_block.interface if target_block else None

            print(f"[kyttar.source]   Demux '{block_name}' ch{channel}: face={face}, hops={hop_count}, target={edge.dst_block} (natural_face={natural_face})")
            impl.set_channel_routing(channel, face, hop_count, target_interface)

    def _configure_mux(self, impl, block_name: str, face_assignment, block_positions,
                       output_port_name: str = None, config=None) -> None:
        """Configure a mux block with face assignments and hop counts."""
        from gr_kyttar.placement import manhattan_distance

        if not hasattr(self, '_block_graph'):
            return

        block_graph = self._block_graph

        # NOTE: Mux now uses entry-address-based channel detection.
        # No input face mapping needed - each channel has its own entry point
        # (R1 for I/ch0, R11 for Q/ch1, R21 for ch2).

        # Set output routing
        output_face = face_assignment.get_mux_output_face(block_name)
        mux_pos = block_positions.get(block_name)

        if output_face is None or mux_pos is None:
            return

        # Find downstream block
        outputs = block_graph.get_outputs(block_name)
        configured = False
        if outputs:
            dst_name = outputs[0].dst_block
            dst_pos = block_positions.get(dst_name)
            if dst_pos:
                hop_count = manhattan_distance(mux_pos, dst_pos)
                target_block = block_graph.get_block(dst_name)
                target_interface = target_block.interface if target_block else None
                print(f"[kyttar.source]   Mux '{block_name}' output: face={output_face}, hops={hop_count}, target={dst_name}")
                impl.set_output_routing(output_face, hop_count, target_interface)
                configured = True

        if not configured:
            # Downstream block is not placed (e.g., GRC sink block).
            # Route to the output port instead. The router will create routing
            # cells from the mux to the output port.
            # Use hop_count=1 as placeholder; the router's _fixup_write_instructions
            # will correct it. But we DO need the correct face.
            # Actually, compute the real distance to the output port if config is available.
            if config and output_port_name:
                output_pos = config.get_port_position(output_port_name)
                if output_pos:
                    hop_count = manhattan_distance(mux_pos, output_pos)
                    # +1 to exit through the output port face
                    hop_count += 1
                    print(f"[kyttar.source]   Mux '{block_name}' output: face={output_face}, hops={hop_count} (to output port at {output_pos})")
                    impl.set_output_routing(output_face, hop_count, None)
                    configured = True

            if not configured:
                print(f"[kyttar.source]   Mux '{block_name}' output: face={output_face} (to output port, hop=1 placeholder)")
                impl.set_output_routing(output_face, 1, None)

    def _configure_all_block_routing(self, dsp_blocks, placement, config) -> None:
        """
        Configure output routing for ALL blocks based on placement positions.

        For non-demux/non-mux blocks (e.g., gain blocks), this computes the
        correct output_hop and target_interface so their WRITE/JUMP instructions
        use the right hop counts.

        Demux/mux blocks are already configured by _configure_branching_blocks,
        so we skip them here.
        """
        from gr_kyttar.placement import (
            is_demux_block, is_mux_block, manhattan_distance,
        )

        if not hasattr(self, '_block_graph') or self._block_graph is None:
            return

        block_graph = self._block_graph
        impl_to_symbol = {impl: symbol for symbol, impl in self._symbol_to_block.items()}

        # Build block positions from placement using GRC symbols
        block_positions = {}
        for def_name, placed in placement.placed_blocks.items():
            for impl in dsp_blocks:
                if impl.name == def_name:
                    symbol = impl_to_symbol.get(impl)
                    if symbol:
                        block_positions[symbol] = placed.anchor
                    else:
                        block_positions[def_name] = placed.anchor
                    break

        for impl in dsp_blocks:
            # Skip demux/mux - already configured
            if is_demux_block(impl) or is_mux_block(impl):
                continue

            block_name = impl_to_symbol.get(impl)
            if block_name is None:
                continue

            src_pos = block_positions.get(block_name)
            if src_pos is None:
                continue

            # Find the output target
            outputs = block_graph.get_outputs(block_name)

            if outputs:
                # Has output connection to another DSP block
                edge = outputs[0]
                dst_pos = block_positions.get(edge.dst_block)
                if dst_pos is None:
                    # Target not found in positions (maybe it's a sink?)
                    # Fall through to output port routing
                    outputs = None
                else:
                    hop_count = manhattan_distance(src_pos, dst_pos)
                    target_block = block_graph.get_block(edge.dst_block)
                    target_interface = target_block.interface if target_block else None

                    print(f"[kyttar.source]   Block '{block_name}': output hops={hop_count}, target={edge.dst_block}")

                    impl._configured_output_hop = hop_count
                    impl._configured_target_interface = target_interface
                    continue

            # No output in block graph OR target not found - this block connects
            # to the sink (output port). Configure routing to the output port.
            output_port_name = self._port_name.replace("_in", "_out")
            if hasattr(config, 'get_port_position'):
                output_pos = config.get_port_position(output_port_name)
                if output_pos:
                    hop_count = manhattan_distance(src_pos, output_pos)
                    # +1 to exit through the output port face
                    hop_count += 1
                    print(f"[kyttar.source]   Block '{block_name}': output hops={hop_count} (to output port at {output_pos})")
                    impl._configured_output_hop = hop_count
                    impl._configured_target_interface = None

    def _count_routing_path(self, cell_map, start_pos, end_pos) -> int:
        """
        Count the actual number of routing hops from start_pos to end_pos.

        Walks the routing path through the cell_map by following each cell's
        fwd_face direction. This gives the actual path length, which may be
        longer than Manhattan distance if the router had to detour around blocks.

        Returns the number of cells traversed (not counting the destination).
        """
        # Face direction deltas: S=0(+y), E=1(+x), W=2(-x), N=3(-y)
        face_deltas = {
            0: (0, 1),   # South: y+1
            1: (1, 0),   # East: x+1
            2: (-1, 0),  # West: x-1
            3: (0, -1),  # North: y-1
        }

        pos = start_pos
        hops = 0
        max_hops = 50  # Safety limit

        while pos != end_pos and hops < max_hops:
            cell = cell_map.get_cell(*pos)
            if cell is None or cell.fwd_face is None:
                # No routing info - fall back to Manhattan distance
                manhattan = abs(end_pos[0] - start_pos[0]) + abs(end_pos[1] - start_pos[1])
                print(f"[kyttar.source]   WARNING: routing path broken at {pos}, using Manhattan={manhattan}")
                return manhattan

            dx, dy = face_deltas[cell.fwd_face.value]
            pos = (pos[0] + dx, pos[1] + dy)
            hops += 1

        if hops >= max_hops:
            manhattan = abs(end_pos[0] - start_pos[0]) + abs(end_pos[1] - start_pos[1])
            print(f"[kyttar.source]   WARNING: routing path exceeded {max_hops} hops, using Manhattan={manhattan}")
            return manhattan

        return hops

    def _fixup_hop_counts_from_routing(self, cell_map, placement, dsp_blocks, config,
                                       output_port_name) -> None:
        """
        Fix up block hop counts and output faces using actual routing paths.

        After the router creates routing paths (which may detour around blocks),
        the actual path lengths may differ from Manhattan distance, and the output
        direction may differ from the face assignment's choice. This method:
        1. Reads the router's actual exit direction for the mux
        2. Counts the actual path length
        3. Rebuilds the mux program with correct face and hop count
        4. Applies output port transit fixup (WRITE hop_cnt=0, JUMP->HALT)

        The output port transit fixup is CRITICAL: the output port captures data
        that TRANSITS through the output port cell and exits on its face. Data
        that executes locally (hop_cnt=31) is NOT captured. Setting hop_cnt=0
        ensures data always transits (never reaches 31 within the route).
        """
        from gr_kyttar.placement import is_mux_block, manhattan_distance

        output_pos = config.get_port_position(output_port_name)
        if output_pos is None:
            return

        impl_to_symbol = {impl: sym for sym, impl in self._symbol_to_block.items()}

        for impl in dsp_blocks:
            if not is_mux_block(impl):
                continue

            block_name = impl_to_symbol.get(impl)
            if block_name is None:
                continue

            # Find the mux's placement position
            for def_name, placed in placement.placed_blocks.items():
                if impl.name == def_name:
                    mux_pos = placed.anchor
                    break
            else:
                continue

            # Get the mux cell from the cell_map
            mux_cell = cell_map.get_cell(*mux_pos)
            if mux_cell is None:
                continue

            configured_face = impl._interface.output_face
            manhattan_hops = manhattan_distance(mux_pos, output_pos) + 1

            # The router may have assigned a different output direction than what
            # the mux was configured with. Use the router's fwd_face if available.
            router_face = configured_face
            if mux_cell.fwd_face is not None:
                router_face = mux_cell.fwd_face if isinstance(mux_cell.fwd_face, int) else mux_cell.fwd_face.value

            # Find the actual routing path from the mux cell, following fwd_face
            # through the routing corridor to the output port.
            from gr_kyttar.placement.cell_map import Face
            face_deltas = {0: (0, 1), 1: (1, 0), 2: (-1, 0), 3: (0, -1)}
            pos = mux_pos
            actual_hops = 0
            visited_path = set()
            path_broken = False
            while pos != output_pos and actual_hops < 50:
                if pos in visited_path:
                    # Routing loop detected
                    path_broken = True
                    break
                visited_path.add(pos)
                cell = cell_map.get_cell(*pos)
                if cell is None or cell.fwd_face is None:
                    path_broken = True
                    break
                dx, dy = face_deltas[cell.fwd_face.value]
                pos = (pos[0] + dx, pos[1] + dy)
                actual_hops += 1
            if actual_hops >= 50:
                path_broken = True

            out_face = router_face

            # If the routing path is broken (loops, dead ends, or exceeds 50 hops),
            # the router's A* failed and fell back to Manhattan through occupied cells.
            # We need to create a valid routing path ourselves.
            if path_broken:
                print(f"[kyttar.source] WARNING: Mux '{block_name}' output route is broken "
                      f"(detected loop/dead-end after {actual_hops} hops)")
                print(f"[kyttar.source]   Mux at {mux_pos}, output port at {output_pos}")
                print(f"[kyttar.source]   Creating new routing path with A*...")

                # Run A* from mux to output port.
                # Block programmed cells and inter-block routing cells.
                # Allow traversal through _output_route (safe to repurpose) and
                # _input_route cells (passable but not modified - the A* can
                # find paths through them without breaking input routing).
                import heapq

                blocked = set()
                passable = set()  # Can traverse but won't modify
                for (cx, cy), cfg in cell_map.cells.items():
                    if (cx, cy) == mux_pos or (cx, cy) == output_pos:
                        continue
                    # Allow repurposing _output_route cells (broken fallback)
                    if cfg.block_name == "_output_route":
                        continue
                    # Allow traversal through _input_route cells
                    if cfg.block_name == "_input_route":
                        passable.add((cx, cy))
                        continue
                    # Block everything else (programmed + inter-block routes)
                    blocked.add((cx, cy))

                def heuristic(p):
                    return abs(p[0] - output_pos[0]) + abs(p[1] - output_pos[1])

                counter = 0
                heap = [(heuristic(mux_pos), counter, mux_pos, [])]
                visited = set()
                path_result = None

                while heap:
                    _, _, p, path = heapq.heappop(heap)
                    if p in visited:
                        continue
                    visited.add(p)
                    if p == output_pos:
                        path_result = path
                        break
                    for dx, dy, face in [(1, 0, Face.EAST), (-1, 0, Face.WEST),
                                         (0, 1, Face.SOUTH), (0, -1, Face.NORTH)]:
                        np_ = (p[0] + dx, p[1] + dy)
                        if 0 <= np_[0] < config.width and 0 <= np_[1] < config.height:
                            if np_ not in visited and (np_ not in blocked or np_ == output_pos):
                                new_path = path + [(p, face)]
                                g = len(new_path)
                                counter += 1
                                heapq.heappush(heap, (g + heuristic(np_), counter, np_, new_path))

                if path_result is None:
                    print(f"[kyttar.source]   ERROR: A* could not find path from {mux_pos} to {output_pos}")
                else:
                    actual_hops = len(path_result)
                    # Apply the routing path: set fwd_face for each cell
                    for (px, py), face in path_result:
                        existing = cell_map.get_cell(px, py)
                        if existing is None:
                            cell_map.add_routing_cell(px, py, face, "_output_route")
                        elif (px, py) in passable:
                            # _input_route cell - don't overwrite, just traverse
                            pass
                        elif existing.entry_addr is None:
                            # _output_route cell - safe to repurpose
                            existing.fwd_face = face
                            existing.block_name = "_output_route"
                        # If it's the mux cell itself, update its fwd_face
                        if (px, py) == mux_pos:
                            out_face = face.value
                            mux_cell.fwd_face = face

                    print(f"[kyttar.source]   New route: {actual_hops} hops, "
                          f"mux fwd_face={out_face}")

            needs_fix = (actual_hops != manhattan_hops) or (out_face != configured_face)

            if needs_fix:
                # Use a valid hop count for assembly (max 31). The output port
                # transit fixup below will override WRITE hop_cnt to 0 anyway.
                assembly_hops = min(actual_hops, 31)
                print(f"[kyttar.source] Fixing mux '{block_name}': "
                      f"face {configured_face}->{out_face}, "
                      f"hops {manhattan_hops}->{actual_hops} (assembly={assembly_hops})")

                # Reconfigure mux with correct face and capped hop count
                impl.set_output_routing(out_face, assembly_hops, impl._interface.target_interface)

                # Rebuild mux program
                mux_programs = impl.build_cell_programs()
                mux_prog = mux_programs[0]

                # Update cell_map with new program
                old_keys = list(mux_cell.memory.keys())
                for k in old_keys:
                    del mux_cell.memory[k]
                for addr, value in mux_prog.memory.items():
                    mux_cell.memory[addr] = value
                if mux_prog.entry_addr is not None:
                    mux_cell.entry_addr = mux_prog.entry_addr

            # Apply output port transit fixup to mux cell.
            # The output port captures data that EXITS the output port cell on its
            # registered face. Data must TRANSIT through (hop_cnt != 31), not execute
            # locally. Set ALL WRITEs' hop_cnt=0 so data always transits.
            # The mux has multiple WRITE instructions (one per channel handler),
            # and ALL of them must have hop_cnt=0 to properly transit to the output port.
            # Do NOT replace JUMPs - the mux uses internal JUMPs for channel-specific
            # branching logic. JUMPs to non-existent neighbors are safely dropped
            # by the simulator's NoNeighbor handler.
            writes_fixed = 0
            for addr in sorted(mux_cell.memory.keys()):
                value = mux_cell.memory[addr]
                opcode = (value >> 12) & 0xF
                if opcode == 0x6:  # WRITE instruction (opcode 0x6 per arch spec v0.11) - set hop_cnt=0
                    dest = value & 0x1F
                    mux_cell.memory[addr] = (0x6 << 12) | (0 << 5) | dest
                    writes_fixed += 1
            print(f"[kyttar.source] Applied output port transit fixup to mux '{block_name}': {writes_fixed} WRITEs hop=0")

    def _rebuild_input_route(self, cell_map, config, mux_pos, output_pos,
                             repurposed_cells, blocked) -> None:
        """
        Rebuild the input routing path after cells were repurposed for the output route.

        When the output route A* repurposes _input_route or inter-block route cells,
        the input path from the input port to the source block (Demux) may be broken.
        This method creates a new input path using A* that avoids:
        - Programmed block cells (blocked set)
        - Cells now used for the output route (repurposed_cells with new fwd_face)
        """
        from gr_kyttar.placement.cell_map import Face
        import heapq

        # Find the input port position and the source block entry position
        # by looking for the _input_route chain's start and end.
        input_port_pos = None
        source_block_pos = None

        # Input route cells form a chain. Find all existing _input_route cells
        # and determine the start (adjacent to input port or edge of grid) and
        # end (adjacent to source block).
        input_route_cells = []
        for (cx, cy), cfg in cell_map.cells.items():
            if cfg.block_name and cfg.block_name == "_input_route":
                input_route_cells.append((cx, cy))

        # Find input port position from config
        for port in config.get_input_ports():
            input_port_pos = config.get_port_position(port.name)
            break

        if input_port_pos is None:
            print(f"[kyttar.source]   WARNING: Cannot find input port position for route rebuild")
            return

        # Find the source block (Demux) position - it's the block that receives from input route
        # Walk from input_port_pos following fwd_face until we hit a programmed cell
        face_deltas = {0: (0, 1), 1: (1, 0), 2: (-1, 0), 3: (0, -1)}
        source_block_pos = None
        pos = input_port_pos
        visited = set()
        while pos not in visited:
            visited.add(pos)
            cell = cell_map.get_cell(*pos)
            if cell is None:
                break
            if cell.entry_addr is not None:
                source_block_pos = pos
                break
            if cell.fwd_face is None:
                break
            # Check if this cell was repurposed - if so, the chain is broken
            if pos in [(c[0], c[1]) for c in repurposed_cells]:
                break
            dx, dy = face_deltas[cell.fwd_face.value]
            pos = (pos[0] + dx, pos[1] + dy)

        if source_block_pos is None:
            # Find source block by looking at which programmed cell is adjacent
            # to any _input_route cell
            for (cx, cy) in input_route_cells:
                for dx, dy in [(1,0), (-1,0), (0,1), (0,-1)]:
                    nx, ny = cx + dx, cy + dy
                    ncell = cell_map.get_cell(nx, ny)
                    if ncell and ncell.entry_addr is not None:
                        source_block_pos = (nx, ny)
                        break
                if source_block_pos:
                    break

        if source_block_pos is None:
            print(f"[kyttar.source]   WARNING: Cannot find source block for route rebuild")
            return

        print(f"[kyttar.source]   Rebuilding input route: {input_port_pos} -> {source_block_pos}")

        # Build the set of cells to avoid: blocked programmed cells + output route cells
        avoid = set(blocked)
        # Also block repurposed cells (they're now part of output route)
        for rp in repurposed_cells:
            avoid.add((rp[0], rp[1]) if isinstance(rp, tuple) else rp)
        # Don't block source/destination
        avoid.discard(input_port_pos)
        avoid.discard(source_block_pos)

        # A* from input_port_pos to source_block_pos
        def heuristic(p):
            return abs(p[0] - source_block_pos[0]) + abs(p[1] - source_block_pos[1])

        counter = 0
        heap = [(heuristic(input_port_pos), counter, input_port_pos, [])]
        visited = set()
        path_result = None

        while heap:
            _, _, p, path = heapq.heappop(heap)
            if p in visited:
                continue
            visited.add(p)
            if p == source_block_pos:
                path_result = path
                break
            for dx, dy, face in [(1, 0, Face.EAST), (-1, 0, Face.WEST),
                                 (0, 1, Face.SOUTH), (0, -1, Face.NORTH)]:
                np_ = (p[0] + dx, p[1] + dy)
                if 0 <= np_[0] < config.width and 0 <= np_[1] < config.height:
                    if np_ not in visited and (np_ not in avoid or np_ == source_block_pos):
                        new_path = path + [(p, face)]
                        g = len(new_path)
                        counter += 1
                        heapq.heappush(heap, (g + heuristic(np_), counter, np_, new_path))

        if path_result is None:
            print(f"[kyttar.source]   ERROR: Could not rebuild input route from {input_port_pos} to {source_block_pos}")
            return

        print(f"[kyttar.source]   New input route: {len(path_result)} hops")

        # First, remove old _input_route cells that are NOT repurposed
        # (repurposed ones are now output route cells, don't touch them)
        repurposed_set = set()
        for rp in repurposed_cells:
            repurposed_set.add((rp[0], rp[1]) if isinstance(rp, tuple) else rp)

        for (cx, cy) in input_route_cells:
            if (cx, cy) not in repurposed_set:
                existing = cell_map.get_cell(cx, cy)
                if existing and existing.block_name == "_input_route":
                    # Remove from cell_map
                    del cell_map.cells[(cx, cy)]

        # Apply new input route path
        for (px, py), face in path_result:
            if (px, py) == source_block_pos:
                continue  # Don't overwrite the source block
            existing = cell_map.get_cell(px, py)
            if existing is None:
                cell_map.add_routing_cell(px, py, face, "_input_route")
            elif existing.entry_addr is None and existing.block_name != "_output_route":
                # Reuse as input route (but don't overwrite output route cells)
                existing.fwd_face = face
                existing.block_name = "_input_route"

    def _fixup_demux_routing(self, cell_map, placement, dsp_blocks, config) -> None:
        """
        Fix routing cells for demux blocks that use indirect paths.

        When a demux outputs via an alternative face (not the natural direction to its target),
        we need to create routing cells that:
        1. Receive data from the demux's output direction
        2. Route it towards the actual target via an L-shaped or multi-hop path

        For example, if demux at (4,5) needs to send to (3,5) but outputs SOUTH:
        - Path is: (4,5) -> (4,6) -> (3,6) -> (3,5)
        - Cell (4,6) needs FWD_FACE=WEST
        - Cell (3,6) needs FWD_FACE=NORTH
        """
        from gr_kyttar.placement import (
            is_demux_block, get_face_direction, compute_manhattan_path,
            FACE_SOUTH, FACE_EAST, FACE_WEST, FACE_NORTH,
        )
        from gr_kyttar.placement.cell_map import Face

        if not hasattr(self, '_block_graph') or self._block_graph is None:
            return

        block_graph = self._block_graph

        # Build reverse map: impl -> GRC symbol name
        impl_to_symbol = {impl: symbol for symbol, impl in self._symbol_to_block.items()}

        # Build block positions from placement using GRC symbols
        block_positions = {}
        for def_name, placed in placement.placed_blocks.items():
            for impl in dsp_blocks:
                if impl.name == def_name:
                    symbol = impl_to_symbol.get(impl)
                    if symbol:
                        block_positions[symbol] = placed.anchor
                    else:
                        block_positions[def_name] = placed.anchor
                    break

        # Collect all occupied positions (blocks) to avoid in routing
        blocked = set(block_positions.values())

        # Also collect _input_route cells - these MUST NOT be overwritten by demux paths
        # If the A* pathfinder routes through input_route cells, the demux sends data there
        # but the cell's FWD_FACE is set for the input path, causing data to go wrong direction
        for (cx, cy), cfg in cell_map.cells.items():
            if cfg.block_name and cfg.block_name.startswith("_input_route"):
                blocked.add((cx, cy))

        # Face constants to CellMap Face conversion
        face_to_cell_face = {
            FACE_SOUTH: Face.SOUTH,
            FACE_EAST: Face.EAST,
            FACE_WEST: Face.WEST,
            FACE_NORTH: Face.NORTH,
        }

        # Direction deltas
        face_deltas = {
            FACE_SOUTH: (0, 1),
            FACE_NORTH: (0, -1),
            FACE_EAST: (1, 0),
            FACE_WEST: (-1, 0),
        }

        # Helper to compute direction between two adjacent cells
        def get_direction_to_next(from_pos, to_pos):
            dx = to_pos[0] - from_pos[0]
            dy = to_pos[1] - from_pos[1]
            if dx > 0:
                return FACE_EAST
            elif dx < 0:
                return FACE_WEST
            elif dy > 0:
                return FACE_SOUTH
            elif dy < 0:
                return FACE_NORTH
            return None

        for impl in dsp_blocks:
            if not is_demux_block(impl):
                continue

            # Get the GRC symbol name for this demux
            block_name = impl_to_symbol.get(impl)
            if block_name is None:
                continue

            demux_pos = block_positions.get(block_name)
            if demux_pos is None:
                continue

            # Get face assignments for this demux
            if not hasattr(self, '_face_assignment') or self._face_assignment is None:
                continue

            demux_faces = self._face_assignment.demux_faces.get(block_name, {})

            # Get output edges for each channel
            outputs = block_graph.get_outputs(block_name)

            for edge in outputs:
                channel = edge.channel
                face = demux_faces.get(channel)

                if face is None:
                    continue

                dst_pos = block_positions.get(edge.dst_block)
                if dst_pos is None:
                    continue

                # Check if this is an indirect path
                natural_face = get_face_direction(demux_pos, dst_pos)

                if face == natural_face:
                    # Direct path - router should have handled it
                    continue

                # Indirect path - we need to fix up routing cells
                print(f"[kyttar.source] Fixing up indirect path for demux '{block_name}' ch{channel}")
                print(f"[kyttar.source]   Demux at {demux_pos}, outputs {['S','E','W','N'][face]}, target at {dst_pos}")

                # Compute the path from first cell in output direction to target
                dx, dy = face_deltas.get(face, (0, 0))
                first_cell = (demux_pos[0] + dx, demux_pos[1] + dy)

                # Route from first_cell to dst_pos, avoiding other blocks (except dst)
                blocked_for_route = blocked - {dst_pos}
                path = compute_manhattan_path(first_cell, dst_pos, blocked_for_route, config.width, config.height)

                print(f"[kyttar.source]   Path: {path}")

                # Set FWD_FACE for each routing cell along the path
                for i in range(len(path) - 1):
                    current_pos = path[i]
                    next_pos = path[i + 1]

                    direction = get_direction_to_next(current_pos, next_pos)
                    if direction is None:
                        continue

                    cell_face = face_to_cell_face.get(direction, Face.SOUTH)

                    # Get existing cell config or create new one
                    existing = cell_map.get_cell(*current_pos)
                    if existing is None:
                        # Create new routing cell
                        cell_map.add_routing_cell(current_pos[0], current_pos[1], cell_face,
                                                  block_name=f"_demux_{block_name}_ch{channel}")
                        print(f"[kyttar.source]   Created routing cell at {current_pos} with FWD_FACE={['S','E','W','N'][direction]}")
                    elif existing.block_name and existing.block_name.startswith("_input_route"):
                        # NEVER overwrite _input_route cells - this breaks the input path
                        print(f"[kyttar.source]   SKIPPED cell at {current_pos} (is _input_route, would break input path)")
                    elif existing.entry_addr is not None:
                        # NEVER overwrite programmed block cells
                        print(f"[kyttar.source]   SKIPPED cell at {current_pos} (is programmed block)")
                    else:
                        # Update existing cell's forward face
                        existing.fwd_face = cell_face
                        print(f"[kyttar.source]   Updated cell at {current_pos} with FWD_FACE={['S','E','W','N'][direction]}")

    def stop(self) -> bool:
        """Called when flowgraph stops."""
        if self._server_mode:
            # Flush the burst if it never hit burst_len (e.g. burst_len=0).
            self._server_dispatch()
            return True
        print("[kyttar.source] Stopping")
        self._initialized = False
        self._chip = None
        return True

    # --- server-batch mode ---------------------------------------------------
    def _server_dispatch(self):
        """Send the accumulated complex burst to the placeKYT SimServer in ONE
        process_batch RPC; stash the recovered words for the matching sink."""
        if self._dispatched or not self._inbuf:
            return
        from ._batch_session import get_session
        sess = get_session(self._device_id)
        out = sess.dispatch(self._server_host, self._server_port, self._inbuf,
                            in_port=self._port_name)
        self._dispatched = True
        print(f"[kyttar.source] SERVER-BATCH: sent {len(self._inbuf)} samples "
              f"-> {len(out)} recovered (one process_batch RPC)", flush=True)

    def work(self, input_items, output_items):
        """Process samples - write to chip input port with TRUE PIPELINING.

        Now that the simulator implements proper 4-phase handshake protocol,
        we can queue all samples at once. The simulator will:
        1. Check if target cell is busy before injecting
        2. Wait (re-schedule) if cell is processing a previous sample
        3. Only proceed when cell completes and sends ACK

        This provides natural backpressure - samples flow through the pipeline
        at the rate the cells can process them, with multiple samples in-flight.

        Multi-channel mode:
        When num_channels > 1, samples are tagged with alternating entry addresses
        so a demux block can route them to different processing paths.
        """
        inp = input_items[0]
        out = output_items[0]
        n_samples = len(inp)

        # === SERVER-BATCH MODE ===
        # Accumulate the whole complex burst; dispatch it to the placeKYT server in
        # ONE process_batch RPC when burst_len is reached (or at stop()). The sink
        # (same device_id) drains the recovered words. No local chip is touched. The
        # float OUTPUT carries the input magnitude only (marker-chain viz; unused).
        if self._server_mode:
            out[:] = np.real(np.asarray(inp, dtype=np.complex64)).astype(np.float32)
            if not self._dispatched:
                self._inbuf.extend(np.asarray(inp, dtype=np.complex64).tolist())
                if self._burst_len > 0 and len(self._inbuf) >= self._burst_len:
                    del self._inbuf[self._burst_len:]
                    self._server_dispatch()
            return n_samples

        # Always pass through for GRC visualization
        out[:] = inp[:]

        # Lazy initialization - try to connect to device if not yet done
        if not self._initialized:
            self._initialized = self._try_initialize()
            if not self._initialized:
                # Device not ready yet, just pass through
                return n_samples

        if self._chip is None:
            return n_samples

        # === PIPELINED OPERATION ===
        # Write samples to input port. The sink block will run the simulation
        # when it needs output data. This ensures proper synchronization in GRC's
        # asynchronous block scheduling model.
        #
        # The simulator's 4-phase handshake protocol ensures proper flow control:
        # - Port checks if target cell is busy before each injection
        # - If busy, port waits (re-schedules) until cell sends ACK
        # - ACK is sent when cell halts after processing

        if self._num_channels == 1:
            # Single-channel mode: all samples to same entry address
            self._chip.write_port(self._port_name, inp)
        else:
            # Multi-channel mode: tag samples with alternating entry addresses
            # Generate entry address array: [addr[0], addr[1], addr[0], addr[1], ...]
            entry_addresses = np.array([
                self.CHANNEL_ENTRY_ADDRESSES[i % self._num_channels]
                for i in range(n_samples)
            ], dtype=np.uint8)

            self._chip.write_port_tagged(self._port_name, inp, entry_addresses)

        # Debug: track samples written
        if not hasattr(self, '_total_written'):
            self._total_written = 0
            self._debug_count = 0
        self._total_written += n_samples
        self._debug_count += 1
        if self._debug_count <= 5 or self._debug_count % 100 == 0:
            mode_str = f"{self._num_channels}ch" if self._num_channels > 1 else "1ch"
            print(f"[kyttar.source] work#{self._debug_count}: wrote {n_samples} ({mode_str}), total={self._total_written}, inp[0:3]={inp[:3] if len(inp) >= 3 else inp}")

        # Don't run simulation here - let the sink do it when it needs data.
        # This ensures output is ready when sink reads it.

        return n_samples
