# SPDX-License-Identifier: GPL-3.0-or-later
"""GRUCellBlock — see :class:`GRUCellBlock`.

One composite multi-cell block implementing a full GRU timestep (H=4 hidden
units, I=2 inputs) WITH its 4-class linear readout head. The recurrence is
entirely INTERNAL (GRC flowgraphs are acyclic — there is no legal external
wire from the state update back to the gate inputs): the hidden state lives
in pinned STATE registers inside the block and is written back each timestep
by the block's own cells.

ARCHITECTURE (48 program cells + 2 closure transits, one 7x8 ring serpentine)
-----------------------------------------------------------------------------
The whole block is ONE closed serial ring (the FLLBandEdge column-pair fold:
a head row + three boustrophedon column pairs, the chain end closing back
into the head row's first cell). Every internal WRITE/JUMP rides the ring
forward at its ring-distance @N (all distances <= 27), so there are no
against-the-grain corridors anywhere.

Dataflow per timestep (x = 2 Q15 feature words in, one raw class word out):

* ``fin`` (input landing) forwards each feature word to ``hstr`` and, on the
  second word, engages its arbiter LOCK — the TIMESTEP BARRIER. The next
  timestep's words wait at the port until ``amx`` clears the lock, so exactly
  one timestep is ever in flight (the FFT16 chain-END-unlock rule).
* ``hstr`` (state streamer) holds h0..h3 as pinned state registers and
  streams the 6-word operand vector [x0, x1, h0..h3] into the MAC-row chain
  (x words relayed as they arrive; h words LOAD-walked in a burst — link
  backpressure paces the chain, which is purely linear).
* 16 uniform MAC-row walk cells (``r0..r3, z0..z3, u0..u3, xc0..xc3`` in the
  DotProductMACBlock coefficient-walk idiom, one row per cell): each taps the
  passing stream, forwards it, and after the 6th word emits its RAW
  (2^-S-scaled) accumulator to its consumer. r/z rows hold the full 7-term
  gate rows; u rows hold [0, 0, Whn[i,:]] (the Whn.h part); xc rows hold
  [Wxn[i,:], 0, 0, 0, 0] with the n-gate bias preloaded.
* Per unit i: ONE shared sigmoid engine (``sf_i``/``sl_i`` — the landed
  17-entry table+interp cell pair imported verbatim from
  ``activation_blocks``) serially activates r_i then z_i (their arrivals are
  separated by three chain cells' processing time). This forces ONE COMMON
  scale S_rz across BOTH gates (the engine's dshift is baked per instance);
  the sigmoid dshift is S_rz - 3, the zero-instruction scale restore.
* ``umA_i`` counting-joins {sig(r), sig(z), u, xc} in any arrival order (the
  AddBlock toggle-counter idiom, count 4; r-before-z within the sigmoid arm
  is causal), then computes the n-gate preactivation word
  ``MULQ(r, u) + xw`` (the reset gate applied AFTER the Whn matmul — the
  trained/PyTorch GRU form; the elementwise-first variant computes a
  DIFFERENT function and measurably collapses accuracy) and feeds the
  per-unit tanh engine (``tf_i``/``tl_i``, dshift = S_n - 2).
* ``umB_i`` blends ``h' = sat(MULQ(0x7FFF - z, n) + MULQ(z, h))`` — the
  pinned overflow-safe form (both partials < 1, ONE saturating add;
  ``n + z*(h-n)`` overflows and is not used). Its own previous output IS
  h_i, kept locally in a pinned state register — no delivery needed.
* ``hcol`` relays each arriving h'_i (four entries, one per unit — index by
  construction, order-free) BOTH back into ``hstr``'s h_i state register
  (the recurrence write-back, ring-forward through the closure transits and
  ``fin``) and into the head chain.
* ``hd0..hd3`` are the same walk cells at K=4 holding the head rows at ONE
  COMMON head scale (per-row head scales would corrupt the argmax; the
  weights file's head block carries one shared exponent). Their raw
  accumulator words ride the head strip corridor IN ORDER into ``amx``.
* ``amx`` is the BinArgmaxBlock signed running-max (CMP + BR.GE on the SLT
  flag — overflow-corrected; first occurrence wins) over the 4 head words,
  emits the winning class index as a RAW word (0..3), then clears ``fin``'s
  LOCK with a backward-riding WRITE.CFG (ring-forward @3) — releasing the
  next timestep only after the entire pipeline has drained.

WEIGHTS
-------
``weights_file`` names the trained-model JSON (schema:
``examples/gru_classifier/ml/README.md``; the shipped single-layer model is
bundled next to this module as ``gru_weights_default.json`` and used when
``weights_file`` is empty). All ~112 weight/bias words are resolved to
cell-local constants at build time from the file's quantized ``{q, e}``
blocks via the landed ``scale_schedule`` (post-rounding guard re-verified on
load at the derived common scales; a violation raises). No per-weight GRC
params exist. :meth:`weight_location_manifest` emits the machine-readable
map of which memory word in which cell holds which weight constant — the
contract a downstream live-weight-swap capability depends on. Schema::

    {"format": "gru-cell-weight-map-v1",
     "scales": {"S_rz": int, "S_n": int, "S_head": int,
                "dshift_sigmoid": int, "dshift_tanh": int},
     "cells": {<cell_id>: {<address>: {"name": <str>, "value": <int>}}}}

where ``name`` is e.g. ``"Wx.r[1][0]"``, ``"Wh.n[2][3]"``, ``"b.z[1]"``,
``"head.Wo[3][2]"``, ``"head.bo[0]"`` (indices into the weights-file
matrices) and ``value`` is the stored prescaled Q15 word (uint16).

STATE CONTRACT
--------------
h persists across bursts/batches (``h = 0 at stream start only; never reset
while streaming`` — the trained model's deployment contract). No state is
``reset_per_batch``.

No stock GNU Radio counterpart: the goldens are the block's own bit-exact
integer model (mirrored by ``examples/gru_classifier/ml/gru_reference_chip``)
and the float GRU (numpy) — the library's established pattern for
no-counterpart blocks. Rate: 2 feature words in -> 1 raw class word out.
"""
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock
from .activation_blocks import (SIGMOID_TABLE_Q15, TANH_TABLE_Q15,
                                activation_fold_program,
                                activation_lut_program,
                                activation_patch_words, activation_ref_word)
