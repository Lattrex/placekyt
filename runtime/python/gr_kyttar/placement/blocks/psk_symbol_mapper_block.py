"""PSKSymbolMapperBlock — see :class:`PSKSymbolMapperBlock`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict, Tuple
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class PSKSymbolMapperBlock(KyttarBlock):
    """
    PSK Symbol Mapper Block (1 cell).

    Maps input bits to PSK constellation symbols for BPSK, QPSK, or 8-PSK
    modulation as used in MIL-STD-188-110B.

    Architecture: Single Cell (1 cell)
    ==================================

    Uses lookup tables for constellation points (I/Q values).
    Gray coding is used for optimal BER performance.

    Constellation mappings (normalized to unit circle):
    ```
    BPSK (1 bit/symbol):
        0 → +1.0 + j0.0  (0°)
        1 → -1.0 + j0.0  (180°)

    QPSK (2 bits/symbol):
        00 → +0.707 + j0.707   (45°)
        01 → -0.707 + j0.707   (135°)
        11 → -0.707 - j0.707   (225°)
        10 → +0.707 - j0.707   (315°)

    8-PSK (3 bits/symbol, Gray coded):
        000 → +1.0 + j0.0      (0°)
        001 → +0.707 + j0.707  (45°)
        011 → 0.0 + j1.0       (90°)
        010 → -0.707 + j0.707  (135°)
        110 → -1.0 + j0.0      (180°)
        111 → -0.707 - j0.707  (225°)
        101 → 0.0 - j1.0       (270°)
        100 → +0.707 - j0.707  (315°)
    ```

    Memory Layout (32 words):
    - R0: Accumulator
    - R1-R10: Program code
    - R11-R18: I lookup table (8 values for 8-PSK)
    - R19-R26: Q lookup table (8 values for 8-PSK)
    - R27: Modulation mode (0=BPSK, 1=QPSK, 2=8PSK)
    - R28: Bit accumulator (for multi-bit symbols)
    - R29: Bit count
    - R31: Input bits

    Total: 1 cell

    Interface:
        - Entry: R1
        - Input: R31 (bits, 1/2/3 per symbol depending on mode)
        - Output: I and Q values alternating
    """
    CATEGORY = "demodulation"
    TAGS = ["psk", "symbol_mapper", "modulation", "demodulation"]

    _interface = BlockInterface(entry_address=1, input_registers=[31], output_registers=[31])

    # Modulation modes
    MODE_BPSK = 0
    MODE_QPSK = 1
    MODE_8PSK = 2

    # Constellation values (Q15 format), GRAY-CODED order.
    # Tables are indexed by accumulated bit pattern (natural binary).
    # Gray code maps each natural index to the correct constellation angle.
    #
    # Gray code to angle mapping for 8-PSK:
    #   bits=000 (0) → 0°    bits=001 (1) → 45°   bits=010 (2) → 135°
    #   bits=011 (3) → 90°   bits=100 (4) → 315°  bits=101 (5) → 270°
    #   bits=110 (6) → 180°  bits=111 (7) → 225°
    _GRAY_TO_INDEX = [0, 1, 3, 2, 7, 6, 4, 5]  # natural index → angle index

    _8PSK_I_Q15 = [
        float_to_q15(1.0),       # bits=000: 0°
        float_to_q15(0.7071),    # bits=001: 45°
        float_to_q15(-0.7071),   # bits=010: 135° (Gray mapped)
        float_to_q15(0.0),       # bits=011: 90°  (Gray mapped)
        float_to_q15(0.7071),    # bits=100: 315° (Gray mapped)
        float_to_q15(0.0),       # bits=101: 270° (Gray mapped)
        float_to_q15(-1.0),      # bits=110: 180° (Gray mapped)
        float_to_q15(-0.7071),   # bits=111: 225° (Gray mapped)
    ]

    _8PSK_Q_Q15 = [
        float_to_q15(0.0),       # bits=000: 0°
        float_to_q15(0.7071),    # bits=001: 45°
        float_to_q15(0.7071),    # bits=010: 135° (Gray mapped)
        float_to_q15(1.0),       # bits=011: 90°  (Gray mapped)
        float_to_q15(-0.7071),   # bits=100: 315° (Gray mapped)
        float_to_q15(-1.0),      # bits=101: 270° (Gray mapped)
        float_to_q15(0.0),       # bits=110: 180° (Gray mapped)
        float_to_q15(-0.7071),   # bits=111: 225° (Gray mapped)
    ]

    # On-chip the symbol_table is a per-cell I + Q LOAD-indirect table (I at
    # addr 1..M, Q at addr M+1..2M, + a few scalars), so the table size M is
    # bounded by the 32-word cell. M<=14 fits comfortably (BPSK/QPSK/8PSK/16-QAM
    # all do); raise above (a larger constellation would need a multi-cell table).
    MAX_SYMBOL_TABLE = 14

    def __init__(
        self,
        name: str,
        modulation: str = "8psk",
        symbol_table=None,
        dimension: int = 1,
    ):
        """Initialize the symbol mapper — GR ``digital.chunks_to_symbols`` parity.

        Two ways to specify the constellation:

        * GR-native ``symbol_table`` (keyword): an arbitrary complex constellation
          (a list of complex points). The TRUE ``chunks_to_symbols`` mapping —
          ``output[n] = symbol_table[input_index[n]]`` (INDEX in, NOT bits; GR does
          not pack bits, that is an upstream block). ``dimension`` D mirrors GR's
          dimension (D entries per index); only D=1 (one complex symbol per index)
          is supported on chip (a documented limit — D>1 vector symbols would need
          a multi-word burst per index). When ``symbol_table`` is given, the block
          is index-driven and the ``modulation`` arg is ignored.
        * ``modulation`` preset (back-compat, a KYTTAR EXTENSION beyond
          chunks_to_symbols): "bpsk"|"qpsk"|"8psk" build the corresponding Gray
          constellation AND retain an internal BIT ACCUMULATOR (1/2/3 input bits ->
          one symbol). This bit-packing is NOT part of GR ``chunks_to_symbols``
          (which is index-in); it is a Kyttar convenience so bit-fed flowgraphs map
          directly. Documented LOUDLY as an extension.

        Args:
            name: block name.
            modulation: "bpsk"|"qpsk"|"8psk" preset (bit-packing extension).
            symbol_table: arbitrary complex constellation (GR-native, index-in).
            dimension: GR dimension D (entries per index); on chip D=1 only.
        """
        super().__init__(name, modulation=modulation, symbol_table=symbol_table,
                         dimension=dimension)
        self._dimension = int(dimension)

        # --- GR-native index-driven path (arbitrary symbol_table) -------------
        if symbol_table is not None:
            tbl = [complex(s) for s in symbol_table]
            if self._dimension != 1:
                raise ValueError(
                    "HARDWARE LIMIT: SymbolMapper supports dimension=1 only (one "
                    "complex symbol per index). A vector symbol (dimension>1) "
                    f"would need a multi-word burst per index; got {dimension}.")
            if not (1 <= len(tbl) <= self.MAX_SYMBOL_TABLE):
                raise ValueError(
                    f"HARDWARE LIMIT: symbol_table has {len(tbl)} entries; the "
                    f"per-cell I+Q LOAD-indirect table holds at most "
                    f"{self.MAX_SYMBOL_TABLE} (a larger constellation needs a "
                    f"multi-cell table).")
            self._symbol_table = tbl
            self._index_driven = True
            self._modulation = "table"
            self._mode = None
            # bits_per_symbol is informational for an index-driven mapper.
            self._bits_per_symbol = max(1, (len(tbl) - 1).bit_length())
            self._bit_buffer = 0
            self._bit_count = 0
            return

        # --- preset (bit-packing) path, back-compat ---------------------------
        self._symbol_table = None
        self._index_driven = False
        self._modulation = modulation.lower()
        if self._modulation == "bpsk":
            self._mode = self.MODE_BPSK
            self._bits_per_symbol = 1
        elif self._modulation == "qpsk":
            self._mode = self.MODE_QPSK
            self._bits_per_symbol = 2
        elif self._modulation == "8psk":
            self._mode = self.MODE_8PSK
            self._bits_per_symbol = 3
        else:
            raise ValueError(f"Unknown modulation: {modulation}")

        self._bit_buffer = 0
        self._bit_count = 0

    @property
    def cell_count(self) -> int:
        if self._index_driven:
            return 1   # index in -> I+Q LOAD-indirect table, one cell
        return 1 if self._modulation == "bpsk" else 2

    @property
    def interface(self) -> BlockInterface:
        if self._index_driven:
            # index in @R0, I+Q out from R0.
            return BlockInterface(entry_address=1, input_registers=[0],
                                  output_registers=[0])
        return self._interface

    @property
    def bits_per_symbol(self) -> int:
        return self._bits_per_symbol

    @property
    def modulation(self) -> str:
        return self._modulation

    @property
    def symbol_table(self):
        return list(self._symbol_table) if self._symbol_table else None

    @property
    def dimension(self) -> int:
        return self._dimension

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """V2 PSK symbol mapper: accumulate bits, LOAD indirect for I/Q constellation lookup.

        Outputs both I and Q components for all modulations.
        BPSK: I only (Q=0), single output — 1 cell.
        QPSK: I and Q tables in same cell, 2 LOADs — 1 cell.
        8-PSK: bit accumulator (cell 0) + I/Q lookup table (cell 1) — 2 cells.
        """
        if self._index_driven:
            return self._build_index_table()

        n_entries = 1 << self._bits_per_symbol  # 2/4/8
        mask_val = n_entries - 1

        if self._modulation == "8psk":
            return self._build_8psk()

        if self._modulation == "qpsk":
            # QPSK uses same 2-cell architecture as 8-PSK:
            # Cell 0 = bit accumulator, Cell 1 = I+Q table lookup.
            return self._build_qpsk()

        # BPSK: 0→+1, 1→-1. Q is always 0 — single output is sufficient.
        i_table_vals = [
            float_to_q15(1.0),       # bit=0: +1
            float_to_q15(-1.0),      # bit=1: -1
        ]

        i_table = [DataWord(f"i{i}", val, address=i + 1)
                   for i, val in enumerate(i_table_vals)]

        base = n_entries + 1
        return {0: CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=i_table + [
                DataWord("mask", mask_val, address=base),
                DataWord("bps", self._bits_per_symbol, address=base + 1),
                DataWord("one", 1, address=base + 2),
                DataWord("zero", 0, address=base + 3),
            ],
            state=[
                StateVar("in_save"),
                StateVar("bit_acc"),
                StateVar("bit_cnt"),
            ],
            assembly_template="""\
