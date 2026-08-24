# SPDX-License-Identifier: GPL-3.0-or-later
"""Q15 activation functions — SigmoidBlock / TanhBlock (one shared core).

One shared two-cell engine (the shared-builder model of ``cordic_blocks.py``:
a composite block can import these cell programs verbatim):

* cell ``fold``  — sign fold + |x| clamp + index/fraction extraction, with the
  per-instance ``dshift`` parameter folded into the two shift immediates;
* cell ``lut``   — a 17-entry Q15 table + linear interpolation + sign unfold.

NUMERIC DESIGN (validated by exhaustive bit-exact integer simulation over all
65536 input words, per ``dshift``, before implementation):

* Form: 16-interval table + linear interpolation, 17 Q15 entries
  ``table[i] = round(32768 * f(R * i / 16))``, on FIXED canonical domains
  ``[-R, R]``: sigmoid ``R = 8`` (k = 3), tanh ``R = 4`` (k = 2).
* Input convention: the input word ``v`` represents the pre-activation
  ``a = (v / 32768) * 2**(k + dshift)`` — i.e. ``dshift`` is a configurable
  binary point.  ``dshift = 0`` is the canonical domain (full-scale input maps
  to +-8 for sigmoid, +-4 for tanh).  An upstream MAC row prescaled by
  ``2**-S`` for headroom (the INV-13 idiom) is compensated for free with
  ``dshift = S - k`` — zero extra instructions, because ``dshift`` lands in
  the two existing shift immediates of the index/fraction extraction::

      idx  = mag >> (11 - dshift)          ; 11 = 15 - log2(16 intervals)
      frac = (mag & mask) << (4 + dshift)  ; realized mask-free, see below
      idx == 16 -> table[16]               ; index clamp == asymptote clamp

* NEGATIVE RESULT (measured, keep): folding the scale into the TABLE DOMAIN
  instead (building the table on ``[-2**S, 2**S]``) passes for sigmoid but
  FAILS for tanh (max error 0.082 at S = 4) — tanh's curvature lives entirely
  in [0, 2] and a 16-interval grid over [-16, 16] starves it.  Always fold
  into the shift immediates against the canonical domain.
* Sign folding: tanh is odd (negate in and out); sigmoid uses
  ``0x8000 - y``, which is WRAP-EXACT in two's complement (y >= 16384, so the
  result needs no saturation path).  Both unfolds are the SAME instruction —
  ``SUB negop, R0`` with ``negop`` = 0x8000 (sigmoid) or 0 (tanh).
* Accuracy vs float (exhaustive, dshift = 0, identical at dshift = +1/+2/-1):
  sigmoid max |err| 0.0030 / RMS 0.0010; tanh 0.0060 / 0.0021.

THE TWO-CELL SPLIT (32-word cells; single-cell does not fit any passing form):

``fold`` (input cell, 28-30 words) computes ``sgn``, ``mag = min(|v|, 32767)``
(realized for dshift > 0 as the equivalent ``min(|v|, 2**(15-dshift))`` — an
unsigned-compare domain cap that also absorbs |−32768| and makes the index
clamp unreachable-by-construction), then ``frac`` (mask-free shift pair),
``idx`` and the two table ADDRESSES, and the sign as a PRE-ASSEMBLED PATCH
INSTRUCTION word.  It sends 4 operands to ``lut``: the patch word, ``frac``,
``addrq`` (= addr+1) and — as the LAST write, the INV-33 accumulator-delivery
idiom — ``addr`` straight into the lut's R0.

``lut`` (output cell, 31/31 words — every address purposeful) is::

    LOAD R0            ; P = table[idx]      (addr was delivered into R0)
    MOVE p, R0
    LOAD addrq         ; Q = table[idx+1]
    SUB R0, p          ; Q - P
    MULQ R0, frac      ; (Q-P)*frac >> 15
    ADD R0, p          ; y
    <patch slot>       ; pos: MOVE p, R0 (harmless)   neg: SUB negop, R0
    {write:out}
    {jump:trig}

The 7th instruction is a RUNTIME PATCH SLOT (the established idiom of
``BlockInterleaverBlock``'s ``store`` cell: an input Port pinned at an
instruction address).  The fold cell writes one of two constant instruction
words into it each sample — the sign unfold costs ZERO operand registers and
zero data words in the lut cell, which is what lets the 17-entry table fit.
At the index clamp (``idx == 16``, reachable only for dshift > 0) the fold
sends ``frac = 0`` and ``addrq`` then points at the (stale) ``p`` word — the
loaded garbage is multiplied by 0, so ``y = table[16]`` bit-exactly.

Both blocks are STATELESS per sample and strictly feed-forward (2-cell straight
chain, no feedback corridor, no reconvergent fan-in — INV-19/20 do not apply),
memoryless (group delay 0), 1:1 rate.

There is NO stock GNU Radio counterpart; the golden reference is numpy
(``1/(1+exp(-a))`` / ``numpy.tanh``) over the same input mapping — the
library's established pattern for no-counterpart blocks.
"""
import numpy as np
from typing import Any, Dict, List, Tuple

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock, assemble_to_words

