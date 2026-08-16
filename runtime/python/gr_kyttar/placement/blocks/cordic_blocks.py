# SPDX-License-Identifier: GPL-3.0-or-later
"""CORDIC vectoring blocks — ComplexToMagBlock / ComplexToArgBlock.

One shared engine: an UNROLLED, fully feed-forward CORDIC vectoring pipeline
(one cell per iteration — shift counts are immediate instruction fields
(INV-34), and a looped cell's loop-control + temporaries would not fit the
32-word unified cell memory regardless; the unrolled chain needs no loop
state at all and pipelines naturally). 14 iterations, Q15, prescale 1/4.

Numerics (validated in the design spike against float goldens BEFORE silicon):

* PRESCALE 1/4 (ones-complement arithmetic shift): the CORDIC gain K = 1.6468
  times |v| reaches 2.33 for corner inputs and would WRAP 16 bits; at 1/4 the
  whole trajectory stays signed-safe. The magnitude path compensates with
  MULQ by 1/K (0.6073) and a SATURATING <<2 restore (INV-13).
* Branchless iteration via the masked identities (msk = 0 - sgn, sgn = y>>15)::

      sigma*asr(y,i) = ((y ^ msk) >> i) + sgn        # goes into x
      y' = (y - ((x >> i) ^ msk)) - sgn              # x >= 0 always, SHR ok

* Angle in HALF-TURNS Q15 ([-1, 1) <-> [-pi, pi)): the ATAN table is
  atan(2^-i)/pi in Q15 and the z accumulator uses PURE 16-bit WRAP — which is
  EXACTLY arithmetic mod 2 half-turns, so the +-pi seam needs no special case.
* Pre-rotation into the right half-plane (x<0 -> rotate by -+90deg, z seeded
  +-0.5 half-turn); the magnitude flavor uses |x|,|y| instead (magnitude is
  quadrant-invariant) and needs no branches at all.

Accuracy (4000-point spike, vs float truth saturated to Q15): magnitude
max 19.5 LSB / mean 8.3 LSB; angle mean 2.3 half-turn-LSB, max 13.5 for
|v| >= 0.3 — below that the error is INPUT-quantization-limited (1 input LSB
subtends ~1/(|v|*pi) half-turn LSB), not algorithm-limited.

Both blocks are STATELESS and feed-forward: no per-packet state, no face
flips, no backward program-order edges (INV-33's feedback-pass hazard cannot
trigger).
"""
import numpy as np
from typing import Any, Dict, List, Tuple

from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from ._base import KyttarBlock, BlockInterface


NITER = 14
# atan(2^-i) in half-turn Q15 units.
ATAN_Q15 = [int(round(np.arctan(2.0 ** -i) / np.pi * 32768)) & 0xFFFF
            for i in range(NITER)]
KINV_Q15 = int(round(0.6072529350088812 * 32768))   # 1/K, Q15
ZSEED_POS = 16384                                   # +0.5 half-turn (+90deg)
ZSEED_NEG = 0xC000                                  # -0.5 half-turn


def _s16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _asr_oc(v: int, n: int) -> int:
    """Ones-complement arithmetic shift right — ((v^msk)>>n)^msk, exactly the
    PRE cells' instruction sequence."""
    v &= 0xFFFF
    msk = 0xFFFF if v & 0x8000 else 0
    return (((v ^ msk) >> n) ^ msk) & 0xFFFF


def _xy_iter(x: int, y: int, i: int) -> Tuple[int, int, int]:
    """One XY cell (cell-exact). Returns (x', y', sgn) — sgn is the PRE-update
    sign bit of y (the vectoring decision d_i)."""
    sgn = y >> 15
    msk = (0 - sgn) & 0xFFFF
    tx = x >> i                                    # SHR immediate; x >= 0
    x = (x + (((((y ^ msk) + sgn) & 0xFFFF)) >> i)) & 0xFFFF
    y = ((y - (tx ^ msk)) - sgn) & 0xFFFF
    return x, y, sgn