from .dot_product_mac_block import scale_schedule

_DEFAULT_WEIGHTS = Path(__file__).resolve().parent / "gru_weights_default.json"

SIG_K = 3    # sigmoid canonical half-domain 2^3
TANH_K = 2   # tanh canonical half-domain 2^2

_GATES = ("r", "z", "n")


def _s16(v: int) -> int:
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def _mulq(a: int, b: int) -> int:
    """Truncating Q15 product (the cell MULQ): (a*b) >> 15, arithmetic."""
    return (_s16(a) * _s16(b)) >> 15


def _sat_add16(a: int, b: int) -> int:
    """Saturating 16-bit add (ADD + V-flag rail pin)."""
    t = _s16(a) + _s16(b)
    return 32767 if t > 32767 else (-32768 if t < -32768 else t)


def _quantize_at(vals, S: int) -> List[int]:
    """``round(v * 2^-S * 32768)`` clipped to int16 — the stored-word rule."""
    return [max(-32768, min(32767, round(float(v) * (2.0 ** -S) * 32768.0)))
            for v in vals]


def _common_scale_rows(rows) -> Tuple[int, List[Tuple[List[int], int]]]:
    """One COMMON headroom scale over a set of ``(coefficients, bias)`` rows:
    S = max of the landed per-row ``scale_schedule`` S, then every row is
    requantized at the common S with the POST-ROUNDING GUARD re-verified per
    row (sum|q| <= 32767 — the no-wrap invariant; S bumps until it holds)."""
    S = 0
    for c, b in rows:
        S = max(S, scale_schedule(list(c), float(b))[0])
    while True:
        out, ok = [], True
        for c, b in rows:
            qs = _quantize_at(list(c) + [float(b)], S)
            if sum(abs(q) for q in qs) > 32767:
                ok = False
                break
            out.append((qs[:-1], qs[-1]))
        if ok:
            return S, out
        S += 1


def _mac_walk_ref(coeff_q: List[int], bias_q: int, xs: List[int]) -> int:
    """Bit-exact model of one MAC-row walk: bias preload + truncating
    MULQ/accumulate in stream order (guard => the int16 acc never wraps)."""
    acc = int(bias_q)
    for c, x in zip(coeff_q, xs):
        acc = _s16(acc + ((int(c) * _s16(x)) >> 15))
    return acc


