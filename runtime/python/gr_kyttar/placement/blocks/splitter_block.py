"""SplitterBlock — see :class:`SplitterBlock`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, DataWord
from typing import Dict
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class SplitterBlock(KyttarBlock):
    """
    Full-duplex Splitter landing cell (1 cell) — tag-routed RX/TX fan-out.

    A single landing cell that lets ONE shared input port feed TWO independent
    signal chains. Incoming WRITE+DATA+JUMP bursts are steered by the JUMP's
    **entry-address tag**:

      * JUMP → ``rx_entry`` : set output FACE to the RX direction (default east)
        and relay the value into the RX chain.
      * JUMP → ``tx_entry`` : set output FACE to the TX direction (default south)
        and relay the value into the TX chain.

    Because the cell's output FACE is a CONFIG register (not per-instruction),
    each entry sets FACE explicitly with ``MOVE [FACE], <dir>`` before re-emitting
    the burst. This is the standard two-entry FACE-steer primitive (cf.
    :class:`CrossoverBlock`), specialized for an input-port splitter: the burst
    value lands in R0 (the input port's ``WRITE @N, 0``), so each arm relays R0.

    The two arms are independent and each terminates (JUMP then HALT) — there is
    NO fall-through between them (a remote JUMP does not stop local execution, so
    a shared/duplicated emit would double-send).

    No "join" block is needed on the output side: each chain's final WRITE keeps
    its own destination-address tag, so RX and TX outputs are distinguishable on
    the shared output port by ``dest``.

    Parameters:
      * ``rx_face`` / ``tx_face`` : output direction for each chain
        ('south'|'east'|'west'|'north'). Defaults: RX east, TX south.
      * ``rx_hop`` / ``tx_hop`` : hops to the chain's first cell (@N).
      * ``rx_dest`` / ``tx_dest`` : destination register at that cell (WRITE addr).
      * ``rx_chain_entry`` / ``tx_chain_entry`` : entry address to JUMP-trigger
        the chain's first cell.

    Interface:
      * Entry ``rx_entry`` (default), Entry ``tx_entry``.
      * The burst value lands in R0.
    """
    CATEGORY = "routing"
    TAGS = ["splitter", "duplex", "relay", "routing", "fanout"]
    # This block authors its own output WRITE/JUMP hops (the relay @N) — the
    # build must NOT default them to @1 abutment.
    RAW_OUTPUT_HOPS = True

    _FACE = {"south": 0, "east": 1, "west": 2, "north": 3}

    def __init__(self, name: str,
                 rx_face: str = "east", rx_hop: int = 1,
                 rx_dest: int = 0, rx_chain_entry: int = 1,
                 tx_face: str = "south", tx_hop: int = 1,
                 tx_dest: int = 0, tx_chain_entry: int = 1):
        super().__init__(name, rx_face=rx_face, rx_hop=rx_hop,
                         rx_dest=rx_dest, rx_chain_entry=rx_chain_entry,
                         tx_face=tx_face, tx_hop=tx_hop,
                         tx_dest=tx_dest, tx_chain_entry=tx_chain_entry)
        self._rx_face, self._rx_hop = rx_face, rx_hop
        self._rx_dest, self._rx_chain_entry = rx_dest, rx_chain_entry
        self._tx_face, self._tx_hop = tx_face, tx_hop
        self._tx_dest, self._tx_chain_entry = tx_dest, tx_chain_entry

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def interface(self) -> BlockInterface:
        # Two entry points (rx_entry default, tx_entry); the burst lands in R0.
        return BlockInterface(entry_address=1, input_registers=[0],
                              output_registers=[0])

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        rx_f = self._FACE.get(self._rx_face, 1)   # east
        tx_f = self._FACE.get(self._tx_face, 0)   # south
        # Each arm: set FACE, relay the burst value (R0) onward, JUMP, HALT.
        # The arms are independent and each terminates — no fall-through.
        tmpl = (
            "rx_entry:\n"
            "    MOVE [FACE], R{data:rx_face}\n"
            "    MOVE R0, R{in:burst}\n"
            f"    WRITE @{self._rx_hop}, {self._rx_dest}\n"
            f"    JUMP @{self._rx_hop}, {self._rx_chain_entry}\n"
            "    HALT\n"
            "tx_entry:\n"
            "    MOVE [FACE], R{data:tx_face}\n"
            "    MOVE R0, R{in:burst}\n"
            f"    WRITE @{self._tx_hop}, {self._tx_dest}\n"
            f"    JUMP @{self._tx_hop}, {self._tx_chain_entry}\n"
            "    HALT\n"
        )
        return {0: CellProgram(
            inputs=[Port("burst", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("rx_entry"), EntryPoint("tx_entry")],
            data=[DataWord("rx_face", rx_f, address=1),
                  DataWord("tx_face", tx_f, address=2)],
            state=[],
            assembly_template=tmpl,
        )}

    def process_reference(self, input_samples: np.ndarray) -> np.ndarray:
        # A splitter passes its input through unchanged (routing only).
        return np.asarray(input_samples, dtype=np.uint16)

    def reset(self):
        pass
