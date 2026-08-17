# SPDX-License-Identifier: GPL-3.0-or-later
"""CWKeyerBlock — SRAM-backed Morse / CW keyer (see :class:`CWKeyerBlock`)."""
from typing import Dict, List, Tuple

import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock, float_to_q15, q15_to_float


# --- International Morse code, transcribed EXACTLY from ITU-R M.1677-1 --------
# (Recommendation ITU-R M.1677-1, 10/2009, Annex 1, Part I).
#   §1.1.1 Letters, §1.1.2 Figures, §1.1.3 Punctuation.
# Dot = '.', dash = '-'. Verified letter-by-letter against the source PDF
# (A .-, E ., Q --.-, etc.; digits 1 .---- .. 0 -----; period .-.-.-).
MORSE_ITU: Dict[str, str] = {
    # 1.1.1 Letters
    "A": ".-",   "B": "-...", "C": "-.-.", "D": "-..",  "E": ".",
    "F": "..-.", "G": "--.",  "H": "....", "I": "..",   "J": ".---",
    "K": "-.-",  "L": ".-..", "M": "--",   "N": "-.",   "O": "---",
    "P": ".--.", "Q": "--.-", "R": ".-.",  "S": "...",  "T": "-",
    "U": "..-",  "V": "...-", "W": ".--",  "X": "-..-", "Y": "-.--",
    "Z": "--..",
    # 1.1.2 Figures
    "1": ".----", "2": "..---", "3": "...--", "4": "....-", "5": ".....",
    "6": "-....", "7": "--...", "8": "---..", "9": "----.", "0": "-----",
    # 1.1.3 Punctuation marks and miscellaneous signs
    ".": ".-.-.-", ",": "--..--", ":": "---...", "?": "..--..",
    "'": ".----.", "-": "-....-", "/": "-..-.",  "(": "-.--.",
    ")": "-.--.-", "\"": ".-..-.", "=": "-...-",  "+": ".-.-.",
    "@": ".--.-.",
}


def morse_codeword(pattern: str) -> int:
    """Pack a Morse dot/dash pattern into ONE 16-bit codeword.

    Layout ``(count << 8) | (elems << (8 - count))``:
      * high byte  = element count (1..6),
      * low byte   = the elements LEFT-JUSTIFIED at bit 7 (MSB = first element),
        each bit ``1`` = dash, ``0`` = dot.
    So a reader takes the count, then walks the elements MSB-first by testing
    bit 7 and shifting left. Max element count is 6 (punctuation), so the
    left-justified elements occupy bits 7..2 — one 16-bit word per character.

    This is the packed Morse-table word that lives in the SRAM PANEL (address ==
    ASCII code point). The build-time run expander (:func:`run_records`) walks it
    to produce the per-character keying runs; the panel returns ONE fixed word
    per character, mirroring the SRAM-backed VaricodeEncoder fixed-word
    packing.
    """
    count = len(pattern)
    if not (1 <= count <= 6):
        raise ValueError(f"Morse pattern length out of range: {pattern!r}")
    elems = 0
    for ch in pattern:
        elems = (elems << 1) | (1 if ch == "-" else 0)
    left = elems << (8 - count)          # MSB (bit7) = first element
    return (count << 8) | (left & 0xFF)


def morse_sram_table(charset: List[str]) -> List[int]:
    """The packed Morse SRAM panel image, address == ASCII code point (0..127).

    Slot ``i`` holds ``morse_codeword(MORSE_ITU[chr(i)])`` for every character in
    ``charset`` (uppercased), 0 elsewhere (0 also marks the word-space / NUL,
    which the keyer treats specially). One 16-bit word per character; stream it
    into the panel via the ``SramControllerBlock`` write path exactly as the
    VaricodeEncoder streams its ``sram_table()``.
    """
    img = [0] * 128
    for c in charset:
        cu = c.upper()
        img[ord(cu)] = morse_codeword(MORSE_ITU[cu])
    return img