start:
    MOVE R{state:in_save}, R{in:sample}
    SHL R{state:bit_acc}, #1
    OR R0, R{state:in_save}
    MOVE R{state:bit_acc}, R0
    ADD R{state:bit_cnt}, R{data:one}
    MOVE R{state:bit_cnt}, R0
    CMP R{state:bit_cnt}, R{data:bps}
    BR.N done
    MOVE R{state:bit_cnt}, R{data:zero}
    AND R{state:bit_acc}, R{data:mask}
    ADD R0, R{data:one}
    MOVE R{state:in_save}, R0
    LOAD R{state:in_save}
    MOVE R{state:bit_acc}, R{data:zero}
    {write:out}
    {jump:out}
done:
    HALT
""",
        )}

    def _build_qpsk(self) -> Dict[int, CellProgram]:
        """V2 QPSK: 2 cells — cell 0 accumulates 2 bits, cell 1 does I+Q table lookup."""
        i_vals = [
            float_to_q15(0.7071),    # bits=00: 45°
            float_to_q15(-0.7071),   # bits=01: 135°
            float_to_q15(0.7071),    # bits=10: 315°
            float_to_q15(-0.7071),   # bits=11: 225°
        ]
        q_vals = [
            float_to_q15(0.7071),    # bits=00: 45°
            float_to_q15(0.7071),    # bits=01: 135°
            float_to_q15(-0.7071),   # bits=10: 315°
            float_to_q15(-0.7071),   # bits=11: 225°
        ]

        # Cell 0: bit accumulator (same structure as 8-PSK accumulator, just 2-bit)
        cell0 = CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("idx")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("mask", 3, address=1),
                DataWord("bps", 2, address=2),
                DataWord("one", 1, address=3),
                DataWord("zero", 0, address=4),
            ],
            state=[
                StateVar("in_save"),
                StateVar("bit_acc"),
                StateVar("bit_cnt"),
            ],
            assembly_template="""\