# --- the pinned 17-entry Q15 tables: table[i] = round(32768*f(R*i/16)) ------
SIGMOID_TABLE_Q15 = [16384, 20397, 23955, 26790, 28862, 30282, 31214, 31807,
                     32179, 32408, 32549, 32635, 32687, 32719, 32738, 32750,
                     32757]
TANH_TABLE_Q15 = [0, 8025, 15143, 20813, 24956, 27797, 29660, 30847, 31589,
                  32048, 32329, 32501, 32606, 32670, 32708, 32732, 32746]

# --- fixed cell layouts (pinned; a test asserts the resolver agrees) --------
# lut: R0 = addr landing/accumulator, inputs @1/@2, negop @3, table @4..20,
# state p @21, 9 instructions @22..30 (entry 22), patch slot = 7th instr @28.
LUT_ADDRQ_REG = 1
LUT_FRAC_REG = 2
LUT_NEGOP_ADDR = 3
LUT_TABLE_BASE = 4          # addr = LUT_TABLE_BASE + idx
LUT_P_REG = 21
LUT_INSTR_COUNT = 9
LUT_ENTRY = 31 - LUT_INSTR_COUNT            # 22
LUT_PATCH_REG = LUT_ENTRY + 6               # 28: the 7th instruction word
# fold: sample lands at R0; data @1..5; state xs @6.
FOLD_PPOS_ADDR = 1
FOLD_PNEG_ADDR = 2
FOLD_ONE_ADDR = 3
FOLD_TBASE1_ADDR = 4        # LUT_TABLE_BASE + 1 (addrq = idx + this)
FOLD_DOMCAP_ADDR = 5        # dshift > 0 only
FOLD_XS_REG = 6

DSHIFT_MIN, DSHIFT_MAX = -4, 10   # shift immediates must stay in 0..15


def _s16(v: int) -> int:
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def _mulq(a: int, b: int) -> int:
    return _s16((_s16(a) * _s16(b)) >> 15)


def activation_patch_words() -> Tuple[int, int]:
    """The two pre-assembled instruction words the fold cell writes into the
    lut cell's patch slot: (positive, negative).

    * positive: ``MOVE p, R0`` — architecturally harmless (p is dead there);
      it is also exactly the instruction AUTHORED in the slot.
    * negative: ``SUB negop, R0`` — ``R0 = negop - y``: the tanh negate
      (negop = 0) and the wrap-exact sigmoid ``0x8000 - y`` (negop = 0x8000).
    """
    ppos = assemble_to_words(f"MOVE R{LUT_P_REG}, R0")[0]
    pneg = assemble_to_words(f"SUB R{LUT_NEGOP_ADDR}, R0")[0]
    return ppos, pneg


def activation_lut_program(table_q15: List[int], negop: int) -> CellProgram:
    """The lut cell: 17-entry table + interpolation + patched sign unfold.

    Shared builder — ``SigmoidBlock``/``TanhBlock`` use it directly and a
    composite block may embed the identical cell (INV-33 register contract:
    every address is pinned; the program is 31/31 words + the R31 HALT).
    """
    if len(table_q15) != 17:
        raise ValueError("activation table must have exactly 17 entries")
    data = [DataWord("negop", int(negop) & 0xFFFF, address=LUT_NEGOP_ADDR)]
    data += [DataWord(f"t{i}", int(v) & 0xFFFF, address=LUT_TABLE_BASE + i)
             for i, v in enumerate(table_q15)]
    return CellProgram(
        inputs=[Port("addr", register=0),          # accumulator delivery
                Port("addrq", register=LUT_ADDRQ_REG),
                Port("frac", register=LUT_FRAC_REG),
                Port("patch", register=LUT_PATCH_REG)],  # patched instruction
        outputs=[Port("out"), Port("trig")],
        entries=[EntryPoint("default")],
        data=data,
        state=[StateVar("p", register=LUT_P_REG)],
        assembly_template="""\
start:
    LOAD R0
    MOVE R{state:p}, R0
    LOAD R{in:addrq}
    SUB R0, R{state:p}
    MULQ R0, R{in:frac}
    ADD R0, R{state:p}
    MOVE R{state:p}, R0
    {write:out}
    {jump:trig}
""",
    )


