# SPDX-License-Identifier: GPL-3.0-or-later
"""StreamSplitterBlock — see the class docstring."""

from ..block import CellProgram, EntryPoint, Port
from ._base import BlockInterface, KyttarBlock


class StreamSplitterBlock(KyttarBlock):
    """
    Stream splitter — an explicit 1-cell fan-out relay (``out[n] = in[n]`` on
    every arm).

    GNU Radio fans a port out implicitly (one output wired to N consumers);
    on the chip that fan-out takes program words in the SOURCE cell — one
    extra WRITE+JUMP pair per additional arm (the single-rail fan-out form,
    ``engine.build._patch_single_rail_multi_handoff``). A source whose exit
    cell is nearly full (e.g. GainBlock: 3 exit words) cannot hold more than
    its wired arms, and the build then aborts with a NAMED error pointing
    here. This block is the escape hatch: a near-empty relay cell (3
    instructions) with ~26 free exit words, so ONE splitter feeds many arms —
    and splitters chain into trees for still wider fan-outs.

    Value semantics: the relay is exact (the input word is re-emitted
    unchanged, no Q15 arithmetic) and memoryless → delay=0.

    Interface: one real input (R0), one output port ``out`` that any number
    of connections may leave from (like a GRC port).
    """
    CATEGORY = "routing"
    TAGS = ["splitter", "fan_out", "copy", "routing"]

    _interface = BlockInterface(
        entry_address=1, input_registers=[0], output_registers=[0])

    def __init__(self, name: str):
        super().__init__(name)

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> dict:
        # The trailing HALTs (0x0000) RESERVE exit words directly after the
        # authored WRITE/JUMP: programs assemble at the TOP of cell memory, so
        # without them the fan-out form would have only word 31 to grow into.
        # The build's _patch_single_rail_multi_handoff rewrites this tail into
        # ``WRITE₁ … WRITE_N; JUMP …`` — 14 reserved words = up to 8 arms
        # (8 WRITEs + 8 JUMPs over the 2 authored + 14 reserved slots). Unused
        # slots stay HALT, where execution stops.
        return {0: CellProgram(
            inputs=[Port("x", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            assembly_template="""\
start:
    MOVE R0, R{in:x}
    {write:out}
    {jump:out}
""" + "    HALT\n" * 14,
        )}

    def output_cell_ids(self):
        return [0]

    # -------------------------------------------------------------- reference
    def process_reference(self, input_samples):
        """Float reference: the identity (each arm re-emits the sample)."""
        return list(input_samples)

    def process_reference_q15(self, samples) -> list:
        """Bit-exact predictor: the identity (each arm re-emits the word)."""
        return [int(v) & 0xFFFF for v in samples]
