# SPDX-License-Identifier: GPL-3.0-or-later
"""FSK4SymbolMapperBlock — see :class:`FSK4SymbolMapperBlock`."""
import numpy as np
from typing import Dict

from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from ._base import KyttarBlock, BlockInterface, float_to_q15, q15_to_float


class FSK4SymbolMapperBlock(KyttarBlock):
    """
    M17 4FSK / C4FM symbol mapper (1 cell).

    The transmit front end of an **M17 4-level FSK** modem (the 4-level FSK scheme
    M17 shares with DMR and Yaesu System Fusion): a real input **dibit** (two bits,
    LSB-first) maps to one of the four signed PAM deviation levels. The output PAM
    level, fed to a :class:`FrequencyModulatorBlock`, produces the M17 ±2400/±800 Hz
    deviations.

    M17 4FSK PARAMETERS (LOCKED — RULE #0)
    ======================================
    * Symbol rate 4800 sym/s, **2 bits/symbol** (dibit) → 9600 bps.
    * Four deviation levels (level × 800 Hz): ``+3 → +2400 Hz``, ``+1 → +800 Hz``,
      ``−1 → −800 Hz``, ``−3 → −2400 Hz``.
    * **Dibit → symbol Gray map, PINNED LSB-FIRST.** A dibit is two bits
      ``(b0, b1)`` where **b0 is the LSB (the first/earlier bit in the stream)** and
      **b1 is the MSB**. The index is ``d = b0 + 2·b1``. The Gray map (written as
      ``(b0,b1) → symbol``)::

          (1,0) → +3     (d=1)
          (0,0) → +1     (d=0)
          (0,1) → −1     (d=2)
          (1,1) → −3     (d=3)

      i.e. the level table indexed by ``d`` is ``[+1, +3, −1, −3]``.

    RULE #0 conformance note — LSB-first transpose
    ----------------------------------------------
    The M17 specification tabulates its dibit→symbol map **MSB-first** (the first
    stream bit is the dibit's high bit). This block is authored to the handoff
    prompt's **LSB-first** convention (b0 = first/earlier bit = LSB), so the spec's
    table was TRANSPOSED (not copied byte-for-byte) to this LSB-first order. The
    matching :class:`FSK4SlicerBlock` is the exact inverse in the same LSB-first
    convention, so a TX→RX loopback recovers the original bitstream unchanged.

    Q15 SCALING (the interface with the FM modulator)
    -------------------------------------------------
    The four PAM levels are stored NORMALISED so ``+3 → +1.0`` (Q15 0x7FFF) and
    ``±1 → ±1/3``: ``[+1/3, +1.0, −1/3, −1.0]`` indexed by ``d``. The downstream
    :class:`FrequencyModulatorBlock` is then given
    ``sensitivity = 2π·f_dev_max/fs`` with ``f_dev_max = 2400 Hz`` and the modem
    sample rate ``fs = sps·4800`` (``sps = 2`` → ``fs = 9600``): a full-scale
    ``+1.0`` (the ``+3`` level) advances the phase ``2π·2400/9600 = π/2`` rad/sample
    (2400 Hz) and the ``±1/3`` level advances ``±800 Hz`` — the M17 deviations,
    exactly. See ``examples/fsk4_modem``.

    Input format: ONE bit (0/1) per input word. The block accumulates two bits
    LSB-first (b0 then b1) into ``d`` and emits one PAM level per dibit (so the
    output rate is half the input bit rate). This bit-accumulator is the 4FSK analog
    of :class:`PSKSymbolMapperBlock`'s preset bit-packing.

    Memory layout (single cell)
    ---------------------------
    R0 accumulator; a 4-entry level table at addr 1..4 (LOAD-indirect); scalars for
    the accumulator; a 2-word state (bit index, saved bit). One cell — comfortably
    inside the ~31-register budget (INV-7).

    Interface:
        - Entry: R1
        - Input: R0 (one dibit bit, LSB-first)
        - Output: R0 (a signed Q15 PAM level, one per two input bits)
    """
    CATEGORY = "modulation"
    TAGS = ["fsk", "4fsk", "c4fm", "m17", "symbol_mapper", "modulation"]

    # d -> normalised PAM level. d = b0 + 2*b1 (LSB-first). Level table [+1,+3,-1,-3]
    # normalised by 3 so +3 -> +1.0 (full scale) and the FM sensitivity places the
    # M17 deviations. GRAY map (RULE #0): d=0->+1, d=1->+3, d=2->-1, d=3->-3.
    _LEVELS = [1.0 / 3.0, 1.0, -1.0 / 3.0, -1.0]

    _interface = BlockInterface(entry_address=1, input_registers=[0],
                                output_registers=[0])

    def __init__(self, name: str):
        super().__init__(name)
        # informational
        self._bits_per_symbol = 2

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    @property
    def bits_per_symbol(self) -> int:
        return self._bits_per_symbol

    @property
    def levels_q15(self):
        """The four Q15 PAM level words, indexed by dibit value ``d``."""
        return [float_to_q15(v) for v in self._LEVELS]

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """Accumulate two bits LSB-first into ``d = b0 + 2·b1``, LOAD the PAM level
        table entry, emit one signed Q15 level per dibit.

        The bit-index state ``bidx`` gates the two accumulation steps. On the FIRST
        bit (bidx==0) ``d`` is set to that bit (the LSB, b0) and nothing is emitted.
        On the SECOND bit (bidx==1) ``d += 2·b1`` (the MSB), the level table is
        LOAD-indexed at addr ``1+d`` and the level is emitted; ``d`` and the index
        reset. Single cell, single output face."""
        level_table = [DataWord(f"lvl{i}", val, address=i + 1)
                       for i, val in enumerate(self.levels_q15)]
        base = 1 + len(level_table)  # first scalar after the 4-entry table
        return {0: CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=level_table + [
                DataWord("one", 1, address=base),
                DataWord("zero", 0, address=base + 1),
            ],
            state=[
                StateVar("bidx"),      # 0 -> expecting b0 (LSB); 1 -> expecting b1
                StateVar("dacc"),      # accumulated dibit d = b0 + 2*b1
                StateVar("in_save"),   # snapshot of the input bit
                StateVar("addr_tmp"),  # table address scratch
            ],
            assembly_template="""\
start:
    MOVE R{state:in_save}, R{in:sample}
    CMP R{state:bidx}, R{data:one}
    BR.Z second
    ; --- first bit (b0 = LSB): d = b0 ---
    MOVE R{state:dacc}, R{state:in_save}
    MOVE R{state:bidx}, R{data:one}
    HALT
second:
    ; --- second bit (b1 = MSB): d += 2*b1 ---
    MOVE R0, R{state:in_save}
    SHL R0, #1
    ADD R0, R{state:dacc}
    MOVE R{state:dacc}, R0
    ; LOAD level table at addr 1 + d
    ADD R0, R{data:one}
    MOVE R{state:addr_tmp}, R0
    LOAD R{state:addr_tmp}
    {write:out}
    ; reset for next dibit
    MOVE R{state:bidx}, R{data:zero}
    MOVE R{state:dacc}, R{data:zero}
    {jump:out}
""",
        )}

    def process_reference(self, input_bits) -> np.ndarray:
        """Reference: bits in (0/1), one signed float PAM level per dibit (LSB-first
        ``d = b0 + 2·b1``), at the on-chip Q15 precision. Mirrors the cell: emits a
        symbol only on every SECOND bit; a trailing odd bit is buffered (dropped)."""
        bits = [int(b) & 1 for b in np.asarray(input_bits).reshape(-1)]
        out = []
        bidx = 0
        dacc = 0
        for b in bits:
            if bidx == 0:
                dacc = b
                bidx = 1
            else:
                dacc = dacc + 2 * b
                out.append(q15_to_float(float_to_q15(self._LEVELS[dacc])))
                bidx = 0
                dacc = 0
        return np.asarray(out, dtype=np.float32)

    def reset(self):
        """No state carried by the reference across calls."""
        pass
