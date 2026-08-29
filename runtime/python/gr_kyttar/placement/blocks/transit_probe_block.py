# SPDX-License-Identifier: GPL-3.0-or-later
"""TransitProbeBlock — the MEASUREMENT FIXTURE for the wide-value transit
ceiling.

This is a verification instrument, not a DSP block: it exists so that a claim
about what a cell can carry is settled by RUNNING something on the real chip
rather than by algebra over a formula. It backs
``verification/tests/test_wide_transit_ceiling.py``, which refutes INV-47's
"a live set wider than 10 words cannot transit a cell at all".

Three shapes, selected by ``mode``:

* ``hold``   — the relay INV-45 prices at ``3W + 1``: every word is held in its
  own register and forwarded with ``MOVE R0, Rw`` + ``WRITE``. Genuinely
  bounded (it overruns at W = 10).
* ``stream`` — one word in, the same word straight out, holding NOTHING. Costs
  a constant 3 instructions at ANY frame width; this is the shape the ceiling
  does not price, and it carries 128-word frames exactly.
* ``loop``   — a two-cell recirculation: a backward ``JUMP`` re-enters the head
  mid-program with the counter intact, so ONE datapath serves N sequential
  passes. Measured exact at 1/2/4/8/10/20/80 passes.
"""
from typing import Any, Dict, List, Tuple

import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock


