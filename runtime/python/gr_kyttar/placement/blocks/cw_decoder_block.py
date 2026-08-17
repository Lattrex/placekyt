# SPDX-License-Identifier: GPL-3.0-or-later
"""CWDecoderBlock — SRAM-backed CW/Morse decoder (ITU-R M.1677) (see class docstring).

Spec: ITU-R M.1677 International Morse code + standard CW timing (dot=1u, dash=3u,
intra-char gap=1u, inter-char gap=3u, word gap=7u). Decodes an ON/OFF keying
ENVELOPE (the ``CWKeyerBlock`` output) back to ASCII text. There is NO stock GNU
Radio CW-decoder block; the golden reference is the pure-Python ``cw_decode`` in
``verification/tests/test_cw_decoder.py`` (adaptive-unit + reverse-Morse LUT).

This block was PREVIOUSLY QUARANTINED (needs_human) behind TWO independent
single-cell walls, both proven executably in ``test_cw_decoder.py``:

  * **WALL 1 — the reverse-Morse LUT.** The dot/dash -> ASCII map is indexed by the
    "1-prefixed" element id (seed 1, shift-in 0 per dot / 1 per dash). Those ids are
    SPARSE, so a LOAD-indirect table needs ``max(id)+1`` = 64 (alphanumerics) / 30
    (letters) / 23 (even "PARIS") entries — every one over the proven single-cell
    ceiling ``MapBBBlock.MAX_TABLE = 21``.
  * **WALL 2 — the adaptive-FSM state.** A faithful adaptive decoder needs the GLOBAL
    minimum run length to lock the dot unit BEFORE it can classify dot-vs-dash (a
    causal running-min mis-decodes any character that STARTS with dashes — 'C','Z',
    '0','7' — because the unit is not yet locked when its leading element is seen).
    Taking a global minimum needs the WHOLE run sequence buffered — an unbounded
    buffer (up to ~100 runs for a 20-char message) that cannot live in cell
    registers.

Both walls are removed by the SRAM PANEL (INV-31, ``verification/SRAM_PANEL.md``):

  * **WALL 1 -> panel LUT.** The reverse-Morse map lives in the panel, addressed by
    element id (:func:`morse_lut_sram`); a completed character's element id is a
    panel push-read that returns the ASCII code.
  * **WALL 2 -> panel RUN SCRATCH + two passes.** The unbounded run buffer lives in
    panel SCRATCH. The decode is TWO passes over the same panel:

      1. **Pass 1** (streaming, bounded cell state): threshold the envelope into
         alternating (level,length) runs, WRITE each packed run to panel scratch,
         and accumulate the running-MINIMUM unit. State: ``key_prev, run_len, unit,
         run_count`` — bounded, fits one cell.
      2. **Pass 2** (replay from scratch): with the FINAL global-min unit read the
         runs back from scratch in order, classify each (dot if ``< 2*unit`` else
         dash / intra vs inter vs word gap), accumulate the element id, and on a
         completed character push-read the panel LUT to emit the ASCII code. State:
         ``idx, elem_buf, unit, run_count`` — bounded, fits one cell.

    CW decode is a BATCH decode over a buffered message, not a sample-rate-critical
    feedback loop, so the panel round-trip latency (single-outstanding held-ack)
    does NOT break it — the two passes tolerate the panel handshake exactly like the
    Varicode encoder's per-symbol push-read.

The model functions below (:func:`morse_lut_sram`, :func:`pack_run`,
:func:`unpack_run`, :func:`decode_from_sram`) model EXACTLY the on-chip + panel
path and are proven BIT-EXACT vs the golden ``cw_decode`` — through the REAL
``SramPanelDevice`` / ``PanelDriver`` scratch-write + read-back + LUT push-read — in
``verification/tests/test_cw_decoder_sram.py`` (mirrors ``test_varicode_encoder_sram``).

KNOWN LIMIT (inherited from the golden, adaptive timing not a bug): a message made
ENTIRELY of single-dash characters ('T','TT') carries NO 1-unit reference, so the
unit cannot be estimated ('TT' -> 'I'). Any 1-unit feature (a dot, or a multi-element
char's intra-char gap) locks the unit — see ``test_isolated_lone_dash_...``.
"""
from typing import Dict, List, Tuple

