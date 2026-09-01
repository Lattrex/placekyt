# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate ``foc_motor.grc`` — the FULL FOC loop as a GNU Radio flowgraph.

The flowgraph is large and highly regular (five on-chip blocks, four ingress
arms each with its own source, the host-side loop block, three scopes), so it
is GENERATED rather than hand-maintained: every kyttar source gets the same
server port, the same pacing parameters and a distinct stream id by
construction, which is exactly the class of thing hand-editing YAML gets
wrong.

Run::

    .venv/bin/python examples/foc_motor/build_grc.py

then validate with ``examples/validate_grc.py`` and codegen with ``grcc``.
"""
from __future__ import annotations

from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
OUT = HERE / "foc_motor.grc"

PORT = 58950            # placeKYT's default host port. A 0 here makes every
                        # kyttar_source silently no-op — the single most common
                        # "GRC does nothing" cause in this repo.
BURST = 600             # control iterations per burst (the settle horizon)


def st(x, y, enabled=True):
    return {"bus_sink": False, "bus_source": False, "bus_structure": None,
            "coordinate": [x, y], "rotation": 0,
            "state": "enabled" if enabled else "disabled"}


def variable(name, value, comment, x, y):
    return {"name": name, "id": "variable",
            "parameters": {"comment": comment, "value": str(value)},
            "states": st(x, y)}


def ksource(name, stream_id, comment, x, y, burst=BURST):
    """A kyttar_source — ONE ingress arm.

    Each independent arm needs its OWN source with a DISTINCT stream_id: the
    placeKYT importer copies the stream id onto the x16_in -> block net so the
    server resolves each burst to that block's own entry/hop/data registers and
    demuxes the returned words by tag. Without distinct ids the arms are
    indistinguishable on the shared input port."""
    return {"name": name, "id": "kyttar_source", "parameters": {
        "affinity": "", "alias": "", "comment": comment,
        "maxoutbuf": "0", "minoutbuf": "0",
        "burst_len": str(burst), "complex_in": "float",
        "device_id": '"kyttar_0"', "max_events_per": "2000000",
        "num_channels": "1", "output_words": '"q15"', "pipelined": "no",
        "port_name": '"x16_in"', "repeat": "no", "schedule": '"interleaved"',
        "server_host": '"127.0.0.1"', "server_port": str(PORT),
        "stream_id": f'"{stream_id}"',
    }, "states": st(x, y)}


def ksink(name, stream_id, comment, x, y, in_type="float"):
    return {"name": name, "id": "kyttar_sink", "parameters": {
        "affinity": "", "alias": "", "comment": comment,
        "maxoutbuf": "0", "minoutbuf": "0",
        "device_id": '"kyttar_0"', "hold_secs": "8.0", "in_type": in_type,
        "num_channels": "1", "port_name": '"x16_out"',
        "server_port": str(PORT), "server_repeat": "True",
        "stream_id": f'"{stream_id}"',
    }, "states": st(x, y)}


def relay(name, comment, x, y):
    """A StreamSplitterBlock relay — the per-arm ingress relay.

    LOAD-BEARING, not decorative (INV-71). A net fanned straight out of the
    chip input port into a face-locking block's arm lands on the PORT CELL, so
    every arm arrives on ONE face, which the rendezvous LOCK bars: the chain
    then routes, builds, and emits nothing while every run reports QueueEmpty.
    Each arm gets its own relay so the three land on three DISTINCT HOPS."""
    return {"name": name, "id": "kyttar_splitter", "parameters": {
        "affinity": "", "alias": "", "comment": comment,
        "maxoutbuf": "0", "minoutbuf": "0", "device_id": '"kyttar_0"',
    }, "states": st(x, y)}


def time_sink(name, title, nconn, size, ymin, ymax, ylabel, comment, x, y,
              labels=None):
    """A QT time sink.

    BLANK-SCOPE DISPLAY CONTRACT: the buffer must be a FULL burst (a short
    buffer paints nothing when the batch arrives all at once), and kyttar.sink
    emits q15/32768 FLOATS, so the y range is in Q15 units, not raw words."""
    p = {"affinity": "", "alias": "", "autoscale": "False", "axislabels": "True",
         "comment": comment, "ctrlpanel": "False", "entags": "True",
         "grid": "True", "gui_hint": "", "legend": "True",
         "name": f'"{title}"', "nconnections": str(nconn), "size": str(size),
         "srate": "samp_rate", "stemplot": "False",
         "tr_chan": "0", "tr_delay": "0", "tr_level": "0.0",
         "tr_mode": "qtgui.TRIG_MODE_FREE", "tr_slope": "qtgui.TRIG_SLOPE_POS",
         "tr_tag": '""', "type": "float", "update_time": "0.10",
         "ylabel": ylabel, "ymax": str(ymax), "ymin": str(ymin), "yunit": '""'}
    colors = ["blue", "red", "green", "black", "cyan", "magenta", "yellow",
              "dark red", "dark green", "dark blue"]
    labels = labels or []
    for i in range(1, 11):
        p[f"alpha{i}"] = "1.0"
        p[f"color{i}"] = colors[i - 1]
        p[f"label{i}"] = labels[i - 1] if i <= len(labels) else f"Signal {i}"
        p[f"marker{i}"] = "-1"
        p[f"style{i}"] = "1"
        p[f"width{i}"] = "1"
    return {"name": name, "id": "qtgui_time_sink_x", "parameters": p,
            "states": st(x, y)}


# --------------------------------------------------------------------------- #
#  The two embedded python blocks: the MOTOR and the ERROR FORMER              #
# --------------------------------------------------------------------------- #

HOST_LOOP_SRC = '''"""THE HOST SIDE OF THE LOOP — plant, error former and the FEEDBACK PATH,
in ONE stateful block.