start:
    MOVE R{state:in_save}, R{in:sample}
    SHL R{state:bit_acc}, #1
    OR R0, R{state:in_save}
    MOVE R{state:bit_acc}, R0
    ADD R{state:bit_cnt}, R{data:one}
    MOVE R{state:bit_cnt}, R0
    CMP R{state:bit_cnt}, R{data:bps}
    BR.N done
    MOVE R{state:bit_cnt}, R{data:zero}
    AND R{state:bit_acc}, R{data:mask}
    MOVE R{state:bit_acc}, R{data:zero}
    {write:idx}
    {jump:idx}
done:
    HALT
""",
        )

        # Cell 1: I+Q table lookup — I at addr 1-4, Q at addr 5-8
        i_table = [DataWord(f"i{i}", val, address=i + 1)
                   for i, val in enumerate(i_vals)]
        q_table = [DataWord(f"q{i}", val, address=i + 5)
                   for i, val in enumerate(q_vals)]
        cell1 = CellProgram(
            inputs=[Port("index", register=0)],
            outputs=[Port("out_i"), Port("out_q"), Port("out_trigger")],
            entries=[EntryPoint("default")],
            data=i_table + q_table + [
                DataWord("one", 1, address=9),
                DataWord("q_offset", 4, address=10),
            ],
            state=[
                StateVar("addr_tmp"),
            ],
            assembly_template="""\