# --- The keying-run record model (the SRAM-backed emit format) ------------------
#
# The single-cell timing state machine (element walk + dot/dash/gap + per-sample
# raised-cosine edge) did NOT fit a 32-word cell (the former INV-7 quarantine).
# The SRAM-backed resolution splits it EXACTLY like VaricodeEncoder: the variable,
# message-dependent part (the Morse-table-derived keying schedule) lives OFF-CELL
# in the SRAM panel as a stream of RUN RECORDS; the on-chip cell is a tiny,
# fixed "run player" that streams the envelope, driven by the panel push-read.
#
# A RUN RECORD is three 16-bit words ``(base, step, count)``:
#   * ``count``  samples to emit,
#   * each sample = ``LUT[cur]`` where ``cur`` starts at ``base`` and advances by
#     ``step`` (a signed value in {-1, 0, +1}) per sample.
# The in-cell LUT is a small FIXED table baked from the Hann edge:
#   * LUT[0]        = 0            (OFF / key-up level),
#   * LUT[1]        = full ON (Q15 ~1.0),
#   * LUT[2..2+e-1] = the raised-cosine RISE weights (Hann), e = edge_samples.
# So the four run kinds are ONE unified loop:
#   * OFF  run: base=0,       step= 0  -> count zeros,
#   * FLAT run: base=1,       step= 0  -> count full-ON samples,
#   * RISE run: base=2,       step=+1  -> LUT[2..2+e-1] (key-down edge),
#   * FALL run: base=2+e-1,   step=-1  -> LUT[2+e-1..2] = rise reversed (key-up).
# An ON element (dot=1 / dash=3 dot units) becomes RISE + FLAT + FALL; each gap is
# an OFF run. This is BIT-EXACT (Q15) to :meth:`CWKeyerBlock.key_envelope_q15`
# (the ITU-R golden) — verified in verification/tests/test_cw_keyer_sram.py.
#
# EDGE / CLICK-SUPPRESSION CHOICE (documented): the raised-cosine (Hann) edge is a
# SMALL in-cell LUT baked from the closed-form Hann formula, NOT generated on the
# fly (a cosine recurrence drifts — measured 12.5 Q15 LSB at edge=32 because the
# 2·cos(w) coefficient exceeds Q15 range and the error compounds). The LUT is
# ``2 + edge_samples`` words and co-fits the ~15-instruction player for edges up
# to MAX_ONCHIP_EDGE. The Morse TABLE itself is off-cell in the panel (the packed
# codewords are :func:`morse_sram_table`; the panel-resident run stream is the
# table walked by :func:`run_records`).

RUN_OFF = 0
RUN_FLAT = 1
RUN_RISE = 2
RUN_FALL = 3


