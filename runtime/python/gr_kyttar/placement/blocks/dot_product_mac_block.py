# SPDX-License-Identifier: GPL-3.0-or-later
"""DotProductMACBlock — see :class:`DotProductMACBlock`."""
import math
from typing import Dict, List, Optional

import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock


def _clip_q15(v: int) -> int:
    return max(-32768, min(32767, int(v)))


def _s16(v: int) -> int:
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def scale_schedule(coefficients, bias):
    """The pinned coefficient-headroom scale schedule (INV-13, correlator form).

    Returns ``(S, coeff_q, bias_q)`` where ``S`` is the headroom shift and the
    Q15 words are the PRESCALED stored constants:

      1. ``S = max(0, ceil(log2(sum|c| + |b|)))`` — from the ORIGINAL floats.
      2. store ``q = round(v * 2^-S * 32768)`` for every coefficient AND the bias.
      3. **POST-ROUNDING GUARD (load-bearing):** if ``sum|q| > 32767`` after
         rounding, bump ``S`` by one and requantize. Rounding really does trip
         this (e.g. every ``q`` rounding UP can push an in-range float sum one
         LSB over), and without the bump the no-wrap invariant below is one LSB
         from false.

    With the guard, ``|bias_q| + sum|coeff_q| <= 32767``, so the running MACQ
    accumulator (bias preload + K truncating Q15 products of the scaled
    coefficients against ``|x| <= 1`` inputs) is bounded by 32767 at EVERY
    partial sum — intermediate 16-bit wrap is IMPOSSIBLE. The verification
    suite asserts this over random coefficient sets and carries the mandatory
    guard-removal mutation (INV-4).
    """
    vals = [float(c) for c in coefficients] + [float(bias)]
    tot = sum(abs(v) for v in vals)
    S = max(0, int(math.ceil(math.log2(tot)))) if tot > 0 else 0
    while True:
        qs = [_clip_q15(round(v * (2.0 ** -S) * 32768.0)) for v in vals]
        if sum(abs(q) for q in qs) <= 32767:
            return S, qs[:-1], qs[-1]
        S += 1


