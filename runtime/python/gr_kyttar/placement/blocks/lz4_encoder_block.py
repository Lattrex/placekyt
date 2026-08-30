# SPDX-License-Identifier: GPL-3.0-or-later
"""LZ4EncoderBlock — SRAM-backed LZ4 *block format* compressor (see the class docstring).

Spec: the published **LZ4 Block Format Description** (``doc/lz4_Block_format.md`` in the
lz4/lz4 reference repository, Yann Collet). There is no stock GNU Radio block for this
(``grc_block`` is ``''``); the golden is the same transcription of that document the
decoder uses (``verification/tests/lz4_golden.py``), and the acceptance bar is that an
**independent** LZ4 decoder — the reference C implementation through its Python binding
(``lz4.block``) — accepts what this block produces.

This is the sibling of :class:`~.lz4_decoder_block.LZ4DecoderBlock` and the THIRD
SRAM-backed DSP block. Where the decoder writes one panel region (the history window)
and reads it at a computed address, this block uses **two disjoint panel regions at
once** — the stored input and a hash table — which is why the region split is a
correctness property and not a tuning knob (see ``window_words``).
"""
from typing import Any, Dict, List, Tuple

import numpy as np

from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from ._base import KyttarBlock, BlockInterface


#: The LZ4 minimum match length. A match-length nibble of 0 encodes a 4-byte match.
MINMATCH = 4

#: The 4-bit length-nibble value that means "read continuation bytes".
NIBBLE_ESCAPE = 15

#: A continuation byte equal to this means "another continuation byte follows".
CONT_ESCAPE = 255

#: The last 5 bytes of a block are ALWAYS literals (LZ4 block-format rule 2).
LAST_LITERALS = 5

#: The last match must start at least this far before the end (rule 3).
MF_LIMIT = 12

#: Default history region size, in panel words. See the class docstring for why the
#: default is 2**15 rather than the decoder's 2**16.
DEFAULT_WINDOW_WORDS = 1 << 15

#: Default hash-table width in bits (4096 slots).
DEFAULT_HASH_BITS = 12

#: The 16-bit golden-ratio multiplier, ``round(2**16 / phi)``. See :func:`hash4`.
HASH_MUL = 40503

#: An input word >= this is the END-OF-BLOCK sentinel rather than a data byte.
#: Input bytes occupy 0..255, so the value is out of band by construction.
EOB_SENTINEL = 1 << 8

# --- Panel-read PHASE codes ---------------------------------------------------
# The push-read's destination register and entry are the panel's R3/R4
# descriptors, which the controller writes from BUILD-TIME params
# (SRAM_PANEL.md §3-4). There is exactly ONE such pair for the whole block, so
# every read returns to the SAME register of the SAME cell at the SAME entry.
# These codes are how the return cell knows which consumer asked.
# --- CELL IDS -----------------------------------------------------------------
# PROGRAM ORDER IS A DESIGN LEVER, NOT BOOKKEEPING (INV-53). The build resolves a
# BACKWARD internal jump — one whose destination cell PRECEDES its source in
# ``build_cell_programs()`` order — by rewriting the source cell's
# HIGHEST-ADDRESSED ``JUMP`` instruction, matched by address and never by port
# name. So a cell may declare at most ONE backward jump, and that jump must BE
# the highest-addressed one it emits, or a different jump is silently redirected
# to the backward edge's target.
#
# These numbers are therefore CHOSEN, not incidental: they are the unique-up-to-
# detail order (found by search over all 14! orderings' violation count) in which
# no cell violates either clause. They are also the order the cells are PLACED
# in, because the panel template places ``sorted(pos)`` and the build binds
# programs to placed cells BY POSITION (INV-51 clause 2) — the two dicts must
# iterate identically or whole cells silently get the wrong program.
#
# Renumbering any of these without re-running the block's own
# ``test_at_most_one_backward_internal_jump_per_cell`` gate is how a silent,
# self-concealing mis-resolution gets in.
# INGEST IS PINNED AT ZERO, and that is not cosmetic. The catalog's PortMap
# derives the block's EXTERNAL input port from the FIRST cell's first input port,
# and ``bus_router._target_input_cell`` falls back to ``placement.cells[0]`` when
# the PortMap has no entry for a named port. MEASURED: with another cell at index
# 0 the ``x16_in`` landing resolved to THAT cell's input register at THAT cell's
# position, so pass 1 was never entered — the chip ran cleanly to quiescence
# (``stop_reason == "QueueEmpty"``) and committed ZERO panel writes. The input
# landing cell must be cell 0.
#
# The rest of the numbering makes every internal JUMP edge FORWARD except the
# three that are backward by design (SEQ→FRAME ``go_seq``, LENRUN→LITS
# ``to_lits``, SEAL→FRAME ``adv``), each of which is its cell's single,
# highest-addressed jump (INV-53, gated). INS sits BELOW C_CTL so its two
# controller jumps (``set_addr`` + ``write``) stay forward — id 14 there was
# measured as TWO backward jumps in one cell, which the build silently halves.
C_INGEST = 0     # pass 1: the input landing cell (PINNED — see above)
C_RET = 1        # the SINGLE push-read return point; dispatches by phase+count
C_FRAME = 2      # the sequence's literal-run bounds, fanned out
C_VERIFY = 3     # the hash-slot test and the offset
C_MATCH = 4      # the compare engine
C_SEQ = 5        # the pass-2 driver: the position cursor and the scan loop
C_LITS = 6       # the literal replay loop
C_HASH = 7       # the rolling 4-byte hash
C_ADDR = 8       # the panel port for the HISTORY region: read issue + phase
C_INS = 9        # the panel port for the HASH-TABLE region: lookup + insert
C_TOKEN = 10     # the token's two nibbles
C_CTL = 11       # the embedded SramControllerBlock
C_SEAL = 12      # the sequence epilogue: offset, match-length re-arm, hand-back
C_LENRUN = 13    # the shared length-continuation engine
C_OUT = 14       # the egress

PH_HASH = 0      # a byte for the rolling hash — AND the hash-table slot, which
                 # RET tells apart by COUNTING (every scan position is exactly
                 # four history reads then one table read; see the RET cell)
PH_MATCH = 1     # either side of a compare step (MATCH tells them apart itself)
PH_LIT = 2       # a literal being replayed into the output


def hash4(b0: int, b1: int, b2: int, b3: int,
          hash_bits: int = DEFAULT_HASH_BITS) -> int:
    """The 4-byte hash, in 16-bit arithmetic.

    The published pseudo-code writes ``(read4(pos) * 2654435761) >> (32 - hash_bits)``
    — a 32x32 multiply. This substrate's registers are 16 bits and its ``MUL`` family
    yields the low 16 (``MUL``) or high 16 (``MULHI``) of a 16x16 product, so the
    32-bit Knuth constant has no native form. The 16-bit equivalent used here is a
    ROLLING multiplicative hash: ``h = h * HASH_MUL + b`` per byte, then one final
    multiply, keeping the top ``hash_bits``. :data:`HASH_MUL` is the 16-bit
    golden-ratio constant ``round(2**16 / phi)``.

    Rolling was chosen over packing the four bytes into two 16-bit halves for a
    measured reason: it is **one ``MUL`` plus one ``ADD`` per byte with no branch on
    which slot the byte fills**, whereas the packed form needs a per-slot branch
    because a shift count on this ISA is an instruction-field IMMEDIATE and can
    never come from a register (INV-34). Compression is identical on every payload
    in the gate (the two forms produce byte-identical blocks on all ten), so the
    simpler cell is free.

    **The hash cannot make the output wrong.** A candidate taken from the table is
    always confirmed by a real four-byte comparison against the panel before any
    match is emitted, so every hash function produces a format-legal block; a good
    one only produces a *shorter* one. That is what makes substituting the 16-bit
    construction a legitimate hardware deviation rather than a spec violation, and it
    is gated: ``test_lz4_encoder.py`` asserts round-trip correctness under a
    deliberately degenerate hash as well as under this one.
    """
    h = 0
    for b in (b0, b1, b2, b3):
        h = ((h * HASH_MUL) + b) & 0xFFFF        # one MUL + one ADD per byte
    return (h * HASH_MUL & 0xFFFF) >> (16 - hash_bits)


def encode_model(data, window_words: int = DEFAULT_WINDOW_WORDS,
                 hash_bits: int = DEFAULT_HASH_BITS) -> Tuple[List[int], dict]:
    """The EXACT two-pass model of the on-chip encoder (the DUT's Python twin).

    Written as the panel-round-trip state machine the chip runs — every panel read
    and write is an explicit step — NOT as a second copy of the golden compressor in
    ``lz4_golden.py``. That it produces blocks the published golden AND the reference
    C decoder both decode back to the input is what the gate proves.

    Returns ``(output_bytes, stats)`` with the panel traffic counted.
    """
    data = [int(b) & 0xFF for b in data]
    n = len(data)
    ht_base = window_words
    panel: Dict[int, int] = {}
    stats = {"writes": 0, "reads": 0}

    def pw(a: int, v: int):
        panel[a & 0xFFFF] = v & 0xFFFF
        stats["writes"] += 1

    def pr(a: int) -> int:
        stats["reads"] += 1
        return panel.get(a & 0xFFFF, 0)

    # ---- PASS 1: ingest. Store the input, nothing else. --------------------
    # The hash table is NOT built here: filling it during ingest records the LAST
    # occurrence of every 4-gram, so by the time pass 2 reaches position i the
    # candidate is almost always AHEAD of i and unusable. Measured: it costs the
    # whole compression ratio (a repetitive payload went from -89% to +0.7%). The
    # insert belongs at the position being encoded, exactly as the spec's
    # single-pass loop does it.
    for pos, b in enumerate(data):
        pw(pos, b)

    # ---- PASS 2: match + emit ---------------------------------------------
    out: List[int] = []

    def emit(b: int):
        out.append(b & 0xFF)

    def split_len(value: int) -> Tuple[int, List[int]]:
        """``(nibble, continuation bytes)`` for a length field.

        The nibble is only known after the continuation run is counted, and the
        token carrying it must be emitted FIRST — so the run is built into a
        scratch list and emitted after the token. On chip this is the EMIT cell's
        ``run``/``nib`` register pair driving a count-down loop.
        """
        if value < NIBBLE_ESCAPE:
            return value, []
        run: List[int] = []
        rest = value - NIBBLE_ESCAPE
        while rest >= CONT_ESCAPE:
            run.append(CONT_ESCAPE)
            rest -= CONT_ESCAPE
        run.append(rest)
        return NIBBLE_ESCAPE, run

    def emit_sequence(lit_start: int, lit_end: int, offset: int, match_len: int):
        """One sequence: token, literal-length extras, the literals replayed out of
        the panel, then (for a match) the LITTLE-ENDIAN offset and the
        match-length extras."""
        lit_nib, run = split_len(lit_end - lit_start)
        if offset:
            mat_nib, tail = split_len(match_len - MINMATCH)
        else:
            mat_nib, tail = 0, []
        emit((lit_nib << 4) | mat_nib)
        for b in run:
            emit(b)
        for p in range(lit_start, lit_end):
            emit(pr(p))
        if offset:
            emit(offset & 0xFF)                    # LITTLE endian
            emit((offset >> 8) & 0xFF)
            for b in tail:
                emit(b)

    lit_start = 0
    i = 0
    limit = n - MF_LIMIT
    while i < limit:
        b0, b1, b2, b3 = pr(i), pr(i + 1), pr(i + 2), pr(i + 3)
        h = hash4(b0, b1, b2, b3, hash_bits)
        slot = pr(ht_base + h)
        cand = slot - 1                            # slot 0 == EMPTY
        pw(ht_base + h, i + 1)
        if not (0 <= cand < i) or (i - cand) > 0xFFFF:
            i += 1
            continue
        # ONE compare loop does both jobs (INV-49 — reuse the datapath rather
        # than build a second one): it walks forward from k = 0, and the
        # four-byte MINMATCH check is simply "did it reach k == 4?". Storing
        # b0..b3 for a separate check would cost four registers in the verify
        # cell and buy nothing, because the extension re-reads both sides
        # anyway.
        k = 0
        while i + k < n - LAST_LITERALS and pr(cand + k) == pr(i + k):
            k += 1
        if k < MINMATCH:                           # rule 1: MINMATCH is 4
            i += 1
            continue
        emit_sequence(lit_start, i, i - cand, k)
        i += k
        lit_start = i
    emit_sequence(lit_start, n, 0, 0)              # the literals-only tail
    return out, stats


