"""SramControllerBlock — see :class:`SramControllerBlock`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class SramControllerBlock(KyttarBlock):
    """
    SRAM Controller Block (1 cell) — memory-controller for an SRAM panel.

    Sits adjacent to (or at) the panel's port and owns ALL panel sequencing, so
    upstream blocks just stream data. Auto-increments the write and read
    addresses internally: the upstream side only sets a base address once (or
    uses the default 0) and then streams. Drives the SRAM panel register
    protocol (see SRAM_PANEL.md) over the chip port it faces.

    Entries:
      * ``write``: WRITE wr_addr→panel R5, data→R2, JUMP→R0 (commit). wr_addr++.
      * ``read``:  set R3/R4 read-out descriptors, WRITE rd_addr→R5,
        JUMP→R1 (read trigger). rd_addr++.
      * ``set_addr``: load the incoming value into BOTH address counters (reset).

    Parameters:
      * ``panel_hop``: hops from this cell to exit the panel port (@N). Default 1
        (the controller sits at the port cell; the WRITE/JUMP exit directly).
      * ``read_wr_desc`` / ``read_jp_desc``: raw 16-bit WRITE / JUMP descriptor
        words the panel re-emits on a read (where the read value lands). These
        are the push-read targets (see SRAM_PANEL.md §3).

    Interface:
      * Entry ``write`` (default). Input data in R{input} (default R25).
    """
    CATEGORY = "memory_interface"
    TAGS = ["sram", "controller", "memory_interface", "auto_increment"]
    # Authors its own panel-protocol WRITE/JUMP hops — keep them (no @1 default).
    RAW_OUTPUT_HOPS = True

    def __init__(self, name: str, panel_hop: int = 1,
                 read_wr_desc: int = 0, read_jp_desc: int = 0):
        super().__init__(name, panel_hop=panel_hop,
                         read_wr_desc=read_wr_desc, read_jp_desc=read_jp_desc)
        self._hop = panel_hop
        self._rwd = read_wr_desc & 0xFFFF
        self._rjd = read_jp_desc & 0xFFFF

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def interface(self) -> BlockInterface:
        return BlockInterface(entry_address=1, input_registers=[25],
                              output_registers=[25])

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        h = self._hop
        # R{data:one}=1, R{state:wraddr}, R{state:rdaddr}, R{in:data}=R25,
        # R{data:rwd}/R{data:rjd}=read descriptors.
        tmpl = (
            # --- write: addr->R5, data->R2, commit; wr_addr++ ---
            "write:\n"
            "    MOVE R0, R{state:wraddr}\n"
            f"    WRITE @{h}, 5\n"
            "    MOVE R0, R{in:data}\n"
            f"    WRITE @{h}, 2\n"
            "    MOVE R0, R{state:wraddr}\n"
            "    ADD R0, R{data:one}\n"
            "    MOVE R{state:wraddr}, R0\n"
            f"    JUMP @{h}, 0\n"
            "    HALT\n"
            # --- read: descriptors -> R3/R4, rd_addr -> R5, trigger R1; rd++ ---
            "read:\n"
            "    MOVE R0, R{data:rwd}\n"
            f"    WRITE @{h}, 3\n"
            "    MOVE R0, R{data:rjd}\n"
            f"    WRITE @{h}, 4\n"
            "    MOVE R0, R{state:rdaddr}\n"
            f"    WRITE @{h}, 5\n"
            "    MOVE R0, R{state:rdaddr}\n"
            "    ADD R0, R{data:one}\n"
            "    MOVE R{state:rdaddr}, R0\n"
            f"    JUMP @{h}, 1\n"
            "    HALT\n"
            # --- set_addr: load incoming value into both counters ---
            "set_addr:\n"
            "    MOVE R{state:wraddr}, R{in:data}\n"
            "    MOVE R{state:rdaddr}, R{in:data}\n"
            "    HALT\n"
        )
        return {0: CellProgram(
            # Auto-allocate the data input register: the program is ~22
            # instructions, so R25 etc. would be CODE, not a free register.
            inputs=[Port("data")],
            outputs=[Port("out")],
            entries=[EntryPoint("write"), EntryPoint("read"),
                     EntryPoint("set_addr")],
            # Pin data words to R1+ so the constant never lands on R0 (the
            # accumulator), which would make ADD R0, R{one} a no-op.
            data=[DataWord("one", 1, address=1),
                  DataWord("rwd", self._rwd, address=2),
                  DataWord("rjd", self._rjd, address=3)],
            state=[StateVar("wraddr"), StateVar("rdaddr")],
            assembly_template=tmpl,
        )}

    def process_reference(self, input_samples: np.ndarray) -> np.ndarray:
        return np.asarray(input_samples, dtype=np.uint16)

    def reset(self):
        pass
