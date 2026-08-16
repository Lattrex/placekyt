# SPDX-License-Identifier: GPL-3.0-or-later
"""GolayDecoderBlock — SRAM-backed extended Golay (24,12) syndrome decoder.

See the class docstring for the full contract. Every convention here is DERIVED
from :class:`~.golay_encoder_block.GolayEncoderBlock` — the executable pin
(``encode_word()`` / ``_column_mask()``); the B matrix is never re-derived.
"""
import functools
import itertools
from typing import Dict, List, Tuple

import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock
from .golay_encoder_block import GolayEncoderBlock


# ---------------------------------------------------------------------------
# The syndrome + error-pattern LUT model (host-side golden, all derived from
# the GolayEncoderBlock convention pin).
#
# THE CONVENTION (verbatim from the encoder — the pin):
#   wire = d11 .. d0 p11 .. p0 MSB-first (first arriving bit = d11);
#   p11..p0 = m . B (mod 2), B column 0 -> p11 (encoder ``_column_mask``).
#
# Syndrome: with the received halves D (data, bit 11 = d11) and P (parity,
# bit 11 = p11), re-encode D's parity word Q (Q bit 11-j = parity(D &
# column_mask(j)) — the SAME masks, the SAME MSB-first build the encoder
# uses) and s = Q ^ P (12 bits). For a clean codeword s == 0. For a
# received word r = c ^ e the syndrome depends ONLY on the error pattern e
# (linearity), and every error pattern of weight <= 3 has a DISTINCT
# syndrome (min distance 8: e1 ^ e2 of two distinct weight-<=3 patterns has
# weight <= 6 < 8, so they cannot share a coset).
# ---------------------------------------------------------------------------

#: Number of correctable syndromes (error patterns of weight 0..3 over 24
#: bits): 1 + 24 + 276 + 2024.
N_CORRECTABLE_SYNDROMES = 2325
#: Number of POPULATED panel words: correctable syndromes whose DATA-half
#: pattern is non-zero (2325 minus the 299 patterns confined to the parity
#: half, including e = 0). See the storage-format statement in the class
#: docstring.
N_POPULATED_WORDS = 2026
#: The LUT spans the full 12-bit syndrome space.
LUT_ADDR_SPACE = 4096


def parity_word_of(d: int) -> int:
    """Re-encoded parity word Q of the 12-bit data half ``d`` — the encoder's
    own column masks, MSB-first (Q bit 11-j = parity(d & column_mask(j)))."""
    q = 0
    for j in range(12):
        q = (q << 1) | (bin(d & GolayEncoderBlock._column_mask(j)).count("1") & 1)
    return q & 0xFFF


def syndrome_of(d: int, p: int) -> int:
    """The 12-bit syndrome of received halves (d, p): s = Q(d) ^ p."""
    return (parity_word_of(d & 0xFFF) ^ p) & 0xFFF


def split_bits24(bits) -> Tuple[int, int]:
    """(D, P) integers from 24 wire bits (bit 0 of the list = d11 — the pin's
    MSB-first arrival order)."""
    b = [int(x) & 1 for x in bits]
    if len(b) != 24:
        raise ValueError(f"need 24 bits, got {len(b)}")
    d = p = 0
    for k in range(12):
        d = (d << 1) | b[k]
        p = (p << 1) | b[12 + k]
    return d, p


@functools.lru_cache(maxsize=1)
def _lut_pairs_cached() -> Tuple[Tuple[int, int], ...]:
    pairs: List[Tuple[int, int]] = []
    seen: Dict[int, int] = {}
    for w in (1, 2, 3):
        for pos in itertools.combinations(range(24), w):
            e_d = e_p = 0
            for i in pos:
                if i < 12:
                    e_d |= 1 << (11 - i)
                else:
                    e_p |= 1 << (23 - i)
            s = syndrome_of(e_d, e_p)
            assert s != 0 and s not in seen, \
                "syndrome collision — impossible for weight<=3 (d_min 8)"
            seen[s] = 1
            if e_d:
                pairs.append((s, e_d))
    assert len(seen) == N_CORRECTABLE_SYNDROMES - 1  # minus the zero pattern
    assert len(pairs) == N_POPULATED_WORDS
    return tuple(pairs)