def activation_fold_program(dshift: int, ppos: int, pneg: int) -> CellProgram:
    """The fold cell: sign fold + clamp + index/fraction, ``dshift`` folded
    into the shift immediates.  Sends (patch, frac, addrq, addr->R0, trig).

    Three template variants by sign(dshift) — identical numeric contract
    (bit-exact to the reference for every input word, proven exhaustively):

    * dshift <= 0: ``mag = min(|v|, 32767)`` via the V-flag on the negate
      (only −32768 overflows); the index clamp is unreachable.
    * dshift  > 0: ``mag = min(|v|, 2**(15-dshift))`` via one unsigned
      compare (CMP borrow) — equivalent output to clamp-at-32767 plus an
      index clamp, and it absorbs |−32768| for free.
    * frac is computed MASK-FREE: for d >= 0 ``(mag << (5+d)) >> 1`` (the
      left shift flushes the index bits mod 2^16); for d < 0 an extra
      ``>> 5, << 4`` pair also flushes the sub-index bits.
    """
    d = int(dshift)
    if not (DSHIFT_MIN <= d <= DSHIFT_MAX):
        raise ValueError(
            f"HARDWARE LIMIT: dshift={d} outside [{DSHIFT_MIN}, {DSHIFT_MAX}]"
            " (shift counts are immediate instruction fields, 0..15)")
    data = [
        DataWord("ppos", int(ppos) & 0xFFFF, address=FOLD_PPOS_ADDR),
        DataWord("pneg", int(pneg) & 0xFFFF, address=FOLD_PNEG_ADDR),
        DataWord("one", 1, address=FOLD_ONE_ADDR),
        DataWord("tbase1", LUT_TABLE_BASE + 1, address=FOLD_TBASE1_ADDR),
    ]
    if d > 0:
        data.append(DataWord("domcap", (1 << (15 - d)) & 0xFFFF,
                             address=FOLD_DOMCAP_ADDR))

    if d > 0:
        neg_and_cap = """\
    NOT R{state:xs}
    ADD R0, R{data:one}
    MOVE R{state:xs}, R0
    GOTO _cap
_pos:
    MOVE R0, R{data:ppos}
    {write:patch}
_cap:
    CMP R{state:xs}, R{data:domcap}
    BR.C _frac
    MOVE R{state:xs}, R{data:domcap}
_frac:
"""
    else:
        neg_and_cap = """\
    NOT R{state:xs}
    ADD R0, R{data:one}
    BR.NV _nc
    SUB R0, R{data:one}
_nc:
    MOVE R{state:xs}, R0
    GOTO _frac
_pos:
    MOVE R0, R{data:ppos}
    {write:patch}
_frac:
"""
    if d >= 0:
        frac_part = (f"    SHL R{{state:xs}}, #{5 + d}\n"
                     f"    SHR R0, #1\n")
    else:
        frac_part = (f"    SHL R{{state:xs}}, #{5 + d}\n"
                     f"    SHR R0, #5\n"
                     f"    SHL R0, #4\n")
    template = ("""\
start:
    MOVE R{state:xs}, R{in:sample}
    SHR R{state:xs}, #15
    BR.Z _pos
    MOVE R0, R{data:pneg}
    {write:patch}
""" + neg_and_cap + frac_part + f"""\
    {{write:frac}}
    SHR R{{state:xs}}, #{11 - d}
    ADD R0, R{{data:tbase1}}
    {{write:addrq}}
    SUB R0, R{{data:one}}
    {{write:addr}}
    {{jump:trig}}
""")
    return CellProgram(
        inputs=[Port("sample", register=0)],
        outputs=[Port("patch"), Port("frac"), Port("addrq"), Port("addr"),
                 Port("trig")],
        entries=[EntryPoint("default")],
        data=data,
        state=[StateVar("xs", register=FOLD_XS_REG)],
        assembly_template=template,
    )


