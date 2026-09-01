# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate ``foc_motor.grc`` — the FULL FOC loop as a GNU Radio flowgraph.

The flowgraph is large and highly regular (five on-chip blocks, four ingress
arms each with its own source, a plant, an error former, three scopes), so it
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

PLANT_SRC = '''"""THE MOTOR — a surface-PMSM plant, the part that is NOT on the chip.

Three SVPWM duty cycles in (one packet per control period, order a, b, c),
the three sensed quantities out: phase currents ia, ib and the rotor
electrical angle theta. So this block speaks exactly the flowgraph's wire
format on both sides and closes the loop around the array.

THE MODEL. Surface PMSM (Ld == Lq == L) in the stationary two-phase frame,
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

The angle is emitted in the shipped 16-bit half-turn convention scaled to a
Q15 FLOAT in [-1, 1): value = theta/pi. The currents are emitted as Q15
fractions of i_base. That is what the kyttar sources inject.
"""
import math
import numpy as np
from gnuradio import gr


class blk(gr.sync_block):
    def __init__(self, r_s=0.35, l_s=1.5e-3, ke=0.035, v_dc=24.0,
                 i_base=10.0, omega_e=200.0, dt=35e-6):
        gr.sync_block.__init__(
            self, name='PMSM plant (duties -> ia, ib, theta)',
            in_sig=[np.float32, np.float32, np.float32],
            out_sig=[np.float32, np.float32, np.float32])
        self.r_s, self.l_s, self.ke = float(r_s), float(l_s), float(ke)
        self.v_dc, self.i_base = float(v_dc), float(i_base)
        self.omega_e, self.dt = float(omega_e), float(dt)
        self.i_alpha = 0.0
        self.i_beta = 0.0
        self.theta_e = 0.0

    def work(self, input_items, output_items):
        da_in, db_in, dc_in = input_items[0], input_items[1], input_items[2]
        ia_o, ib_o, th_o = output_items[0], output_items[1], output_items[2]
        n = len(da_in)
        for k in range(n):
            # --- the sensors, BEFORE this period's update: what the drive
            # --- measured is the state the duties were computed from.
            ia = self.i_alpha
            ib = (math.sqrt(3.0) * self.i_beta - ia) / 2.0
            th = self.theta_e % (2.0 * math.pi)
            if th >= math.pi:
                th -= 2.0 * math.pi
            ia_o[k] = ia / self.i_base
            ib_o[k] = ib / self.i_base
            th_o[k] = th / math.pi

            # --- the plant update -------------------------------------------
            A, B, C = float(da_in[k]), float(db_in[k]), float(dc_in[k])
            # forward Clarke of the duty set. The SVPWM's common-mode
            # injection cancels here exactly as it does in an isolated-neutral
            # machine: common mode drives no current.
            v_alpha = (2.0 * A - B - C) / 3.0 * self.v_dc
            v_beta = (B - C) / math.sqrt(3.0) * self.v_dc
            amp = self.ke * self.omega_e
            e_alpha = -amp * math.sin(self.theta_e)
            e_beta = amp * math.cos(self.theta_e)
            self.i_alpha += (v_alpha - self.r_s * self.i_alpha - e_alpha) \\
                / self.l_s * self.dt
            self.i_beta += (v_beta - self.r_s * self.i_beta - e_beta) \\
                / self.l_s * self.dt
            self.theta_e = (self.theta_e + self.omega_e * self.dt) \\
                % (2.0 * math.pi)
        return n
'''

