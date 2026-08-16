# SPDX-License-Identifier: GPL-3.0-or-later
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
      * ``lookup``: RANDOM-ACCESS read — set ``rd_addr`` from the incoming value,
        then fall through into ``read`` (descriptors → R3/R4, addr → R5, trigger).
        This is the STREAMING entry a table-lookup chain injects each key into
        (e.g. one ASCII byte per Varicode symbol): one data word + one JUMP does
        the whole ``sram[key]`` push-read, no separate set_addr burst.

    Parameters:
      * ``panel_hop``: hops from this cell to exit the panel port (@N). Default 1
        (the controller sits at the port cell; the WRITE/JUMP exit directly).
      * ``read_wr_desc`` / ``read_jp_desc``: raw 16-bit WRITE / JUMP descriptor
        words the panel re-emits on a read (where the read value lands). These
        are the push-read targets (see SRAM_PANEL.md §3).
      * ``primary_entry``: which entry is the block's DEFAULT (the one a chip
        input port / upstream net JUMPs into): ``"write"`` (default — the load-
        phase streaming convention, unchanged) or ``"lookup"`` (a lookup-driven
        embedding, e.g. the SRAM-backed Varicode encoder's char input).

    Interface:
      * Entry ``write`` (default). Input data in R{input} (default R25).
    """
    CATEGORY = "memory_interface"
    TAGS = ["sram", "controller", "memory_interface", "auto_increment"]
    # Authors its own panel-protocol WRITE/JUMP hops — keep them (no @1 default).
    RAW_OUTPUT_HOPS = True

    def __init__(self, name: str, panel_hop: int = 1,
                 read_wr_desc: int = 0, read_jp_desc: int = 0,
                 primary_entry: str = "write", addr_base: int = 0):
        if primary_entry not in ("write", "lookup"):
            raise ValueError(
                f"primary_entry must be 'write' or 'lookup', got {primary_entry!r}")
        super().__init__(name, panel_hop=panel_hop,
                         read_wr_desc=read_wr_desc, read_jp_desc=read_jp_desc,
                         primary_entry=primary_entry, addr_base=addr_base)
        self._hop = panel_hop
        self._rwd = read_wr_desc & 0xFFFF
        self._rjd = read_jp_desc & 0xFFFF
        self._primary = primary_entry
        # addr_base: a constant offset ADDED to every lookup key before the
        # read (rd_addr := key + addr_base) — how a SHARED panel keeps two
        # clients' tables in disjoint address regions (the duplex transceiver:
        # the encoder's 0..127 table offset clear of the decoder's 1..955
        # reverse map). 0 (the default) emits the exact historical program.
        self._addr_base = int(addr_base) & 0xFFFF

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
        # With addr_base the lookup gains an ADD, so the counter increments
        # compress to register-target ADDs (2 words shorter each) to keep the
        # register gap; base==0 keeps the historical program byte-identical.
        if self._addr_base:
            # ADD's result lands in R0 (accumulator ISA) — store it back.
            _wr_inc = ("    ADD R{state:wraddr}, R{data:one}\n"
                       "    MOVE R{state:wraddr}, R0\n")
            _rd_inc = ("    ADD R{state:rdaddr}, R{data:one}\n"
                       "    MOVE R{state:rdaddr}, R0\n")
        else:
            _wr_inc = ("    MOVE R0, R{state:wraddr}\n"
                       "    ADD R0, R{data:one}\n"
                       "    MOVE R{state:wraddr}, R0\n")
            _rd_inc = ("    MOVE R0, R{state:rdaddr}\n"
                       "    ADD R0, R{data:one}\n"
                       "    MOVE R{state:rdaddr}, R0\n")
        tmpl = (
            # --- write: addr->R5, data->R2, commit; wr_addr++ ---
            "write:\n"
            "    MOVE R0, R{state:wraddr}\n"
            f"    WRITE @{h}, 5\n"
            "    MOVE R0, R{in:data}\n"
            f"    WRITE @{h}, 2\n"
            + _wr_inc +
            f"    JUMP @{h}, 0\n"
            "    HALT\n"
            # --- lookup: rd_addr := incoming key (+ addr_base), FALL THROUGH
            # into read. The base ADD is emitted only when addr_base != 0, so
            # the default program (and every proven layout) is unchanged. ---
            "lookup:\n"
            "    MOVE R{state:rdaddr}, R{in:data}\n"
            + ("" if not self._addr_base else
               "    ADD R{state:rdaddr}, R{data:base}\n"
               "    MOVE R{state:rdaddr}, R0\n")
            +
            # --- read: descriptors -> R3/R4, rd_addr -> R5, trigger R1; rd++ ---
            "read:\n"
            "    MOVE R0, R{data:rwd}\n"
            f"    WRITE @{h}, 3\n"
            "    MOVE R0, R{data:rjd}\n"
            f"    WRITE @{h}, 4\n"
            "    MOVE R0, R{state:rdaddr}\n"
            f"    WRITE @{h}, 5\n"
            + _rd_inc +
            f"    JUMP @{h}, 1\n"
            "    HALT\n"
            # --- set_addr: load incoming value into both counters. OMITTED in
            # the addr_base variant (cell budget): a based controller serves a
            # PRELOADED shared-panel table — the streamed load phase (and its
            # sparse set_addr) is not part of that contract. ---
            + ("" if self._addr_base else
               "set_addr:\n"
               "    MOVE R{state:wraddr}, R{in:data}\n"
               "    MOVE R{state:rdaddr}, R{in:data}\n"
               "    HALT\n")
        )
        return {0: CellProgram(
            # Auto-allocate the data input register: the program is ~22
            # instructions, so R25 etc. would be CODE, not a free register.
            inputs=[Port("data")],
            outputs=[Port("out")],
            # The FIRST entry is the block's DEFAULT (resolved_io / port-injection
            # target); addresses come from the template labels, so reordering the
            # list changes only which entry a plain net JUMPs into.
            entries=[e for e in
                     (([EntryPoint("lookup"), EntryPoint("write"),
                        EntryPoint("read"), EntryPoint("set_addr")]
                       if self._primary == "lookup" else
                       [EntryPoint("write"), EntryPoint("read"),
                        EntryPoint("set_addr"), EntryPoint("lookup")]))
                     if not (self._addr_base and e.name == "set_addr")],
            # Pin data words to R1+ so the constant never lands on R0 (the
            # accumulator), which would make ADD R0, R{one} a no-op.
            data=([DataWord("one", 1, address=1),
                   DataWord("rwd", self._rwd, address=2),
                   DataWord("rjd", self._rjd, address=3)]
                  + ([DataWord("base", self._addr_base, address=4)]
                     if self._addr_base else [])),
            state=[StateVar("wraddr"), StateVar("rdaddr")],
            assembly_template=tmpl,
        )}

    def process_reference(self, input_samples: np.ndarray) -> np.ndarray:
        return np.asarray(input_samples, dtype=np.uint16)

    def reset(self):
        pass