start:
    ADD R{in:index}, R{data:one}
    MOVE R{state:addr_tmp}, R0
    LOAD R{state:addr_tmp}
    {write:out_i}
    ADD R{state:addr_tmp}, R{data:q_offset}
    MOVE R{state:addr_tmp}, R0
    LOAD R{state:addr_tmp}
    {write:out_q}
    {jump:out_trigger}
""",
        )

        return {0: cell0, 1: cell1}

    def _build_8psk(self) -> Dict[int, CellProgram]:
        """V2 8-PSK: 2 cells — cell 0 accumulates 3 bits, cell 1 does I+Q table lookup.

        Cell 1 stores both I (addr 1-8) and Q (addr 9-16) tables. Does two LOADs
        to produce both I and Q values, outputs both.
        """
        # Cell 0: bit accumulator — sends index (0-7) to cell 1
        cell0 = CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("idx")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("mask", 7, address=1),
                DataWord("bps", 3, address=2),
                DataWord("one", 1, address=3),
                DataWord("zero", 0, address=4),
            ],
            state=[
                StateVar("in_save"),
                StateVar("bit_acc"),
                StateVar("bit_cnt"),
            ],
            assembly_template="""\
start:
    MOVE R{state:in_save}, R{in:sample}
    SHL R{state:bit_acc}, #1
    OR R0, R{state:in_save}
    MOVE R{state:bit_acc}, R0
    ADD R{state:bit_cnt}, R{data:one}
    MOVE R{state:bit_cnt}, R0
    CMP R{state:bit_cnt}, R{data:bps}
    BR.N done
    MOVE R{state:bit_cnt}, R{data:zero}
    AND R{state:bit_acc}, R{data:mask}
    MOVE R{state:bit_acc}, R{data:zero}
    {write:idx}
    {jump:idx}
done:
    HALT
""",
        )

        # Cell 1: I+Q lookup table — receives index in R0
        # I table at addr 1-8, Q table at addr 9-16
        i_table = [DataWord(f"i{i}", val, address=i + 1)
                   for i, val in enumerate(self._8PSK_I_Q15)]
        q_table = [DataWord(f"q{i}", val, address=i + 9)
                   for i, val in enumerate(self._8PSK_Q_Q15)]
        cell1 = CellProgram(
            inputs=[Port("index", register=0)],
            outputs=[Port("out_i"), Port("out_q"), Port("out_trigger")],
            entries=[EntryPoint("default")],
            data=i_table + q_table + [
                DataWord("one", 1, address=17),
                DataWord("q_offset", 8, address=18),
            ],
            state=[
                StateVar("addr_tmp"),
            ],
            assembly_template="""\
start:
    ADD R{in:index}, R{data:one}
    MOVE R{state:addr_tmp}, R0
    LOAD R{state:addr_tmp}
    {write:out_i}
    ADD R{state:addr_tmp}, R{data:q_offset}
    MOVE R{state:addr_tmp}, R0
    LOAD R{state:addr_tmp}
    {write:out_q}
    {jump:out_trigger}