class DotProductMACBlock(KyttarBlock):
    """
    Fixed-coefficient dot product over a K-element input vector (a
    correlator-style weighted sum with a bias) — the reusable "weight row"
    primitive. No stock GNU Radio counterpart (verified against a bit-exact
    numpy golden; the parameter names follow the Kyttar FIR family).

    FUNCTION
    --------
    The block consumes a FRESH K-element vector per output: K consecutive input
    samples form one vector, one output word is emitted, and the next K samples
    form the next vector. There is NO delay line and NO sample aging — this is
    the correlator/dot-product pattern, NOT the FIR pattern (an FIR streams
    every sample past every tap; this block uses each sample exactly once).
    It is rate-REDUCING (K inputs -> 1 output); a trailing partial vector of
    fewer than K samples is never emitted (``floor(nin/K)`` outputs).

    Per vector, with the PRESCALED stored constants (see the scale schedule):

        y_raw = bias_q + sum_{i=0..K-1} MULQ(coeff_q[i], x[i])

    where ``MULQ(a, b) = (a*b) >> 15`` (truncating) and the accumulator is a
    16-bit word. The accumulator PRELOADS the scaled bias, then performs K
    MULQ-accumulates as the samples arrive — one multiply-accumulate per
    trigger, coefficient fetched by a LOAD-indirect walk over the cell-local
    coefficient table.

    THE SCALE SCHEDULE (coefficient headroom, INV-13)
    -------------------------------------------------
    ``S = max(0, ceil(log2(sum|c| + |b|)))``; every coefficient and the bias
    are stored as ``round(v * 2^-S * 32768)``. A POST-ROUNDING GUARD then bumps
    ``S`` by one and requantizes if the rounded magnitudes sum past 32767 —
    rounding does trip this in practice. With the guard, intermediate wrap is
    impossible at every partial sum (no per-tap clamping needed anywhere).

    OUTPUT MODES (``mode``)
    -----------------------
    * ``"raw"`` (default): emit the accumulated word UNRESTORED — the emitted
      Q15 word represents ``y / 2^S``. This is the composite-use form: a
      downstream consumer absorbs the ``2^S`` scale into its own shift
      immediates (the folded-shift idiom, cf. Nlog10Block's scaled output
      convention). The derived ``S`` is exposed as the read-only
      :attr:`scale_shift` property (and mirrored in the block's metrics /
      manifest notes) so downstream instances can be configured with it.
      Single cell.
    * ``"restored"``: shift the accumulator left by ``S`` WITH SATURATION
      before emitting (pins to +/-full-scale on true overdrive) — the
      standalone drop-in form whose output is ``clamp(y)`` in plain Q15.
      When ``S == 0`` the restore is the identity (raw == restored, 1 cell);
      when ``S > 0`` the saturating restore lives in a second ``restore``
      cell (2-cell feed-forward chain ``mac -> restore``, I/O co-located on
      one row — INV-8/14), using the exact FIR bias-and-shift saturating
      left shift (SHL reports no overflow, so overflow is detected with
      ``(acc + 2^(15-S)) >> (16-S) != 0`` and pinned to ``0x7FFF + signbit``).

    SUPPORTED RANGE (documented hardware discipline — raises loudly)
    ----------------------------------------------------------------
    * ``k`` (the vector length K) must be **2..7**: one cell holds the K
      coefficients + bias + counters + code (K + 3 data words + 3 state
      registers + the 14-instruction MAC walk, comfortably inside the 32-word
      cell at K = 7; the cap is the pinned per-cell MAC-operand discipline —
      coefficients co-resident with the code). Out-of-range K RAISES.
    * ``S`` must be <= 15 (the SHL/SHR immediate count field is 4 bits). A
      coefficient set with ``sum|c| + |b| > 2^15`` RAISES.
    * ``len(coefficients)`` must equal ``k`` — a mismatch RAISES (never
      silently truncated or padded).

    Interface:
        - Entry: R1
        - Input: R0 (``sample``, one Q15 word per trigger)
        - Output: ``out`` — one word per K triggers (raw or restored).
    """
    CATEGORY = "math_operators"
    TAGS = ["dot product", "correlator", "mac", "weighted sum", "inner product",
            "math_operators"]

    _interface = BlockInterface(
        entry_address=1, input_registers=[0], output_registers=[0])

    MIN_K = 2
    MAX_K = 7
    MAX_SHIFT = 15          # SHL/SHR immediate count field is 4 bits (0..15)
    SAT_POS_Q15 = 0x7FFF    # +full-scale rail; 0x7FFF + signbit also yields 0x8000

    def __init__(self, name: str, coefficients: Optional[List[float]] = None,
                 bias: float = 0.0, k: Optional[int] = None, mode: str = "raw"):
        """Fixed-coefficient dot product (weighted sum) over K-sample vectors.

        Args:
            name: block instance name.
            coefficients: the K weights (floats; arbitrary magnitude — the
                scale schedule absorbs ``sum|c| + |b|`` up to ``2^15``).
                Default ``[0.25, 0.25, 0.25, 0.25]`` (a 4-point average).
            bias: the additive constant preloaded into the accumulator
                (float, same generality as the coefficients). Default 0.0.
            k: the vector length K (2..7). ``None`` derives it from
                ``len(coefficients)``; an explicit ``k`` MUST equal it.
            mode: ``"raw"`` (default — emit the 2^-S-scaled accumulator; see
                :attr:`scale_shift`) or ``"restored"`` (saturating left shift
                by S before emit).
        """
        if coefficients is None:
            coefficients = [0.25, 0.25, 0.25, 0.25]
        coefficients = [float(c) for c in coefficients]
        if k is None:
            k = len(coefficients)
        k = int(k)
        if mode not in ("raw", "restored"):
            raise ValueError(
                f"mode must be 'raw' or 'restored'; got {mode!r}.")
        if not (self.MIN_K <= k <= self.MAX_K):
            raise ValueError(
                f"DotProductMACBlock supports k in {self.MIN_K}..{self.MAX_K} "
                f"(one cell holds the K coefficients + bias + code; the "
                f"per-cell MAC-operand discipline caps K at {self.MAX_K}); "
                f"got k={k}. Not silently clamping.")
        if len(coefficients) != k:
            raise ValueError(
                f"len(coefficients)={len(coefficients)} must equal k={k} "
                f"(never silently truncated or padded).")
        super().__init__(name, coefficients=list(coefficients), bias=float(bias),
                         k=k, mode=mode)
        self._coefficients = coefficients
        self._bias = float(bias)
        self._k = k
        self._mode = mode

        # The pinned scale schedule (S + prescaled stored constants).
        S, cq, bq = scale_schedule(coefficients, bias)
        if S > self.MAX_SHIFT:
            raise ValueError(
                f"HARDWARE LIMIT: the derived headroom shift S={S} exceeds "
                f"{self.MAX_SHIFT} (the shift immediate count field is 4 bits) "
                f"— sum|coefficients| + |bias| must be <= 2^{self.MAX_SHIFT}. "
                f"Not silently rescaling.")
        self._scale_shift = S
        self._coeff_q15 = cq
        self._bias_q15 = bq

    # ------------------------------------------------------------------ props
    @property
    def coefficients(self) -> List[float]:
        return list(self._coefficients)

    @property
    def bias(self) -> float:
        return self._bias

    @property
    def k(self) -> int:
        return self._k

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def scale_shift(self) -> int:
        """The derived headroom shift S (metadata for downstream consumers).

        In ``"raw"`` mode the emitted word represents ``y / 2^S``; a downstream
        block absorbs the scale by folding ``S`` into its own shift immediates.
        Derived from ``coefficients``/``bias`` by the pinned scale schedule
        (post-rounding guard included) — read-only, never a free knob."""
        return self._scale_shift

    @property
    def quantized_coefficients(self) -> List[int]:
        """The PRESCALED stored Q15 coefficient words (signed ints)."""
        return list(self._coeff_q15)

    @property
    def quantized_bias(self) -> int:
        """The PRESCALED stored Q15 bias word (signed int)."""
        return self._bias_q15

    @property
    def cell_count(self) -> int:
        # raw (any S) and restored-at-S=0 are the single MAC cell; a restored
        # block with S > 0 adds the saturating-restore cell.
        return 2 if (self._mode == "restored" and self._scale_shift > 0) else 1

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    # ------------------------------------------------------------------ build
    def _mac_cell(self, two_cell: bool) -> CellProgram:
        """The MAC cell: coefficient LOAD-indirect walk + bias-preloaded
        accumulator. One trigger = one sample = one MULQ-accumulate; every K-th
        trigger emits the accumulated word and re-arms (acc <- scaled bias,
        idx <- 1).

        The coefficient for sample i of the vector lives at address ``1 + i``
        (natural order); ``idx`` walks 1..K and doubles as the LOAD address.
        The input is snapshotted FIRST (LOAD writes R0, where the sample
        lands). ``acc`` cold-starts at the scaled bias (StateVar initial), and
        the emit path re-preloads it for the next vector BEFORE the write
        (MOVE does not disturb R0), so the bias preload holds for every vector.
        """
        K = self._k
        data = [DataWord(f"c{i}", q & 0xFFFF, address=1 + i)
                for i, q in enumerate(self._coeff_q15)]
        data += [
            DataWord("one", 1, address=K + 1),
            DataWord("kend", K + 1, address=K + 2),
            DataWord("biasw", self._bias_q15 & 0xFFFF, address=K + 3),
        ]
        state = [
            StateVar("xs"),
            StateVar("acc", initial_value=self._bias_q15 & 0xFFFF),
            StateVar("idx", initial_value=1),
        ]
        write_p, jump_p = (("acc_fwd", "trig") if two_cell else ("out", "out"))
        outputs = ([Port("acc_fwd"), Port("trig")] if two_cell
                   else [Port("out")])
        template = f"""\
start:
    MOVE R{{state:xs}}, R{{in:sample}}
    LOAD R{{state:idx}}
    MULQ R0, R{{state:xs}}
    ADD R{{state:acc}}, R0
    MOVE R{{state:acc}}, R0
    ADD R{{state:idx}}, R{{data:one}}
    MOVE R{{state:idx}}, R0
    CMP R{{state:idx}}, R{{data:kend}}
    BR.NZ done
    MOVE R0, R{{state:acc}}
    MOVE R{{state:acc}}, R{{data:biasw}}
    MOVE R{{state:idx}}, R{{data:one}}
    {{write:{write_p}}}
    {{jump:{jump_p}}}
done:
"""
        return CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=outputs,
            entries=[EntryPoint("default")],
            data=data,
            state=state,
            assembly_template=template,
        )

    def _restore_cell(self) -> CellProgram:
        """The saturating-restore cell (restored mode, S > 0): the exact FIR
        bias-and-shift saturating left shift by S (SHL sets no overflow flag;
        ``acc << S`` overflows iff ``(acc + 2^(15-S)) >> (16-S) != 0``
        (logical), and the rail is ``0x7FFF + signbit``). Two-path structure
        with a duplicated emit and a terminal HALT on the in-range path — a
        branch must never target a write/jump placeholder label, and a remote
        JUMP does not stop local execution (the proven FIR emit form)."""
        S = self._scale_shift
        return CellProgram(
            inputs=[Port("acc", register=0)],
            outputs=[Port("out"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("bias", 1 << (15 - S), address=1),
                DataWord("satpos", self.SAT_POS_Q15, address=2),
            ],
            state=[StateVar("save")],
            assembly_template=f"""\
start:
    MOVE R{{state:save}}, R{{in:acc}}
    ADD R{{state:save}}, R{{data:bias}}
    SHR R0, #{16 - S}
    BR.NZ _sat
    SHL R{{state:save}}, #{S}
    {{write:out}}
    {{jump:trig}}
    HALT
_sat:
    SHR R{{state:save}}, #15
    ADD R0, R{{data:satpos}}
    {{write:out}}
    {{jump:trig}}
""",
        )

    def build_cell_programs(self) -> Dict:
        if self.cell_count == 1:
            return {"mac": self._mac_cell(two_cell=False)}
        return {
            "mac": self._mac_cell(two_cell=True),
            "restore": self._restore_cell(),
        }

    def internal_connections(self):
        if self.cell_count == 2:
            return [("mac", "acc_fwd", "restore", "acc")]
        return []

    def internal_jumps(self):
        if self.cell_count == 2:
            return [("mac", "trig", "restore", "default")]
        return []

    def default_layout(self):
        if self.cell_count == 1:
            return {"mac": (0, 0, "east")}
        # 2-cell feed-forward chain on one row: input on `mac`, output on
        # `restore` (the LAST cell) — I/O co-located on the bus-facing row
        # (INV-8; the canonical n=2 -> 2x1 fold, INV-14).
        return {"mac": (0, 0, "east"), "restore": (1, 0, "east")}

    # -------------------------------------------------------------- reference
    def _emit_word(self, acc: int) -> int:
        """The emitted word for a completed vector's accumulator (signed)."""
        if self._mode == "raw" or self._scale_shift == 0:
            return acc
        S = self._scale_shift
        return _clip_q15(acc << S)   # == the on-chip bias-and-shift restore

    def process_reference_q15(self, x_q15) -> list:
        """Bit-exact predictor of the on-chip datapath.

        Models the exact accumulation order: acc preloads the PRESCALED bias
        word, then per sample ``acc += (coeff_q[i] * x) >> 15`` (truncating
        product, 16-bit wrapping add — though with the post-rounding guard the
        partial sum provably never leaves int16). Every K-th sample emits
        (raw: the accumulator; restored: ``clamp(acc << S)``) and re-arms.
        A trailing partial vector emits nothing. Returns uint16 words.
        """
        out = []
        acc = self._bias_q15
        i = 0
        for w in x_q15:
            x = _s16(int(w) & 0xFFFF)
            acc = _s16(acc + ((self._coeff_q15[i] * x) >> 15))
            i += 1
            if i == self._k:
                out.append(self._emit_word(acc) & 0xFFFF)
                acc = self._bias_q15
                i = 0
        return out

    def process_reference(self, input_samples) -> np.ndarray:
        """Float reference: per K-sample vector, ``y = bias + sum c[i]*x[i]``.

        raw mode returns ``y / 2^S`` (the scaled on-chip word convention);
        restored mode returns ``y`` clipped to Q15 ``[-1, 32767/32768]``.
        ``floor(n/K)`` outputs; the trailing partial vector is dropped.
        """
        arr = np.asarray(input_samples, dtype=np.float64).reshape(-1)
        n_out = len(arr) // self._k
        out = np.zeros(n_out, dtype=np.float64)
        c = np.asarray(self._coefficients, dtype=np.float64)
        for j in range(n_out):
            y = self._bias + float(c @ arr[j * self._k:(j + 1) * self._k])
            if self._mode == "raw":
                out[j] = y / (2.0 ** self._scale_shift)
            else:
                out[j] = min(max(y, -1.0), 32767.0 / 32768.0)
        return out.astype(np.float32)

    def reset(self):
        """No cross-call state (each reference call is a fresh stream)."""
        pass
