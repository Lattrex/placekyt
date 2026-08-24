# SPDX-License-Identifier: GPL-3.0-or-later
"""ConjChirpMixerBlock — see :class:`ConjChirpMixerBlock`."""
from typing import Dict, List, Tuple

import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import float_to_q15
from .complex_mixer_block import ComplexMixerBlock


class ConjChirpMixerBlock(ComplexMixerBlock):
    """
    CSS dechirp — multiply by the CONJUGATE reference up-chirp.

    (placeKYT-native; no stock GNU Radio streaming counterpart — Python golden.)

    Multiplies the incoming complex stream by the conjugate of the FREE-RUNNING
    ``s = 0`` reference up-chirp (the :class:`ChirpGeneratorBlock` base chirp,
    repeating every ``n`` samples)::

        out[k] = in[k] · conj(c0[k mod n·…])        (phase carried, see below)
        yi = xi·cos θ_k + xq·sin θ_k
        yq = xq·cos θ_k − xi·sin θ_k                (the CONJUGATE product)

    After the dechirp a received symbol chirp (cyclic shift ``s``) becomes a
    CONSTANT tone at frequency word ``s·(65536/m)`` — an N-point FFT then
    concentrates it in one bin, and the winning bin index IS the symbol
    (dechirp → FFT16 → ComplexToMagSquared → BinArgmax → ChirpSync is the CSS
    receive spine).

    THE REFERENCE MATCHES ChirpGeneratorBlock's SCALING BIT-FOR-BIT
    ----------------------------------------------------------------
    The reference chirp phase is the generator's own double accumulator with
    ``s = 0`` fixed, in 16-bit wrap arithmetic::

        phase(0) = 0,  freq(0) = 0x8000               (= −BW/2, the s=0 start)
        each sample:  θ_k = phase;  phase += freq;  freq += rate,
                      rate = 65536/n                  (all mod 2^16)

    ``n·rate = 65536 ≡ 0``, so the frequency word returns to ``0x8000`` every
    ``n`` samples ON ITS OWN — the free-running accumulator IS the repeating
    reference (no symbol counter, no reset; the generator's 16-bit-wraparound-
    is-the-cyclic-shift insight, applied to the s = 0 chirp). The dechirp of a
    :class:`ChirpGeneratorBlock` stream is therefore EXACT in phase-increment
    terms: within a symbol the phase difference advances by ``s·(65536/m)`` per
    sample, a constant — gated bit-exact against the composed integer goldens
    for every symbol.

    ALIGNMENT: the reference is free-running from the FIRST input sample —
    input sample 0 multiplies reference sample 0, and symbol boundaries sit at
    sample indices ≡ 0 (mod n). Timing offset within a symbol is NOT recovered
    here (it shifts the post-FFT peak bin); the documented system-level
    handling is that the demod tracks the LOCKED bin reported by
    :class:`ChirpSyncBlock` as the symbol reference.

    HONEST DECOMPOSITION (what is reused vs built):
      * The NCO front is :class:`ComplexMixerBlock`'s VERIFIED pipeline
        (quarter-wave fold/even/odd/interp table columns, the xi/xq relay, the
        2-column fold, the opt-in INV-20 serialize-LOCK) — inherited unchanged.
      * The ``phase`` cell swaps the mixer's constant ``freq`` DataWord for the
        ChirpGenerator's DOUBLE ACCUMULATOR (``freq`` becomes a state register
        initialized to 0x8000; one extra ADD/MOVE pair applies ``rate``). The
        generator's burst/self-pacing machinery (iternext kick, symbol LOCK,
        sample counter) is NOT needed: this block is 1:1 (each input sample
        triggers exactly one reference sample), so the plain free-running
        accumulator suffices.
      * The product tail is :class:`MultiplyCCBlock`'s verified prods→combine
        pair (4 full-scale MULQs, then the SATURATING V-flag rail combines)
        with the CONJUGATE as the ops swap: ``yi = sat(P1 + P2)``,
        ``yq = sat(P3 − P4)`` with ``P1 = xi·c, P2 = xq·s, P3 = xq·c,
        P4 = xi·s`` (MultiplyCC computes ``sat(P1 − P2)`` / ``sat(P3 + P4)``
        of its own products — one ADD↔SUB swap per rail). No counting join
        (the operands arrive over the block's own internal jump chain, single
        trigger).

    WHY THE TAIL SATURATES (a REAL divergence from ComplexMixerBlock — its
    wrapping combine is WRONG for this block): the dechirped signal is
    unit-magnitude BY DESIGN (chirp × conj-chirp), so its rails constantly
    graze ±1.0; MULQ's floor-truncation pushes each product down ≤1 LSB, and a
    true rail of −1.0 then lands at −32769/−32770 — which a wrapping combine
    folds to +32767, a FULL-SCALE SIGN FLIP. Measured on the s=4/n=16 dechirp:
    the wrapping model turns the exact bin-4 tone into a spectrum with spurs
    at 1/3 of the peak (every 4th sample sign-flipped); the saturating combine
    restores the clean tone. ComplexMixerBlock tolerates the wrap only because
    its stimulus contract keeps |rail| < 1; this block's PRIMARY input is a
    full-scale chirp, so the saturating rails are correctness, not polish.

    Q15 corner notes: the reference chirp's table rails never reach −32768
    (the quarter-wave table is ±32767), so the MULQ (−1)·(−1) wrap corner is
    UNREACHABLE from the reference side — every product is exact-range; only
    the rail COMBINE can overflow, and it is exactly V-recoverable (the AddCC
    minuend-sign restore).

    Parameters (CSS family names, shared with ChirpGeneratorBlock):
      * ``n`` — samples per chirp symbol (power of two, 2..65536; the reference
        chirp-rate word 65536/n must be a nonzero integer). MUST equal the
        transmitter's ``n``.

    ``pipeline_lock`` is a substrate hint (the inherited INV-20 serialize-LOCK
    for saturated drive), not a DSP parameter — excluded from GRC like
    ComplexMixerBlock's.

    Interface: COMPLEX input (xi@R0, xq@R1), COMPLEX output (yi, yq); 1:1,
    delay 0. Precision: the inherited 33-entry interpolated quarter-wave table
    (~11 LSB worst case off-grid; exact-grid phases for n ≤ 128 as in the
    generator).
    """
    CATEGORY = "demodulation"
    TAGS = ["chirp", "css", "dechirp", "conjugate", "mixer", "demodulation"]

    GRC_UNSUPPORTED_PARAMS = ("pipeline_lock",)

    def __init__(self, name: str, n: int = 128, pipeline_lock: bool = False):
        n = int(n)
        if n < 2 or n > 65536 or (n & (n - 1)):
            raise ValueError(
                f"ConjChirpMixerBlock: n must be a power of two in [2, 65536] "
                f"(the 16-bit reference chirp-rate word 65536/n must be a "
                f"nonzero integer, matching ChirpGeneratorBlock); got {n}")
        # Reuse the ComplexMixer plumbing (table pipeline, relay, mixer cell,
        # serialize-LOCK). frequency=0 is unused (the phase cell is replaced);
        # amplitude=1.0 keeps the unit reference table (a CSS reference chirp
        # is unit-amplitude by definition).
        super().__init__(name, sample_rate=1.0, frequency=0.0, amplitude=1.0,
                         offset=0.0, phase=0.0, pipeline_lock=pipeline_lock)
        # Re-record THIS block's real GRC-facing params over the mixer internals
        # (the ChirpGeneratorBlock kwargs convention).
        self._kwargs = {"n": n}
        self._n = n
        self._rate_word = 65536 // n     # reference chirp-rate word (exact)

    # ------------------------------------------------------------- properties
    @property
    def n(self) -> int:
        return self._n

    @property
    def rate_word(self) -> int:
        """The derived per-sample reference chirp-rate word (65536/n)."""
        return self._rate_word

    # ------------------------------------------------------------------ cells
    def build_cell_programs(self) -> Dict[str, CellProgram]:
        # Inherit the verified ComplexMixer cells (fold/even/odd/interp table
        # columns, relay), then REPLACE the phase cell (constant-increment NCO
        # -> free-running double accumulator) and the mixer cell (complex
        # product -> CONJUGATE complex product). Cell ids/ports/order stay the
        # mixer's so the inherited internal connections, jump chain, layout,
        # and lock machinery line up.
        cells = super().build_cell_programs()

        # --- phase cell: the free-running double accumulator -----------------
        # ChirpGenerator's sweep, s = 0 fixed, 1:1 (no burst machinery): emit
        # the CURRENT phase to both table arms + forward xi/xq to the relay,
        # THEN phase += freq, THEN freq += rate (both wrapping — the free
        # 16-bit wrap returns freq to 0x8000 every n samples). Data words pack
        # contiguously at 4..5 (+ the lock words at 6..7, the inherited INV-20
        # addresses); state PINNED at 8..11 (INV-33; the locked build's
        # auto-gap starts exactly there).
        ph_lock_data = ([DataWord("lock_face", 1, address=6, is_face=True),
                         DataWord("one", 1, address=7)]
                        if self._pipeline_lock else [])
        ph_lock_tail = ("""\
    MOVE R0, R{data:lock_face}
    MOVE [LOCK_FACE], R0
    MOVE R0, R{data:one}
    MOVE [LOCK], R0
""" if self._pipeline_lock else "")
        cells["phase"] = CellProgram(
            inputs=[Port("xi", register=0), Port("xq", register=1)],
            outputs=[Port("ph_sin"), Port("ph_cos"), Port("xi_fwd"),
                     Port("xq_fwd"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("quarter", 16384, address=4),
                  DataWord("rate", self._rate_word, address=5)] + ph_lock_data,
            state=[StateVar("phase", register=8, initial_value=0),
                   StateVar("freq", register=9, initial_value=0x8000),
                   StateVar("xis", register=10),
                   StateVar("xqs", register=11)],
            assembly_template="""\
start:
    MOVE R{state:xis}, R{in:xi}
    MOVE R{state:xqs}, R{in:xq}
    MOVE R0, R{state:phase}
    {write:ph_sin}
    ADD R{state:phase}, R{data:quarter}
    {write:ph_cos}
    MOVE R0, R{state:xis}
    {write:xi_fwd}
    MOVE R0, R{state:xqs}
    {write:xq_fwd}
    ADD R{state:phase}, R{state:freq}
    MOVE R{state:phase}, R0
    ADD R{state:freq}, R{data:rate}
    MOVE R{state:freq}, R0
    {jump:trig}
""" + ph_lock_tail,
        )

        # --- prods cell: the four full-scale products ------------------------
        # Replaces ComplexMixer's fused mixer cell. MultiplyCCBlock's prods
        # pattern WITHOUT the counting join (the four operands arrive over the
        # block's own internal chain; cos_interp's trig is the single fire).
        # Each input register is read EXACTLY ONCE (the stale-latch trap);
        # MULQ does not clobber its state operands, so the four snapshots feed
        # all four products. NO data words -> every state PINNED (INV-33
        # no-data-words corollary: the auto-scan would land state on R0).
        del cells["mixer"]
        cells["prods"] = CellProgram(
            inputs=[Port("cosv", register=0), Port("sinv", register=1),
                    Port("xi", register=2), Port("xq", register=3)],
            outputs=[Port("p1"), Port("p2"), Port("p3"), Port("p4"),
                     Port("trig")],
            entries=[EntryPoint("default")],
            data=[],
            state=[StateVar("c", register=4), StateVar("s", register=5),
                   StateVar("xi2", register=6), StateVar("xq2", register=7)],
            assembly_template="""\
start:
    MOVE R{state:c}, R{in:cosv}
    MOVE R{state:s}, R{in:sinv}
    MOVE R{state:xi2}, R{in:xi}
    MOVE R{state:xq2}, R{in:xq}
    MULQ R{state:xi2}, R{state:c}
    {write:p1}
    MULQ R{state:xq2}, R{state:s}
    {write:p2}
    MULQ R{state:xq2}, R{state:c}
    {write:p3}
    MULQ R{state:xi2}, R{state:s}
    {write:p4}
    {jump:trig}
""",
        )

        # --- combine cell: the CONJUGATE saturating rails + emit -------------
        # MultiplyCCBlock's combine cell with the conjugate as the ops swap:
        # yi = sat(P1 + P2), yq = sat(P3 - P4). The AddCC V-flag minuend-sign
        # restore per rail (SUB overflow -> sign(minuend); ADD overflow ->
        # operands share sign -> sign(P1)); conditional branches ONLY (exit
        # cell — a GOTO would be rewritten by the output-handoff pass). The
        # serialize-LOCK release (INV-20) is the ComplexMixer dual-FACE idiom:
        # after yi/yq, flip FACE to the unlock corridor (combine sits at (1,0),
        # DIRECTLY EAST of phase at (0,0), so unlock_face=WEST(2) and the
        # authored hop is the @1 abutment — no transit cell), clear phase's
        # arbiter LOCK with a backward WRITE.CFG, flip back to face_tap so the
        # trailing trig rides the routed output face. R0=0 for the CFG payload
        # comes from XOR satpos,satpos (no dedicated zero word).
        lock_face_data = ([DataWord("face_tap", 1, address=5, is_face=True),
                           DataWord("unlock_face", 2, address=6, is_face=True)]
                          if self._pipeline_lock else [])
        lock_release_tail = ("""\
    MOVE [FACE], R{data:unlock_face}
    XOR R{data:satpos}, R{data:satpos}
    WRITE.CFG @1, 4
    MOVE [FACE], R{data:face_tap}
""" if self._pipeline_lock else "")
        cells["combine"] = CellProgram(
            inputs=[Port("p1", register=0), Port("p2", register=1),
                    Port("p3", register=2), Port("p4", register=3)],
            outputs=[Port("yi"), Port("yq"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("satpos", 0x7FFF, address=4)] + lock_face_data,
            state=[StateVar("p1s"), StateVar("p3s"), StateVar("yqs")],
            assembly_template="""\
start:
    MOVE R{state:p1s}, R0
    MOVE R{state:p3s}, R{in:p3}
    SUB R{state:p3s}, R{in:p4}
    BR.NV +3
    MOVE R0, R{state:p3s}
    SHR R0, #15
    ADD R0, R{data:satpos}
    MOVE R{state:yqs}, R0
    ADD R{state:p1s}, R{in:p2}
    BR.NV +3
    MOVE R0, R{state:p1s}
    SHR R0, #15
    ADD R0, R{data:satpos}
    {write:yi}
    MOVE R0, R{state:yqs}
    {write:yq}
""" + lock_release_tail + """\
    {jump:trig}
""",
        )
        return cells

    # ------------------------------------------------------- multi-cell wiring
    @property
    def cell_count(self) -> int:
        # 13 cells, locked OR unlocked: the ComplexMixer 2x6 fold with the
        # fused mixer replaced by prods(1,1) + combine(1,0); the unlock
        # corridor is the combine->phase @1 west abutment, so no transit cell.
        return 13

    def internal_connections(self) -> List[Tuple[str, str, str, str]]:
        conns = [
            ("phase", "ph_sin", "sin_fold", "phase"),
            ("phase", "ph_cos", "cos_fold", "phase"),
            ("phase", "xi_fwd", "relay", "xi"),
            ("phase", "xq_fwd", "relay", "xq"),
            ("relay", "xi_fwd", "prods", "xi"),
            ("relay", "xq_fwd", "prods", "xq"),
        ]
        for ch in ("sin", "cos"):
            conns += [
                (f"{ch}_fold", "idx_e", f"{ch}_even", "idx"),
                (f"{ch}_fold", "idx_o", f"{ch}_odd", "idx"),
                (f"{ch}_fold", "frac", f"{ch}_interp", "frac"),
                (f"{ch}_fold", "neg", f"{ch}_interp", "neg"),
                (f"{ch}_even", "eval", f"{ch}_interp", "eval"),
                (f"{ch}_even", "par", f"{ch}_interp", "par"),
                (f"{ch}_odd", "oval", f"{ch}_interp", "oval"),
            ]
        conns += [
            ("sin_interp", "val", "prods", "sinv"),
            ("cos_interp", "val", "prods", "cosv"),
            ("prods", "p1", "combine", "p1"),
            ("prods", "p2", "combine", "p2"),
            ("prods", "p3", "combine", "p3"),
            ("prods", "p4", "combine", "p4"),
        ]
        if self._pipeline_lock:
            # combine's BACKWARD config-only edge to phase (the serialize-LOCK
            # release), resolved by _apply_internal_feedback's config_only
            # branch over the @1 west abutment.
            conns += [("combine", "unlock", "phase", "xi")]
        return conns

    def internal_jumps(self) -> List[Tuple[str, str, str, str]]:
        chain = ["phase", "sin_fold", "sin_even", "sin_odd", "sin_interp",
                 "relay", "cos_fold", "cos_even", "cos_odd", "cos_interp",
                 "prods", "combine"]
        jumps = [(chain[i], "trig", chain[i + 1], "default")
                 for i in range(len(chain) - 1)]
        if self._pipeline_lock:
            # combine is the block's OUTPUT + LAST cell; its trig SELF-
            # TERMINATES (the ComplexMixer/iq_upconvert idiom) so the exit-
            # defaulting never routes the trig down the unlock abutment.
            jumps.append(("combine", "trig", "__terminate__", "default"))
        return jumps

    def output_cell_ids(self) -> List[str]:
        return ["combine"]

    def output_cell_id(self):
        # combine IS the last cell in dict order (locked and unlocked), so the
        # default last-cell exit is already correct.
        return None

    def default_layout(self):
        # The ComplexMixer 2-column serpentine with the product tail stacked
        # at the TOP of column 1: prods at (1,1), combine at (1,0). I/O
        # CO-LOCATED on the top edge (phase input at (0,0), combine output at
        # (1,0) — INV-8/14), and the locked unlock corridor is the direct
        # combine->phase west abutment (no transit cell).
        col0 = ["phase", "sin_fold", "sin_even", "sin_odd", "sin_interp",
                "relay"]
        col1_bottom_up = ["cos_fold", "cos_even", "cos_odd", "cos_interp",
                          "prods", "combine"]
        layout = {}
        for j, cid in enumerate(col0):
            face = "east" if cid == "relay" else "south"
            layout[cid] = (0, j, face)
        for k, cid in enumerate(col1_bottom_up):
            face = "east" if cid == "combine" else "north"
            layout[cid] = (1, 5 - k, face)
        return layout

    # -------------------------------------------------------------- reference
    def reference_phase_words(self, count: int) -> List[Tuple[int, int]]:
        """The reference accumulator trajectory: ``count`` (phase, freq) word
        pairs BEFORE each sample's increment — the s=0 ChirpGenerator sweep."""
        out = []
        phase, freq = 0, 0x8000
        for _ in range(count):
            out.append((phase, freq))
            phase = (phase + freq) & 0xFFFF
            freq = (freq + self._rate_word) & 0xFFFF
        return out

    @staticmethod
    def _sat_combine(p_min: int, p_other: int, sign: int) -> int:
        """One saturating rail exactly as the combine cell computes it:
        ``sat(p_min (+|-) p_other)``; on 16-bit V-overflow, pin to the rail of
        the MINUEND's sign (``0x7FFF + signbit``) — the MultiplyCC/AddCC
        idiom. Operands are signed ints; returns an unsigned 16-bit word."""
        r = p_min + sign * p_other
        if r > 32767 or r < -32768:
            return (0x7FFF + (1 if p_min < 0 else 0)) & 0xFFFF
        return r & 0xFFFF

    def process_reference_q15(self, input_iq) -> List[Tuple[int, int]]:
        """Bit-exact predictor: out = in · conj(ref chirp) via the on-chip
        interpolated cos/sin (the inherited quarter-wave table), the four
        truncating MULQ products, and the SATURATING conjugate rail combines.
        State resets per call (fresh stream: phase 0, freq 0x8000)."""
        tbl = self._quarter_table()
        s16 = self._s16
        arr = np.asarray(input_iq)
        if np.iscomplexobj(arr):
            xs = [(float_to_q15(c.real), float_to_q15(c.imag)) for c in arr]
        elif arr.ndim == 2 and arr.shape[1] == 2:
            xs = [(int(x) & 0xFFFF, int(y) & 0xFFFF) for x, y in arr]
        else:
            xs = [(float_to_q15(float(x)), 0) for x in arr]
        out = []
        phase, freq = 0, 0x8000
        for (xi, xq) in xs:
            cos = self._signed_sine_q15((phase + 16384) & 0xFFFF, tbl)
            sin = self._signed_sine_q15(phase, tbl)
            xi_s, xq_s = s16(xi), s16(xq)
            p1 = (xi_s * cos) >> 15      # truncating MULQ (floor)
            p2 = (xq_s * sin) >> 15
            p3 = (xq_s * cos) >> 15
            p4 = (xi_s * sin) >> 15
            yi = self._sat_combine(p1, p2, +1)   # yi = sat(P1 + P2)
            yq = self._sat_combine(p3, p4, -1)   # yq = sat(P3 - P4)
            out.append((yi, yq))
            phase = (phase + freq) & 0xFFFF
            freq = (freq + self._rate_word) & 0xFFFF
        return out

    def process_reference(self, input_samples) -> np.ndarray:
        ref = self.process_reference_q15(input_samples)
        return np.array([complex(self._s16(yi) / 32768.0,
                                 self._s16(yq) / 32768.0)
                         for yi, yq in ref], dtype=np.complex64)

    def reset(self):
        self._phase = 0
