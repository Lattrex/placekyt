# SPDX-License-Identifier: GPL-3.0-or-later
"""Crc16Block — see :class:`Crc16Block`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict
from ._base import KyttarBlock, BlockInterface


class Crc16Block(KyttarBlock):
    """
    Frame CRC-16 — a placeKYT-native ([Kyttar]) data-link block, NO stock GNU
    Radio counterpart.

    Computes a 16-bit cyclic redundancy check over fixed-length frames of a BYTE
    stream. Consumes ``frame_len`` input bytes and emits ONE 16-bit CRC word per
    frame (rate-REDUCING ``frame_len``:1, the PackKBits pattern); the CRC
    register is then re-initialised to ``init`` for the next frame. A trailing
    partial frame (< ``frame_len`` bytes) is never emitted.

    The algorithm is the published bit-serial MSB-first (non-reflected) CRC — the
    ITU-T V.41 / CRC-16/CCITT-FALSE family, exactly as catalogued in Greg Cook's
    CRC RevEng catalogue ("Catalogue of parametrised CRC algorithms"):

        crc = init
        for each byte:                       # MSB-first byte feed
            crc ^= (byte & 0xFF) << 8
            repeat 8 times:
                crc = ((crc << 1) ^ poly) if crc & 0x8000 else (crc << 1)
        (16-bit register; refin=false, refout=false, xorout=0x0000)

    With the defaults ``poly=0x1021, init=0xFFFF`` this is exactly
    **CRC-16/CCITT-FALSE** (width=16 poly=0x1021 init=0xFFFF refin=false
    refout=false xorout=0x0000 check=0x29B1: ``b"123456789" -> 0x29B1``), which
    Python's stdlib ``binascii.crc_hqx(data, 0xFFFF)`` reproduces bit-for-bit.
    Other catalogued non-reflected models are reachable through the params, e.g.
    CRC-16/XMODEM (``init=0``), CRC-16/AUG-CCITT (``init=0x1D0F``),
    CRC-16/UMTS (``poly=0x8005, init=0``), CRC-16/CMS (``poly=0x8005,
    init=0xFFFF``). Reflected models (ARC, MODBUS, KERMIT, ...) are NOT this
    block — it is strictly the MSB-first engine.

    Why there is no GR counterpart: GNU Radio's CRC blocks (``digital.crc16``,
    ``digital.crc_append``/``crc_check``) are tagged-PDU / packet blocks — a
    host-scheduler idiom with no per-sample streaming form; the fabric streams
    words. The golden reference is therefore the published algorithm above
    (cross-checked against ``binascii.crc_hqx`` on the same parameterization),
    not a GR flowgraph.

    Datapath: single cell, bit-serial LFSR-style shift/XOR (the LFSRScrambler
    idiom). Per input byte the cell XORs ``byte << 8`` into the CRC register and
    runs 8 shift steps; the shifted-out bit-15 lands in the CARRY flag (``SHL``
    sets C = last bit shifted out, guide §4.3), which selects the polynomial XOR
    without any mask word. A frame down-counter emits the CRC word and reloads
    ``crc=init`` every ``frame_len`` bytes.

    Byte/word streams are RAW 16-bit words, not Q15: the input uses the low 8
    bits of each word (high bits are ignored, as ``crc ^= byte << 8`` drops
    them), and the output word IS the 16-bit CRC value.

    Usage note (receive-side check): run TWO Crc16Blocks — one over the received
    payload (recompute) and the TX-appended CRC word beside it — and compare
    with XorBlock; a zero result means the frame checks.

    Interface:
        - Entry: R1
        - Input: R0 (one byte per trigger, low 8 bits used)
        - Output: the 16-bit CRC word, emitted once per ``frame_len`` inputs.
    """
    CATEGORY = "fec"
    TAGS = ["crc", "checksum", "frame", "data-link", "fec", "ccitt"]

    _interface = BlockInterface(
        entry_address=1, input_registers=[0], output_registers=[0])

    def __init__(
        self,
        name: str,
        poly: int = 0x1021,
        init: int = 0xFFFF,
        frame_len: int = 8,
    ):
        """Frame CRC-16 (MSB-first, non-reflected).

        Args:
            poly: 16-bit generator polynomial (default 0x1021, the CCITT/V.41
                polynomial x^16 + x^12 + x^5 + 1).
            init: initial CRC register value per frame (default 0xFFFF —
                CRC-16/CCITT-FALSE).
            frame_len: bytes per frame; one CRC word is emitted per
                ``frame_len`` input bytes (default 8).
        """
        if not (0 <= int(poly) <= 0xFFFF):
            raise ValueError(
                f"poly must be a 16-bit value (0..0xFFFF); got {poly:#x}.")
        if not (0 <= int(init) <= 0xFFFF):
            raise ValueError(
                f"init must be a 16-bit value (0..0xFFFF); got {init:#x}.")
        # frame_len drives a 16-bit down-counter register; 1..0xFFFF fits it.
        if not (1 <= int(frame_len) <= 0xFFFF):
            raise ValueError(
                f"frame_len must be 1..65535 (16-bit frame counter); got {frame_len}.")
        super().__init__(name, poly=int(poly), init=int(init),
                         frame_len=int(frame_len))
        self._poly = int(poly) & 0xFFFF
        self._init = int(init) & 0xFFFF
        self._frame_len = int(frame_len)

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def frame_len(self) -> int:
        return self._frame_len

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """Bit-serial MSB-first CRC-16, one byte per trigger:

            crc ^= (byte << 8)                  ; SHL #8 also masks to 8 bits
            repeat 8:
                C = crc bit15; crc <<= 1        ; SHL sets C = shifted-out bit
                if C: crc ^= poly
            n -= 1
            if n == 0:  emit crc;  crc = init;  n = frame_len

        The CARRY flag carries the pre-shift bit 15 (guide §4.3: C = the last
        bit shifted out) across the flag-preserving ``MOVE`` store, so the
        polynomial-select needs no 0x8000 mask word and no GOTO merge (the
        LFSRScrambler GOTO-in-tail trap is avoided by construction: the only
        branches are conditional forward skips and the loop back-edge, kept
        clear of the ``{write}/{jump}`` placeholders per the PackKBits shape).
        State registers are pinned explicitly (INV-33).
        """
        return {0: CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("eight", 8, address=1),
                DataWord("one", 1, address=2),
                DataWord("poly", self._poly, address=3),
                DataWord("init", self._init, address=4),
                DataWord("flen", self._frame_len, address=5),
            ],
            state=[
                StateVar("crc", register=6, initial_value=self._init),
                StateVar("i", register=7),
                StateVar("n", register=8, initial_value=self._frame_len),
            ],
            assembly_template="""\