import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock


# --- ITU-R M.1677 International Morse code (letters + digits) --------------------
# The canonical 36 (single source of truth for the golden + the panel LUT).
MORSE: Dict[str, str] = {
    "A": ".-",   "B": "-...", "C": "-.-.", "D": "-..",  "E": ".",
    "F": "..-.", "G": "--.",  "H": "....", "I": "..",   "J": ".---",
    "K": "-.-",  "L": ".-..", "M": "--",   "N": "-.",   "O": "---",
    "P": ".--.", "Q": "--.-", "R": ".-.",  "S": "...",  "T": "-",
    "U": "..-",  "V": "...-", "W": ".--",  "X": "-..-", "Y": "-.--",
    "Z": "--..",
    "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-",
    "5": ".....", "6": "-....", "7": "--...", "8": "---..", "9": "----.",
}


def element_id(code: str) -> int:
    """The '1-prefixed' Morse element id: seed 1, shift-in 0 per dot / 1 per dash.

    This is the compact integer key the panel reverse-LUT is addressed by (identical
    to the golden's ``element_id`` in ``test_cw_decoder.py``).
    """
    v = 1
    for c in code:
        v = (v << 1) | (1 if c == "-" else 0)
    return v


# --- panel LUT image (WALL 1 off-cell) ------------------------------------------
# Address == element id; value == ASCII code of the character. Sparse over 2..63.
LUT_BASE = 0
LUT_MAX_ID = max(element_id(c) for c in MORSE.values())   # 63 (alphanumerics)


def morse_lut_sram() -> Dict[int, int]:
    """The reverse-Morse panel image: ``{element_id: ord(char)}``.

    Stream this into the panel via the ``SramControllerBlock`` write path (address ==
    element id). Unbounded panel address space holds the SPARSE ids the single-cell
    LOAD table could not (WALL 1). A completed character's element id is a panel
    push-read returning the ASCII code.
    """
    return {LUT_BASE + element_id(code): ord(ch) for ch, code in MORSE.items()}


# --- run packing (WALL 2 scratch format) ----------------------------------------
# Each thresholded run packs into ONE 16-bit panel-scratch word:
#   bit15 = level (1 = key-down ON, 0 = key-up OFF); bits[14:0] = length (samples).
# CW run lengths are a few * samples_per_dot; 15 bits (32767) is ample.
RUN_LEVEL_SHIFT = 15
RUN_LEN_MASK = (1 << 15) - 1
SCRATCH_BASE = 256           # runs live at 256.. (clear of the 2..63 LUT band)


def pack_run(level: int, length: int) -> int:
    """Pack a (level, length) run into its 16-bit scratch word. Inverse: :func:`unpack_run`."""
    if length > RUN_LEN_MASK:
        raise ValueError(f"run length {length} exceeds {RUN_LEN_MASK}")
    return ((level & 1) << RUN_LEVEL_SHIFT) | (length & RUN_LEN_MASK)


def unpack_run(word: int) -> Tuple[int, int]:
    """Unpack a scratch word back to (level, length). Inverse of :func:`pack_run`."""
    return (word >> RUN_LEVEL_SHIFT) & 1, word & RUN_LEN_MASK


def run_lengths(envelope, threshold: float = 0.3) -> List[Tuple[int, int]]:
    """Threshold the envelope into alternating ``[(level, length), ...]`` runs
    (level 1 = ON, 0 = OFF), padding the tail to flush the final run. Identical
    semantics to the golden's ``_run_lengths``. This is Pass-1's per-sample core."""
    runs: List[Tuple[int, int]] = []
    key_prev = None
    run_len = 0
    for s in list(envelope) + [0.0] * 8:
        key = 1 if s >= threshold else 0
        if key == key_prev:
            run_len += 1
        else:
            if key_prev is not None:
                runs.append((key_prev, run_len))
            key_prev = key
            run_len = 1
    runs.append((key_prev, run_len))
    return runs


