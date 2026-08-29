# SPDX-License-Identifier: GPL-3.0-or-later
"""LZ4DecoderBlock — SRAM-backed LZ4 *block format* decoder (see the class docstring).

Spec: the published **LZ4 Block Format Description** (``doc/lz4_Block_format.md`` in the
lz4/lz4 reference repository, Yann Collet). There is NO stock GNU Radio block for this
(``grc_block`` is ``''``), so the golden reference is a pure-Python transcription of that
document (``verification/tests/lz4_golden.py``), itself cross-checked byte-for-byte
against the reference **C** implementation through its Python binding.

This is the SECOND SRAM-backed DSP block (Varicode was the first, INV-31), and the first
one that addresses a **computed** panel address rather than a fixed table index: the LZ4
history window lives in the panel and every match byte is a push-read at
``(wpos - offset) & 0xFFFF`` — an address the chip derives at run time from its own output
position. See :class:`LZ4DecoderBlock` for the construction.
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

#: ``mat`` is seeded as ``nibble + MINMATCH``, so this value means "the match-length
#: nibble was 15, and a continuation run follows".
MAT_ESCAPE = NIBBLE_ESCAPE + MINMATCH        # 19

#: Panel history-window size. LZ4's offset field is 16 bits, so 65536 words is the
#: EXACT window the format can address; the window index wraps naturally in the
#: cell's 16-bit arithmetic (``wpos - off`` mod 2**16), which is why no explicit
#: masking instruction is needed on chip.
DEFAULT_WINDOW_WORDS = 65536

# --- FSM phase codes (the value carried in the router cell's `st` register) -----
ST_TOKEN = 0     # the next compressed byte is a sequence token
ST_LIT = 1       # the next byte belongs to the literal phase (length or payload)
ST_MATCH = 2     # the next byte belongs to the match phase (offset or length)


def decode_model(src, window_words: int = DEFAULT_WINDOW_WORDS
                 ) -> Tuple[List[int], dict]:
    """The EXACT byte-serial model of the on-chip FSM (the DUT's Python twin).

    This is deliberately written as the cell-level state machine the chip runs — one
    input byte per step, the same ``st``/``lit``/``mat``/``off``/``wpos`` registers,
    the same window semantics — NOT as a second copy of the golden. That it is
    bit-exact to ``lz4_golden.lz4_decompress_block`` on every well-formed block is
    what the gate proves.

    Returns ``(output_bytes, stats)`` with the panel traffic counted:
    ``hist_writes`` (one per emitted byte) and ``push_reads`` (one per match byte).
    """
    mask = window_words - 1
    st = ST_TOKEN
    lit = 0          # NEGATIVE == inside the literal-length continuation
    mat = 0
    off = 0
    nb = 0           # offset bytes still to collect
    ext = False      # inside the match-length continuation
    wpos = 0
    hist: Dict[int, int] = {}
    out: List[int] = []
    stats = {"hist_writes": 0, "push_reads": 0}

    def emit(b: int):
        nonlocal wpos
        out.append(b)
        hist[wpos & mask] = b
        stats["hist_writes"] += 1
        wpos = (wpos + 1) & 0xFFFF

    def run_match():
        nonlocal mat, st
        while mat:
            b = hist.get((wpos - off) & mask, 0)
            stats["push_reads"] += 1
            emit(b)
            mat -= 1
        st = ST_TOKEN

    for raw in src:
        b = int(raw) & 0xFF
        if st == ST_TOKEN:
            mat = (b & 0x0F) + MINMATCH
            n = b >> 4
            if n == NIBBLE_ESCAPE:
                lit, st = -NIBBLE_ESCAPE, ST_LIT
            elif n:
                lit, st = n, ST_LIT
            else:
                nb, off, ext, st = 2, 0, False, ST_MATCH
        elif st == ST_LIT:
            if lit < 0:                                  # length continuation
                lit -= b
                if b != CONT_ESCAPE:
                    lit = -lit
            else:                                        # copy one literal
                emit(b)
                lit -= 1
                if lit == 0:
                    nb, off, ext, st = 2, 0, False, ST_MATCH
        else:                                            # ST_MATCH
            if nb:
                off = ((off >> 8) | (b << 8)) & 0xFFFF   # LITTLE-endian shift-in
                nb -= 1
                if nb == 0:
                    if mat == MAT_ESCAPE:
                        ext = True
                    else:
                        run_match()
            elif ext:
                mat += b
                if b != CONT_ESCAPE:
                    run_match()
    return out, stats


class LZ4DecoderBlock(KyttarBlock):
    """LZ4 **block format** decoder — SRAM-backed (INV-31). No stock GR block.

    One compressed byte per input word in (the ``data_link`` one-byte-per-16-bit-word
    convention); the decompressed byte stream out, one byte per output word.

    Why it needs the panel (INV-29/INV-31)
    --------------------------------------
    LZ4 is a back-reference format: a match copies ``match_len`` bytes from ``offset``
    bytes earlier in the *already decoded output*. The offset field is 16 bits, so a
    conformant decoder must retain a **64 KB history window** — 2048x a 32-word cell.
    The window therefore lives in the SRAM panel, addressed rather than searched, and
    only the FSM stays in cells.

    **What is new here versus Varicode (the first SRAM-backed block).** Varicode reads
    a preloaded ROM at ``address == the input symbol``. This block **writes** the panel
    as it decodes (every emitted byte is appended to the window) and reads it back at a
    **computed** address, ``(wpos - offset) & 0xFFFF``, derived on chip from the block's
    own output position. The window is live read-write state, not a table — which is
    what makes the write-before-read ordering load-bearing (see the overlap note).

    The cell decomposition (7 cells + the panel)
    --------------------------------------------
    The parse FSM does not fit one cell: measured at 33 instructions against a 23-word
    budget once its data words, state and input register are accounted for. It is split
    so **each cell owns exactly the state it uses** and every cross-cell hand-off is a
    resolver-placed ``WRITE``/``JUMP``:

    ===== ============= ==============================================================
    cell  state         role
    ===== ============= ==============================================================
    0     ``st``        **ROUTER** — the landing cell. Steers each compressed byte to
                        the handler for the current phase.
    1     --            **TOKEN** — splits the token's two nibbles, seeds cell 2's
                        ``lit`` and cell 4's ``mat`` *by writing those state registers
                        directly*, and writes cell 0's ``st``.
    2     ``lit``       **LITERAL** — the literal-length continuation and the copy.
    3     ``off``       **OFFSET** — collects the two LITTLE-ENDIAN offset bytes.
          ``nb``
    4     ``mat``       **MATCHLEN** — the match-length continuation; hands the final
          --            run length to cell 5 and fires the first fetch.
    5     ``wpos``      **EMIT** — the single egress + history-write point, the
          ``off``       ``src = wpos - off`` address calculation, AND the match copy
          ``mat``       loop. Also the cell the panel push-read delivers into.
    6     --            **CTL** — the embedded :class:`SramControllerBlock`.
    ===== ============= ==============================================================

    Three encodings keep every cell inside budget, and all three are load-bearing:

    * **``lit`` is held NEGATIVE while inside the literal-length 15-continuation.**
      The phase test is then the sign bit of a value the cell already has (one ``SUB``
      sets ``N``), which removes an ``ext`` state register *and* its ``CMP``.
    * **``mat`` is seeded as ``nibble + MINMATCH``**, so "was the nibble 15?" is a
      single compare against :data:`MAT_ESCAPE` (19) — the +4 is applied once, at the
      token, instead of being carried as a separate flag.
    * **``mat`` doubles as the copy-loop counter AND the literal/match
      discriminator.** ``emit_lit`` zeroes it, so after the shared decrement it is
      ``-1`` (``N`` set) and the emit cell stops; ``emit_mat`` leaves the run length,
      so the same decrement yields ``Z`` on the last byte (finish the sequence) or
      non-zero (fetch the next). One ``SUB`` + two branches replace a whole ``inmatch``
      register and its two setup instructions.

    The match copy is BYTE-BY-BYTE, and that is the correctness argument
    ---------------------------------------------------------------------
    The copy loop lives ENTIRELY inside cell 5 and is closed through the panel: cell 5
    computes ``wpos - off`` and issues the push-read, the panel delivers that one byte
    back into cell 5, cell 5 emits it, **appends it to the window**, bumps ``wpos``,
    decrements ``mat`` and — if more remain — branches back to ``fetch`` locally.
    Because ``wpos`` advances and the byte is written to the window *before* the next
    fetch is issued, a match with ``match_len > offset`` reads back bytes this same
    match produced. That is exactly the overlap the format requires; ``offset == 1``
    degenerates to a byte-run. A block move would be wrong here, and this loop cannot
    be one, since every byte is an independent panel round-trip.

    Keeping the loop local also keeps the block routable: each cell has **at most one
    backward internal edge**, which is what the build's backward-edge patch pass
    supports (it restores the highest-address ``JUMP`` per cell). An earlier revision
    put the run counter in cell 4 and needed a cell-5 -> cell-4 back-trigger *plus* the
    cell-5 -> panel edge; no ordering of the cells satisfies both.

    Panel cost (the number that scopes the encoder)
    -----------------------------------------------
    Per the ``SRAM_PANEL.md`` §2-3 protocol, one **emitted** byte costs a history write
    (``set_addr`` + ``write`` = 3 panel-port words) and one **match** byte costs that
    plus a push-read (4 words out + the 2-word panel-originated return) = **9
    panel-port words per back-reference byte**. The link is single-outstanding
    (``SRAM_PANEL.md`` §5), so these are sequential held-ack handshakes with the
    upstream stalled behind them — the per-sample pacing the panel contract specifies.

    Port topology
    -------------
    The shipped, proven panel topology (``engine/sram_demo.py``,
    ``engine/panel_pnr.py``, and the ``psk31_transceiver``) puts the panel on the **x1
    port pair** — ``x1_out`` carries cells->panel, ``x1_in`` carries the push-read
    return — and duplexes the DATA stream through ``x16_in``/``x16_out``. That is the
    assignment this block uses. It has to be this way round here: a decompressed byte
    is a 16-bit word, and the x1 port is one bit wide.

    Parameters
    ----------
    window_words:
        History-window size in words. Must be a power of two; the LZ4 offset field is
        16 bits so :data:`DEFAULT_WINDOW_WORDS` (65536) is the only value that decodes
        every conformant block. Smaller values are accepted for panel-constrained
        designs and decode exactly any block whose offsets fit the smaller window.
    """
    CATEGORY = "coding"
    TAGS = ["lz4", "decompress", "decoder", "coding", "sram"]
    # The embedded controller authors its own panel-protocol @N hops; the build must
    # not @1-abutment-default them.
    RAW_OUTPUT_HOPS = True

    _interface = BlockInterface(entry_address=1, input_registers=[25],
                                output_registers=[25])

    MINMATCH = MINMATCH
    MAT_ESCAPE = MAT_ESCAPE

    def __init__(self, name: str, window_words: int = DEFAULT_WINDOW_WORDS,
                 panel_hop: int = 1, read_wr_desc: int = 0, read_jp_desc: int = 0,
                 addr_base: int = 0):
        if window_words <= 0 or (window_words & (window_words - 1)):
            raise ValueError(
                f"window_words must be a power of two, got {window_words}")
        if window_words > DEFAULT_WINDOW_WORDS:
            raise ValueError(
                f"window_words > {DEFAULT_WINDOW_WORDS} exceeds the 16-bit LZ4 "
                f"offset field, got {window_words}")
        super().__init__(name, window_words=window_words, panel_hop=panel_hop,
                         read_wr_desc=read_wr_desc, read_jp_desc=read_jp_desc,
                         addr_base=addr_base)
        self._window_words = int(window_words)
        self._panel_hop = int(panel_hop)
        self._read_wr_desc = int(read_wr_desc) & 0xFFFF
        self._read_jp_desc = int(read_jp_desc) & 0xFFFF
        self._addr_base = int(addr_base) & 0xFFFF

    # ------------------------------------------------------------------ shape
    @property
    def cell_count(self) -> int:
        # router + token + literal + offset + matchlen + emit + SRAM controller.
        return 7

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    @property
    def window_words(self) -> int:
        """The history-window size in panel words."""
        return self._window_words

    def panel_requirements(self) -> dict:
        """This block needs an SRAM panel to hold the LZ4 history window.

        Unlike the Varicode ROM the window starts EMPTY and is written as the block
        decodes (``image`` is empty): it is live read-write state, not a preloaded
        table. Cell 6 (the embedded :class:`SramControllerBlock`) sits at the panel's
        ``x1_out`` port; the panel push-reads each match byte back into cell 5's ``b``
        register via ``x1_in`` (the return corridor).
        """
        return {
            "label": f"LZ4 history window ({self._window_words} x 16b)",
            "image": {},
            "words": self._window_words,
            "controller_cell": 6,
            "input_cell": 0,
            "return_port": "b",
            "return_cell": 5,
        }

    # ------------------------------------------------------------- the programs
    def build_cell_programs(self) -> Dict[Any, CellProgram]:
        """The seven cell programs. See the class docstring for the decomposition."""
        # ---------------------------------------------------------- cell 0 ROUTER
        # Holds `st`. Every compressed byte lands here and is steered to the handler
        # for the current phase. A handler that changes the phase WRITEs the new
        # value straight into `st` (no trigger needed — the next input byte is what
        # re-enters this cell), so `st` is the only backward DATA edge in the block.
        router = CellProgram(
            inputs=[Port("byte")],
            outputs=[Port("tok"), Port("lit"), Port("mat")],
            entries=[EntryPoint("feed"), EntryPoint("settoken")],
            data=[DataWord("st_lit", ST_LIT, address=1),
                  DataWord("st_token", ST_TOKEN, address=2)],
            state=[StateVar("st", register=3, initial_value=ST_TOKEN)],
            assembly_template=(
                "feed:\n"
                "    MOVE R0, R{in:byte}\n"
                "    CMP R{state:st}, R{data:st_lit}\n"
                "    BR.LT to_tok\n"                  # st == ST_TOKEN
                "    BR.Z to_lit\n"                   # st == ST_LIT
                "    {write:mat}\n"                   # st == ST_MATCH
                "    {jump:mat}\n"
                "    HALT\n"
                "to_tok:\n"
                "    {write:tok}\n"
                "    {jump:tok}\n"
                "    HALT\n"
                "to_lit:\n"
                "    {write:lit}\n"
                "    {jump:lit}\n"
                "    HALT\n"
                # The emit cell finishes a sequence with a bare trigger (it has no
                # spare word to carry a value), so this entry says "expect a token".
                "settoken:\n"
                "    MOVE R{state:st}, R{data:st_token}\n"
                "    HALT\n"
            ),
        )

        # ----------------------------------------------------------- cell 1 TOKEN
        # Splits the token's nibbles and SEEDS the two counter cells by writing their
        # state registers directly — no local state at all.
        token = CellProgram(
            inputs=[Port("b")],
            outputs=[Port("lit_seed"), Port("mat_seed"), Port("st_set"),
                     Port("arm")],
            entries=[EntryPoint("tok")],
            data=[DataWord("st_lit", ST_LIT, address=1),
                  DataWord("st_match", ST_MATCH, address=2),
                  DataWord("f0", 0x0F, address=3),
                  DataWord("minmatch", MINMATCH, address=4),
                  DataWord("c15", NIBBLE_ESCAPE, address=5),
                  DataWord("neg15", (-NIBBLE_ESCAPE) & 0xFFFF, address=6),
                  DataWord("one", 1, address=7)],
            assembly_template=(
                "tok:\n"
                # match nibble + MINMATCH -> cell 4's `mat`, so MAT_ESCAPE (19) is
                # the "the nibble was 15" sentinel and the +4 is applied once, here.
                "    AND R{in:b}, R{data:f0}\n"
                "    ADD R0, R{data:minmatch}\n"
                "    {write:mat_seed}\n"
                # the literal nibble decides the next phase
                "    SHR R{in:b}, #4\n"
                "    CMP R0, R{data:c15}\n"
                "    BR.Z ext\n"
                "    CMP R0, R{data:one}\n"
                "    BR.LT none\n"
                "    {write:lit_seed}\n"              # 1..14 literals
                "    MOVE R0, R{data:st_lit}\n"
                "    {write:st_set}\n"
                "    HALT\n"
                "ext:\n"
                # NEGATIVE lit == inside the literal-length 15-continuation.
                "    MOVE R0, R{data:neg15}\n"
                "    {write:lit_seed}\n"
                "    MOVE R0, R{data:st_lit}\n"
                "    {write:st_set}\n"
                "    HALT\n"
                "none:\n"
                # zero literals -> straight to the match phase
                "    MOVE R0, R{data:st_match}\n"
                "    {write:st_set}\n"
                "    {jump:arm}\n"
                "    HALT\n"
            ),
        )

        # --------------------------------------------------------- cell 2 LITERAL
        # `lit` NEGATIVE == inside the 15-continuation, so the phase test is the sign
        # bit of a value already in hand — no separate `ext` register.
        literal = CellProgram(
            inputs=[Port("b")],
            outputs=[Port("emit"), Port("st_set"), Port("arm")],
            entries=[EntryPoint("feed")],
            data=[DataWord("one", 1, address=1),
                  DataWord("zero", 0, address=2),
                  DataWord("c255", CONT_ESCAPE, address=3),
                  DataWord("st_match", ST_MATCH, address=4)],
            state=[StateVar("lit", register=5, initial_value=0)],
            assembly_template=(
                "feed:\n"
                "    SUB R{state:lit}, R{data:zero}\n"     # R0 = lit, sets N
                "    BR.NN copy\n"
                # --- a literal-length continuation byte -------------------------
                # lit stays negative while accumulating; a byte != 255 ends the run
                # and the total is negated back to a positive count. EVERY byte read
                # is summed in, including the terminator.
                "    SUB R{state:lit}, R{in:b}\n"
                "    MOVE R{state:lit}, R0\n"
                "    CMP R{in:b}, R{data:c255}\n"
                "    BR.Z done\n"
                "    SUB R{data:zero}, R{state:lit}\n"
                "    MOVE R{state:lit}, R0\n"
                "    HALT\n"
                # --- copy one literal straight through --------------------------
                "copy:\n"
                "    MOVE R0, R{in:b}\n"
                "    {write:emit}\n"
                "    {jump:emit}\n"
                "    SUB R{state:lit}, R{data:one}\n"
                "    MOVE R{state:lit}, R0\n"
                "    BR.NZ done\n"
                # the literals are exhausted: hand the phase to the match side
                "    MOVE R0, R{data:st_match}\n"
                "    {write:st_set}\n"
                "    {jump:arm}\n"
                "done:\n"
                "    HALT\n"
            ),
        )

        # ---------------------------------------------------------- cell 3 OFFSET
        # Collects the two LITTLE-ENDIAN offset bytes with a shift-in from the top:
        # off = (off >> 8) | (b << 8). After two bytes the FIRST byte has been
        # shifted down into bits[7:0] and the SECOND sits in bits[15:8] — exactly
        # little-endian, and exactly what the big-endian mutation breaks.
        offset = CellProgram(
            inputs=[Port("b")],
            outputs=[Port("setoff"), Port("ready")],
            entries=[EntryPoint("feed"), EntryPoint("arm")],
            data=[DataWord("one", 1, address=1),
                  DataWord("two", 2, address=2)],
            state=[StateVar("off", register=3, initial_value=0),
                   StateVar("nb", register=4, initial_value=0)],
            assembly_template=(
                "arm:\n"
                "    MOVE R{state:nb}, R{data:two}\n"
                "    HALT\n"
                "feed:\n"
                "    SHR R{state:off}, #8\n"
                "    MOVE R{state:off}, R0\n"
                "    SHL R{in:b}, #8\n"
                "    OR R0, R{state:off}\n"
                "    MOVE R{state:off}, R0\n"
                "    SUB R{state:nb}, R{data:one}\n"
                "    MOVE R{state:nb}, R0\n"
                "    BR.NZ done\n"
                "    MOVE R0, R{state:off}\n"
                "    {write:setoff}\n"                # cell 5's `off`
                "    {jump:ready}\n"                  # cell 4's `ready`
                "done:\n"
                "    HALT\n"
            ),
        )

        # -------------------------------------------------------- cell 4 MATCHLEN
        # `mat` arrives pre-seeded as nibble+MINMATCH. MAT_ESCAPE (19) means the
        # nibble was 15 and a continuation follows. When the final length is known
        # it is handed to cell 5 (which owns the copy loop) and the first fetch fires.
        matchlen = CellProgram(
            inputs=[Port("b")],
            outputs=[Port("setmat"), Port("fetch")],
            entries=[EntryPoint("ready"), EntryPoint("feed")],
            data=[DataWord("escape", MAT_ESCAPE, address=1),
                  DataWord("c255", CONT_ESCAPE, address=2)],
            state=[StateVar("mat", register=3, initial_value=0)],
            assembly_template=(
                "ready:\n"                            # both offset bytes are in
                "    CMP R{state:mat}, R{data:escape}\n"
                "    BR.Z wait_ext\n"
                "fire:\n"
                "    MOVE R0, R{state:mat}\n"
                "    {write:setmat}\n"                # cell 5's `mat` (run length)
                "    {jump:fetch}\n"                  # cell 5's `fetch`
                "wait_ext:\n"
                "    HALT\n"
                "feed:\n"                             # a match-length extra byte
                "    ADD R{state:mat}, R{in:b}\n"
                "    MOVE R{state:mat}, R0\n"
                "    CMP R{in:b}, R{data:c255}\n"
                "    BR.NZ fire\n"
                "    HALT\n"
            ),
        )

        # ------------------------------------------------------------ cell 5 EMIT
        # The single egress + history-write point, and the WHOLE match copy loop.
        # `mat` is both the run counter and the literal/match discriminator:
        #   emit_lit zeroes it -> after the shared decrement it is -1 (N) -> stop;
        #   emit_mat leaves the run length -> 0 (Z) on the last byte -> finish the
        #   sequence; > 0 -> branch back to `fetch` LOCALLY.
        emit = CellProgram(
            inputs=[Port("b")],
            outputs=[Port("out"), Port("hist_addr"), Port("hist_data"),
                     Port("read"), Port("gohead")],
            entries=[EntryPoint("emit_lit"), EntryPoint("emit_mat"),
                     EntryPoint("fetch")],
            data=[DataWord("one", 1, address=1),
                  DataWord("zero", 0, address=2)],
            state=[StateVar("wpos", register=3, initial_value=0),
                   StateVar("off", register=4, initial_value=0),
                   StateVar("mat", register=5, initial_value=0)],
            assembly_template=(
                # src = wpos - off, mod 2**16 -- the 16-bit register IS the window
                # wrap, which is why no masking instruction is needed.
                "fetch:\n"
                "    SUB R{state:wpos}, R{state:off}\n"
                "    {write:read}\n"
                "    {jump:read}\n"
                "    HALT\n"
                "emit_lit:\n"
                "    MOVE R{state:mat}, R{data:zero}\n"
                "emit_mat:\n"
                "    MOVE R0, R{in:b}\n"
                "    {write:out}\n"
                "    {jump:out}\n"
                # Append to the history window BEFORE the next fetch is issued —
                # this ordering is what makes an overlapping match (match_len >
                # offset; offset == 1 in the limit) read back the bytes this same
                # match just produced.
                "    MOVE R0, R{state:wpos}\n"
                "    {write:hist_addr}\n"
                "    {jump:hist_addr}\n"              # ctl.set_addr(wpos)
                "    MOVE R0, R{in:b}\n"
                "    {write:hist_data}\n"
                "    {jump:hist_data}\n"              # ctl.write(byte)
                "    ADD R{state:wpos}, R{data:one}\n"
                "    MOVE R{state:wpos}, R0\n"
                "    SUB R{state:mat}, R{data:one}\n"
                "    MOVE R{state:mat}, R0\n"         # MOVE keeps the SUB's flags
                "    BR.N done\n"                     # was 0 -> a LITERAL byte
                "    BR.NZ fetch\n"                   # more match bytes to copy
                "    {jump:gohead}\n"                 # sequence done -> expect token
                "done:\n"
                "    HALT\n"
            ),
        )

        # ------------------------------------------------------------- cell 6 CTL
        from .sram_controller_block import SramControllerBlock
        ctl = SramControllerBlock(self.name + "_ctl", panel_hop=self._panel_hop,
                                  read_wr_desc=self._read_wr_desc,
                                  read_jp_desc=self._read_jp_desc,
                                  addr_base=self._addr_base)
        ctl_cell = ctl.build_cell_programs()[0]

        return {0: router, 1: token, 2: literal, 3: offset, 4: matchlen,
                5: emit, 6: ctl_cell}

    # ------------------------------------------------------------ block wiring
    def internal_connections(self) -> List[Tuple[Any, str, Any, str]]:
        """The DATA (``WRITE``) hand-offs between the seven cells."""
        return [
            (0, "tok", 1, "b"),
            (0, "lit", 2, "b"),
            (0, "mat", 3, "b"),
            (1, "lit_seed", 2, "lit"),
            (1, "mat_seed", 4, "mat"),
            (1, "st_set", 0, "st"),
            (2, "emit", 5, "b"),
            (2, "st_set", 0, "st"),
            (3, "setoff", 5, "off"),
            (4, "setmat", 5, "mat"),
            (5, "hist_addr", 6, "data"),
            (5, "hist_data", 6, "data"),
            (5, "read", 6, "data"),
        ]

    def internal_jumps(self) -> List[Tuple[Any, str, Any, str]]:
        """The TRIGGER (``JUMP``) edges between the seven cells.

        Note what is NOT here: the ``st_set`` hand-offs are DATA-only. A phase change
        needs no trigger, because the router is re-entered by the next input byte
        anyway — which is also what keeps every cell to at most ONE backward jump
        (the build's backward-edge pass restores the highest-address ``JUMP`` per
        cell, so a second backward jump in the same cell would be silently lost).
        """
        return [
            (0, "tok", 1, "tok"),
            (0, "lit", 2, "feed"),
            (0, "mat", 3, "feed"),
            (1, "arm", 3, "arm"),
            (2, "emit", 5, "emit_lit"),
            (2, "arm", 3, "arm"),
            (3, "ready", 4, "ready"),
            (4, "fetch", 5, "emit_mat"),
            (5, "hist_addr", 6, "set_addr"),
            (5, "hist_data", 6, "write"),
            (5, "read", 6, "lookup"),
            (5, "gohead", 0, "settoken"),
        ]

    def default_layout(self) -> Dict[Any, Tuple[int, int, str]]:
        """A 4x2 FOLD (serpentine), not a 7x1 strip (INV-8/9/14).

        Row 0 runs east ``router -> token -> literal -> offset``; the fold turns down
        and row 1 runs west ``matchlen -> emit -> ctl``. That puts:

        * the ROUTER (cell 0, the block's external input) and the CTL (cell 6, the
          panel-port cell) both on the west edge, one row apart — the bus-facing edge
          taps the data input and the panel traffic without wrapping;
        * EMIT (cell 5, the block's external output) adjacent to CTL, so each of the
          three per-byte panel hand-offs is a single abutment rather than a corridor;
        * MATCHLEN (cell 4) adjacent to EMIT, so ``fetch`` is one hop.

        4 wide x 2 tall: comfortably inside the <= 8-across convention on this 10x12
        chip, and an EVEN column count so the serpentine returns the last cell to the
        input edge (INV-14).
        """
        return {0: (0, 0, "east"), 1: (1, 0, "east"), 2: (2, 0, "east"),
                3: (3, 0, "south"), 4: (3, 1, "west"), 5: (2, 1, "west"),
                6: (1, 1, "west")}

    # --------------------------------------------------------------- reference
    def process_reference(self, input_bytes) -> np.ndarray:
        """GOLDEN reference: the decompressed byte stream for a compressed block.

        Computed by :func:`decode_model` — the byte-serial twin of the on-chip FSM,
        proven bit-exact against ``lz4_golden.lz4_decompress_block`` (the published
        spec) and against the reference C decoder in
        ``verification/tests/test_lz4_decoder.py``.
        """
        data = np.asarray(input_bytes).reshape(-1).tolist()
        out, _stats = decode_model([int(b) & 0xFF for b in data],
                                   self._window_words)
        return np.asarray(out, dtype=np.int16)

    def panel_cost(self, input_bytes) -> dict:
        """The measured panel-port traffic for a compressed block.

        The controller macro is what speaks to the panel, so a decoder operation
        costs whatever controller entry it invokes (``SRAM_PANEL.md`` §2-4):

        * **history write** = ``set_addr`` (0 panel words — it only latches the
          controller's own address counters) then ``write`` = ``WRITE R5``,
          ``WRITE R2``, ``JUMP R0`` = **3** panel-port words;
        * **push-read** = ``lookup``/``read`` = ``WRITE R3``, ``WRITE R4``,
          ``WRITE R5``, ``JUMP R1`` = **4** words out, plus the panel-originated
          return (a ``WRITE`` + a ``JUMP`` injected into the chip input port) = **2**
          more = **6** words.

        Every emitted byte costs a history write; a match byte costs a history write
        AND a push-read, i.e. **9 panel-port words per back-reference byte** against
        3 for a literal byte. The link is single-outstanding (``SRAM_PANEL.md`` §5),
        so these are sequential held-ack handshakes, not pipelined.
        """
        data = [int(b) & 0xFF for b in np.asarray(input_bytes).reshape(-1)]
        _out, stats = decode_model(data, self._window_words)
        hw, pr = stats["hist_writes"], stats["push_reads"]
        return {
            "history_writes": hw,
            "push_reads": pr,
            "write_words": 3 * hw,
            "read_words": 6 * pr,
            "total_words": 3 * hw + 6 * pr,
            "words_per_match_byte": 9,
            "words_per_literal_byte": 3,
        }

    def reset(self):
        pass
