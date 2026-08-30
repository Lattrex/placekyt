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
PH_HASH = 0      # a byte for the rolling hash
PH_MATCH = 1     # either side of a compare step (MATCH tells them apart itself)
PH_LIT = 2       # a literal being replayed into the output
PH_HTAB = 3      # a hash-table slot


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

    The cell decomposition (8 cells + the panel)
    --------------------------------------------
    Split so each cell owns exactly the state it uses, per INV-46/INV-49 ("prefer
    more cells doing less"; cells are the surplus resource, words and instruction
    addresses are the scarce ones):

    ===== ================== ==========================================================
    cell  state              role
    ===== ================== ==========================================================
    0     ``pos``            **INGEST** — the landing cell. Pass 1: every input byte
                             goes to ``panel[pos]``. The out-of-band sentinel word
                             ends pass 1 and starts pass 2.
    1     ``i`` ``ls``       **SEQ** — the pass-2 driver: the position cursor, the
          ``lim``           literal-run start, and the ``n - 12`` bound. Dispatches
                             each phase and is re-entered when a phase completes.
    2     ``b0..b3`` ``h``   **HASH** — collects the four bytes at ``i`` and computes
                             :func:`hash4`.
    3     ``cand`` ``k``     **VERIFY** — ONE compare engine, reused for the four-byte
          ``exp`` ``ph``     candidate check AND the forward extension (INV-49).
    4     ``run`` ``nib``    **EMIT** — the formatter: the length-nibble/continuation
          ``p``              encoding, the token, the literal replay cursor, the
                             LITTLE-ENDIAN offset, and the match-length extras.
    5     ``adr``            **ADDR** — the panel address port: adds the region base
                             and issues the controller hand-off. Rests on the ring
                             and flips EAST once per burst (INV-52's bounded rule).
    7     --                 **OUT** — the block's egress, on its own cell.
    6     --                 **CTL** — the embedded :class:`SramControllerBlock`.
    ===== ================== ==========================================================

    The egress is its own cell for exactly the reason the decoder's is (INV-46/48):
    a cell serves ONE direction free and each extra costs a flip (2 instructions +
    1 ``is_face`` DataWord). Cell 7 sits BETWEEN cell 5 and the controller and rests
    facing the controller, so cell 5's panel words transit it untouched while its own
    north flip serves the output corridor — the flip-and-restore that INV-52's
    measured table shows is safe under concurrent transit.

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
        # ingest + seq + hash + verify + emit + addr + controller + egress
        return 8

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
            outputs=[Port("h_i"), Port("v_i"), Port("scan"),
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
                # --- a MATCH sequence finished ------------------------------
                "took:\n"
                # `v` carries the new position i + k. The `lim` compare re-sets the
                # flags the local branch needs — INV-13: this ISA has conditional
                # local branches ONLY, and an unconditional `GOTO` near a
                # `{write}`/`{jump}` miscompiles into an EXTERNAL jump.
                "    MOVE R{state:i}, R{in:v}\n"
                "    CMP R{state:i}, R{state:lim}\n"
                "    BR.GE tail\n"
                "    BR.LT issue\n"
                # No trailing HALT: execution auto-halts once the program counter
                # runs past the last instruction, so the word it would occupy is
                # free — and this cell needed exactly one.
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
        hashc = CellProgram(
            inputs=[Port("v")],
            outputs=[Port("rd"), Port("probe")],
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
                # all four folded in: one final multiply, keep the top hash_bits.
                "    MUL R{state:h}, R{data:mul}\n"
                f"    SHR R0, #{hshift}\n"
                "    {write:probe}\n"                # verify.slot (the table index)
                "    {jump:probe}\n"                 # verify.probe
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
            outputs=[Port("rd"), Port("miss"), Port("arm"), Port("m_off"),
                     Port("m_ii"), Port("li_off")],
            entries=[EntryPoint("probe"), EntryPoint("slot")],
            data=[DataWord("one", 1, address=1),
                  DataWord("zero", 0, address=2)],
            state=[StateVar("i", register=3, initial_value=0),
                   StateVar("cand", register=4, initial_value=0)],
            assembly_template=(
                "probe:\n"
                # The table index arrived as data in `v`; ask ADDR for the slot.
                # ADDR adds the table base and performs the read-then-insert, so
                # this cell never sees an absolute panel address.
                "    MOVE R0, R{in:v}\n"
                "    {write:rd}\n"                   # addr.a = the slot index
                "    {jump:rd}\n"                    # addr.htab (read + insert)
                "    HALT\n"
                "slot:\n"
                # A slot holds position+1, so 0 means EMPTY. cand = slot - 1, and
                # only a STRICTLY EARLIER position is usable — which is ALSO why
                # LZ4's "offset 0 is invalid" can never be violated here: the
                # offset is i - cand and cand < i, so it is at least 1, by
                # construction rather than by a check.
                "    SUB R{in:v}, R{data:one}\n"
                "    MOVE R{state:cand}, R0\n"
                "    BR.N no\n"                      # the slot was 0 -> EMPTY
                "    CMP R{state:cand}, R{state:i}\n"
                "    BR.GE no\n"                     # not strictly earlier
                # The OFFSET is computed HERE, the one moment both operands are in
                # one cell, and written straight to the two cells that need it —
                # MATCH (which walks the candidate side as ii - off) and LITS
                # (which emits it). Routing `cand` to SEQ instead would have made
                # SEQ carry a register it has no other use for.
                "    SUB R{state:i}, R{state:cand}\n"
                "    {write:m_off}\n"                # match.off
                "    {write:li_off}\n"               # lits.off
                "    MOVE R0, R{state:i}\n"
                "    {write:m_ii}\n"                 # match.ii = i (the cursor)
                "    {jump:arm}\n"                   # match.begin
                "    HALT\n"
                "no:\n"
                # No usable candidate. LITS must not emit a stale offset on the
                # NEXT sequence, so the tail's zero is written here too.
                "    MOVE R0, R{data:zero}\n"
                "    {write:li_off}\n"
                "    {jump:miss}\n"                  # seq.step -> i += 1
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
            data=[DataWord("one", 1, address=1),
                  DataWord("novalue", EOB_SENTINEL, address=3)],
            state=[StateVar("off", register=4, initial_value=0),
                   StateVar("held", register=5, initial_value=EOB_SENTINEL),
                   StateVar("ii", register=6, initial_value=0),
                   StateVar("stop", register=7, initial_value=0)],
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
            outputs=[Port("out"), Port("go")],
            entries=[EntryPoint("seq")],
            data=[DataWord("f15", NIBBLE_ESCAPE, address=1)],
            state=[StateVar("lit", register=2, initial_value=0),
                   StateVar("mat", register=3, initial_value=0),
                   StateVar("hi", register=4, initial_value=0)],
            assembly_template=(
                # `lit` (the literal count) and `mat` (match length - MINMATCH, or
                # a NEGATIVE marker for the literals-only tail) arrive as data.
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
                "    MOVE R0, R{state:mat}\n"
                "    BR.N mzero\n"
                "    BR.GE mgot\n"
                "mzero:\n"
                "    SUB R{state:mat}, R{state:mat}\n"
                "mgot:\n"
                "    OR R0, R{state:hi}\n"
                "    {write:out}\n"                  # the TOKEN
                "    {jump:out}\n"
                "    {jump:go}\n"                    # lenrun.lit_run
                "    HALT\n"
            ),
        )

        # --------------------------------------------------------- cell 6 LENRUN
        # ONE length-continuation engine, used by BOTH length fields (INV-49 —
        # check whether a second datapath is needed before paying for it). A value
        # of 15 or more is encoded as the nibble 15 followed by ``value - 15``
        # written as a run of 255s and a final byte below 255.
        #
        # `nxt` says where to go when the run finishes, so the same loop serves the
        # literal-length caller (-> the literal replay) and the match-length caller
        # (-> the end of the sequence).
        lenrun = CellProgram(
            inputs=[Port("v")],
            outputs=[Port("out"), Port("to_lits"), Port("to_end")],
            entries=[EntryPoint("lit_run"), EntryPoint("mat_run")],
            data=[DataWord("f15", NIBBLE_ESCAPE, address=1),
                  DataWord("c255", CONT_ESCAPE, address=2),
                  DataWord("one", 1, address=3),
                  DataWord("zero", 0, address=4)],
            state=[StateVar("rest", register=5, initial_value=0),
                   StateVar("nxt", register=6, initial_value=0)],
            assembly_template=(
                "lit_run:\n"
                "    MOVE R{state:nxt}, R{data:zero}\n"
                "    BR.GE enter\n"
                "mat_run:\n"
                "    MOVE R{state:nxt}, R{data:one}\n"
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
                "    CMP R{state:nxt}, R{data:zero}\n"
                "    BR.NZ toend\n"
                "    {jump:to_lits}\n"               # lits.replay
                "    HALT\n"
                "toend:\n"
                "    {jump:to_end}\n"                # seq.took
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
            outputs=[Port("out"), Port("rd"), Port("mrun")],
            entries=[EntryPoint("replay"), EntryPoint("byte")],
            data=[DataWord("one", 1, address=1),
                  DataWord("ff", 0xFF, address=2),
                  DataWord("zero", 0, address=3)],
            state=[StateVar("p", register=4, initial_value=0),
                   StateVar("end", register=5, initial_value=0),
                   StateVar("off", register=6, initial_value=0)],
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
                # A literals-only tail carries off == 0 and ENDS here — which is
                # exactly the format's "the block ends right after its final
                # literals". A real sequence emits the offset and then hands the
                # match-length remainder back to the run engine.
                "    CMP R{state:off}, R{data:zero}\n"
                "    BR.Z fin\n"
                "    AND R{state:off}, R{data:ff}\n"
                "    {write:out}\n"                  # LOW byte  (little endian)
                "    {jump:out}\n"
                "    SHR R{state:off}, #8\n"
                "    {write:out}\n"                  # HIGH byte
                "    {jump:out}\n"
                # `off` is NOT cleared here: SEQ writes it before every sequence,
                # including the literals-only tail (which writes 0), so a reset
                # would be a redundant word — and this cell is exactly one word
                # from its budget.
                "    {jump:mrun}\n"                  # lenrun.mat_run
                "fin:\n"
                # THE LITERALS-ONLY TAIL ENDS HERE, triggering NOTHING. That is
                # the LZ4 rule "the block ends right after its final literals",
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

        # ----------------------------------------------------------- cell 9 ADDR
        # THE PANEL PORT, and the single RETURN POINT for every push-read.
        #
        # Why one cell must be both: the push-read's destination register and
        # entry are the panel's R3/R4 descriptors, which the controller writes
        # from BUILD-TIME params (SRAM_PANEL.md §3-4). There is exactly ONE pair
        # for the whole block, so every read in the design comes back to the same
        # register of the same cell at the same entry. Four different consumers
        # (HASH, MATCH twice, LITS) therefore cannot each be a return target; ADDR
        # receives every byte and RE-ISSUES it to the consumer that asked, which
        # it remembers in `ph`.
        #
        # It is also where the panel REGION BASE is added, so no other cell ever
        # holds an absolute panel address: `hist` reads are used as-is, `htab`
        # reads add ``ht_base``. Keeping the region arithmetic in one cell is what
        # makes the two-region split checkable in one place (see the class
        # docstring on why an overlap is a silent wrong answer).
        #
        # FACES: rests on the ring and flips toward the controller for each burst,
        # restoring at the TAIL — INV-52 clause 1, whose measured table shows a
        # flip-and-restore is safe even for a cell other walks transit.
        addr = CellProgram(
            inputs=[Port("a")],
            outputs=[Port("rd"), Port("st"), Port("setph")],
            entries=[EntryPoint("store"), EntryPoint("htab"), EntryPoint("hist"),
                     EntryPoint("hist_m"), EntryPoint("hist_l")],
            data=[DataWord("htbase", htb & 0xFFFF, address=1),
                  DataWord("ph_htab", PH_HTAB, address=2),
                  DataWord("ph_hash", PH_HASH, address=3),
                  DataWord("ph_match", PH_MATCH, address=4),
                  DataWord("ph_lit", PH_LIT, address=5),
                  DataWord("zero", 0, address=6),
                  DataWord("face_panel", 1, address=7, is_face=True),
                  DataWord("face_ring", 3, address=8, is_face=True)],
            assembly_template=(
                # FOUR entries — one per KIND of read — and each costs exactly TWO
                # words, because ``SUB const, zero`` both loads the accumulator and
                # sets the flags the local branch needs. Making the CALLERS carry a
                # phase code instead was MEASURED three words over budget in BOTH
                # the match cell and the literal cell: two instructions and a data
                # word each, in the two cells with the least room. Paying it once
                # here, where there is room, is INV-46's rule applied to WORDS
                # rather than cells.
                #
                # (`hist` is the HASH caller's door. It is spelled out rather than
                # shared so that every path sets `ph` explicitly — an entry that
                # inherited a stale phase would send the byte to the wrong cell,
                # and that is a silent wrong answer, not a crash.)
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
                "    MOVE [FACE], R{data:face_ring}\n"
                "    HALT\n"
                "htab:\n"
                # The hash-table probe. This is the ONLY place the table's region
                # base is added, so no other cell in the block ever holds an
                # absolute panel address — which is what makes the two-region split
                # checkable in one place. An overlap between the regions is a
                # SILENT wrong answer (see the class docstring).
                "    ADD R{in:a}, R{data:htbase}\n"
                "    MOVE R{in:a}, R0\n"
                "    SUB R{data:ph_htab}, R{data:zero}\n"
                "    BR.NZ go\n"
                "hist_m:\n"
                "    SUB R{data:ph_match}, R{data:zero}\n"
                "    BR.NZ go\n"
                "hist_l:\n"
                "    SUB R{data:ph_lit}, R{data:zero}\n"
                "    BR.NZ go\n"
                "hist:\n"
                # A history read needs no base: an input position IS its address.
                "    SUB R{data:ph_hash}, R{data:zero}\n"
                "go:\n"
                "    {write:setph}\n"                # ret.ph — the return needs it
                "    MOVE [FACE], R{data:face_panel}\n"
                "    MOVE R0, R{in:a}\n"
                "    {write:rd}\n"                   # ctl.data = the address
                "    {jump:rd}\n"                    # ctl.lookup -> a push-read
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
        # (HASH, MATCH twice, LITS) cannot each be a return target; this cell
        # receives every byte and RE-ISSUES it to whichever asked, using the phase
        # ADDR latched when the read went out.
        ret = CellProgram(
            inputs=[Port("v"), Port("ph")],
            outputs=[Port("to_hash"), Port("to_slot"), Port("to_match"),
                     Port("to_lit")],
            entries=[EntryPoint("word")],
            data=[DataWord("ph_match", PH_MATCH, address=1),
                  DataWord("ph_lit", PH_LIT, address=2),
                  DataWord("ph_htab", PH_HTAB, address=3)],
            assembly_template=(
                "word:\n"
                "    MOVE R0, R{in:v}\n"
                "    CMP R{in:ph}, R{data:ph_match}\n"
                "    BR.Z r_match\n"
                "    CMP R{in:ph}, R{data:ph_lit}\n"
                "    BR.Z r_lit\n"
                "    CMP R{in:ph}, R{data:ph_htab}\n"
                "    BR.Z r_slot\n"
                "    {write:to_hash}\n"              # hash.v
                "    {jump:to_hash}\n"               # hash.byte
                "    HALT\n"
                "r_match:\n"
                "    {write:to_match}\n"             # match.v
                "    {jump:to_match}\n"              # match.got
                "    HALT\n"
                "r_lit:\n"
                "    {write:to_lit}\n"               # lits.v
                "    {jump:to_lit}\n"                # lits.byte
                "    HALT\n"
                "r_slot:\n"
                "    {write:to_slot}\n"              # verify.v (the OLD slot)
                "    {jump:to_slot}\n"               # verify.slot
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
        out_cell = CellProgram(
            inputs=[Port("b")],
            outputs=[Port("egress")],
            entries=[EntryPoint("send")],
            data=[DataWord("face_egress", 3, address=1, is_face=True),
                  DataWord("face_rest", 1, address=2, is_face=True)],
            assembly_template=(
                "send:\n"
                "    MOVE [FACE], R{data:face_egress}\n"
                "    MOVE R0, R{in:b}\n"
                f"    WRITE @{eh}, {od}\n"
                f"    JUMP @{eh}, {ee}\n"
                # RESTORE at the TAIL (INV-52 clause 1): the resting face is a
                # contract with every walk that crosses this cell, not only with
                # its own edges, and an UNRESTORED face silently deflects them.
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

        return {0: ingest, 1: seq, 2: hashc, 3: verify, 4: match, 5: token,
                6: lenrun, 7: lits, 8: frame, 9: addr, 10: ret, 11: out_cell,
                12: ctl_cell}

    # ------------------------------------------------------------ block wiring
    def internal_connections(self) -> List[Tuple[Any, str, Any, str]]:
        """The DATA (``WRITE``) hand-offs between the thirteen cells."""
        return [
            # --- pass 1 -> pass 2 hand-over -----------------------------------
            (0, "hist", 9, "a"),
            (0, "setn", 1, "n"),
            (0, "setlim", 1, "lim"),
            (0, "setstop", 4, "stop"),
            # --- the scan --------------------------------------------------
            (1, "h_i", 2, "i"),
            (1, "v_i", 3, "i"),
            (2, "rd", 9, "a"),
            (2, "probe", 3, "v"),
            (3, "rd", 9, "a"),
            (3, "m_off", 4, "off"),
            (3, "m_ii", 4, "ii"),
            (3, "li_off", 7, "off"),
            (4, "rd_c", 9, "a"),
            (4, "rd_i", 9, "a"),
            (4, "len", 1, "v"),
            (4, "f_mend", 8, "mend"),
            # --- the sequence ----------------------------------------------
            (1, "f_end", 8, "v"),
            (1, "f_mend", 8, "mend"),
            (8, "t_lit", 5, "lit"),
            (8, "t_mat", 5, "mat"),
            (8, "l_rest", 6, "rest"),
            (8, "li_p", 7, "p"),
            (8, "li_end", 7, "end"),
            # --- the formatter's output rail --------------------------------
            (5, "out", 11, "b"),
            (6, "out", 11, "b"),
            (7, "out", 11, "b"),
            (7, "rd", 9, "a"),
            # --- the panel port and its single return -----------------------
            (9, "setph", 10, "ph"),
            (9, "rd", 12, "data"),
            (9, "st", 12, "data"),
            (10, "to_hash", 2, "v"),
            (10, "to_slot", 3, "v"),
            (10, "to_match", 4, "v"),
            (10, "to_lit", 7, "v"),
        ]

    def internal_jumps(self) -> List[Tuple[Any, str, Any, str]]:
        """The TRIGGER (``JUMP``) edges between the thirteen cells.

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
            (0, "hist", 9, "store"),
            (0, "go", 1, "start"),
            (1, "scan", 2, "begin"),
            (1, "go_seq", 8, "seq"),
            (2, "rd", 9, "hist"),
            (2, "probe", 3, "probe"),
            (3, "rd", 9, "htab"),
            (3, "arm", 4, "begin"),
            (3, "miss", 1, "step"),
            (4, "rd_c", 9, "hist_m"),
            (4, "rd_i", 9, "hist_m"),
            (4, "len", 1, "decide"),
            (8, "go", 5, "seq"),
            (5, "out", 11, "send"),
            (5, "go", 6, "lit_run"),
            (6, "out", 11, "send"),
            (6, "to_lits", 7, "replay"),
            (6, "to_end", 8, "adv"),
            (7, "out", 11, "send"),
            (7, "rd", 9, "hist_l"),
            (7, "mrun", 6, "mat_run"),
            (9, "rd", 12, "lookup"),
            (9, "st", 12, "write"),
            (10, "to_hash", 2, "byte"),
            (10, "to_slot", 3, "slot"),
            (10, "to_match", 4, "got"),
            (10, "to_lit", 7, "byte"),
        ]

    def output_cell_id(self) -> Any:
        """The compressed byte stream leaves the OUT cell (11).

        Cell 12 is the embedded SRAM controller — it speaks only to the panel, so
        the default "output leaves the last cell" assumption would aim this block's
        egress at the panel port.
        """
        return 11

    def panel_requirements(self) -> dict:
        """This block needs an SRAM panel, and it uses TWO DISJOINT REGIONS of it.

        ``[0, window_words)`` holds the stored input (address == the byte's
        position) and ``[window_words, window_words + 2**hash_bits)`` holds the
        hash table. Both start EMPTY (``image`` is empty) — this block writes
        everything it later reads, which is what makes the region split
        load-bearing rather than cosmetic. See the class docstring on why an
        overlap is a SILENT wrong answer rather than a crash.

        Five roles, and they are five DIFFERENT cells:

        * ``controller_cell`` (12) sits on ``x1_out``;
        * ``input_cell`` (0) is where the raw stream lands;
        * ``return_cell`` (10) is where every push-read lands — so it must sit on
          the ``x1_in`` row. It is a cell of its own because the block has ONE
          return-descriptor pair and FOUR consumers (see the RET cell);
        * ``panel_client_cell`` (9) is the cell whose WRITE/JUMPs must reach the
          controller;
        * ``output_cell`` (11) owns the EGRESS walk.
        """
        return {
            "label": (f"LZ4 input window ({self._window_words} x 16b) + hash "
                      f"table ({1 << self._hash_bits} slots)"),
            "image": {},
            "words": self._window_words + (1 << self._hash_bits),
            "controller_cell": 12,
            "input_cell": 0,
            "return_port": "v",
            "return_cell": 10,
            "panel_client_cell": 9,
            "output_cell": 11,
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
        lay = {
            0: (-2, -1, "west"),      # INGEST  — the input landing cell
            1: (-9, -1, "south"),     # SEQ
            2: (-5, 0, "east"),       # HASH
            3: (-7, -2, "south"),     # VERIFY
            4: (-3, -1, "north"),     # MATCH
            5: (-9, 0, "east"),       # TOKEN
            6: (-3, -2, "west"),      # LENRUN
            7: (-6, 0, "east"),       # LITS
            8: (-7, -1, "west"),      # FRAME
            9: (-2, 0, "north"),      # ADDR   — the panel client
            10: (-4, 0, "east"),      # RET    — on the x1_in row
            11: (-6, -2, "west"),     # OUT    — the egress
            12: (0, 0, "south"),      # CTL    — pinned on x1_out, facing the port
        }
        order = list(self.build_cell_programs().keys())
        assert set(order) == set(lay), (order, sorted(lay))
        return {cid: lay[cid] for cid in order}

    def reset(self):
        pass