ERROR_SRC = '''"""THE ERROR FORMER — e = reference - measured, on the HOST.

Two inputs (the measured i_d and i_q that come back off the chip's
measurement half), two outputs (the d- and q-axis current errors that go into
the chip's two PI controllers).

This subtraction is on the host BY CONSTRUCTION, not by omission: the
reference is a supervisory quantity (a torque command from an outer speed
loop, or an operator setpoint), it changes on a far slower timescale than the
current loop, and putting a two-input subtract on the array would spend a
rendezvous — the scarcest resource in this design — on an operation the host
does for free between bursts.

i_d_ref is ZERO for a surface PMSM: the magnets already supply the rotor
flux, so any d-axis current is pure loss. i_q_ref is the torque command.

The output saturates to the Q15 range, matching what the chip's PI input can
represent.
"""
import numpy as np
from gnuradio import gr


class blk(gr.sync_block):
    def __init__(self, i_d_ref=0.0, i_q_ref=0.30):
        gr.sync_block.__init__(
            self, name='FOC error (ref - measured)',
            in_sig=[np.float32, np.float32],
            out_sig=[np.float32, np.float32])
        self.i_d_ref = float(i_d_ref)
        self.i_q_ref = float(i_q_ref)

    def work(self, input_items, output_items):
        lim = 32767.0 / 32768.0
        output_items[0][:] = np.clip(
            self.i_d_ref - input_items[0], -1.0, lim)
        output_items[1][:] = np.clip(
            self.i_q_ref - input_items[1], -1.0, lim)
        return len(input_items[0])
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
    "embedded PMSM plant so the loop actually runs. "
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

    # ----- the plant ------------------------------------------------------- #
    blocks.append(epy("plant", PLANT_SRC,
                      "THE MOTOR — the only block here that is not on the "
                      "chip and never will be. Duties in, sensed ia/ib/theta "
                      "out; this is what closes the loop.", 200, 620,
                      r_s="0.35", l_s="1.5e-3", ke="0.035", v_dc="24.0",
                      i_base="10.0", omega_e="200.0", dt="1.0/samp_rate"))

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

    # ----- the host-side error former -------------------------------------- #
    blocks.append(epy("err", ERROR_SRC,
                      "e = reference - measured, on the HOST by construction. "
                      "The reference is supervisory and slow; spending an "
                      "on-chip rendezvous — the scarcest resource here — on a "
                      "subtraction the host does for free would be a poor "
                      "trade.", 1784, 664,
                      i_d_ref="i_d_ref", i_q_ref="i_q_ref"))

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

    # ----- close the loop -------------------------------------------------- #
    # duties -> plant -> sensed ia, ib, theta -> the four ingress sources.
    C("duty_split", 0, "plant", 0)
    C("duty_split", 1, "plant", 1)
    C("duty_split", 2, "plant", 2)
    C("plant", 0, "src_ia", 0)
    C("plant", 1, "src_ib", 0)
    C("plant", 2, "src_th_park", 0)
    C("plant", 2, "src_th_ipark", 0)

    # measurement half
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

    # error formation
    C("idq_split", 0, "err", 0)            # i_d
    C("idq_split", 1, "err", 1)            # i_q

    # command half
    C("err", 0, "src_ed", 0)
    C("err", 1, "src_eq", 0)
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
        "scope_idq", "measured rotor-frame currents i_d, i_q (Q15)", 2,
        "burst_len", -0.6, 0.6, "current (Q15 fraction of full scale)",
        "THE DEMO PLOT — the loop regulating. i_q climbs to the torque "
        "reference and i_d is held at zero. The buffer is a FULL burst: a "
        "short buffer paints nothing when the batch arrives at once, and the "
        "kyttar sink emits q15/32768 floats, so the y axis is in Q15 units.",
        1976, 560, labels=["i_d (measured)", "i_q (measured)"]))
    blocks.append(time_sink(
        "scope_duty", "inverter duty cycles a, b, c (Q15)", 3, "burst_len",
        -0.7, 0.7, "duty (Q15)",
        "the three SVPWM duty cycles — the min-max injected set the inverter "
        "legs actually switch on.",
        1736, 296, labels=["duty a", "duty b", "duty c"]))
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
    C("err", 0, "scope_err", 0)
    C("err", 1, "scope_err", 1)

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
        "window_size": "(2400,1200)"},
        "states": st(8, 8)}

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