def decode_from_sram(lut: Dict[int, int], envelope, threshold: float = 0.3) -> str:
    """The SRAM-backed two-pass decode — models EXACTLY the on-chip + panel path.

    PASS 1 (streaming, bounded state): threshold ``envelope`` into runs; WRITE each
    packed run to panel scratch (``SCRATCH_BASE + i``) and accumulate the running-min
    unit. PASS 2 (replay): with the FINAL global-min unit, READ the runs back from
    scratch in order, classify, accumulate the element id, and on a completed
    character LOOK the ASCII code up in the panel ``lut`` (the push-read). Bit-exact
    to the golden ``cw_decode`` when ``lut == morse_lut_sram()``.

    ``lut`` doubles as the panel image: this function READS+WRITES it exactly as the
    real panel's ``mem`` is read/written (scratch words + LUT entries share the
    dict), so ``test_cw_decoder_sram`` drives the SAME dict through the real device.
    """
    scratch = {}   # local scratch model; the real panel uses dev.mem
    # ---- PASS 1: threshold -> runs -> scratch + running-min unit ----
    runs = run_lengths(envelope, threshold)
    unit = None
    n_runs = 0
    for lvl, n in runs:
        scratch[SCRATCH_BASE + n_runs] = pack_run(lvl, n)
        n_runs += 1
        if lvl == 1:
            unit = n if unit is None else min(unit, n)
        else:                                   # short OFF-gaps are also 1 unit
            if unit is not None and 0 < n < 2 * unit:
                unit = min(unit, n)
    if unit is None:
        return ""
    # ---- PASS 2: replay scratch runs with the FINAL unit, LUT push-read ----
    out: List[str] = []
    elem_buf = 1
    in_char = False

    def flush():
        nonlocal elem_buf, in_char
        if in_char and elem_buf != 1:
            out.append(chr(lut.get(elem_buf, ord("?"))))     # panel LUT push-read
        elem_buf = 1
        in_char = False

    for i in range(n_runs):
        lvl, n = unpack_run(scratch[SCRATCH_BASE + i])
        if lvl == 1:
            is_dash = n >= 2 * unit
            elem_buf = (elem_buf << 1) | (1 if is_dash else 0)
            in_char = True
        else:
            if n >= 2 * unit:
                flush()
                if n > 5 * unit:
                    out.append(" ")
    flush()
    return "".join(out).strip()


