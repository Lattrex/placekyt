# SPDX-License-Identifier: GPL-3.0-or-later
"""Poly1305MACBlock — see :class:`Poly1305MACBlock`."""
from typing import Any, Dict, List, Tuple

import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock

MASK16 = 0xFFFF

#: The Poly1305 prime, ``2**130 - 5`` (RFC 8439 §2.5).
P1305 = (1 << 130) - 5

#: RFC 8439 §2.5 clamp on the low 16 key bytes.
R_CLAMP = 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFF

#: ``2**130 mod p`` — the reduction constant. NOT a tunable.
REDUCTION_CONSTANT = 5

#: Limb radix. See the class docstring for why it is 10 and not 26.
LIMB_BITS = 10
#: ``130 == 10 * 13`` exactly, so the ``2**130 ≡ 5`` fold is limb-aligned.
N_LIMBS = 13
LIMB_MASK = (1 << LIMB_BITS) - 1

#: Words in the emitted tag (16 bytes, little-endian 16-bit words).
TAG_WORDS = 8
#: Words in one full message block (16 bytes).
BLOCK_WORDS = 8

#: Bit offset of limb ``k`` inside a 16-bit word frame: ``(10*k) mod 16``.
_BK = [(10 * k) % 16 for k in range(N_LIMBS)]
#: Limbs that COMPLETE a 16-bit output word (b_k + 10 >= 16, and limb 12).
_EMITTERS = [k for k in range(N_LIMBS) if _BK[k] + 10 >= 16 or k == 12]

_FACE_CODE = {"south": 0, "east": 1, "west": 2, "north": 3}
_DELTA = {"south": (0, 1), "east": (1, 0), "west": (-1, 0), "north": (0, -1)}


def _clamp_r(r_bytes: bytes) -> int:
    """RFC 8439 §2.5 clamp — mandatory, never optional."""
    return int.from_bytes(bytes(r_bytes), "little") & R_CLAMP


def _to_limbs(x: int) -> List[int]:
    return [(x >> (LIMB_BITS * i)) & LIMB_MASK for i in range(N_LIMBS)]


def _from_limbs(limbs) -> int:
    return sum(int(v) << (LIMB_BITS * i) for i, v in enumerate(limbs))