class GRUCellBlock(KyttarBlock):
    """GRU timestep (H=4, I=2) + 4-class argmax readout, recurrence internal.

    See the module docstring for the full architecture, the weights-file
    contract, and the weight-location-manifest schema.

    Parameters:
        hidden: hidden state size. HARDWARE-VERIFIED CONFIGURATION: 4 —
            the placed 7x8 ring, the per-unit engine count, and the common
            gate scales are derived and verified for H=4; any other value
            RAISES (never silently reshapes).
        inputs: feature vector size per timestep. Verified configuration: 2.
        classes: readout classes. Verified configuration: 4.
        weights_file: path to the trained-model JSON (schema in
            examples/gru_classifier/ml/README.md). Empty (the default) uses
            the bundled trained single-layer model
            (``gru_weights_default.json``). A relative path is resolved
            against the current directory, then against the bundled
            directory; a missing file RAISES.

    Interface: entry = ``fin``'s ``feat`` entry, input register R0 of
    ``fin``, output register R0 of ``amx`` (one RAW class word 0..3 per 2
    input words — an index, NOT a Q15 sample; a float scope shows it as
    index/32768, cf. BinArgmaxBlock's raw-word convention).
    """

    CATEGORY = "demodulation"
    TAGS = ["gru", "classifier", "neural", "recurrent", "argmax",
            "demodulation"]

    _interface = BlockInterface(
        entry_address=1, input_registers=[0], output_registers=[0])

    H = 4
    I = 2
    C = 4

    def __init__(self, name: str, hidden: int = 4, inputs: int = 2,
                 classes: int = 4, weights_file: str = ""):
        if int(hidden) != self.H or int(inputs) != self.I or \
                int(classes) != self.C:
            raise ValueError(
                f"HARDWARE-VERIFIED CONFIGURATION: GRUCellBlock is built and "
                f"verified for hidden=4, inputs=2, classes=4 (the placed 7x8 "
                f"ring and its scales are derived for exactly this shape); "
                f"got hidden={hidden}, inputs={inputs}, classes={classes}. "
                f"Not silently reshaping.")
        super().__init__(name, hidden=int(hidden), inputs=int(inputs),
                         classes=int(classes), weights_file=str(weights_file))
        self._weights_file = str(weights_file)
        path = self._resolve_weights_path(self._weights_file)
        self._weights_path = path
        params = json.loads(path.read_text())
        self._derive_constants(params)

    # ------------------------------------------------------------------ setup
    @staticmethod
    def _resolve_weights_path(weights_file: str) -> Path:
        if not weights_file:
            return _DEFAULT_WEIGHTS
        p = Path(weights_file)
        if p.is_file():
            return p
        alt = _DEFAULT_WEIGHTS.parent / weights_file
        if not p.is_absolute() and alt.is_file():
            return alt
        raise ValueError(
            f"GRUCellBlock weights_file not found: {weights_file!r} "
            f"(tried {p} and {alt}). The weights JSON schema is documented "
            f"in examples/gru_classifier/ml/README.md.")

    def _derive_constants(self, params: dict) -> None:
        """Derive every on-chip constant from the weights file's quantized
        ``{q, e}`` blocks (dequantized exactly, requantized by the landed
        scale schedule at the derived common scales)."""
        try:
            lp = params["layers"][0]["quant"]
            hq = params["head"]["quant"]
        except (KeyError, IndexError, TypeError) as e:
            raise ValueError(
                f"GRUCellBlock weights file {self._weights_path} does not "
                f"match the expected schema (see examples/gru_classifier/ml/"
                f"README.md): {e}") from e

        def deq(m):
            q = np.asarray(m["q"], dtype=np.float64)
            return q * (2.0 ** int(m["e"])) / 32768.0

        Wx = {g: deq(lp["Wx"][g]) for g in _GATES}
        Wh = {g: deq(lp["Wh"][g]) for g in _GATES}
        b = {g: np.asarray(lp["b"][g], dtype=np.float64) / 32768.0
             for g in _GATES}
        H, I = self.H, self.I
        for g in _GATES:
            if Wx[g].shape != (H, I) or Wh[g].shape != (H, H) or \
                    len(b[g]) != H:
                raise ValueError(
                    f"weights file gate '{g}' shapes {Wx[g].shape}/"
                    f"{Wh[g].shape}/{len(b[g])} do not match H={H}, I={I}")

        # r/z: full 7-term rows at ONE COMMON scale (shared sigmoid engine).
        rz = ([(list(Wx["r"][i]) + list(Wh["r"][i]), float(b["r"][i]))
               for i in range(H)]
              + [(list(Wx["z"][i]) + list(Wh["z"][i]), float(b["z"][i]))
                 for i in range(H)])
        self._S_rz, both = _common_scale_rows(rz)
        self._r_rows, self._z_rows = both[:H], both[H:]

        # n gate, split rows at one common S_n: u rows [0,0,Whn[i,:]] (bias
        # 0), xc rows [Wxn[i,:],0,0,0,0] (bias bn) — the combine
        # MULQ(r,u)+xw then needs no shifts, and the no-wrap guard covers
        # the summed parts (|MULQ(r,u)| <= sum|Whn_q|).
        full_n = [(list(Wx["n"][i]) + list(Wh["n"][i]), float(b["n"][i]))
                  for i in range(H)]
        S = _common_scale_rows(full_n)[0]
        while True:
            u_rows = [(_quantize_at([0.0] * I + list(Wh["n"][i]), S), 0)
                      for i in range(H)]
            xc_rows = [(_quantize_at(list(Wx["n"][i]) + [0.0] * H, S),
                        _quantize_at([b["n"][i]], S)[0]) for i in range(H)]
            if all(sum(abs(q) for q in u_rows[i][0])
                   + sum(abs(q) for q in xc_rows[i][0])
                   + abs(xc_rows[i][1]) <= 32767 for i in range(H)):
                break
            S += 1
        self._S_n = S
        self._u_rows, self._xc_rows = u_rows, xc_rows

        # head: K=4 rows at ONE COMMON scale (argmax scale-consistency).
        eo = int(hq["e"])
        Wo = np.asarray(hq["Wo_q"], dtype=np.float64) * (2.0 ** eo) / 32768.0
        bo = np.asarray(hq["bo_acc"], dtype=np.float64) * (2.0 ** eo) / (
            1 << 30)
        if Wo.shape != (self.C, H) or len(bo) != self.C:
            raise ValueError(
                f"weights file head shapes {Wo.shape}/{len(bo)} do not "
                f"match classes={self.C}, H={H}")
        self._S_head, self._head_rows = _common_scale_rows(
            [(list(Wo[j]), float(bo[j])) for j in range(self.C)])

        self._dshift_sig = self._S_rz - SIG_K
        self._dshift_tanh = self._S_n - TANH_K
        self._ppos, self._pneg = activation_patch_words()

    # ------------------------------------------------------------------ props
    @property
    def cell_count(self) -> int:
        # 49 program cells (incl. the off-ring output relay) + 2
        # ring-closure transit cells (first-class).
        return 51

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    @property
    def weights_file(self) -> str:
        return self._weights_file

    @property
    def scale_shifts(self) -> Dict[str, int]:
        """The derived headroom scales (read-only metadata)."""
        return {"S_rz": self._S_rz, "S_n": self._S_n,
                "S_head": self._S_head,
                "dshift_sigmoid": self._dshift_sig,
                "dshift_tanh": self._dshift_tanh}

    def output_cell_id(self) -> Optional[str]:
        """The class word leaves the OFF-RING ``oout`` relay (the routed
        egress must not own any ring cell's resting face)."""
        return "oout"

    def output_cell_ids(self) -> List[str]:
        return ["oout"]

    # -------------------------------------------------------- cell builders
    _AMX_FACE_OUT_ADDR = 5

    def _fin_program(self) -> CellProgram:
        """Input landing: toggle x0/x1, forward each to ``hstr``; on x1
        engage the arbiter LOCK (the timestep barrier — LOCK_FACE = the
        ring-inbound face, so the unlock WRITE.CFG and the h write-back
        transit are admitted while the port corridor is held) and fire
        ``hstr``'s h-stream. Snapshot-first + lock-before-forward keeps the
        next timestep's port word out of the running program."""
        return CellProgram(
            inputs=[Port("f", register=0, entry="feat")],
            outputs=[Port("xf"), Port("goj")],
            entries=[EntryPoint("feat")],
            data=[DataWord("one", 1, address=1),
                  DataWord("lockface", 0, address=2, is_face=True)],
            state=[StateVar("tog", register=3), StateVar("xs", register=4)],
            assembly_template="""\
feat:
    MOVE R{state:xs}, R{in:f}
    MOVE R0, R{data:one}
    SUB R0, R{state:tog}
    BR.Z second
    MOVE R{state:tog}, R0
    MOVE R0, R{state:xs}
    {write:xf}
    {jump:xf}
    HALT
second:
    MOVE R{state:tog}, R0
    MOVE R0, R{data:lockface}
    MOVE [LOCK_FACE], R0
    MOVE R0, R{data:one}
    MOVE [LOCK], R0
    MOVE R0, R{state:xs}
    {write:xf}
    {jump:xf}
    {jump:goj}
""",
        )

    def _hstr_program(self) -> CellProgram:
        """State streamer: holds h0..h3 (pinned states, written back by
        ``hcol`` each timestep — a feedback landing is a pinned STATE
        register, never an input Port). ``xin`` relays the port-paced x
        words; ``go`` LOAD-walks h0..h3 into the chain (link backpressure
        paces the burst; the fin LOCK bars the next timestep)."""
        return CellProgram(
            inputs=[Port("xw", register=1, entry="xin")],
            outputs=[Port("s"), Port("s2")],
            entries=[EntryPoint("xin"), EntryPoint("go")],
            data=[DataWord("one", 1, address=2),
                  # value 4 = the h-word count AND h0's register address
                  DataWord("four", 4, address=3)],
            state=[StateVar("h0", register=4), StateVar("h1", register=5),
                   StateVar("h2", register=6), StateVar("h3", register=7),
                   StateVar("idx", register=8), StateVar("scnt", register=9)],
            assembly_template="""\
xin:
    MOVE R0, R{in:xw}
    {write:s}
    {jump:s}
    HALT
go:
    MOVE R{state:scnt}, R{data:four}
    MOVE R{state:idx}, R{data:four}
loop:
    LOAD R{state:idx}
    {write:s2}
    {jump:s2}
    ADD R{state:idx}, R{data:one}
    MOVE R{state:idx}, R0
    SUB R{state:scnt}, R{data:one}
    MOVE R{state:scnt}, R0
    BR.NZ loop
""",
        )

    @staticmethod
    def _row_program(coeff_q: List[int], bias_q: int, *,
                     forward: bool) -> CellProgram:
        """One MAC-row walk cell (the DotProductMACBlock coefficient-walk
        idiom + the delay-line stream forward): per trigger, snapshot the
        arriving stream word, forward it (mid-chain cells), LOAD-walk the
        coefficient, MULQ-accumulate; on the K-th word emit the RAW
        accumulator to the row's consumer and re-arm (bias preload)."""
        K = len(coeff_q)
        data = [DataWord(f"c{j}", int(coeff_q[j]) & 0xFFFF, address=1 + j)
                for j in range(K)]
        data += [DataWord("one", 1, address=K + 1),
                 DataWord("kend", K + 1, address=K + 2),
                 DataWord("biasw", int(bias_q) & 0xFFFF, address=K + 3)]
        state = [StateVar("xs", register=K + 4),
                 StateVar("acc", register=K + 5,
                          initial_value=int(bias_q) & 0xFFFF),
                 StateVar("idx", register=K + 6, initial_value=1)]
        # {write:res} and {jump:resj} are SEPARATE ports: the router resolves
        # a port's jump ENTRY from its internal_connection (default entry)
        # BEFORE consulting internal_jumps, so a jump that must land on a
        # NAMED entry (the umA counting-join ``cnt``) has to ride a port with
        # NO data connection of its own.
        outputs = ([Port("fwd")] if forward else []) + [Port("res"),
                                                        Port("resj")]
        # The stream forward happens at the END of the program (not the
        # start): the wavefront then advances one cell per FULL program
        # (~17 instr), which makes every row's res emission STRUCTURALLY
        # serial — in particular r_i's sigmoid word leaves ~3 full cell
        # programs before z_i's, so the shared per-unit engine is provably
        # idle between the two (an early forward shrinks the spacing to a
        # few instructions and the corridor-length difference then RACES
        # the r/z order at the engine — measured, not theoretical).
        if forward:
            tail = """\
fwd_:
    MOVE R0, R{state:xs}
    {write:fwd}
    {jump:fwd}
"""
            brt = "fwd_"
        else:
            tail = """\
fwd_:
    HALT
"""
            brt = "fwd_"
        template = ("""\
start:
    MOVE R{state:xs}, R{in:v}
    LOAD R{state:idx}
    MULQ R0, R{state:xs}
    ADD R{state:acc}, R0
    MOVE R{state:acc}, R0
    ADD R{state:idx}, R{data:one}
    MOVE R{state:idx}, R0
    CMP R{state:idx}, R{data:kend}
    BR.NZ """ + brt + """
    MOVE R0, R{state:acc}
    {write:res}
    {jump:resj}
    MOVE R{state:acc}, R{data:biasw}
    MOVE R{state:idx}, R{data:one}
""" + tail)
        return CellProgram(
            inputs=[Port("v", register=0)],
            outputs=outputs,
            entries=[EntryPoint("default")],
            data=data, state=state,
            assembly_template=template,
        )

    def _uma_program(self) -> CellProgram:
        """Unit join + n-gate combine: counting join (count 4, any order)
        over {sig(r), sig(z), u, xc}; the shared sigmoid engine delivers r
        then z serially into ``s`` (a toggle files them). On the 4th arm:
        forward z to ``umB`` and emit the tanh input ``MULQ(r, u) + xw``
        (plain ADD — the split-row guard makes wrap impossible)."""
        return CellProgram(
            inputs=[Port("s", register=1, entry="sig"),
                    Port("u", register=2, entry="cnt"),
                    Port("xw", register=3, entry="cnt")],
            outputs=[Port("zf"), Port("nw")],
            entries=[EntryPoint("sig"), EntryPoint("cnt")],
            data=[DataWord("one", 1, address=4),
                  DataWord("four", 4, address=5)],
            state=[StateVar("tog", register=6), StateVar("rs", register=7),
                   StateVar("zs", register=8), StateVar("jc", register=9)],
            assembly_template="""\
sig:
    MOVE R0, R{data:one}
    SUB R0, R{state:tog}
    BR.Z zp
    MOVE R{state:tog}, R0
    MOVE R{state:rs}, R{in:s}
    BR.NZ cnt
zp:
    MOVE R{state:tog}, R0
    MOVE R{state:zs}, R{in:s}
cnt:
    ADD R{state:jc}, R{data:one}
    MOVE R{state:jc}, R0
    CMP R{state:jc}, R{data:four}
    BR.NZ fin
    SUB R0, R0
    MOVE R{state:jc}, R0
    MOVE R0, R{state:zs}
    {write:zf}
    MULQ R{state:rs}, R{in:u}
    ADD R0, R{in:xw}
    {write:nw}
    {jump:nw}
fin:
    HALT
""",
        )

    def _umb_program(self) -> CellProgram:
        """Blend: ``h' = sat(MULQ(0x7FFF - z, n) + MULQ(z, h))``. ``zs`` is
        deposited by ``umA`` (pinned state); ``hs`` is this unit's OWN
        previous output — the recurrence lives here (initial 0 = cold
        start; NEVER reset while streaming). The saturating add pins the
        V-flag overflow to ``0x7FFF + signbit`` (the INV-13 rail; both
        partials share the true result's sign on overflow)."""
        return CellProgram(
            inputs=[Port("n", register=1)],
            outputs=[Port("hu"), Port("hj")],
            entries=[EntryPoint("default")],
            data=[DataWord("m7fff", 0x7FFF, address=2)],
            state=[StateVar("zs", register=3), StateVar("hs", register=4),
                   StateVar("t", register=5)],
            assembly_template="""\
start:
    SUB R{data:m7fff}, R{state:zs}
    MOVE R{state:t}, R0
    MULQ R{state:t}, R{in:n}
    MOVE R{state:t}, R0
    MULQ R{state:zs}, R{state:hs}
    ADD R0, R{state:t}
    BR.NV ok
    SHR R{state:t}, #15
    ADD R0, R{data:m7fff}
ok:
    MOVE R{state:hs}, R0
    {write:hu}
    {jump:hj}
""",
        )

    def _hcol_program(self) -> CellProgram:
        """h' collector/relay: four entries, one per unit (index by
        construction — arrival order is irrelevant). Each relays the unit's
        h' BOTH back into ``hstr``'s h_i state register (the recurrence
        write-back, riding the ring through the closure transits and
        ``fin``'s allowed lock face) and into the head chain."""
        lines = []
        for i in range(self.H):
            lines.append(f"e{i}:")
            lines.append(f"    MOVE R0, R{{in:hp{i}}}")
            lines.append(f"    {{write:hb{i}}}")
            lines.append(f"    {{write:hs{i}}}")
            lines.append(f"    {{jump:hs{i}}}")
            lines.append("    HALT")
        return CellProgram(
            inputs=[Port(f"hp{i}", register=1 + i, entry=f"e{i}")
                    for i in range(self.H)],
            outputs=([Port(f"hb{i}") for i in range(self.H)]
                     + [Port(f"hs{i}") for i in range(self.H)]),
            entries=[EntryPoint(f"e{i}") for i in range(self.H)],
            data=[], state=[],
            assembly_template="\n".join(lines) + "\n",
        )

    def _amx_program(self) -> CellProgram:
        """Running argmax over the 4 serially-arriving head words (the
        BinArgmaxBlock SLT-branch idiom: ``CMP maxv, x`` + ``BR.GE`` is the
        overflow-corrected signed strictly-greater update; first occurrence
        wins), emitting the RAW class index, then clearing ``fin``'s LOCK
        (WRITE.CFG ring-forward — the chain-END unlock bounds in-flight
        timesteps to ONE). Dual-face: ``out`` on ``face_out`` (route-aimed
        by the build via ``output_face_addr``), everything else on the
        resting ring face."""
        return CellProgram(
            inputs=[Port("w", register=1)],
            outputs=[Port("out_f"), Port("unlock")],
            entries=[EntryPoint("default")],
            data=[DataWord("one", 1, address=2),
                  DataWord("nfrm", self.C, address=3),
                  DataWord("minw", 0x8000, address=4),
                  DataWord("face_out", 2, address=self._AMX_FACE_OUT_ADDR,
                           is_face=True),
                  DataWord("face_ring", 3, address=6, is_face=True)],
            state=[StateVar("xs", register=7),
                   StateVar("maxv", register=8, initial_value=0x8000),
                   StateVar("cm", register=9, initial_value=self.C),
                   StateVar("cnt", register=10, initial_value=self.C)],
            assembly_template="""\
start:
    MOVE R{state:xs}, R{in:w}
    CMP R{state:maxv}, R{state:xs}
    BR.GE skip
    MOVE R{state:maxv}, R{state:xs}
    MOVE R{state:cm}, R{state:cnt}
skip:
    SUB R{state:cnt}, R{data:one}
    MOVE R{state:cnt}, R0
    BR.NZ done
    SUB R0, R0
    WRITE.CFG @3, 4
    MOVE [FACE], R{data:face_out}
    SUB R{data:nfrm}, R{state:cm}
    {write:out_f}
    {jump:out_f}
    MOVE [FACE], R{data:face_ring}
    MOVE R{state:cnt}, R{data:nfrm}
    MOVE R{state:cm}, R{data:nfrm}
    MOVE R{state:maxv}, R{data:minw}
done:
    HALT
""",
        )

    @staticmethod
    def _oout_program() -> CellProgram:
        """The OFF-RING output relay. The block's routed egress sources HERE,
        so the route's fwd_face override lands on a cell nothing transits —
        every ring cell keeps its authored face at every build stage (the
        route-time face-trace of the h write-back and the unlock corridor
        both cross ``amx``, which must therefore never be route-owned)."""
        return CellProgram(
            inputs=[Port("w", register=1)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[], state=[],
            assembly_template="""\
start:
    MOVE R0, R{in:w}
    {write:out}
    {jump:out}
""",
        )

    # ------------------------------------------------------------ assembly
    def _chain_ids(self) -> List[str]:
        """The 16 MAC-row chain cells in stream order (unit-major bands)."""
        out = []
        for i in range(self.H):
            out += [f"r{i}", f"u{i}", f"xc{i}", f"z{i}"]
        return out

    def _ring_ids(self) -> List[str]:
        """All 48 program cells in ring (= dict = layout) order."""
        ids = ["fin", "hstr"] + self._chain_ids()
        for i in range(self.H):
            ids += [f"sf{i}", f"sl{i}", f"umA{i}", f"tf{i}", f"tl{i}",
                    f"umB{i}"]
        # ``oout`` sits mid-dict (before hcol) so that ``amx``'s POSITIONAL
        # next cell is the first ring-closure transit — the route-time face
        # fallback then keeps amx's face on the ring (the FFT16 rule).
        ids += ["oout", "hcol"] + [f"hd{j}" for j in range(self.C)] + ["amx"]
        return ids

    def build_cell_programs(self) -> Dict[str, CellProgram]:
        progs: Dict[str, CellProgram] = {
            "fin": self._fin_program(), "hstr": self._hstr_program()}
        rows = {"r": self._r_rows, "u": self._u_rows, "z": self._z_rows,
                "xc": self._xc_rows}
        for i in range(self.H):
            for kind in ("r", "u", "xc", "z"):
                cq, bq = rows[kind][i]
                forward = not (kind == "z" and i == self.H - 1)
                progs[f"{kind}{i}"] = self._row_program(
                    cq, bq, forward=forward)
        # per-unit engines + unit math (dict order == ring order)
        reordered: Dict[str, CellProgram] = {
            "fin": progs["fin"], "hstr": progs["hstr"]}
        for cid in self._chain_ids():
            reordered[cid] = progs[cid]
        for i in range(self.H):
            reordered[f"sf{i}"] = activation_fold_program(
                self._dshift_sig, self._ppos, self._pneg)
            reordered[f"sl{i}"] = activation_lut_program(
                SIGMOID_TABLE_Q15, 0x8000)
            reordered[f"umA{i}"] = self._uma_program()
            reordered[f"tf{i}"] = activation_fold_program(
                self._dshift_tanh, self._ppos, self._pneg)
            reordered[f"tl{i}"] = activation_lut_program(
                TANH_TABLE_Q15, 0x0000)
            reordered[f"umB{i}"] = self._umb_program()
        reordered["oout"] = self._oout_program()
        reordered["hcol"] = self._hcol_program()
        for j in range(self.C):
            cq, bq = self._head_rows[j]
            reordered[f"hd{j}"] = self._row_program(
                cq, bq, forward=(j != self.C - 1))
        reordered["amx"] = self._amx_program()
        return reordered

    def internal_connections(self) -> List[Tuple[Any, str, Any, str]]:
        """Data wiring. ORDERING RULE (the FFT16 route-time face rule): for
        every source cell the LAST-listed connection's dst is either its
        ring successor (adjacent) or non-adjacent, so the route-time face
        always follows the ring."""
        conns: List[Tuple[Any, str, Any, str]] = [
            ("fin", "xf", "hstr", "xw"),
            ("hstr", "s", "r0", "v"),
            ("hstr", "s2", "r0", "v"),
        ]
        chain = self._chain_ids()
        for i in range(self.H):
            conns.append((f"r{i}", "res", f"sf{i}", "sample"))
            conns.append((f"u{i}", "res", f"umA{i}", "u"))
            conns.append((f"xc{i}", "res", f"umA{i}", "xw"))
            conns.append((f"z{i}", "res", f"sf{i}", "sample"))
        # chain forwards LAST per source (dict-next or non-adjacent)
        for k, cid in enumerate(chain[:-1]):
            conns.append((cid, "fwd", chain[k + 1], "v"))
        for i in range(self.H):
            # the activation-engine internal packets (shared-builder wiring)
            for p in ("patch", "frac", "addrq", "addr"):
                conns.append((f"sf{i}", p, f"sl{i}", p))
            conns.append((f"sl{i}", "out", f"umA{i}", "s"))
            conns.append((f"umA{i}", "zf", f"umB{i}", "zs"))
            conns.append((f"umA{i}", "nw", f"tf{i}", "sample"))
            for p in ("patch", "frac", "addrq", "addr"):
                conns.append((f"tf{i}", p, f"tl{i}", p))
            conns.append((f"tl{i}", "out", f"umB{i}", "n"))
            conns.append((f"umB{i}", "hu", "hcol", f"hp{i}"))
        for i in range(self.H):
            conns.append(("hcol", f"hb{i}", "hstr", f"h{i}"))
        for i in range(self.H):
            conns.append(("hcol", f"hs{i}", "hd0", "v"))
        for j in range(self.C):
            conns.append((f"hd{j}", "res", "amx", "w"))
            if j != self.C - 1:
                conns.append((f"hd{j}", "fwd", f"hd{j + 1}", "v"))
        # amx -> the off-ring output relay (a dual-FACE @1 abutment), then
        # the lock-clear config edge LAST (its dst is non-adjacent, so amx's
        # route-time face falls back to its positional next cell = the ring
        # transit — keeping the ring trace intact).
        conns.append(("amx", "out_f", "oout", "w"))
        conns.append(("amx", "unlock", "fin", "sample"))
        return conns

    def internal_jumps(self) -> List[Tuple[Any, str, Any, str]]:
        jumps: List[Tuple[Any, str, Any, str]] = [
            ("fin", "xf", "hstr", "xin"),
            ("fin", "goj", "hstr", "go"),
            ("hstr", "s", "r0", "default"),
            ("hstr", "s2", "r0", "default"),
        ]
        chain = self._chain_ids()
        for k, cid in enumerate(chain[:-1]):
            jumps.append((cid, "fwd", chain[k + 1], "default"))
        for i in range(self.H):
            jumps.append((f"r{i}", "resj", f"sf{i}", "default"))
            jumps.append((f"z{i}", "resj", f"sf{i}", "default"))
            jumps.append((f"u{i}", "resj", f"umA{i}", "cnt"))
            jumps.append((f"xc{i}", "resj", f"umA{i}", "cnt"))
            jumps.append((f"sf{i}", "trig", f"sl{i}", "default"))
            jumps.append((f"sl{i}", "trig", f"umA{i}", "sig"))
            jumps.append((f"umA{i}", "nw", f"tf{i}", "default"))
            jumps.append((f"tf{i}", "trig", f"tl{i}", "default"))
            jumps.append((f"tl{i}", "trig", f"umB{i}", "default"))
            jumps.append((f"umB{i}", "hj", "hcol", f"e{i}"))
        for i in range(self.H):
            jumps.append(("hcol", f"hs{i}", "hd0", "default"))
        for j in range(self.C - 1):
            jumps.append((f"hd{j}", "fwd", f"hd{j + 1}", "default"))
        for j in range(self.C):
            jumps.append((f"hd{j}", "resj", "amx", "default"))
        jumps.append(("amx", "out_f", "oout", "default"))
        return jumps

    def default_layout(self) -> Dict[Any, Tuple[int, int, str]]:
        """The 7x8 ring serpentine (FLL column-pair fold): head row west->
        east, three boustrophedon column pairs, the chain end closing back
        through two transit cells into ``fin``'s SOUTH face. Both dims <= 8
        (INV-9) and <= 7 wide (the AGCCC big-ring routability rule); the six
        in-bbox holes all sit on the open WEST edge."""
        ids = self._ring_ids() + ["transit_ring_a", "transit_ring_b"]
        pos: List[Tuple[int, int]] = [(c, 0) for c in range(7)]      # 0..6
        for pair in range(3):                                        # pairs
            cd = 6 - 2 * pair          # down column
            cu = cd - 1                # up column
            pos += [(cd, r) for r in range(1, 8)]
            pos += [(cu, r) for r in range(7, 0, -1)]
        # splice the off-ring output relay at its dict position (after
        # umB3 = ring slot 41) at (0, 2) — a west-edge hole, off the ring.
        pos.insert(42, (0, 2))
        assert len(pos) == 50
        pos.append((0, 1))                                           # t_b
        lay: Dict[Any, Tuple[int, int, str]] = {}
        for k, cid in enumerate(ids):
            x, y = pos[k]
            j = (k + 1) % len(pos)
            if cid == "umB3":
                j = k + 2          # ring successor skips the off-ring oout
            nx, ny = pos[j]
            if cid == "oout":
                face = "west"      # off-ring; the route override owns it
            elif (nx, ny) == (x, y + 1):
                face = "south"
            elif (nx, ny) == (x, y - 1):
                face = "north"
            elif (nx, ny) == (x + 1, y):
                face = "east"
            else:
                face = "west"
            lay[cid] = (x, y, face)
        return lay

    # ------------------------------------------------------------ reference
    def step_q15(self, x_q: List[int], h: List[int]
                 ) -> Tuple[List[int], List[int], int]:
        """One bit-exact timestep: ``(h_next, head_words, class)`` for one
        2-word Q15 feature vector and the current hidden state (signed
        ints). Mirrors the on-chip datapath operation for operation."""
        xin = [_s16(v) for v in x_q]
        stream = xin + [_s16(v) for v in h]
        r = [_s16(activation_ref_word(
            _mac_walk_ref(cq, bq, stream) & 0xFFFF,
            SIGMOID_TABLE_Q15, 0x8000, self._dshift_sig))
            for cq, bq in self._r_rows]
        z = [_s16(activation_ref_word(
            _mac_walk_ref(cq, bq, stream) & 0xFFFF,
            SIGMOID_TABLE_Q15, 0x8000, self._dshift_sig))
            for cq, bq in self._z_rows]
        n = []
        for i in range(self.H):
            u = _mac_walk_ref(*self._u_rows[i], stream)
            xw = _mac_walk_ref(*self._xc_rows[i], stream)
            word = _s16(_mulq(r[i], u) + xw)     # guard => no wrap
            n.append(_s16(activation_ref_word(
                word & 0xFFFF, TANH_TABLE_Q15, 0x0000, self._dshift_tanh)))
        hp = [_sat_add16(_mulq(0x7FFF - z[i], n[i]), _mulq(z[i], h[i]))
              for i in range(self.H)]
        head = [_mac_walk_ref(cq, bq, hp) for cq, bq in self._head_rows]
        cls_i, best = 0, head[0]
        for j in range(1, self.C):
            if head[j] > best:                    # first occurrence wins
                cls_i, best = j, head[j]
        return hp, head, cls_i

    def process_reference_q15(self, x_q15) -> List[int]:
        """Bit-exact predictor: consume pairs of Q15 feature words (uint16),
        emit ONE raw class word per pair (h persists across the call from a
        cold start h=0 — each call is a fresh stream)."""
        h = [0] * self.H
        out: List[int] = []
        words = [int(w) & 0xFFFF for w in x_q15]
        for t in range(len(words) // self.I):
            x2 = words[t * self.I:(t + 1) * self.I]
            h, _head, c = self.step_q15(x2, h)
            out.append(c & 0xFFFF)
        return out

    def h_trajectory_q15(self, x_q15) -> Tuple[List[List[int]],
                                               List[List[int]], List[int]]:
        """Per-timestep ``(h states, head words, classes)`` — the two-level
        verification gate's trajectory view."""
        h = [0] * self.H
        hs, heads, cls = [], [], []
        words = [int(w) & 0xFFFF for w in x_q15]
        for t in range(len(words) // self.I):
            h, head, c = self.step_q15(words[t * self.I:(t + 1) * self.I], h)
            hs.append(list(h))
            heads.append(list(head))
            cls.append(c)
        return hs, heads, cls

    def process_reference(self, input_samples) -> np.ndarray:
        """FLOAT reference: the float GRU + argmax over the file's float
        weights (decision-level context only; the bit-exact gate is
        :meth:`process_reference_q15`). Returns raw class indices as
        float32 — one per 2 input samples."""
        params = json.loads(self._weights_path.read_text())
        lp = params["layers"][0]
        Wx = np.asarray(lp["Wx"], dtype=np.float64)
        Wh = np.asarray(lp["Wh"], dtype=np.float64)
        b = np.asarray(lp["b"], dtype=np.float64)
        Wo = np.asarray(params["head"]["Wo"], dtype=np.float64)
        bo = np.asarray(params["head"]["bo"], dtype=np.float64)
        H = self.H
        arr = np.asarray(input_samples, dtype=np.float64).reshape(-1)
        h = np.zeros(H)
        out = []
        for t in range(len(arr) // self.I):
            x = arr[t * self.I:(t + 1) * self.I]
            px, ph = Wx @ x, Wh @ h
            r = 1.0 / (1.0 + np.exp(-(px[:H] + ph[:H] + b[:H])))
            z = 1.0 / (1.0 + np.exp(-(px[H:2 * H] + ph[H:2 * H]
                                      + b[H:2 * H])))
            n = np.tanh(px[2 * H:] + r * ph[2 * H:] + b[2 * H:])
            h = (1.0 - z) * n + z * h
            out.append(float(np.argmax(Wo @ h + bo)))
        return np.asarray(out, dtype=np.float32)

    def reset(self):
        """Reference-side reset (each ``process_reference*`` call already
        cold-starts its own h)."""
        pass

    # ---------------------------------------------------- weight manifest
    def weight_location_manifest(self) -> dict:
        """The machine-readable weight-location map (see the module
        docstring for the schema). Addresses are the CELL-LOCAL memory
        addresses of the stored prescaled Q15 words."""
        cells: Dict[str, Dict[int, dict]] = {}

        def row(cell: str, names: List[Optional[str]], cq: List[int],
                bias_name: Optional[str], bq: int) -> None:
            m: Dict[int, dict] = {}
            for j, (nm, q) in enumerate(zip(names, cq)):
                m[1 + j] = {"name": nm if nm else "pad",
                            "value": int(q) & 0xFFFF}
            m[len(cq) + 3] = {"name": bias_name if bias_name else "pad",
                              "value": int(bq) & 0xFFFF}
            cells[cell] = m

        H, I = self.H, self.I
        for i in range(H):
            cq, bq = self._r_rows[i]
            row(f"r{i}", [f"Wx.r[{i}][{j}]" for j in range(I)]
                + [f"Wh.r[{i}][{j}]" for j in range(H)], cq,
                f"b.r[{i}]", bq)
            cq, bq = self._z_rows[i]
            row(f"z{i}", [f"Wx.z[{i}][{j}]" for j in range(I)]
                + [f"Wh.z[{i}][{j}]" for j in range(H)], cq,
                f"b.z[{i}]", bq)
            cq, bq = self._u_rows[i]
            row(f"u{i}", [None] * I
                + [f"Wh.n[{i}][{j}]" for j in range(H)], cq, None, bq)
            cq, bq = self._xc_rows[i]
            row(f"xc{i}", [f"Wx.n[{i}][{j}]" for j in range(I)]
                + [None] * H, cq, f"b.n[{i}]", bq)
        for j in range(self.C):
            cq, bq = self._head_rows[j]
            row(f"hd{j}", [f"head.Wo[{j}][{k}]" for k in range(H)], cq,
                f"head.bo[{j}]", bq)
        return {
            "format": "gru-cell-weight-map-v1",
            "weights_file": str(self._weights_path),
            "scales": self.scale_shifts,
            "cells": cells,
        }
