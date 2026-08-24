# SPDX-License-Identifier: GPL-3.0-or-later
"""ChirpGeneratorBlock — see :class:`ChirpGeneratorBlock`."""
import math
from typing import Any, Dict, List, Tuple

import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, float_to_q15
from .nco_block import NCOBlock


class ChirpGeneratorBlock(NCOBlock):
    """
    Chirp-Spread-Spectrum (CSS) modulator — cyclic-shifted linear up-chirps.

    (placeKYT-native; no stock GNU Radio streaming counterpart — Python golden.)

    For each input word ``s`` (a RAW symbol word in ``0..m-1``) the block emits
    ``n`` complex samples of a linear UP-chirp whose instantaneous frequency
    starts at ``f(s) = (s/m - 1/2)·BW`` and sweeps upward by ``BW`` over the
    ``n`` samples, WRAPPING (mod BW) when it crosses ``+BW/2`` — the standard
    cyclic-shifted chirp ``c_s[k] = exp(j·2π·Σφ)``. ``BW`` is the full digital
    bandwidth (the sample rate: -fs/2..+fs/2). This is the CSS/LoRa-style symbol
    waveform: the cyclic shift of the base chirp encodes the symbol, and a
    dechirp + N-point FFT + argmax recovers ``s`` as the winning bin.

    THE 16-BIT WRAPAROUND *IS* THE MOD (the load-bearing insight)
    ------------------------------------------------------------
    The datapath is a DOUBLE phase accumulator in 16-bit two's-complement:

        freq  <- (s << (16 - log2 m)) + 0x8000        (per SYMBOL, mod 2^16)
        each sample:   emit exp(j·2π·phase/65536)
                       phase <- phase + freq          (mod 2^16)
                       freq  <- freq  + rate          (mod 2^16), rate = 65536/n

    A frequency word is a signed Q0.15-style phase increment: ``0x8000 = -32768
    = -BW/2``; ``+32767 ≈ +BW/2``. Adding ``rate`` past ``+BW/2`` overflows the
    16-bit word straight to the ``-BW/2`` end — the natural integer wraparound IS
    the ``mod BW`` cyclic shift. No compare, no branch: the wrap path costs zero
    instructions and cannot be "missed". (Gated: a saturate-instead-of-wrap
    mutant golden must FAIL on any symbol whose sweep crosses +BW/2 mid-symbol.)

    Exact word arithmetic (all mod 2^16):
      * ``step = 65536/m`` (m power of two) — applied as ``s << (16 - log2 m)``.
      * initial frequency word ``fw(s) = s·step + 0x8000``  ==  Q15 frequency
        ``(s/m - 1/2)`` of full scale, i.e. ``f(s) = (s/m - 1/2)·fs`` Hz.
      * chirp-rate word ``rate = 65536/n`` — per-sample increment, so the sweep
        covers the full 65536 (= BW) in exactly n samples. n a power of two
        makes ``rate`` exact (no frequency drift vs the ideal chirp — the ONLY
        error is the NCO's 33-entry-table interpolation floor, ≈11 LSB).
      * per-sample order: emit at the CURRENT phase, THEN ``phase += freq``,
        THEN ``freq += rate`` — so sample 0 of a symbol sits at the carried
        phase with instantaneous frequency ``fw(s)``.

    PHASE CONTINUITY (pinned + gated): the phase accumulator CARRIES across
    symbols — it is NEVER reset at a symbol boundary. This is the natural
    hardware behaviour (zero instructions), gives the phase-continuous TX
    spectrum, and is invisible to the magnitude-based (dechirp+FFT+argmax) CSS
    receiver. With n even and m | n, each symbol advances the carried phase by
    exactly half a cycle (+π), so a reset-per-symbol mutant golden differs by a
    sign flip on odd symbols — the continuity gate proves the pinned convention.

    ITERATION = a SELF-PACED return kick (why this block cannot flood itself)
    -------------------------------------------------------------------------
    The datapath is the verified NCO 10-cell pipeline (phase | sin arm | cos arm
    | emit) with a swept-increment phase cell. Emitting the n samples of a
    symbol back-to-back from the phase cell would put 2+ samples inside the
    NCO's reconvergent fan-in — the proven INV-20 deadlock. Instead the block
    SELF-PACES: the sweep cell emits ONE sample per activation, and the ``emit``
    cell — the point every sample has fully cleared the pipeline — kicks the
    sweep cell's ``iternext`` entry (a backward WRITE+JUMP one cell WEST via an
    in-program FACE flip, the dual-face idiom of the INV-20 lock-clear). The
    sweep cell counts samples with the SAME wrap trick (``cnt += rate`` wraps to
    0 after exactly n kicks — no count constant, no compare).

    SATURATION SAFETY: on symbol receipt the sweep cell LOCKs its input arbiter
    to the kick face (INV-19 serialize-LOCK idiom, always on), so a saturated
    back-to-back symbol stream is held at the arbiter until the current symbol's
    n samples have drained; the final kick clears the LOCK (``MOVE [LOCK], R0``
    with the wrapped-to-zero counter already in R0). One symbol is in flight at
    a time by construction; the port FIFO still pipelines input.

    Parameters (no GR counterpart to mirror; names shared with the CSS family):
      * ``n`` — samples per chirp symbol (power of two, 2..65536; verified
        16..128). Chirp rate = BW/n per sample.
      * ``m`` — symbol alphabet size (power of two, 2 ≤ m ≤ n). The classic
        LoRa-style configuration is m == n (the default).
      * ``amplitude`` — output amplitude 0..1 (Q15 gain in the emit cell),
        default 1.0.

    Input: one RAW symbol word per symbol (0..m-1; wider words are taken mod m
    by the 16-bit shift wrap). Output: n complex (yi, yq) pairs per input word —
    rate-EXPANDING 1:n with a complex-packet egress (INV-17 budgeted).

    Precision: inherits the NCO's 33-entry quarter-wave interpolated table —
    ≈11 LSB worst case vs the exact tone (measured SNR vs the ideal float chirp
    is reported by the test; the frequency words themselves are EXACT).
    """
    CATEGORY = "modulation"
    TAGS = ["chirp", "css", "spread_spectrum", "modulator", "sweep", "modulation"]

    # REAL input (one raw symbol word), complex (yi, yq) output.
    _interface = BlockInterface(
        entry_address=1, input_registers=[0], output_registers=[0, 1])

    def __init__(self, name: str, n: int = 128, m: int = 128,
                 amplitude: float = 1.0):
        n = int(n)
        if n < 2 or n > 65536 or (n & (n - 1)):
            raise ValueError(
                f"ChirpGeneratorBlock: n must be a power of two in [2, 65536] "
                f"(the 16-bit chirp-rate word 65536/n must be a nonzero "
                f"integer); got {n}")
        m = int(m)
        if m < 2 or (m & (m - 1)) or m > n:
            raise ValueError(
                f"ChirpGeneratorBlock: m must be a power of two with "
                f"2 <= m <= n (symbol step 65536/m, alphabet within one chirp "
                f"length); got m={m}, n={n}")
        if not (0.0 <= float(amplitude) <= 1.0):
            raise ValueError(
                f"ChirpGeneratorBlock: amplitude must be in [0, 1] (a Q15 "
                f"gain); got {amplitude}")
        # Reuse the NCO plumbing (table pipeline + emit). frequency=0 is unused
        # (the phase cell is replaced); amplitude rides the NCO emit MULQ.
        super().__init__(name, sample_rate=1.0, frequency=0.0,
                         amplitude=float(amplitude), offset=0.0, phase=0.0,
                         waveform="cos", pipeline_lock=False)
        # KyttarBlock records **kwargs from the NCO super() call; re-record THIS
        # block's real GRC-facing params (n, m, amplitude) over the NCO internals.
        self._kwargs = {"n": n, "m": m, "amplitude": float(amplitude)}
        self._n = n
        self._m = m
        self._rate_word = 65536 // n          # chirp-rate word (exact, nonzero)
        self._shift = 16 - int(round(math.log2(m)))   # s -> s*step via SHL

    # ------------------------------------------------------------- properties
    @property
    def n(self) -> int:
        return self._n

    @property
    def m(self) -> int:
        return self._m

    @property
    def rate_word(self) -> int:
        """The derived per-sample chirp-rate word (65536/n)."""
        return self._rate_word

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    @property
    def cell_count(self) -> int:
        return 10  # the NCO 10-cell pipeline; the kick corridor is the @1 abutment

    # ------------------------------------------------------------------ cells
    def build_cell_programs(self) -> Dict[str, CellProgram]:
        # Inherit the verified NCO cells (sin/cos fold+table+interp), then
        # REPLACE the phase cell (constant-increment NCO -> double accumulator
        # sweep with the iternext kick entry) and the emit cell (adds the
        # backward return kick). Cell ids/order stay the NCO's so the inherited
        # internal connections, jump chain, and 2x5 default_layout line up.
        cells = super().build_cell_programs()

        # --- sweep cell (id "phase"): the double phase accumulator -----------
        # Data words pack contiguously at 1..4; state PINNED at 5..8 (INV-33).
        # lock_face = EAST(1): the kick corridor is emit(1,0) -> FACE-flip WEST
        # -> @1 into phase(0,0), so the kick ENTERS phase on its EAST side; the
        # is_face word rotates with the block. LOCK is engaged with the `one`
        # word (the LOCK CONFIG reads BIT 0 — see the DataWord note) and
        # cleared in iternext with the wrapped-to-zero counter already in R0.
        cells["phase"] = CellProgram(
            inputs=[Port("s", register=0)],
            outputs=[Port("ph_sin"), Port("ph_cos"), Port("trig")],
            entries=[EntryPoint("default"), EntryPoint("iternext")],
            data=[DataWord("half", 0x8000, address=1),
                  DataWord("quarter", 16384, address=2),
                  DataWord("rate", self._rate_word, address=3),
                  DataWord("lock_face", 1, address=4, is_face=True),
                  # LOCK enable value: the arbiter's LOCK CONFIG reads BIT 0
                  # (measured: MOVE [LOCK], rate=0x1000 left the cell UNLOCKED
                  # and saturated symbols barged into a mid-flight iteration) —
                  # "any nonzero" is NOT sufficient, the value must have bit0=1.
                  DataWord("one", 1, address=5)],
            state=[StateVar("phase", register=6, initial_value=0),
                   StateVar("freq", register=7),
                   StateVar("cnt", register=8)],
            # start: freq = (s << shift) + 0x8000 (mod 2^16); LOCK the arbiter
            #        to the kick face; emit sample 0 of the symbol.
            # iternext (kicked by emit once a sample has fully drained):
            #        cnt += rate — wraps to 0 after exactly n kicks (the same
            #        16-bit-wrap trick as the cyclic shift; no count constant).
            #        Nonzero -> emit the next sample; zero -> clear LOCK (R0
            #        already holds the wrapped 0) and go idle for the next
            #        symbol held at the arbiter.
            # emit_s: emit at the CURRENT phase (sin = phase, cos = phase+90°),
            #        then phase += freq, then freq += rate — both wrapping.
            assembly_template=f"""\
start:
    SHL R{{in:s}}, #{self._shift}
    ADD R0, R{{data:half}}
    MOVE R{{state:freq}}, R0
    MOVE [LOCK_FACE], R{{data:lock_face}}
    MOVE [LOCK], R{{data:one}}
    GOTO emit_s
iternext:
    ADD R{{state:cnt}}, R{{data:rate}}
    MOVE R{{state:cnt}}, R0
    BR.NZ emit_s
    MOVE [LOCK], R0
    HALT
emit_s:
    MOVE R0, R{{state:phase}}
    {{write:ph_sin}}
    ADD R{{state:phase}}, R{{data:quarter}}
    {{write:ph_cos}}
    ADD R{{state:phase}}, R{{state:freq}}
    MOVE R{{state:phase}}, R0
    ADD R{{state:freq}}, R{{data:rate}}
    MOVE R{{state:freq}}, R0
    {{jump:trig}}
""",
        )

        # --- emit cell: NCO emit + the backward return kick ------------------
        # The kick (a bare JUMP to the sweep cell's iternext entry) fires LAST,
        # after the yi/yq pair has left the cell — so a sample is FULLY OUT of
        # the pipeline before the sweep launches the next one (strict
        # one-sample-at-a-time; the INV-20 reconvergent fan-in can never hold
        # two samples). An early kick (before yi/yq) permits 2-sample
        # co-residency — the arms run sample k+1 while emit drains sample k —
        # and a saturated-drive deadlock was observed in that form (a circular
        # wait through the serpentine's shared column transits, entangled with
        # the LOCK bit-0 defect fixed in the sweep cell); the kick-last form
        # removes the co-residency class outright. Kicking after yi/yq also
        # paces symbol generation to the output-port drain — the correct
        # backpressure.
        # The kick is JUMP-ONLY (no data WRITE) so the cell's data-write tail
        # stays the canonical complex-egress shape: yi/yq are the LAST data
        # WRITEs (what _patch_last_write_handoff / the complex tail patchers
        # key on — a trailing kick WRITE was measurably re-patched into the
        # output corridor and killed the iteration). The downstream
        # {jump:trig} fires BEFORE the FACE flip so it rides the routed output
        # face; execution continues past it into the kick, and the trailing
        # FACE restore re-arms the output face for the next sample.
        # ret_face = WEST(2) (emit(1,0) -> phase(0,0) is the @1 west abutment,
        # rigid under D4 with the is_face word); face_tap is auto-set to the
        # REAL routed output face by build._apply_rotate_tap_face.
        cells["emit"] = CellProgram(
            inputs=[Port("cos_mag", register=0), Port("sin_mag", register=1)],
            outputs=[Port("yi"), Port("yq"), Port("trig"), Port("ret_trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("amp", self._amp_q15, address=2),
                  DataWord("ret_face", 2, address=3, is_face=True),
                  DataWord("face_tap", 1, address=4, is_face=True)],
            state=[StateVar("cv", register=5), StateVar("sv", register=6)],
            assembly_template="""\
start:
    MOVE R{state:cv}, R{in:cos_mag}
    MOVE R{state:sv}, R{in:sin_mag}
    MULQ R{state:cv}, R{data:amp}
    {write:yi}
    MULQ R{state:sv}, R{data:amp}
    {write:yq}
    {jump:trig}
    MOVE [FACE], R{data:ret_face}
    {jump:ret_trig}
    MOVE [FACE], R{data:face_tap}
""",
        )
        return cells

    def internal_jumps(self) -> List[Tuple[str, str, str, str]]:
        jumps = super().internal_jumps()
        # The BACKWARD return kick: fire the sweep cell's iternext entry over
        # the @1 west abutment (FACE flipped to ret_face in-program). Resolved
        # by the router's internal-jumps branch (named entry) and re-patched +
        # PRESERVED against exit-defaulting by _apply_internal_feedback's
        # backward-jump pass.
        jumps.append(("emit", "ret_trig", "phase", "iternext"))
        return jumps

    # -------------------------------------------------------------- reference
    def process_reference_q15(self, symbols) -> List[Tuple[int, int]]:
        """Bit-exact on-chip predictor: n ``(yi, yq)`` unsigned Q15 pairs per
        input symbol word. Models the double accumulator (16-bit wrap = the
        cyclic shift), the carried phase, and the NCO interpolated-table +
        sign-before-amp emit path op-for-op. State resets per call (each call
        is a fresh stream; phase starts at 0)."""
        tbl = self._quarter_table()
        amp = self._s16(self._amp_q15)
        out: List[Tuple[int, int]] = []
        phase = 0
        for s in symbols:
            freq = (((int(s) << self._shift) & 0xFFFF) + 0x8000) & 0xFFFF
            for _ in range(self._n):
                cos = self._channel_q15((phase + 16384) & 0xFFFF, tbl, amp) & 0xFFFF
                sin = self._channel_q15(phase & 0xFFFF, tbl, amp) & 0xFFFF
                out.append((cos, sin))
                phase = (phase + freq) & 0xFFFF
                freq = (freq + self._rate_word) & 0xFFFF
        return out

    def process_reference(self, input_samples) -> np.ndarray:
        """Complex reference (float view of the bit-exact integer model):
        ``input_samples`` is the raw symbol word list."""
        q = self.process_reference_q15(input_samples)
        s16 = self._s16
        return np.asarray([complex(s16(a) / 32768.0, s16(b) / 32768.0)
                           for (a, b) in q], dtype=np.complex64)

    def ideal_chirp(self, symbols) -> np.ndarray:
        """The IDEAL float cyclic-shifted up-chirp with the SAME phase/frequency
        recursion in exact real arithmetic (unit amplitude) — the SNR reference.
        The integer model's frequency words are exact multiples of 1/65536, so
        the ONLY difference vs this ideal is the table-interpolation floor and
        the Q15 amplitude quantization."""
        out = []
        phase = 0.0                      # cycles
        for s in symbols:
            f = (int(s) % self._m) / self._m - 0.5    # cycles/sample
            for _ in range(self._n):
                out.append(complex(math.cos(2 * math.pi * phase),
                                   math.sin(2 * math.pi * phase)))
                phase = (phase + f) % 1.0
                f = f + 1.0 / self._n
                if f >= 0.5:             # the wrap (mod BW): +BW/2 -> -BW/2
                    f -= 1.0
        return np.asarray(out, dtype=np.complex64)

    def reset(self):
        self._phase = 0