def error_lut_pairs() -> List[Tuple[int, int]]:
    """The ``(syndrome, data_half_error_pattern)`` pairs to store in the panel
    (sparse ``set_addr``-per-pair load — syndromes are scattered over the
    4096-address space). Only pairs with a NON-ZERO data-half pattern are
    stored; see the storage-format statement in the class docstring."""
    return list(_lut_pairs_cached())


def sram_error_image() -> Dict[int, int]:
    """The sparse panel image {syndrome: e_d} (2026 populated of 4096)."""
    return dict(_lut_pairs_cached())


@functools.lru_cache(maxsize=1)
def correctable_syndromes() -> frozenset:
    """ALL correctable syndromes (weight 0..3 error patterns), including 0 and
    the parity-only ones that are deliberately NOT stored."""
    out = {0}
    for w in (1, 2, 3):
        for pos in itertools.combinations(range(24), w):
            e_d = e_p = 0
            for i in pos:
                if i < 12:
                    e_d |= 1 << (11 - i)
                else:
                    e_p |= 1 << (23 - i)
            out.add(syndrome_of(e_d, e_p))
    return frozenset(out)


def decode_word_from_sram(image: Dict[int, int], bits24) -> List[int]:
    """The SRAM-backed decode MODEL for one 24-bit group — the exact on-chip
    path: split halves, syndrome, ONE panel lookup (sparse default 0 == no
    data-half correction), emit the 12 corrected data bits MSB-first."""
    d, p = split_bits24(bits24)
    e_d = image.get(syndrome_of(d, p), 0) & 0xFFF
    c = (d ^ e_d) & 0xFFF
    return [(c >> (11 - i)) & 1 for i in range(12)]


