# SPDX-License-Identifier: GPL-3.0-or-later
"""SqrtBlock — standalone Q15 square root; see :class:`SqrtBlock`.

Extracted from the RMS family's sqrt pipeline (``rms_block.py``), which fuses
power + IIR + sqrt into ONE block and therefore cannot accept an EXTERNAL power
word. This module re-uses the SAME normalize / quartic-polynomial / denormalize
cells and the SAME frozen coefficients, so the two agree bit-for-bit (a shared
gate asserts ``SqrtBlock.process_reference_q15`` == ``_RMSCoreBlock._sqrt_q15``
over ALL 32768 input words).
"""
import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock
from .rms_block import (_INV_SQRT2_Q15, _S_ZERO_SENTINEL, _SQRT_C0, _SQRT_C1,
                        _SQRT_C2, _SQRT_C3, _SQRT_C4, _RMSCoreBlock)


class SqrtBlock(KyttarBlock):
    """y = sqrt(x) for a Q15 word x in [0, 1) — e.g. a power / magnitude-squared
    word in, a magnitude word out.

    GNU Radio counterpart: ``blocks.transcendental("sqrt", "float")`` — the
    elementwise libm dispatcher, run with ``name="sqrt"``. That block's two
    parameters are BOTH structurally inexpressible here and are declared
    ``GRC_UNSUPPORTED_PARAMS`` (see the HW-DEVIATION section); this block takes
    NO parameters, which is why it can also be discovered from GRC as
    ``kyttar_sqrt``.

    DATAPATH (3 cells, feed-forward, lifted VERBATIM from
    :class:`~gr_kyttar.placement.blocks.rms_block._RMSCoreBlock`'s sqrt tail):

      ``norm``   — shift-count normalize: left-shift x until it lands in
                   [0x4000, 0x7FFF] (value [0.5, 1)) counting ``s``; forward
                   ``f = (m' - 0x4000) << 1`` (the fractional part, [0,1)) and
                   ``s``. x = 0 forwards f = 0 with the ``s = 30`` SENTINEL
                   (even parity, so floor(s/2) = 15 right-shifts in ``denorm``
                   turn ANY polynomial output (< 2^15) into exactly 0) — that
                   is why no downstream cell needs a zero branch, and why a
                   zero word does not shift forever.
      ``poly``   — quartic Horner ``c4 f^4 + c3 f^3 + c2 f^2 + c1 f + c0`` ~=
                   ``sqrt(0.5 + f/2)``. Every coefficient is sub-unity (Q15
                   representable, no INV-15 halving) and every intermediate is
                   in range; peak fit error 1.61e-5 = 0.53 Q15 LSB.
      ``denorm`` — multiply by 1/sqrt(2) when ``s`` is ODD, then shift right
                   floor(s/2) times with a SHR-#1 counter LOOP (shift counts are
                   IMMEDIATE instruction fields — INV-34 — so a data-dependent
                   count MUST loop, it cannot be a single SHR #s).

    Because ``sqrt(x/2^15) = sqrt(m'/2^15) * 2^(-s/2)``, the three stages compose
    to the Q15 square root. The composition is EXHAUSTIVELY bounded over ALL
    32768 input words — two statements of the SAME measurement against two
    different goldens, so quote whichever the context wants:
      * vs the ROUNDED ideal ``round(sqrt(x/2^15) * 2^15)`` — ``[-4, +1]`` LSB.
        This is what ``test_sqrt.py`` re-measures and asserts every run, and
        what this block's tolerance is derived from.
      * vs the UNROUNDED float ideal — ``[-4.5, +0.6]`` LSB, the interval
        RMSBlock's guard test pins. Same code, same errors; the half-LSB
        difference is only the rounding of the reference.

    EXIT-CELL TRAP (inherited, do not "simplify"): ``denorm``'s shift loop uses
    CONDITIONAL branches only. A ``GOTO`` assembles to a local hop-31 JUMP, and
    the build's output-handoff pass rewrites JUMPs in the block's EXIT cell into
    the external output trigger — a GOTO at the loop tail became a SECOND
    external JUMP and the loop ran exactly once (outputs one shift short, i.e.
    exactly 2x, for s >= 4).

    LAYOUT: a 3-cell L fold — ``norm``(0,0) -> ``poly``(0,1) -> ``denorm``(1,1).
    3 cells has NO even-full-column fold, and INV-14 explicitly forbids PADDING
    the last column to force I/O co-location (a relay in the egress path makes
    the source-exit WRITE hop land one cell short and the block emits NOTHING).
    So this takes the most compact fold and lets the router hook the output up —
    "get close, then let the router connect it"; co-location is a preference,
    not a hard requirement. Both footprint dims are 2 (<= 8, INV-9).
    Feed-forward only — no feedback corridor, no reconvergent fan-in — so no
    serialize-LOCK is needed (INV-19/20 N/A) and the block is freely orientable
    (all faces come from ``default_layout``).

    Hardware deviations from blocks.transcendental:
    -------------------------------------------------------------------------
    HW-DEVIATION (INV-0, Q15 ISA) — both of GR's params are omitted:
      1. ``name`` (the libm function) is FIXED to ``"sqrt"``. GR's
         ``blocks.transcendental`` is a generic dispatcher
         (``sin``/``cos``/``exp``/``log``/``tanh``/…); each of those is a
         COMPLETELY DIFFERENT on-chip datapath (a CORDIC, a different
         polynomial, a different range reduction) and several are not
         Q15-representable at all. One Kyttar block cannot be all of them, so
         the function is the block's IDENTITY rather than a parameter — the
         same convention the catalog already uses for ``AbsBlock`` /
         ``NLog10Block`` / the CORDIC blocks. It ALSO cannot be a class param
         at all: the catalog constructs every block as
         ``cls(name=<instance name>, **params)``, so a GR param literally
         called ``name`` would collide with the placeKYT instance name.
      2. ``type`` (GR's stream item type, "float"/"double") does not exist on
         this fabric: every stream word is Q15. There is nothing to select.
      3. Q15 DOMAIN: the input is a Q15 word in [0, 1). ``sqrt`` of a NEGATIVE
         value is not real (GR's libm returns NaN); on the Q15 datapath a
         negative word has bit 15 set and the normalize loop would never
         terminate, so a negative input is CLAMPED to 0 (output 0) — a
         documented, deliberate deviation, pinned by a bit-exact edge test.

    DERIVED TOLERANCE (NOT tuned): the exhaustive sweep bound of this exact
    datapath is ``err in [-4, +1]`` LSB, so the GR-equivalence gate uses 5 LSB —
    ``ceil`` of the MEASURED interval's larger magnitude. It is derived from the
    datapath and is never widened to make a test pass; ``test_sqrt.py``
    re-measures the bound on every run and FAILS if it moves, so a regression
    cannot be absorbed by the tolerance. (Measured against live GNU Radio the
    worst case is 3 LSB, corr 1.0000, NMSE -83 dB.)
    """
    # Same category as Nlog10Block, the catalog's other transcendental.
    CATEGORY = "math_operators"
    TAGS = ["sqrt", "transcendental", "magnitude", "math"]

    # Both of blocks.transcendental's params are documented HW-deviations (see
    # the class docstring): ``name`` is the block's identity (and would collide
    # with the placeKYT instance name), ``type`` has no meaning on a Q15 fabric.
    GRC_UNSUPPORTED_PARAMS = ("name", "type")

    _interface = BlockInterface(
        entry_address=1, input_registers=[0], output_registers=[0])

    def __init__(self, name: str):
        super().__init__(name)

    @property
    def cell_count(self) -> int:
        return 3

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    # ------------------------------------------------------------------ build
    def build_cell_programs(self) -> dict:
        cells = {}

        # (1) norm — clamp a negative word to zero, then left-shift x until it
        # reaches [0x4000, 0x7FFF] counting s. x <= 0 forwards f = 0 with the
        # s = 30 sentinel (even parity; denorm's 15 right-shifts make the result
        # exactly 0), so neither poly nor denorm needs a zero branch and the
        # loop can never spin on a zero/negative word.
        #
        # The negative clamp is the ONE line RMSBlock's norm does not need (its
        # upstream IIR average is non-negative by construction). Here x arrives
        # from ANY producer, so bit 15 must be handled. The ISA has no BR.GT, so
        # the test is the equivalent SIGNED ``CMP ys, 1; BR.LT`` — ys < 1 covers
        # BOTH zero and every negative word in one branch, straight to the
        # sentinel path. (SLT = N^V, so it is overflow-correct.)
        cells["norm"] = CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("f"), Port("s"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("zero", 0, address=1),
                DataWord("c4000", 0x4000, address=2),
                DataWord("one", 1, address=3),
                DataWord("szero", _S_ZERO_SENTINEL, address=4),
            ],
            state=[
                StateVar("ys", register=5),
                StateVar("s", register=6),
            ],
            assembly_template="""\
start:
    MOVE R{state:ys}, R{in:sample}
    CMP R{state:ys}, R{data:one}
    BR.GE _nz
    MOVE R0, R{data:zero}
    {write:f}
    MOVE R0, R{data:szero}
    {write:s}
    {jump:trig}
    HALT
_nz:
    MOVE R{state:s}, R{data:zero}
_lp:
    CMP R{state:ys}, R{data:c4000}
    BR.GE _dn
    SHL R{state:ys}, #1
    MOVE R{state:ys}, R0
    ADD R{state:s}, R{data:one}
    MOVE R{state:s}, R0
    GOTO _lp
_dn:
    SUB R{state:ys}, R{data:c4000}
    SHL R0, #1
    {write:f}
    MOVE R0, R{state:s}
    {write:s}
    {jump:trig}
""",
        )

        # (2) poly — quartic Horner on f (all coeffs sub-unity, all
        # intermediates in range); forwards (p, s). Identical to the RMS poly.
        cells["poly"] = CellProgram(
            inputs=[Port("f", register=0), Port("s", register=1)],
            outputs=[Port("p"), Port("s"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("c4", _SQRT_C4 & 0xFFFF, address=2),
                DataWord("c3", _SQRT_C3 & 0xFFFF, address=3),
                DataWord("c2", _SQRT_C2 & 0xFFFF, address=4),
                DataWord("c1", _SQRT_C1 & 0xFFFF, address=5),
                DataWord("c0", _SQRT_C0 & 0xFFFF, address=6),
            ],
            state=[
                StateVar("fr", register=7),
                StateVar("ss", register=8),
            ],
            assembly_template="""\
start:
    MOVE R{state:fr}, R{in:f}
    MOVE R{state:ss}, R{in:s}
    MOVE R0, R{data:c4}
    MULQ R0, R{state:fr}
    ADD R0, R{data:c3}
    MULQ R0, R{state:fr}
    ADD R0, R{data:c2}
    MULQ R0, R{state:fr}
    ADD R0, R{data:c1}
    MULQ R0, R{state:fr}
    ADD R0, R{data:c0}
    {write:p}
    MOVE R0, R{state:ss}
    {write:s}
    {jump:trig}
""",
        )

        # (3) denorm (EXIT cell) — 1/sqrt(2) on odd s, then floor(s/2) SHR-#1
        # iterations. NO GOTO here (see the class docstring's EXIT-CELL TRAP):
        # the loop is a do-while on SUB's Z flag with a pre-test for k == 0
        # (SHR sets Z). Identical to the RMS denorm.
        cells["denorm"] = CellProgram(
            inputs=[Port("p", register=0), Port("s", register=1)],
            outputs=[Port("out"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("one", 1, address=2),
                DataWord("invsqrt2", _INV_SQRT2_Q15, address=3),
            ],
            state=[
                StateVar("ps", register=4),
                StateVar("ks", register=5),
            ],
            assembly_template="""\
start:
    MOVE R{state:ps}, R{in:p}
    MOVE R{state:ks}, R{in:s}
    AND R{state:ks}, R{data:one}
    BR.Z _even
    MULQ R{state:ps}, R{data:invsqrt2}
    MOVE R{state:ps}, R0
_even:
    SHR R{state:ks}, #1
    MOVE R{state:ks}, R0
    BR.Z _dn
_lp:
    SHR R{state:ps}, #1
    MOVE R{state:ps}, R0
    SUB R{state:ks}, R{data:one}
    MOVE R{state:ks}, R0
    BR.NZ _lp
_dn:
    MOVE R0, R{state:ps}
    {write:out}
    {jump:trig}
""",
        )
        return cells

    # ------------------------------------------------------- multi-cell wiring
    def internal_connections(self):
        return [("norm", "f", "poly", "f"),
                ("norm", "s", "poly", "s"),
                ("poly", "p", "denorm", "p"),
                ("poly", "s", "denorm", "s")]

    def internal_jumps(self):
        return [("norm", "trig", "poly", "default"),
                ("poly", "trig", "denorm", "default")]

    def output_cell_ids(self):
        return ["denorm"]

    def default_layout(self):
        # Compact 3-cell L fold (INV-9: both dims 2 <= 8). INV-35: program cells
        # in build_cell_programs() order, the external-egress cell LAST.
        return {"norm": (0, 0, "south"),
                "poly": (0, 1, "east"),
                "denorm": (1, 1, "north")}

    # -------------------------------------------------------------- reference
    @staticmethod
    def sqrt_q15(x: int) -> int:
        """Bit-exact model of the norm -> poly -> denorm pipeline for ONE Q15
        input word (uint16). Negative words (bit 15 set) clamp to 0, matching
        the ``norm`` cell's guard."""
        w = int(x) & 0xFFFF
        if w == 0 or (w & 0x8000):
            return 0
        # The core is literally the RMS sqrt path, so the two can never drift.
        return _RMSCoreBlock._sqrt_q15(w)

    def process_reference_q15(self, x_q15) -> list:
        """Bit-exact predictor of the on-chip datapath, one word per input."""
        return [self.sqrt_q15(w) & 0xFFFF for w in x_q15]

    def process_reference(self, input_samples) -> np.ndarray:
        """Float reference matching GR ``blocks.transcendental('sqrt')``,
        clipped to the Q15 range (negatives -> 0, per the HW-DEVIATION)."""
        arr = np.asarray(input_samples, dtype=np.float64)
        out = np.sqrt(np.clip(arr, 0.0, None))
        return np.clip(out, 0.0, 32767.0 / 32768.0).astype(np.float32)

    def reset(self):
        pass