def activation_ref_word(word: int, table_q15: List[int], negop: int,
                        dshift: int) -> int:
    """Bit-exact model of the two-cell datapath for ONE input word.

    Proven exhaustively equal (all 65536 words, dshift in [-4, 10]) to the
    canonical form ``mag = min(|v|, 32767); idx = mag >> (11-d);
    idx >= 16 -> table[16]; frac = (mag & mask) << (4+d);
    y = P + ((Q-P)*frac >> 15); unfold`` — the design-reference definition.
    """
    d = int(dshift)
    w = int(word) & 0xFFFF
    sgn = w >> 15
    if sgn:
        mag = (~w + 1) & 0xFFFF                  # NOT + ADD one
        if d <= 0 and mag == 0x8000:             # V flag on the ADD
            mag = 0x7FFF
    else:
        mag = w
    if d > 0:
        cap = 1 << (15 - d)
        if mag >= cap:                           # unsigned CMP + BR.C
            mag = cap
    if d >= 0:
        frac = ((mag << (5 + d)) & 0xFFFF) >> 1
    else:
        frac = ((((mag << (5 + d)) & 0xFFFF) >> 5) << 4) & 0xFFFF
    idx = mag >> (11 - d)
    P = table_q15[idx]
    if idx == 16:
        y = P & 0xFFFF                           # frac == 0 by construction
    else:
        y = (P + _mulq(_s16(table_q15[idx + 1] - P), frac)) & 0xFFFF
    if sgn:
        y = (int(negop) - y) & 0xFFFF            # the patched SUB negop, R0
    return y


class _ActivationBase(KyttarBlock):
    """Shared block plumbing for the two-cell activation engine.

    Subclasses pin ``TABLE_Q15`` (17 entries), ``NEGOP`` (the unfold constant)
    and ``CANON_K`` (log2 of the canonical half-domain), plus the float golden.
    """

    CATEGORY = "math_operators"
    GRC_UNSUPPORTED_PARAMS = ()

    TABLE_Q15: List[int] = []
    NEGOP = 0
    CANON_K = 0

    # fold cell: entry 31-22=9, sample lands at R0; lut emits from R0.
    # Static defaults only — the harness resolves per-instance (INV-6).
    _interface = BlockInterface(
        entry_address=9, input_registers=[0], output_registers=[0])

    def __init__(self, name: str, dshift: int = 0):
        d = int(dshift)
        if d != dshift:
            raise ValueError(f"dshift must be an integer, got {dshift!r}")
        if not (DSHIFT_MIN <= d <= DSHIFT_MAX):
            raise ValueError(
                f"HARDWARE LIMIT: dshift={d} outside [{DSHIFT_MIN}, "
                f"{DSHIFT_MAX}] (shift counts are immediate instruction "
                "fields, 0..15; the index/fraction shift immediates are "
                f"11-dshift and 4+dshift)")
        super().__init__(name, dshift=d)
        self._dshift = d
        self._ppos, self._pneg = activation_patch_words()

    # ------------------------------------------------------------------ props
    @property
    def dshift(self) -> int:
        """Binary-point shift of the input word (0 = canonical domain)."""
        return self._dshift

    @property
    def cell_count(self) -> int:
        return 2

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    # ------------------------------------------------------------------ build
    def build_cell_programs(self) -> Dict[str, CellProgram]:
        # dict order == default_layout order (positional pairing, INV-33)
        return {
            "fold": activation_fold_program(self._dshift, self._ppos,
                                            self._pneg),
            "lut": activation_lut_program(self.TABLE_Q15, self.NEGOP),
        }

    def internal_connections(self) -> List[Tuple[Any, str, Any, str]]:
        # template write order: patch, frac, addrq, then addr LAST (addr is
        # the accumulator delivery into the lut's R0 — INV-33).
        return [("fold", "patch", "lut", "patch"),
                ("fold", "frac", "lut", "frac"),
                ("fold", "addrq", "lut", "addrq"),
                ("fold", "addr", "lut", "addr")]

    def internal_jumps(self) -> List[Tuple[Any, str, Any, str]]:
        return [("fold", "trig", "lut", "default")]

    def output_cell_ids(self):
        return ["lut"]

    def default_layout(self) -> Dict[Any, Tuple[int, int, str]]:
        # 2x1 fold, I/O co-located on the bus edge (INV-8/14), output on the
        # LAST cell (INV-10) — the nlog10 shape.
        return {"fold": (0, 0, "east"), "lut": (1, 0, "east")}

    # -------------------------------------------------------------- reference
    def process_reference_q15(self, x_q15) -> list:
        """Bit-exact predictor of the on-chip datapath (uint16 words)."""
        return [activation_ref_word(int(w), self.TABLE_Q15, self.NEGOP,
                                    self._dshift)
                for w in x_q15]

    def _float_fn(self, a: np.ndarray) -> np.ndarray:
        raise NotImplementedError

    def process_reference(self, input_samples) -> np.ndarray:
        """Float golden: ``f(x * 2**(CANON_K + dshift))`` for Q15 float input
        ``x`` in [-1, 1), clipped/quantizable to Q15 — numpy is the reference
        (no stock GNU Radio counterpart)."""
        arr = np.asarray(input_samples, dtype=np.float64)
        a = arr * float(2 ** (self.CANON_K + self._dshift))
        out = self._float_fn(a)
        return np.clip(out, -1.0, 32767.0 / 32768.0).astype(np.float32)

    def reset(self):
        """Memoryless — no state carried across samples."""
        pass


