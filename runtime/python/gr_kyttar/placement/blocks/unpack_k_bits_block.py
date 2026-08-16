# SPDX-License-Identifier: GPL-3.0-or-later
"""UnpackKBitsBlock — see the class docstring."""
import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock


class UnpackKBitsBlock(KyttarBlock):
    """Unpack one input byte into ``k`` MSB-first bits — GNU Radio
    ``blocks.unpack_k_bits_bb(k)``.

    Per input byte, GR ``unpack_k_bits_bb`` emits the LOW ``k`` bits of that byte,
    MOST-SIGNIFICANT bit FIRST, as ``k`` separate output bytes (each 0 or 1):

        out[0] = (byte >> (k-1)) & 1
        out[1] = (byte >> (k-2)) & 1
        ...
        out[k-1] = (byte >> 0)   & 1

    (Verified against live GR: ``unpack_k_bits_bb(2)`` of ``0xAA`` -> ``[1, 0]``
    — the low 2 bits ``0b10`` MSB-first; ``0x80`` -> ``[0, 0]`` — its low 2 bits
    are 0.) It is the exact INVERSE of ``pack_k_bits_bb``: pack ``k`` MSB-first
    bits into a byte, unpack that byte back into the same ``k`` bits.

    One input byte -> ``k`` outputs (rate-EXPANDING), so a single trigger emits a
    burst of ``k`` WRITE+JUMP pairs. The emit is a COUNTED LOOP (not unrolled):
    a fully-unrolled k=8 needs 8*(shift+mask+write+jump) = 32 instructions, which
    exceeds the cell's 31-usable-address budget (INV: one data word occupies addr
    0). The loop peels the MSB, emits it, shifts the working value left by one, and
    repeats ``k`` times — a constant instruction count independent of ``k``. Single
    cell, memoryless (working copy + counter are per-trigger scratch, re-seeded
    from the input each time).

    Params mirror GR verbatim: ``k`` (the number of bits to unpack; GRC ``k``).
    Pure bit manipulation (no Q15 arithmetic), so the comparison is BIT-EXACT.
    """
    CATEGORY = "signal_conditioning"
    TAGS = ["unpack", "bits", "byte", "signal_conditioning"]

    _interface = BlockInterface(
        entry_address=1, input_registers=[0], output_registers=[0])

    def __init__(self, name: str, k: int = 8):
        if int(k) < 1 or int(k) > 8:
            # GR unpack_k_bits_bb requires 1 <= k <= 8 (one byte in). We build for
            # the verified k=2..8 sweep; k=1 is a degenerate pass-through of bit 0.
            raise ValueError(f"k must be in 1..8, got {k}")
        super().__init__(name, k=int(k))
        self._k = int(k)

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def k(self) -> int:
        return self._k

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> dict:
        """Emit the low ``k`` bits of the input byte MSB-first via a counted loop.

        Seed a working copy ``w`` = ``byte & kmask`` (the low ``k`` bits) and a
        counter ``cnt`` = ``k``. Each iteration peels the MSB of the k-bit window:
        ``SHR R{state:w}, #(k-1)`` puts bit ``k-1`` (the current MSB, MSB-first) in
        R0's bit 0 (``AND R0, R{data:one}`` isolates it), which is emitted; then
        ``SHL R{state:w}, #1`` + ``AND R0, R{data:kmask}`` rotates the next bit into
        the MSB slot and drops the bit that just left (keeping only the low ``k``
        bits). ``cnt`` counts down; ``BR.NZ loop`` repeats until all ``k`` bits are
        out. The output-port handshake paces each emission (single-outstanding), so
        the burst is delivered in order; every iteration's ``{jump:out}`` closes and
        re-launches the entry. The backward branch (``BR.NZ loop``) is separated
        from the ``{jump:out}`` by the shift/mask/decrement so it never abuts the
        output launch."""
        kmask = (1 << self._k) - 1
        shift = self._k - 1  # peel the MSB of the k-bit window each iteration
        template = f"""\
start:
    AND R{{in:byte}}, R{{data:kmask}}
    MOVE R{{state:w}}, R0
    MOVE R{{state:cnt}}, R{{data:k}}
loop:
    SHR R{{state:w}}, #{shift}
    AND R0, R{{data:one}}
    {{write:out}}
    {{jump:out}}
    SHL R{{state:w}}, #1
    AND R0, R{{data:kmask}}
    MOVE R{{state:w}}, R0
    SUB R{{state:cnt}}, R{{data:one}}
    MOVE R{{state:cnt}}, R0
    CMP R{{state:cnt}}, R{{data:zero}}
    BR.NZ loop
    HALT
"""
        return {0: CellProgram(
            inputs=[Port("byte", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[DataWord("one", 1, address=1),
                  DataWord("zero", 0, address=2),
                  DataWord("k", self._k, address=3),
                  DataWord("kmask", kmask, address=4)],
            state=[StateVar("w"), StateVar("cnt")],
            assembly_template=template,
        )}

    # -------------------------------------------------------------- reference
    def process_reference_q15(self, x_q15) -> list:
        """The unpacked bit stream: low k bits of each input word, MSB-first."""
        out = []
        for w in x_q15:
            b = int(w) & 0xFFFF
            for pos in range(self._k - 1, -1, -1):
                out.append((b >> pos) & 1)
        return out

    def process_reference(self, input_samples) -> np.ndarray:
        """Float reference: low k bits of each input byte, MSB-first (0.0/1.0).

        Inputs are interpreted as unsigned byte values (0..255); GR
        unpack_k_bits_bb reads the low k bits regardless of the upper bits."""
        out = []
        for v in input_samples:
            b = int(round(float(v))) & 0xFFFF
            for pos in range(self._k - 1, -1, -1):
                out.append(float((b >> pos) & 1))
        return np.asarray(out, dtype=np.float32)
