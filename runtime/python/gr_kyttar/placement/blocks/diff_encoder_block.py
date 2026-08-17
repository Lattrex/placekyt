# SPDX-License-Identifier: GPL-3.0-or-later
"""DiffEncoderBlock — see :class:`DiffEncoderBlock`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict
from ._base import KyttarBlock, BlockInterface


class DiffEncoderBlock(KyttarBlock):
    """Differential encoder (1 cell, 1-sample carry state).

    Exact drop-in for GNU Radio ``digital.diff_encoder_bb(modulus, coding)``.

    Recurrence (verified against LIVE GNU Radio, not a datasheet)::

        DIFF_DIFFERENTIAL (default):  y[n] = (x[n] + y[n-1])     mod M
        DIFF_NRZI:                    y[n] = (x[n] + y[n-1] + 1)  mod M

    with the cold-start ``y[-1] = 0`` (GR's initial accumulator). This is the
    DBPSK/DQPSK precoder used by differential PSK (PSK31, DBPSK, DQPSK): the
    symbol carries the *change* between successive inputs so the receiver never
    needs an absolute phase reference. It is the exact inverse of
    ``digital.diff_decoder_bb`` — encode then decode is the identity.

    Parameters (names mirror GNU Radio VERBATIM — INV-0)
    ----------------------------------------------------
    modulus : int
        Modulus M of the code's alphabet. Inputs and outputs are in ``[0, M-1]``.
        GR default is 2 (binary → DBPSK precoder).
    coding : str
        Differential coding type. ``"DIFF_DIFFERENTIAL"`` (GR default, the plain
        ``y=(x+y_prev) mod M`` precoder) or ``"DIFF_NRZI"`` (adds a ``+1`` bias,
        i.e. ``y=(x+y_prev+1) mod M``). Mirrors GR's ``digital.diff_coding_type``.

    Architecture (single cell)
    --------------------------
    One cell holds the running ``y`` in a persistent state register (the 1-sample
    feedback carry). Per sample::

        R0 = y_prev + x + bias            ; bias = 0 (DIFFERENTIAL) or 1 (NRZI)
        if R0 >= M: R0 = R0 - M           ; single conditional subtract == mod M
                                          ;   (sum in [0, 2M-1] < 2M, so ONE
                                          ;    subtract fully reduces it)
        y = R0                            ; carry forward for the next sample
        emit R0

    The modulo is a signed conditional subtract (CMP + BR.GE-skip + SUB), which is
    GR-faithful for ANY modulus — not a power-of-2 AND mask — because the summands
    are each ``< M`` so the sum is always ``< 2M``.

    Interface:
        - Entry: R1
        - Input: R31 (the input symbol x[n], an int in [0, M-1])
        - Output: the encoded symbol y[n] (int in [0, M-1])
    """
    CATEGORY = "fec"
    TAGS = ["differential", "encoder", "dbpsk", "precoder", "fec"]

    _interface = BlockInterface(entry_address=1, input_registers=[31],
                                output_registers=[31])

    _CODING = ("DIFF_DIFFERENTIAL", "DIFF_NRZI")

    def __init__(
        self,
        name: str,
        modulus: int = 2,
        coding: str = "DIFF_DIFFERENTIAL",
    ):
        super().__init__(name, modulus=modulus, coding=coding)
        modulus = int(modulus)
        if modulus < 2:
            raise ValueError(
                f"DiffEncoderBlock modulus must be >= 2 (got {modulus})")
        # HARDWARE LIMIT: the alphabet symbols (and the modulus constant) must be
        # representable as a small non-negative Q15/int16 word and the running sum
        # (< 2M) must not touch the sign bit. M up to 0x4000 is safe; anything a
        # real differential PSK alphabet uses (2, 4, 8, ...) is far below it.
        if modulus > 0x4000:
            raise ValueError(
                f"DiffEncoderBlock: modulus {modulus} exceeds the HARDWARE LIMIT "
                f"(<= 0x4000); a differential PSK alphabet is 2/4/8/...")
        if coding not in self._CODING:
            raise ValueError(
                f"DiffEncoderBlock coding must be one of {self._CODING} "
                f"(got {coding!r})")
        # GR PARITY: diff_encoder_bb rejects NRZI for any modulus != 2 (verified
        # against LIVE GR: "NRZI only supported with modulus 2"). Mirror it exactly.
        if coding == "DIFF_NRZI" and modulus != 2:
            raise ValueError(
                "DiffEncoderBlock: NRZI only supported with modulus 2 "
                f"(got modulus {modulus}) — matches digital.diff_encoder_bb")
        self._modulus = modulus
        self._coding = coding
        self._bias = 1 if coding == "DIFF_NRZI" else 0
        self._y = 0  # cold-start carry: y[-1] = 0 (matches GR)

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """Single-cell differential encoder with a 1-sample carry state.

        Modulo M is a single conditional subtract: the sum ``y_prev + x + bias``
        is in ``[0, 2M-1]`` (each summand < M, bias <= 1), so if it is >= M one
        subtraction of M reduces it into ``[0, M-1]`` — exactly ``mod M``.

        GOTO-in-tail note (INV-13 / LFSR lesson): the ``BR.GE`` skip targets a
        REAL instruction (``store:`` = a ``MOVE``), NEVER a ``{write}``/``{jump}``
        placeholder — the build rewrites a branch that labels a placeholder into an
        output-routing JUMP, which would silently corrupt control flow. The write
        and jump stay at the tail, unbranched.
        """
        return {0: CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("modulus", self._modulus, address=1),
                DataWord("bias", self._bias, address=2),
            ],
            state=[StateVar("y", initial_value=self._y)],
            assembly_template="""\
start:
    ; R0 = y_prev + x
    ADD R{state:y}, R{in:sample}
    ; R0 = y_prev + x + bias   (bias = 0 DIFFERENTIAL / 1 NRZI)
    ADD R0, R{data:bias}
    ; reduce mod M: if R0 >= M subtract M (sum < 2M => one subtract suffices).
    ; CMP sets flags from R0 - M (R0 untouched); BR.LT (SLT = signed <) skips the
    ; subtract when R0 < M. Summands are small non-negative, so signed == unsigned.
    CMP R0, R{data:modulus}
    BR.LT store
    SUB R0, R{data:modulus}
store:
    ; carry y[n] forward for the next sample; R0 still holds y[n] for the emit
    MOVE R{state:y}, R0
    {write:out}
    {jump:out}
""",
        )}

    def process_reference(self, input_symbols: np.ndarray) -> np.ndarray:
        """Bit-exact reference: y[n] = (x[n] + y[n-1] + bias) mod M.

        Matches GNU Radio ``diff_encoder_bb`` with the cold-start y[-1]=0.
        """
        M = self._modulus
        b = self._bias
        out = np.zeros(len(input_symbols), dtype=np.int32)
        for i in range(len(input_symbols)):
            self._y = (int(input_symbols[i]) + self._y + b) % M
            out[i] = self._y
        return out

    def reset(self):
        """Reset the carry to the cold-start state (y[-1] = 0)."""
        self._y = 0