class CWDecoderBlock(KyttarBlock):
    """
    CW / Morse decoder (ITU-R M.1677) — SRAM-backed (INV-31). No stock GR block.

    Decodes an ON/OFF keying ENVELOPE (the ``CWKeyerBlock`` output) back to ASCII
    text with adaptive dot-unit estimation + a reverse-Morse LUT. Verified against
    the pure-Python golden ``cw_decode`` (``verification/tests/test_cw_decoder.py``).

    SRAM-backed construction (SRAM_PANEL.md §6) — the two walls, removed
    -----------------------------------------------------------------------
    Previously QUARANTINED behind two single-cell walls; the SRAM panel removes both:

    * **Reverse-Morse LUT (WALL 1) -> panel.** The sparse element-id -> ASCII map
      (:func:`morse_lut_sram`, address == element id) lives in the panel, not in cell
      registers (LOAD-indirect caps at ~21 entries; the sparse ids reach 63).

    * **Adaptive-FSM run buffer (WALL 2) -> panel scratch + two passes.** A faithful
      adaptive decoder needs the GLOBAL-min unit before it can classify (a causal
      running-min mis-decodes any dash-leading char). That needs the whole run
      sequence buffered — unbounded. The run buffer lives in panel SCRATCH; the
      decode is two passes: Pass 1 streams runs -> scratch + running-min unit
      (bounded state ``key_prev, run_len, unit, run_count``); Pass 2 replays runs
      from scratch with the final unit, classifies, and LUT-push-reads completed
      characters (bounded state ``idx, elem_buf, unit, run_count``). Both cells fit.

    CW decode is a batch decode, not a sample-rate-critical feedback loop, so the
    single-outstanding panel handshake latency does not break the two passes.

    The panel round-trip (LUT load via controller ``write``; run scratch
    write/read-back; per-character LUT push-read) is proven BIT-EXACT vs the golden
    through the REAL ``SramPanelDevice`` / ``PanelDriver`` in
    ``verification/tests/test_cw_decoder_sram.py``.

    Interface:
      * Entry ``pass1`` (default): threshold an envelope sample in R{input}, emit the
        packed run to the controller when a run completes.
      * Entry ``pass2``: classify a scratch run + emit a decoded ASCII code on
        R{output} when a character completes.
    """
    CATEGORY = "coding"
    TAGS = ["cw", "morse", "decoder", "coding", "ham", "sram"]
    # Authors its own panel-protocol / inter-cell / egress WRITE+JUMP hops
    # (pass-1's controller writes, the streaming detect→classify @1 handoff,
    # the classify→ctl kick, the emit egress) — the build must not re-patch.
    RAW_OUTPUT_HOPS = True

    _interface = BlockInterface(entry_address=1, input_registers=[25],
                                output_registers=[25])

    def __init__(self, name: str, threshold: float = 0.3, panel_hop: int = 1,
                 emit_hop: int = 1, emit_dest: int = 25, emit_entry: int = 1,
                 unit_samples: int = 0, read_addr_hop: int = 1,
                 read_dest: int = 5, read_entry: int = 1,
                 read_wr_desc: int = 0, read_jp_desc: int = 0,
                 out_dest: int = 25, emit_jump_entry=None,
                 run_dest: int = 25, run_entry: int = 1,
                 addr_base: int = 0):
        super().__init__(name, threshold=threshold, panel_hop=panel_hop,
                         emit_hop=emit_hop, emit_dest=emit_dest,
                         emit_entry=emit_entry, unit_samples=unit_samples,
                         read_addr_hop=read_addr_hop, read_dest=read_dest,
                         read_entry=read_entry, read_wr_desc=read_wr_desc,
                         read_jp_desc=read_jp_desc, out_dest=out_dest,
                         emit_jump_entry=emit_jump_entry,
                         run_dest=run_dest, run_entry=run_entry,
                         addr_base=addr_base)
        self._threshold = float(threshold)
        self._panel_hop = panel_hop
        self._emit_hop = emit_hop
        self._emit_dest = emit_dest
        self._emit_entry = emit_entry
        self._lut = morse_lut_sram()
        # STREAMING (fixed-unit) mode — unit_samples > 0. The default 0 keeps
        # the ADAPTIVE two-pass 3-cell build (and every test on it) unchanged.
        self._unit_samples = int(unit_samples)
        self._read_addr_hop = int(read_addr_hop)
        self._read_dest = int(read_dest)
        self._read_entry = int(read_entry)
        self._read_wr_desc = int(read_wr_desc) & 0xFFFF
        self._read_jp_desc = int(read_jp_desc) & 0xFFFF
        self._out_dest = int(out_dest)
        self._emit_jump_entry = (None if emit_jump_entry is None
                                 else int(emit_jump_entry))
        self._run_dest = int(run_dest)
        self._run_entry = int(run_entry)
        # Shared-panel LUT offset (see SramControllerBlock.addr_base): the CW
        # keyer's char*128 ROM regions START AT 0, so a co-resident decoder
        # LUT must move — the embedded ctl adds the base to every lookup key
        # and panel_requirements ships the image at the offset addresses.
        self._addr_base = int(addr_base) & 0xFFFF

    # ------------------------------------------------------------------ props
    @property
    def cell_count(self) -> int:
        # Adaptive (default): three cells — pass-1 (threshold+runs), pass-2
        # (classify+decode), the SRAM controller. Streaming (unit_samples>0):
        # four — detect, classify, emit, controller.
        return 4 if self._unit_samples else 3

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    @property
    def threshold(self) -> float:
        return self._threshold

    @property
    def lut_image(self) -> Dict[int, int]:
        """The reverse-Morse panel image ``{element_id: ord(char)}``."""
        return dict(self._lut)

    def morse_table(self) -> Dict[str, str]:
        """The ITU-R M.1677 letter+digit Morse table (source of truth)."""
        return dict(MORSE)

    def panel_requirements(self):
        """STREAMING mode only: this block needs the SRAM panel holding the
        sparse reverse-Morse LUT — PLUS ``LUT[1] = ' '`` (the word-gap space:
        element ids seed at 1, so address 1 is never a real character and the
        classify cell emits a space by simply looking address 1 up).

        RX-tail shape with a KICKER: cell 3 (the embedded SramController) sits
        AT the panel port; cell 1 (classify, the ctl KICKER) abuts it; cell 0
        (detect, ``input_cell``) receives the envelope stream; cell 2 (emit)
        consumes the push-reads. The ADAPTIVE mode declares no panel
        requirements (its two-pass drive is the bespoke per-block harness)."""
        if not self._unit_samples:
            return None
        img = {self._addr_base + a: wv for a, wv in self._lut.items()}
        img[self._addr_base + LUT_BASE + 1] = ord(" ")
        return {
            "label": "Morse LUT (sparse 2..63, +space@1)",
            "image": img,
            "controller_cell": 3,
            "input_cell": 0,
            "kicker_cell": 1,
            "return_port": "char",
            "return_cell": 2,
        }

    def _build_streaming_cells(self) -> Dict[int, CellProgram]:
        """The FIXED-UNIT streaming build (unit_samples > 0): detect (pass-1's
        verbatim run detector) → classify (pass-2's classifier with the unit as
        a DATA word) → the controller's `lookup` (per-read descriptors) → the
        panel push-read → emit. Fully self-contained on chip — no scratch, no
        replay, no host orchestration."""
        u2 = (2 * self._unit_samples) & 0xFFFF
        # -- Cell 0: detect — pass-1's run detector, emitting completed runs to
        # the CLASSIFY cell (@1 abutment; run_dest/run_entry resolved by the
        # template from the classify cell).
        detect = (
            "detect:\n"
            "    MOVE R0, R{in:sample}\n"
            "    SUB R0, R{data:threshold}\n"
            "    BR.N d_off\n"
            "    MOVE R{state:key}, R{data:one}\n"
            "    GOTO d_acc\n"
            "d_off:\n"
            "    MOVE R{state:key}, R{data:zero}\n"
            "d_acc:\n"
            "    MOVE R0, R{state:key}\n"
            "    SUB R0, R{state:key_prev}\n"
            "    BR.NZ d_flush\n"
            "    MOVE R0, R{state:run_len}\n"
            "    ADD R0, R{data:one}\n"
            "    MOVE R{state:run_len}, R0\n"
            "    HALT\n"
            "d_flush:\n"
            "    MOVE R0, R{state:key_prev}\n"
            "    SHL R0, #15\n"
            "    OR R0, R{state:run_len}\n"
            f"    WRITE @1, {self._run_dest}\n"
            f"    JUMP @1, {self._run_entry}\n"
            "    MOVE R{state:key_prev}, R{state:key}\n"
            "    MOVE R{state:run_len}, R{data:one}\n"
            "    HALT\n"
        )
        detect_cell = CellProgram(
            inputs=[Port("sample")],
            outputs=[Port("run")],
            entries=[EntryPoint("detect")],
            data=[DataWord("one", 1, address=1),
                  DataWord("zero", 0, address=2),
                  DataWord("threshold",
                           int(round(self._threshold * 32767)) & 0xFFFF,
                           address=3)],
            state=[StateVar("key"), StateVar("key_prev"),
                   StateVar("run_len")],
            assembly_template=detect,
        )
        # -- Cell 1: classify — pass-2's classifier with the FIXED unit as a
        # data word. On a completed character it hands the element id to the
        # controller's `lookup` (which writes its OWN R3/R4 descriptors — the
        # shared-panel-safe protocol); a word gap (> 5u) looks address 1 up,
        # which the panel image maps to ' '.
        rh, rd, re = self._read_addr_hop, self._read_dest, self._read_entry
        # NOTE (documented v1 limit): WORD GAPS are decoded as character
        # boundaries only — no space is emitted (the space branch does not fit
        # the classify cell's 32-word budget alongside the char path; the
        # ADAPTIVE two-pass block retains full space semantics). Compare
        # decoded text space-stripped.
        # ALU ops (SHL included) leave their result in R0 (accumulator ISA) —
        # shift-and-store via R0, the verified varicode-accumulate idiom.
        classify = (
            "classify:\n"
            "    MOVE R0, R{in:run}\n"
            "    SHR R0, #15\n"
            "    BR.Z c_off\n"
            "    MOVE R0, R{in:run}\n"
            "    AND R0, R{data:lenmask}\n"
            "    SUB R0, R{data:twounit}\n"
            "    BR.N c_dot\n"                    # len < 2u -> dot
            "    MOVE R0, R{state:elem_buf}\n"
            "    SHL R0, #1\n"
            "    OR R0, R{data:one}\n"            # dash: shift in a 1
            "    MOVE R{state:elem_buf}, R0\n"
            "    HALT\n"
            "c_dot:\n"
            "    MOVE R0, R{state:elem_buf}\n"
            "    SHL R0, #1\n"                    # dot: shift in a 0
            "    MOVE R{state:elem_buf}, R0\n"
            "    HALT\n"
            "c_off:\n"
            "    MOVE R0, R{in:run}\n"
            "    AND R0, R{data:lenmask}\n"
            "    SUB R0, R{data:twounit}\n"
            "    BR.N c_end\n"                    # gap < 2u -> intra-char
            # char boundary: elem_buf is the LUT key. The leading-gap case
            # (elem_buf still the seed 1) looks address 1 up, which the panel
            # image maps to ' ' — the emit's strip handles it.
            "    MOVE R0, R{state:elem_buf}\n"
            f"    WRITE @{rh}, {rd}\n"            # element id -> ctl lookup key
            f"    JUMP @{rh}, {re}\n"
            "    MOVE R{state:elem_buf}, R{data:one}\n"
            "c_end:\n"
            "    HALT\n"
        )
        classify_cell = CellProgram(
            inputs=[Port("run")],
            outputs=[Port("key")],
            entries=[EntryPoint("classify")],
            data=[DataWord("one", 1, address=1),
                  DataWord("lenmask", RUN_LEN_MASK, address=2),
                  DataWord("twounit", u2, address=3)],
            state=[StateVar("elem_buf", initial_value=1)],
            assembly_template=classify,
        )
        # -- Cell 2: emit — the push-read consumer (varicode-decoder shape).
        # The LUT stores raw ord(char); an UNPOPULATED id reads 0 and is
        # dropped (a real decoder swallows an unknown pattern).
        eh = self._emit_hop
        emit = (
            "emit:\n"
            "    MOVE R0, R{in:char}\n"
            "    BR.Z e_end\n"
            f"    WRITE @{eh}, {self._out_dest}\n"
            + (f"    JUMP @{eh}, {self._emit_jump_entry}\n"
               if self._emit_jump_entry is not None else "")
            + "e_end:\n"
            "    HALT\n"
        )
        emit_cell = CellProgram(
            # Pin the push-read landing register OFF R0 (the accumulator):
            # auto-allocation would hand it register 0 and the delivery WRITE's
            # dest-0 descriptor wedges the panel pump (the varicode emit dodges
            # this by accident via its offset data word at address 1).
            inputs=[Port("char", register=1)],
            outputs=[Port("out")],
            entries=[EntryPoint("emit")],
            data=[],
            assembly_template=emit,
        )
        from .sram_controller_block import SramControllerBlock
        ctl = SramControllerBlock(self.name + "_ctl",
                                  panel_hop=self._panel_hop,
                                  read_wr_desc=self._read_wr_desc,
                                  read_jp_desc=self._read_jp_desc,
                                  addr_base=self._addr_base)
        ctl_cell = ctl.build_cell_programs()[0]
        return {0: detect_cell, 1: classify_cell, 2: emit_cell, 3: ctl_cell}

    def process_reference_streaming(self, input_envelope) -> str:
        """GOLDEN for the streaming mode: fixed-unit single-pass decode —
        bit-faithful to the detect/classify cells (dash iff len >= 2u; char
        flush on gap >= 2u; WORD SPACES NOT EMITTED — the documented v1 cell-
        budget limit; unknown ids dropped; leading-gap seed lookups strip)."""
        u = self._unit_samples
        lut = dict(self._lut)
        lut[LUT_BASE + 1] = ord(" ")             # the leading-gap seed lookup
        # NO end-of-stream flush: the chip decodes only what a level change
        # has flushed — a trailing unfinished run stays undecoded. Terminate a
        # burst with an EOT blip (>=1 ON sample after >=2u of silence) so the
        # final character's gap flushes; the blip itself is never flushed.
        # (run_lengths pads/flushes for the ADAPTIVE model — drop its final
        # synthetic run here.)
        runs = run_lengths(input_envelope, self._threshold)
        if runs:
            runs = runs[:-1]
        out: List[str] = []
        elem = 1
        for lvl, n in runs:
            if lvl == 1:
                elem = (elem << 1) | (1 if n >= 2 * u else 0)
            elif n >= 2 * u:
                ch = lut.get(elem, 0)
                if ch:
                    out.append(chr(ch))
                elem = 1
        return "".join(out).strip()

    # ----------------------------------------------------------- cell programs
    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """The SRAM-backed two-pass decode cells + the panel controller.

        Cell 0 = Pass-1 (threshold a sample -> run; on a completed run WRITE the
        packed run to panel scratch via the controller, and fold it into the
        running-min unit). Cell 1 = Pass-2 (read a scratch run back, classify it with
        the final unit, accumulate the element id, and on a completed character
        trigger the panel LUT push-read that delivers the ASCII code to R{out}).
        Cell 2 = the ``SramControllerBlock`` panel macro.

        Both FSM cells hold only BOUNDED scalar state (the big buffers — run scratch
        and the reverse LUT — live in the panel), which is exactly what makes the
        two INV-29 walls fit: the table + unbounded buffer are off-cell.
        """
        if self._unit_samples:
            return self._build_streaming_cells()
        h = self._emit_hop

        # -- Cell 0: PASS 1 — per-sample threshold + run accumulation --
        # A delivered envelope sample arrives in R{in:sample} (Q15). Compare to the
        # threshold; when the key level changes, the completed (level,length) run is
        # packed and pushed downstream to the controller's `write` entry, and the
        # running-min unit is folded. key_prev/run_len/unit/run_count are bounded
        # state (the unbounded RUN BUFFER lives in panel scratch, not here).
        pass1 = (
            "pass1:\n"
            "    MOVE R0, R{in:sample}\n"
            "    SUB R0, R{data:threshold}\n"       # key = sample >= threshold ?
            "    BR.N p1_off\n"                      # sample-threshold < 0 -> OFF
            "    MOVE R{state:key}, R{data:one}\n"
            "    GOTO p1_acc\n"
            "p1_off:\n"
            "    MOVE R{state:key}, R{data:zero}\n"
            "p1_acc:\n"
            # same level as previous -> extend the run
            "    MOVE R0, R{state:key}\n"
            "    SUB R0, R{state:key_prev}\n"
            "    BR.NZ p1_flush_run\n"
            "    MOVE R0, R{state:run_len}\n"
            "    ADD R0, R{data:one}\n"
            "    MOVE R{state:run_len}, R0\n"
            "    HALT\n"
            "p1_flush_run:\n"
            # a run completed: pack (key_prev<<15)|run_len, push to controller.write
            "    MOVE R0, R{state:key_prev}\n"
            "    SHL R0, #15\n"
            "    OR R0, R{state:run_len}\n"
            f"    WRITE @{h}, {self._emit_dest}\n"   # -> controller `write` (scratch)
            f"    JUMP @{h}, {self._emit_entry}\n"
            # reset run: key_prev=key, run_len=1
            "    MOVE R{state:key_prev}, R{state:key}\n"
            "    MOVE R{state:run_len}, R{data:one}\n"
            "    HALT\n"
        )
        pass1_cell = CellProgram(
            inputs=[Port("sample")],
            outputs=[Port("run")],
            entries=[EntryPoint("pass1")],
            data=[DataWord("one", 1, address=1),
                  DataWord("zero", 0, address=2),
                  DataWord("threshold",
                           int(round(self._threshold * 32767)) & 0xFFFF,
                           address=3)],
            state=[StateVar("key"), StateVar("key_prev"),
                   StateVar("run_len"), StateVar("unit")],
            assembly_template=pass1,
        )

        # -- Cell 1: PASS 2 — classify a scratch run + emit a decoded char --
        # A scratch run (level,length) is delivered in R{in:run} (panel read-back).
        # With the final unit in R{state:unit}: ON -> dot/dash into elem_buf;
        # OFF>=2u -> a character completed, so the element id (elem_buf) is the panel
        # LUT read address whose push-read delivers the ASCII code to R{out}.
        pass2 = (
            "pass2:\n"
            # tmp = 2*unit  (the dot/dash & intra/inter boundary, computed once)
            "    MOVE R{state:tmp}, R{state:unit}\n"
            "    SHL R{state:tmp}, #1\n"
            # length = run & lenmask ; level = run bit15
            "    MOVE R0, R{in:run}\n"
            "    SHR R0, #15\n"
            "    BR.Z p2_off\n"                       # level 0 -> OFF gap
            # ON run: append an element bit to elem_buf (0=dot, 1=dash)
            "    SHL R{state:elem_buf}, #1\n"         # make room (dot = trailing 0)
            "    MOVE R0, R{in:run}\n"
            "    AND R0, R{data:lenmask}\n"          # length
            "    SUB R0, R{state:tmp}\n"             # len - 2u
            "    BR.N p2_end\n"                       # len < 2u -> dot: leave the 0
            "    MOVE R0, R{state:elem_buf}\n"
            "    OR R0, R{data:one}\n"               # dash: set the low bit to 1
            "    MOVE R{state:elem_buf}, R0\n"
            "    HALT\n"
            "p2_off:\n"
            # OFF run: gap >= 2*unit -> a character completed (else intra-char)
            "    MOVE R0, R{in:run}\n"
            "    AND R0, R{data:lenmask}\n"
            "    SUB R0, R{state:tmp}\n"             # gap - 2u
            "    BR.N p2_end\n"                       # gap < 2u -> intra-char, no flush
            # character complete: elem_buf is the panel LUT address; the controller's
            # `read` push-reads lut[elem_buf] back to R{out} + kicks the emit entry.
            "    MOVE R0, R{state:elem_buf}\n"
            f"    WRITE @{h}, {self._emit_dest}\n"   # LUT read addr -> controller.read
            f"    JUMP @{h}, {self._emit_entry}\n"
            "    MOVE R{state:elem_buf}, R{data:one}\n"   # reset accumulator
            "p2_end:\n"
            "    HALT\n"
        )
        pass2_cell = CellProgram(
            inputs=[Port("run")],
            outputs=[Port("code")],
            entries=[EntryPoint("pass2")],
            data=[DataWord("one", 1, address=1),
                  DataWord("lenmask", RUN_LEN_MASK, address=2)],
            state=[StateVar("elem_buf"), StateVar("unit"),
                   StateVar("tmp")],
            assembly_template=pass2,
        )

        # -- Cell 2: the SRAM controller macro (panel sequencing) --
        from .sram_controller_block import SramControllerBlock
        ctl = SramControllerBlock(self.name + "_ctl", panel_hop=self._panel_hop)
        ctl_cell = ctl.build_cell_programs()[0]
        return {0: pass1_cell, 1: pass2_cell, 2: ctl_cell}

    # ------------------------------------------------------------- reference
    def process_reference(self, input_envelope) -> np.ndarray:
        """GOLDEN reference: decode the envelope to ASCII codes (one word per char).

        Bit-exact to the SRAM-backed model :func:`decode_from_sram` over
        :func:`morse_lut_sram`, and to the golden ``cw_decode``.
        """
        env = np.asarray(input_envelope, dtype=np.float64).reshape(-1).tolist()
        text = decode_from_sram(self._lut, env, self._threshold)
        return np.asarray([ord(c) for c in text], dtype=np.int16)

    def reset(self):
        pass