class TransitProbeBlock(KyttarBlock):
    """Collect W words, relay them through `stages` cells, burst them out.

    Params:
        width  — the live-set size W (words held + forwarded per hop)
        stages — how many pure relay cells the frame transits
    """

    CATEGORY = "fec"
    TAGS = ["probe"]

    _interface = BlockInterface(
        entry_address=16, input_registers=[5], output_registers=[0])

    GRC_UNSUPPORTED_PARAMS = ()

    def __init__(self, name: str, width: int = 8, stages: int = 1,
                 mode: str = "hold"):
        super().__init__(name)
        self.width = int(width)
        self.stages = int(stages)
        self.mode = str(mode)          # "hold" | "stream"

    @property
    def _slots(self) -> Tuple[str, ...]:
        return tuple(f"w{i}" for i in range(self.width))

    @property
    def cell_count(self) -> int:
        if self.mode == "loop":
            return 3                      # head + tail + egress
        if self.mode == "stream":
            return self.stages + 1        # relays + emit
        return 1 + self.stages + 1        # collector + relays + emit

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def output_cell_id(self):
        return "emit"

    # --------------------------------------------------------------- cells
    def _collect(self) -> CellProgram:
        """Shift W words in; on the Wth, publish all W and fire the chain."""
        slots = self._slots
        W = self.width
        body = ""
        # shift register: sW-1 <- sW-2 <- ... <- s0 <- x
        for i in range(W - 1, 0, -1):
            body += f"    MOVE R{{state:s{i}}}, R{{state:s{i-1}}}\n"
        body += "    MOVE R{state:s0}, R{in:x}\n"
        body += "    SUB R{state:n}, R{data:one}\n"
        body += "    MOVE R{state:n}, R0\n"
        body += "    BR.NZ done\n"
        body += f"    MOVE R{{state:n}}, R{{data:cnt}}\n"
        # publish oldest-first: slot k gets s[W-1-k]
        for k, sl in enumerate(slots):
            body += (f"    MOVE R0, R{{state:s{W-1-k}}}\n"
                     f"    {{write:h_{sl}}}\n")
        body += "    {jump:trig}\ndone:\n"
        return CellProgram(
            inputs=[Port("x", register=1)],
            outputs=[Port(f"h_{s}") for s in slots] + [Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("one", 1, address=2),
                  DataWord("cnt", W, address=3)],
            state=([StateVar(f"s{i}", register=4 + i) for i in range(W)]
                   + [StateVar("n", register=4 + W, initial_value=W,
                               reset_per_batch=True, reset_value=W)]),
            assembly_template="default:\n" + body,
        )

    def _relay(self, idx: int) -> CellProgram:
        """A PURE relay: hold W words, forward all W, trigger. This is the
        exact shape INV-45 prices at 3W + 1."""
        slots = self._slots
        body = ""
        for s in slots:
            body += f"    MOVE R0, R{{in:{s}}}\n    {{write:o_{s}}}\n"
        body += "    {jump:trig}\n"
        return CellProgram(
            inputs=[Port(s, register=1 + i) for i, s in enumerate(slots)],
            outputs=[Port(f"o_{s}") for s in slots] + [Port("trig")],
            entries=[EntryPoint("default")],
            data=[], state=[],
            assembly_template="default:\n" + body,
        )

    def _emit(self) -> CellProgram:
        slots = self._slots
        body = "    {write:out}\n    {jump:out}\n"     # slot 0 via R0
        for s in slots[1:]:
            body += ("    MOVE R0, R{in:%s}\n" % s) + \
                    "    {write:out}\n    {jump:out}\n"
        return CellProgram(
            inputs=([Port(slots[0], register=0)]
                    + [Port(s, register=1 + i)
                       for i, s in enumerate(slots[1:])]),
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[], state=[],
            assembly_template="default:\n" + body,
        )

    def _stream_relay(self) -> CellProgram:
        """A STREAMING relay: one word in, same word straight out, holding
        NOTHING. Cost is CONSTANT in the frame width W — this is the
        construction INV-47's `3W + 1` ceiling does not price."""
        return CellProgram(
            inputs=[Port("w", register=1)],
            outputs=[Port("o"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[], state=[],
            assembly_template=("default:\n"
                               "    MOVE R0, R{in:w}\n"
                               "    {write:o}\n"
                               "    {jump:trig}\n"),
        )

    def _stream_emit(self) -> CellProgram:
        return CellProgram(
            inputs=[Port("w", register=1)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[], state=[],
            assembly_template=("default:\n"
                               "    MOVE R0, R{in:w}\n"
                               "    {write:out}\n"
                               "    {jump:out}\n"),
        )

    # ------------------------------------------------- recirculation probe
    def _loop_head(self) -> CellProgram:
        """Loop head: accept a word from OUTSIDE or from the loop TAIL, add 1,
        and either send it round again or emit it.

        `stages` is reused as the number of PASSES the word must make. This is
        the shape a reusable ChaCha20 round engine needs: one datapath, N
        sequential invocations, a counter deciding when to stop.
        """
        return CellProgram(
            # `back` and `acc` are the SAME register: the tail delivers the
            # recirculated word straight into the accumulator slot the loop
            # body reads, so re-entry needs no copy.
            inputs=[Port("w", register=1), Port("back", register=5)],
            outputs=[Port("fwd"), Port("out"), Port("trig"), Port("done")],
            entries=[EntryPoint("default"), EntryPoint("body")],
            data=[DataWord("one", 1, address=3),
                  DataWord("npass", self.stages, address=4),
                  # Dynamic FACE switching (guide §3): the head sends the
                  # RECIRCULATED word EAST to the tail and the FINISHED word
                  # SOUTH to egress, from one invocation.
                  DataWord("f_east", 1, address=7, is_face=True),
                  DataWord("f_south", 0, address=8, is_face=True)],
            state=[StateVar("acc", register=5),
                   StateVar("n", register=6, initial_value=0,
                            reset_per_batch=True, reset_value=0)],
            assembly_template="""\
default:
    MOVE R{state:acc}, R{in:w}
    MOVE R{state:n}, R{data:npass}
body:
    ADD R{state:acc}, R{data:one}
    MOVE R{state:acc}, R0
    SUB R{state:n}, R{data:one}
    MOVE R{state:n}, R0
    BR.Z finish
    MOVE [FACE], R{data:f_east}
    MOVE R0, R{state:acc}
    {write:fwd}
    {jump:trig}
    HALT
finish:
    MOVE [FACE], R{data:f_south}
    MOVE R0, R{state:acc}
    {write:out}
    {jump:out}
""",
        )

    def _loop_tail(self) -> CellProgram:
        """Loop tail: bounce the word straight back to the head's `again`."""
        return CellProgram(
            inputs=[Port("w", register=1)],
            outputs=[Port("back"), Port("kick")],
            entries=[EntryPoint("default")],
            data=[], state=[],
            assembly_template=("default:\n"
                               "    MOVE R0, R{in:w}\n"
                               "    {write:back}\n"
                               "    {jump:kick}\n"),
        )

    def _loop_egress(self) -> CellProgram:
        """Egress for `loop` mode.

        The head cannot send its finished word out along the loop's own axis:
        the tail's face points back at the head, so an outbound word would be
        consumed there (the used-cell transit rule, INV-32). So the head
        switches FACE and drops the result into this cell, off the loop axis.
        """
        return CellProgram(
            inputs=[Port("v", register=1)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[], state=[],
            assembly_template=("default:\n"
                               "    MOVE R0, R{in:v}\n"
                               "    {write:out}\n"
                               "    {jump:out}\n"),
        )

    def build_cell_programs(self) -> Dict[Any, CellProgram]:
        if self.mode == "loop":
            return {"head": self._loop_head(), "tail": self._loop_tail(),
                    "egress": self._loop_egress()}
        if self.mode == "stream":
            progs: Dict[Any, CellProgram] = {}
            for i in range(self.stages):
                progs[f"r{i}"] = self._stream_relay()
            progs["emit"] = self._stream_emit()
            return progs
        progs = {"collect": self._collect()}
        for i in range(self.stages):
            progs[f"r{i}"] = self._relay(i)
        progs["emit"] = self._emit()
        return progs

    @property
    def _chain(self) -> Tuple[str, ...]:
        return tuple(f"r{i}" for i in range(self.stages))

    def internal_connections(self) -> List[Tuple[Any, str, Any, str]]:
        if self.mode == "loop":
            return [("head", "fwd", "tail", "w"),
                    ("tail", "back", "head", "back"),
                    ("head", "out", "egress", "v")]
        if self.mode == "stream":
            chain = self._chain
            conns: List[Tuple[Any, str, Any, str]] = []
            for a, b in zip(chain, chain[1:]):
                conns.append((a, "o", b, "w"))
            if chain:
                conns.append((chain[-1], "o", "emit", "w"))
            return conns
        slots = self._slots
        chain = self._chain
        conns: List[Tuple[Any, str, Any, str]] = []
        first = chain[0] if chain else "emit"
        conns += [("collect", f"h_{s}", first, s) for s in slots]
        for a, b in zip(chain, chain[1:]):
            conns += [(a, f"o_{s}", b, s) for s in slots]
        if chain:
            conns += [(chain[-1], f"o_{s}", "emit", s) for s in slots]
        return conns

    def internal_jumps(self) -> List[Tuple[Any, str, Any, str]]:
        if self.mode == "loop":
            return [("head", "trig", "tail", "default"),
                    ("tail", "kick", "head", "again")]
        if self.mode == "stream":
            chain = self._chain
            js: List[Tuple[Any, str, Any, str]] = []
            for a, b in zip(chain, chain[1:]):
                js.append((a, "trig", b, "default"))
            if chain:
                js.append((chain[-1], "trig", "emit", "default"))
            return js
        chain = self._chain
        jumps: List[Tuple[Any, str, Any, str]] = []
        first = chain[0] if chain else "emit"
        jumps.append(("collect", "trig", first, "default"))
        for a, b in zip(chain, chain[1:]):
            jumps.append((a, "trig", b, "default"))
        if chain:
            jumps.append((chain[-1], "trig", "emit", "default"))
        return jumps

    def default_layout(self) -> Dict[Any, Tuple[int, int, str]]:
        if self.mode == "loop":
            return {"head": (0, 0, "east"), "tail": (1, 0, "west"),
                    "egress": (0, 1, "east")}
        if self.mode == "stream":
            ids = list(self._chain)
            lay: Dict[Any, Tuple[int, int, str]] = {}
            for i, cid in enumerate(ids):
                lay[cid] = (i, 0, "east" if i < len(ids) - 1 else "south")
            lay["emit"] = (len(ids) - 1, 1, "west")
            return lay
        ids = ["collect"] + list(self._chain)
        lay: Dict[Any, Tuple[int, int, str]] = {}
        # single row east, emit drops south under column 0
        for i, cid in enumerate(ids):
            lay[cid] = (i, 0, "east" if i < len(ids) - 1 else "south")
        lay["emit"] = (len(ids) - 1, 1, "west")
        return lay

    def process_reference(self, input_words) -> np.ndarray:
        w = [int(v) & 0xFFFF for v in np.asarray(input_words).ravel()]
        W = self.width
        out: List[int] = []
        for f in range(len(w) // W):
            out += w[f * W:(f + 1) * W]
        return np.array(out, dtype=np.uint16)

    def reset(self):
        pass
