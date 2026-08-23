# SPDX-License-Identifier: GPL-3.0-or-later
"""ZeroCrossingRateBlock — see the class docstring."""
import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock

# Powers of two the count-to-rate shift supports: shift = 15 - log2(window_size)
# must be a legal immediate shift count (0..15) -> window_size in [1, 32768].
# window_size=1 is excluded (a 1-sample "window" degenerates to a per-sample
# crossing flag, not a rate); see __init__.
_MIN_WINDOW = 2
_MAX_WINDOW = 32768


class ZeroCrossingRateBlock(KyttarBlock):
    """
    Windowed zero-crossing rate of a real Q15 stream — a standard signal-analysis
    feature (speech/music discrimination, noisiness/pitch estimation). There is NO
    stock GNU Radio streaming counterpart (a placeKYT-native block, like Crc16Block),
    so the golden reference is the pinned definition below.

    Definition (all conventions PINNED and gated by the test):

    * The sign of a Q15 word is its bit 15 — an EXACT ZERO is NON-NEGATIVE (the tie
      convention: 0 has sign bit 0, exactly like any positive value).
    * A zero crossing occurs between two CONSECUTIVE samples whose sign bits differ.
    * The stream is treated as preceded by ONE implicit zero sample (the previous-
      sample state initialises to 0, a non-negative value). So the very first pair
      of the stream is (0, x[0]) — it counts a crossing iff x[0] < 0.
    * For each NON-OVERLAPPING window of ``window_size`` samples the block counts
      the crossings over the ``window_size`` consecutive pairs ending at the
      window's samples — INCLUDING the inter-window boundary pair (the state
      carries the last sample of the previous window across the boundary) — and
      emits ONE Q15 word:

          out = count / window_size        (exact: count << (15 - log2 window_size))

      saturated to 0x7FFF (= 1 - 2^-15) when count == window_size, since a rate of
      exactly 1.0 is not Q15-representable (a fully alternating input reads
      ~0.99997, the Q15 rail).

    Rate-REDUCING: window_size samples in -> 1 word out (emits on input indices
    window_size-1, 2*window_size-1, ...). Single cell; the count-to-rate scaling is
    an exact shift, so the block is BIT-EXACT vs the integer reference (metric:
    exact, delay 0 on the emitted stream).

    Parameters:
        window_size: samples per window (default 64). MUST be a power of two in
            [2, 32768] — the exact-shift scaling (count << (15 - log2 N)) is only
            exact for a power of two, and the shift count must fit the immediate
            field. Any other value RAISES (never silently clamped).
    """
    CATEGORY = "measurement"
    TAGS = ["zero_crossing", "zcr", "rate", "measurement", "feature"]

    _interface = BlockInterface(
        entry_address=1, input_registers=[0], output_registers=[0])

    def __init__(self, name: str, window_size: int = 64):
        window_size = int(window_size)
        if window_size < _MIN_WINDOW or window_size > _MAX_WINDOW or (
                window_size & (window_size - 1)) != 0:
            raise ValueError(
                f"window_size must be a power of two in [{_MIN_WINDOW}, "
                f"{_MAX_WINDOW}] (the count->Q15 rate scaling is an exact shift "
                f"by 15 - log2(window_size)); got {window_size}")
        super().__init__(name, window_size=window_size)
        self._n = window_size
        self._shift = 15 - (window_size.bit_length() - 1)   # 15 - log2(N), 0..14

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def window_size(self) -> int:
        return self._n

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> dict:
        # Register map (INV-33: inputs low, data above, state PINNED above data,
        # instructions fill the top): input sample -> R0 (it is saved to xs first
        # thing), data words at 1..3, state pinned at 4..7, the 23-instruction
        # program at 8..30, R31 = the resolver's auto-HALT (32/32 words used).
        # The 23-instruction form is deliberate: the main emit path falls through
        # into _skip's HALT, and the _sat path ends on the auto-HALT at R31, so
        # no extra HALT words are spent. NOTE the resolver reserves R31, so a
        # cell's real budget is data + state + instructions <= 31 words (a
        # 24-instruction version pinned `counter` INTO the instruction region and
        # died silently — see the lessons log).
        return {0: CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[DataWord("one", 1, address=1),
                  DataWord("n", self._n & 0xFFFF, address=2),
                  DataWord("sat", 0x7FFF, address=3)],
            state=[StateVar("xs", register=4),
                   StateVar("prev", register=5, initial_value=0),
                   StateVar("count", register=6, initial_value=0),
                   StateVar("counter", register=7, initial_value=0)],
            assembly_template=f"""\
start:
    MOVE R{{state:xs}}, R{{in:sample}}     ; save the input (R0 is clobbered below)
    XOR R{{state:prev}}, R{{state:xs}}     ; R0 bit15 = sign(prev) ^ sign(x)
    SHR R0, #15                            ; R0 = crossing flag (0/1, logical fill)
    ADD R{{state:count}}, R0
    MOVE R{{state:count}}, R0              ; count += crossing
    MOVE R{{state:prev}}, R{{state:xs}}    ; prev = x (carries across windows)
    ADD R{{state:counter}}, R{{data:one}}
    MOVE R{{state:counter}}, R0            ; counter += 1
    CMP R{{state:counter}}, R{{data:n}}
    BR.NZ _skip                            ; window not complete -> no emit
    MOVE R{{state:xs}}, R{{state:count}}   ; xs (input already consumed) = count
    XOR R{{state:count}}, R{{state:count}} ; R0 = 0
    MOVE R{{state:count}}, R0              ; count = 0 for the next window
    MOVE R{{state:counter}}, R0            ; counter = 0
    CMP R{{state:xs}}, R{{data:n}}
    BR.Z _sat                              ; count == N -> rate 1.0 -> Q15 rail
    SHL R{{state:xs}}, #{self._shift}      ; R0 = count << (15 - log2 N), exact
    {{write:out}}
    {{jump:out}}
_skip:
    HALT                                   ; also ends the main emit path
_sat:
    MOVE R0, R{{data:sat}}                 ; 0x7FFF = 1 - 2^-15
    {{write:out}}
    {{jump:out}}
""",
        )}

    # -------------------------------------------------------------- reference
    def process_reference_q15(self, x_q15) -> list:
        """Bit-exact integer reference: one uint16 Q15 word per complete window."""
        n, shift = self._n, self._shift
        prev = 0
        count = 0
        out = []
        for i, w in enumerate(x_q15):
            w = int(w) & 0xFFFF
            count += ((prev ^ w) >> 15) & 1     # sign bits differ -> crossing
            prev = w
            if (i + 1) % n == 0:
                out.append(0x7FFF if count == n else (count << shift) & 0xFFFF)
                count = 0
        return out

    def process_reference(self, input_samples) -> np.ndarray:
        """Float reference: count/window_size per window (sign of the QUANTIZED
        sample; exact zero is non-negative; implicit zero predecessor), with the
        count == N window at the Q15 rail (32767/32768)."""
        x = np.asarray(input_samples, dtype=np.float64)
        q = np.clip(np.round(x * 32768.0), -32768, 32767).astype(np.int64)
        n = self._n
        prev = 0
        count = 0
        out = []
        for i, v in enumerate(q):
            if (v < 0) != (prev < 0):
                count += 1
            prev = int(v)
            if (i + 1) % n == 0:
                out.append(32767.0 / 32768.0 if count == n else count / n)
                count = 0
        return np.asarray(out, dtype=np.float32)
