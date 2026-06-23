"""
CellProgram Resolver

Resolves new-style CellProgram templates into fully-assembled memory layouts.

Address space layout (32 registers, addr 0-31):
- Data words packed at BOTTOM (addr 0 upward)
- Instructions packed at TOP (ending at addr 30, R31 reserved for HALT)
- State registers and input registers in the gap between data and instructions
- R31 is ALWAYS set to HALT (0x0000) to avoid output corruption under backpressure

Template placeholder syntax:
- R{in:name}     → register number where input data arrives
- R{data:name}   → register number where coefficient is stored
- R{state:name}  → register number for persistent state
- {write:name}   → full "WRITE @hop, addr" instruction line
- {jump:name}    → full "JUMP @hop, addr" instruction line
- {entry:name}   → address of a named entry point (integer)
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .block import CellProgram, Port, EntryPoint, StateVar, DataWord

try:
    import simkyt
    HAS_KYTTAR = True
except ImportError:
    HAS_KYTTAR = False


class ResolverError(Exception):
    """Raised when template resolution fails."""
    pass


@dataclass
class WriteTarget:
    """Resolved target for a WRITE placeholder."""
    distance: int      # Routing distance (used as @distance in assembly)
    target_addr: int   # Target register address (where data lands)


@dataclass
class JumpTarget:
    """Resolved target for a JUMP placeholder."""
    distance: int      # Routing distance (used as @distance in assembly)
    target_addr: int   # Target entry address


@dataclass
class ResolvedTargets:
    """All resolved targets for a single cell program."""
    writes: Dict[str, WriteTarget] = field(default_factory=dict)  # output_name -> WriteTarget
    jumps: Dict[str, JumpTarget] = field(default_factory=dict)    # name -> JumpTarget


# Regex patterns for placeholders
_RE_IN = re.compile(r'\{in:(\w+)\}')
_RE_DATA = re.compile(r'\{data:(\w+)\}')
_RE_STATE = re.compile(r'\{state:(\w+)\}')
_RE_WRITE = re.compile(r'^\s*\{write:(\w+)\}\s*$', re.MULTILINE)
_RE_JUMP = re.compile(r'^\s*\{jump:(\w+)\}\s*$', re.MULTILINE)
_RE_ENTRY = re.compile(r'\{entry:(\w+)\}')


class CellProgramResolver:
    """
    Resolves a new-style CellProgram template into a fully-assembled CellProgram.

    Two-phase resolution:
    - Phase 1: Allocate registers and count instructions (determines entry addresses)
    - Phase 2: Substitute WRITE/JUMP targets with known addresses, assemble final code
    """

    MAX_ADDR = 31  # Register file is 0-31

    def resolve(
        self,
        program: CellProgram,
        targets: Optional[ResolvedTargets] = None,
    ) -> CellProgram:
        """
        Resolve a template CellProgram into a fully-assembled one.

        Args:
            program: CellProgram with assembly_template and declarative fields
            targets: Resolved WRITE/JUMP targets (from router). If None,
                     uses dummy values (useful for dry-run/counting).

        Returns:
            New CellProgram with populated memory dict and entry_addr
        """
        if not program.assembly_template:
            raise ResolverError("CellProgram has no assembly_template")

        if targets is None:
            targets = ResolvedTargets()

        template = program.assembly_template

        # --- Phase 1: Allocate addresses and registers ---

        # 1a. Pack data words at bottom (addr 0 upward)
        data_map = self._allocate_data(program.data)
        next_data_addr = max(data_map.values(), default=-1) + 1

        # 1b. Count instructions by doing a dry-run substitution
        #     Use dummy register/target values just to get line count
        dummy_asm = self._substitute_registers(
            template, program, data_map,
            state_map={}, input_map={},
            dummy=True,
        )
        dummy_asm = self._substitute_write_jump(
            dummy_asm, targets, dummy=True,
        )
        instr_count = self._count_instructions(dummy_asm)

        # 1c. Instructions pack at top: base_addr = 31 - instr_count
        #     R31 is auto-set to HALT. Only external WRITE/JUMP instructions
        #     are unsafe at R31 (backpressure lockup). Other instructions
        #     (MOVE, ADD, CMP, BR, etc.) are safe at R31 since they are
        #     self-timed and don't interact with the output fabric.
        base_addr = 31 - instr_count
        if base_addr < next_data_addr:
            raise ResolverError(
                f"Not enough register space: {instr_count} instructions need "
                f"addr {base_addr}-31, but data occupies addr 0-{next_data_addr - 1}"
            )

        # 1d. Allocate state and input registers in the gap
        gap_start = next_data_addr
        gap_end = base_addr  # exclusive
        gap_regs = list(range(gap_start, gap_end))

        state_map = self._allocate_state(program.state, gap_regs)
        used_state = set(state_map.values())
        remaining_regs = [r for r in gap_regs if r not in used_state]

        input_map = self._allocate_inputs(program.inputs, remaining_regs)
        used_inputs = set(input_map.values())
        remaining_regs = [r for r in remaining_regs if r not in used_inputs]

        # Also allocate output source registers if specified
        # (outputs typically use R0 / accumulator, but can be explicit)
        output_map = self._allocate_outputs(program.outputs, remaining_regs)

        # 1e. Compute entry point addresses (label-aware: an entry named like a
        #     template label resolves to that label's address). The dummy asm has
        #     the same instruction layout as the final, so label addresses match.
        entry_map = self._compute_entries(
            program.entries, base_addr, template, resolved_asm=dummy_asm)

        # --- Phase 2: Final substitution and assembly ---

        # 2a. Substitute register placeholders
        final_asm = self._substitute_registers(
            template, program, data_map,
            state_map=state_map, input_map=input_map,
            dummy=False,
        )

        # 2b. Substitute entry placeholders
        final_asm = self._substitute_entries(final_asm, entry_map)

        # 2c. Substitute WRITE/JUMP placeholders
        final_asm = self._substitute_write_jump(
            final_asm, targets, dummy=False,
        )

        # 2d. Assemble via Rust
        if not HAS_KYTTAR:
            raise ResolverError("simkyt module not available - cannot assemble")

        prog_obj = simkyt.Program.from_source("resolved", final_asm, base_addr)
        words = prog_obj.get_words()

        # --- Build final CellProgram ---
        result = CellProgram()
        result.fwd_face = program.fwd_face

        # Set entry address (default entry = first instruction)
        default_entry = program.entries[0].name if program.entries else "default"
        result.entry_addr = entry_map.get(default_entry, base_addr)

        # Load data words into memory
        for dw in program.data:
            addr = data_map[dw.name]
            result.memory[addr] = dw.value & 0xFFFF

        # Load state initial values into memory
        for sv in program.state:
            addr = state_map[sv.name]
            result.memory[addr] = sv.initial_value & 0xFFFF

        # Load instructions into memory
        for i, word in enumerate(words):
            result.memory[base_addr + i] = word & 0xFFFF

        # R31 is always HALT (0x0000)
        result.memory[31] = 0x0000

        return result

    def count_instructions(self, program: CellProgram) -> int:
        """
        Count instructions in a template (dry-run, no targets needed).

        Useful for Phase 1 of two-phase resolution where entry addresses
        need to be known across all cells before WRITE/JUMP can be resolved.
        """
        if not program.assembly_template:
            return 0
        data_map = self._allocate_data(program.data)
        dummy_asm = self._substitute_registers(
            program.assembly_template, program, data_map,
            state_map={}, input_map={}, dummy=True,
        )
        dummy_asm = self._substitute_write_jump(dummy_asm, ResolvedTargets(), dummy=True)
        return self._count_instructions(dummy_asm)

    def compute_entry_addresses(self, program: CellProgram) -> Dict[str, int]:
        """
        Compute entry point addresses without full resolution.

        Returns dict of entry_name -> address.
        """
        if not program.assembly_template:
            return {}

        data_map = self._allocate_data(program.data)
        next_data_addr = max(data_map.values(), default=-1) + 1

        dummy_asm = self._substitute_registers(
            program.assembly_template, program, data_map,
            state_map={}, input_map={}, dummy=True,
        )
        dummy_asm = self._substitute_write_jump(dummy_asm, ResolvedTargets(), dummy=True)
        instr_count = self._count_instructions(dummy_asm)
        base_addr = 31 - instr_count  # R31 reserved for HALT

        return self._compute_entries(
            program.entries, base_addr, program.assembly_template,
            resolved_asm=dummy_asm)

    def compute_state_registers(self, program: CellProgram) -> Dict[str, int]:
        """Compute state-var -> register WITHOUT full resolution (mirrors the
        allocation in :meth:`resolve`). Used to point an internal-feedback WRITE
        at a state register (e.g. a loop-filter feeding a corrected value back
        into the resampler's persistent `period` state)."""
        if not program.assembly_template:
            return {}
        data_map = self._allocate_data(program.data)
        next_data_addr = max(data_map.values(), default=-1) + 1
        instr_count = self.count_instructions(program)
        base_addr = 31 - instr_count
        gap_regs = list(range(next_data_addr, base_addr))
        return dict(self._allocate_state(program.state, gap_regs))

    def classify_addresses(self, program: CellProgram) -> Dict[int, Dict[str, Any]]:
        """Classify each used memory address of a v2 CellProgram by role.

        Returns ``{addr: {"role": str, "name": str|None}}`` where ``role`` is one
        of ``"data"``, ``"state"``, ``"input"``, ``"output"``, ``"instruction"``,
        or ``"halt"`` (R31). Mirrors the allocation done by :meth:`resolve`, so
        the addresses match the resolved memory image. Read-only; does not
        assemble. Empty for a non-template (v1) program.

        This lets a UI distinguish DATA words (coefficients, etc., which merely
        live in memory) from executable INSTRUCTIONS — a coefficient whose bits
        happen to match a WRITE/JUMP opcode is data, not an instruction.
        """
        out: Dict[int, Dict[str, Any]] = {}
        if not program.assembly_template:
            return out

        data_map = self._allocate_data(program.data)
        for name, addr in data_map.items():
            out[addr] = {"role": "data", "name": name}
        next_data_addr = max(data_map.values(), default=-1) + 1

        instr_count = self.count_instructions(program)
        base_addr = 31 - instr_count

        gap_regs = list(range(next_data_addr, base_addr))
        state_map = self._allocate_state(program.state, gap_regs)
        for name, addr in state_map.items():
            out[addr] = {"role": "state", "name": name}
        remaining = [r for r in gap_regs if r not in set(state_map.values())]
        input_map = self._allocate_inputs(program.inputs, remaining)
        for name, addr in input_map.items():
            out[addr] = {"role": "input", "name": name}
        remaining = [r for r in remaining if r not in set(input_map.values())]
        output_map = self._allocate_outputs(program.outputs, remaining)
        for name, addr in output_map.items():
            # Outputs commonly map to R0 (accumulator) — don't relabel R0 if a
            # more specific role already claimed it.
            out.setdefault(addr, {"role": "output", "name": name})

        for addr in range(base_addr, 31):
            out[addr] = {"role": "instruction", "name": None}
        out[31] = {"role": "halt", "name": None}
        return out

    # --- Internal helpers ---

    def _allocate_data(self, data: List[DataWord]) -> Dict[str, int]:
        """Pack data words at bottom of address space. Returns name -> addr."""
        result = {}
        next_addr = 0
        for dw in data:
            if dw.address is not None:
                result[dw.name] = dw.address
            else:
                result[dw.name] = next_addr
                next_addr += 1
        # For explicit addresses, update next_addr
        if result:
            next_addr = max(next_addr, max(result.values()) + 1)
        return result

    def _allocate_state(self, state: List[StateVar], gap_regs: List[int]) -> Dict[str, int]:
        """Allocate state registers. Returns name -> register."""
        result = {}
        auto_idx = 0
        for sv in state:
            if sv.register is not None:
                result[sv.name] = sv.register
            else:
                if auto_idx >= len(gap_regs):
                    raise ResolverError(f"No register space for state '{sv.name}'")
                result[sv.name] = gap_regs[auto_idx]
                auto_idx += 1
        return result

    def _allocate_inputs(self, inputs: List[Port], gap_regs: List[int]) -> Dict[str, int]:
        """Allocate input registers. Returns name -> register."""
        result = {}
        auto_idx = 0
        for port in inputs:
            if port.register is not None:
                result[port.name] = port.register
            else:
                if auto_idx >= len(gap_regs):
                    raise ResolverError(f"No register space for input '{port.name}'")
                result[port.name] = gap_regs[auto_idx]
                auto_idx += 1
        return result

    def _allocate_outputs(self, outputs: List[Port], gap_regs: List[int]) -> Dict[str, int]:
        """Allocate output source registers. Returns name -> register."""
        result = {}
        auto_idx = 0
        for port in outputs:
            if port.register is not None:
                result[port.name] = port.register
            else:
                # Default: output from R0 (accumulator)
                result[port.name] = 0
        return result

    def _compute_entries(
        self, entries: List[EntryPoint], base_addr: int, template: str,
        resolved_asm: Optional[str] = None,
    ) -> Dict[str, int]:
        """Compute entry point addresses. Returns name -> address.

        Resolution order for each :class:`EntryPoint`:
          1. an explicit ``ep.address`` wins;
          2. else, if the entry's name matches a **label** in the (resolved)
             assembly, the entry is that label's address — this lets a
             multi-entry control block (e.g. an SRAM controller with separate
             write/read entries) point each entry at a labelled section;
          3. else, the block's first instruction (``base_addr``).

        ``resolved_asm`` is a fully (dummy- or final-) substituted assembly used
        only to read label addresses; when absent, only rules 1 and 3 apply.
        """
        label_addrs: Dict[str, int] = {}
        if resolved_asm is not None and HAS_KYTTAR:
            try:
                prog = simkyt.Program.from_source(
                    "entries", resolved_asm, base_addr)
                # simkyt upper-cases label names.
                for lbl in prog.get_labels():
                    label_addrs[lbl.upper()] = prog.get_label_address(lbl)
            except Exception:  # noqa: BLE001 — fall back to base_addr
                label_addrs = {}
        result = {}
        for ep in entries:
            if ep.address is not None:
                result[ep.name] = ep.address
            elif ep.name.upper() in label_addrs:
                result[ep.name] = label_addrs[ep.name.upper()]
            else:
                result[ep.name] = base_addr
        return result

    def _substitute_registers(
        self,
        template: str,
        program: CellProgram,
        data_map: Dict[str, int],
        state_map: Dict[str, int],
        input_map: Dict[str, int],
        dummy: bool = False,
    ) -> str:
        """Substitute {in:name}, {data:name}, {state:name} with register numbers."""
        result = template

        def replace_in(m):
            name = m.group(1)
            if dummy:
                return '0'
            if name not in input_map:
                raise ResolverError(f"Unknown input port '{name}'")
            return str(input_map[name])

        def replace_data(m):
            name = m.group(1)
            if name not in data_map:
                raise ResolverError(f"Unknown data word '{name}'")
            return str(data_map[name])

        def replace_state(m):
            name = m.group(1)
            if dummy:
                return '0'
            if name not in state_map:
                raise ResolverError(f"Unknown state var '{name}'")
            return str(state_map[name])

        result = _RE_IN.sub(replace_in, result)
        result = _RE_DATA.sub(replace_data, result)
        result = _RE_STATE.sub(replace_state, result)
        return result

    def _substitute_entries(self, template: str, entry_map: Dict[str, int]) -> str:
        """Substitute {entry:name} with entry point addresses."""
        def replace_entry(m):
            name = m.group(1)
            if name not in entry_map:
                raise ResolverError(f"Unknown entry point '{name}'")
            return str(entry_map[name])
        return _RE_ENTRY.sub(replace_entry, template)

    def _substitute_write_jump(
        self,
        template: str,
        targets: ResolvedTargets,
        dummy: bool = False,
    ) -> str:
        """Substitute {write:name} and {jump:name} with full instructions."""
        def replace_write(m):
            name = m.group(1)
            if dummy:
                return f'    WRITE @1, 0'
            if name not in targets.writes:
                raise ResolverError(f"No write target for output '{name}'")
            t = targets.writes[name]
            return f'    WRITE @{t.distance}, {t.target_addr}'

        def replace_jump(m):
            name = m.group(1)
            if dummy:
                return f'    JUMP @1, 0'
            if name not in targets.jumps:
                raise ResolverError(f"No jump target for '{name}'")
            t = targets.jumps[name]
            return f'    JUMP @{t.distance}, {t.target_addr}'

        result = _RE_WRITE.sub(replace_write, template)
        result = _RE_JUMP.sub(replace_jump, result)
        return result

    def _count_instructions(self, assembly: str) -> int:
        """
        Count instructions in assembly text.

        Counts non-empty, non-comment, non-label lines.
        """
        count = 0
        for line in assembly.split('\n'):
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith(';'):
                continue
            if stripped.endswith(':'):
                continue
            count += 1
        return count