class SigmoidBlock(_ActivationBase):
    """
    Q15 logistic sigmoid — ``out = 1 / (1 + exp(-a))``.

    No stock GNU Radio counterpart (golden reference: numpy). The input word
    ``v`` is interpreted with a configurable binary point via the ``dshift``
    parameter: it represents the pre-activation ``a = (v/32768) * 2**(3 +
    dshift)``. The default ``dshift = 0`` is the canonical domain — full-scale
    input maps to +-8, where sigmoid is within 3.4e-4 of its asymptote. An
    upstream dot product prescaled by ``2**-S`` for Q15 headroom is
    compensated with ``dshift = S - 3`` at zero instruction cost (the shift
    folds into the index/fraction immediates — never into the table domain,
    which is a measured accuracy failure for tanh; see the module docstring).

    Datapath: 16-interval table + linear interpolation (17 Q15 entries over
    [0, 8]), sign-folded (``sigmoid(-a) = 1 - sigmoid(a)``; the Q15 unfold
    ``0x8000 - y`` is wrap-exact). Two cells (fold -> lut), feed-forward,
    memoryless, delay 0, 1:1 rate. Output is unsigned-positive Q15 in
    [0x0001, 0x7FF5] (i.e. (0, 1)); out(0) = 0.5 = 16384 exactly.

    Accuracy vs float (exhaustive over all 65536 input words): max abs error
    0.0030, RMS 0.0010 — identical at dshift = +1/+2/-1.

    Parameters:
        dshift: integer binary-point shift of the input word (default 0).
            Valid range [-4, 10] (shift-immediate hardware limit; raises
            outside it).

    Interface: entry R9 (default dshift; resolve per-instance, INV-6),
    input R0 of the fold cell, output R0 of the lut cell.
    """

    TAGS = ["sigmoid", "logistic", "activation", "neural", "math_operators"]

    TABLE_Q15 = SIGMOID_TABLE_Q15
    NEGOP = 0x8000          # unfold: 0x8000 - y (wrap-exact)
    CANON_K = 3             # canonical domain [-8, 8]

    def _float_fn(self, a: np.ndarray) -> np.ndarray:
        return 1.0 / (1.0 + np.exp(-a))


class TanhBlock(_ActivationBase):
    """
    Q15 hyperbolic tangent — ``out = tanh(a)``.

    No stock GNU Radio counterpart (golden reference: numpy.tanh). The input
    word ``v`` represents ``a = (v/32768) * 2**(2 + dshift)``; the default
    ``dshift = 0`` maps full-scale input to +-4, where tanh is within 6.7e-4
    of its asymptote. An upstream dot product prescaled by ``2**-S`` is
    compensated with ``dshift = S - 2`` at zero instruction cost (see
    :class:`SigmoidBlock` and the module docstring).

    Datapath: identical two-cell engine as :class:`SigmoidBlock` — only the
    17-entry table (tanh over [0, 4]) and the unfold constant differ (tanh is
    odd: ``negop = 0`` gives ``-y``). Feed-forward, memoryless, delay 0,
    1:1 rate. Output is signed Q15 in (-1, 1); out(0) = 0 exactly.

    Accuracy vs float (exhaustive over all 65536 input words): max abs error
    0.0060, RMS 0.0021 — identical at dshift = +1/+2/-1.

    Parameters:
        dshift: integer binary-point shift of the input word (default 0).
            Valid range [-4, 10] (shift-immediate hardware limit; raises
            outside it).

    Interface: entry R9 (default dshift; resolve per-instance, INV-6),
    input R0 of the fold cell, output R0 of the lut cell.
    """

    TAGS = ["tanh", "activation", "neural", "math_operators"]

    TABLE_Q15 = TANH_TABLE_Q15
    NEGOP = 0x0000          # unfold: 0 - y (odd function)
    CANON_K = 2             # canonical domain [-4, 4]

    def _float_fn(self, a: np.ndarray) -> np.ndarray:
        return np.tanh(a)