start:
    ; crc ^= byte << 8   (MSB-first byte feed; the shift drops any high bits,
    ; so the input word is masked to its low 8 bits by construction)
    SHL R{in:sample}, #8
    XOR R0, R{state:crc}
    MOVE R{state:crc}, R0
    ; 8 bit-serial polynomial steps
    MOVE R{state:i}, R{data:eight}
loop:
    SHL R{state:crc}, #1
    MOVE R{state:crc}, R0
    BR.NC step
    XOR R{state:crc}, R{data:poly}
    MOVE R{state:crc}, R0
step:
    SUB R{state:i}, R{data:one}
    MOVE R{state:i}, R0
    BR.NZ loop
    ; frame boundary: n -= 1; on zero emit the CRC word and re-arm
    SUB R{state:n}, R{data:one}
    MOVE R{state:n}, R0
    BR.NZ done
    MOVE R0, R{state:crc}
    {write:out}
    {jump:out}
    MOVE R{state:crc}, R{data:init}
    MOVE R{state:n}, R{data:flen}
done:
""",
        )}

    def process_reference(self, input_bytes: np.ndarray) -> np.ndarray:
        """Bit-exact reference: the published MSB-first CRC-16 (CRC RevEng
        catalogue, non-reflected family; CRC-16/CCITT-FALSE at the defaults —
        equals ``binascii.crc_hqx(frame, init)`` for ``poly=0x1021``).

        Consumes ``frame_len`` bytes per frame (low 8 bits of each input word),
        emits one CRC word per completed frame; a trailing partial frame is
        dropped. Each call is a fresh stream (crc starts at ``init``).
        """
        inp = np.asarray(input_bytes).astype(np.int64)
        fl = self._frame_len
        n_out = len(inp) // fl
        out = np.zeros(n_out, dtype=np.uint16)
        for j in range(n_out):
            crc = self._init
            for b in inp[j * fl:(j + 1) * fl]:
                crc ^= (int(b) & 0xFF) << 8
                for _ in range(8):
                    if crc & 0x8000:
                        crc = ((crc << 1) ^ self._poly) & 0xFFFF
                    else:
                        crc = (crc << 1) & 0xFFFF
            out[j] = crc
        return out.view(np.int16) if n_out else np.zeros(0, dtype=np.int16)

    def reset(self):
        """No cross-call state (each stream starts a fresh frame at ``init``)."""
        pass
