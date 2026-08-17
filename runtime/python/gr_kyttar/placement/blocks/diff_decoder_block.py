# SPDX-License-Identifier: GPL-3.0-or-later
"""DiffDecoderBlock — see :class:`DiffDecoderBlock`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict
from ._base import KyttarBlock, BlockInterface


class DiffDecoderBlock(KyttarBlock):
    """
    Differential decoder — GNU Radio ``digital.diff_decoder_bb``.

    Computes ``y[n] = (x[n] - x[n-1]) mod M`` (with ``x[-1] = 0``), the inverse of
    the differential encoder ``diff_encoder_bb``. This is the classic DBPSK/DQPSK
    receive-side precoder inverse: the decoded symbol depends on the DIFFERENCE
    between the current and previous received symbols, so an unknown constant phase
    rotation of the whole stream cancels out.

    ``coding`` selects the differential coding convention (GR ``diff_coding_type``):

        * ``DIFF_DIFFERENTIAL`` (0, the default): ``y[n] = (x[n] - x[n-1]) mod M``.
        * ``DIFF_NRZI`` (1, ``modulus == 2`` ONLY): ``y[n] = (x[n] - x[n-1] + 1) mod 2``
          — the bit-complement of the DIFFERENTIAL result. GR RAISES if NRZI is used
          with any modulus other than 2, so this block does too (INV-0: mirror GR).

    Pinned against LIVE GNU Radio (not a datasheet — the sign/direction of the
    subtraction and the ``x[-1] = 0`` cold-start are verified against the actual
    installed ``digital.diff_decoder_bb`` output for modulus 2 AND 4, DIFFERENTIAL
    and NRZI). Bit-exact.

    Architecture: single cell (1 cell). One 1-sample state word holds the PREVIOUS
    INPUT symbol ``x[n-1]`` (NOT the previous output — a differential DECODER's state
    is the previous input; the previous OUTPUT is the encoder's state). Cold-start
    ``x[-1] = 0``. One input symbol in, one decoded symbol out per trigger. The single
    cell serializes state naturally, so it is saturation-safe with no feedback lock
    (like LFSRScrambler / any 1-sample-state cell — INV-19 does not apply: the state
    is a local data-write consumed by the SAME cell on the next trigger, never a
    cross-cell feedback corridor).

    Modulus is done with a mask ``& (M-1)`` because the manifest scope (and every
    real DBPSK/DQPSK use) has ``M`` a power of two (2 or 4). Two's-complement
    ``(x - prev) & (M-1)`` is the correct non-negative ``(x - prev) mod M`` for
    power-of-two ``M`` even when ``x - prev`` is negative (the low ``log2(M)`` bits
    are the modulus). A non-power-of-two modulus is a genuine ISA limitation here (a
    general ``mod`` would need a compare/subtract loop) and RAISES (INV-0: never
    silently compute a different function).

    Hardware deviations from digital.diff_decoder_bb:
        - ``modulus`` must be a POWER OF TWO (2, 4, 8, ...). The on-chip modulo is a
          bitmask ``& (modulus-1)``; a non-power-of-two modulus would need a runtime
          compare/subtract and is a real ISA cost. GR accepts any modulus; this block
          RAISES on a non-power-of-two value (INV-0: raise, never silently clamp).
          The manifest scope is modulus 2 and 4, both powers of two.

    Interface:
        - Entry: R1
        - Input: R0 (current symbol x[n], 0..M-1)
        - Output: decoded symbol y[n] (0..M-1), one per sample.
    """
    CATEGORY = "digital"
    TAGS = ["differential", "decoder", "dbpsk", "dqpsk", "precoder"]

    # GR diff_coding_type enum values (gnuradio.digital).
    DIFF_DIFFERENTIAL = 0
    DIFF_NRZI = 1

    _interface = BlockInterface(
        entry_address=1, input_registers=[0], output_registers=[0])

    def __init__(
        self,
        name: str,
        modulus: int = 2,
        coding: int = DIFF_DIFFERENTIAL,
    ):
        """Differential decoder.

        Args (mirror ``digital.diff_decoder_bb`` VERBATIM):
            modulus: modulus of the code's alphabet (Kyttar: power of two).
            coding: differential coding type (``DIFF_DIFFERENTIAL`` default,
                ``DIFF_NRZI`` for modulus-2 NRZI). GR names this positional
                argument ``coding``; older GR used the name ``nrzi`` for the same
                slot — both are the ``diff_coding_type`` enum.
        """
        modulus = int(modulus)
        coding = int(coding)
        if modulus < 2:
            raise ValueError(
                f"DiffDecoderBlock requires modulus >= 2; got {modulus}.")
        # HARDWARE DEVIATION (INV-0): on-chip modulo is a bitmask -> power-of-two only.
        if (modulus & (modulus - 1)) != 0:
            raise ValueError(
                "DiffDecoderBlock requires a POWER-OF-TWO modulus (2, 4, 8, ...) — "
                "the on-chip modulo is a bitmask & (modulus-1); "
                f"got modulus={modulus}.")
        if coding not in (self.DIFF_DIFFERENTIAL, self.DIFF_NRZI):
            raise ValueError(
                f"DiffDecoderBlock coding must be DIFF_DIFFERENTIAL (0) or "
                f"DIFF_NRZI (1); got {coding}.")
        # Mirror GR: NRZI only supported with modulus 2 (GR raises the same way).
        if coding == self.DIFF_NRZI and modulus != 2:
            raise ValueError(
                "DiffDecoderBlock: NRZI only supported with modulus 2 "
                f"(got modulus={modulus}) — mirrors digital.diff_decoder_bb.")
        super().__init__(name, modulus=modulus, coding=coding)
        self._modulus = modulus
        self._coding = coding
        # NRZI adds 1 before the modulus mask; DIFFERENTIAL adds 0.
        self._nrzi_add = 1 if coding == self.DIFF_NRZI else 0
        # runtime state (used by process_reference; reset() restores cold-start).
        self._prev = 0

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """Bit-exact single-cell differential decoder.

            y[n] = (x[n] - prev + nrzi_add) & (M-1)   ; prev = x[n-1], x[-1]=0
            prev = x[n]

        The input symbol arrives in R0. It MUST be saved to ``prev`` (the state for
        the NEXT trigger) BEFORE R0 is clobbered by the arithmetic — mirrors the
        LFSR/BPSK-slicer "read the input reg first" pattern. There is NO GOTO and NO
        branch in the datapath: one straight SUB -> (optional ADD) -> AND -> emit
        (INV-13: a GOTO/branch near the {write}/{jump} tail compiles to a stray output
        JUMP; a straight-line program avoids the whole class of hazard).

        ``mask = M-1`` gives the power-of-two modulo. For NRZI (``nrzi_add=1``) a
        ``+1`` is folded in before the mask via one extra ADD; DIFFERENTIAL omits it.
        """
        data = [
            DataWord("mask", (self._modulus - 1) & 0xFFFF, address=1),
        ]
        state = [
            # prev = x[n-1]; cold-start x[-1] = 0 (GR's initial previous symbol).
            StateVar("prev", initial_value=0),
            StateVar("cur"),
        ]

        # NRZI folds a +1 before the modulus mask; emit the ADD only when needed so
        # the DIFFERENTIAL (default) path is the minimal SUB/AND and stays in budget.
        nrzi_asm = ""
        if self._nrzi_add:
            data.append(DataWord("one", 1, address=2))
            nrzi_asm = "    ADD R0, R{data:one}\n"

        # cur = x[n]  (save the input before R0 is overwritten by SUB)
        # R0 = (cur - prev)          ; SUB writes R0
        # R0 = (R0 + nrzi_add)       ; NRZI only
        # R0 = R0 & (M-1)            ; power-of-two modulo, result in 0..M-1
        # prev = cur                 ; state for the next trigger
        # emit y[n]
        assembly = """\
start:
    MOVE R{state:cur}, R{in:sample}
    SUB R{state:cur}, R{state:prev}
%s\
    AND R0, R{data:mask}
    MOVE R{state:prev}, R{state:cur}
    {write:out}
    {jump:out}
""" % (nrzi_asm,)

        return {0: CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=data,
            state=state,
            assembly_template=assembly,
        )}

    def process_reference(self, input_symbols: np.ndarray) -> np.ndarray:
        """Bit-exact reference for ``digital.diff_decoder_bb``.

        ``y[n] = (x[n] - prev + nrzi_add) mod M``; ``prev = x[n]``; ``x[-1] = 0``.
        Stateful across calls until :meth:`reset`.
        """
        inp = np.asarray(input_symbols).astype(np.int64)
        out = np.zeros(len(inp), dtype=np.int32)
        prev = int(self._prev)
        M = self._modulus
        add = self._nrzi_add
        mask = M - 1
        for i in range(len(inp)):
            cur = int(inp[i])
            # (cur - prev + add) & (M-1) == (cur - prev + add) mod M for power-of-2 M.
            out[i] = (cur - prev + add) & mask
            prev = cur
        self._prev = prev
        return out

    def reset(self):
        """Reset the decoder's previous-symbol state to the cold-start (0)."""
        self._prev = 0