WHY ONE BLOCK. GNU Radio's stream scheduler forbids cycles outright, and a
control loop IS a cycle: the duties drive the motor, the motor's sensed
currents drive the next period's duties. Wiring that as streams — a plant
block here, an error former there, the array between them — asks the
scheduler for a ring, and it refuses at start() with "flow graph has loops!".
No buffer sizing fixes it; there is no priming that makes a stream ring legal.

So the ring is closed INSIDE this block instead. It holds every piece of loop
state that has to persist from one control period to the next — the motor's
two stationary-frame currents and its rotor angle, and the two PI
controllers' 32-bit integrators — and advances the WHOLE period in one step:

    sensed (ia, ib, theta)            [this block's motor state]
      -> Clarke -> forward Park       [the measurement half's models]
      -> e = ref - measured           [the error former]
      -> PI(d), PI(q) -> inverse Park -> SVPWM     [the command half's models]
      -> three duties -> the motor    [the plant update]

Because the feedback is an internal assignment rather than a stream, this
block has NO stream inputs at all. Everything it emits is an OUTPUT, so the
flowgraph the scheduler sees is a tree: this block at the root, the two
on-chip halves hanging off it as feed-forward branches. That is the whole
trick, and it is what GNU Radio users conventionally do with a control loop.

WHAT THE ARRAY THEN DOES. The chip branches are not decoration and they are
not replay of a canned recording: every word this block emits is the live
value of that wire in the loop it is running right now, computed this period
from the previous period's duties. The array recomputes the measurement half
from the sensed currents and the command half from the errors, and the scopes
show what came BACK off the array. The loop is genuinely closed; the array
sits across it rather than inside the scheduler's ring.

THE MOTOR MODEL. Surface PMSM (Ld == Lq) in the stationary two-phase frame,
forward Euler at the control period dt:

    L d(i_alpha)/dt = v_alpha - R*i_alpha - e_alpha
    L d(i_beta )/dt = v_beta  - R*i_beta  - e_beta

with a sinusoidal machine's back-EMF

    e_alpha = -ke*omega_e*sin(theta_e)
    e_beta  = +ke*omega_e*cos(theta_e)

and theta_e advancing at a constant electrical speed omega_e.

WHAT IS HELD CONSTANT, and why that is honest: the mechanical dynamics.
omega_e does not respond to the torque the loop produces. A current loop
closes two to three orders of magnitude faster than any real machine's
mechanical pole, so constant speed over a current-loop settle is the standard
modelling assumption. This is a CURRENT-loop model, not a drivetrain.

The inverter is ideal: duty cycles are taken as applied phase voltages scaled
by v_dc. No dead time, no device drop, no switching ripple — those change a
real drive's current ripple, not whether the regulator regulates.

THE ERROR FORMER is on the host BY CONSTRUCTION, not by omission: the
reference is a supervisory quantity (a torque command from an outer speed
loop, or an operator setpoint), it changes on a far slower timescale than the
current loop, and putting a two-input subtract on the array would spend a
rendezvous — the scarcest resource in this design — on an operation the host
does for free between bursts. i_d_ref is ZERO for a surface PMSM: the magnets
already supply the rotor flux, so any d-axis current is pure loss.

THE INTEGRATOR TRAP, stated because it is silent when you hit it: the PI
block's batch reference model keeps its 32-bit accumulator LOCAL to the call,
so stepping it one sample at a time deletes the integral action entirely —
the loop then runs proportional-only, settles short of its reference, and
changing ki changes nothing at all. StatefulPI carries the accumulator across
calls, and that is what this block steps.

WIRE FORMAT, unchanged on every port: currents are Q15 fractions of i_base,
the angle is the 16-bit half-turn convention as a Q15 float in [-1, 1)
(value = theta/pi), duties are Q15. That is exactly what the kyttar sources
inject.
"""
import os
import sys

import numpy as np
from gnuradio import gr

# The loop's arithmetic is the SHIPPED models — the same integer models the
# gates and the two-array harness use — rather than a second transcription
# that could drift from them.
# GRC EVALUATES THIS SOURCE AT EDIT TIME, in a namespace with no __file__, so
# the import path is discovered by SEARCH rather than relative to this file: an
# unguarded __file__ here makes GRC fail to interpret the source, the block
# then declares no ports, and every connection to it is reported unmakeable.
def _add_model_paths():
    seen = set(sys.path)
    roots = []
    for base in [os.getcwd()] + [p for p in sys.path if p]:
        try:
            base = os.path.abspath(base)
        except Exception:
            continue
        d = base
        for _ in range(6):
            if d in roots:
                break
            roots.append(d)
            nxt = os.path.dirname(d)
            if nxt == d:
                break
            d = nxt
    for r in roots:
        cand = [os.path.join(r, 'examples', 'foc_motor'),
                os.path.join(r, 'runtime', 'python'),
                os.path.join(r, 'verification', 'tests')]
        if not os.path.isfile(os.path.join(cand[0], 'foc_loop_model.py')):
            continue
        for c in cand:
            if c not in seen and os.path.isdir(c):
                sys.path.insert(0, c)
                seen.add(c)
        return True
    return False


_add_model_paths()

from foc_loop_model import (PMSMPlant, MotorParams, StatefulPI, DEFAULT_PI,
                            foc_loop_golden, q15, from_q15)


class blk(gr.sync_block):
    """The host half of the loop: motor + error former + feedback, no inputs.

    Eleven outputs, all of them a live wire of the running loop:

        0 ia          1 ib          2 theta      -> the measurement half
        3 e_d         4 e_q         5 theta      -> the command half
        6 i_d         7 i_q                      -> the host's own monitors
        8 duty_a      9 duty_b     10 duty_c     -> what drove the motor

    theta is emitted TWICE because it feeds BOTH rotations, and on-chip
    fan-out to two rendezvous arms is the hard part — so it is delivered as
    two independent ingress arms, one per consumer.
    """

    def __init__(self, r_s=0.35, l_s=1.5e-3, ke=0.035, v_dc=24.0,
                 i_base=10.0, omega_e=200.0, dt=35e-6,
                 kp=0.25, ki=0.01, limit=1.0,
                 i_d_ref=0.0, i_q_ref=0.30):
        gr.sync_block.__init__(
            self, name='FOC host loop (motor + error + feedback)',
            in_sig=[],
            out_sig=[np.float32] * 11)
        self.params = MotorParams(r_s=float(r_s), l_s=float(l_s),
                                  ke=float(ke), v_dc=float(v_dc),
                                  i_base=float(i_base))
        self.plant = PMSMPlant(params=self.params, dt=float(dt),
                               omega_e=float(omega_e))
        pi = dict(DEFAULT_PI)
        pi.update(kp=float(kp), ki=float(ki), limit=float(limit))
        self.pi_d = StatefulPI('d', **pi)
        self.pi_q = StatefulPI('q', **pi)
        self.i_d_ref = float(i_d_ref)
        self.i_q_ref = float(i_q_ref)
        # The sensed state the FIRST control period is computed from: the
        # motor at rest. Every later period reads what the previous period's
        # duties left behind — that is the closed loop.
        self._sensed = self.plant.sensed_words()

    def work(self, input_items, output_items):
        n = len(output_items[0])
        ref_d = q15(self.i_d_ref)
        ref_q = q15(self.i_q_ref)
        for k in range(n):
            ia_w, ib_w, th_w = self._sensed

            # ONE whole control period, every on-chip stage computed by that
            # block's own pinned integer model, with the LIVE integrators.
            da, db, dc, i_d, i_q = foc_loop_golden(
                ia_w, ib_w, th_w, ref_d, ref_q, self.pi_d, self.pi_q)

            e_d = from_q15(ref_d) - from_q15(i_d)
            e_q = from_q15(ref_q) - from_q15(i_q)
            lim = 32767.0 / 32768.0
            e_d = min(max(e_d, -1.0), lim)
            e_q = min(max(e_q, -1.0), lim)

            # the measurement half's three ingress arms
            output_items[0][k] = from_q15(ia_w)
            output_items[1][k] = from_q15(ib_w)
            output_items[2][k] = from_q15(th_w)
            # the command half's three ingress arms
            output_items[3][k] = e_d
            output_items[4][k] = e_q
            output_items[5][k] = from_q15(th_w)
            # the host's own view of the loop
            output_items[6][k] = from_q15(i_d)
            output_items[7][k] = from_q15(i_q)
            output_items[8][k] = from_q15(da)
            output_items[9][k] = from_q15(db)
            output_items[10][k] = from_q15(dc)

            # CLOSE THE LOOP: this period's duties drive the motor, and what
            # it then senses is what the NEXT period is computed from. This
            # assignment is the feedback path — the thing that would be a
            # stream cycle if it were wired instead of held.
            self._sensed = self.plant.step(da, db, dc)
        return n
'''


def epy(name, src, comment, x, y, **ctor):
    """An embedded python block.

    ``ctor`` becomes GRC PARAMETERS. This is not optional decoration: GRC
    generates the constructor call from the declared parameter list, so a
    ``__init__`` keyword with no matching GRC parameter is emitted as a bare
    ``kw=`` and the generated Python does not parse."""
    params = {"_source_code": src, "affinity": "", "alias": "",
              "comment": comment, "maxoutbuf": "0", "minoutbuf": "0"}
    params.update({k: str(v) for k, v in ctor.items()})
    return {"name": name, "id": "epy_block", "parameters": params,
            "states": st(x, y)}


DESCRIPTION = (
    "THE FULL FOC CURRENT LOOP as a logical flowgraph: the measurement half "
    "(Clarke + forward Park), the host-side error former, and the command "
    "half (two PI controllers + inverse Park + SVPWM), closed around an "
    "an embedded PMSM plant so the loop actually runs. "
    "THE LOOP CLOSES INSIDE ONE HOST BLOCK. GNU Radio's stream scheduler "
    "forbids cycles outright -- a stream ring is refused at start() with "
    "'flow graph has loops!', and no buffer sizing fixes it -- so the motor, "
    "the error former and the feedback path all live in a single stateful "
    "block, foc_host, which therefore has NO stream inputs. The graph the "
    "scheduler sees is a TREE: foc_host at the root, the measurement half and "
    "the command half hanging off it as feed-forward branches. Every word "
    "foc_host emits is the LIVE value of that wire in the loop it is running "
    "now, computed this period from the previous period's duties -- this is a "
    "genuinely closed loop, not a replay of a canned recording. "
    "PLACEMENT IS THE READER'S JOB AND THIS FILE DOES NOT PRESUME IT. The "
    "whole loop does NOT route on one 10x12 array: the limit is corridor/arm "
    "budget, not cells (the full chain is 55 block cells of 120, yet the best "
    "of ~2600 whole-chain placements still left 2 of 13 nets unrouted, and "
    "the unrouted nets were always rendezvous arms). The NATURAL SPLIT is the "
    "measurement half on one die and the command half on another; the two "
    "halves are joined only by (i_d, i_q) out and (e_d, e_q) back, plus "
    "theta, so the crossing is narrow. A chip crossing costs about 40 ns, "
    "which is negligible against the ~35 us loop period. "
    "RATE: the command half alone measures 55.8 kHz sustained on one array. "
    "The two halves are strictly SERIAL within a sample -- sample k's duties "
    "cannot be computed until sample k's currents have been measured and "
    "rotated -- so the whole loop is expected near 28 kHz, which is the "
    "number this flowgraph exists to let you verify. "
    "The chain RE-ARMS rather than pipelines: each rendezvous bars its arms "
    "until the current group has cleared, so one iteration is in flight at a "
    "time and the period equals whole-chain depth. "
    "EVERY ingress arm has its OWN kyttar_source with a DISTINCT stream id, "
    "and its OWN relay block: a net fanned straight off the input port lands "
    "every word on the port cell, hence on ONE face, which the rendezvous "
    "LOCK bars -- the chain then builds, routes and emits nothing. THETA fans "
    "out to BOTH rotations and therefore has TWO sources and TWO relays, one "
    "per consumer. "
    "SVPWM emits THREE words per sample (duty a, b, c) on one stream, so its "
    "sink is set to q15 output words and split with a Deinterleave at 3. "
    "SINK PAIRING: each kyttar_sink names the stream id of the source that "
    "HEADS its chain -- sink_idq takes 'ia' and sink_duty takes 'e_d' -- "
    "following the shipped multi-stream convention. Note that each chain here "
    "is fed by THREE arms rather than one, so if you re-tag the arms when you "
    "place this, keep each sink's id equal to its chain's head source."
)



# --------------------------------------------------------------------------- #
#  Auto-layout                                                                 #
# --------------------------------------------------------------------------- #
# The coordinates authored inline above are LOGICAL (which lane, which row).
# GRC renders a block as a title bar plus one line per VISIBLE parameter, so a
# kyttar_source (13 visible params) is ~230 px tall while a splitter is ~45 px.
# Hand-authored coordinates on a uniform pitch therefore overlap badly: the
# first cut of this flowgraph packed 120 px rows against 230 px blocks and the
# result was unreadable.
#
# So the layout is COMPUTED. Blocks are assigned to a lane (a pipeline stage,
# left to right) and ordered within it; this pass measures each block's
# rendered height and packs each lane top-down with a real gutter, then centres
# every lane vertically so the signal flow reads straight across.

_ROW_PX = 15            # GRC's per-parameter line height
_TITLE_PX = 30          # the title bar
_VGUTTER = 70           # vertical clearance between blocks in a lane
_LANE_X = 260           # horizontal pitch between lanes

# Parameters GRC does not render in the block body.
_UNRENDERED = {"affinity", "alias", "comment", "maxoutbuf", "minoutbuf"}

# A qtgui time sink declares ~83 params but renders only a handful (the rest
# are per-trace styling, hidden unless enabled). Measured against the shipped
# examples: it draws about the height of a 6-param block.
_HEIGHT_OVERRIDE = {"qtgui_time_sink_x": _TITLE_PX + 6 * _ROW_PX}


def _height(b):
    over = _HEIGHT_OVERRIDE.get(b["id"])
    if over is not None:
        return over
    vis = [k for k, v in b.get("parameters", {}).items()
           if k not in _UNRENDERED and str(v) != ""]
    return _TITLE_PX + max(1, len(vis)) * _ROW_PX


def _apply_layout(blocks, lanes):
    """Place ``blocks`` by ``lanes`` — a list of lists of block NAMES, one per
    column, in top-to-bottom order within the column.

    Every block named in ``lanes`` is repositioned; anything not named keeps
    the coordinate it was authored with (the Options block, the variables).
    """
    by_name = {b["name"]: b for b in blocks}
    missing = [n for lane in lanes for n in lane if n not in by_name]
    if missing:
        raise KeyError(f"lane names not in the flowgraph: {missing}")

    # Pack each lane top-down, then record its total extent so it can be centred.
    packed, extents = [], []
    for lane in lanes:
        y, col = 0, []
        for name in lane:
            col.append((name, y))
            y += _height(by_name[name]) + _VGUTTER
        packed.append(col)
        extents.append(y - _VGUTTER if col else 0)

    tallest = max(extents) if extents else 0
    for i, col in enumerate(packed):
        x = 8 + i * _LANE_X
        offset = (tallest - extents[i]) // 2      # centre the lane vertically
        for name, y in col:
            by_name[name]["states"]["coordinate"] = [x, _TOP + offset + y]


# The variables column occupies the top-left, so the graph starts below it.
_TOP = 380

# The pipeline, left to right. Row order within a lane follows the signal:
# the measurement half on top, the command half beneath it.
_LANES = [
    ["foc_host"],
    ["src_ia", "src_ib", "src_th_park", "src_ed", "src_eq", "src_th_ipark"],
    ["relay_ia", "relay_ib", "relay_th_park",
     "relay_ed", "relay_eq", "relay_th_ipark"],
    ["clarke", "pi_d", "pi_q"],
    ["ab_split"],
    ["park", "ipark"],
    ["sink_idq", "v_split"],
    ["idq_split", "svpwm"],
    ["sink_duty"],
    ["duty_split"],
    ["scope_idq", "scope_err", "scope_duty"],
]


def build():
    blocks = []
    conns = []

    def C(a, ap, b, bp):
        conns.append([a, str(ap), b, str(bp)])

    # ----- variables ------------------------------------------------------- #
    blocks += [
        variable("samp_rate", 28600,
                 "the CONTROL-LOOP rate the full loop targets: 28.6 kHz, i.e. "
                 "a 35 us period. The command half alone measures 55.8 kHz; "
                 "the two halves are serial within a sample.", 8, 100),
        variable("burst_len", BURST,
                 "control iterations per burst. Sized to the settle horizon: "
                 "the closed loop reaches 2% of the q-axis reference by about "
                 "step 432 with the shipped conservative ki.", 8, 172),
        variable("i_q_ref", 0.30,
                 "the TORQUE command, as a Q15 fraction of full-scale current "
                 "(0.30 = 3 A at the 10 A i_base). This is what an outer speed "
                 "loop would drive.", 8, 244),
        variable("i_d_ref", 0.0,
                 "the d-axis (flux) reference. ZERO for a surface PMSM: the "
                 "magnets already supply the rotor flux, so d-axis current is "
                 "pure loss. Field weakening would drive it negative.",
                 8, 316),
    ]

    # ----- the HOST SIDE of the loop, in ONE block ------------------------- #
    # THE CYCLE BREAK. The motor, the error former and the feedback path all
    # live inside this block, so the loop closes as an internal assignment
    # rather than a stream. It therefore has NO stream inputs, and the graph
    # the GNU Radio scheduler sees is a TREE — this block at the root, the two
    # on-chip halves hanging off it — instead of the ring it refuses to start.
    blocks.append(epy("foc_host", HOST_LOOP_SRC,
                      "THE HOST SIDE — the motor, the error former and the "
                      "feedback path in one stateful block. Nothing here is "
                      "on the chip and nothing here ever will be. It has no "
                      "inputs on purpose: the loop closes INSIDE it, which is "
                      "what keeps the flowgraph acyclic and startable.",
                      200, 620,
                      r_s="0.35", l_s="1.5e-3", ke="0.035", v_dc="24.0",
                      i_base="10.0", omega_e="200.0", dt="1.0/samp_rate",
                      kp="0.25", ki="0.01", limit="1.0",
                      i_d_ref="i_d_ref", i_q_ref="i_q_ref"))

    # ----- MEASUREMENT HALF ------------------------------------------------ #
    # ia and ib are two independent ingress arms into the Clarke rendezvous.
    blocks += [
        ksource("src_ia", "ia",
                "ingress arm: phase current ia. Its own stream id so the "
                "server resolves this burst to the ia arm's own entry/hop/"
                "data registers.", 440, 560),
        ksource("src_ib", "ib",
                "ingress arm: phase current ib. The two-shunt FOC front end "
                "senses only ia and ib; with ia+ib+ic=0 the third is "
                "redundant.", 440, 680),
        relay("relay_ia",
              "per-arm relay (INV-71): without it this arm lands on the port "
              "cell and shares a face with the others, which the Clarke "
              "rendezvous LOCK bars.", 632, 560),
        relay("relay_ib", "per-arm relay (INV-71) — see relay_ia.", 632, 680),
    ]
    blocks.append({"name": "clarke", "id": "kyttar_clarke_transform",
                   "parameters": {"affinity": "", "alias": "",
                                  "comment": "ON CHIP: the amplitude-invariant "
                                  "two-current Clarke. i_alpha = ia; i_beta = "
                                  "(ia + 2*ib)/sqrt(3). One cell, a two-arm "
                                  "face-locking rendezvous — the two inputs "
                                  "MUST reach it on two different faces.",
                                  "device_id": '"kyttar_0"',
                                  "maxoutbuf": "0", "minoutbuf": "0"},
                   "states": st(824, 608)})

    # Clarke's complex (i_alpha + j i_beta) -> the Park rotation's x and y.
    # blocks_complex_to_float is SPLICED OUT by the placeKYT importer: output 0
    # becomes the upstream block's I rail (yi) and output 1 its Q rail (yq), so
    # this is pure GRC type glue and costs no cell.
    blocks.append({"name": "ab_split", "id": "blocks_complex_to_float",
                   "parameters": {"affinity": "", "alias": "",
                                  "comment": "type glue only — the importer "
                                  "splices this out, wiring Clarke's I rail to "
                                  "the rotation's x and its Q rail to y. No "
                                  "cell is spent.",
                                  "maxoutbuf": "0", "minoutbuf": "0",
                                  "vlen": "1"},
                   "states": st(1016, 608)})

    blocks += [
        ksource("src_th_park", "th_park",
                "ingress arm: rotor electrical angle theta for the FORWARD "
                "Park rotation. theta feeds BOTH rotations, and on-chip "
                "fan-out to two rendezvous arms is the hard part — so it is "
                "delivered as TWO independent ingress arms, one per consumer, "
                "each with its own stream id and its own relay.", 440, 800),
        relay("relay_th_park",
              "per-arm relay for the forward Park's theta arm.", 632, 800),
    ]
    blocks.append({"name": "park", "id": "kyttar_cordic_rotate",
                   "parameters": {"affinity": "", "alias": "",
                                  "comment": "ON CHIP: the FORWARD Park "
                                  "rotation, sign = -1 (rotate by -theta). "
                                  "Takes the measured stationary-frame current "
                                  "vector into the rotor frame, where the "
                                  "torque- and flux-producing components "
                                  "separate. A THREE-arm rendezvous: x, y and "
                                  "theta must land on three distinct faces.",
                                  "device_id": '"kyttar_0"', "sign": "-1",
                                  "maxoutbuf": "0", "minoutbuf": "0"},
                   "states": st(1208, 664)})

    blocks.append(ksink("sink_idq", "ia",
                        "drains the measurement half's result: the rotor-frame "
                        "currents (i_d, i_q) interleaved. Q15 floats — the "
                        "sink emits word/32768.", 1400, 664, in_type="complex"))
    # The kyttar SINK always emits FLOAT, whatever its input type: a complex
    # on-chip stream egresses as its two rails INTERLEAVED. So the recovered
    # (i_d, i_q) stream is split with a Deinterleave at 2, not a complex_to_float.
    blocks.append({"name": "idq_split", "id": "blocks_deinterleave",
                   "parameters": {"affinity": "", "alias": "",
                                  "blocksize": "1", "comment":
                                  "split the recovered interleaved "
                                  "[i_d, i_q, i_d, i_q, ...] float stream into "
                                  "the two rails the error former subtracts "
                                  "from. The kyttar sink emits FLOAT even for a "
                                  "complex on-chip stream — the rails come back "
                                  "interleaved.",
                                  "maxoutbuf": "0", "minoutbuf": "0",
                                  "num_streams": "2", "type": "float",
                                  "vlen": "1"},
                   "states": st(1592, 664)})

    # ----- COMMAND HALF ---------------------------------------------------- #
    blocks += [
        ksource("src_ed", "e_d",
                "ingress arm: the d-axis current error into PI(d).", 200, 200),
        ksource("src_eq", "e_q",
                "ingress arm: the q-axis current error into PI(q).", 200, 320),
        relay("relay_ed", "per-arm relay (INV-71) for the e_d arm.", 392, 200),
        relay("relay_eq", "per-arm relay (INV-71) for the e_q arm.", 392, 320),
    ]
    for nm, axis, x, y in (("pi_d", "d", 584, 200), ("pi_q", "q", 584, 320)):
        blocks.append({"name": nm, "id": "kyttar_pi_controller",
                       "parameters": {"affinity": "", "alias": "",
                                      "comment": f"ON CHIP: the {axis}-axis "
                                      "current PI. 32-bit anti-windup "
                                      "integrator — the one place 16 bits is "
                                      "NOT enough: ki*e per step can fall "
                                      "below one Q15 LSB and would vanish "
                                      "silently in a 16-bit accumulator. Both "
                                      "axes share the tuning because Ld == Lq "
                                      "on a surface PMSM.",
                                      "device_id": '"kyttar_0"',
                                      "kp": "0.25", "ki": "0.01",
                                      "limit": "1.0",
                                      "maxoutbuf": "0", "minoutbuf": "0"},
                       "states": st(x, y)})

    blocks += [
        ksource("src_th_ipark", "th_ipark",
                "ingress arm: theta for the INVERSE Park rotation — the "
                "second of theta's two independent deliveries.", 200, 440),
        relay("relay_th_ipark",
              "per-arm relay for the inverse Park's theta arm.", 392, 440),
    ]
    blocks.append({"name": "ipark", "id": "kyttar_cordic_rotate",
                   "parameters": {"affinity": "", "alias": "",
                                  "comment": "ON CHIP: the INVERSE Park "
                                  "rotation, sign = +1 (rotate by +theta). "
                                  "Carries the rotor-frame voltage command "
                                  "(v_d, v_q) back to the stationary frame as "
                                  "(v_alpha, v_beta). A THREE-arm rendezvous.",
                                  "device_id": '"kyttar_0"', "sign": "1",
                                  "maxoutbuf": "0", "minoutbuf": "0"},
                   "states": st(776, 296)})

    blocks.append({"name": "v_split", "id": "blocks_complex_to_float",
                   "parameters": {"affinity": "", "alias": "",
                                  "comment": "type glue — spliced out by the "
                                  "importer; the rotation's I rail becomes "
                                  "v_alpha and its Q rail v_beta.",
                                  "maxoutbuf": "0", "minoutbuf": "0",
                                  "vlen": "1"},
                   "states": st(968, 296)})

    blocks.append({"name": "svpwm", "id": "kyttar_svpwm",
                   "parameters": {"affinity": "", "alias": "",
                                  "comment": "ON CHIP: space-vector PWM by "
                                  "min-max (common-mode) injection. Emits "
                                  "THREE words per sample on ONE stream, fixed "
                                  "order a, b, c — hence the q15 sink and the "
                                  "Deinterleave at 3 downstream. The midpoint "
                                  "injection buys SVPWM its ~15.5% linear-range "
                                  "advantage over sine PWM.",
                                  "device_id": '"kyttar_0"',
                                  "maxoutbuf": "0", "minoutbuf": "0"},
                   "states": st(1160, 296)})

    blocks.append(ksink("sink_duty", "e_d",
                        "drains the SVPWM duty stream: three Q15 words per "
                        "control period, order a, b, c.", 1352, 296))

    blocks.append({"name": "duty_split", "id": "blocks_deinterleave",
                   "parameters": {"affinity": "", "alias": "",
                                  "blocksize": "1", "comment":
                                  "split the 3-word duty packet into the three "
                                  "per-phase duty streams. Deinterleave at 3 "
                                  "is the documented way to unpack this "
                                  "block's output.",
                                  "maxoutbuf": "0", "minoutbuf": "0",
                                  "num_streams": "3", "type": "float",
                                  "vlen": "1"},
                   "states": st(1544, 296)})

    # ----- the loop, and where it CLOSES ----------------------------------- #
    # The feedback edge (duties -> motor -> sensed currents) is NOT a
    # connection here: it is an assignment inside foc_host. That is precisely
    # why this flowgraph starts. What remains are two feed-forward branches.
    # measurement half: the three sensed quantities, live off the motor
    C("foc_host", 0, "src_ia", 0)          # ia
    C("foc_host", 1, "src_ib", 0)          # ib
    C("foc_host", 2, "src_th_park", 0)     # theta, arm 1 of 2
    C("src_ia", 0, "relay_ia", 0)
    C("src_ib", 0, "relay_ib", 0)
    C("relay_ia", 0, "clarke", 0)          # ia
    C("relay_ib", 0, "clarke", 1)          # ib
    C("clarke", 0, "ab_split", 0)
    C("ab_split", 0, "park", 0)            # i_alpha -> x
    C("ab_split", 1, "park", 1)            # i_beta  -> y
    C("src_th_park", 0, "relay_th_park", 0)
    C("relay_th_park", 0, "park", 2)       # theta
    C("park", 0, "sink_idq", 0)
    C("sink_idq", 0, "idq_split", 0)

    # command half: the two current errors, formed on the host this period
    C("foc_host", 3, "src_ed", 0)          # e_d
    C("foc_host", 4, "src_eq", 0)          # e_q
    C("foc_host", 5, "src_th_ipark", 0)    # theta, arm 2 of 2
    C("src_ed", 0, "relay_ed", 0)
    C("src_eq", 0, "relay_eq", 0)
    C("relay_ed", 0, "pi_d", 0)
    C("relay_eq", 0, "pi_q", 0)
    C("pi_d", 0, "ipark", 0)               # v_d -> x
    C("pi_q", 0, "ipark", 1)               # v_q -> y
    C("src_th_ipark", 0, "relay_th_ipark", 0)
    C("relay_th_ipark", 0, "ipark", 2)     # theta
    C("ipark", 0, "v_split", 0)
    C("v_split", 0, "svpwm", 0)            # v_alpha
    C("v_split", 1, "svpwm", 1)            # v_beta
    C("svpwm", 0, "sink_duty", 0)
    C("sink_duty", 0, "duty_split", 0)

    # ----- the scopes ------------------------------------------------------ #
    blocks.append(time_sink(
        "scope_idq", "rotor-frame currents i_d, i_q — array vs host (Q15)", 4,
        "burst_len", -0.6, 0.6, "current (Q15 fraction of full scale)",
        "THE DEMO PLOT — the loop regulating. i_q climbs to the torque "
        "reference and i_d is held at zero. FOUR traces: the two the ARRAY "
        "returned and the two the host computed for the same control periods, "
        "so the array's measurement half is checked against its own golden on "
        "screen rather than merely displayed. The buffer is a FULL burst: a "
        "short buffer paints nothing when the batch arrives at once, and the "
        "kyttar sink emits q15/32768 floats, so the y axis is in Q15 units.",
        1976, 560, labels=["i_d (array)", "i_q (array)",
                           "i_d (host)", "i_q (host)"]))
    blocks.append(time_sink(
        "scope_duty", "inverter duty cycles a, b, c — array vs host (Q15)", 6,
        "burst_len", -0.7, 0.7, "duty (Q15)",
        "the three SVPWM duty cycles — the min-max injected set the inverter "
        "legs actually switch on — as the ARRAY returned them, over the three "
        "the host computed for the same periods. The host set is the one that "
        "actually drove the motor this run; the array set is the check on it.",
        1736, 296, labels=["duty a (array)", "duty b (array)", "duty c (array)",
                           "duty a (host)", "duty b (host)", "duty c (host)"]))
    blocks.append(time_sink(
        "scope_err", "current errors e_d, e_q (Q15)", 2, "burst_len",
        -0.6, 0.6, "error (Q15)",
        "the two current errors driving the PI controllers — both converge to "
        "zero as the loop settles.",
        1976, 800, labels=["e_d", "e_q"]))

    C("idq_split", 0, "scope_idq", 0)
    C("idq_split", 1, "scope_idq", 1)
    C("duty_split", 0, "scope_duty", 0)
    C("duty_split", 1, "scope_duty", 1)
    C("duty_split", 2, "scope_duty", 2)
    C("foc_host", 6, "scope_idq", 2)       # i_d, as the host computed it
    C("foc_host", 7, "scope_idq", 3)       # i_q, as the host computed it
    C("foc_host", 8, "scope_duty", 3)      # duty a, host
    C("foc_host", 9, "scope_duty", 4)      # duty b, host
    C("foc_host", 10, "scope_duty", 5)     # duty c, host
    C("foc_host", 3, "scope_err", 0)       # e_d
    C("foc_host", 4, "scope_err", 1)       # e_q

    options = {"id": "options", "parameters": {
        "author": "Lattrex", "catch_exceptions": "True",
        "category": "[GRC Hier Blocks]", "cmake_opt": "", "comment": "",
        "copyright": "", "description": DESCRIPTION, "gen_cmake": "On",
        "gen_linking": "dynamic", "generate_options": "qt_gui",
        "hier_block_src_path": ".:", "id": "foc_motor", "max_nouts": "0",
        "output_language": "python", "placement": "(0,0)", "qt_qss_theme": "",
        "realtime_scheduling": "", "run": "True",
        "run_command": "{python} -u {filename}", "run_options": "prompt",
        "sizing_mode": "fixed", "thread_safe_setters": "",
        "title": "FOC motor control — the FULL current loop",
        "window_size": "(2600,1400)"},
        "states": st(8, 8)}

    _apply_layout(blocks, _LANES)

    return {"options": options, "blocks": blocks, "connections": conns,
            "metadata": {"file_format": 1, "grc_version": "3.10.12.0"}}


def main():
    fg = build()
    OUT.write_text(yaml.safe_dump(fg, sort_keys=False, default_flow_style=False,
                                  width=100))
    n_chip = sum(1 for b in fg["blocks"] if b["id"].startswith("kyttar_")
                 and b["id"] not in ("kyttar_source", "kyttar_sink"))
    print(f"wrote {OUT}")
    print(f"  {len(fg['blocks'])} blocks, {len(fg['connections'])} connections, "
          f"{n_chip} on-chip blocks")


if __name__ == "__main__":
    main()