""",
        )

        return {0: cell0, 1: cell1}

    def _build_index_table(self) -> Dict[int, CellProgram]:
        """GR-native chunks_to_symbols (dimension 1): index in -> symbol_table[idx].

        One cell: I table at addr 1..M, Q table at addr M+1..2M; the index selects
        the entry via LOAD-indirect; emit I then Q. (A remote JUMP does not halt
        the issuer, so both emits + the trigger ride one program.)"""
        M = len(self._symbol_table)
        i_table = [DataWord(f"i{k}", float_to_q15(self._symbol_table[k].real),
                            address=1 + k) for k in range(M)]
        q_table = [DataWord(f"q{k}", float_to_q15(self._symbol_table[k].imag),
                            address=1 + M + k) for k in range(M)]
        base = 1 + 2 * M
        return {0: CellProgram(
            inputs=[Port("index", register=0)],
            outputs=[Port("out_i"), Port("out_q"), Port("out_trigger")],
            entries=[EntryPoint("default")],
            data=i_table + q_table + [
                DataWord("one", 1, address=base),
                DataWord("q_offset", M, address=base + 1),
            ],
            state=[StateVar("addr_tmp")],
            assembly_template="""\
start:
    ADD R{in:index}, R{data:one}
    MOVE R{state:addr_tmp}, R0
    LOAD R{state:addr_tmp}
    {write:out_i}
    ADD R{state:addr_tmp}, R{data:q_offset}
    MOVE R{state:addr_tmp}, R0
    LOAD R{state:addr_tmp}
    {write:out_q}
    {jump:out_trigger}
""",
        )}

    def process_reference_index(self, indices) -> Tuple[np.ndarray, np.ndarray]:
        """Index-driven reference (GR chunks_to_symbols, dimension 1):
        out[n] = symbol_table[indices[n]], modelled at the on-chip Q15 precision."""
        i_out, q_out = [], []
        for ix in indices:
            s = self._symbol_table[int(ix)]
            i_out.append(q15_to_float(float_to_q15(s.real)))
            q_out.append(q15_to_float(float_to_q15(s.imag)))
        return np.asarray(i_out, dtype=np.float32), np.asarray(q_out, dtype=np.float32)

    def process_reference(self, input_bits: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """
        Reference implementation of PSK symbol mapping.

        Args:
            input_bits: Input bits (0 or 1)

        Returns:
            Tuple of (I_samples, Q_samples) as complex constellation points
        """
        n_bits = len(input_bits)
        n_symbols = n_bits // self._bits_per_symbol
        i_out = np.zeros(n_symbols, dtype=np.float32)
        q_out = np.zeros(n_symbols, dtype=np.float32)

        for sym_idx in range(n_symbols):
            # Gather bits for this symbol
            bit_start = sym_idx * self._bits_per_symbol
            symbol = 0
            for b in range(self._bits_per_symbol):
                symbol = (symbol << 1) | int(input_bits[bit_start + b])

            if self._mode == self.MODE_BPSK:
                # BPSK: 0 -> +1, 1 -> -1
                i_out[sym_idx] = 1.0 if symbol == 0 else -1.0
                q_out[sym_idx] = 0.0

            elif self._mode == self.MODE_QPSK:
                # QPSK Gray coded
                qpsk_i = [0.7071, -0.7071, 0.7071, -0.7071]  # 00, 01, 10, 11
                qpsk_q = [0.7071, 0.7071, -0.7071, -0.7071]
                i_out[sym_idx] = qpsk_i[symbol]
                q_out[sym_idx] = qpsk_q[symbol]

            elif self._mode == self.MODE_8PSK:
                # 8-PSK Gray coded
                # Map gray code to angle index
                angle_idx = self._GRAY_TO_INDEX[symbol]
                angle = angle_idx * np.pi / 4  # 0, 45, 90, ... degrees
                i_out[sym_idx] = np.cos(angle)
                q_out[sym_idx] = np.sin(angle)

        return i_out, q_out

    def reset(self):
        """Reset symbol mapper state."""
        self._bit_buffer = 0
        self._bit_count = 0
