"""
Router for Kyttar Fabric

Takes a Placement (blocks with positions) and produces a CellMap
(complete cell configurations including routing).

The router:
1. Configures FACE for each cell based on data flow
2. Creates routing paths between blocks
3. Creates routing paths from I/O ports to blocks
4. Copies cell programs from BlockDefinitions to the CellMap
"""

from dataclasses import dataclass
from typing import List, Dict, Set, Tuple, Optional

from .placer import Placement, PlacedBlock
from .region import ArrayConfig
from .cell_map import CellMap, CellConfig, Face
from .block import BlockDefinition, CellProgram
from .resolver import CellProgramResolver, ResolvedTargets, WriteTarget, JumpTarget


class RouterError(Exception):
    """Raised when routing fails."""
    pass


class Router:
    """
    Routes data flow between placed blocks.

    After placement, the router:
    1. Determines FACE for each cell based on filament structure
    2. Creates routing paths between connected blocks
    3. Creates routing paths from input port to first block
    4. Creates routing paths from last block to output port
    5. Copies cell programs from BlockDefinitions

    Output: A complete CellMap ready for bitstream generation.
    """

    def __init__(
        self,
        config: ArrayConfig,
        input_port: Optional[str] = None,
        output_port: Optional[str] = None,
    ):
        """
        Initialize router.

        Args:
            config: Array configuration
            input_port: Name of input port to use (defaults to first input port)
            output_port: Name of output port to use (defaults to first output port)
        """
        self.config = config
        self._input_port = input_port
        self._output_port = output_port

    def get_input_position(self) -> Tuple[int, int]:
        """Get the input port cell position."""
        if self._input_port and self.config.ports:
            return self.config.get_port_position(self._input_port)
        return self.config.input_position()

    def get_output_position(self) -> Tuple[int, int]:
        """Get the output port cell position."""
        if self._output_port and self.config.ports:
            return self.config.get_port_position(self._output_port)
        return self.config.output_position()

    def route(self, placement: Placement, blocks: List[BlockDefinition],
              skip_write_fixup: bool = False,
              skip_io_routing: bool = False) -> CellMap:
        """
        Generate complete cell map from placement.

        Args:
            placement: Completed placement
            blocks: Original block definitions (with cell programs)
            skip_write_fixup: If True, skip the WRITE/JUMP hop count fixup.
                Use this when block programs have already been built with
                correct hop counts (e.g., after configuring branching blocks).
            skip_io_routing: If True, do NOT auto-route the input/output ports
                or block→block connections via A* pathfinding. The CALLER owns
                routing (e.g. placeKYT, which faces cells from the user's drawn
                route waypoints). Without this the Router fabricates I/O paths
                that don't exist in the user's design.

        Returns:
            CellMap with complete cell configurations
        """
        cell_map = CellMap(width=self.config.width, height=self.config.height)

        # Build block lookup
        block_lookup = {b.name: b for b in blocks}

        # Step 1: Configure cells for each placed block
        for name, pb in placement.placed_blocks.items():
            block_def = block_lookup.get(name)
            self._configure_block_cells(pb, block_def, cell_map)

        # Step 2: Route I/O ports FIRST, before inter-block routing.
        # Route OUTPUT first - it's typically the longest/most critical path
        # (from the last block to the far corner of the grid).
        # Route INPUT second - it's typically shorter (from edge to first block).
        # Each route gets its own dedicated cells (no sharing via passable_prefixes)
        # because each cell has only ONE fwd_face.
        # The caller may own routing entirely (placeKYT applies the user's drawn
        # route waypoints). In that case do NOT fabricate any paths via A* — that
        # would invent cells/routes the user never drew.
        if not skip_io_routing:
            self._route_output_port(placement, blocks, cell_map)
            self._route_input_port(placement, blocks, cell_map)

            # Step 3: Route between connected blocks
            for name, pb in placement.placed_blocks.items():
                block_def = block_lookup.get(name)
                if block_def:
                    self._route_block_connections(pb, block_def, placement, cell_map)

        # Step 4: Resolve new-style blocks (template-based)
        new_style_names = self._resolve_new_style_blocks(placement, blocks, cell_map)

        # Step 5: Fix up WRITE instructions with correct hop counts (old-style only)
        if not skip_write_fixup:
            self._fixup_write_instructions(placement, blocks, cell_map,
                                           skip_blocks=new_style_names)

        # --- DEBUG: dump cell usage ---
        import os
        if os.environ.get('ROUTER_DEBUG'):
            by_type = {}
            for (cx, cy), cfg in cell_map.cells.items():
                t = cfg.block_name or 'unknown'
                by_type[t] = by_type.get(t, 0) + 1
            total = sum(by_type.values())
            free = self.config.width * self.config.height - total
            print(f'[ROUTER] Grid {self.config.width}x{self.config.height}={self.config.width*self.config.height}, Occupied={total}, Free={free}')
            for t, c in sorted(by_type.items()):
                print(f'[ROUTER]   {t}: {c}')
            # Trace output route from mux
            face_deltas = {Face.SOUTH: (0,1), Face.EAST: (1,0), Face.WEST: (-1,0), Face.NORTH: (0,-1)}
            for name, pb in placement.placed_blocks.items():
                if 'Mux' in name or 'mux' in name:
                    pos = pb.exit_cell
                    out_pos = self.get_output_position()
                    print(f'[ROUTER] Output trace from {name} exit={pos} to output={out_pos}:')
                    visited = set()
                    for step in range(50):
                        if pos in visited:
                            print(f'[ROUTER]   LOOP@{pos}'); break
                        visited.add(pos)
                        cfg = cell_map.get_cell(pos[0], pos[1])
                        if cfg is None:
                            print(f'[ROUTER]   {pos}: NONE'); break
                        fn = cfg.fwd_face.name if cfg.fwd_face is not None else '?'
                        print(f'[ROUTER]   {pos}: {fn} ({cfg.block_name})')
                        if cfg.fwd_face is None:
                            print(f'[ROUTER]   (fwd_face is None, stopping)')
                            break
                        dx, dy = face_deltas[cfg.fwd_face]
                        pos = (pos[0]+dx, pos[1]+dy)
                        if not (0<=pos[0]<self.config.width and 0<=pos[1]<self.config.height):
                            print(f'[ROUTER]   {pos}: OFF_GRID'); break

        return cell_map

    def _configure_block_cells(
        self,
        pb: PlacedBlock,
        block_def: Optional[BlockDefinition],
        cell_map: CellMap,
    ):
        """
        Configure cells within a placed block.

        Sets up internal routing (each cell forwards to next) and
        copies cell programs from the block definition.
        """
        cells = pb.cells  # Ordered list of (col, row)

        # Cell-program keys in positional order (may be ints or str ids like the
        # DFE's "ff0"…). Index i lines up with cells[i].
        prog_keys = list(block_def.cell_programs.keys()) if block_def else []
        # Map src cell-id -> dst cell-id for explicit internal handoffs, so a
        # cell faces its DECLARED successor (e.g. the DFE's ff20 -> lock_drv ->
        # dc) instead of the positional next cell.
        internal = getattr(block_def, "internal_connections", None) or []
        src_to_dst = {src_cid: dst_cid for (src_cid, _sp, dst_cid, _dp) in internal}

        for i, (col, row) in enumerate(cells):
            config = CellConfig(
                block_name=pb.block.name,
                cell_index=i,
            )

            # FACE: prefer an explicit internal connection (face the declared
            # destination cell); otherwise face the next cell in positional order.
            face = None
            cell_key = prog_keys[i] if i < len(prog_keys) else None
            dst_cid = src_to_dst.get(cell_key)
            if dst_cid is not None and dst_cid in prog_keys:
                d = prog_keys.index(dst_cid)
                if d < len(cells):
                    dcol, drow = cells[d]
                    face = self._get_face_to_neighbor(col, row, dcol, drow)
            if face is None and i < len(cells) - 1:
                next_col, next_row = cells[i + 1]
                face = self._get_face_to_neighbor(col, row, next_col, next_row)
            if face is not None:
                config.fwd_face = face

            # Copy cell program from block definition
            if block_def and i in block_def.cell_programs:
                prog = block_def.cell_programs[i]
                config.memory = dict(prog.memory)
                config.entry_addr = prog.entry_addr
                if prog.fwd_face is not None:
                    config.fwd_face = Face(prog.fwd_face)

            cell_map.set_cell(col, row, config)

    def _route_block_connections(
        self,
        pb: PlacedBlock,
        block_def: BlockDefinition,
        placement: Placement,
        cell_map: CellMap,
    ):
        """
        Route connections from this block to its connected blocks.
        """
        for conn in block_def.connections:
            target_name = conn.target
            if target_name not in placement.placed_blocks:
                continue

            target_pb = placement.placed_blocks[target_name]

            # Route from exit cell to target entry cell
            exit_cell = pb.exit_cell
            entry_cell = target_pb.entry_cell

            # Check if direct neighbors
            face = self._get_face_to_neighbor(
                exit_cell[0], exit_cell[1],
                entry_cell[0], entry_cell[1]
            )

            if face is not None:
                # Direct neighbor - update exit cell's FACE
                config = cell_map.get_cell(exit_cell[0], exit_cell[1])
                if config:
                    config.fwd_face = face
            else:
                # Need routing path - no passable_prefixes, each route gets dedicated cells
                first_hop_face = self._get_first_hop_face(exit_cell, entry_cell, cell_map)

                # Update exit cell's face to point toward the routing
                config = cell_map.get_cell(exit_cell[0], exit_cell[1])
                if config and first_hop_face is not None:
                    config.fwd_face = first_hop_face

                # Create the routing path
                self._create_routing_path(
                    exit_cell, entry_cell, cell_map,
                    f"route_{pb.block.name}_to_{target_name}",
                )

    def _route_input_port(self, placement: Placement, blocks: List[BlockDefinition], cell_map: CellMap):
        """
        Route from input port to the source block (the one with no incoming connections).

        The input port is at the configured position (typically (0,0)).
        We route to the block that has no incoming connections from other blocks.
        """
        if not placement.placed_blocks:
            return

        input_pos = self.get_input_position()

        # Find the source block (the one with no incoming connections)
        blocks_with_inputs = set()
        for b in blocks:
            for conn in b.connections:
                blocks_with_inputs.add(conn.target)

        # Find blocks with no inputs - these are the source blocks
        source_blocks = [
            pb for name, pb in placement.placed_blocks.items()
            if name not in blocks_with_inputs
        ]

        if source_blocks:
            first_block = source_blocks[0]
        else:
            # Fallback: just use first block in dict
            first_block = next(iter(placement.placed_blocks.values()))

        entry_cell = first_block.entry_cell

        # Create routing path from input to entry
        # No passable_prefixes - each route gets its own dedicated cells
        self._create_routing_path(
            input_pos, entry_cell, cell_map,
            "_input_route",
        )

    def _route_output_port(self, placement: Placement, blocks: List[BlockDefinition], cell_map: CellMap):
        """
        Route from the sink block (the one with no outgoing connections) to the output port.

        Finds the block with no outgoing connections and routes its exit to the output port.

        The output port cell's FWD_FACE must point to the output port face
        so data transits through and exits to be captured.
        """
        if not placement.placed_blocks:
            return

        output_pos = self.get_output_position()

        # Find the sink block (the one with no outgoing connections)
        block_lookup = {b.name: b for b in blocks}
        sink_blocks = [
            pb for name, pb in placement.placed_blocks.items()
            if name in block_lookup and not block_lookup[name].connections
        ]

        if sink_blocks:
            last_block = sink_blocks[0]
        else:
            # Fallback: just use last block in dict
            last_block = list(placement.placed_blocks.values())[-1]

        exit_cell = last_block.exit_cell

        # Determine the direction from exit cell to first routing cell
        # No passable_prefixes - output route gets its own dedicated cells
        first_hop_face = self._get_first_hop_face(exit_cell, output_pos, cell_map)

        # Update the sink block's exit cell face to point toward output routing
        config = cell_map.get_cell(exit_cell[0], exit_cell[1])
        if config and first_hop_face is not None:
            config.fwd_face = first_hop_face

        # Create routing path from exit to output
        self._create_routing_path(
            exit_cell, output_pos, cell_map,
            "_output_route",
        )

        # Set the output port cell's FWD_FACE to point to the output port face
        # This ensures data transits through the cell and exits to be captured
        if self._output_port and self.config.ports:
            port = self.config.ports[self._output_port]
            # Convert region.Face to cell_map.Face by name
            output_face = Face[port.face.name]
            config = cell_map.get_cell(output_pos[0], output_pos[1])
            if config:
                config.fwd_face = output_face
            else:
                # Create the output cell config if it doesn't exist
                cell_map.add_routing_cell(output_pos[0], output_pos[1], output_face, "_output_port")

    def _create_routing_path(
        self,
        from_pos: Tuple[int, int],
        to_pos: Tuple[int, int],
        cell_map: CellMap,
        route_name: str,
        passable_prefixes: Optional[Set[str]] = None,
    ):
        """
        Create a routing path between two positions, avoiding programmed blocks
        and existing routing cells.

        Uses A* pathfinding to find a route that doesn't pass through programmed
        cells (which have entry_addr set) or cells already used as routing cells
        by previous routes. This prevents routing conflicts where a later route
        overwrites an earlier route's fwd_face.

        Each cell along the path gets its fwd_face set to point to the next cell.
        The destination cell is NOT modified (it's the target, not part of the path).

        Args:
            passable_prefixes: Set of block_name prefixes that this route is allowed
                to traverse (but not overwrite). Used so I/O routes can share cells
                when they happen to go the same direction. Cells with these prefixes
                are treated as passable but only if the face is compatible.
        """
        import heapq

        fx, fy = from_pos
        tx, ty = to_pos

        # If source is same as destination, nothing to do
        if from_pos == to_pos:
            return

        if passable_prefixes is None:
            passable_prefixes = set()

        # Identify blocked cells and "shareable" cells:
        # 1. Programmed blocks (entry_addr set) - don't route through active code
        # 2. Existing routing cells (no entry_addr but already configured by a
        #    previous route) - don't overwrite their fwd_face
        # Exception: the source and destination cells are never blocked
        # Shareable: cells whose block_name starts with a passable prefix are
        # traversable but we won't overwrite their fwd_face.
        blocked = set()
        shareable = set()  # Can traverse but not modify
        for (cx, cy), cfg in cell_map.cells.items():
            if (cx, cy) == from_pos or (cx, cy) == to_pos:
                continue
            if cfg.entry_addr is not None:
                blocked.add((cx, cy))
            elif cfg.block_name and cfg.block_name.startswith("route_"):
                # This is a routing cell from a previous route - block it
                blocked.add((cx, cy))
            elif cfg.block_name and cfg.block_name.startswith("_"):
                # Check if this is a passable I/O route cell
                is_passable = any(cfg.block_name.startswith(p) for p in passable_prefixes)
                if is_passable:
                    shareable.add((cx, cy))
                else:
                    blocked.add((cx, cy))

        def heuristic(pos):
            """Manhattan distance heuristic."""
            return abs(pos[0] - tx) + abs(pos[1] - ty)

        def get_neighbors(pos):
            """Get valid neighbors (not blocked, within bounds)."""
            x, y = pos
            neighbors = []
            for dx, dy, face in [(1, 0, Face.EAST), (-1, 0, Face.WEST),
                                  (0, 1, Face.SOUTH), (0, -1, Face.NORTH)]:
                nx, ny = x + dx, y + dy
                # Check bounds
                if 0 <= nx < self.config.width and 0 <= ny < self.config.height:
                    # Allow if not blocked, or if it's the destination or shareable
                    if (nx, ny) == to_pos or ((nx, ny) not in blocked):
                        # For shareable cells, add a cost penalty to prefer fresh cells
                        # but still allow traversal
                        neighbors.append(((nx, ny), face))
            return neighbors

        # A* search
        # Priority queue entries: (priority, counter, pos, path)
        # counter is used as tiebreaker
        counter = 0
        start_entry = (heuristic(from_pos), counter, from_pos, [])
        heap = [start_entry]
        visited = set()

        path_result = None

        while heap:
            _, _, pos, path = heapq.heappop(heap)

            if pos in visited:
                continue
            visited.add(pos)

            # Check if we reached the destination
            if pos == to_pos:
                path_result = path
                break

            # Explore neighbors
            for (npos, face), in [(n,) for n in get_neighbors(pos)]:
                if npos not in visited:
                    new_path = path + [(pos, face)]
                    g_cost = len(new_path)
                    f_cost = g_cost + heuristic(npos)
                    counter += 1
                    heapq.heappush(heap, (f_cost, counter, npos, new_path))

        # If no path found, fall back to simple Manhattan routing
        if path_result is None:
            import os
            if os.environ.get('ROUTER_DEBUG'):
                print(f'[ROUTER] WARNING: A* failed for {route_name}: {from_pos} -> {to_pos}, using Manhattan fallback')
            # Fallback: simple Manhattan - but mark cells that are already occupied
            # so the application loop can skip them instead of overwriting
            path_result = []
            x, y = fx, fy
            while x != tx:
                face = Face.EAST if tx > x else Face.WEST
                path_result.append(((x, y), face))
                x += 1 if tx > x else -1
            while y != ty:
                face = Face.SOUTH if ty > y else Face.NORTH
                path_result.append(((x, y), face))
                y += 1 if ty > y else -1

        # Debug: log path
        import os
        if os.environ.get('ROUTER_DEBUG'):
            print(f'[ROUTER] Path {route_name}: {from_pos} -> {to_pos}, {len(path_result)} steps')
            for (px, py), face in path_result:
                existing = cell_map.get_cell(px, py)
                bn = existing.block_name if existing else 'None'
                print(f'[ROUTER]   ({px},{py}) face={face.name} existing_block={bn}')

        # Apply faces to all cells in the path
        for (px, py), face in path_result:
            existing = cell_map.get_cell(px, py)
            if existing is None:
                cell_map.add_routing_cell(px, py, face, route_name)
            elif (px, py) in shareable:
                # Shareable cell from another I/O route - don't overwrite fwd_face
                pass
            elif existing.block_name and (existing.block_name.startswith("_") or existing.block_name.startswith("route_")):
                # This cell belongs to another route - NEVER overwrite it.
                # This happens when Manhattan fallback goes through occupied cells.
                import os
                if os.environ.get('ROUTER_DEBUG'):
                    print(f'[ROUTER] SKIPPING overwrite of ({px},{py}) block={existing.block_name} by {route_name}')
            elif existing.entry_addr is not None:
                # Programmed block - never overwrite
                pass
            else:
                # Unowned routing cell - safe to update
                existing.fwd_face = face

    def _get_face_to_neighbor(
        self,
        from_col: int,
        from_row: int,
        to_col: int,
        to_row: int,
    ) -> Optional[Face]:
        """
        Get the face needed to reach a neighbor cell.

        Returns None if cells are not adjacent.
        """
        dcol = to_col - from_col
        drow = to_row - from_row

        if dcol == 1 and drow == 0:
            return Face.EAST
        elif dcol == -1 and drow == 0:
            return Face.WEST
        elif dcol == 0 and drow == 1:
            return Face.SOUTH
        elif dcol == 0 and drow == -1:
            return Face.NORTH
        else:
            return None

    def _get_first_hop_face(
        self,
        from_pos: Tuple[int, int],
        to_pos: Tuple[int, int],
        cell_map: CellMap,
        passable_prefixes: Optional[Set[str]] = None,
    ) -> Optional[Face]:
        """
        Get the face direction for the first hop of a routing path.

        This uses the same A* logic as _create_routing_path to determine
        which direction the source cell should point to reach the routing path.

        Returns the face direction, or None if no path found.
        """
        import heapq

        if passable_prefixes is None:
            passable_prefixes = set()

        fx, fy = from_pos
        tx, ty = to_pos

        if from_pos == to_pos:
            return None

        # Identify blocked cells (same logic as _create_routing_path)
        blocked = set()
        for (cx, cy), cfg in cell_map.cells.items():
            if (cx, cy) == from_pos or (cx, cy) == to_pos:
                continue
            if cfg.entry_addr is not None:
                blocked.add((cx, cy))
            elif cfg.block_name and cfg.block_name.startswith("route_"):
                blocked.add((cx, cy))
            elif cfg.block_name and cfg.block_name.startswith("_"):
                is_passable = any(cfg.block_name.startswith(p) for p in passable_prefixes)
                if not is_passable:
                    blocked.add((cx, cy))

        def heuristic(pos):
            return abs(pos[0] - tx) + abs(pos[1] - ty)

        def get_neighbors(pos):
            x, y = pos
            neighbors = []
            for dx, dy, face in [(1, 0, Face.EAST), (-1, 0, Face.WEST),
                                  (0, 1, Face.SOUTH), (0, -1, Face.NORTH)]:
                nx, ny = x + dx, y + dy
                if 0 <= nx < self.config.width and 0 <= ny < self.config.height:
                    if (nx, ny) not in blocked or (nx, ny) == to_pos:
                        neighbors.append(((nx, ny), face))
            return neighbors

        # A* to find first step direction
        counter = 0
        start_entry = (heuristic(from_pos), counter, from_pos, None)  # (cost, counter, pos, first_face)
        heap = [start_entry]
        visited = set()

        while heap:
            _, _, pos, first_face = heapq.heappop(heap)

            if pos in visited:
                continue
            visited.add(pos)

            if pos == to_pos:
                return first_face

            for (npos, face), in [(n,) for n in get_neighbors(pos)]:
                if npos not in visited:
                    # Record the first face from the original position
                    new_first_face = first_face if first_face is not None else face
                    g_cost = 1 if first_face is None else 2  # Approximate cost
                    f_cost = g_cost + heuristic(npos)
                    counter += 1
                    heapq.heappush(heap, (f_cost, counter, npos, new_first_face))

        # No path found, try simple fallback
        # Prefer horizontal first, then vertical
        if tx != fx:
            return Face.EAST if tx > fx else Face.WEST
        elif ty != fy:
            return Face.SOUTH if ty > fy else Face.NORTH
        return None

    def _get_routing_distance(
        self,
        from_pos: Tuple[int, int],
        to_pos: Tuple[int, int],
        cell_map: CellMap,
    ) -> int:
        """
        Calculate the actual routing distance between two cells.

        First tries to trace the existing route in the cell_map by following
        fwd_face links. Falls back to Manhattan distance if no route exists.
        """
        fx, fy = from_pos
        tx, ty = to_pos

        if from_pos == to_pos:
            return 0

        # Trace the existing route by following fwd_face links
        face_deltas = {
            Face.SOUTH: (0, 1),
            Face.NORTH: (0, -1),
            Face.EAST: (1, 0),
            Face.WEST: (-1, 0),
        }
        pos = from_pos
        distance = 0
        visited = set()
        while pos != to_pos and distance < 100:
            if pos in visited:
                break  # Loop detected
            visited.add(pos)
            cfg = cell_map.get_cell(pos[0], pos[1])
            if cfg is None or cfg.fwd_face is None:
                break
            dx, dy = face_deltas[cfg.fwd_face]
            pos = (pos[0] + dx, pos[1] + dy)
            distance += 1

        if pos == to_pos:
            return distance

        # Fallback to Manhattan distance
        return abs(tx - fx) + abs(ty - fy)

        def heuristic(pos):
            return abs(pos[0] - tx) + abs(pos[1] - ty)

        def get_neighbors(pos):
            x, y = pos
            neighbors = []
            for dx, dy in [(1, 0), (-1, 0), (0, 1), (0, -1)]:
                nx, ny = x + dx, y + dy
                if 0 <= nx < self.config.width and 0 <= ny < self.config.height:
                    if (nx, ny) not in blocked or (nx, ny) == to_pos:
                        neighbors.append((nx, ny))
            return neighbors

        # A* search
        counter = 0
        start_entry = (heuristic(from_pos), counter, from_pos, 0)  # (cost, counter, pos, distance)
        heap = [start_entry]
        visited = set()

        while heap:
            _, _, pos, dist = heapq.heappop(heap)

            if pos in visited:
                continue
            visited.add(pos)

            if pos == to_pos:
                return dist

            for npos in get_neighbors(pos):
                if npos not in visited:
                    new_dist = dist + 1
                    f_cost = new_dist + heuristic(npos)
                    counter += 1
                    heapq.heappush(heap, (f_cost, counter, npos, new_dist))

        # No path found, return Manhattan distance as fallback
        return abs(tx - fx) + abs(ty - fy)

    def _resolve_new_style_blocks(
        self,
        placement: Placement,
        blocks: List[BlockDefinition],
        cell_map: CellMap,
    ) -> Set[str]:
        """
        Resolve new-style (template-based) blocks after routing.

        For each new-style block:
        1. Compute routing distances to targets
        2. Build ResolvedTargets (WRITE hop counts, JUMP targets)
        3. Run CellProgramResolver to produce final memory contents
        4. Update cell_map with resolved programs

        Returns set of block names that were resolved (to skip in fixup).
        """
        resolver = CellProgramResolver()
        block_lookup = {b.name: b for b in blocks}
        resolved_names: Set[str] = set()

        for name, pb in placement.placed_blocks.items():
            block_def = block_lookup.get(name)
            if not block_def:
                continue

            # Check if any cell program has an assembly_template
            has_template = any(
                cp.assembly_template
                for cp in block_def.cell_programs.values()
            )
            if not has_template:
                continue

            resolved_names.add(name)

            # Process each cell in the block. ``cell_idx`` is the cell_programs
            # KEY, which may be an int (most blocks) or a string cell_id (e.g.
            # the DFE's "ff0".."ffN"). ``cell_pos`` is the POSITIONAL index into
            # ``pb.cells`` (the ordered placed positions); cell_programs is
            # ordered to match the block's layout, so position lines up.
            for cell_pos, (cell_idx, cell_prog) in enumerate(
                    block_def.cell_programs.items()):
                if not cell_prog.assembly_template:
                    continue

                # Build resolved targets from routing
                targets = ResolvedTargets()

                # For each output port, find the target cell and compute distance
                for out_port in cell_prog.outputs:
                    port_name = out_port.name

                    # Find target via block connections
                    target_info = self._find_output_target(
                        name, port_name, cell_idx, block_def,
                        placement, block_lookup, cell_map,
                        cell_pos=cell_pos,
                    )

                    if target_info is not None:
                        distance, target_addr, target_entry = target_info
                        targets.writes[port_name] = WriteTarget(
                            distance=distance,
                            target_addr=target_addr,
                        )
                        targets.jumps[port_name] = JumpTarget(
                            distance=distance,
                            target_addr=target_entry,
                        )

                # Resolve the template
                resolved = resolver.resolve(cell_prog, targets)

                # Update cell_map with resolved program
                cells = pb.cells
                if cell_pos < len(cells):
                    col, row = cells[cell_pos]
                    config = cell_map.get_cell(col, row)
                    if config is not None:
                        config.memory = dict(resolved.memory)
                        config.entry_addr = resolved.entry_addr

        return resolved_names

    def _find_output_target(
        self,
        block_name: str,
        port_name: str,
        cell_idx: int,
        block_def: BlockDefinition,
        placement: Placement,
        block_lookup: Dict[str, BlockDefinition],
        cell_map: CellMap,
        cell_pos: Optional[int] = None,
    ) -> Optional[Tuple[int, int, int]]:
        """
        Find the target for an output port and compute routing distance.

        Returns (distance, target_input_addr, target_entry_addr) or None.
        """
        pb = placement.placed_blocks[block_name]
        exit_cell = pb.exit_cell

        # SELF-TERMINATING port: a block may declare that an output port goes
        # NOWHERE downstream — it must self-terminate locally, not fall through to
        # the positional-next cell. This is essential for cells that conditionally
        # branch (e.g. the Gardner TED/resampler: a MID-strobe path JUMPs to a dead
        # end while a CENTER-strobe path advances the chain). The block declares
        # this with a destination cell id of "__terminate__" in either
        # internal_connections or internal_jumps. Resolve it to a local terminator:
        # @0 (HOP_CNT=31, executes locally) at entry 31 (HALT) / dead reg 31. This
        # mirrors the proven proto's JumpTarget(0, 31)/WriteTarget(0, dead).
        _ic = getattr(block_def, "internal_connections", None) or []
        _ij = getattr(block_def, "internal_jumps", None) or []
        for (_scid, _sport, _dcid, _dport) in list(_ic) + list(_ij):
            if _scid == cell_idx and _sport == port_name and _dcid == "__terminate__":
                return (0, 31, 31)

        # EXPLICIT internal connection (declared by the block) takes precedence
        # over the positional default below. This is how a NON-LINEAR multi-cell
        # block (e.g. the DFE: ff20 -> lock-driver -> dc, not ff20 -> fb0) routes
        # its internal handoffs. Each entry is
        # (src_cell_id, src_output_port, dst_cell_id, dst_input_port).
        internal = getattr(block_def, "internal_connections", None) or []
        for (src_cid, src_port, dst_cid, dst_port) in internal:
            if src_cid != cell_idx or src_port != port_name:
                continue
            keys = list(block_def.cell_programs.keys())
            if dst_cid not in keys:
                continue
            dst_pos_idx = keys.index(dst_cid)
            if cell_pos is None or cell_pos >= len(pb.cells) \
                    or dst_pos_idx >= len(pb.cells):
                continue
            src_pos = pb.cells[cell_pos]
            dst_pos = pb.cells[dst_pos_idx]
            distance = self._get_routing_distance(src_pos, dst_pos, cell_map)
            dst_cp = block_def.cell_programs[dst_cid]
            # An empty dst_port means "write to the target's R0" (e.g. the DFE
            # lock driver writing the FF sum into DC's accumulator). Otherwise
            # resolve the named DESTINATION input directly — the block named it,
            # so don't use the fwd_X->X_in heuristic.
            _ti, target_entry = self._resolve_named_input(dst_cp, None)
            if dst_port == "":
                target_input = 0
            else:
                target_input, _e = self._resolve_named_input(dst_cp, dst_port)
            return (distance, target_input, target_entry)

        # INTERNAL forward (DEFAULT): a non-last cell of a multi-cell block hands
        # off to the NEXT cell in the chain (cell_pos + 1). cell_programs is
        # ordered to match pb.cells, so the next placed position is the target.
        # The distance is the routed path between them (traced through the block's
        # own transit cells via fwd_face) — abutting cells give @1, a serpentine
        # wrap gives the real transit distance. Each NAMED output (e.g.
        # "fwd_partial") maps to the next cell's matching NAMED input
        # ("partial_in"/"partial"), so a multi-signal chain (sample/error/partial)
        # lands in the right registers.
        if cell_pos is not None:
            cps = list(block_def.cell_programs.values())
            if cell_pos < len(cps) - 1 and cell_pos + 1 < len(pb.cells):
                src_pos = pb.cells[cell_pos]
                next_pos = pb.cells[cell_pos + 1]
                distance = self._get_routing_distance(src_pos, next_pos, cell_map)
                next_cp = cps[cell_pos + 1]
                target_input, target_entry = self._resolve_named_input(
                    next_cp, port_name)
                return (distance, target_input, target_entry)

        # Check inter-block connections first
        for conn in block_def.connections:
            target_name = conn.target
            if target_name not in placement.placed_blocks:
                continue

            target_pb = placement.placed_blocks[target_name]
            target_block = block_lookup.get(target_name)
            entry_cell = target_pb.entry_cell

            # Compute routing distance
            distance = self._get_routing_distance(exit_cell, entry_cell, cell_map)

            # Determine the target block's landing cell (first cell that
            # declares inputs — NOT necessarily key 0; multi-cell blocks like
            # the DFE key cell_programs by string ids "ff0"…). Resolve its entry
            # address and input register.
            target_cp = None
            if target_block and target_block.cell_programs:
                for cp in target_block.cell_programs.values():
                    if getattr(cp, "assembly_template", "") and getattr(cp, "inputs", None):
                        target_cp = cp
                        break
                if target_cp is None:
                    target_cp = next(
                        (cp for cp in target_block.cell_programs.values()
                         if getattr(cp, "assembly_template", "")),
                        next(iter(target_block.cell_programs.values())))
            target_input, target_entry = self._resolve_cell_landing(target_cp)
            return (distance, target_input, target_entry)

        # Check if this is the sink block (output goes to I/O port)
        if not block_def.connections:
            output_pos = self.get_output_position()
            distance = self._get_routing_distance(exit_cell, output_pos, cell_map)
            # For output port transit: distance+1 so data passes through without executing
            return (distance + 1, 0, 0)

        return None

    def _resolve_cell_landing(self, cell_cp) -> Tuple[int, int]:
        """(input_register, entry_address) for a cell program's landing.

        Resolves the FIRST input port register and the default entry address of
        a v2 CellProgram. Falls back to (31, 1) for a non-template program.
        """
        return self._resolve_named_input(cell_cp, None)

    @staticmethod
    def _match_input_name(out_name: str, input_names: list) -> Optional[str]:
        """Map an output port name to the matching input port name of the next
        cell. Convention: a ``fwd_X`` / ``X_out`` output feeds the ``X_in`` /
        ``X`` input. Returns the matched input name, or None."""
        base = out_name
        for prefix in ("fwd_", "out_"):
            if base.startswith(prefix):
                base = base[len(prefix):]
        for suffix in ("_out",):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
        names = set(input_names)
        for cand in (f"{base}_in", base, out_name, f"{out_name}_in"):
            if cand in names:
                return cand
        return None

    def _resolve_named_input(self, cell_cp, out_name) -> Tuple[int, int]:
        """(input_register, entry_address) of a cell, for a specific output name.

        ``out_name`` (e.g. "fwd_partial") selects the matching named input of the
        target cell ("partial_in"). When ``out_name`` is None or unmatched, falls
        back to the first input register. Entry address is the cell's default.
        """
        target_input = 31  # default
        target_entry = 1   # default
        if cell_cp is None:
            return (target_input, target_entry)
        if getattr(cell_cp, "assembly_template", "") and getattr(cell_cp, "inputs", None):
            resolver = CellProgramResolver()
            entry_addrs = resolver.compute_entry_addresses(cell_cp)
            if entry_addrs:
                default = (cell_cp.entries[0].name if cell_cp.entries else None)
                target_entry = entry_addrs.get(
                    default, next(iter(entry_addrs.values())))
            # Build name → register map from classification (role == input).
            cls = resolver.classify_addresses(cell_cp)
            name_to_addr = {c["name"]: a for a, c in cls.items()
                            if c["role"] == "input" and c["name"]}
            in_addrs = sorted(a for a, c in cls.items() if c["role"] == "input")
            # An internal feedback may target a persistent STATE var by exact name
            # (e.g. the Gardner loop filter writing the corrected period into the
            # resampler's `period` state) — resolve that register directly.
            state_to_addr = {c["name"]: a for a, c in cls.items()
                             if c["role"] == "state" and c["name"]}
            matched = None
            if out_name is not None and out_name in state_to_addr:
                target_input = state_to_addr[out_name]
            else:
                if out_name is not None:
                    matched = self._match_input_name(out_name, list(name_to_addr))
                if matched is not None:
                    target_input = name_to_addr[matched]
                elif in_addrs:
                    target_input = in_addrs[0]
        elif getattr(cell_cp, "entry_addr", None) is not None:
            target_entry = cell_cp.entry_addr
        return (target_input, target_entry)

    def _fixup_write_instructions(
        self,
        placement: Placement,
        blocks: List[BlockDefinition],
        cell_map: CellMap,
        skip_blocks: Optional[Set[str]] = None,
    ):
        """
        Fix up WRITE instructions with correct hop counts based on routing.

        WRITE instructions in the cell programs are built with placeholder
        hop counts. This method updates them based on actual placement.

        WRITE instruction format (v0.11): 0x6000 | (cfg << 10) | (hop_cnt << 5) | dest
        JUMP instruction format (v0.11): 0x7000 | (hop_cnt << 5) | dest

        HOP_CNT semantics: The instruction hop count field specifies how
        far the data should travel. The value is 31 - distance, where
        distance is the actual routing distance (not Manhattan).
        """
        block_lookup = {b.name: b for b in blocks}

        for name, pb in placement.placed_blocks.items():
            block_def = block_lookup.get(name)
            if not block_def:
                continue
            if skip_blocks and name in skip_blocks:
                continue

            # For each connection, calculate the hop count from exit cell to target entry
            for conn in block_def.connections:
                target_name = conn.target
                if target_name not in placement.placed_blocks:
                    continue

                target_pb = placement.placed_blocks[target_name]
                exit_cell = pb.exit_cell
                entry_cell = target_pb.entry_cell

                # Calculate actual routing distance (not Manhattan!)
                distance = self._get_routing_distance(exit_cell, entry_cell, cell_map)

                # HOP_CNT = 31 - distance for WRITE instructions generated by block programs
                # Unlike port injection, the exit cell creates the WRITE instruction locally
                # and sends it to the first routing cell. The instruction visits `distance`
                # cells total (not counting the exit cell), so:
                # hop_cnt + distance = 31, so hop_cnt = 31 - distance
                hop_cnt = 31 - distance

                # Get the cell config for the exit cell
                config = cell_map.get_cell(exit_cell[0], exit_cell[1])
                if config is None:
                    continue

                # Find and fix WRITE and JUMP instructions in memory
                # v0.11 opcodes: WRITE = 0x6, JUMP = 0x7 (bits [15:12])
                #
                # IMPORTANT: Only modify addresses in the CODE section (starting at
                # entry_addr and ending at HALT). Data values (like coefficients)
                # may happen to have bit patterns that look like WRITE/JUMP instructions,
                # but we must NOT modify them.
                #
                # Scan memory in ADDRESS ORDER starting from entry_addr until HALT.
                entry_addr = config.entry_addr or 1  # Default entry at R1
                for addr in range(entry_addr, 32):  # Max 32 registers
                    if addr not in config.memory:
                        continue  # Skip uninitialized addresses
                    value = config.memory[addr]

                    # Stop at HALT (value 0x0000)
                    if value == 0x0000:
                        break

                    opcode = (value >> 12) & 0xF

                    if opcode == 0x6:  # WRITE instruction (v0.11)
                        cfg_bit = (value >> 10) & 1  # Preserve CFG bit
                        dest = value & 0x1F
                        # Update with correct hop count
                        new_value = (0x6 << 12) | (cfg_bit << 10) | ((hop_cnt & 0x1F) << 5) | dest
                        config.memory[addr] = new_value
                    elif opcode == 0x7:  # JUMP instruction (v0.11)
                        dest = value & 0x1F
                        # Update with correct hop count
                        new_value = (0x7 << 12) | ((hop_cnt & 0x1F) << 5) | dest
                        config.memory[addr] = new_value

        # Also fix WRITE for output to the output port
        # Find the sink block (the one with no outgoing connections)
        if placement.placed_blocks:
            # Find the sink block
            sink_blocks = [
                pb for name, pb in placement.placed_blocks.items()
                if name in block_lookup and not block_lookup[name].connections
            ]

            if not sink_blocks:
                return  # No sink block found

            last_block = sink_blocks[0]
            exit_cell = last_block.exit_cell
            output_pos = self.get_output_position()

            # Calculate actual routing distance to output port (trace fwd_face links)
            distance = self._get_routing_distance(exit_cell, output_pos, cell_map)

            # The data must TRANSIT through the output port cell (not execute locally there)
            # This means HOP_CNT should NOT reach 31 at the output port cell.
            # The data exits through the output port's face and gets captured.
            #
            # HOP_CNT starts at some value and increments at each cell.
            # For data to transit through all `distance` cells:
            #   initial_hop_cnt + distance < 31
            #
            # We want the data to EXACTLY reach the output port and exit.
            # The data should NOT execute at any cell (including the output port cell).
            # So we need initial_hop_cnt such that even after `distance` increments,
            # HOP_CNT is still < 31.
            #
            # Safe choice: initial_hop_cnt = 30 - distance
            # This ensures: 30 - distance + distance = 30 < 31 at the output port
            # The data will transit through and exit without executing.
            hop_cnt = max(0, 30 - distance)

            config = cell_map.get_cell(exit_cell[0], exit_cell[1])
            if config:
                # The sink block has no connections, its WRITE/JUMP go to output port
                # v0.11 opcodes: WRITE = 0x6, JUMP = 0x7
                # Only modify code section, not data - scan in address order
                #
                # NOTE: We do NOT replace JUMP with HALT! The JUMP is needed to
                # complete the handshake with the port system for continuous
                # sample processing. The output port captures data when it transits
                # through, and JUMP triggers the next sample injection.
                #
                # MID-BLOCK OUTPUT: if the sink block declares output_at_last_write,
                # its exit cell ALSO carries internal-handoff WRITEs (e.g. a Costas
                # rotate cell: yi -> phase detector @1, AND yi_tap -> output port).
                # Patch ONLY the LAST WRITE (the output one, emitted last) so the
                # internal handoffs keep their resolved hops; leave JUMPs alone.
                sink_def = block_lookup.get(last_block.block.name)
                mid_output = bool(getattr(sink_def, "output_at_last_write", False))
                entry_addr = config.entry_addr or 1
                if mid_output:
                    write_addrs = []
                    for addr in range(entry_addr, 32):
                        if addr not in config.memory:
                            continue
                        value = config.memory[addr]
                        if value == 0x0000:
                            break
                        if (value >> 12) & 0xF == 0x6:
                            write_addrs.append(addr)
                    if write_addrs:
                        addr = max(write_addrs)
                        value = config.memory[addr]
                        cfg_bit = (value >> 10) & 1
                        dest = value & 0x1F
                        config.memory[addr] = (0x6 << 12) | (cfg_bit << 10) \
                            | ((hop_cnt & 0x1F) << 5) | dest
                    # done — internal WRITEs + JUMPs untouched.
                else:
                    for addr in range(entry_addr, 32):
                        if addr not in config.memory:
                            continue
                        value = config.memory[addr]
                        if value == 0x0000:
                            break
                        opcode = (value >> 12) & 0xF
                        if opcode == 0x6:  # WRITE (v0.11)
                            cfg_bit = (value >> 10) & 1  # Preserve CFG bit
                            dest = value & 0x1F
                            new_value = (0x6 << 12) | (cfg_bit << 10) | ((hop_cnt & 0x1F) << 5) | dest
                            config.memory[addr] = new_value
                        elif opcode == 0x7:  # JUMP (v0.11)
                            # Update JUMP hop count to match WRITE - same destination
                            dest = value & 0x1F
                            new_value = (0x7 << 12) | ((hop_cnt & 0x1F) << 5) | dest
                            config.memory[addr] = new_value


def route_placement(
    placement: Placement,
    blocks: List[BlockDefinition],
    config: ArrayConfig,
    input_port: Optional[str] = None,
    output_port: Optional[str] = None,
) -> CellMap:
    """
    Convenience function to route a placement.

    Args:
        placement: Completed placement
        blocks: Block definitions with cell programs
        config: Array configuration
        input_port: Name of input port to use (defaults to first input port)
        output_port: Name of output port to use (defaults to first output port)

    Returns:
        Complete cell map
    """
    router = Router(config, input_port=input_port, output_port=output_port)
    return router.route(placement, blocks)