def pack_pieces(j: int, w: int) -> List[Tuple[int, int]]:
    """Word ``j``'s static-shift limb pieces as ``[(limb_index, value)]``.

    Word ``j`` occupies bits ``16j .. 16j+15``; limb ``k`` is bits
    ``10k .. 10k+9``, so each word contributes to 2-3 limbs with
    COMPILE-TIME shifts and masks — exactly what the ``pack_j`` cells run.
    """
    out = []
    base = 16 * j
    v = (w & MASK16) << base
    for k in range(base // 10, min(N_LIMBS, (base + 15) // 10 + 1)):
        out.append((k, (v >> (10 * k)) & LIMB_MASK))
    return out


class Poly1305MACBlock(KyttarBlock):
    """
    **Poly1305** one-time authenticator (RFC 8439 §2.5) — a placeKYT-native
    ([Kyttar]) cryptographic primitive with **no stock GNU Radio counterpart**.
    The golden reference is the published algorithm in
    ``verification/tests/poly1305_golden.py``, itself pinned by the RFC's own
    §2.5.2 worked example and the §A.3 edge-case vectors.

    Per 16-byte message block, over the field ``GF(2**130 - 5)``::

        acc = ((acc + block_with_high_bit) * r) mod (2**130 - 5)
        tag = (acc + s) mod 2**128

    This is **exact modular integer arithmetic, not Q15 DSP**: nothing
    saturates, every carry is real, and the Q15 idioms (INV-13) must NOT be
    inherited.

    Interface — a ONE-TIME MAC, exactly as the RFC demands
    ======================================================

    * ``r_key`` / ``s_key``: the 32-byte one-time key halves, hex, little-
      endian, exactly as RFC 8439 §2.5.2 prints them. ``r`` is clamped on the
      way in (the clamp is part of the algorithm, not an option).
    * ``msg_words``: the message length in 16-bit little-endian words. The
      block consumes exactly this many input words and then emits the 16-byte
      tag as 8 little-endian words in one burst. RFC 8439 REQUIRES a Poly1305
      key to authenticate exactly ONE message, so a build authenticates one
      message per run — additional input words beyond ``msg_words`` are held
      at the input arbiter, never silently mixed into a finished tag.
    * KNOWN INTERFACE LIMIT (documented, not hidden): the input is a 16-bit
      word stream, so an ODD-byte message is not expressible here; the three
      odd-length §A.3 vectors are gated at the golden instead.

    Why radix 2**10 and not the textbook 2**26 (INV-57, MEASURED)
    =============================================================

    ``MUL``/``MULHI`` are **SIGNED** (measured: ``0x0002 * 0xFFFF`` returns
    ``0xFFFFFFFE``), so an exact unsigned 16x16->32 product needs both
    operands in ``[0, 0x7FFF]``. The radix must also divide 130 for the
    ``2**130 ≡ 5`` fold to stay limb-aligned (2, 5, 10, 13, 26 only). Radix
    2**26 has 26-bit limbs; radix 2**13's folded coefficient ``5*r[j]``
    reaches ``0x9FFB``; radix **2**10 with 13 limbs** is the largest survivor
    (limbs and every multiplicand stay under 2**15).

    Datapath — ONE CONVEYOR CYCLE; every phase is a serial chain
    ============================================================

    Pass 1 built the field multiply as a systolic ring with parallel fan-fired
    sweeps and spent most of its budget on sweep-staging hazards (INV-58). A
    MAC has no throughput requirement, so this build removes the hazard class
    outright: **every phase runs as a SERIAL chain** — one cell executes at a
    time and hands the baton forward, so ordering is program order and no
    staging discipline exists to get wrong.

    The whole block lies on ONE conveyor cycle::

        control row (east) -> ring rows 1..9 (serpentine) -> closure
        (row-9 tail + column 0, north) -> back into the control row

    so every control edge — sequencer-to-chain-head injections AND
    end-of-chain returns — is a plain hop-counted write/jump on resting
    faces. The only flipped edges are the three group-internal hop-1
    deliveries (below). Thirteen limb GROUPS ``[mulA_k, lh_k, mulC_k,
    mulB_k, fin_k]`` sit along the ring:

    * ``mulA_k`` owns line limb ``a`` and the 32-bit accumulator ``(hi,lo)``.
      ``smac`` is one serial multiply pass: the 7-instruction 32-bit MAC
      (high half FIRST — INV-58), forward the line limb, adopt the
      predecessor's (cell 0 adopts FIRST from the wrap; cell 12 forwards
      ``5*a`` — the ``2**130 ≡ 5`` fold rides the rotation's closing edge,
      relayed around the cycle by ``rrel_a``/``rrel_b``). The coefficient
      ``r_i`` rides the same chain. 13 passes per block; ``xfer`` then moves
      ``(hi,lo)`` into ``mulB_k`` and clears it.
    * ``mulB_k`` runs two ``nrm`` rounds: forward own ``hi``; the RECEIVER
      applies the ``x64`` (2**16 = 64 * 2**10) with a full 32-bit MAC; round
      2's seed is ``5*hi12`` and the leftover wrap folds into the first
      ``spl`` seed as ``320*hi12``.
    * ``mulC_k`` runs ``spl`` rounds: split at bit 10, carry
      ``(v>>10) + 64*C + 64*hi`` forward (the ADD's 17th bit captured with
      ``ADC z,z`` — measured), post the limb into ``lh_k`` (hop-1 flip),
      loop until the wrap carry is zero.
    * ``lh_k`` holds the between-block limb ``lv``: message pieces add in
      (``addv``), ``pub`` posts it into ``mulA_k.a`` per block (hop-1 flip),
      and at message end ``gprobe`` (the ``[acc >= p]`` probe: seed 5, keep
      only the top carry ``f`` — no canonical reduction is materialised,
      because ``tag = (acc + s + 5f) mod 2**128``) and ``fpub`` feed the
      finish.
    * ``fin_k``: ``v = limb + s_k + carry``, static-shift word assembly
      (limb 12 masked to 8 bits = the mod 2**128); finished words cascade
      emitter-to-emitter to ``crq`` and out.
    * ``pack_j`` (one per word slot, mod-8 counters) splits each arriving
      word into its 2-3 static-shift pieces; ``pack_7`` folds the full-block
      high bit (+256 on limb 12) into its own piece; a partial final block's
      high bit is a compile-time piece injected by ``pack_{m-1}``.
    * ``seq_top`` (the input landing, cell 0) serializes words with the
      arbiter LOCK (INV-20's idiom): one word traverses the whole pack chain
      and, at block boundaries, the whole compute, before ``ulk`` (west of
      ``seq_top``) clears the lock — so saturated back-to-back drive cannot
      corrupt state. The unlock trigger rides the cycle via ``u1/u2/u3``.
    * ``crw/crx/crn/crs/crg/crp/crq`` on the closure return each chain's
      completion (and value) to its sequencer; every cell carries at most
      ONE backward jump, kept highest-addressed (INV-53/INV-63).

    The schedule is bit-exact against the golden over the RFC vectors and
    hundreds of random messages — see ``process_reference``, which models
    the EXACT cell schedule.
    """

    CATEGORY = "fec"
    TAGS = ["poly1305", "crypto", "mac", "authenticator", "rfc8439",
            "multi-word", "130-bit"]

    # 100 cells (94 programs + 6 face-only closure transits) on a 10x10 fold:
    # the sole occupant of its die, at the pinned (0,1) anchor.
    CHIP_SCALE = True
    CHIP_SCALE_ORIENTATIONS = ((),)

    _interface = BlockInterface(
        entry_address=22, input_registers=[1], output_registers=[0])

    #: The egress authors its own port pair (INV-63) — see ``out`` below.
    RAW_OUTPUT_HOPS = True

    GRC_UNSUPPORTED_PARAMS = ()

    TAG_WORDS = TAG_WORDS
    N_LIMBS = N_LIMBS
    LIMB_BITS = LIMB_BITS
    REDUCTION_CONSTANT = REDUCTION_CONSTANT

    def __init__(self, name: str,
                 r_key: str = "85d6be7857556d337f4452fe42d506a8",
                 s_key: str = "0103808afb0db2fd4abff6af4149f51b",
                 msg_words: int = 17):
        """Poly1305 over a 32-byte one-time key, split into ``r`` and ``s``.

        Both halves are 16 bytes of lowercase hex, little-endian, exactly as
        RFC 8439 §2.5.2 prints them. ``r_key`` is **clamped** on the way in.
        ``msg_words`` is the message length in 16-bit words (>= 1).
        """
        super().__init__(name, r_key=r_key, s_key=s_key, msg_words=msg_words)
        self.r_key = r_key
        self.s_key = s_key
        self.msg_words = int(msg_words)
        if self.msg_words < 1:
            raise ValueError("msg_words must be >= 1")
        self._r = _clamp_r(bytes.fromhex(r_key))
        self._s = int.from_bytes(bytes.fromhex(s_key), "little")
        self._r_limbs = _to_limbs(self._r)
        self._s_limbs = _to_limbs(self._s)
        #: words in the FINAL block, 1..8
        self._m_last = ((self.msg_words - 1) % BLOCK_WORDS) + 1

    # ------------------------------------------------------------- structure
    @property
    def cell_count(self) -> int:
        return len(self._geometry())

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def output_cell_id(self):
        return "out"

    # ------------------------------------------------------------- geometry
    @staticmethod
    def _group(k: int) -> List[str]:
        return [f"mulA_{k}", f"lh_{k}", f"mulC_{k}", f"mulB_{k}", f"fin_{k}"]

    def _ring_sequence(self) -> List[str]:
        """Ring-row cells in CONVEYOR order (packs, groups, unlock relays)."""
        pack_before = {0: 0, 1: 1, 2: 3, 3: 4, 4: 6, 5: 8, 6: 9, 7: 11}
        before: Dict[int, List[str]] = {}
        for j, g in pack_before.items():
            before.setdefault(g, []).append(f"pack_{j}")
        # unlock relays interleave at ring positions that keep every hop
        # of the bnd/seq_spl -> u1 -> u2 -> u3 -> ulk chain under 31.
        u_after_group = {2: "u1", 6: "u2", 10: "u3"}
        seq: List[str] = []
        for g in range(N_LIMBS):
            seq += before.get(g, [])
            seq += self._group(g)
            if g in u_after_group:
                seq.append(u_after_group[g])
        return seq

    #: control-row cells, west to east (fold row 0, columns 1..8).
    _CONTROL = ["seq_top", "bnd", "seq_mul", "rrom", "seq_nrm", "seq_spl",
                "seq_fin", "out"]

    #: closure relays in CONVEYOR order along the row-9 tail + column 0.
    _CLOSURE_RELAYS = ["crw", "crx", "crn", "crs", "crg", "crp", "crq",
                      "rrel_a", "rrel_b"]

    def _geometry(self) -> Dict[str, Tuple[int, int, str]]:
        """The fold — ONE conveyor cycle over a 10x11 footprint.

        * fold row 0: ``ulk`` at (0,0) then the sequencer cells and the
          egress, all resting EAST; (9,0) turns SOUTH into the ring.
        * fold rows 1..9: the ring serpentine — cols 1..9, ODD rows west,
          EVEN rows east, each row's last cell facing SOUTH.
        * closure: ring end -> row-9 tail westward -> column 0 north -> back
          into ``ulk`` -> the control row. The ``cr*`` relays live on it;
          face-only ``transit_cl_*`` cells pave the rest.
        """
        # seq_top FIRST: the catalog derives the block's external input from
        # the first cell (INV-61.4), and program order == dict order here.
        lay: Dict[str, Tuple[int, int, str]] = {}
        for i, cid in enumerate(self._CONTROL):
            lay[cid] = (1 + i, 0, "east")
        lay["ulk"] = (0, 0, "east")
        lay["transit_t0"] = (9, 0, "south")

        ring = self._ring_sequence()
        pos = 0
        for cid in ring:
            row = pos // 9
            col = pos % 9
            y = 1 + row
            if row % 2 == 0:        # rows 1,3,5,... run WEST
                x = 9 - col
                face = "west" if col < 8 else "south"
            else:                   # rows 2,4,6,... run EAST
                x = 1 + col
                face = "east" if col < 8 else "south"
            lay[cid] = (x, y, face)
            pos += 1
        end_row, end_col = (pos - 1) // 9, (pos - 1) % 9
        assert end_row == 8, (end_row, end_col)
        ex, ey, _ = lay[ring[-1]]
        # ring end is on row 9 (a WEST row: 9,11,... row index 8 is even ->
        # WEST). Closure: continue west along row 9, then column 0 north.
        closure: List[Tuple[int, int, str]] = []
        for x in range(ex - 1, -1, -1):
            closure.append((x, 9, "west" if x > 0 else "north"))
        closure += [(0, y, "north") for y in range(8, 0, -1)]
        relays = list(self._CLOSURE_RELAYS)
        assert len(closure) >= len(relays), (len(closure), len(relays))
        t = 0
        for i, (x, y, f) in enumerate(closure):
            if i < len(closure) - 1 and relays:
                lay[relays.pop(0)] = (x, y, f)
            else:
                lay[f"transit_cl_{t}"] = (x, y, f)
                t += 1
        assert not relays, "closure too short for the relays"
        assert len({v[:2] for v in lay.values()}) == len(lay), \
            "two cells share a position"
        return lay

    def default_layout(self) -> Dict[Any, Tuple[int, int, str]]:
        """:meth:`_geometry`, reindexed into PROGRAM order (INV-51 clause 2:
        positional pairing), with the face-only ``transit_*`` cells last."""
        lay = self._geometry()
        order = list(self.build_cell_programs().keys())
        assert set(order) <= set(lay), "programs name cells not in the layout"
        out = {cid: lay[cid] for cid in order}
        for cid, v in lay.items():
            if cid.startswith("transit_"):
                out[cid] = v
        assert len(out) == len(lay)
        return out

    # ---------------------------------------------------------- face helpers
    def _face_to(self, lay, src: str, dst: str) -> int:
        """The face code pointing from placed ``src`` to ABUTTING ``dst``."""
        sx, sy, _ = lay[src]
        dx, dy, _ = lay[dst]
        step = (dx - sx, dy - sy)
        for name, d in _DELTA.items():
            if d == step:
                return _FACE_CODE[name]
        raise AssertionError(f"{src} and {dst} are not abutting: {step}")

    def _rest(self, lay, cid: str) -> int:
        return _FACE_CODE[lay[cid][2]]

    # --------------------------------------------------------- cell builders
    def build_cell_programs(self) -> Dict[Any, CellProgram]:
        lay = self._geometry()
        progs: Dict[str, CellProgram] = {}
        rl = self._r_limbs
        sl = self._s_limbs
        m = self._m_last

        # ---------------- control row ----------------
        # seq_top: the input landing (cell 0). Locks its arbiter to the WEST
        # face (the cycle's return traffic) so the NEXT port word is held
        # until this word's whole pipeline finishes (INV-20 serialize-LOCK).
        progs["seq_top"] = CellProgram(
            inputs=[Port("w", register=1)],
            outputs=[Port("w0"), Port("pb0")],
            entries=[EntryPoint("go")],
            data=[DataWord("fW", 2, address=2),
                  DataWord("one", 1, address=3)],
            state=[],
            assembly_template="""\
go:
    MOVE R0, R{data:fW}
    MOVE [LOCK_FACE], R0
    MOVE R0, R{data:one}
    MOVE [LOCK], R0
    MOVE R0, R{in:w}
    {write:w0}
    {jump:pb0}
""")

        # ulk: clears seq_top's arbiter lock (WRITE.CFG @1 east, CONFIG 4).
        progs["ulk"] = CellProgram(
            inputs=[],
            outputs=[Port("unlock")],
            entries=[EntryPoint("go")],
            data=[DataWord("z0", 0, address=1)],
            state=[],
            assembly_template="""\
go:
    MOVE R0, R{data:z0}
    WRITE.CFG @1, 4
""")

        # bnd: word/message counters; boundary -> pub chain; final -> high-bit
        # inject + fflag + pub chain; per-word unlock rides u1/u2/u3 -> ulk.
        final_tail = ("    {jump:hbgo}\n    {jump:pubgo}\n"
                      if m < BLOCK_WORDS else "    {jump:pubgo}\n")
        bnd_outs = [Port("fflag"), Port("pubgo"), Port("ubgo")]
        if m < BLOCK_WORDS:
            bnd_outs.append(Port("hbgo"))
        progs["bnd"] = CellProgram(
            inputs=[],
            outputs=bnd_outs,
            entries=[EntryPoint("wdone")],
            data=[DataWord("one", 1, address=3),
                  DataWord("c8", 8, address=4)],
            state=[StateVar("wc", register=1, initial_value=8,
                            reset_per_batch=True, reset_value=8),
                   StateVar("tc", register=2, initial_value=self.msg_words,
                            reset_per_batch=True,
                            reset_value=self.msg_words)],
            assembly_template="""\
unlk:
    {jump:ubgo}
    HALT
wdone:
    SUB R{state:tc}, R{data:one}
    MOVE R{state:tc}, R0
    BR.Z final
    SUB R{state:wc}, R{data:one}
    MOVE R{state:wc}, R0
    BR.NZ unlk
    MOVE R{state:wc}, R{data:c8}
    {jump:pubgo}
    HALT
final:
    MOVE R0, R{data:one}
    {write:fflag}
""" + final_tail)

        # seq_mul: multiply pass counter; fires rrom per pass, xfer at end.
        progs["seq_mul"] = CellProgram(
            inputs=[],
            outputs=[Port("rq"), Port("rrst"), Port("xj")],
            entries=[EntryPoint("step")],
            data=[DataWord("c1", 1, address=2),
                  DataWord("c13", 13, address=3)],
            state=[StateVar("pcnt", register=1, initial_value=13,
                            reset_per_batch=True, reset_value=13)],
            assembly_template="""\
step:
    SUB R{state:pcnt}, R{data:c1}
    MOVE R{state:pcnt}, R0
    BR.N xf
    {jump:rq}
    HALT
xf:
    MOVE R{state:pcnt}, R{data:c13}
    {jump:rrst}
    {jump:xj}
""")

        # rrom: the r-limb table + pointer; injects the pass coefficient.
        progs["rrom"] = CellProgram(
            inputs=[],
            outputs=[Port("cw"), Port("sj")],
            entries=[EntryPoint("get"), EntryPoint("rst")],
            data=[DataWord(f"r{i}", rl[i], address=1 + i)
                  for i in range(N_LIMBS)]
            + [DataWord("c1", 1, address=14)],
            state=[StateVar("rptr", register=15, initial_value=1,
                            reset_per_batch=True, reset_value=1)],
            assembly_template="""\
rst:
    MOVE R{state:rptr}, R{data:c1}
    HALT
get:
    LOAD R{state:rptr}
    {write:cw}
    {jump:sj}
    ADD R{state:rptr}, R{data:c1}
    MOVE R{state:rptr}, R0
""")

        # seq_nrm: two nrm rounds, then xfer2 + the spl seed (320 * hi12_r2).
        progs["seq_nrm"] = CellProgram(
            inputs=[Port("wv", register=1)],
            outputs=[Port("na"), Port("nj"), Port("sa"), Port("x2j2")],
            entries=[EntryPoint("n1"), EntryPoint("nr")],
            data=[DataWord("c1", 1, address=3),
                  DataWord("z0", 0, address=4),
                  DataWord("five", 5, address=5),
                  DataWord("c320", 320, address=6)],
            state=[StateVar("rc", register=2)],
            assembly_template="""\
n1:
    MOVE R{state:rc}, R{data:c1}
    MOVE R0, R{data:z0}
    {write:na}
    {jump:nj}
    HALT
nr:
    SUB R{state:rc}, R{data:c1}
    MOVE R{state:rc}, R0
    BR.N x2
    MUL R{in:wv}, R{data:five}
    {write:na}
    {jump:nj}
    HALT
x2:
    MUL R{in:wv}, R{data:c320}
    {write:sa}
    {jump:x2j2}
""")

        # seq_spl: spl round loop; on convergence branch normal/final.
        progs["seq_spl"] = CellProgram(
            inputs=[Port("sv", register=1), Port("ff", register=2)],
            outputs=[Port("sa2"), Port("sj2"), Port("gp"), Port("ub")],
            entries=[EntryPoint("spr")],
            data=[DataWord("z0", 0, address=3),
                  DataWord("five", 5, address=4)],
            state=[],
            assembly_template="""\
spr:
    SUB R{in:sv}, R{data:z0}
    BR.Z done
    MUL R{in:sv}, R{data:five}
    {write:sa2}
    {jump:sj2}
    HALT
done:
    SUB R{in:ff}, R{data:z0}
    BR.Z nrm
    {jump:gp}
    HALT
nrm:
    {jump:ub}
""")

        # seq_fin: gprobe launch, then the finish chain seeded with 5f.
        progs["seq_fin"] = CellProgram(
            inputs=[Port("gv", register=1)],
            outputs=[Port("gc"), Port("gj"), Port("fc"), Port("fst")],
            entries=[EntryPoint("gp"), EntryPoint("gpr")],
            data=[DataWord("z0", 0, address=2),
                  DataWord("five", 5, address=3)],
            state=[],
            assembly_template="""\
gp:
    MOVE R0, R{data:five}
    {write:gc}
    {jump:gj}
    HALT
gpr:
    SUB R{in:gv}, R{data:z0}
    BR.Z f0
    MOVE R0, R{data:five}
    {write:fc}
    {jump:fst}
    HALT
f0:
    MOVE R0, R{data:z0}
    {write:fc}
    {jump:fst}
""")

        # out: the tag egress. The cell RESTS EAST — its face is load-bearing
        # for the conveyor (sequencer injections transit it into the ring) —
        # so the port pair flips NORTH into the (8,0)->(9,0) corridor and
        # restores. RAW_OUTPUT_HOPS (INV-63): the pair is AUTHORED literals
        # (`WRITE @3, 0` / `JUMP @3, 0` — two corridor cells + the edge exit),
        # valid for the block's pinned (0, 1) anchor; the sink fixup would
        # otherwise rewrite hops resolved against a 92-hop resting-face walk
        # (measured) and re-face this cell off its conveyor duty.
        progs["out"] = CellProgram(
            inputs=[Port("wr", register=1)],
            outputs=[Port("out")],
            entries=[EntryPoint("emit")],
            data=[DataWord("fN", 3, address=2, is_face=True),
                  DataWord("fE", 1, address=3, is_face=True)],
            state=[],
            assembly_template="""\
emit:
    MOVE R0, R{in:wr}
    MOVE [FACE], R{data:fN}
    WRITE @3, 0
    JUMP @3, 0
    MOVE [FACE], R{data:fE}
""")

        # ---------------- pack cells ----------------
        mask_vals = {"m1023": 1023, "m15": 15, "m255": 255, "m3": 3,
                     "m63": 63, "c256": 256}
        piece_asm_lines = {
            "AND w,m1023": ["    AND R{in:w}, R{data:m1023}"],
            ">>10": ["    SHR R{in:w}, #10"],
            "&15<<6": ["    AND R{in:w}, R{data:m15}", "    SHL R0, #6"],
            ">>4&": ["    SHR R{in:w}, #4", "    AND R0, R{data:m1023}"],
            ">>14": ["    SHR R{in:w}, #14"],
            "&255<<2": ["    AND R{in:w}, R{data:m255}", "    SHL R0, #2"],
            ">>8": ["    SHR R{in:w}, #8"],
            "&3<<8": ["    AND R{in:w}, R{data:m3}", "    SHL R0, #8"],
            ">>2&": ["    SHR R{in:w}, #2", "    AND R0, R{data:m1023}"],
            ">>12": ["    SHR R{in:w}, #12"],
            "&63<<4": ["    AND R{in:w}, R{data:m63}", "    SHL R0, #4"],
            ">>6&": ["    SHR R{in:w}, #6", "    AND R0, R{data:m1023}"],
            ">>8+256": ["    SHR R{in:w}, #8", "    ADD R0, R{data:c256}"],
        }
        piece_ops = {
            0: [("AND w,m1023", 0), (">>10", 1)],
            1: [("&15<<6", 1), (">>4&", 2), (">>14", 3)],
            2: [("&255<<2", 3), (">>8", 4)],
            3: [("&3<<8", 4), (">>2&", 5), (">>12", 6)],
            4: [("&63<<4", 6), (">>6&", 7)],
            5: [("AND w,m1023", 8), (">>10", 9)],
            6: [("&15<<6", 9), (">>4&", 10), (">>14", 11)],
            7: [("&255<<2", 11), (">>8+256", 12)],
        }
        op_masks = {
            "AND w,m1023": ["m1023"], ">>10": [], "&15<<6": ["m15"],
            ">>4&": ["m1023"], ">>14": [], "&255<<2": ["m255"], ">>8": [],
            "&3<<8": ["m3"], ">>2&": ["m1023"], ">>12": [],
            "&63<<4": ["m63"], ">>6&": ["m1023"], ">>8+256": ["c256"],
        }
        hb_limb = (16 * m) // 10
        hb_val = 1 << (16 * m - 10 * hb_limb)
        hb_owner = f"pack_{m - 1}" if m < BLOCK_WORDS else None

        for j in range(BLOCK_WORDS):
            cid = f"pack_{j}"
            masks = sorted({mn for op, _k in piece_ops[j]
                            for mn in op_masks[op]})
            data = [DataWord("c1", 1, address=3), DataWord("c8", 8, address=4)]
            addr = 5
            for mn in masks:
                data.append(DataWord(mn, mask_vals[mn], address=addr))
                addr += 1
            outs = []
            body = ""
            for op, k in piece_ops[j]:
                port = f"v{k}"
                outs += [Port(port), Port(f"{port}j")]
                body += ("\n".join(piece_asm_lines[op])
                         + "\n    {write:%s}\n    {jump:%sj}\n" % (port, port))
            hb_entry = ""
            if cid == hb_owner:
                data.append(DataWord("hbc", hb_val, address=addr))
                addr += 1
                outs += [Port("hv"), Port("hvj")]
                hb_entry = ("hb:\n    MOVE R0, R{data:hbc}\n"
                            "    {write:hv}\n    {jump:hvj}\n    HALT\n")
            if j < BLOCK_WORDS - 1:
                tail = ("fwd:\n    MOVE R0, R{in:w}\n"
                        "    {write:wn}\n    {jump:pbn}\n")
                outs += [Port("wn"), Port("pbn")]
            else:
                tail = "fwd:\n    {jump:bat}\n"
                outs += [Port("bat")]
            asm = (hb_entry + "pb:\n"
                   "    SUB R{state:cnt}, R{data:c1}\n"
                   "    MOVE R{state:cnt}, R0\n"
                   "    BR.NZ fwd\n"
                   "    MOVE R{state:cnt}, R{data:c8}\n"
                   + body + tail)
            entries = ([EntryPoint("hb")] if hb_entry else []) + \
                [EntryPoint("pb")]
            progs[cid] = CellProgram(
                inputs=[Port("w", register=1)],
                outputs=outs,
                entries=entries,
                data=data,
                state=[StateVar("cnt", register=2, initial_value=j + 1,
                                reset_per_batch=True, reset_value=j + 1)],
                assembly_template=asm)

        # ---------------- unlock relays ----------------
        # u1/u2 double as the HIGH-BIT trigger relay for final-block residues
        # whose pack sits more than 31 hops downstream of bnd (measured: m=4
        # resolved the direct hbgo at 32 and failed the build).
        hb_relay = self._hb_relay()
        for uid, nxt in (("u1", "u2go"), ("u2", "u3go"), ("u3", "ulkgo")):
            outs = [Port(nxt)]
            asm = f"go:\n    {{jump:{nxt}}}\n"
            if uid in hb_relay:
                outs.append(Port("hbj"))
                asm = "hbr:\n    {jump:hbj}\n    HALT\n" + asm
            progs[uid] = CellProgram(
                inputs=[], outputs=outs,
                entries=(([EntryPoint("hbr")] if uid in hb_relay else [])
                         + [EntryPoint("go")]),
                data=[], state=[],
                assembly_template=asm)

        # ---------------- the 13 limb groups ----------------
        for k in range(N_LIMBS):
            last = (k == N_LIMBS - 1)

            # ---- mulA_k
            a_data = [DataWord("z0", 0, address=7)]
            if last:
                a_data.append(DataWord("five", 5, address=8))
            mac7 = """\
    MULHI R{in:c}, R{in:a}
    MOVE R{state:t}, R0
    MUL R{in:c}, R{in:a}
    ADD R0, R{state:lo}
    MOVE R{state:lo}, R0
    ADC R{state:t}, R{state:hi}
    MOVE R{state:hi}, R0
"""
            if k == 0:
                smac = ("smac:\n    MOVE R{in:a}, R{in:ain}\n" + mac7
                        + "    MOVE R0, R{in:a}\n    {write:rot}\n"
                        "    MOVE R0, R{in:c}\n    {write:cfw}\n"
                        "    {jump:snx}\n")
            elif last:
                smac = ("smac:\n" + mac7
                        + "    MUL R{data:five}, R{in:a}\n    {write:rot}\n"
                        "    {jump:rtj}\n"
                        "    MOVE R{in:a}, R{in:ain}\n    {jump:snx}\n")
            else:
                smac = ("smac:\n" + mac7
                        + "    MOVE R0, R{in:a}\n    {write:rot}\n"
                        "    MOVE R{in:a}, R{in:ain}\n"
                        "    MOVE R0, R{in:c}\n    {write:cfw}\n"
                        "    {jump:snx}\n")
            xfer = ("xfer:\n    MOVE R0, R{state:hi}\n    {write:xh}\n"
                    "    MOVE R0, R{state:lo}\n    {write:xl}\n"
                    "    MOVE R{state:hi}, R{data:z0}\n"
                    "    MOVE R{state:lo}, R{data:z0}\n"
                    "    {jump:xnx}\n    HALT\n")
            outs = [Port("xh"), Port("xl"), Port("xnx"), Port("rot"),
                    Port("snx")]
            if last:
                outs.append(Port("rtj"))
            else:
                outs.append(Port("cfw"))
            progs[f"mulA_{k}"] = CellProgram(
                inputs=[Port("c", register=1), Port("ain", register=2),
                        Port("a", register=3)],
                outputs=outs,
                entries=[EntryPoint("xfer"), EntryPoint("smac")],
                data=a_data,
                state=[StateVar("hi", register=4), StateVar("lo", register=5),
                       StateVar("t", register=6)],
                assembly_template=xfer + smac)

            # ---- lh_k
            fp = self._face_to(lay, f"lh_{k}", f"mulA_{k}")
            fr = self._rest(lay, f"lh_{k}")
            # The pub delivery is a FLIPPED hop-1 write into an EARLIER cell:
            # declared, _apply_internal_feedback would re-patch its hop by
            # tracing the RESTING corridor (measured: @21, a permanent
            # ping-pong deadlock at the turn cells). So it is an AUTHORED
            # literal against mulA's pinned input registers (INV-63's escape),
            # with the abutment guaranteed by the serpentine (checked in
            # _face_to) and the registers asserted below.
            a_reg = next(pt.register for pt in progs[f"mulA_{k}"].inputs
                         if pt.name == "a")
            ain_reg = next(pt.register for pt in progs[f"mulA_{k}"].inputs
                           if pt.name == "ain")
            pub = ("pub:\n    MOVE [FACE], R{data:fp}\n"
                   "    MOVE R0, R{in:lv}\n    WRITE @1, %d\n" % a_reg
                   + ("    WRITE @1, %d\n" % ain_reg if k == 0 else "")
                   + "    MOVE [FACE], R{data:fr}\n    {jump:pnx}\n")
            outs = [Port("pnx"), Port("gco"), Port("gnx"),
                    Port("lb"), Port("fj")]
            progs[f"lh_{k}"] = CellProgram(
                inputs=[Port("lv", register=1), Port("vin", register=2),
                        Port("cin", register=3)],
                outputs=outs,
                entries=[EntryPoint("addv"), EntryPoint("gprobe"),
                         EntryPoint("fpub"), EntryPoint("pub")],
                data=[DataWord("fp", fp, address=5, is_face=True),
                      DataWord("fr", fr, address=6, is_face=True)],
                state=[StateVar("t", register=4)],
                assembly_template="""\
addv:
    ADD R{in:lv}, R{in:vin}
    MOVE R{in:lv}, R0
    HALT
gprobe:
    ADD R{in:lv}, R{in:cin}
    MOVE R{state:t}, R0
    SHR R{state:t}, #10
    {write:gco}
    {jump:gnx}
    HALT
fpub:
    MOVE R0, R{in:lv}
    {write:lb}
    {jump:fj}
    HALT
""" + pub)

            # ---- mulC_k
            fpC = self._face_to(lay, f"mulC_{k}", f"lh_{k}")
            frC = self._rest(lay, f"mulC_{k}")
            progs[f"mulC_{k}"] = CellProgram(
                inputs=[Port("hi", register=1), Port("lo", register=2),
                        Port("ain", register=3)],
                outputs=[Port("co"), Port("snx2")],
                entries=[EntryPoint("spl")],
                data=[DataWord("c64", 64, address=5),
                      DataWord("m1023", 1023, address=6),
                      DataWord("z0", 0, address=7),
                      DataWord("fp", fpC, address=8, is_face=True),
                      DataWord("fr", frC, address=9, is_face=True)],
                state=[StateVar("t", register=4)],
                assembly_template="""\
spl:
    ADD R{in:lo}, R{in:ain}
    MOVE R{in:lo}, R0
    ADC R{data:z0}, R{data:z0}
    MOVE R{state:t}, R0
    SHR R{in:lo}, #10
    MAC R{state:t}, R{data:c64}
    MAC R{in:hi}, R{data:c64}
    {write:co}
    MOVE R{in:hi}, R{data:z0}
    AND R{in:lo}, R{data:m1023}
    MOVE R{in:lo}, R0
    MOVE [FACE], R{data:fp}
    WRITE @1, 1
    MOVE [FACE], R{data:fr}
    {jump:snx2}
""")

            # ---- mulB_k
            fpB = self._face_to(lay, f"mulB_{k}", f"mulC_{k}")
            frB = self._rest(lay, f"mulB_{k}")
            progs[f"mulB_{k}"] = CellProgram(
                inputs=[Port("hi", register=1), Port("lo", register=2),
                        Port("ain", register=3)],
                outputs=[Port("co"), Port("nnx"), Port("x2nx")],
                entries=[EntryPoint("xfer2"), EntryPoint("nrm")],
                data=[DataWord("c64", 64, address=5),
                      DataWord("z0", 0, address=6),
                      DataWord("fp", fpB, address=7, is_face=True),
                      DataWord("fr", frB, address=8, is_face=True)],
                state=[StateVar("t", register=4)],
                assembly_template="""\
xfer2:
    MOVE [FACE], R{data:fp}
    MOVE R0, R{in:hi}
    WRITE @1, 1
    MOVE R0, R{in:lo}
    WRITE @1, 2
    MOVE [FACE], R{data:fr}
    {jump:x2nx}
    HALT
nrm:
    MOVE R0, R{in:hi}
    {write:co}
    MOVE R{in:hi}, R{data:z0}
    MULHI R{in:ain}, R{data:c64}
    MOVE R{state:t}, R0
    MUL R{in:ain}, R{data:c64}
    ADD R0, R{in:lo}
    MOVE R{in:lo}, R0
    ADC R{state:t}, R{data:z0}
    MOVE R{in:hi}, R0
    {jump:nnx}
""")

            # ---- fin_k
            bk = _BK[k]
            emit = k in _EMITTERS
            has_casc_in = emit and _EMITTERS.index(k) > 0
            fin_data = [DataWord("sk", sl[k], address=6),
                        DataWord("m255" if k == 12 else "m1023",
                                 255 if k == 12 else 1023, address=7)]
            head = """\
femit:
    ADD R{in:lb}, R{data:sk}
    MOVE R{in:lb}, R0
    ADD R{in:lb}, R{in:cin}
"""
            if k == 12:
                body = ("    AND R0, R{data:m255}\n"
                        "    MOVE R{in:lb}, R0\n"
                        "    SHL R{in:lb}, #8\n"
                        "    OR R0, R{in:pin}\n"
                        "    {write:wq}\n    {jump:wqj}\n")
            else:
                body = ("    MOVE R{state:t}, R0\n"
                        "    SHR R{state:t}, #10\n"
                        "    {write:co}\n"
                        "    AND R{state:t}, R{data:m1023}\n")
                if emit:
                    body += ("    MOVE R{in:lb}, R0\n"
                             "    SHL R{in:lb}, #%d\n"
                             "    OR R0, R{in:pin}\n"
                             "    {write:wq}\n    {jump:wqj}\n"
                             "    SHR R{in:lb}, #%d\n"
                             "    {write:pout}\n"
                             "    {jump:fnx}\n" % (bk, 16 - bk))
                elif bk == 0:
                    body += "    {write:pout}\n    {jump:fnx}\n"
                else:
                    body += ("    MOVE R{in:lb}, R0\n"
                             "    SHL R{in:lb}, #%d\n"
                             "    OR R0, R{in:pin}\n"
                             "    {write:pout}\n"
                             "    {jump:fnx}\n" % bk)
            wfw = ""
            outs = []
            ins = [Port("lb", register=1), Port("cin", register=2),
                   Port("pin", register=3)]
            if emit:
                outs += [Port("wq"), Port("wqj")]
            if has_casc_in:
                ins.append(Port("wf", register=5))
                wfw = ("wfw:\n    MOVE R0, R{in:wf}\n"
                       "    {write:cq}\n    {jump:cqj}\n    HALT\n")
                outs += [Port("cq"), Port("cqj")]
            if k != 12:
                outs += [Port("co"), Port("pout"), Port("fnx")]
            progs[f"fin_{k}"] = CellProgram(
                inputs=ins,
                outputs=outs,
                entries=([EntryPoint("wfw")] if has_casc_in else [])
                + [EntryPoint("femit")],
                data=fin_data,
                state=[StateVar("t", register=4)],
                assembly_template=wfw + head + body)

        # ---------------- closure relays ----------------
        def jrelay(jport: str) -> CellProgram:
            return CellProgram(
                inputs=[], outputs=[Port(jport)],
                entries=[EntryPoint("go")],
                data=[], state=[],
                assembly_template=f"go:\n    {{jump:{jport}}}\n")

        def vrelay(wport: str, jport: str) -> CellProgram:
            return CellProgram(
                inputs=[Port("v", register=1)],
                outputs=[Port(wport), Port(jport)],
                entries=[EntryPoint("go")],
                data=[], state=[],
                assembly_template=("go:\n    MOVE R0, R{in:v}\n"
                                   "    {write:%s}\n    {jump:%s}\n"
                                   % (wport, jport)))

        progs["crw"] = jrelay("wdj")
        progs["crx"] = jrelay("n1j")
        progs["crn"] = vrelay("sv", "nrj")
        progs["crs"] = vrelay("sv", "spj")
        progs["crg"] = vrelay("sv", "gpj")
        progs["crp"] = jrelay("stj")
        progs["crq"] = vrelay("wv", "wj")
        progs["rrel_a"] = CellProgram(
            inputs=[Port("v", register=1)],
            outputs=[Port("rv"), Port("rvj"), Port("x2go")],
            entries=[EntryPoint("rot"), EntryPoint("x2j")],
            data=[], state=[],
            assembly_template="rot:\n    MOVE R0, R{in:v}\n"
                              "    {write:rv}\n    {jump:rvj}\n    HALT\n"
                              "x2j:\n    {jump:x2go}\n")
        progs["rrel_b"] = CellProgram(
            inputs=[Port("v", register=1)],
            outputs=[Port("rv"), Port("x2go")],
            entries=[EntryPoint("rot"), EntryPoint("x2j")],
            data=[], state=[],
            assembly_template="rot:\n    MOVE R0, R{in:v}\n"
                              "    {write:rv}\n    HALT\n"
                              "x2j:\n    {jump:x2go}\n")

        # program order must equal layout order (positional pairing).
        ordered: Dict[str, CellProgram] = {}
        for cid in self._geometry():
            if cid in progs:
                ordered[cid] = progs[cid]
        assert len(ordered) == len(progs), (
            sorted(set(progs) - set(ordered)))
        return ordered

    # ---------------------------------------------------------------- wiring
    def _piece_targets(self, j: int) -> List[int]:
        """The limb targets of pack_j — mirrors build_cell_programs."""
        return {0: [0, 1], 1: [1, 2, 3], 2: [3, 4], 3: [4, 5, 6],
                4: [6, 7], 5: [8, 9], 6: [9, 10, 11], 7: [11, 12]}[j]

    def _hb_relay(self):
        """The u-relay CHAIN carrying bnd's high-bit trigger (compile-time).

        Packs 0-2 are within bnd's 31-hop reach (direct, ``[]``); packs 3-5
        route via u1; pack 6 via u1 THEN u2 (measured: the direct hop for m=4
        resolved at 32 and failed the build; u2 is beyond bnd's own reach)."""
        m = self._m_last
        if m >= BLOCK_WORDS or m <= 3:
            return []
        return ["u1"] if m <= 6 else ["u1", "u2"]

    def internal_connections(self) -> List[Tuple[Any, str, Any, str]]:
        m = self._m_last
        conns: List[Tuple[Any, str, Any, str]] = [
            ("seq_top", "w0", "pack_0", "w"),
            ("bnd", "fflag", "seq_spl", "ff"),
            # config-only serialize-LOCK release (INV-20 idiom).
            ("ulk", "unlock", "seq_top", "w"),
            ("rrom", "cw", "mulA_0", "c"),
            ("seq_nrm", "na", "mulB_0", "ain"),
            ("seq_nrm", "sa", "mulC_0", "ain"),
            ("seq_spl", "sa2", "mulC_0", "ain"),
            ("seq_fin", "gc", "lh_0", "cin"),
            ("seq_fin", "fc", "fin_0", "cin"),
            ("crn", "sv", "seq_nrm", "wv"),
            ("crs", "sv", "seq_spl", "sv"),
            ("crg", "sv", "seq_fin", "gv"),
            ("crq", "wv", "out", "wr"),
            ("rrel_a", "rv", "rrel_b", "v"),
            ("rrel_b", "rv", "mulA_0", "ain"),
            ("mulA_12", "rot", "rrel_a", "v"),
            ("mulB_12", "co", "crn", "v"),
            ("mulC_12", "co", "crs", "v"),
            ("lh_12", "gco", "crg", "v"),
            ("fin_12", "wq", "crq", "v"),
        ]
        for j in range(BLOCK_WORDS):
            cid = f"pack_{j}"
            if j < BLOCK_WORDS - 1:
                conns.append((cid, "wn", f"pack_{j + 1}", "w"))
            for k in self._piece_targets(j):
                conns.append((cid, f"v{k}", f"lh_{k}", "vin"))
        if m < BLOCK_WORDS:
            hb_limb = (16 * m) // 10
            conns.append((f"pack_{m - 1}", "hv", f"lh_{hb_limb}", "vin"))
        for k in range(N_LIMBS):
            conns += [
                (f"mulA_{k}", "xh", f"mulB_{k}", "hi"),
                (f"mulA_{k}", "xl", f"mulB_{k}", "lo"),
                (f"lh_{k}", "lb", f"fin_{k}", "lb"),
            ]
            # xfer2 (mulB->mulC), lvout (mulC->lh), pub (lh->mulA) are RAW
            # literal @1 flip writes — deliberately UNDECLARED (see pub).
            if k < N_LIMBS - 1:
                conns += [
                    (f"mulA_{k}", "rot", f"mulA_{k + 1}", "ain"),
                    (f"mulA_{k}", "cfw", f"mulA_{k + 1}", "c"),
                    (f"mulB_{k}", "co", f"mulB_{k + 1}", "ain"),
                    (f"mulC_{k}", "co", f"mulC_{k + 1}", "ain"),
                    (f"lh_{k}", "gco", f"lh_{k + 1}", "cin"),
                    (f"fin_{k}", "co", f"fin_{k + 1}", "cin"),
                    (f"fin_{k}", "pout", f"fin_{k + 1}", "pin"),
                ]
        # word cascade: each emitter posts/forwards to the NEXT emitter.
        for i, k in enumerate(_EMITTERS[:-1]):
            nxt = _EMITTERS[i + 1]
            conns.append((f"fin_{k}", "wq", f"fin_{nxt}", "wf"))
            if i > 0:
                conns.append((f"fin_{k}", "cq", f"fin_{nxt}", "wf"))
        conns.append(("fin_12", "cq", "crq", "v"))
        return conns

    def internal_jumps(self) -> List[Tuple[Any, str, Any, str]]:
        m = self._m_last
        jumps: List[Tuple[Any, str, Any, str]] = [
            ("seq_top", "pb0", "pack_0", "pb"),
            ("bnd", "pubgo", "lh_0", "pub"),
            ("bnd", "ubgo", "u1", "go"),
            ("seq_mul", "rq", "rrom", "get"),
            ("seq_mul", "rrst", "rrom", "rst"),
            ("seq_mul", "xj", "mulA_0", "xfer"),
            ("rrom", "sj", "mulA_0", "smac"),
            ("seq_nrm", "nj", "mulB_0", "nrm"),
            ("seq_nrm", "x2j2", "mulB_0", "xfer2"),
            ("seq_spl", "sj2", "mulC_0", "spl"),
            ("seq_spl", "gp", "seq_fin", "gp"),
            ("seq_spl", "ub", "u1", "go"),
            ("seq_fin", "gj", "lh_0", "gprobe"),
            ("seq_fin", "fst", "lh_0", "fpub"),
            ("u1", "u2go", "u2", "go"),
            ("u2", "u3go", "u3", "go"),
            ("u3", "ulkgo", "ulk", "go"),
            ("crw", "wdj", "bnd", "wdone"),
            ("crx", "n1j", "seq_nrm", "n1"),
            ("crn", "nrj", "seq_nrm", "nr"),
            ("crs", "spj", "seq_spl", "spr"),
            ("crg", "gpj", "seq_fin", "gpr"),
            ("crp", "stj", "seq_mul", "step"),
            ("crq", "wj", "out", "emit"),
            ("rrel_a", "x2go", "rrel_b", "x2j"),
            ("rrel_a", "rvj", "rrel_b", "rot"),
            ("rrel_b", "x2go", "mulC_0", "spl"),
            ("mulA_12", "snx", "crp", "go"),
            ("mulA_12", "rtj", "rrel_a", "rot"),
            ("mulA_12", "xnx", "crx", "go"),
            ("mulB_12", "nnx", "crn", "go"),
            ("mulB_12", "x2nx", "rrel_a", "x2j"),
            ("mulC_12", "snx2", "crs", "go"),
            ("lh_12", "gnx", "crg", "go"),
            ("lh_12", "pnx", "crp", "go"),
            ("lh_12", "fj", "fin_12", "femit"),
            ("fin_12", "wqj", "crq", "go"),
            ("fin_12", "cqj", "crq", "go"),
        ]
        if m < BLOCK_WORDS:
            hb_limb = (16 * m) // 10
            relay = self._hb_relay()
            hops = ["bnd"] + relay + [f"pack_{m - 1}"]
            for a, b2 in zip(hops, hops[1:]):
                sp = "hbgo" if a == "bnd" else "hbj"
                dp = "hb" if b2.startswith("pack_") else "hbr"
                jumps.append((a, sp, b2, dp))
            jumps.append((f"pack_{m - 1}", "hvj", f"lh_{hb_limb}", "addv"))
        for j in range(BLOCK_WORDS):
            cid = f"pack_{j}"
            for k in self._piece_targets(j):
                jumps.append((cid, f"v{k}j", f"lh_{k}", "addv"))
            if j < BLOCK_WORDS - 1:
                jumps.append((cid, "pbn", f"pack_{j + 1}", "pb"))
            else:
                jumps.append((cid, "bat", "crw", "go"))
        for k in range(N_LIMBS - 1):
            jumps += [
                (f"mulA_{k}", "snx", f"mulA_{k + 1}", "smac"),
                (f"mulA_{k}", "xnx", f"mulA_{k + 1}", "xfer"),
                (f"mulB_{k}", "nnx", f"mulB_{k + 1}", "nrm"),
                (f"mulB_{k}", "x2nx", f"mulB_{k + 1}", "xfer2"),
                (f"mulC_{k}", "snx2", f"mulC_{k + 1}", "spl"),
                (f"lh_{k}", "gnx", f"lh_{k + 1}", "gprobe"),
                (f"lh_{k}", "pnx", f"lh_{k + 1}", "pub"),
                (f"lh_{k}", "fj", f"fin_{k}", "femit"),
                (f"fin_{k}", "fnx", f"lh_{k + 1}", "fpub"),
            ]
        for i, k in enumerate(_EMITTERS[:-1]):
            nxt = _EMITTERS[i + 1]
            jumps.append((f"fin_{k}", "wqj", f"fin_{nxt}", "wfw"))
            if i > 0:
                jumps.append((f"fin_{k}", "cqj", f"fin_{nxt}", "wfw"))
        return jumps


    # ------------------------------------------------------------- reference
    def process_reference(self, input_words) -> np.ndarray:
        """Bit-exact reference: the EXACT cell schedule the chip runs.

        ``input_words`` is the message as raw little-endian 16-bit words (NOT
        Q15); only the first ``msg_words`` are consumed. Output: the 16-byte
        RFC 8439 tag as 8 little-endian words.
        """
        w = [int(v) & MASK16 for v in np.asarray(input_words).ravel()]
        w = w[:self.msg_words]
        if len(w) < self.msg_words:
            return np.array([], dtype=np.uint16)
        rl = self._r_limbs
        sl = self._s_limbs
        N = N_LIMBS

        a = [0] * N
        ain0 = 0
        hi = [0] * N
        lo = [0] * N
        lv = [0] * N

        n_blocks = (len(w) + BLOCK_WORDS - 1) // BLOCK_WORDS
        for b in range(n_blocks):
            blk = w[BLOCK_WORDS * b: BLOCK_WORDS * (b + 1)]
            mm = len(blk)
            for j, wv in enumerate(blk):
                for k, piece in pack_pieces(j, wv):
                    if j == 7 and k == 12:
                        piece += 256
                    lv[k] += piece
            if mm < BLOCK_WORDS:
                hb_limb = (16 * mm) // 10
                lv[hb_limb] += 1 << (16 * mm - 10 * hb_limb)
            # pub
            for k in range(N):
                a[k] = lv[k]
            ain0 = lv[0]
            # 13 serial smac passes
            for i in range(N):
                c = rl[i]
                prev = None
                for k in range(N):
                    if k == 0:
                        a[0] = ain0
                    av = a[k]
                    acc = ((hi[k] << 16) | lo[k]) + c * av
                    hi[k], lo[k] = (acc >> 16) & MASK16, acc & MASK16
                    if k == 0:
                        prev = av
                    elif k == N - 1:
                        ain0 = (5 * av) & MASK16
                        a[k] = prev
                    else:
                        a[k], prev = prev, av
            # xfer
            hiB, loB = list(hi), list(lo)
            hi = [0] * N
            lo = [0] * N
            # two nrm rounds
            w1 = 0
            for rnd in range(2):
                cin = (5 * w1) & MASK16
                for k in range(N):
                    fwd = hiB[k]
                    acc = loB[k] + 64 * cin
                    hiB[k], loB[k] = (acc >> 16) & MASK16, acc & MASK16
                    cin = fwd
                w1 = cin
            # spl rounds; first seed folds the nrm wrap: 320 * hi12_r2
            seed = (320 * w1) & MASK16
            while True:
                cin = seed
                for k in range(N):
                    v = loB[k] + cin
                    vlo, carry = v & MASK16, v >> 16
                    co = (vlo >> 10) + 64 * carry + 64 * hiB[k]
                    hiB[k] = 0
                    loB[k] = vlo & LIMB_MASK
                    lv[k] = loB[k]
                    cin = co
                if cin == 0:
                    break
                seed = (5 * cin) & MASK16
        # gprobe
        cin = 5
        for k in range(N):
            cin = (lv[k] + cin) >> 10
        f = 1 if cin else 0
        # finish chain
        cin = 5 * f
        out_words: List[int] = []
        partial = 0
        for k in range(N):
            v = lv[k] + sl[k] + cin
            cin = v >> 10
            limb = v & (0xFF if k == 12 else LIMB_MASK)
            bk = _BK[k]
            merged = (partial | ((limb << bk) & MASK16)) & MASK16
            if bk + 10 >= 16 or k == 12:
                out_words.append(merged)
                partial = limb >> (16 - bk) if k < 12 else 0
            else:
                partial = merged
        return np.array(out_words, dtype=np.uint16)

    def reset(self):
        """One message per run (a Poly1305 key is one-time by specification);
        the counters re-arm per batch via ``reset_per_batch``."""
        pass