def cordic_mag_word(xi: int, xq: int) -> int:
    """Bit-exact Q15 model of the WHOLE magnitude chain (PRE1 -> PRE2m ->
    XY0..XY13 -> MAG) for ONE (i, q) word pair -> one magnitude word.

    Shared by :meth:`ComplexToMagBlock.process_reference` and the AGCCCBlock
    reference (whose loop embeds this exact chain on the chip)."""
    x, y = _asr_oc(int(xi), 2), _asr_oc(int(xq), 2)

    # PRE2m: |x|, |y| (two's-complement abs via mask+carry).
    def _abs(v):
        sgn = v >> 15
        msk = (0 - sgn) & 0xFFFF
        return ((v ^ msk) + sgn) & 0xFFFF

    x, y = _abs(x), _abs(y)
    for i in range(NITER):
        x, y, _ = _xy_iter(x, y, i)
    m = (_s16(x) * _s16(KINV_Q15)) >> 15
    m = min(m * 2, 32767)
    m = min(m * 2, 32767)
    return m & 0xFFFF


class _CordicBase(KyttarBlock):
    """Shared cell-program builders for the unrolled vectoring chain."""

    CATEGORY = "signal_conditioning"

    # Landing cell PRE1: xi@R1, xq@R2 (NOT R0 — every ALU op clobbers R0 and
    # PRE1 re-reads both inputs after its first shifts). Resolved dynamically
    # by resolved_io (INV-6); the static values are the true PRE1 layout.
    _interface = BlockInterface(
        entry_address=5, input_registers=[1, 2], output_registers=[0])

    GRC_UNSUPPORTED_PARAMS = ()

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    # ---------------------------------------------------------- cell builders
    @staticmethod
    def _pre1_program() -> CellProgram:
        """Prescale both components by 1/4 (ones-complement asr by 2)."""
        return CellProgram(
            inputs=[Port("xi", register=1), Port("xq", register=2)],
            outputs=[Port("x"), Port("y"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[],
            state=[StateVar("tmp", register=3), StateVar("msk", register=4)],
            assembly_template="""\
start:
    SHR R{in:xi}, #15
    MOVE R{state:tmp}, R0
    SUB R0, R{state:tmp}
    SUB R0, R{state:tmp}
    MOVE R{state:msk}, R0
    XOR R{in:xi}, R{state:msk}
    SHR R0, #2
    XOR R0, R{state:msk}
    {write:x}
    SHR R{in:xq}, #15
    MOVE R{state:tmp}, R0
    SUB R0, R{state:tmp}
    SUB R0, R{state:tmp}
    MOVE R{state:msk}, R0
    XOR R{in:xq}, R{state:msk}
    SHR R0, #2
    XOR R0, R{state:msk}
    {write:y}
    {jump:trig}
""",
        )

    @staticmethod
    def _xy_program(i: int, *, emit_y2z: bool, emit_xy: bool) -> CellProgram:
        """Iteration cell i. ``emit_y2z``: stream the pre-update y to Z_i (arg
        chain). ``emit_xy``: forward x,y to the next XY cell (all but the last
        iteration)."""
        outs: List[Port] = []
        head = ""
        if emit_y2z:
            outs.append(Port("y2z"))
            head = """\
    MOVE R0, R{in:y}
    {write:y2z}
"""
        tail = ""
        if emit_xy:
            outs += [Port("x"), Port("y")]
            tail = """\
    MOVE R0, R{in:x}
    {write:x}
    MOVE R0, R{in:y}
    {write:y}
"""
        elif not emit_y2z:
            # magnitude chain, last iteration: only x survives.
            outs.append(Port("x"))
            tail = """\
    MOVE R0, R{in:x}
    {write:x}
"""
        outs.append(Port("trig"))
        body = f"""\
    SHR R{{in:y}}, #15
    MOVE R{{state:sgn}}, R0
    SUB R0, R{{state:sgn}}
    SUB R0, R{{state:sgn}}
    MOVE R{{state:msk}}, R0
    SHR R{{in:x}}, #{i}
    MOVE R{{state:t}}, R0
    XOR R{{in:y}}, R{{state:msk}}
    ADD R0, R{{state:sgn}}
    SHR R0, #{i}
    ADD R{{in:x}}, R0
    MOVE R{{in:x}}, R0
    XOR R{{state:t}}, R{{state:msk}}
    SUB R{{in:y}}, R0
    SUB R0, R{{state:sgn}}
    MOVE R{{in:y}}, R0
"""
        return CellProgram(
            inputs=[Port("x", register=1), Port("y", register=2)],
            outputs=outs,
            entries=[EntryPoint("default")],
            data=[],
            state=[StateVar("sgn", register=3), StateVar("msk", register=4),
                   StateVar("t", register=5)],
            assembly_template="start:\n" + head + body + tail + "    {jump:trig}\n",
        )

    @staticmethod
    def _pre2m_program() -> CellProgram:
        """PRE2m: |x|, |y| — branchless abs (magnitude is quadrant-invariant).
        Shared by ComplexToMagBlock and AGCCCBlock (identical cell)."""
        return CellProgram(
            inputs=[Port("x", register=1), Port("y", register=2)],
            outputs=[Port("x"), Port("y"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[],
            state=[StateVar("tmp", register=3), StateVar("msk", register=4)],
            assembly_template="""\
start:
    SHR R{in:x}, #15
    MOVE R{state:tmp}, R0
    SUB R0, R{state:tmp}
    SUB R0, R{state:tmp}
    MOVE R{state:msk}, R0
    XOR R{in:x}, R{state:msk}
    ADD R0, R{state:tmp}
    {write:x}
    SHR R{in:y}, #15
    MOVE R{state:tmp}, R0
    SUB R0, R{state:tmp}
    SUB R0, R{state:tmp}
    MOVE R{state:msk}, R0
    XOR R{in:y}, R{state:msk}
    ADD R0, R{state:tmp}
    {write:y}
    {jump:trig}
""",
        )

    @staticmethod
    def _mag_program() -> CellProgram:
        """MAG: gain compensation (MULQ 1/K) + saturating <<2 prescale restore
        (INV-13). x >= 0 always, so overflow can only clamp HIGH (0x7FFF).
        Shared by ComplexToMagBlock (exit cell) and AGCCCBlock (mid-chain —
        the write/jump placeholders resolve per the owning block's wiring)."""
        return CellProgram(
            inputs=[Port("x", register=1)],
            outputs=[Port("mag"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("kinv", KINV_Q15, address=2),
                  DataWord("rail", 0x7FFF, address=3)],
            state=[],
            assembly_template="""\
start:
    MULQ R{data:kinv}, R{in:x}
    ADD R0, R0
    BR.NV ok1
    MOVE R0, R{data:rail}
ok1:
    ADD R0, R0
    BR.NV ok2
    MOVE R0, R{data:rail}
ok2:
    {write:mag}
    {jump:trig}
""",
        )


class ComplexToMagBlock(_CordicBase):
    """
    True complex magnitude |x + jy| — the GNU Radio counterpart is
    ``blocks.complex_to_mag`` (float; the chip emits Q15 where 1.0 = 32768,
    the standard kyttar.sink q15/32768 rescale).

    CORDIC vectoring, 17 cells in a 9x2 serpentine, all EAST/WEST corridors::

        row0:  PRE1(E) PRE2(E) XY0..XY6(E, XY6 faces S)
        row1:  MAG(W)  XY13..XY7(W)

    PRE2 takes |x|,|y| (magnitude is quadrant-invariant — branchless), the 14
    XY cells run the unrolled vectoring recurrence, MAG compensates the CORDIC
    gain (MULQ 1/K) and RESTORES the 1/4 prescale with a saturating <<2.

    |v| > 1 (only reachable at the corner of the Q15 square) SATURATES to
    0x7FFF by design. Accuracy vs saturated float truth: max 19.5 LSB.
    Stateless, feed-forward, no face flips.
    """

    TAGS = ["cordic", "magnitude", "complex", "envelope", "signal_conditioning"]

    def __init__(self, name: str):
        super().__init__(name)

    @property
    def cell_count(self) -> int:
        return NITER + 3          # PRE1, PRE2, XY0..13, MAG

    def output_cell_id(self):
        return "mag"

    # ------------------------------------------------------------ reference
    def process_reference(self, samples) -> np.ndarray:
        """Bit-exact Q15 model of the cell chain. ``samples``: iterable of
        (i, q) Q15 word pairs -> one magnitude word per sample."""
        return np.array([cordic_mag_word(int(xi), int(xq))
                         for (xi, xq) in samples], dtype=np.uint16)

    # ------------------------------------------------------- cell programs
    def build_cell_programs(self) -> Dict[str, CellProgram]:
        progs: Dict[str, CellProgram] = {"pre1": self._pre1_program()}

        # PRE2m: |x|, |y| — branchless abs, magnitude is quadrant-invariant
        # (shared builder — AGCCCBlock embeds the identical cell).
        progs["pre2"] = self._pre2m_program()

        for i in range(NITER):
            progs[f"xy{i}"] = self._xy_program(
                i, emit_y2z=False, emit_xy=(i < NITER - 1))

        # MAG: gain compensation (MULQ 1/K) + saturating <<2 prescale restore.
        # x >= 0 always, so overflow can only clamp HIGH (0x7FFF).
        progs["mag"] = self._mag_program()
        return progs

    # ------------------------------------------------------------- wiring
    def internal_connections(self) -> List[Tuple[Any, str, Any, str]]:
        conns: List[Tuple[Any, str, Any, str]] = [
            ("pre1", "x", "pre2", "x"), ("pre1", "y", "pre2", "y"),
            ("pre2", "x", "xy0", "x"), ("pre2", "y", "xy0", "y"),
        ]
        for i in range(NITER - 1):
            conns += [(f"xy{i}", "x", f"xy{i+1}", "x"),
                      (f"xy{i}", "y", f"xy{i+1}", "y")]
        conns.append((f"xy{NITER-1}", "x", "mag", "x"))
        return conns

    def internal_jumps(self) -> List[Tuple[Any, str, Any, str]]:
        jumps = [("pre1", "trig", "pre2", "default"),
                 ("pre2", "trig", "xy0", "default")]
        for i in range(NITER - 1):
            jumps.append((f"xy{i}", "trig", f"xy{i+1}", "default"))
        jumps += [(f"xy{NITER-1}", "trig", "mag", "default"),
                  ("mag", "trig", "__terminate__", "default")]
        return jumps

    def default_layout(self) -> Dict[Any, Tuple[int, int, str]]:
        """9x2 serpentine, I/O co-located on the WEST edge::

            col:   0        1        2    3    4    5    6    7    8
            row0:  PRE1(E)  PRE2(E)  XY0  XY1  XY2  XY3  XY4  XY5  XY6(S)
            row1:  (empty)  MAG(W)   XY13 XY12 XY11 XY10 XY9  XY8  XY7(W)

        Program-dict order == layout order (positional pairing, INV-33)."""
        lay: Dict[Any, Tuple[int, int, str]] = {
            "pre1": (0, 0, "east"), "pre2": (1, 0, "east")}
        for i in range(7):                       # XY0..XY6 eastbound
            lay[f"xy{i}"] = (2 + i, 0, "east" if i < 6 else "south")
        for i in range(7, NITER):                # XY7..XY13 westbound
            lay[f"xy{i}"] = (8 - (i - 7), 1, "west")
        lay["mag"] = (1, 1, "west")
        return lay


class ComplexToArgBlock(_CordicBase):
    """
    Complex argument atan2(y, x) — the GNU Radio counterpart is
    ``blocks.complex_to_arg`` (radians float). The chip emits the angle in
    HALF-TURN Q15 units: word/32768 * pi radians, [-1,1) <-> [-pi,pi) — the
    natural fixed-point angle representation (16-bit wrap IS mod 2pi, so the
    +-pi seam and all angle arithmetic wrap for free; NCOBlock shares this
    convention).

    CORDIC vectoring with pre-rotation, 30 cells in an 8x4 serpentine of
    interleaved iteration/angle cells::

        row0:  PRE1 PRE2 XY0 Z0  XY1 Z1  XY2 Z2(S)    (east)
        row1:  Z6(S) XY6 Z5 XY5  Z4 XY4  Z3 XY3       (west)
        row2:  XY7 Z7  XY8 Z8  XY9 Z9  XY10 Z10(S)    (east)
        row3:  ..   ..  Z13 XY13 Z12 XY12 Z11 XY11    (west, out at Z13)

    Each XY_i streams its PRE-update y (the vectoring decision source) to Z_i;
    Z_i adds +-atan(2^-i)/pi to the wrapping z accumulator. PRE2 pre-rotates
    x<0 inputs by -+90deg and forwards the quadrant flags to Z0, which seeds
    z with -+0.5 half-turn. Z13 is the output cell, landing at column 2 of the
    last serpentine row so its westward egress corridor is free.

    Angle error is input-quantization-limited below |v|~0.3 (1 input LSB
    subtends ~1/(|v| pi) half-turn LSB); atan2(0,0) returns +0.25 half-turn
    (the d-sequence of the all-zero trajectory), matching no contract — GR
    itself returns 0 there; callers gate on magnitude as usual.
    Stateless, feed-forward, no face flips.
    """

    TAGS = ["cordic", "atan2", "argument", "phase", "complex",
            "signal_conditioning"]

    def __init__(self, name: str):
        super().__init__(name)

    @property
    def cell_count(self) -> int:
        return 2 * NITER + 2      # PRE1, PRE2, (XY+Z) x 14

    def output_cell_id(self):
        return f"z{NITER-1}"

    # ------------------------------------------------------------ reference
    def process_reference(self, samples) -> np.ndarray:
        """Bit-exact Q15 model -> one half-turn angle word per sample."""
        out = []
        for (xi, xq) in samples:
            x, y = _asr_oc(int(xi), 2), _asr_oc(int(xq), 2)
            zf1, zf2 = x >> 15, y >> 15
            if zf1:
                if zf2 == 0:
                    x, y = y, (~x + 1) & 0xFFFF
                else:
                    x, y = (~y + 1) & 0xFFFF, x
            z = (ZSEED_POS if zf2 == 0 else ZSEED_NEG) if zf1 else 0
            for i in range(NITER):
                x, y, sgn = _xy_iter(x, y, i)
                msk = (0 - sgn) & 0xFFFF
                z = (z + ((ATAN_Q15[i] ^ msk) + sgn)) & 0xFFFF
            out.append(z)
        return np.array(out, dtype=np.uint16)

    # ------------------------------------------------------- cell programs
    def build_cell_programs(self) -> Dict[str, CellProgram]:
        progs: Dict[str, CellProgram] = {"pre1": self._pre1_program()}

        # PRE2a: pre-rotation into the right half-plane + quadrant flags to Z0.
        # WRITE preserves R0 and the flags, so the sign shifts drive the
        # branches directly; each path ends in GOTO fin (no unconditional
        # branch flag exists — GOTO is the local-JUMP idiom).
        progs["pre2"] = CellProgram(
            inputs=[Port("x", register=1), Port("y", register=2)],
            outputs=[Port("x"), Port("y"), Port("zf1"), Port("zf2"),
                     Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("one", 1, address=3)],
            state=[StateVar("ys", register=4)],
            assembly_template="""\
start:
    SHR R{in:y}, #15
    {write:zf2}
    MOVE R{state:ys}, R0
    SHR R{in:x}, #15
    {write:zf1}
    OR R0, R0
    BR.Z pos
    OR R{state:ys}, R{state:ys}
    BR.NZ yneg
    MOVE R0, R{in:y}
    {write:x}
    NOT R{in:x}
    ADD R0, R{data:one}
    {write:y}
    GOTO fin
yneg:
    NOT R{in:y}
    ADD R0, R{data:one}
    {write:x}
    MOVE R0, R{in:x}
    {write:y}
    GOTO fin
pos:
    MOVE R0, R{in:x}
    {write:x}
    MOVE R0, R{in:y}
    {write:y}
fin:
    {jump:trig}
""",
        )

        for i in range(NITER):
            progs[f"xy{i}"] = self._xy_program(
                i, emit_y2z=True, emit_xy=(i < NITER - 1))
            if i == 0:
                # Z0: seed z from the quadrant flags, then the i=0 update.
                progs["z0"] = CellProgram(
                    inputs=[Port("y2z", register=1), Port("zf1", register=2),
                            Port("zf2", register=3)],
                    outputs=[Port("z"), Port("trig")],
                    entries=[EntryPoint("default")],
                    data=[DataWord("at", ATAN_Q15[0], address=4),
                          DataWord("zp", ZSEED_POS, address=5),
                          DataWord("zn", ZSEED_NEG, address=6)],
                    state=[StateVar("zs", register=7), StateVar("t", register=8),
                          StateVar("m", register=9)],
                    assembly_template="""\
start:
    OR R{in:zf1}, R{in:zf1}
    BR.Z qz
    OR R{in:zf2}, R{in:zf2}
    BR.NZ qn
    MOVE R{state:zs}, R{data:zp}
    GOTO dd
qn:
    MOVE R{state:zs}, R{data:zn}
    GOTO dd
qz:
    SUB R0, R0
    MOVE R{state:zs}, R0
dd:
    SHR R{in:y2z}, #15
    MOVE R{state:t}, R0
    SUB R0, R{state:t}
    SUB R0, R{state:t}
    MOVE R{state:m}, R0
    XOR R{data:at}, R{state:m}
    ADD R0, R{state:t}
    ADD R0, R{state:zs}
    {write:z}
    {jump:trig}
""",
                )
            else:
                # Z_i: z += sigma_i * atan(2^-i)/pi, PURE WRAP (mod 2 half-turns).
                progs[f"z{i}"] = CellProgram(
                    inputs=[Port("y2z", register=1), Port("z", register=2)],
                    outputs=[Port("z"), Port("trig")],
                    entries=[EntryPoint("default")],
                    data=[DataWord("at", ATAN_Q15[i], address=3)],
                    state=[StateVar("t", register=4), StateVar("m", register=5)],
                    assembly_template="""\
start:
    SHR R{in:y2z}, #15
    MOVE R{state:t}, R0
    SUB R0, R{state:t}
    SUB R0, R{state:t}
    MOVE R{state:m}, R0
    XOR R{data:at}, R{state:m}
    ADD R0, R{state:t}
    ADD R0, R{in:z}
    {write:z}
    {jump:trig}
""",
                )
        # Program-dict order must equal layout order (positional pairing,
        # INV-33): rebuild interleaved — pre1, pre2, xy0, z0, xy1, z1, ...
        ordered: Dict[str, CellProgram] = {
            "pre1": progs["pre1"], "pre2": progs["pre2"]}
        for i in range(NITER):
            ordered[f"xy{i}"] = progs[f"xy{i}"]
            ordered[f"z{i}"] = progs[f"z{i}"]
        return ordered

    # ------------------------------------------------------------- wiring
    def internal_connections(self) -> List[Tuple[Any, str, Any, str]]:
        conns: List[Tuple[Any, str, Any, str]] = [
            ("pre1", "x", "pre2", "x"), ("pre1", "y", "pre2", "y"),
            ("pre2", "x", "xy0", "x"), ("pre2", "y", "xy0", "y"),
            ("pre2", "zf1", "z0", "zf1"), ("pre2", "zf2", "z0", "zf2"),
        ]
        for i in range(NITER):
            conns.append((f"xy{i}", "y2z", f"z{i}", "y2z"))
            if i < NITER - 1:
                conns += [(f"xy{i}", "x", f"xy{i+1}", "x"),
                          (f"xy{i}", "y", f"xy{i+1}", "y"),
                          (f"z{i}", "z", f"z{i+1}", "z")]
        return conns

    def internal_jumps(self) -> List[Tuple[Any, str, Any, str]]:
        jumps = [("pre1", "trig", "pre2", "default"),
                 ("pre2", "trig", "xy0", "default")]
        for i in range(NITER):
            jumps.append((f"xy{i}", "trig", f"z{i}", "default"))
            if i < NITER - 1:
                jumps.append((f"z{i}", "trig", f"xy{i+1}", "default"))
        jumps.append((f"z{NITER-1}", "trig", "__terminate__", "default"))
        return jumps

    def default_layout(self) -> Dict[Any, Tuple[int, int, str]]:
        """10x3 serpentine, flow-through (input WEST edge, output EAST edge).

        The corridor is the serpentine itself: row0 EAST (turn cell Z3 faces
        SOUTH), row1 WEST (turn cell Z8 faces SOUTH), row2 EAST. Every @N write
        transits only same-direction corridor cells that are idle at transit
        time (each cell triggers its successor only after all its writes)."""
        order: List[str] = ["pre1", "pre2"]
        for i in range(NITER):
            order += [f"xy{i}", f"z{i}"]
        lay: Dict[Any, Tuple[int, int, str]] = {}
        for pos, cid in enumerate(order):
            row, k = divmod(pos, 8)
            col = k if row % 2 == 0 else 7 - k
            face = "east" if row % 2 == 0 else "west"
            if k == 7 and pos != len(order) - 1:
                face = "south"                    # serpentine turn cell
            lay[cid] = (col, row, face)
        return lay
