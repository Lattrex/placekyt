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

    The cell decomposition (8 cells + the panel)
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
    5     ``wpos``      **EMIT** — the history-write point, the ``src = wpos - off``
          ``off``       address calculation, AND the match copy loop. Also the cell
          ``mat``       the panel push-read delivers into.
    7     --            **OUT** — the block's egress. Receives each decoded byte from
                        cell 5 and issues the ``out`` ``WRITE``/``JUMP`` up the output
                        corridor. See "why the egress is its own cell" below.
    6     --            **CTL** — the embedded :class:`SramControllerBlock`.
    ===== ============= ==============================================================

    Why the egress is its own cell (INV-46 / INV-48)
    ------------------------------------------------
    A word leaves on its SOURCE cell's face, and every cell it then arrives at
    forwards it on **that cell's own** resting face, so a cell serves ONE direction
    for free and each extra one costs an in-program FACE FLIP (2 instructions + 1
    ``is_face`` DataWord = 3 words). With the egress in cell 5 that cell had to serve
    THREE directions — the ring-forward to the router, the panel, and the output
    corridor — i.e. 6 words against the 5 it has. Exhaustive search over three
    independent placement windows found no arrangement that avoids it.

    Splitting the egress out is INV-46's "prefer more cells doing less": cell 5 now
    rests on the ring face (its ``gohead`` is free) and flips EAST once for a burst
    that serves cell 7 at hop 1 and the controller at hop 2 — **one flip, one
    direction, three words**. Cell 7 is nearly empty, so the flip its own egress
    needs is free. Cell 7 sits BETWEEN cell 5 and the controller and rests facing
    the controller, so cell 5's panel words transit it (an occupied cell IS
    transparent to a hop-counted word — measured), and its own north flip does not
    disturb them (measured: 180 concurrent transits across a flipping cell, zero
    losses).

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
    back into cell 5, cell 5 hands it to cell 7 for egress, **appends it to the
    window**, bumps ``wpos``,
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
    (``write`` = 3 panel-port words) and one **match** byte costs that plus a push-read
    (4 words out + the 2-word panel-originated return) = **9 panel-port words per
    back-reference byte**. The link is single-outstanding
    (``SRAM_PANEL.md`` §5), so these are sequential held-ack handshakes with the
    upstream stalled behind them — the per-sample pacing the panel contract specifies.

    The per-byte ``set_addr`` an earlier revision issued before every ``write`` is
    GONE, and its removal is proven on silicon: the read and write paths do NOT share
    a counter. ``write`` drives the controller's ``wraddr``; ``lookup``/``read`` drive
    its ``rdaddr``; each re-writes the panel's single R5 address latch from its OWN
    counter before its own trigger, so the interleaving is safe. ``wraddr`` boots at 0
    and this block appends exactly one byte per emitted byte from 0 — the same
    sequence ``wpos`` follows. Dropping it costs nothing and freed the three words the
    emit cell's face flip needed.

    Port topology
    -------------
    The shipped, proven panel topology (``engine/sram_demo.py``,
    ``engine/panel_pnr.py``, and the ``psk31_transceiver``) puts the panel on the **x1
    port pair** — ``x1_out`` carries cells->panel, ``x1_in`` carries the push-read
    return — and duplexes the DATA stream through ``x16_in``/``x16_out``. That is the
    assignment this block uses.

    Note that this is a CONVENTION, not a width constraint. The x1 port is a
    **SERDES**: ``width: 1`` in the chip YAML is a PIN COUNT, and the panel pushes
    full 16-bit words through it (``engine/sram_panel.py`` masks each word ``&
    0xFFFF``; ``model/panel.py`` documents x1 as serial). There is no bit-serial
    bottleneck to design around — the panel is on x1 because the chip has exactly one
    such pair and the panel protocol owns it, leaving x16 free for the data stream.

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
                 addr_base: int = 0, emit_hop: int = 2, out_dest: int = 0,
                 emit_entry: int = 0):
        if window_words <= 0 or (window_words & (window_words - 1)):
            raise ValueError(
                f"window_words must be a power of two, got {window_words}")
        if window_words > DEFAULT_WINDOW_WORDS:
            raise ValueError(
                f"window_words > {DEFAULT_WINDOW_WORDS} exceeds the 16-bit LZ4 "
                f"offset field, got {window_words}")
        super().__init__(name, window_words=window_words, panel_hop=panel_hop,
                         read_wr_desc=read_wr_desc, read_jp_desc=read_jp_desc,
                         addr_base=addr_base, emit_hop=emit_hop,
                         out_dest=out_dest, emit_entry=emit_entry)
        self._window_words = int(window_words)
        self._panel_hop = int(panel_hop)
        self._read_wr_desc = int(read_wr_desc) & 0xFFFF
        self._read_jp_desc = int(read_jp_desc) & 0xFFFF
        self._addr_base = int(addr_base) & 0xFFFF
        # The block is RAW_OUTPUT_HOPS: the emit cell issues its own `out`
        # WRITE/JUMP rather than letting the build patch the cell (which would
        # rewrite the panel hand-offs in the SAME cell — see the emit program).
        # The panel template derives these from the placed geometry.
        self._emit_hop = int(emit_hop)
        self._out_dest = int(out_dest) & 0x1F
        self._emit_entry = int(emit_entry) & 0x1F

    # ------------------------------------------------------------------ shape
    @property
    def cell_count(self) -> int:
        # router + token + literal + offset + matchlen + emit + SRAM controller
        # + the dedicated egress cell (see the class docstring: the emit cell
        # cannot afford a THIRD direction, so the output moves to its own cell).
        return 8

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

        ``self_contained`` selects the panel template shape (``engine/panel_pnr.py``).
        The Varicode/CW RX shape names 3-4 ROLE cells and places ONLY those, deriving
        a read-via-controller indirection and a crossover egress. This block is
        different in kind: it has EIGHT cells, its emit cell drives the controller
        directly (no read indirection), and its egress is its own ``out`` port on a
        cell of its own — so it supplies its whole ``default_layout`` and asks the
        template to lay that fold down with the controller pinned on the panel port.
        See :meth:`default_layout` for the shape and why it is the shape.

        Four roles are named, and they are FOUR DIFFERENT CELLS:

        * ``controller_cell`` (6) sits on ``x1_out``;
        * ``input_cell`` (0) is where the compressed stream lands;
        * ``return_cell`` (5) is where the panel push-read lands — so it must sit on
          the ``x1_in`` row;
        * ``panel_client_cell`` (5) is the cell whose WRITE/JUMPs reach the
          controller, and ``output_cell`` (7) is the cell that owns the EGRESS walk.
          Those two are the same cell in the Varicode shape and DIFFERENT here, which
          is exactly the split this block needed (see the class docstring).
        """
        return {
            "label": f"LZ4 history window ({self._window_words} x 16b)",
            "image": {},
            "words": self._window_words,
            "controller_cell": 6,
            "input_cell": 0,
            "return_port": "b",
            "return_cell": 5,
            # The cell whose panel hand-offs must REACH the controller. Defaults to
            # the return cell; named explicitly here because this block's egress
            # lives on a different cell (below) and the template must check the two
            # walks separately.
            "panel_client_cell": 5,
            # The cell that owns the block's OUTPUT: the template finds the free
            # EGRESS cell on THIS cell's walk and starts the output corridor there.
            # In the Varicode shape it is the return cell; here it is cell 7, which
            # exists precisely so the emit cell does not have to serve a third face.
            "output_cell": 7,
            # The push-read result must kick `emit_mat`, NOT the return cell's
            # lowest-addressed entry: a fetched match byte re-enters the copy loop
            # mid-body (emit the byte, append it, decrement, refetch). Landing on
            # `fetch` instead would re-issue the read and spin.
            "return_entry": "emit_mat",
            "self_contained": True,
        }

    # ------------------------------------------------------------- the programs
    def build_cell_programs(self) -> Dict[Any, CellProgram]:
        """The eight cell programs. See the class docstring for the decomposition."""
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
        #
        # It is also the WHOLE match phase's landing cell: the router steers every
        # ST_MATCH byte here, and after the two offset bytes the phase is not over
        # — a token whose match nibble was 15 is followed by match-length
        # CONTINUATION bytes (``decode_model``'s ``ext`` branch). Those arrive on
        # this same entry with ``nb`` already 0, so the cell must RELAY them to
        # cell 4's `feed` rather than fall through to HALT. Without the relay the
        # continuation byte is silently dropped and the match never fires — which
        # is exactly what happened on chip for every payload whose match is longer
        # than 18 bytes (`b'Q'*40`, `b'abc'*12`, ordinary English text).
        offset = CellProgram(
            inputs=[Port("b")],
            outputs=[Port("setoff"), Port("ready"), Port("ext")],
            entries=[EntryPoint("feed"), EntryPoint("arm")],
            data=[DataWord("one", 1, address=1),
                  DataWord("two", 2, address=2),
                  DataWord("zero", 0, address=3)],
            state=[StateVar("off", register=4, initial_value=0),
                   StateVar("nb", register=5, initial_value=0)],
            assembly_template=(
                "arm:\n"
                "    MOVE R{state:nb}, R{data:two}\n"
                "    HALT\n"
                "feed:\n"
                # nb == 0 -> this is a match-LENGTH continuation byte, not an
                # offset byte: hand it to cell 4 and stay out of the way.
                "    SUB R{state:nb}, R{data:zero}\n"    # R0 = nb, sets Z
                "    BR.Z relay\n"
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
                "    HALT\n"
                "relay:\n"
                "    MOVE R0, R{in:b}\n"
                "    {write:ext}\n"                   # cell 4's `b`
                "    {jump:ext}\n"                    # cell 4's `feed`
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
            # `mat` is pinned to R6, NOT the next free R3, to keep it clear of
            # cell 0's `st` (also R3). The build's backward-edge pass identifies
            # the WRITE to patch by its DESTINATION REGISTER alone
            # (`build._patch_one_handoff`) and takes the lowest-addressed match,
            # so when one cell drives two different cells' registers that happen
            # to share a number — here cell 1 writes cell 4's `mat` AND, later,
            # cell 0's `st` — the backward edge's hop lands on the FORWARD
            # WRITE instead. Distinct numbers make the match unambiguous.
            state=[StateVar("mat", register=6, initial_value=0)],
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
        # The history-write point and the WHOLE match copy loop. `mat` is both the
        # run counter and the literal/match discriminator:
        #   emit_lit zeroes it -> after the shared decrement it is -1 (N) -> stop;
        #   emit_mat leaves the run length -> 0 (Z) on the last byte -> finish the
        #   sequence; > 0 -> branch back to `fetch` LOCALLY.
        #
        # FACES. The cell RESTS on the ring face (`gohead` -> the router, hop 1,
        # free) and FLIPS EAST for one burst that serves BOTH cell 7 (the egress
        # cell, hop 1) and the controller (hop 2, transiting cell 7 — an occupied
        # cell is transparent to a hop-counted word). That is ONE flip: 2
        # instructions + 1 `is_face` DataWord. Resting east instead would break the
        # ring, because cells 1 and 2 reach the router's `st` THROUGH this cell.
        emit = CellProgram(
            inputs=[Port("b")],
            outputs=[Port("hist_data"), Port("read"), Port("out"),
                     Port("gohead")],
            entries=[EntryPoint("emit_lit"), EntryPoint("emit_mat"),
                     EntryPoint("fetch")],
            data=[DataWord("one", 1, address=1),
                  DataWord("zero", 0, address=2),
                  # S=0, E=1, W=2, N=3. Both are `is_face`, so the placer D4-rotates
                  # them with the block (INV-23).
                  DataWord("face_panel", 1, address=3, is_face=True),
                  DataWord("face_ring", 3, address=4, is_face=True)],
            state=[StateVar("wpos", register=5, initial_value=0),
                   StateVar("off", register=6, initial_value=0),
                   StateVar("mat", register=7, initial_value=0)],
            assembly_template=(
                # src = wpos - off, mod 2**16 -- the 16-bit register IS the window
                # wrap, which is why no masking instruction is needed.
                "fetch:\n"
                "    SUB R{state:wpos}, R{state:off}\n"
                "    MOVE [FACE], R{data:face_panel}\n"
                "    {write:read}\n"                  # ctl.data = wpos - off
                "    {jump:read}\n"                   # ctl.lookup
                "    MOVE [FACE], R{data:face_ring}\n"
                "    HALT\n"
                "emit_lit:\n"
                "    MOVE R{state:mat}, R{data:zero}\n"
                "emit_mat:\n"
                "    MOVE [FACE], R{data:face_panel}\n"
                "    MOVE R0, R{in:b}\n"
                "    {write:out}\n"                   # cell 7's `b`
                "    {jump:out}\n"                    # cell 7's `send`
                # R0 still holds the byte: WRITE/JUMP transmit the accumulator, they
                # do not clobber it, so the history write needs no second MOVE.
                # Append to the history window BEFORE the next fetch is issued —
                # this ordering is what makes an overlapping match (match_len >
                # offset; offset == 1 in the limit) read back the bytes this same
                # match just produced. The controller's `write` entry auto-increments
                # its own `wraddr`, which is why no `set_addr` precedes it.
                "    {write:hist_data}\n"
                "    {jump:hist_data}\n"              # ctl.write(byte)
                "    MOVE [FACE], R{data:face_ring}\n"
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

        # ------------------------------------------------------------- cell 7 OUT
        # The block's EGRESS, on a cell of its own. It rests facing the CONTROLLER
        # so that cell 5's panel words transit it untouched, and flips toward the
        # output corridor for its own WRITE/JUMP.
        #
        # The `out` hop is AUTHORED (RAW_OUTPUT_HOPS), not a {write:out} placeholder:
        # the build's output-port patch rewrites every WRITE/JUMP in an exit cell to
        # the egress route, which — when this lived in cell 5 — retargeted the panel
        # hand-offs at the output corridor and killed the panel traffic outright.
        # Authoring the hop keeps the two apart. (Cell 7 has no other WRITE, so the
        # patch would be harmless here; the flag is kept because cell 5 and the
        # embedded controller both still author their own hops.)
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
                "    MOVE [FACE], R{data:face_rest}\n"
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
                5: emit, 6: ctl_cell, 7: out_cell}

    # ------------------------------------------------------------ block wiring
    def internal_connections(self) -> List[Tuple[Any, str, Any, str]]:
        """The DATA (``WRITE``) hand-offs between the eight cells."""
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
            (3, "ext", 4, "b"),
            (4, "setmat", 5, "mat"),
            (5, "out", 7, "b"),
            (5, "hist_data", 6, "data"),
            (5, "read", 6, "data"),
        ]

    def internal_jumps(self) -> List[Tuple[Any, str, Any, str]]:
        """The TRIGGER (``JUMP``) edges between the eight cells.

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
            (3, "ext", 4, "feed"),
            # The FIRST fetch of a match run: cell 4 has the final run length and
            # kicks the emit cell's ``fetch`` — which computes ``wpos - off`` and
            # issues the push-read. It must NOT kick ``emit_mat``: that entry is
            # the RETURN door (the panel's fetched byte re-enters the copy loop
            # mid-body there), and firing it with no byte fetched emits whatever
            # ``b`` still holds — the previous literal. Measured: the first match
            # byte came out as the last literal and every later one was one
            # position early, because the run then continued from a window the
            # spurious byte had already corrupted.
            (4, "fetch", 5, "fetch"),
            (5, "out", 7, "send"),
            (5, "hist_data", 6, "write"),
            (5, "read", 6, "lookup"),
            (5, "gohead", 0, "settoken"),
        ]

    def output_cell_id(self) -> Any:
        """The decompressed byte stream leaves the OUT cell (7).

        Cell 6 is the embedded SRAM controller — it speaks only to the panel, so the
        default "output leaves the last cell" assumption would aim this block's
        egress at the panel port. The `out` port is cell 7's: a cell that exists
        precisely so the emit cell (5) does not have to serve a third direction.
        """
        return 7

    def default_layout(self) -> Dict[Any, Tuple[int, int, str]]:
        """A 3x2 RING over cells 0..5, with the OUT cell and the CTL on the tail.

        Coordinates are relative; the template translates the whole fold so the CTL
        lands on the ``x1_out`` port cell. On ``kyttar_10x12`` that is::

            x:        5         6        7        8        9
            y=10:  LITERAL    TOKEN    ROUTER  (egress)
                   face S     face W   face W   (blank, the corridor turns N here)
            y=11:  OFFSET   MATCHLEN   EMIT      OUT      CTL
                   face E     face E   face N   face E   face S

        **Why a ring.** A word leaves on its SOURCE cell's face and every cell it
        then arrives at forwards it on **that cell's own** face, so each cell has
        exactly ONE free outgoing walk and all of its targets must lie along it. On
        a LINE the phase hand-backs (``1 -> 0`` and ``2 -> 0``, the ``st_set``
        writes) run against the traffic and every arrangement needs a flip
        somewhere it cannot be afforded. A ring closes them for free: the walk from
        any cell continues round to the router. Cells 0..5 sit on the ring in their
        natural order 0->1->2->3->4->5->0, and **thirteen of the block's fifteen
        internal edges deliver on the RESTING face with no flip at all** —
        including ``1 -> 0`` (hop 5) and ``2 -> 0`` (hop 4), which is what the ring
        buys.

        **Why the OUT cell exists.** The remaining two edges are the emit cell's:
        it must reach the controller AND the output corridor, on top of its ring
        face. Three directions is two flips = 6 words against the 5 the cell has —
        the measured wall this block sat behind. Cell 7 takes the egress, and it is
        placed BETWEEN the emit cell and the controller, resting toward the
        controller. So the emit cell's ONE eastward flip serves cell 7 at hop 1
        (the decoded byte) and the controller at hop 2 (the panel words transit
        cell 7, which an occupied cell forwards untouched). One flip, three words,
        two words to spare.

        **The three corridors** the template draws around this fold:

        * ``x16_in`` (0,0) east along row 0, then SOUTH down the router's column,
          landing on the ROUTER — column 7 rows 1..9 are free;
        * ``x1_in`` (0,11) east along row 11, landing on the EMIT cell — the two
          ring cells it transits (OFFSET, MATCHLEN) both rest EAST, so they forward
          the push-read along the corridor instead of deflecting it;
        * the EGRESS from cell 7: north to the free cell at (8,10), up column 8 to
          row 0, then east to ``x16_out`` (9,0).
        """
        return {
            # --- the ring, in order 0 -> 1 -> 2 -> 3 -> 4 -> 5 -> 0 -----------
            0: (2, 0, "west"),      # ROUTER    -> 1 h1, 2 h2, 3 h3
            1: (1, 0, "west"),      # TOKEN     -> 2 h1, 3 h2, 4 h3, 0 h5
            2: (0, 0, "south"),     # LITERAL   -> 3 h1, 5 h3, 0 h4
            3: (0, 1, "east"),      # OFFSET    -> 4 h1, 5 h2
            4: (1, 1, "east"),      # MATCHLEN  -> 5 h1
            5: (2, 1, "north"),     # EMIT      -> 0 h1 (ring); flips E: 7 h1, 6 h2
            # --- the tail: egress cell, then the controller on the panel port --
            7: (3, 1, "east"),      # OUT       -> transparent to 6; flips N to egress
            6: (4, 1, "south"),     # CTL       -> the x1_out panel port
        }

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

        * **history write** = ``write`` = ``WRITE R5``, ``WRITE R2``, ``JUMP R0``
          = **3** panel-port words (no ``set_addr`` precedes it: the controller
          auto-increments its own ``wraddr``, and the read path drives a separate
          ``rdaddr``, so the two never collide — proven on silicon);
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