class LZ4EncoderBlock(KyttarBlock):
    """LZ4 **block format** encoder — SRAM-backed (INV-31). No stock GR block.

    One raw byte per input word in (the ``data_link`` one-byte-per-16-bit-word
    convention), terminated by the :data:`EOB_SENTINEL` word; the compressed LZ4
    block out, one byte per output word.

    Why it needs the panel, and why it needs TWO regions
    ----------------------------------------------------
    Compression is a search, not a transform: to emit a match the encoder must
    compare the four bytes at the current position against the four bytes at a
    *candidate* position it has already seen, and then walk both forward. Both
    operands are arbitrary earlier input, so the encoder needs random access to the
    whole input — and it needs a **hash table** to find the candidate in the first
    place. Neither fits in cells (INV-29), so both live in the panel:

    * ``[0, window_words)`` — the stored input, one byte per word, address == the
      byte's position;
    * ``[window_words, window_words + 2**hash_bits)`` — the hash table, one slot per
      hash value, holding ``position + 1`` (so that a zero slot means EMPTY, since
      position 0 is itself valid).

    **The region split is a correctness property, not a tuning knob (INV-33's
    overlap hazard, in the panel's address space).** ``SramPanelDevice`` wraps every
    address modulo its size, so a table based at 65536 aliases straight back onto
    history address 0. Measured: with ``window_words = 2**16`` the encoder read hash
    slots as input bytes and produced a block that was still format-legal and still
    the right LENGTH, but decoded to the WRONG payload — a silent wrong answer, which
    is exactly the failure mode INV-33 describes for a cell. The constructor
    therefore REJECTS a combination whose two regions do not fit disjointly in the
    panel, and the default window is 2**15 rather than the decoder's 2**16.

    Why TWO PASSES
    --------------
    LZ4 puts the **token before its literals**, and the token carries the literal
    count — which is not known until the literal run closes. A one-pass encoder with
    no output buffer therefore cannot emit the token when it reaches it. The panel
    resolves this for free: the input is *already stored there* (that is pass 1), so
    when a run closes the encoder emits the token and then **replays the literals out
    of the panel**. No extra storage, no output buffer, and the literal replay reuses
    the same read path the match verify uses.

    Pass 1 stores only. It deliberately does NOT also build the hash table — see the
    measured dead end in :func:`encode_model`.

    The cell decomposition (15 cells + the panel)
    ---------------------------------------------
    Split so each cell owns exactly the state it uses, per INV-46/INV-49 ("prefer
    more cells doing less"; cells are the surplus resource, words and instruction
    addresses are the scarce ones):

    ==== ======== =============================================================
    cell id       role
    ==== ======== =============================================================
    0    INGEST   pass 1: the input landing; stores every byte, derives the two
                  end-of-block bounds at the sentinel and starts pass 2.
    1    RET      the SINGLE push-read return point; dispatches by phase for
                  MATCH/LITS reads and by COUNT for the scan's 4+1 (see RET).
    2    FRAME    the sequence's literal-run bounds, fanned to the formatter.
    3    VERIFY   the hash-slot test, the offset, and the model's
                  ``i += 1; continue`` on a miss.
    4    MATCH    the compare engine (MINMATCH + the final-five-literals rule).
    5    SEQ      the pass-2 driver: the cursor, the scan bound, the dispatch.
    6    LITS     the literal replay loop.
    7    HASH     the rolling 4-byte hash; hands the whole table transaction
                  (lookup + insert) to INS.
    8    ADDR     the HISTORY-region panel port: read issue + return phase.
    9    INS      the HASH-TABLE panel port: holds the region base, reads the
                  old slot and inserts ``i + 1`` at the position being encoded.
    10   TOKEN    the token's two nibbles.
    11   CTL      the embedded :class:`SramControllerBlock`.
    12   SEAL     the sequence epilogue: LITTLE-ENDIAN offset, match-length
                  re-arm, the hand-back that resumes the scan.
    13   LENRUN   the shared length-continuation engine (both length fields).
    14   OUT      the block's egress, on its own cell.
    ==== ======== =============================================================

    The egress is its own cell for exactly the reason the decoder's is (INV-46/48):
    a cell serves ONE direction free and each extra costs a flip (2 instructions +
    1 ``is_face`` DataWord). OUT rests on the ring and flips NORTH only for its
    own egress burst — the flip-and-restore that INV-52's measured table shows is
    safe under concurrent transit.

    Format rules the block honours (all four are gated with INV-4 mutants)
    ----------------------------------------------------------------------
    * **MINMATCH 4** — a candidate that matches fewer than 4 bytes is not a match at
      all; those bytes stay literals. The verify engine only reports success after
      four confirmed bytes.
    * **The last 5 bytes are always literals** — the forward extension stops at
      ``n - 5``, so no match can consume them.
    * **The last match starts at least 12 bytes before the end** — the scan bound is
      ``n - 12``.
    * **The offset is 16-bit LITTLE-endian and never 0** — emitted low byte first;
      offset 0 cannot arise because a candidate is only usable when ``cand < i``.

    Parameters
    ----------
    window_words:
        Size of the panel's history region, in words (a power of two). Positions are
        addressed directly, so this is also the largest input the block compresses in
        one block.
    hash_bits:
        Width of the hash table, in bits (the table has ``2**hash_bits`` slots). Only
        affects the compression RATIO — never correctness, because every candidate is
        confirmed by a real four-byte compare.
    """
    CATEGORY = "coding"
    TAGS = ["lz4", "compress", "encoder", "coding", "sram"]
    # The embedded controller and the emit/out cells author their own panel-protocol
    # @N hops; the build must not @1-abutment-default them.
    RAW_OUTPUT_HOPS = True

    _interface = BlockInterface(entry_address=1, input_registers=[25],
                                output_registers=[25])

    MINMATCH = MINMATCH
    EOB_SENTINEL = EOB_SENTINEL

    def __init__(self, name: str, window_words: int = DEFAULT_WINDOW_WORDS,
                 hash_bits: int = DEFAULT_HASH_BITS,
                 panel_hop: int = 1, read_wr_desc: int = 0, read_jp_desc: int = 0,
                 addr_base: int = 0, emit_hop: int = 2, out_dest: int = 0,
                 emit_entry: int = 0):
        if window_words <= 0 or (window_words & (window_words - 1)):
            raise ValueError(
                f"window_words must be a power of two, got {window_words}")
        if not (1 <= int(hash_bits) <= 16):
            raise ValueError(f"hash_bits must be 1..16, got {hash_bits}")
        total = int(window_words) + (1 << int(hash_bits))
        if total > (1 << 16):
            raise ValueError(
                f"window_words {window_words} + hash table {1 << int(hash_bits)} "
                f"= {total} words exceeds the 65536-word panel. The two regions "
                "must be DISJOINT: the panel wraps every address modulo its size, "
                "so an overlapping table aliases onto the stored input and the "
                "block returns a wrong answer silently (see the class docstring).")
        super().__init__(name, window_words=window_words, hash_bits=hash_bits,
                         panel_hop=panel_hop, read_wr_desc=read_wr_desc,
                         read_jp_desc=read_jp_desc, addr_base=addr_base,
                         emit_hop=emit_hop, out_dest=out_dest,
                         emit_entry=emit_entry)
        self._window_words = int(window_words)
        self._hash_bits = int(hash_bits)
        self._panel_hop = int(panel_hop)
        self._read_wr_desc = int(read_wr_desc) & 0xFFFF
        self._read_jp_desc = int(read_jp_desc) & 0xFFFF
        self._addr_base = int(addr_base) & 0xFFFF
        self._emit_hop = int(emit_hop)
        self._out_dest = int(out_dest) & 0x1F
        self._emit_entry = int(emit_entry) & 0x1F

    # ------------------------------------------------------------------ shape
    @property
    def cell_count(self) -> int:
        # ingest, ret, frame, verify, match, seq, lits, hash, addr, ins, token,
        # controller, seal, lenrun, egress.
        return 15

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    @property
    def window_words(self) -> int:
        """The history-region size in panel words."""
        return self._window_words

    @property
    def hash_bits(self) -> int:
        """The hash table's width in bits."""
        return self._hash_bits

    @property
    def hash_table_base(self) -> int:
        """The panel address the hash table starts at (== ``window_words``)."""
        return self._window_words

    def process_reference(self, input_bytes) -> np.ndarray:
        """GOLDEN reference: the compressed LZ4 block for a raw byte stream.

        Computed by :func:`encode_model` — the panel-round-trip twin of the on-chip
        state machine. The acceptance bar is not that it equals some other
        compressor's output (LZ4 does not specify one) but that the block it
        produces decodes back to the input under an INDEPENDENT decoder; see
        ``verification/tests/test_lz4_encoder.py``.
        """
        data = np.asarray(input_bytes).reshape(-1).tolist()
        data = [int(b) & 0xFF for b in data if int(b) < EOB_SENTINEL]
        out, _stats = encode_model(data, self._window_words, self._hash_bits)
        return np.asarray(out, dtype=np.int16)

    def panel_cost(self, input_bytes) -> dict:
        """The measured panel-port traffic for a raw byte stream.

        Per ``SRAM_PANEL.md`` §2-4 a ``write`` costs 3 panel-port words and a
        ``lookup``/``read`` costs 4 out plus the 2-word panel-originated return = 6.
        """
        data = [int(b) & 0xFF for b in np.asarray(input_bytes).reshape(-1)
                if int(b) < EOB_SENTINEL]
        _out, st = encode_model(data, self._window_words, self._hash_bits)
        w, r = st["writes"], st["reads"]
        return {
            "panel_writes": w,
            "panel_reads": r,
            "write_words": 3 * w,
            "read_words": 6 * r,
            "total_words": 3 * w + 6 * r,
            "words_per_write": 3,
            "words_per_read": 6,
        }

    # --------------------------------------------------- face helpers (INV-52)
    #: Hardware FACE codes. South=0, East=1, West=2, North=3.
    _FACE_CODE = {"south": 0, "east": 1, "west": 2, "north": 3}

    def _resting_face(self, cell_id) -> int:
        """The hardware face code of a cell's RESTING face, from the layout.

        Every in-program face constant is derived through this or
        :meth:`_face_to` rather than written as a literal. A literal survives a
        re-fold and a copy-paste from another block, and the result is a cell
        that restores itself to a face the layout does not give it — which is a
        HEAD-ON PAIR the static layout check cannot see, because the layout is
        right and the PROGRAM is wrong (INV-52 clause 1, INV-56 clause 3).
        """
        return self._FACE_CODE[str(self.default_layout()[cell_id][2])]

    def _face_to(self, src, dst) -> int:
        """The face code pointing from ``src`` to ``dst``, from the layout.

        Requires the two to be on a common row or column; a flip aims at a
        DIRECTION, and the direction has to come from the geometry rather than
        from a remembered constant.
        """
        lay = self.default_layout()
        sx, sy, _ = lay[src]
        dx, dy, _ = lay[dst]
        if sy == dy:
            return self._FACE_CODE["east" if dx > sx else "west"]
        if sx == dx:
            return self._FACE_CODE["south" if dy > sy else "north"]
        raise ValueError(
            f"cells {src} and {dst} are not on a common row or column, so no "
            "single face points from one to the other")

    # ------------------------------------------------------------- the programs
    def build_cell_programs(self) -> Dict[Any, CellProgram]:
        """The eight cell programs. See the class docstring for the decomposition.

        Register discipline throughout follows INV-33: inputs low, data words above
        them, every ``StateVar`` PINNED explicitly (never auto-allocated), and no
        data address or state register at or above ``31 - instr_count``. The block's
        own gate re-derives that arithmetic for every cell.
        """
        htb = self._window_words                 # the hash table's panel base
        hshift = 16 - self._hash_bits

        # --------------------------------------------------------- cell 0 INGEST
        # PASS 1. Every input byte is written straight into the panel at
        # ``panel[pos]`` (the controller auto-increments its own ``wraddr``, which
        # is why no address accompanies the byte — the same saving the decoder
        # proved on silicon). The out-of-band sentinel word ends pass 1: it hands
        # ``n`` to the sequencer and starts pass 2.
        #
        # FACES: rests on the ring (toward SEQ, hop 1) and flips toward the panel
        # column for the one write burst, restoring at the TAIL (INV-52 clause 1).
        ingest = CellProgram(
            inputs=[Port("b")],
            outputs=[Port("hist"), Port("setn"), Port("setlim"), Port("setstop"),
                     Port("go")],
            entries=[EntryPoint("feed")],
            data=[DataWord("one", 1, address=1),
                  DataWord("eob", EOB_SENTINEL, address=2),
                  DataWord("mflimit", MF_LIMIT, address=3),
                  DataWord("lastlit", LAST_LITERALS, address=4)],
            state=[StateVar("pos", register=5, initial_value=0)],
            assembly_template=(
                "feed:\n"
                "    CMP R{in:b}, R{data:eob}\n"
                "    BR.GE flush\n"
                "    MOVE R0, R{in:b}\n"
                "    {write:hist}\n"                  # addr.a = the byte
                "    {jump:hist}\n"                   # addr.store -> ctl.write
                "    ADD R{state:pos}, R{data:one}\n"
                "    MOVE R{state:pos}, R0\n"
                "    HALT\n"
                "flush:\n"
                # `pos` is now the input length n. The two END-OF-BLOCK bounds are
                # derived HERE rather than in SEQ, because this cell has words to
                # spare and SEQ does not — the scan loop is what is tight. Both
                # are LZ4 format rules:
                #   lim  = n - 12  (rule 3: the last match starts >= 12 from the
                #                   end) -> SEQ's scan bound;
                #   stop = n - 5   (rule 2: the last five bytes are ALWAYS
                #                   literals) -> MATCH's extension bound.
                # Both are SIGNED: a payload shorter than 12 bytes yields a
                # negative `lim`, which sends the whole input to the
                # literals-only tail — the correct behaviour at the small end.
                "    SUB R{state:pos}, R{data:mflimit}\n"
                "    {write:setlim}\n"                # seq.lim
                "    SUB R{state:pos}, R{data:lastlit}\n"
                "    {write:setstop}\n"               # match.stop
                "    MOVE R0, R{state:pos}\n"
                "    {write:setn}\n"                  # seq.n
                "    {jump:go}\n"                     # seq.start
                "    HALT\n"
            ),
        )

        # ------------------------------------------------------------ cell 1 SEQ
        # The pass-2 driver. Owns the position cursor `i`, the literal-run start
        # `ls` and the scan bound `lim = n - 12` (LZ4 rule 3: the last match must
        # start >= 12 bytes before the end). Every phase that completes re-enters
        # this cell at one of its entries, so the whole control flow of pass 2 is
        # readable here.
        # FAN-OUT is the scarce resource in this cell, not arithmetic. A first
        # revision broadcast `i` to three cells and six more values to the
        # formatter and MEASURED 38 instructions against a 31-word cell. The rule
        # that fixed it: a value is written ONCE, to the cell that consumes it, at
        # the moment it changes — never broadcast to everyone who might want it.
        # So VERIFY (which is given `i` anyway, for the `cand < i` test) seeds
        # MATCH and computes the offset itself; MATCH reports its own match
        # length straight to the formatter; and SEQ writes only what nothing else
        # can know: the literal run's bounds.
        seq = CellProgram(
            inputs=[Port("v")],
            outputs=[Port("h_i"), Port("v_i"), Port("scan"), Port("li_zero"),
                     Port("f_end"), Port("f_mend"), Port("go_seq")],
            entries=[EntryPoint("start"), EntryPoint("step"),
                     EntryPoint("decide"), EntryPoint("took")],
            data=[DataWord("one", 1, address=1),
                  DataWord("four", MINMATCH, address=2)],
            state=[StateVar("i", register=3, initial_value=0),
                   StateVar("n", register=4, initial_value=0),
                   StateVar("lim", register=5, initial_value=0)],
            assembly_template=(
                # `n` and `lim` are DATA-only deliveries from INGEST (which derives
                # both LZ4 end-of-block bounds — see its `flush`); no entry is
                # needed for them, because `start` is what INGEST triggers.
                # --- a MATCH sequence finished ------------------------------
                # `took` is placed FIRST so it FALLS THROUGH into `step` instead
                # of branching there. That is one word, and this cell needed
                # exactly one for its input register (the resolver allocates
                # inputs from what is left after data and state, so a full cell
                # fails at build time with "No register space for input 'v'",
                # not with a budget error). Program order is free to change here
                # because SEQ declares NO backward internal jump — both of its
                # `{jump:}` edges target higher-numbered cells (INV-53).
                "took:\n"
                # `v` carries the new position i + k.
                "    MOVE R{state:i}, R{in:v}\n"
                # --- the scan loop -------------------------------------------
                "start:\n"
                "step:\n"
                # while i < lim: scan. `lim` is SIGNED — a payload shorter than 12
                # bytes gives a negative bound and goes straight to the
                # literals-only tail, which IS LZ4 rule 3 at the small end.
                "    CMP R{state:i}, R{state:lim}\n"
                "    BR.GE tail\n"
                "issue:\n"
                # The ONE place the scan is issued. Both the MINMATCH miss and the
                # end of a sequence branch BACK here rather than carrying a second
                # copy — a backward local branch is free and the duplicate cost
                # five words this cell did not have.
                "    MOVE R0, R{state:i}\n"
                "    {write:h_i}\n"                  # hash.i
                "    {write:v_i}\n"                  # verify.i  (which seeds MATCH)
                "    {jump:scan}\n"                  # hash.begin
                "    HALT\n"
                # --- a match was measured: MINMATCH decides ------------------
                "decide:\n"
                # MATCH reports its END cursor ii = i + k, so the run length is
                # k = ii - i. LZ4 rule 1 (MINMATCH): a k below four is not a match
                # at all and those bytes stay literals — the scan just advances.
                "    SUB R{in:v}, R{state:i}\n"
                "    CMP R0, R{data:four}\n"
                "    BR.GE hit\n"
                "    ADD R{state:i}, R{data:one}\n"
                "    MOVE R{state:i}, R0\n"
                "    CMP R{state:i}, R{state:lim}\n"
                "    BR.LT issue\n"
                # --- hand the sequence's END position to FRAME ---------------
                # FRAME owns `ls` (the literal-run start), so only the END is
                # handed over — one write, not two — and FRAME tells the
                # literals-only tail from a real sequence by WHICH ENTRY it lands
                # on rather than by a flag.
                "tail:\n"
                # The literals-only final sequence: literals ls..n and NO match.
                # Setting the cursor to `n` lets this fall THROUGH into `hit`, so
                # there is ONE hand-off rather than two — which is what brought
                # this cell inside budget (MEASURED two words over with the paths
                # separate).
                #
                # FIRST, clear SEAL's offset. A candidate that failed MINMATCH
                # leaves VERIFY's per-hit `li_off` write STALE in SEAL (no
                # sequence ran, so SEAL never consumed and cleared it), and a
                # mid-scan stale value is always overwritten by the next slot
                # outcome — EXCEPT when the failed candidate was the last scan
                # position and control falls straight here. The tail must emit NO
                # offset, so the one place the zero is needed is the one place it
                # is written: once per block, off the hot path. `SUB one, one`
                # loads the zero without a data word.
                "    SUB R{data:one}, R{data:one}\n"
                "    {write:li_zero}\n"               # seal.off = 0
                #
                # The match-length input is forced to n + MINMATCH so FRAME's
                # ``mat = mend - end - 4`` comes out ZERO, which is the token's
                # match nibble for a literals-only sequence. Without this the
                # PREVIOUS match's cursor would still be sitting in `mend` and the
                # final token would claim a match that is not there — a
                # format-legal block that decodes to the wrong bytes.
                "    MOVE R{state:i}, R{state:n}\n"
                "    ADD R{state:n}, R{data:four}\n"
                "    {write:f_mend}\n"
                "hit:\n"
                "    MOVE R0, R{state:i}\n"
                "    {write:f_end}\n"
                "    {jump:go_seq}\n"                # frame.seq
                "    HALT\n"
            ),
        )
        # ----------------------------------------------------------- cell 2 HASH
        # Reads the four bytes at `i`, one panel round trip each, and folds each
        # into the ROLLING hash ``h = h * HASH_MUL + b`` (see :func:`hash4`).
        # Rolling rather than packing is what keeps this cell branch-free per byte:
        # a packed form needs a per-slot branch because a shift count is an
        # instruction-field IMMEDIATE and can never come from a register (INV-34).
        # Measured: the two forms produce byte-identical blocks on all ten gate
        # payloads, so the simpler cell is free.
        # The probe hand-off is a FACE FLIP toward INS (the hash-table port cell,
        # which this cell abuts). The two face codes are NOT extra data words:
        # the flip face (east = 1) is numerically `one` and the resting face
        # (south = 0) is numerically `zero`, so the existing arithmetic constants
        # double as the face codes — the INV-55 `wbk` idiom, which is what makes
        # the 9-instruction probe tail fit a cell that is otherwise exactly full.
        # The coincidence is LOAD-BEARING and therefore GATED: the block's test
        # suite re-derives both codes from the layout and fails if a re-fold
        # breaks the equality (INV-61 clause 3 — never trust a face literal).
        # The router does not read these words; the flip edges are DECLARED in
        # :meth:`emit_faces`, which is authoritative (INV-50 rule 1).
        assert self._face_to(C_HASH, C_INS) == 1, "flip face must equal `one`"
        assert self._resting_face(C_HASH) == 0, "resting face must equal `zero`"
        hashc = CellProgram(
            inputs=[Port("v")],
            outputs=[Port("rd"), Port("ins_h"), Port("ins_v"), Port("ins_go")],
            entries=[EntryPoint("begin"), EntryPoint("byte")],
            data=[DataWord("one", 1, address=1),
                  DataWord("four", MINMATCH, address=2),
                  DataWord("mul", HASH_MUL, address=3),
                  DataWord("zero", 0, address=4)],
            state=[StateVar("i", register=5, initial_value=0),
                   StateVar("c", register=6, initial_value=0),
                   StateVar("h", register=7, initial_value=0)],
            assembly_template=(
                "begin:\n"
                # `i` arrives as DATA from SEQ before this trigger fires.
                "    MOVE R{state:c}, R{data:zero}\n"
                "    MOVE R{state:h}, R{data:zero}\n"
                "next:\n"
                "    ADD R{state:i}, R{state:c}\n"   # ask for hist[i + c]
                "    {write:rd}\n"                   # addr.a
                "    {jump:rd}\n"                    # addr.hist -> a panel read
                "    HALT\n"
                "byte:\n"                            # the panel's answer re-enters
                "    MUL R{state:h}, R{data:mul}\n"
                "    ADD R0, R{in:v}\n"
                "    MOVE R{state:h}, R0\n"
                "    ADD R{state:c}, R{data:one}\n"
                "    MOVE R{state:c}, R0\n"
                "    CMP R{state:c}, R{data:four}\n"
                "    BR.LT next\n"
                # All four folded in: one final multiply, keep the top hash_bits,
                # and hand the WHOLE hash-table transaction to INS — the slot
                # index and the value to insert (i + 1; slot 0 means EMPTY, and
                # position 0 is valid). The insert happens HERE, at the position
                # being encoded (INV-61 clause 5's last hazard): pass 1 must NOT
                # build the table, and a design with no insert at all never finds
                # a single candidate — the slot stays 0 forever, and (measured on
                # the first pass's chip) the scan re-probes an empty table on
                # every position.
                "    MUL R{state:h}, R{data:mul}\n"
                f"    SHR R0, #{hshift}\n"
                "    MOVE [FACE], R{data:one}\n"     # flip EAST (= 1) toward INS
                "    {write:ins_h}\n"                # ins.h = the slot index
                "    ADD R{state:i}, R{data:one}\n"
                "    {write:ins_v}\n"                # ins.v = i + 1
                "    {jump:ins_go}\n"                # ins.go: lookup + insert
                "    MOVE [FACE], R{data:zero}\n"    # restore SOUTH (= 0)
                "    HALT\n"
            ),
        )

        # --------------------------------------------------------- cell 9 INS
        # THE HASH-TABLE PORT — the ONE cell that holds the table's region base,
        # exactly as ADDR is the one cell for the history region. Keeping each
        # region's arithmetic in exactly one cell is what makes the two-region
        # partition checkable (INV-61 clause 1: an overlap returns a wrong
        # answer of the RIGHT length, silently).
        #
        # It abuts the controller from the NORTH and rests facing it, so every
        # word it emits rides its resting face into the port corner — the
        # (0,-1)-relative slot is the only position besides ADDR's that can
        # reach the controller at all, because a word transiting ANY other cell
        # is forwarded on that cell's own face, never into the port corner.
        #
        # IT SPEAKS THE RAW PANEL REGISTER PROTOCOL, TRANSITING THE CONTROLLER.
        # The first design routed the transaction through the controller's
        # `lookup` + `set_addr` + `write` entries — three jumps from one
        # activation. MEASURED on the built chip: the second jump's words
        # arrive while the controller is stalled MID-PORT-BURST on the
        # lookup's held-ack handshake, and on some (deterministic, payload-
        # dependent) alignments the whole port wedges — the panel held an ack
        # forever, the chip drained to QueueEmpty with the scan half done
        # (n=492 died at position 3; n=416 completed; the identical program).
        # The cure is to give the controller NOTHING to execute: this cell
        # emits the panel REGISTER words itself (SRAM_PANEL.md §2-3), hop @2 —
        # one transit THROUGH the port cell (a transiting word is forwarded on
        # the cell's face, south, straight out the port; only a LANDING word
        # executes there) plus the port exit. The port serializes the words in
        # emission order, so the READ trigger always precedes the COMMIT.
        #
        # THE COMMIT IS DEFERRED BY ONE PROBE, and that is load-bearing. A first
        # revision committed right after its own read trigger (R2 + the R0
        # commit, 3 more port words). MEASURED on the built chip: the slot's
        # push-read return races those trailing words — the return reaches
        # VERIFY and (on a HIT) MATCH's first read arrives at the controller
        # while the commit pair is still stalling through the port's held-ack
        # handshake, the two protocol streams interleave at the panel
        # registers, and the port wedges (n=492 died at position 125 with
        # MATCH holding a candidate byte forever; every smaller case passed).
        # Deferring the commit to the START of the next probe makes the cell
        # emit NOTHING after its read trigger, so no INS word is ever in
        # flight when the return spawns new controller traffic — and the
        # PANEL-VISIBLE ORDER IS UNCHANGED: read(a_p), write(a_p, p+1),
        # read(a_p1) is exactly the model's sequence, because the commit still
        # precedes the next read. The final probe's insert never commits;
        # nothing reads the table after the scan, so the output cannot see it.
        #
        # THREE protocol facts carry the cell's small size:
        # * the panel's R5 (address) persists until rewritten and the read-out
        #   descriptors R3/R4 are rewritten by EVERY controller read with the
        #   same build-time constants (`read_wr_desc`/`read_jp_desc`), so this
        #   cell writes neither — every scan position begins with four history
        #   reads through the controller, which seed R3/R4 before the first
        #   probe can trigger;
        # * the deferred commit needs only R5 (address), R2 (payload), R0
        #   (trigger); the new lookup only R5 and R1;
        # * the return lands on RET exactly like every other read — told apart
        #   by COUNT (five reads per position, the fifth is the slot; see
        #   RET). No phase write is needed, which is just as well: this cell
        #   rests facing the controller and has no walk to RET at all.
        #
        # `sa` seeds to the table base and `sv` to 0, so the first activation's
        # "previous commit" writes EMPTY (0) to slot 0 — a semantic no-op.
        _hop2 = self._panel_hop + 1          # transit the port cell + exit
        ins = CellProgram(
            inputs=[Port("h"), Port("v")],
            outputs=[],
            entries=[EntryPoint("go")],
            data=[DataWord("htbase", htb & 0xFFFF, address=1)],
            state=[StateVar("sa", register=2, initial_value=htb & 0xFFFF),
                   StateVar("sv", register=3, initial_value=0)],
            assembly_template=(
                "go:\n"
                # 1. COMMIT the PREVIOUS probe's insert.
                "    MOVE R0, R{state:sa}\n"
                f"    WRITE @{_hop2}, 5\n"           # panel R5 := prev address
                "    MOVE R0, R{state:sv}\n"
                f"    WRITE @{_hop2}, 2\n"           # panel R2 := prev i + 1
                f"    JUMP @{_hop2}, 0\n"            # COMMIT
                # 2. LOOK UP this probe's slot (the OLD value, pre-insert).
                "    ADD R{in:h}, R{data:htbase}\n"  # R0 = the ABSOLUTE address
                "    MOVE R{state:sa}, R0\n"         # remember it for step 1
                f"    WRITE @{_hop2}, 5\n"           # panel R5 := ht_base + h
                f"    JUMP @{_hop2}, 1\n"            # READ -> push lands on RET
                # 3. Remember the value to insert; nothing more leaves the cell.
                "    MOVE R{state:sv}, R{in:v}\n"
                "    HALT\n"
            ),
        )

        # --------------------------------------------------------- cell 3 VERIFY
        # ONE compare engine, reused for BOTH the MINMATCH check and the forward
        # extension (INV-49: check whether a second datapath is really needed
        # before paying for it). The loop walks forward from k = 0 comparing
        # ``hist[cand + k]`` with ``hist[i + k]``; "is this a legal match?" is
        # simply "did k reach MINMATCH?". Keeping the four bytes of the initial
        # check in registers would cost four state words and buy nothing, since the
        # extension re-reads both sides regardless.
        #
        # The two reads of one step are SEQUENTIAL (the panel link is
        # single-outstanding, SRAM_PANEL.md §5): ask for the candidate byte, hold
        # it in `held`, ask for the current byte, then compare. `side` says which
        # of the two the arriving byte is.
        #
        # THE MINMATCH RULE LIVES HERE and nowhere else: a run that stops short of
        # four is reported as a MISS, so those bytes stay literals. That is the
        # LZ4 rule the `match_len_3` mutant breaks.
        # A 39-instruction single cell was MEASURED first (base_addr = -8, i.e.
        # eight words over a 31-word cell) — so it is split in two, which is the
        # INV-46 move: cells are the surplus resource and instruction addresses
        # are the scarce one. The split is along the natural seam, the hash slot
        # test versus the byte compare loop, so neither half needs the other's
        # state.
        verify = CellProgram(
            inputs=[Port("v")],
            outputs=[Port("miss"), Port("arm"), Port("m_off"),
                     Port("m_ii"), Port("li_off"), Port("s_i")],
            entries=[EntryPoint("slot")],
            data=[DataWord("one", 1, address=1),
                  # DERIVED from the layout, never literals (INV-61 clause 3):
                  # the miss hand-back flips WEST onto the abutting SEQ cell.
                  DataWord("face_seq", self._face_to(C_VERIFY, C_SEQ),
                           address=2, is_face=True),
                  DataWord("face_rest", self._resting_face(C_VERIFY),
                           address=3, is_face=True)],
            state=[StateVar("i", register=4, initial_value=0),
                   StateVar("cand", register=5, initial_value=0)],
            assembly_template=(
                "slot:\n"
                # The OLD slot value arrives via RET (which told it apart from a
                # history byte by COUNT — see RET). A slot holds position+1, so 0
                # means EMPTY. cand = slot - 1, and only a STRICTLY EARLIER
                # position is usable — which is ALSO why LZ4's "offset 0 is
                # invalid" can never be violated here: the offset is i - cand and
                # cand < i, so it is at least 1, by construction, not by a check.
                "    SUB R{in:v}, R{data:one}\n"
                "    MOVE R{state:cand}, R0\n"
                "    BR.N no\n"                      # the slot was 0 -> EMPTY
                "    CMP R{state:cand}, R{state:i}\n"
                "    BR.GE no\n"                     # not strictly earlier
                # The OFFSET is computed HERE, the one moment both operands are in
                # one cell, and written straight to the two cells that need it —
                # MATCH (which walks the candidate side as ii - off) and SEAL
                # (which emits it). Routing `cand` to SEQ instead would have made
                # SEQ carry a register it has no other use for.
                "    SUB R{state:i}, R{state:cand}\n"
                "    {write:m_off}\n"                # match.off
                "    {write:li_off}\n"               # seal.off
                "    MOVE R0, R{state:i}\n"
                "    {write:m_ii}\n"                 # match.ii = i (the cursor)
                "    {jump:arm}\n"                   # match.begin
                "    HALT\n"
                "no:\n"
                # NO USABLE CANDIDATE: this is the model's `i += 1; continue`,
                # and BOTH halves must happen HERE. The first pass jumped
                # straight to seq.step with no increment — MEASURED on the built
                # chip: `i` never advanced, the empty slot was re-probed forever
                # (RET dispatched 1596 times while the scan sat on position 0),
                # and the run ended at EventLimit with zero output. The advance
                # is computed from this cell's own copy of `i` and written into
                # SEQ's cursor, then seq.step re-checks the bound — exactly the
                # model's loop.
                #
                # SEAL's offset must ALSO be cleared, or the literals-only tail
                # inherits the offset of a hit whose match failed MINMATCH.
                # `SUB one, one` loads the zero and costs no data word; the write
                # rides the resting face (SEAL is on this cell's eastward walk).
                "    SUB R{data:one}, R{data:one}\n"
                "    {write:li_off}\n"               # seal.off = 0
                "    ADD R{state:i}, R{data:one}\n"
                "    MOVE [FACE], R{data:face_seq}\n"
                "    {write:s_i}\n"                  # seq.i = i + 1 (abutting)
                "    {jump:miss}\n"                  # seq.step: bound-check, scan
                # RESTORE at the TAIL (INV-52 clause 1): the resting face is a
                # contract with every walk that crosses this cell.
                "    MOVE [FACE], R{data:face_rest}\n"
                "    HALT\n"
            ),
        )

        # ---------------------------------------------------------- cell 4 MATCH
        # The compare engine. Walks forward from k = 0 comparing ``hist[cand + k]``
        # with ``hist[i + k]``; the MINMATCH test is simply "did k reach 4?".
        #
        # The two reads of a step are SEQUENTIAL — the panel link is
        # single-outstanding (SRAM_PANEL.md §5) — so the candidate byte is held in
        # `held` while the current byte is fetched, and `side` says which of the
        # two an arriving byte is.
        #
        # TWO LZ4 FORMAT RULES LIVE HERE, and nowhere else:
        #   * MINMATCH — a run that stops short of four is reported as a MISS, so
        #     those bytes stay literals (the `match_len_3` mutant breaks this);
        #   * the LAST FIVE BYTES ARE ALWAYS LITERALS — the walk stops at
        #     ``stop = n - 5``, so no match can consume them (the `final_literals`
        #     mutant breaks this).
        match = CellProgram(
            inputs=[Port("v")],
            outputs=[Port("rd_c"), Port("rd_i"), Port("len"), Port("f_mend")],
            entries=[EntryPoint("begin"), EntryPoint("got")],
            # PACKED with no hole. An earlier revision left address 2 empty
            # (a leftover from a removed word); state auto-allocates above
            # ``max_data_address``, so a hole below it is simply LOST — and this
            # cell was one word short of room for its input register, which the
            # build reports only as "No register space for input 'v'".
            data=[DataWord("one", 1, address=1),
                  DataWord("novalue", EOB_SENTINEL, address=2)],
            state=[StateVar("off", register=3, initial_value=0),
                   StateVar("held", register=4, initial_value=EOB_SENTINEL),
                   StateVar("ii", register=5, initial_value=0),
                   StateVar("stop", register=6, initial_value=0)],
            assembly_template=(
                # `ci` (= cand) and `ii` (= i) are seeded by the two cells that own
                # those positions and then ADVANCE together, one per confirmed
                # byte. Carrying cursors rather than recomputing ``cand + k`` and
                # ``i + k`` on every step is one of the three cuts that brought
                # this cell inside budget (measured: 33 instructions before).
                #
                # The other two: the MINMATCH decision moved to SEQ, and the two
                # reads of one step land on SEPARATE ENTRIES rather than being told
                # apart by a marker register. The latter is what the ADDR cell's
                # dispatch is for — the panel's return entry is fixed for the whole
                # block, so ADDR is the single return point and re-issues to the
                # entry the ISSUING cell asked for.
                "begin:\n"
                "step:\n"
                # LZ4 rule 2: stop == n - 5, so a match never reads the final five
                # bytes of the input.
                "    CMP R{state:ii}, R{state:stop}\n"
                "    BR.GE done\n"
                "    SUB R{state:ii}, R{state:off}\n"
                "    {write:rd_c}\n"                 # addr.a = ii - off (= cand+k)
                "    {jump:rd_c}\n"                  # addr.hist_m
                "    HALT\n"
                # BOTH reads of a step come back to the SAME entry: the block has
                # exactly ONE push-read return descriptor pair, so a per-read entry
                # would cost a phase code, a data word and a WRITE for each — three
                # words this cell does not have (MEASURED). Instead the two are
                # told apart by whether a candidate byte is already HELD. `held` is
                # parked at a value no panel byte can take (bytes are 0..255), so
                # the test needs no extra register.
                "got:\n"
                "    CMP R{state:held}, R{data:novalue}\n"
                "    BR.NZ compare\n"
                "    MOVE R{state:held}, R{in:v}\n"
                "    MOVE R0, R{state:ii}\n"
                "    {write:rd_i}\n"                 # addr.a = ii
                "    {jump:rd_i}\n"                  # addr.hist
                "    HALT\n"
                "compare:\n"
                "    CMP R{state:held}, R{in:v}\n"
                "    MOVE R{state:held}, R{data:novalue}\n"
                "    BR.NZ done\n"                   # the bytes differ: run ends
                # ONE cursor advances, not two: the candidate side is always
                # ``ii - off``, and `off` is constant for the whole match, so
                # carrying a second cursor cost an ADD/MOVE pair per byte that
                # this cell (MEASURED two words over) could not afford. `off` also
                # has to be held somewhere regardless — it is what the formatter
                # emits as the little-endian offset field.
                "    ADD R{state:ii}, R{data:one}\n"
                "    MOVE R{state:ii}, R0\n"
                "    BR.NZ step\n"                   # ii is never 0 here
                "done:\n"
                # `ii` is now i + k. SEQ holds `i`, so it derives the run length
                # itself and applies LZ4 rule 1 (MINMATCH): a k below four is not a
                # match at all and those bytes stay literals. Sending the CURSOR
                # rather than a separate counter drops a whole state register and
                # its increment pair.
                "    MOVE R0, R{state:ii}\n"
                "    {write:f_mend}\n"               # frame.mend (for mat = k - 4)
                "    {write:len}\n"                  # seq.mend
                "    {jump:len}\n"                   # seq.decide
                # No trailing HALT: execution auto-halts past the last
                # instruction. INV-43 — a remote JUMP does not stop local
                # execution, so this is the ONLY place the omission is safe; every
                # other `{jump:}` in this cell is followed by a real HALT.
            ),
        )
        # ---------------------------------------------------------- cell 5 TOKEN
        # The FORMATTER's head. An LZ4 sequence is a fixed ORDER of variable-length
        # parts:
        #
        #   token | literal-length extras | literals | offset LE | matchlen extras
        #
        # and the ORDERING CONSTRAINT is what shapes the whole formatter: the token
        # carries BOTH length nibbles and must go out FIRST, but a nibble is only
        # known once its length is in hand. So this cell computes both nibbles
        # (``nib(v) = min(v, 15)``), emits the token, and hands the two remainders
        # on. A single 62-instruction formatter cell was MEASURED first — twice a
        # cell — so it is three cells, split at the seams the phases already have.
        token = CellProgram(
            inputs=[Port("v")],
            outputs=[Port("out"), Port("go"), Port("m_park")],
            entries=[EntryPoint("seq")],
            data=[DataWord("f15", NIBBLE_ESCAPE, address=1),
                  DataWord("zero", 0, address=2)],
            state=[StateVar("lit", register=3, initial_value=0),
                   StateVar("mat", register=4, initial_value=0),
                   StateVar("hi", register=5, initial_value=0)],
            assembly_template=(
                # `lit` (the literal count) and `mat` (match length - MINMATCH, or
                # a NEGATIVE marker for the literals-only tail) arrive as data.
                #
                # EVERY branch here is preceded by a FLAG-SETTING instruction, and
                # that is load-bearing: on this ISA `MOVE` does NOT touch the
                # flags (PROGRAMMING_GUIDE §4), so a branch after a bare `MOVE`
                # reads whatever the last ALU op left behind. MEASURED on chip:
                # an earlier revision loaded `mat` with `MOVE R0, R{state:mat}`
                # and then branched on N. The stale N came from the preceding
                # `CMP mat, f15`, which is negative for every mat < 15 — so the
                # tail-zeroing path was taken for EVERY ordinary match and the
                # token's match nibble came out 0 in all of them. It cost nothing
                # to fix (`SUB mat, zero` both loads the accumulator and sets the
                # flags) and it is invisible to every model-level gate, because
                # the model has no flags.
                "seq:\n"
                "    CMP R{state:lit}, R{data:f15}\n"
                "    BR.LT lnib\n"
                "    MOVE R0, R{data:f15}\n"
                "    BR.GE lgot\n"
                "lnib:\n"
                "    MOVE R0, R{state:lit}\n"
                "lgot:\n"
                "    SHL R0, #4\n"
                "    MOVE R{state:hi}, R0\n"
                "    CMP R{state:mat}, R{data:f15}\n"
                "    BR.LT mnib\n"
                "    MOVE R0, R{data:f15}\n"
                "    BR.GE mgot\n"
                "mnib:\n"
                # A NEGATIVE `mat` is the literals-only tail: its match nibble is
                # 0, which is also what the format requires (there is no match).
                # `SUB` loads R0 AND sets N from the value itself.
                "    SUB R{state:mat}, R{data:zero}\n"
                "    BR.NN mgot\n"
                "    SUB R{state:mat}, R{state:mat}\n"
                "    MOVE R{state:mat}, R0\n"
                "mgot:\n"
                "    OR R0, R{state:hi}\n"
                "    {write:out}\n"                  # the TOKEN
                "    {jump:out}\n"
                # The MATCH-length remainder is parked in LITS now, not in the run
                # engine: the engine's `rest` is about to be consumed by the
                # LITERAL run, and LITS is what re-arms it after the offset goes
                # out. One value, delivered once, to the cell that will need it.
                "    MOVE R0, R{state:mat}\n"
                "    {write:m_park}\n"               # lits.mat
                "    {jump:go}\n"                    # lenrun.enter (literal run)
                "    HALT\n"
            ),
        )

        # --------------------------------------------------------- cell 6 LENRUN
        # ONE length-continuation engine, used by BOTH length fields (INV-49 —
        # check whether a second datapath is needed before paying for it). A value
        # of 15 or more is encoded as the nibble 15 followed by ``value - 15``
        # written as a run of 255s and a final byte below 255.
        #
        # ONE entry, and NO "where to go next" register: both callers hand back to
        # the SAME place, LITS's `replay`, and LITS already knows which of the two
        # it was from its own state — after the LITERAL run it still has literals
        # to replay (p < end); after the MATCH-LENGTH run it does not (p == end)
        # and its offset has been cleared, so it falls straight through to the end
        # of the sequence.
        #
        # That removed a `nxt` register, its two seeds and a four-word dispatch —
        # NINE words — from a cell that had none to spare, at a cost of one word
        # in LITS. It also removed a REAL BUG the dispatch carried: its `BR.GE`
        # read the flags of a preceding `MOVE`, and on this ISA `MOVE` does not
        # set flags at all, so which caller it believed it had was whatever the
        # last ALU operation happened to leave behind.
        lenrun = CellProgram(
            inputs=[Port("v")],
            outputs=[Port("out"), Port("to_lits")],
            entries=[EntryPoint("enter")],
            data=[DataWord("f15", NIBBLE_ESCAPE, address=1),
                  DataWord("c255", CONT_ESCAPE, address=2)],
            state=[StateVar("rest", register=3, initial_value=0)],
            assembly_template=(
                "enter:\n"
                "    CMP R{state:rest}, R{data:f15}\n"
                "    BR.LT fin\n"
                "    SUB R{state:rest}, R{data:f15}\n"
                "    MOVE R{state:rest}, R0\n"
                "run:\n"
                "    CMP R{state:rest}, R{data:c255}\n"
                "    BR.LT last\n"
                "    MOVE R0, R{data:c255}\n"
                "    {write:out}\n"
                "    {jump:out}\n"
                "    SUB R{state:rest}, R{data:c255}\n"
                "    MOVE R{state:rest}, R0\n"
                "    BR.GE run\n"
                "last:\n"
                "    MOVE R0, R{state:rest}\n"
                "    {write:out}\n"
                "    {jump:out}\n"
                "fin:\n"
                "    {jump:to_lits}\n"               # lits.replay
                "    HALT\n"
            ),
        )

        # ----------------------------------------------------------- cell 7 LITS
        # The literal replay and the offset. The literals are read back out of the
        # panel one at a time — that is the whole reason pass 1 stores the input
        # (see the class docstring): the token has to precede its literals, so the
        # literals must come from somewhere after the fact, and the panel already
        # holds them.
        #
        # LZ4 rule 4 lives here: the offset is 16-bit and LITTLE-ENDIAN, LOW byte
        # first. The `offset_big_endian` mutant swaps exactly those two emissions.
        lits = CellProgram(
            inputs=[Port("v")],
            outputs=[Port("out"), Port("rd"), Port("post")],
            entries=[EntryPoint("replay"), EntryPoint("byte")],
            data=[DataWord("one", 1, address=1)],
            state=[StateVar("p", register=2, initial_value=0),
                   StateVar("end", register=3, initial_value=0)],
            assembly_template=(
                "replay:\n"
                "    CMP R{state:p}, R{state:end}\n"
                "    BR.GE post\n"
                "    MOVE R0, R{state:p}\n"
                "    {write:rd}\n"                   # addr.a = p
                "    {jump:rd}\n"                    # addr.hist_l -> back at `byte`
                "    HALT\n"
                "byte:\n"
                "    MOVE R0, R{in:v}\n"
                "    {write:out}\n"                  # one literal byte
                "    {jump:out}\n"
                "    ADD R{state:p}, R{data:one}\n"
                "    MOVE R{state:p}, R0\n"
                "    BR.NZ replay\n"
                "post:\n"
                "    {jump:post}\n"                  # seal.post
                "    HALT\n"
            ),
        )

        # ---------------------------------------------------------- cell 13 SEAL
        # The tail of a sequence: the LITTLE-ENDIAN offset, the match-length
        # re-arm, and the hand-back that resumes the scan. It is its own cell
        # because folding it into the literal replay measured TEN words over a
        # 32-word cell — INV-46 again, and the split is at a real seam (the replay
        # is a loop over the panel; this is a fixed three-step epilogue).
        #
        # LZ4 rule 4 lives here: the offset is 16-bit and LITTLE-ENDIAN, LOW byte
        # first. The `offset_big_endian` mutant swaps exactly those two emissions.
        seal = CellProgram(
            inputs=[Port("v")],
            outputs=[Port("out"), Port("mrun"), Port("m_rest"), Port("adv")],
            entries=[EntryPoint("post")],
            data=[DataWord("one", 1, address=1),
                  DataWord("ff", 0xFF, address=2),
                  DataWord("zero", 0, address=3)],
            state=[StateVar("off", register=4, initial_value=0),
                   StateVar("sealed", register=5, initial_value=0),
                   StateVar("mat", register=6, initial_value=0)],
            assembly_template=(
                # THREE cases land on this one entry, and the two registers tell
                # them apart:
                #   * off  > 0  — the literals of a MATCH sequence are out. Emit
                #     the offset, clear `off`, re-arm the run engine with the
                #     match-length remainder, and run it.
                #   * off == 0, sealed — the match-length run has handed back.
                #     The sequence is complete; resume the scan.
                #   * off == 0, not sealed — the literals-only TAIL. VERIFY wrote
                #     a zero offset because no candidate was accepted.
                "post:\n"
                "    CMP R{state:off}, R{data:zero}\n"
                "    BR.Z after\n"
                "    AND R{state:off}, R{data:ff}\n"
                "    {write:out}\n"                  # LOW byte  (LITTLE endian)
                "    {jump:out}\n"
                "    SHR R{state:off}, #8\n"
                "    {write:out}\n"                  # HIGH byte
                "    {jump:out}\n"
                # Clearing `off` is what lets the run engine hand back to this
                # same entry: the second visit takes the `after` path.
                "    MOVE R{state:off}, R{data:zero}\n"
                "    MOVE R{state:sealed}, R{data:one}\n"
                "    MOVE R0, R{state:mat}\n"
                "    {write:m_rest}\n"               # lenrun.rest = k - MINMATCH
                "    {jump:mrun}\n"                  # lenrun.enter
                "    HALT\n"
                "after:\n"
                "    CMP R{state:sealed}, R{data:zero}\n"
                "    BR.Z fin\n"
                "    MOVE R{state:sealed}, R{data:zero}\n"
                "    {jump:adv}\n"                   # frame.adv — resume the scan
                "    HALT\n"
                "fin:\n"
                # THE LITERALS-ONLY TAIL ENDS HERE, triggering NOTHING. That is
                # the format's "the block ends right after its final literals",
                # and it is also what terminates the whole encode: nothing hands
                # control back to the scan, so the tail is emitted exactly once.
                "    HALT\n"
            ),
        )
        # ---------------------------------------------------------- cell 8 FRAME
        # The sequence's literal-run bounds, fanned out to the formatter. FRAME
        # owns `ls` (the literal-run start) so SEQ hands over only the END — one
        # write instead of two — and FRAME advances `ls` itself when the sequence
        # completes.
        frame = CellProgram(
            inputs=[Port("v"), Port("mend")],
            outputs=[Port("t_lit"), Port("t_mat"), Port("l_rest"), Port("li_p"),
                     Port("li_end"), Port("go"), Port("s_pos"), Port("s_took")],
            entries=[EntryPoint("seq"), EntryPoint("adv")],
            data=[DataWord("four", MINMATCH, address=1)],
            state=[StateVar("ls", register=2, initial_value=0),
                   StateVar("end", register=3, initial_value=0),
                   StateVar("mpos", register=4, initial_value=0)],
            assembly_template=(
                "seq:\n"
                "    MOVE R{state:end}, R{in:v}\n"
                "    MOVE R{state:mpos}, R{in:mend}\n"
                "    MOVE R0, R{state:ls}\n"
                "    {write:li_p}\n"                 # lits.p   = ls
                "    MOVE R0, R{state:end}\n"
                "    {write:li_end}\n"               # lits.end = end
                # The match-length nibble's raw value is k - MINMATCH, and
                # k = mend - end. MATCH delivers `mend` (its end cursor) here at
                # the same moment it reports to SEQ, so the subtraction happens
                # once, in the cell that is going to emit it.
                "    SUB R{state:mpos}, R{state:end}\n"
                "    SUB R0, R{data:four}\n"
                "    {write:t_mat}\n"                # token.mat = k - 4
                "    SUB R{state:end}, R{state:ls}\n"
                "    {write:t_lit}\n"                # token.lit   = end - ls
                "    {write:l_rest}\n"               # lenrun.rest = the same value
                "    {jump:go}\n"                    # token.seq
                "    HALT\n"
                "adv:\n"
                # A MATCH sequence is fully out (the run engine hands back here
                # after the match-length extras). `mend` is the position just past
                # the match, which is both the next literal-run start and the
                # scan's new cursor.
                #
                # THE LITERALS-ONLY TAIL NEVER LANDS HERE, and that asymmetry is
                # what terminates the block: LITS ends a tail by triggering
                # nothing at all (its `off` is 0). If the tail did hand back,
                # SEQ's `took` would set i = n, the `lim` test would send control
                # straight to `tail` again, and the final literals would be
                # emitted forever.
                "    MOVE R{state:ls}, R{state:mpos}\n"
                "    MOVE R0, R{state:mpos}\n"
                "    {write:s_pos}\n"                # seq.v
                "    {jump:s_took}\n"                # seq.took
                "    HALT\n"
            ),
        )

        # ----------------------------------------------------------- cell 8 ADDR
        # THE HISTORY-REGION PANEL PORT. Every history read (the rolling hash's
        # four bytes, both sides of a compare step, the literal replay) is
        # issued here, and the return PHASE for the two explicitly-phased
        # consumers (MATCH, LITS) is written here — once, where there is room,
        # instead of two instructions and a data word in each of the two
        # tightest cells (INV-46 applied to WORDS).
        #
        # A history read needs no region base: an input position IS its panel
        # address. The hash TABLE's base lives in INS, its own port cell — each
        # region's arithmetic in exactly one cell, so the two-region partition
        # stays checkable (see the class docstring on why an overlap is a
        # silent wrong answer).
        #
        # FACES: rests on the ring and flips toward the controller for each burst,
        # restoring at the TAIL — INV-52 clause 1, whose measured table shows a
        # flip-and-restore is safe even for a cell other walks transit.
        #
        # THE PHASE RACE, measured: `setph` departs on the resting walk (5 hops
        # -> RET, ~75 ns) BEFORE the read is issued, and the read's own path
        # (controller program + panel + return corridor) was measured at
        # ~570 ns on the first pass's chip — the phase always lands first.
        addr = CellProgram(
            inputs=[Port("a")],
            outputs=[Port("rd"), Port("st"), Port("setph")],
            entries=[EntryPoint("store"), EntryPoint("hist"),
                     EntryPoint("hist_m"), EntryPoint("hist_l")],
            data=[
                  # `zero` doubles as PH_HASH, which IS 0.
                  DataWord("zero", PH_HASH, address=1),
                  DataWord("ph_match", PH_MATCH, address=2),
                  DataWord("ph_lit", PH_LIT, address=3),
                  # DERIVED from the layout, never literals — see the OUT cell
                  # for what a stale inherited face constant costs.
                  # ``face_panel`` points at the controller; ``face_ring`` is
                  # this cell's own resting face, restored at the tail.
                  DataWord("face_panel", self._face_to(C_ADDR, C_CTL),
                           address=4, is_face=True),
                  DataWord("face_ring", self._resting_face(C_ADDR),
                           address=5, is_face=True)],
            assembly_template=(
                # THREE read entries — one per KIND of history read — and each
                # costs at most TWO words, because ``SUB const, zero`` both loads
                # the accumulator and sets the flags the local branch needs.
                # Making the CALLERS carry a phase code instead was MEASURED
                # three words over budget in BOTH the match cell and the literal
                # cell. The HASH-TABLE read has no entry here at all: the table
                # lives behind its own port cell (INS), and its return needs no
                # phase because RET tells the fifth read of a position apart by
                # COUNT.
                #
                # (`hist` is the HASH caller's door. It is spelled out rather
                # than shared so that every path sets `ph` explicitly — an entry
                # that inherited a stale phase would send the byte to the wrong
                # cell, and that is a silent wrong answer, not a crash.)
                "store:\n"
                # PASS 1's history write. It comes through THIS cell rather than
                # straight from INGEST for a geometric reason: a cell reaches the
                # controller only if some walk from it gets there, and INGEST is
                # already pinned by the input corridor (it must be reachable down
                # its own column from row 0). Making the panel port the ONE cell
                # that talks to the controller removed the constraint — MEASURED:
                # an exhaustive fold search over rows 9-11 found no placement with
                # both an INGEST->controller walk and every other edge delivering.
                "    MOVE [FACE], R{data:face_panel}\n"
                "    MOVE R0, R{in:a}\n"
                "    {write:st}\n"                   # ctl.data = the byte
                "    {jump:st}\n"                    # ctl.write  (wraddr++)
                "    SUB R{data:zero}, R{data:zero}\n"
                "    BR.Z rest\n"                    # share the face restore
                "hist_m:\n"
                "    SUB R{data:ph_match}, R{data:zero}\n"
                "    BR.NZ go\n"
                "hist_l:\n"
                "    SUB R{data:ph_lit}, R{data:zero}\n"
                "    BR.NZ go\n"
                "hist:\n"
                # A history read needs no base: an input position IS its address.
                # PH_HASH is 0, so the shared `zero` word IS its phase code — one
                # word, two meanings, and the SUB that loads it also clears Z's
                # partner flag so the fallthrough below is unconditional.
                "    SUB R{data:zero}, R{data:zero}\n"
                "go:\n"
                "    {write:setph}\n"                # ret.ph — the return needs it
                "    MOVE [FACE], R{data:face_panel}\n"
                "    MOVE R0, R{in:a}\n"
                "    {write:rd}\n"                   # ctl.data = the address
                "    {jump:rd}\n"                    # ctl.lookup -> a push-read
                "rest:\n"
                # RESTORE at the TAIL (INV-52 clause 1). Both bursts share this
                # one restore — the resting face is a contract with every walk
                # that crosses this cell, and an UNRESTORED face silently deflects
                # them (measured: 0/160 transits delivered).
                "    MOVE [FACE], R{data:face_ring}\n"
                "    HALT\n"
            ),
        )

        # ------------------------------------------------------------ cell 10 RET
        # THE SINGLE RETURN POINT for every push-read, and the reason it has to
        # exist as its own cell: the push-read's destination register and entry are
        # the panel's R3/R4 descriptors, which the controller writes from
        # BUILD-TIME params (SRAM_PANEL.md §3-4). There is exactly ONE such pair
        # for the whole block, so every read in the design comes back to the same
        # register of the same cell at the same entry. Four different consumers
        # (HASH, VERIFY, MATCH, LITS) cannot each be a return target; this cell
        # receives every word and RE-ISSUES it to whichever asked, using the
        # phase ADDR latched when the read went out — except the hash-table
        # slot, which is identified by COUNT (see below).
        # `one` doubles as PH_MATCH, which IS 1 — a plain numeric share (no face
        # semantics), so it is safe under every orientation.
        assert PH_MATCH == 1 and PH_HASH == 0
        ret = CellProgram(
            inputs=[Port("v"), Port("ph")],
            outputs=[Port("to_hash"), Port("to_slot"), Port("to_match"),
                     Port("to_lit")],
            entries=[EntryPoint("word")],
            data=[DataWord("one", PH_MATCH, address=1),
                  DataWord("ph_lit", PH_LIT, address=2),
                  DataWord("five", 5, address=3)],
            state=[StateVar("cnt", register=4, initial_value=5)],
            assembly_template=(
                # THE COUNT PROTOCOL. The block has exactly ONE push-read return
                # descriptor pair, so every read lands here and must be told
                # apart. MATCH and LITS reads carry an explicit phase (ADDR
                # writes `ph` before each — 1 and 2). The scan's own reads carry
                # ph == 0, and are told apart by ARITHMETIC instead of a wire:
                # a scan position is ALWAYS exactly four history reads (the
                # rolling hash) followed by ONE hash-table read, in order, with
                # nothing interleaved (the control flow is a single thread and
                # the panel link is single-outstanding). So the fifth ph==0
                # return IS the slot. This deletes the phase write the table
                # port cannot afford: INS rests facing the controller and has no
                # walk to this cell at all.
                #
                # `cnt` starts at 5 and is reseeded to 5 when the slot is
                # dispatched, so the count is position-local and self-repairing.
                "word:\n"
                "    CMP R{in:ph}, R{data:one}\n"    # PH_MATCH == 1
                "    BR.Z r_match\n"
                "    CMP R{in:ph}, R{data:ph_lit}\n"
                "    BR.Z r_lit\n"
                "    SUB R{state:cnt}, R{data:one}\n"
                "    MOVE R{state:cnt}, R0\n"
                "    MOVE R0, R{in:v}\n"
                "    BR.Z r_slot\n"                  # flags: the SUB (MOVEs keep)
                "    {write:to_hash}\n"              # hash.v
                "    {jump:to_hash}\n"               # hash.byte
                "    HALT\n"
                "r_slot:\n"
                "    MOVE R{state:cnt}, R{data:five}\n"
                "    {write:to_slot}\n"              # verify.v (the OLD slot)
                "    {jump:to_slot}\n"               # verify.slot
                "    HALT\n"
                "r_match:\n"
                "    MOVE R0, R{in:v}\n"
                "    {write:to_match}\n"             # match.v
                "    {jump:to_match}\n"              # match.got
                "    HALT\n"
                "r_lit:\n"
                "    MOVE R0, R{in:v}\n"
                "    {write:to_lit}\n"               # lits.v
                "    {jump:to_lit}\n"                # lits.byte
                "    HALT\n"
            ),
        )
        # ------------------------------------------------------------ cell 11 OUT
        # The block's EGRESS, on a cell of its own — for exactly the reason the
        # decoder's is (INV-46/INV-48). A cell serves ONE direction free and every
        # extra one costs an in-program face flip (2 instructions + 1 `is_face`
        # DataWord). Giving the egress its own cell keeps the formatter's three
        # emitting cells (TOKEN, LENRUN, LITS) on their resting faces: all three
        # write HERE, and this one cell owns the corridor.
        #
        # The `out` hop is AUTHORED (RAW_OUTPUT_HOPS), not a `{write:out}`
        # placeholder: the build's output-port patch rewrites every WRITE/JUMP in
        # an exit cell to the egress route, which would retarget any other
        # hand-off in the same cell. Authoring the hop keeps them apart.
        eh, od, ee = self._emit_hop, self._out_dest, self._emit_entry
        # NOTE the input port is ``w``, not ``b``. The block's EXTERNAL input port
        # is INGEST's ``b``, and the build resolves a chip-port landing by PORT
        # NAME — so a second cell declaring a port called ``b`` makes the landing
        # ambiguous. MEASURED: with this cell's input also named ``b`` the
        # ``x16_in`` landing resolved to the WRONG CELL, and the chip ran to
        # quiescence (``stop_reason == "QueueEmpty"``) with ZERO panel writes
        # committed — pass 1's first store never happened, because pass 1 was
        # never entered. Every port name that the outside world can name must be
        # unique across the block's cells.
        # The RESTING face is DERIVED FROM THE LAYOUT, never a literal.
        #
        # MEASURED: this cell was copied from ``LZ4DecoderBlock``, whose OUT cell
        # rests EAST — and the constant came with it while THIS fold rests it
        # WEST. The cell therefore restored itself to EAST after every egress
        # burst, and SEQ (its EAST neighbour) rests WEST: a HEAD-ON PAIR created
        # by the PROGRAM rather than by the layout, which the static layout check
        # cannot see. On chip the two ping-ponged one WRITE forever, its hop count
        # climbing 22 -> 31, and the run reported ``stop_reason == "Deadlock"``
        # after emitting only the sequence's first byte (INV-52 clause 1 + INV-56
        # clause 3).
        _FACE_CODE = {"south": 0, "east": 1, "west": 2, "north": 3}
        _rest_code = _FACE_CODE[str(self.default_layout()[C_OUT][2])]
        out_cell = CellProgram(
            inputs=[Port("w")],
            outputs=[Port("egress")],
            entries=[EntryPoint("send")],
            data=[DataWord("face_egress", 3, address=1, is_face=True),
                  DataWord("face_rest", _rest_code, address=2, is_face=True)],
            assembly_template=(
                "send:\n"
                "    MOVE [FACE], R{data:face_egress}\n"
                "    MOVE R0, R{in:w}\n"
                f"    WRITE @{eh}, {od}\n"
                f"    JUMP @{eh}, {ee}\n"
                # RESTORE at the TAIL (INV-52 clause 1): the resting face is a
                # contract with every walk that crosses this cell, not only with
                # its own edges, and an UNRESTORED — or WRONGLY restored — face
                # silently deflects them.
                "    MOVE [FACE], R{data:face_rest}\n"
                "    HALT\n"
            ),
        )

        # ------------------------------------------------------------ cell 12 CTL
        from .sram_controller_block import SramControllerBlock
        ctl = SramControllerBlock(self.name + "_ctl", panel_hop=self._panel_hop,
                                  read_wr_desc=self._read_wr_desc,
                                  read_jp_desc=self._read_jp_desc,
                                  addr_base=self._addr_base)
        ctl_cell = ctl.build_cell_programs()[0]

        # PROGRAM ORDER IS A DESIGN LEVER, not bookkeeping (INV-53). Whether an
        # internal jump counts as BACKWARD — and therefore gets re-resolved by
        # the build, which rewrites the source cell's HIGHEST-ADDRESSED jump — is
        # decided entirely by this dict's order. The order is ascending by cell
        # id because the panel template places cells sorted by id and the build
        # binds programs to placed cells BY POSITION (INV-51 clause 2); the two
        # must iterate identically or whole cells get the wrong program with
        # nothing raised.
        by_id = {C_INGEST: ingest, C_SEQ: seq, C_HASH: hashc, C_VERIFY: verify,
                 C_MATCH: match, C_TOKEN: token, C_LENRUN: lenrun, C_LITS: lits,
                 C_FRAME: frame, C_ADDR: addr, C_INS: ins, C_RET: ret,
                 C_OUT: out_cell, C_CTL: ctl_cell, C_SEAL: seal}
        # ASCENDING BY ID, always. The panel template places ``sorted(pos)`` and
        # the build binds programs to placed cells BY POSITION, so this dict and
        # ``default_layout()`` must iterate identically (INV-51 clause 2) — the
        # ids hide a mismatch, and the design then places, routes, builds and
        # DRCs clean while whole cells come out with empty memory.
        return {cid: by_id[cid] for cid in sorted(by_id)}

    # ------------------------------------------------------------ block wiring
    def internal_connections(self) -> List[Tuple[Any, str, Any, str]]:
        """The DATA (``WRITE``) hand-offs between the fifteen cells."""
        return [
            # --- pass 1 -> pass 2 hand-over -----------------------------------
            (C_INGEST, "hist", C_ADDR, "a"),
            (C_INGEST, "setn", C_SEQ, "n"),
            (C_INGEST, "setlim", C_SEQ, "lim"),
            (C_INGEST, "setstop", C_MATCH, "stop"),
            # --- the scan --------------------------------------------------
            (C_SEQ, "h_i", C_HASH, "i"),
            (C_SEQ, "v_i", C_VERIFY, "i"),
            (C_SEQ, "li_zero", C_SEAL, "off"),
            (C_HASH, "rd", C_ADDR, "a"),
            (C_HASH, "ins_h", C_INS, "h"),
            (C_HASH, "ins_v", C_INS, "v"),
            (C_VERIFY, "m_off", C_MATCH, "off"),
            (C_VERIFY, "m_ii", C_MATCH, "ii"),
            (C_VERIFY, "li_off", C_SEAL, "off"),
            (C_VERIFY, "s_i", C_SEQ, "i"),
            (C_MATCH, "rd_c", C_ADDR, "a"),
            (C_MATCH, "rd_i", C_ADDR, "a"),
            (C_MATCH, "len", C_SEQ, "v"),
            (C_MATCH, "f_mend", C_FRAME, "mend"),
            # --- the sequence ----------------------------------------------
            (C_SEQ, "f_end", C_FRAME, "v"),
            (C_SEQ, "f_mend", C_FRAME, "mend"),
            (C_FRAME, "t_lit", C_TOKEN, "lit"),
            (C_FRAME, "t_mat", C_TOKEN, "mat"),
            (C_FRAME, "l_rest", C_LENRUN, "rest"),
            (C_FRAME, "li_p", C_LITS, "p"),
            (C_FRAME, "li_end", C_LITS, "end"),
            # --- the formatter's output rail --------------------------------
            (C_TOKEN, "out", C_OUT, "w"),
            (C_TOKEN, "m_park", C_SEAL, "mat"),
            (C_LENRUN, "out", C_OUT, "w"),
            (C_LITS, "out", C_OUT, "w"),
            (C_LITS, "rd", C_ADDR, "a"),
            (C_SEAL, "out", C_OUT, "w"),
            (C_SEAL, "m_rest", C_LENRUN, "rest"),
            # --- the two panel ports and the single return ------------------
            (C_ADDR, "setph", C_RET, "ph"),
            (C_ADDR, "rd", C_CTL, "data"),
            (C_ADDR, "st", C_CTL, "data"),
            (C_RET, "to_hash", C_HASH, "v"),
            (C_RET, "to_slot", C_VERIFY, "v"),
            (C_RET, "to_match", C_MATCH, "v"),
            (C_RET, "to_lit", C_LITS, "v"),
        ]

    def internal_jumps(self) -> List[Tuple[Any, str, Any, str]]:
        """The TRIGGER (``JUMP``) edges between the fifteen cells.

        Note what is NOT here: the pure state deliveries (``setn``, ``setlim``,
        ``setstop``, ``m_off``, ``m_ii``, ``t_lit``, ``t_mat``, ``l_rest``,
        ``li_p``, ``li_end``, ``li_off``, ``f_mend``, ``setph``) are DATA-only.
        A value that is going to be READ by a later trigger needs no trigger of
        its own, and every trigger omitted is a word saved in the cell that would
        have issued it — which is what INV-53 makes expensive to get wrong: the
        build rewrites a cell's HIGHEST-ADDRESSED jump when it resolves a backward
        edge, so a cell with more jumps than it needs is a cell with more ways to
        be silently mis-patched.
        """
        return [
            (C_INGEST, "hist", C_ADDR, "store"),
            (C_INGEST, "go", C_SEQ, "start"),
            (C_SEQ, "scan", C_HASH, "begin"),
            (C_SEQ, "go_seq", C_FRAME, "seq"),
            (C_HASH, "rd", C_ADDR, "hist"),
            (C_HASH, "ins_go", C_INS, "go"),
            (C_VERIFY, "arm", C_MATCH, "begin"),
            (C_VERIFY, "miss", C_SEQ, "step"),
            (C_MATCH, "rd_c", C_ADDR, "hist_m"),
            (C_MATCH, "rd_i", C_ADDR, "hist_m"),
            (C_MATCH, "len", C_SEQ, "decide"),
            (C_FRAME, "s_took", C_SEQ, "took"),
            (C_FRAME, "go", C_TOKEN, "seq"),
            (C_TOKEN, "out", C_OUT, "send"),
            (C_TOKEN, "go", C_LENRUN, "enter"),
            (C_LENRUN, "out", C_OUT, "send"),
            (C_LENRUN, "to_lits", C_LITS, "replay"),
            (C_LITS, "out", C_OUT, "send"),
            (C_LITS, "rd", C_ADDR, "hist_l"),
            (C_LITS, "post", C_SEAL, "post"),
            (C_SEAL, "out", C_OUT, "send"),
            (C_SEAL, "mrun", C_LENRUN, "enter"),
            (C_SEAL, "adv", C_FRAME, "adv"),
            (C_ADDR, "rd", C_CTL, "lookup"),
            (C_ADDR, "st", C_CTL, "write"),
            (C_RET, "to_hash", C_HASH, "byte"),
            (C_RET, "to_slot", C_VERIFY, "slot"),
            (C_RET, "to_match", C_MATCH, "got"),
            (C_RET, "to_lit", C_LITS, "byte"),
        ]

    def output_cell_id(self) -> Any:
        """The compressed byte stream leaves the OUT cell.

        The default "output leaves the LAST cell" assumption is wrong for this
        block twice over: the last cell by id is not the egress, and one of the
        cells is the embedded SRAM controller, which speaks only to the panel —
        so the default would aim the block's egress at the panel port.
        """
        return C_OUT

    def panel_requirements(self) -> dict:
        """This block needs an SRAM panel, and it uses TWO DISJOINT REGIONS of it.

        ``[0, window_words)`` holds the stored input (address == the byte's
        position) and ``[window_words, window_words + 2**hash_bits)`` holds the
        hash table. Both start EMPTY (``image`` is empty) — this block writes
        everything it later reads, which is what makes the region split
        load-bearing rather than cosmetic. See the class docstring on why an
        overlap is a SILENT wrong answer rather than a crash.

        Five roles, and they are five DIFFERENT cells:

        * ``controller_cell`` (11) sits on ``x1_out``;
        * ``input_cell`` (0) is where the raw stream lands;
        * ``return_cell`` (1) is where every push-read lands — so it must sit on
          the ``x1_in`` row. It is a cell of its own because the block has ONE
          return-descriptor pair and FOUR consumers (see the RET cell);
        * ``panel_client_cell`` (8) is the cell whose WRITE/JUMPs must reach the
          controller;
        * ``output_cell`` (14) owns the EGRESS walk.

        A SIXTH cell also speaks to the controller: INS (9), the hash-table
        port, which abuts the controller from the north and needs no template
        role — its hand-offs are ordinary 1-hop resting writes.
        """
        return {
            "label": (f"LZ4 input window ({self._window_words} x 16b) + hash "
                      f"table ({1 << self._hash_bits} slots)"),
            "image": {},
            "words": self._window_words + (1 << self._hash_bits),
            "controller_cell": C_CTL,
            "input_cell": C_INGEST,
            "return_port": "v",
            "return_cell": C_RET,
            "panel_client_cell": C_ADDR,
            "output_cell": C_OUT,
            "return_entry": "word",
            "self_contained": True,
        }

    def default_layout(self) -> Dict[Any, Tuple[int, int, str]]:
        """The fold. Coordinates are relative; the panel template translates the
        whole thing so the CTL lands on the ``x1_out`` port cell.

        The dict is reindexed against ``build_cell_programs()`` at the end, because
        the router and the build walk the two dicts **in lockstep BY POSITION** —
        the ids hide a mismatch, and a mismatched pairing places, routes, builds
        and DRCs clean while whole cells come out with empty memory (INV-51
        clause 2).
        """
        # THE FOLD IS FREQUENCY-WEIGHTED (INV-61 clause 5): the per-position
        # scan cycle rides short walks, the per-sequence formatter takes the
        # long ones. The 12 datapath cells close one CCW ring —
        #
        #   RET(-6,0) N -> SEQ(-6,-1) E -> VERIFY -> MATCH -> LITS -> OUT ->
        #   HASH(-1,-1) S -> ADDR(-1,0) W -> FRAME -> TOKEN -> SEAL ->
        #   LENRUN(-5,0) W -> RET
        #
        # — a ring on PURPOSE: the scan is a genuine cycle (issue -> read ->
        # return -> dispatch -> issue), so a serpentine's free ends cannot carry
        # it; what killed the first fold was not the ring but hot edges placed
        # the long way round it (plus the three program defects the trace
        # found). Hot placements here: RET's dispatches reach SEQ/VERIFY/MATCH/
        # LITS at 1-4 hops, the read issues reach ADDR at 1-4 hops (HASH abuts
        # it), VERIFY hands the miss back to SEQ by a 1-hop west flip, and
        # ADDR's phase word reaches RET in 5 hops on its resting walk — 75 ns
        # against a panel round trip measured at ~570 ns, so the phase always
        # lands first.
        #
        # CTL is pinned on the x1_out port cell and INS abuts it from the north
        # — the ONLY position besides ADDR's that can reach the controller,
        # since a word transiting any other cell leaves on that cell's face.
        # INGEST tops the input column; OUT's egress flips north to the free
        # (-2,-2) slot. RET is the WESTERNMOST row-0 cell so the x1_in corridor
        # reaches it over free cells only.
        lay = {
            C_INGEST: (-6, -2, "south"),  # the input landing cell
            C_RET: (-6, 0, "north"),      # on the x1_in row; corridor lands here
            C_FRAME: (-2, 0, "west"),
            C_VERIFY: (-5, -1, "east"),
            C_MATCH: (-4, -1, "east"),
            C_SEQ: (-6, -1, "east"),
            C_LITS: (-3, -1, "east"),
            C_HASH: (-1, -1, "south"),
            C_ADDR: (-1, 0, "west"),      # the history-region panel port
            C_INS: (0, -1, "south"),      # the hash-table panel port, on CTL
            C_TOKEN: (-3, 0, "west"),
            C_CTL: (0, 0, "south"),       # pinned on x1_out, facing the port
            C_SEAL: (-4, 0, "west"),
            C_LENRUN: (-5, 0, "west"),
            C_OUT: (-2, -1, "east"),      # the egress (flips north to emit)
        }
        return {cid: lay[cid] for cid in sorted(lay)}

    def emit_faces(self) -> Dict[Tuple[Any, str], Any]:
        """The ports this block emits while FLIPPED, as neighbour CELL IDs.

        A DECLARED emit face is authoritative for the router (INV-50/INV-52
        clause 3): without it the walk starts on the cell's RESTING face and
        sizes the edge along a path the word never takes — which does not raise,
        it just lands the word on the wrong cell.

        The value is a CELL ID, never a compass direction, so the router derives
        the face from the two cells' PLACED coordinates. That is
        orientation-correct by construction (INV-23) rather than needing a
        by-hand D4 pass that a rotation can invalidate.

        Three cells emit while flipped: ADDR flips toward the controller for
        each panel burst; HASH flips toward INS for the hash-table hand-off;
        VERIFY flips toward SEQ for the miss hand-back. Each restores at the
        tail (INV-52 clause 1).
        """
        return {
            (C_ADDR, "rd"): C_CTL,
            (C_ADDR, "st"): C_CTL,
            (C_HASH, "ins_h"): C_INS,
            (C_HASH, "ins_v"): C_INS,
            (C_HASH, "ins_go"): C_INS,
            (C_VERIFY, "s_i"): C_SEQ,
            (C_VERIFY, "miss"): C_SEQ,
        }

    def reset(self):
        pass