def decode_stream_from_sram(image: Dict[int, int], bits) -> List[int]:
    """Stream model: group the bit stream 24 at a time (trailing partial group
    dropped — the pack floor, mirroring the encoder), decode each group."""
    b = [int(x) & 1 for x in bits]
    out: List[int] = []
    for g in range(len(b) // 24):
        out.extend(decode_word_from_sram(image, b[24 * g: 24 * g + 24]))
    return out


class GolayDecoderBlock(KyttarBlock):
    """Extended binary Golay (24,12) hard-decision syndrome DECODER — SRAM-backed
    (INV-31). No GNU Radio counterpart (gr-fec has no Golay factory); the golden
    is the GolayEncoderBlock convention pin + an independent nearest-codeword
    decoder in the test.

    THE CONVENTION PIN (verbatim from GolayEncoderBlock — this block is built
    against the encoder's executable ``encode_word()`` / ``_column_mask()``):

        codeword layout MSB-first on the wire = d11 d10 .. d0 p11 p10 .. p0,
        first arriving bit = d11; p11..p0 = m . B (mod 2), B column 0 -> p11.

    Bit stream in, bit stream out (one 0/1 word per sample, only the input LSB
    is read — the Pack/Unpack convention). The block consumes 24 received bits
    and emits the 12 CORRECTED data bits MSB-first (rate-COMPRESSING 24:12; a
    trailing partial group of fewer than 24 bits is not emitted). Corrects any
    error pattern of weight <= 3 per codeword (min distance 8).

    Decode path: syndrome s = Q(D) ^ P, where Q re-encodes the received data
    half with the ENCODER's OWN column masks (the parity-mask P-flag idiom,
    identical LOAD-table loop) and P is the received parity half; then ONE
    SRAM-panel lookup at address s yields the data-half error pattern, and the
    output is D ^ e_d.

    STORAGE FORMAT — THE DESIGN CALL, STATED LOUDLY
    -----------------------------------------------
    The manifest offered two layouts for the 24-bit error pattern (two panel
    words at 2s/2s+1, or a packed descriptor). This block stores **ONE panel
    word per populated syndrome: the 12-bit DATA-HALF error pattern e_d**
    (bit 11 <-> d11), address == the 12-bit syndrome integer (bit 11 <-> the
    p11-column check). The parity-half pattern e_p is DEAD STATE here: the
    block emits only the corrected data bits, so correcting the received
    parity half would change nothing observable. One word per syndrome halves
    the panel image, needs no 2s/2s+1 double-read sequencing, and makes the
    lookup a single push-read per codeword. 2026 words are populated (the
    2324 non-zero weight-<=3 patterns minus the 298 confined to the parity
    half) of the 4096-address space.

    THE STORED-VALUE-CAN-BE-0 TRAP (the VaricodeDecoder CHAR_OFFSET lesson),
    VERIFIED EXPLICITLY: an unpopulated panel address reads 0, and with THIS
    storage format a value of 0 is also what three legitimate cases need:

      * s == 0 (clean codeword): e = 0, no correction;
      * s != 0 but the correctable error is confined to the PARITY half
        (e_d == 0, 298 such syndromes): the data half is already correct;
      * s outside the correctable set (>= 4 errors): documented passthrough
        (see the known limit below).

    All three collide on the read value 0 AND all three require the SAME
    action — XOR nothing into D. The collision is therefore semantically
    harmless and NO offset (CHAR_OFFSET) is needed; entries with e_d == 0 are
    deliberately not stored. This reasoning is gated by dedicated tests
    (parity-only error patterns decode exactly; their syndromes are proven
    absent from the image). Consequence (also stated loudly): s == 0 does NOT
    branch around the panel — EVERY codeword takes the SAME single lookup
    path (uniform timing, no clean/dirty fork in the correct cell); address 0
    is guaranteed unpopulated (syndrome 0 <-> e = 0, never stored), so the
    clean-codeword read returns 0 == no correction. On a SHARED panel the LUT
    must own its full 4096-address region (``addr_base``-aligned) — a foreign
    table word inside the region would masquerade as an error pattern.

    KNOWN LIMIT — uncorrectable (>= 4 errors), documented honestly: a
    weight-4 error pattern can NEVER alias a weight-<=3 syndrome (their XOR
    would be a codeword of weight <= 7 < 8), so exactly-4-error words always
    read an unpopulated address and PASS THE RECEIVED DATA HALF THROUGH
    unchanged — no miscorrection, but any of the 4 errors that fell in the
    data half remain in the output (detected-uncorrectable is not signalled
    on a port). For >= 5 errors the syndrome CAN alias a correctable one and
    the block may miscorrect into a wrong codeword — the standard bounded-
    distance behaviour of any t=3 Golay decoder. Both behaviours are gated.

    Datapath (SEVEN cells — the SRAM_PANEL.md §6 recipe on the encoder's
    LOAD-table spine):

      cell 0 ``pack``   — 24-bit group accumulator (the encoder pack idiom,
        depth 24): bits shift MSB-first into ``w``; at count==12 the data half
        is latched into ``d``; at count==24 both halves forward + trigger.
        Stale bits climb above bit 11 of both halves and every downstream read
        is masked (the masked-read invariant).
      cells 1-3 ``syn1/syn2/syn3`` — the re-encode parity loop, the ENCODER's
        LOAD-table P-flag loop VERBATIM (down-counter as LOAD address), split
        5/4/3 columns. The split is budget-forced: each cell forwards THREE
        words (D, P, partial Q) so with the 30-word cell budget (data + state
        + instructions + non-R0 input registers) the mask tables cap at
        5/4/3 (syn2 pays one extra entry MOVE for the partial-Q copy; syn3
        trades a mask slot for the final XOR). syn3 computes s = Q ^ P and
        forwards only (D, s). All non-R0 input registers sit below
        31 - n_instructions AND outside the LOAD table range (the encoder's
        silent-collision lesson — gated by an explicit register-layout test).
      cell 4 ``correct`` — masks s to 12 bits (AND 0xFFF kills the stale
        bits), forwards D to ``emit``, then speaks the panel read protocol:
        WRITE s -> ``read_dest`` @``read_addr_hop``, JUMP ``read_entry`` (raw
        panel R5/R1 by default, or the companion controller's data register +
        ``lookup`` entry in the shared-panel template mode). SAME path for
        every codeword — no s == 0 branch (see above).
      cell 5 ``emit``   — the panel push-read lands e_d in R{ew} (NEVER R0 —
        the push-read landing rule) and kicks this entry: out = (D ^ e_d)
        peeled 12 bits MSB-first (SHR #11 + AND 1 masks the stale bits),
        counted-loop burst out the block egress. Exit cell: conditional
        branches only (no GOTO — the exit-cell rewrite rule).
      cell 6 — the companion :class:`SramControllerBlock` (LOAD phase:
        sparse ``set_addr``-per-pair streaming of :func:`error_lut_pairs` in
        ONE persistent chip run — controller wraddr is cell state; and the
        template-mode per-read ``lookup`` relay carrying its own R3/R4
        descriptors).

    Per-sample panel contract: the server forces per-sample pacing for panel
    designs, so this block's saturation-coverage entry is NEEDS_BESPOKE with
    that reason (its own gate drives the real panel round-trip per codeword).

    Raw-word bit streams in and out (0/1 words), NOT Q15 — the comparison is
    BIT-EXACT (metric DECISION, tolerance 0).

    Parameters (panel plumbing only — the (24,12) code itself is fixed):
      panel_hop, read_addr_hop, read_dest, read_entry, read_wr_desc,
      read_jp_desc — see ``__init__``.
    """
    CATEGORY = "fec"
    TAGS = ["golay", "fec", "decoder", "block_code", "data_link", "sram"]
    # Authors its own panel-protocol WRITE/JUMP hops (read_addr_hop is baked
    # into the correct cell) — the build must not re-patch them.
    RAW_OUTPUT_HOPS = True

    _interface = BlockInterface(
        entry_address=1, input_registers=[0], output_registers=[0])

    ADDR_SPACE = LUT_ADDR_SPACE

    def __init__(self, name: str, panel_hop: int = 1, read_addr_hop: int = 1,
                 read_dest: int = 5, read_entry: int = 1,
                 read_wr_desc: int = 0, read_jp_desc: int = 0):
        """Golay (24,12) decoder — SRAM-backed.

        Args:
            panel_hop: hops from the companion controller cell to exit the
                panel port (LOAD phase + template-mode lookups).
            read_addr_hop: hops from the ``correct`` cell to its read target
                (@N on the WRITE s / JUMP pair).
            read_dest / read_entry: the DEST register / JUMP entry the
                per-codeword read words carry. Defaults (5, 1) speak the RAW
                PANEL protocol (addr -> R5, trigger R1 — relies on the panel's
                R3/R4 descriptors being pre-set). The shared-panel template
                mode points them at the companion controller's ``data``
                register + ``lookup`` entry so every read carries its OWN
                R3/R4 descriptors.
            read_wr_desc / read_jp_desc: the companion controller's push-read
                descriptors (template mode) — where the looked-up error
                pattern lands (the emit cell's ``ew`` register + entry).
        """
        super().__init__(name, panel_hop=panel_hop, read_addr_hop=read_addr_hop,
                         read_dest=read_dest, read_entry=read_entry,
                         read_wr_desc=read_wr_desc, read_jp_desc=read_jp_desc)
        self._panel_hop = int(panel_hop)
        self._read_hop = int(read_addr_hop)
        self._read_dest = int(read_dest)
        self._read_entry = int(read_entry)
        self._read_wr_desc = int(read_wr_desc) & 0xFFFF
        self._read_jp_desc = int(read_jp_desc) & 0xFFFF
        self._image = sram_error_image()

    @property
    def cell_count(self) -> int:
        return 7

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    @property
    def sram_image(self) -> Dict[int, int]:
        """The sparse syndrome -> data-half-error-pattern panel image."""
        return dict(self._image)

    def panel_requirements(self) -> dict:
        """This block needs an SRAM panel holding the sparse 4096-address
        syndrome -> error-pattern LUT (RX-tail topology: cell 6 is the
        embedded controller at the panel port; cell 0 receives the chain's
        bit stream; the panel push-reads each pattern into cell 5's ``ew``
        register — the return corridor)."""
        return {
            "label": "Golay(24,12) syndrome LUT (4096, sparse)",
            "image": dict(self._image),
            "controller_cell": 6,
            "input_cell": 0,
            "return_port": "ew",
            "return_cell": 5,
        }

    # The 5/4/3 budget-forced column split (see the docstring).
    _SYN_SPLIT = ((0, 5), (5, 4), (9, 3))

    # ------------------------------------------------------------------ build
    def build_cell_programs(self) -> Dict[int, CellProgram]:
        cells: Dict[int, CellProgram] = {}

        # (0) pack — accumulate 24 bits MSB-first; latch D at the 12th, forward
        # (D, P) + trigger at the 24th. 19 instr + 3 data + 4 state = 26.
        cells[0] = CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("dw"), Port("pw"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("one", 1, address=1),
                DataWord("twelve", 12, address=2),
                DataWord("tf", 24, address=3),      # counter reload
            ],
            state=[
                StateVar("bit", register=4),
                StateVar("w", register=5),
                StateVar("d", register=6),
                StateVar("count", register=7, initial_value=24),
            ],
            assembly_template="""\
start:
    ; bit = sample & 1 (Pack convention: only the input LSB is data)
    AND R{in:sample}, R{data:one}
    MOVE R{state:bit}, R0
    ; w = (w << 1) | bit   (MSB-first: the group's first bit ends at bit 11)
    SHL R{state:w}, #1
    OR R0, R{state:bit}
    MOVE R{state:w}, R0
    SUB R{state:count}, R{data:one}
    MOVE R{state:count}, R0
    BR.Z full
    ; at count==12 the DATA half is complete -> latch it
    CMP R{state:count}, R{data:twelve}
    BR.NZ done
    MOVE R{state:d}, R{state:w}
    GOTO done
full:
    ; 24th bit: forward D then P (stale bits above 11 — masked downstream)
    MOVE R0, R{state:d}
    {write:dw}
    MOVE R0, R{state:w}
    {write:pw}
    {jump:trig}
    MOVE R{state:count}, R{data:tf}
done:
    HALT
""",
        )

        # (1)/(2)/(3) syn1..syn3 — the encoder's LOAD-table parity loop over
        # the SAME column masks (the pin), 5/4/3 split; each forwards D, P and
        # the partial Q; syn3 XORs P in (s = Q ^ P) and forwards (D, s).
        # Register layout is EXPLICIT: inputs + state below 31-n_instr and
        # outside the LOAD/data range (the encoder's silent-collision lesson).
        for cid, (first_col, nmask) in zip((1, 2, 3), self._SYN_SPLIT):
            last = (cid == 3)
            data = [DataWord(f"m{k}",
                             GolayEncoderBlock._column_mask(first_col + k),
                             address=nmask - k) for k in range(nmask)]
            data += [DataWord("one", 1, address=nmask + 1),
                     DataWord("n", nmask, address=nmask + 2)]
            base = nmask + 3                     # first free register
            if cid == 1:
                inputs = [Port("dw", register=base), Port("pw", register=base + 1)]
                state = [StateVar("q", register=base + 2),
                         StateVar("count", register=base + 3)]
                entry_copy = ""
            else:
                # partial Q arrives in R0 (written LAST before the trigger) —
                # copied FIRST, before any ALU op clobbers R0 (INV-33).
                inputs = [Port("dw", register=base), Port("pw", register=base + 1),
                          Port("qw", register=0)]
                state = [StateVar("q", register=base + 2),
                         StateVar("count", register=base + 3)]
                entry_copy = "    MOVE R{state:q}, R{in:qw}\n"
            if last:
                tail = """\
    MOVE R0, R{in:dw}
    {write:dout}
    ; s = Q ^ P (stale bits above 11 in both — masked in the correct cell)
    XOR R{state:q}, R{in:pw}
    {write:sout}
    {jump:trig}
    HALT
"""
                outputs = [Port("dout"), Port("sout"), Port("trig")]
            else:
                tail = """\
    MOVE R0, R{in:dw}
    {write:dout}
    MOVE R0, R{in:pw}
    {write:pout}
    MOVE R0, R{state:q}
    {write:qout}
    {jump:trig}
    HALT
"""
                outputs = [Port("dout"), Port("pout"), Port("qout"), Port("trig")]
            cells[cid] = CellProgram(
                inputs=inputs,
                outputs=outputs,
                entries=[EntryPoint("default")],
                data=data,
                state=state,
                assembly_template="start:\n" + entry_copy + """\
    MOVE R{state:count}, R{data:n}
loop:
    ; q = (q << 1) | parity(D & T[count]) — the P flag of the masked AND;
    ; the down-counter IS the LOAD address (the HammingDecoder trick).
    SHL R{state:q}, #1
    MOVE R{state:q}, R0
    LOAD R{state:count}
    AND R0, R{in:dw}
    BR.NP _next
    OR R{state:q}, R{data:one}
    MOVE R{state:q}, R0
_next:
    SUB R{state:count}, R{data:one}
    MOVE R{state:count}, R0
    BR.NZ loop
""" + tail,
            )

        # (4) correct — mask s, forward D, ONE panel lookup for EVERY codeword
        # (no s==0 branch: address 0 is guaranteed unpopulated — see docstring).
        rh, rd, re = self._read_hop, self._read_dest, self._read_entry
        cells[4] = CellProgram(
            inputs=[Port("dw", register=2), Port("sw", register=0)],
            outputs=[Port("dout")],
            entries=[EntryPoint("default")],
            data=[DataWord("mask12", 0x0FFF, address=1)],
            state=[StateVar("s", register=3)],
            assembly_template="""\
start:
    ; s arrives in R0 (written last before the trigger); mask the stale bits
    AND R0, R{data:mask12}
    MOVE R{state:s}, R0
    ; D to the emit cell FIRST (it must be latched before the push-read kick)
    MOVE R0, R{in:dw}
    {write:dout}
    ; the panel read: address = s -> read_dest, then the read trigger
    MOVE R0, R{state:s}
""" + (f"    WRITE @{rh}, {rd}\n"
       f"    JUMP @{rh}, {re}\n") + """\
    HALT
""",
        )

        # (5) emit — the push-read consumer: e_d lands in R{ew} (non-R0 — the
        # push-read landing rule) + this entry is kicked; burst (D ^ e_d) 12
        # bits MSB-first. Exit cell: conditional branch only.
        cells[5] = CellProgram(
            inputs=[Port("dw", register=3), Port("ew", register=4)],
            outputs=[Port("out"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("one", 1, address=1),
                DataWord("twelve", 12, address=2),
            ],
            state=[
                StateVar("w", register=5),
                StateVar("cnt", register=6),
            ],
            assembly_template="""\
start:
    ; corrected word = D ^ e_d (stale bits above 11 masked by the peel)
    XOR R{in:dw}, R{in:ew}
    MOVE R{state:w}, R0
    MOVE R{state:cnt}, R{data:twelve}
dloop:
    SHR R{state:w}, #11
    AND R0, R{data:one}
    {write:out}
    {jump:trig}
    SHL R{state:w}, #1
    MOVE R{state:w}, R0
    SUB R{state:cnt}, R{data:one}
    MOVE R{state:cnt}, R0
    BR.NZ dloop
    HALT
""",
        )

        # (6) the companion SRAM controller (LOAD phase + template lookups).
        from .sram_controller_block import SramControllerBlock
        ctl = SramControllerBlock(self.name + "_ctl", panel_hop=self._panel_hop,
                                  read_wr_desc=self._read_wr_desc,
                                  read_jp_desc=self._read_jp_desc)
        cells[6] = ctl.build_cell_programs()[0]
        return cells

    # ------------------------------------------------------- multi-cell wiring
    def internal_connections(self):
        return [(0, "dw", 1, "dw"), (0, "pw", 1, "pw"),
                (1, "dout", 2, "dw"), (1, "pout", 2, "pw"), (1, "qout", 2, "qw"),
                (2, "dout", 3, "dw"), (2, "pout", 3, "pw"), (2, "qout", 3, "qw"),
                (3, "dout", 4, "dw"), (3, "sout", 4, "sw"),
                (4, "dout", 5, "dw")]

    def internal_jumps(self):
        return [(0, "trig", 1, "default"),
                (1, "trig", 2, "default"),
                (2, "trig", 3, "default"),
                (3, "trig", 4, "default")]

    def output_cell_ids(self):
        return [5]

    # -------------------------------------------------------------- reference
    @classmethod
    def decode_word(cls, bits24) -> List[int]:
        """Golden per-word decode via the model (built from the encoder pin):
        24 received bits -> the 12 corrected data bits MSB-first."""
        return decode_word_from_sram(sram_error_image(), bits24)

    def process_reference_q15(self, x_q15) -> list:
        """Bit-exact reference: group input words 24 at a time (LSB of each
        word is the bit, first bit = d11), emit 12 corrected data bits per
        group MSB-first. A trailing partial group (< 24 bits) is dropped."""
        return decode_stream_from_sram(self._image,
                                       [int(w) & 1 for w in x_q15])

    def process_reference(self, input_samples) -> np.ndarray:
        """Float reference (0.0/1.0 bits): same grouping."""
        words = [int(round(float(v))) & 0xFFFF for v in input_samples]
        return np.asarray(self.process_reference_q15(words), dtype=np.float32)

    def reset(self):
        """No cross-call host state (each reference call is a fresh stream)."""
        pass
