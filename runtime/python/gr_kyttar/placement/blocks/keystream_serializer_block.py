# SPDX-License-Identifier: GPL-3.0-or-later
"""KeystreamSerializerBlock — see the class docstring."""
import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock

MASK16 = 0xFFFF


class KeystreamSerializerBlock(KyttarBlock):
    """Serialize a hi/lo 16-bit half-word stream into RFC 8439 keystream BYTES.

    placeKYT-native (no stock GNU Radio counterpart). The measured need
    (INV-66 context): ``ChaCha20KeystreamBlock`` emits its keystream as the 16
    state words in hi-then-lo 16-bit halves (32 raw words per block), but RFC
    8439 §2.3 encryption XORs the keystream as the LITTLE-ENDIAN BYTES of each
    32-bit state word, and the data-link convention carries ONE byte per 16-bit
    word. This block converts between the two conventions:

        input : w0=hi, w1=lo, w2=hi, w3=lo, ...   (raw 16-bit half-words)
        output: per (hi, lo) pair, the 32-bit word ``v = (hi << 16) | lo``
                emitted as its four bytes LITTLE-ENDIAN, one byte per word:

                    out = [ v         & 0xFF,    # = lo & 0xFF
                            (v >> 8)  & 0xFF,    # = lo >> 8
                            (v >> 16) & 0xFF,    # = hi & 0xFF
                            (v >> 24) & 0xFF ]   # = hi >> 8

    which is exactly ``int(v).to_bytes(4, "little")`` — the ``serialize()``
    of the ChaCha20 golden (RFC 8439 §2.3's byte order). Rate 1:2 — 2 input
    words -> 4 output words. A trailing unpaired hi word produces no output
    (it is held awaiting its lo half).

    Pure streaming, ONE cell, no panel. The hi/lo phase is tracked by a pinned
    parity StateVar (INV-33): parity 0 = the incoming word is a hi half (hold
    it, arm parity); parity 1 = the incoming word is the lo half (emit the four
    bytes, clear parity). ``par`` and the held ``hi`` half are loop memory
    across triggers and are declared ``reset_per_batch`` so the hosted bridge's
    packet-boundary reset returns the block to hi-phase for a fresh batch —
    without it, a batch ending on an unpaired hi word would skew every later
    batch by one half-word (gated, INV-4).

    Byte extraction is shifts and masks only: ``AND`` with 0xFF (LOGIC) and a
    LOGICAL ``SHR`` by the immediate 8 (INV-34 — the count is an instruction
    field). No Q15 arithmetic anywhere, so the comparison is BIT-EXACT.

    The block is head-and-tail composable: plain routed ingress and egress
    (``{write:out}`` / ``{jump:out}``, NOT raw port literals), so unlike the
    RAW-egress blocks (INV-66: tail-only) it can sit mid-chain between two
    other blocks on one chip — gated on a real placed+routed+built chain.

    Interface:
        - Input: ``word`` — one raw 16-bit half-word per trigger.
        - Output: ``out`` — a 4-word byte burst on every second trigger.
    """
    CATEGORY = "fec"
    TAGS = ["chacha20", "keystream", "serialize", "rfc8439", "bytes",
            "little-endian", "crypto"]

    _interface = BlockInterface(
        entry_address=1, input_registers=[0], output_registers=[0])

    GRC_UNSUPPORTED_PARAMS = ()

    def __init__(self, name: str):
        # No parameters: the serialization order is fixed by RFC 8439 §2.3
        # (little-endian bytes per 32-bit word) — spec, not a user setting.
        super().__init__(name)

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> dict:
        """One cell; two paths selected by the hi/lo parity.

        Parity 0 (hi half): hold the word in ``hi``, set parity, HALT — no
        output this trigger. Parity 1 (lo half): save the word (R0 is the
        accumulator — the first ALU op would destroy it, INV-33), clear parity
        FIRST (so every exit of the emit path leaves the cell in hi-phase),
        then emit ``lo&0xFF``, ``lo>>8``, ``hi&0xFF``, ``hi>>8`` — the 32-bit
        word's four bytes little-endian. The output port's single-outstanding
        handshake paces the 4-word burst (the UnpackKBits idiom); every
        ``{write:out}`` sends R0, and a LOGICAL SHR #8 of a 16-bit word needs
        no extra mask.

        Registers (INV-33 contract — input low, data above, state pinned):
        input @R0, data @1..3, state pinned @4..6, instructions at the top.
        CMP leaves R0 unchanged and BR is a non-ALU op, so the incoming word
        survives the dispatch and is saved on whichever path runs.
        """
        template = """\
start:
    CMP R{state:par}, R{data:zero}
    BR.NZ emit
    MOVE R{state:hi}, R{in:word}
    MOVE R{state:par}, R{data:one}
    HALT
emit:
    MOVE R{state:lo}, R{in:word}
    MOVE R{state:par}, R{data:zero}
    AND R{state:lo}, R{data:ff}
    {write:out}
    {jump:out}
    SHR R{state:lo}, #8
    {write:out}
    {jump:out}
    AND R{state:hi}, R{data:ff}
    {write:out}
    {jump:out}
    SHR R{state:hi}, #8
    {write:out}
    {jump:out}
    HALT
"""
        return {0: CellProgram(
            inputs=[Port("word", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[DataWord("ff", 0xFF, address=1),
                  DataWord("one", 1, address=2),
                  DataWord("zero", 0, address=3)],
            state=[
                # Loop memory across triggers: the held hi half and the hi/lo
                # parity. Both reset at a packet boundary so a fresh batch
                # always starts in hi-phase (the hosted bridge applies the
                # build's batch_reset_writes at every process_batch).
                StateVar("hi", register=4, initial_value=0,
                         reset_per_batch=True),
                StateVar("par", register=5, initial_value=0,
                         reset_per_batch=True),
                # Per-trigger scratch (the saved lo half) — not loop memory.
                StateVar("lo", register=6, initial_value=0),
            ],
            assembly_template=template,
        )}

    # -------------------------------------------------------------- reference
    def process_reference_q15(self, words) -> list:
        """Bit-exact predictor: little-endian bytes per (hi, lo) input pair.

        ``words`` are raw 16-bit values (the hi/lo half-word stream). Per
        complete pair the four bytes of ``(hi << 16) | lo`` are emitted
        little-endian, one byte per output word — exactly
        ``int(v).to_bytes(4, "little")``. A trailing unpaired hi word emits
        nothing (it is held on-chip awaiting its lo half).
        """
        w = [int(x) & MASK16 for x in words]
        out: list = []
        for i in range(0, len(w) - 1, 2):
            hi, lo = w[i], w[i + 1]
            out.extend([lo & 0xFF, (lo >> 8) & 0xFF,
                        hi & 0xFF, (hi >> 8) & 0xFF])
        return out

    def process_reference(self, input_samples) -> np.ndarray:
        """Float view of the byte stream (raw byte VALUES, not Q15 samples)."""
        words = [int(round(float(v))) & MASK16 for v in input_samples]
        return np.asarray(self.process_reference_q15(words), dtype=np.float32)