class CWKeyerBlock(KyttarBlock):
    r"""Morse / CW keyer — ASCII characters -> an ON/OFF keying envelope.

    NO stock GNU Radio counterpart. Verified against a Python GOLDEN of the
    **International Morse code (ITU-R M.1677-1)** + standard CW timing (§2 of the
    same Recommendation).

    Behaviour (one input word = one ASCII character code):
      * Each character maps to a sequence of dots and dashes (the ITU-R table).
      * Timing, in **dot units** (ITU-R M.1677-1 §2): dot = 1 (§2, baseline),
        **dash = 3** (§2.1), intra-character (between signals of one letter) gap =
        1 (§2.2), inter-character (between letters) gap = 3 (§2.3), inter-word gap
        = 7 (§2.4).
      * ``wpm`` sets the dot duration via the **PARIS standard**
        ``dot_ms = 1200 / wpm`` (the word "PARIS" = 50 dot units; at ``wpm`` words
        per minute one dot lasts ``60000 ms / (50 * wpm) = 1200/wpm`` ms).
      * The envelope is emitted at ``samples_per_dot`` samples per dot unit: a dot
        is ``samples_per_dot`` ON samples, a dash ``3*samples_per_dot``, each gap
        the corresponding multiple of ``samples_per_dot`` OFF samples.
      * **Key-click suppression:** each key-DOWN and key-UP transition is shaped
        with a RAISED-COSINE (Hann) edge of ``edge_samples`` samples — the ON level
        rises ``0.5*(1 - cos(pi*k/(e+1)))`` and falls with the mirror — so the
        envelope has no hard step (which would splatter as key clicks).

    A special input value ``0x0000`` (ASCII NUL) is treated as a WORD SPACE and
    emits the inter-word gap (7 dot units OFF); it is the keyer's space character.

    SRAM-backed construction (INV-31, the recipe; SRAM_PANEL.md §6)
    ==============================================================
    This block was PREVIOUSLY QUARANTINED (INV-7 register wall): the full keyer
    (Morse LOAD table + dot/dash/gap timing state machine + raised-cosine edge)
    does not fit a 32-word cell — the FSM alone assembles to ~50 instructions
    before ANY table or edge LUT. It is now **SRAM-backed**, split exactly like the
    proven SRAM-backed :class:`~gr_kyttar.placement.blocks.varicode_encoder_block.VaricodeEncoderBlock`:

    * **Table + timing schedule -> SRAM panel.** The packed Morse table lives in
      the panel (:func:`morse_sram_table`, address == ASCII code point). The
      message-dependent keying schedule — the timing FSM's output — is a stream of
      RUN RECORDS ``(base, step, count)`` (:func:`run_records`), computed once at
      build time from the verified golden and streamed into the panel. The
      unbounded, variable-length part lives OFF-CELL where the panel is unbounded
      (INV-29); the timing FSM never has to run on a cell.

    * **Per-sample emit -> a tiny unified run player.** The on-chip cell is a
      fixed ~15-instruction loop that reads one run record via the panel push-read
      and emits ``count`` samples ``LUT[cur]`` (``cur += step``). The raised-cosine
      edge is a SMALL in-cell Hann LUT (``2 + edge_samples`` words) baked from the
      closed-form formula (see the module note on the edge choice). The player
      resolves into ONE 32-word cell for ``edge_samples <= MAX_ONCHIP_EDGE``.

    Load phase (once): ``set_addr`` the controller to base 0, then stream the run
    records for the message through the controller's ``write`` entry (the
    controller auto-increments). Lookup phase (per run): the panel PUSH-READs the
    three run words into the player cell's ``base``/``step``/``count`` registers +
    kicks its ``play`` entry; the player streams the samples out. Proven BIT-EXACT
    (Q15) vs the ITU-R golden through REAL SramPanelDevice/PanelDriver + real
    simkyt routing in ``verification/tests/test_cw_keyer_sram.py``.

    HARDWARE DEVIATION — on-chip edge length (INV-0, register budget)
    ================================================================
    The Hann edge LUT is in-cell (``2 + edge_samples`` words) so the player can
    read it with a single LOAD per sample. It co-fits the loop for
    ``edge_samples <= MAX_ONCHIP_EDGE``; a larger on-chip edge would need the LUT
    itself moved into the panel (delivered at load), which is a straightforward
    extension of the same push-read path but not built here. The PYTHON GOLDEN
    (:meth:`key_envelope`) is unbounded — it spans the full ITU-R table and any
    edge length; only the on-chip DUT carries the ``MAX_ONCHIP_EDGE`` cap and
    RAISES above it (never silently truncates).

    Params (mirror the CW-keyer knobs a user sets):
      * ``wpm`` (default 20)              — words per minute (PARIS).
      * ``samples_per_dot`` (default 4800) — output samples per dot unit.
      * ``edge_samples`` (default 8)       — raised-cosine rise/fall length
        (on-chip: <= ``MAX_ONCHIP_EDGE``).
      * ``charset`` (optional)             — the panel Morse subset (default
        A-Z0-9 + common punctuation).

    Interface: ASCII code in @R{input}; Q15 envelope sample(s) out @R{output}
    (one input -> many output samples), driven by the SRAM controller +
    per-run panel push-read. Two cells (player + SRAM controller).
    """
    CATEGORY = "modulation"
    TAGS = ["cw", "morse", "keyer", "ook", "modulation", "sram"]

    # The in-cell Hann edge LUT is (2 + edge) words and must co-fit the player's
    # loop + END-record test + completion kick in one 32-word cell (v2). The
    # on-chip build caps here; the Python golden is uncapped.
    # v2 measured budget: the player (END-record test + completion kick) co-fits
    # its Hann LUT in one 32-word cell up to edge_samples=4 (28/32 words used);
    # edge=5 overflows the register allocator. The Python golden stays unbounded.
    MAX_ONCHIP_EDGE = 4

    # v2 STANDALONE transmitter: the block authors its own output WRITE/JUMP hops
    # (the player's per-sample emit + the completion kick target the crossover's
    # track_b / track_c with placement-derived @N hops — like the SramController
    # and Crossover, the build must NOT re-patch them to a single target).
    RAW_OUTPUT_HOPS = True

    # SRAM ROM region stride: character c's run records live at panel address
    # c * ROM_STRIDE (the fetch cell computes the base with one SHL #7). 128
    # words = 41 records/char, comfortably above the longest ITU code (~30
    # records incl. edges + gaps). The panel array is sparse and unwritten words
    # read 0, so every region is implicitly terminated by a (0,0,0) END record —
    # and an unmapped code point's empty region plays silence (no kick, chain
    # idles awaiting the next character).
    ROM_STRIDE = 128
    ROM_SHIFT = 7

    # Default panel Morse subset: the full ITU order (letters, figures, common
    # punctuation). The panel is unbounded (INV-31), so — unlike the old
    # single-cell LOAD-table wall — there is NO register-budget truncation here.
    _DEFAULT_CHARSET = list(MORSE_ITU.keys())

    _interface = BlockInterface(entry_address=1, input_registers=[0],
                                output_registers=[0])

    # SRAM panel base for the packed Morse table (address == ASCII code point).
    TABLE_BASE = 0
    TABLE_WORDS = 128

    def __init__(self, name: str, wpm: int = 20, samples_per_dot: int = 4800,
                 edge_samples: int = 4, charset: str | None = None,
                 panel_hop: int = 1, emit_hop: int = 1, emit_dest: int = 0,
                 emit_entry: int = 1, done_entry: int = 0,
                 read_wr_desc: int = 0, read_jp_desc: int = 0):
        if int(wpm) <= 0:
            raise ValueError(f"wpm must be > 0, got {wpm}")
        spd = int(samples_per_dot)
        if spd < 1:
            raise ValueError(f"samples_per_dot must be >= 1, got {samples_per_dot}")
        if 3 * spd >= 32768:
            # dash = 3*samples_per_dot must fit a signed 16-bit down-counter.
            raise ValueError(
                f"3*samples_per_dot={3*spd} exceeds the 16-bit counter range "
                f"(32767); reduce samples_per_dot")
        edge = int(edge_samples)
        if edge < 0 or edge > spd:
            raise ValueError(
                f"edge_samples must be in [0, samples_per_dot={spd}], got {edge}")
        if edge > self.MAX_ONCHIP_EDGE:
            raise ValueError(
                f"edge_samples={edge} exceeds MAX_ONCHIP_EDGE={self.MAX_ONCHIP_EDGE} "
                f"(the in-cell Hann LUT + player must co-fit one 32-word cell, "
                f"INV-7). Reduce edge_samples, or move the edge LUT into the panel "
                f"(an extension of the same push-read path). The Python golden is "
                f"unbounded and works for any edge.")
        order = list(charset) if charset else list(self._DEFAULT_CHARSET)
        for c in order:
            cu = c.upper()
            if cu not in MORSE_ITU:
                raise ValueError(f"character {c!r} has no ITU-R M.1677 Morse code")

        super().__init__(name, wpm=int(wpm), samples_per_dot=spd,
                         edge_samples=edge, charset="".join(order),
                         panel_hop=panel_hop, emit_hop=emit_hop,
                         emit_dest=int(emit_dest), emit_entry=emit_entry,
                         done_entry=int(done_entry),
                         read_wr_desc=int(read_wr_desc),
                         read_jp_desc=int(read_jp_desc))
        self._wpm = int(wpm)
        self._spd = spd
        self._edge = edge
        self._charset = [c.upper() for c in order]
        self._panel_hop = panel_hop
        # The player's per-sample emission target (RAW hops): each envelope
        # sample is WRITE @emit_hop -> emit_dest + JUMP @emit_hop -> emit_entry
        # (the crossover's track_b relay in the placed design), and the RECORD
        # COMPLETION kick is JUMP @emit_hop -> done_entry (track_c, relayed to
        # the fetch cell's 'next' entry) — the flow control that makes record
        # sequencing self-paced instead of timing-dependent. All PLACEMENT-
        # DERIVED (set by the panel template).
        self._emit_hop = emit_hop
        self._emit_dest = int(emit_dest) & 0x1F
        self._emit_entry = emit_entry
        self._done_entry = int(done_entry) & 0x1F
        # Push-read delivery descriptors for the record fetcher (cell 0):
        # read_wr_desc delivers the record's FIRST word to the player's ``base``
        # register (step/cnt follow at base+1/base+2 — fetch adds 1 per word);
        # read_jp_desc kicks the player's ``play`` entry after the third word.
        # PLACEMENT-DERIVED (set by the panel template from the routed return
        # corridor), like the Varicode encoder's.
        self._read_wr_desc = int(read_wr_desc) & 0xFFFF
        self._read_jp_desc = int(read_jp_desc) & 0xFFFF

    # ------------------------------------------------------------------ props
    @property
    def cell_count(self) -> int:
        # Two cells: the run player + the SRAM controller (panel sequencing).
        return 2

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    @property
    def wpm(self) -> int:
        return self._wpm

    @property
    def samples_per_dot(self) -> int:
        return self._spd

    @property
    def edge_samples(self) -> int:
        return self._edge

    @property
    def dot_ms(self) -> float:
        """PARIS-standard dot duration in ms: 1200/wpm."""
        return 1200.0 / self._wpm

    @property
    def charset(self) -> List[str]:
        return list(self._charset)

    def codeword_table(self) -> List[int]:
        """The packed Morse codewords, one per subset character (slot order)."""
        return [morse_codeword(MORSE_ITU[c]) for c in self._charset]

    def sram_image(self) -> List[int]:
        """The 128-word packed Morse SRAM panel image (address == code point)."""
        return morse_sram_table(self._charset)

    # ------------------------------------------------------------- edge shape
    def _edge_rise(self) -> np.ndarray:
        """Raised-cosine (Hann) RISE weights, one per edge sample (Q15 floats)."""
        e = self._edge
        if e == 0:
            return np.array([], dtype=np.float64)
        # k = 1..e -> 0.5*(1 - cos(pi*k/(e+1))) rises 0->~1 across the edge.
        k = np.arange(1, e + 1)
        return 0.5 * (1.0 - np.cos(np.pi * k / (e + 1)))

    def edge_lut_q15(self) -> List[int]:
        """The in-cell Hann envelope LUT (Q15 uint16), the emit table:

            LUT[0]        = 0            (OFF / key-up level)
            LUT[1]        = full ON      (Q15 ~1.0)
            LUT[2..2+e-1] = raised-cosine RISE weights (e = edge_samples)

        A RISE run walks LUT[2..2+e-1] (step +1); a FALL run walks it in reverse
        (step -1) — the Hann fall is the rise mirror. OFF/FLAT runs point at
        LUT[0]/LUT[1] with step 0.
        """
        lut = [0, float_to_q15(1.0)]
        lut.extend(float_to_q15(v) for v in self._edge_rise())
        return [w & 0xFFFF for w in lut]

    # ------------------------------------------------------- run-record model
    def run_records(self, chars) -> List[Tuple[int, int, int]]:
        """Build-time expansion: ASCII chars -> the keying RUN records.

        Each record ``(base, step, count)`` drives the on-chip unified player
        (:meth:`build_cell_programs`) to emit ``count`` samples ``LUT[cur]`` with
        ``cur`` starting at ``base`` and advancing by ``step`` per sample. The
        record stream is the timing FSM's output, computed here (off-cell) from
        the verified golden and streamed into the SRAM panel. Emitting the
        records against :meth:`edge_lut_q15` is BIT-EXACT to
        :meth:`key_envelope_q15` (the ITU-R golden).

        Layout follows :meth:`key_envelope`: per character, each element is an ON
        run (RISE + FLAT + FALL) with dot=1 / dash=3 dot units; an intra-character
        gap (1 unit OFF) separates elements; an inter-character gap (3 units OFF)
        ends the character. NUL (0) emits the inter-word gap (7 units OFF).
        """
        spd = self._spd
        e = self._edge
        recs: List[Tuple[int, int, int]] = []

        def key_on(units: int):
            n = units * spd
            if e == 0:
                recs.append((RUN_FLAT, 0, n))
                return
            recs.append((RUN_RISE, +1, e))            # LUT[2..2+e-1]
            mid = n - 2 * e
            if mid > 0:
                recs.append((RUN_FLAT, 0, mid))
            recs.append((2 + e - 1, -1, e))           # LUT[2+e-1..2] (fall)

        def key_off(units: int):
            recs.append((RUN_OFF, 0, units * spd))

        for ch in chars:
            code = int(ch) & 0xFFFF
            if code == 0:
                key_off(7)          # word space -> inter-word gap (§2.4)
                continue
            c = chr(code).upper()
            if c not in MORSE_ITU:
                raise ValueError(f"character {c!r} has no ITU-R M.1677 Morse code")
            pattern = MORSE_ITU[c]
            for i, el in enumerate(pattern):
                key_on(3 if el == "-" else 1)          # dash=3 (§2.1), dot=1
                if i < len(pattern) - 1:
                    key_off(1)                         # intra-character gap (§2.2)
            key_off(3)                                 # inter-character gap (§2.3)
        return recs

    def emit_from_records(self, recs, lut=None) -> List[int]:
        """The SRAM-backed emit model: play the run records against the LUT.

        Mirrors the on-chip player EXACTLY (``count`` samples, ``LUT[base + step*i]``
        per record). BIT-EXACT to :meth:`key_envelope_q15` when ``recs`` come from
        :meth:`run_records` and ``lut`` from :meth:`edge_lut_q15`.
        """
        if lut is None:
            lut = self.edge_lut_q15()
        out: List[int] = []
        for base, step, cnt in recs:
            cur = base
            for _ in range(int(cnt)):
                out.append(lut[cur] & 0xFFFF)
                cur += step
        return out

    def run_records_flat(self, chars) -> List[int]:
        """The run records flattened to the panel load stream (3 words / record:
        base, step&0xFFFF, count) — stream this into the panel via the controller
        ``write`` path (auto-increment)."""
        flat: List[int] = []
        for base, step, cnt in self.run_records(chars):
            flat.extend((base & 0xFFFF, step & 0xFFFF, cnt & 0xFFFF))
        return flat

    # ------------------------------------------------- v2 message-independent ROM
    def char_records(self, code_point: int) -> List[Tuple[int, int, int]]:
        """ONE character's run records (the per-region unit of the v2 ROM).

        Identical timing to :meth:`run_records` for that character (a Morse char's
        elements + its trailing 3-unit inter-character gap; NUL and SPACE both
        key the 7-unit inter-word gap), so playing regions in message order
        concatenates to EXACTLY the message-level record stream — the golden
        (:meth:`key_envelope_q15`) is unchanged."""
        return self.run_records([int(code_point)])

    def rom_image(self) -> Dict[int, int]:
        """The message-INDEPENDENT panel ROM: every charset character's records
        at panel address ``code_point * ROM_STRIDE`` (+ SPACE at 0x20 and NUL at
        0, both the inter-word gap). Unwritten words read 0 on the sparse panel,
        so each region is implicitly terminated by a (0,0,0) END record and an
        unmapped code point's empty region plays silence. This is what makes the
        keyer a STANDALONE transmitter: the input stream is ASCII at runtime; no
        rebuild per message."""
        image: Dict[int, int] = {}
        code_points = {0, 0x20}
        for c in self._charset:
            code_points.add(ord(c))
            code_points.add(ord(c.lower()) if c.isalpha() else ord(c))
        for cp in sorted(code_points):
            recs = self.char_records(cp if cp not in (0, 0x20) else 0)
            if 3 * len(recs) > self.ROM_STRIDE - 3:
                raise ValueError(
                    f"character {cp:#x} needs {len(recs)} records "
                    f"({3 * len(recs)} words) — exceeds the ROM region "
                    f"({self.ROM_STRIDE} words incl. the END record)")
            base = cp * self.ROM_STRIDE
            for i, (b, s, n) in enumerate(recs):
                image[base + 3 * i + 0] = b & 0xFFFF
                image[base + 3 * i + 1] = s & 0xFFFF
                image[base + 3 * i + 2] = n & 0xFFFF
        return image

    # ------------------------------------------------------------- reference
    def key_envelope(self, chars) -> np.ndarray:
        """Python GOLDEN: ASCII chars -> the ON/OFF keying envelope (float).

        This is the spec-defined reference the DUT is gated against. It emits, per
        character: for each Morse element an ON run (dot=1 / dash=3 dot units) with
        raised-cosine shaped key-down/up edges, an intra-character gap (1 dot unit
        OFF) between elements, and the inter-character gap so the total inter-letter
        silence is 3 dot units (§2.3). ``0`` (NUL) emits the inter-word gap
        (7 dot units OFF, §2.4).
        """
        spd = self._spd
        rise = self._edge_rise()
        fall = rise[::-1]
        env: List[float] = []

        def key_on(units: int):
            n = units * spd
            seg = np.ones(n, dtype=np.float64)
            e = len(rise)
            if e:
                seg[:e] = rise
                seg[n - e:] = fall
            env.extend(seg.tolist())

        def key_off(units: int):
            env.extend([0.0] * (units * spd))

        for ch in chars:
            code = int(ch) & 0xFFFF
            if code == 0:
                key_off(7)          # word space -> inter-word gap (§2.4)
                continue
            c = chr(code).upper()
            # The GOLDEN spans the FULL ITU-R M.1677 table (pure Python, no
            # register budget).
            if c not in MORSE_ITU:
                raise ValueError(f"character {c!r} has no ITU-R M.1677 Morse code")
            pattern = MORSE_ITU[c]
            for i, el in enumerate(pattern):
                key_on(3 if el == "-" else 1)      # dash=3 (§2.1), dot=1
                if i < len(pattern) - 1:
                    key_off(1)                     # intra-character gap (§2.2)
            key_off(3)                             # inter-character gap (§2.3)
        return np.asarray(env, dtype=np.float64)

    def key_envelope_q15(self, chars) -> List[int]:
        """The golden envelope quantized to on-chip Q15 words (uint16)."""
        return [float_to_q15(v) & 0xFFFF for v in self.key_envelope(chars)]

    def process_reference(self, input_samples) -> np.ndarray:
        """float reference (envelope at on-chip Q15 precision)."""
        q = self.key_envelope_q15(input_samples)
        return np.asarray([q15_to_float(w) for w in q], dtype=np.float32)

    # --------------------------------------------------------- on-chip build
    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """The SRAM-backed run player + the SRAM controller macro.

        Cell 0 is the unified run PLAYER: it receives one run record
        ``(base, step, count)`` in its input registers via the panel push-read and
        emits ``count`` output samples ``LUT[cur]`` (``cur`` starts at ``base``,
        advances by ``step`` per sample). The Hann edge LUT (:meth:`edge_lut_q15`)
        is baked in-cell as data words. Cell 1 is the :class:`SramControllerBlock`
        that streams the run records into the panel (load) and drives the per-run
        push-read (lookup) — the same 1-cell macro the SRAM-backed VaricodeEncoder
        uses.

        The whole per-sample timing state machine that formerly overflowed the
        cell is now off-cell: it ran ONCE at build time (:meth:`run_records`) and
        its output lives in the panel. The player is a small fixed loop.
        """
        h = self._emit_hop
        lut = self.edge_lut_q15()
        # v2 PLAYER: an END record (count == 0, the sparse ROM's implicit
        # region terminator) HALTS without kicking — the chain idles awaiting
        # the next character. A real record plays count samples, then JUMPs the
        # COMPLETION kick (@emit_hop -> done_entry: crossover track_c -> the
        # fetch's 'next' entry) — the flow control that makes record sequencing
        # self-paced: the next fetch physically cannot start until this record
        # finishes (the record-overwrite race is impossible by construction).
        tmpl = (
            "play:\n"
            "    MOVE R0, R{in:cnt}\n"
            "    OR R0, R{in:cnt}\n"                  # set Z/NZ from count
            "    BR.Z idle\n"                         # END record: no kick
            "    MOVE R{state:cur}, R{in:base}\n"
            "loop:\n"
            "    MOVE R0, R{state:cur}\n"
            "    ADD R0, R{data:lutbase}\n"
            "    LOAD R0, [R0]\n"                     # R0 = LUT[cur]
            f"    WRITE @{h}, {self._emit_dest}\n"
            f"    JUMP @{h}, {self._emit_entry}\n"
            "    MOVE R0, R{state:cur}\n"
            "    ADD R0, R{in:step}\n"                # step in {-1,0,+1} (two's comp)
            "    MOVE R{state:cur}, R0\n"
            "    MOVE R0, R{in:cnt}\n"
            "    SUB R0, R{data:one}\n"
            "    MOVE R{in:cnt}, R0\n"
            "    BR.NZ loop\n"
            f"    JUMP @{h}, {self._done_entry}\n"    # record done -> fetch next
            "idle:\n"
            "    HALT\n"
        )
        # lutbase is the address of LUT[0]; the edge LUT words follow it. Pin
        # constants low (R1/R2) so R0 (accumulator) stays free.
        data = [DataWord("one", 1, address=1),
                DataWord("lutbase", 3, address=2)]
        for i, w in enumerate(lut):
            data.append(DataWord(f"lut{i}", w & 0xFFFF, address=3 + i))
        player = CellProgram(
            inputs=[Port("base"), Port("step"), Port("cnt")],
            outputs=[Port("samples")],
            entries=[EntryPoint("play")],
            data=data,
            state=[StateVar("cur")],
            assembly_template=tmpl,
        )
        # Cell 0: the RECORD-FETCH controller. The ``char`` entry (the landing/
        # default entry — one ASCII byte per invocation) points the panel at the
        # character's ROM region (address = byte << ROM_SHIFT) and streams the
        # region's FIRST record; the ``next`` entry (kicked by the player's
        # completion JUMP via crossover track_c) streams each following record.
        # Per record: three push-read triggers, the k-th delivering panel word ->
        # player register base+k (write descriptor rwd0+k — base/step/cnt are
        # CONSECUTIVE registers, asserted in panel_requirements), with a no-op
        # JUMP descriptor on the first two words and the PLAYER-KICK descriptor
        # on the third. The panel runs with READ auto-increment
        # (SramPanel.auto_inc_read), so only the per-character region base is
        # ever written to the address register. This is what the generic
        # SramControllerBlock cannot do (one FIXED descriptor pair): a 3-word
        # record needs 3 different deliveries + a kick.
        ph = self._panel_hop
        rwd2 = (self._read_wr_desc + 2) & 0xFFFF
        fetch_tmpl = (
            "char:\n"
            "    MOVE R0, R{in:char}\n"
            f"    SHL R0, #{self.ROM_SHIFT}\n"       # region base = char * 128
            f"    WRITE @{ph}, 5\n"                  # panel address = region base
            # fall through: stream the region's first record.
            "next:\n"
            "    MOVE R{state:wd}, R{data:rwd0}\n"
            "floop:\n"
            "    MOVE R0, R{state:wd}\n"
            f"    WRITE @{ph}, 3\n"                  # R3 = write descriptor
            "    MOVE R0, R{data:rjds}\n"
            f"    WRITE @{ph}, 4\n"                  # R4 = sentinel (no kick)
            f"    JUMP @{ph}, 1\n"                   # push-read word k
            "    MOVE R0, R{state:wd}\n"
            "    ADD R0, R{data:one}\n"
            "    MOVE R{state:wd}, R0\n"             # next player register
            "    CMP R{state:wd}, R{data:rwd2}\n"    # done base+step (2 words)?
            "    BR.NZ floop\n"
            # third word: cnt, then KICK the player's play entry.
            "    MOVE R0, R{state:wd}\n"
            f"    WRITE @{ph}, 3\n"
            "    MOVE R0, R{data:rjdk}\n"
            f"    WRITE @{ph}, 4\n"
            f"    JUMP @{ph}, 1\n"
            "    HALT\n"
        )
        fetch = CellProgram(
            inputs=[Port("char")],                   # one ASCII byte per char
            outputs=[Port("out")],
            entries=[EntryPoint("char"), EntryPoint("next")],
            data=[DataWord("one", 1, address=1),
                  DataWord("rwd0", self._read_wr_desc, address=2),
                  DataWord("rwd2", rwd2, address=3),
                  DataWord("rjds", 0x73E0, address=4),   # _jp(31,0) sentinel
                  DataWord("rjdk", self._read_jp_desc, address=5)],
            state=[StateVar("wd")],
            assembly_template=fetch_tmpl,
        )
        # Cell 0 = fetch (the LANDING cell — ASCII chars inject here);
        # cell 1 = player (the LAST cell — the block's output is its samples).
        return {0: fetch, 1: player}

    def panel_requirements(self) -> dict:
        """This block needs an SRAM panel holding the MESSAGE-INDEPENDENT Morse
        run-record ROM (:meth:`rom_image` — one region per charset character),
        read with address auto-increment. The fetch cell (0) sits AT the panel's
        x1_out port; each record's words push-read back to the player (cell 1)
        via x1_in, landing in its consecutive base/step/cnt registers. The
        player's completion kick targets the fetch's ``next`` entry
        (``completion_entry`` — relayed via the crossover's control track)."""
        from ..resolver import CellProgramResolver
        # ASSERT the player's base/step/cnt registers are consecutive — the
        # fetch's rwd0+k arithmetic depends on it. A future re-allocation that
        # breaks adjacency must FAIL here, not silently mis-deliver.
        cp = self.build_cell_programs()[1]
        r = CellProgramResolver()
        cls = r.classify_addresses(cp)
        regs = {v.get("name"): a for a, v in cls.items() if v.get("name")}
        b, s, c = regs.get("base"), regs.get("step"), regs.get("cnt")
        if not (isinstance(b, int) and s == b + 1 and c == b + 2):
            raise ValueError(
                f"CW player base/step/cnt registers not consecutive "
                f"(base={b}, step={s}, cnt={c}) — the record fetch's rwd0+k "
                f"delivery would mis-land; fix the register allocation")
        return {
            "label": f"Morse ROM ({len(self._charset)} chars, "
                     f"{self._spd} samp/dot, edge {self._edge})",
            "image": self.rom_image(),
            "controller_cell": 0,
            "return_port": "base",
            "return_cell": 1,
            "auto_inc_read": True,
            "completion_entry": "next",
        }

    def reset(self):
        pass
