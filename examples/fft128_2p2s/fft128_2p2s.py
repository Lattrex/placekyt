# SPDX-License-Identifier: GPL-3.0-or-later
"""FFT128 on the 2P2S board — build it, place it, route it, drive it.

N=128 does not fit one die: its 7-stage ctl/out spine needs 14 rows in ONE
column against a 12-row array. The supported topology is a STAGE-BOUNDARY
SPLIT across two dies, cut after stage 0. This module maps that split onto
the **2P2S dev board** (``placekyt/resources/boards/dev2p2s.kdb``) — four
dies in two parallel daisy-chains of two — rather than an ad-hoc two-chip
project.

    chain A   chip 0 (A0, head)                chip 1 (A1, tail)
              ┌──────────────────────┐         ┌──────────────────────┐
              │ FFT128Die0           │         │ FFT128Die1           │
              │ stage 0 — the        │         │ stages 1..6          │
              │ period-64 octant     │         │ (delays 32/16/…/1)   │
              │ fold, 30 cells       │         │ 84 cells             │
              └──────────────────────┘         └──────────────────────┘
    chainA_in ─▶ x16_in ─▶ die0 ─▶ x16_out ═══▶ x16_in ─▶ die1 ─▶ x16_out ─▶ chainA_out
                                    the board's ON-CARRIER series link
                                    (the FPGA never sees it)

    chain B   chip 2 (B0) ═══▶ chip 3 (B1)     — idle in this example, and
                                                 that is the point: the board
                                                 has a second chain the design
                                                 does not need.

**Why chain A, and why this die order.** The board gives the FPGA exactly two
handles per chain: the chain HEAD's ``x16_in`` and the chain TAIL's
``x16_out``. A stage-boundary cut of a feed-forward pipeline needs one
crossing in one direction, and the board provides exactly that as an
on-carrier series link. So the transform's input enters the chain head, the
partially-transformed stream crosses the carrier link, and the bins leave the
chain tail — the design's dataflow and the board's wiring are the same shape.
Putting die 0 on the TAIL would need the stream to run backwards over a link
the carrier does not provide.

Chain B is deliberately left empty. The 2P2S board's two chains are
independent, and a 2-die design occupies one of them; the free chain is where
a second instance (or the 1P4S re-chaining) would go.

THE DRIVE IS PART OF THE DESIGN. A complex sample is a THREE-PART transaction
— ``WRITE xi``, ``WRITE xq``, then ONE ``JUMP`` — and on the multi-chip path
each part must be pumped to quiescence before the next is injected. Queuing
all three and running once, or omitting the pump between the two operand
writes, overruns the single-outstanding input handshake and the design makes
no forward progress. See ``README.md`` and ``fft128_2p2s_demo.py``.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for _p in (str(ROOT / "runtime" / "python"), str(ROOT / "placekyt")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

KYT_PATH = HERE / "fft128_2p2s.kyt"
GRC_PATH = HERE / "fft128_2p2s.grc"
CHIP_YAML = str(ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml")
BOARD_KDB = str(ROOT / "placekyt" / "resources" / "boards" / "dev2p2s.kdb")

#: The transform size and its latency (the two dies contribute 64 + 63).
N = 128
LATENCY = N - 1

#: The 2P2S board's four dies. The transform occupies CHAIN A; chain B's two
#: dies are present (the board has them) and carry no blocks.
CHIP_DIE0 = 0   # A0 — chain A head, the die the FPGA drives
CHIP_DIE1 = 1   # A1 — chain A tail, the die the FPGA reads
CHAIN_B = (2, 3)

#: The board's per-chip labels, so an opened .kyt names the dies the way the
#: board does rather than "Chip 0"/"Chip 1".
CHIP_LABELS = {
    0: "A0 (chain A head)",
    1: "A1 (chain A tail)",
    2: "B0 (chain B head)",
    3: "B1 (chain B tail)",
}

#: Declared anchors. A die MUST be placed at its own ``default_anchor`` — the
#: placer normalises a footprint to its bounding box, so anchoring anywhere
#: else translates the fold and invalidates the reserved egress lane. Die 0's
#: plan sits at min x = 1; placed at (0, 0) it seals the input port.
DIE0_ANCHOR = (1, 0)
DIE1_ANCHOR = (0, 0)

#: The stream's logical name and its egress tag. The GRC source/sink carry
#: only ``stream_id``; placeKYT resolves it to a chip/port/hop/tag. An input
#: net with NO stream_id is skipped by the multi-chip resolver entirely, so
#: the flowgraph would connect and then quietly deliver nothing.
STREAM_ID = "fft"
OUT_TAG = 7

#: Per-sample simulation budget. ``run(events, rounds)`` is events-per-chip-
#: PER-ROUND x rounds, so these are not "big numbers to be safe" — they are
#: shapes. The two operand pumps only have to walk a word to its landing;
#: the settle after the JUMP has to carry a sample through both dies AND the
#: crossing. Never pass ``max_events_per_chip=None``: rounds bound only the
#: OUTER loop, so one non-terminating round hangs the call outright.
PUMP = (60_000, 5)      # after each operand WRITE
SETTLE = (200_000, 50)  # after the JUMP


#: THE DISPLAY CONTRACT. The array is asynchronous — it has no clock of its
#: own — so the sample rate is DECLARED by the stimulus, and it is what turns a
#: dimensionless bin index into a physical frequency. 32000 is the repo-wide
#: example convention (see examples/fft_spectrum, examples/bpsk_modem); at
#: N = 128 that makes each bin 250 Hz wide. Change it and the whole frequency
#: axis rescales; the chip does not change at all.
SAMP_RATE = 32000.0

#: The shipped ``.grc``'s two ON-BIN stimulus tones, as NATURAL bin indices,
#: with their amplitudes. Kept here so gen_grc.py, the gate and the README all
#: cite ONE statement of the stimulus instead of three that can drift.
TONE_A, AMP_A = 9, 0.45
TONE_B, AMP_B = 37, 0.35

#: The ``.grc``'s dispatched burst: two whole frames past the 127 latency.
BURST = 384


def q15(x: float) -> int:
    """Float in [-1, 1) to a Q15 word."""
    return int(round(max(-1.0, min(32767 / 32768.0, float(x)))
                     * 32768.0)) & 0xFFFF


def s16(w: int) -> int:
    """Q15 word to a signed int."""
    return ((int(w) & 0xFFFF) ^ 0x8000) - 0x8000


def load_board():
    """The 2P2S board definition this example targets."""
    from engine.io.board_io import load_board as _lb
    return _lb(BOARD_KDB)


def build_2p2s():
    """Place both dies on CHAIN A of the 2P2S board, route every net, wire the
    chain's carrier link, and build. Returns ``(controller, build_result,
    die0_name, die1_name)``.

    All FOUR board dies are instantiated — this is the real board, not a
    two-chip subset of it — and both chains' carrier links are wired, so the
    project's inter-chip topology matches ``dev2p2s.kdb`` exactly and passes
    DRC against it.

    Raises AssertionError with the router/build diagnosis if either fails —
    this is a real place-and-route, not a structural approximation of one.
    """
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])

    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from model.connection import BlockEndpoint, ChipPortEndpoint
    from model.project import BoardRef
    from ui.controller import AppController

    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)

    ctrl = AppController(catalog=cat)
    ctrl.new_project("fft128_2p2s", "kyttar_10x12")
    for _ in range(3):
        ctrl.add_chip()                     # chips 1, 2, 3 (chip 0 exists)
    # Name the dies the way the board does.
    for c in ctrl.project.chips:
        c.label = CHIP_LABELS.get(c.id, c.label)
    # Record which board this design targets, so an opened .kyt knows.
    ctrl.project.board = BoardRef(name="dev2p2s",
                                  config="resources/boards/dev2p2s.kdb")

    d0 = ctrl.place_block("FFT128Die0", CHIP_DIE0, *DIE0_ANCHOR,
                          library="lattrex.official")
    d1 = ctrl.place_block("FFT128Die1", CHIP_DIE1, *DIE1_ANCHOR,
                          library="lattrex.official")

    # chain A head: the transform's input lands on die 0; die 0's primary rail
    # exits toward the carrier link. Only the PRIMARY complex rail is wired to
    # a port — a complex exit cell emits out_i then out_q from one cell and
    # they ride the same corridor interleaved; wiring a second net to the same
    # port kills egress.
    ctrl.add_logical_connection(
        ChipPortEndpoint(chip=CHIP_DIE0, port="x16_in"),
        BlockEndpoint(block=d0, port="xi"), name="a0_in_xi")
    ctrl.add_logical_connection(
        ChipPortEndpoint(chip=CHIP_DIE0, port="x16_in"),
        BlockEndpoint(block=d0, port="xq"), name="a0_in_xq")
    ctrl.add_logical_connection(
        BlockEndpoint(block=d0, port="out_i"),
        ChipPortEndpoint(chip=CHIP_DIE0, port="x16_out"), name="a0_out")
    # chain A tail: the carrier link lands on die 1; die 1's output is the
    # transform's, and it leaves at the chain tail the FPGA reads.
    ctrl.add_logical_connection(
        ChipPortEndpoint(chip=CHIP_DIE1, port="x16_in"),
        BlockEndpoint(block=d1, port="xi"), name="a1_in_xi")
    ctrl.add_logical_connection(
        ChipPortEndpoint(chip=CHIP_DIE1, port="x16_in"),
        BlockEndpoint(block=d1, port="xq"), name="a1_in_xq")
    ctrl.add_logical_connection(
        BlockEndpoint(block=d1, port="out_i"),
        ChipPortEndpoint(chip=CHIP_DIE1, port="x16_out"), name="a1_out")

    rep = ctrl.auto_route_all({"kyttar_10x12": ct})
    assert rep.ok, ("route failed: "
                    + "; ".join(f"{r.name}:{r.reason}" for r in rep.failed))

    # Tag the stream so the GRC live bridge can resolve it. The GR source/sink
    # carry ONLY ``stream_id``; placeKYT owns which chip/port/hop/tag it maps
    # to. Without a stream_id on the input nets the multi-chip server resolves
    # NO targets and the flowgraph silently produces nothing.
    for c in ctrl.project.connections:
        if c.name in ("a0_in_xi", "a0_in_xq"):
            c.stream_id = STREAM_ID
        elif c.name == "a1_out":
            c.out_tag = OUT_TAG

    # THE BOARD'S CARRIER LINKS. Both chains' series links are wired, exactly
    # as dev2p2s.kdb describes them — chain A carries the transform, chain B
    # is wired and idle. The hop is continuous across a link: the build
    # composes die 0's exit hop past the boundary onto die 1's landing, so a
    # crossing word self-routes exactly like an intra-chip hop.
    ctrl.add_inter_chip(CHIP_DIE0, "x16_out", CHIP_DIE1, "x16_in")
    ctrl.add_inter_chip(CHAIN_B[0], "x16_out", CHAIN_B[1], "x16_in")

    bres = ctrl.build()
    assert bres.ok, "build failed: " + "; ".join(str(e) for e in bres.errors)
    return ctrl, bres, d0, d1


def open_engine(bres, *, trace=()):
    """A loaded, connected, configured ``MultiChipSimEngine`` for a built
    design, plus die 0's resolved input landing.

    All four board dies are loaded and both carrier links connected, so the
    simulated system is the whole board. Ordering is load-bearing: construct →
    ``connect`` the wires → per-chip ``load`` + ``configure_input_port`` → only
    then inject. ``trace`` is the chip ids to enable tracing on (it must be
    requested AT LOAD — re-loading a chip afterwards to turn it on resets that
    chip's state).
    """
    from engine.simulator import MultiChipSimEngine

    paths = {cid: CHIP_YAML for cid in sorted(CHIP_LABELS)}
    eng = MultiChipSimEngine(paths)
    eng.connect(CHIP_DIE0, "x16_out", CHIP_DIE1, "x16_in")
    eng.connect(CHAIN_B[0], "x16_out", CHAIN_B[1], "x16_in")
    for cid in sorted(CHIP_LABELS):
        eng.load(cid, bres.words(cid), trace=(cid in trace))
        landings = list(bres.chips[cid].input_landings.values())
        if not landings:
            continue          # chain B carries no blocks — nothing to land
        il = landings[0]
        # ROUTED: both dies land off the port cell (a corridor reaches them),
        # so the head injection AND the inter-chip relay must deliver by
        # WRITE+JUMP over the configured hop, not an at-landing raw queue.
        eng.configure_input_port(cid, "x16_in", entry_addr=il["entry"],
                                 hop_count=il["hop"],
                                 data_addr=il["data_addrs"][0], routed=True)
    return eng, list(bres.chips[CHIP_DIE0].input_landings.values())[0]


def drive(eng, landing, words, *, on_sample=None):
    """Run ``words`` (a list of ``(i_q15, q_q15)`` pairs) through chain A.

    Each sample is the complex transaction — WRITE xi, pump, WRITE xq, pump,
    JUMP, settle — then the chain TAIL's output port is drained. Returns the
    flat list of egressed Q15 words (two per sample: out_i then out_q).

    ``on_sample(k, out_words, run_info)`` is called after every sample, which
    is how the demo reports where forward progress stops.
    """
    sim = eng._sim
    head = f"chip{CHIP_DIE0}"
    hop, entry = int(landing["hop"]), int(landing["entry"])
    a0, a1 = int(landing["data_addrs"][0]), int(landing["data_addrs"][1])

    got: list[int] = []
    for k, (wi, wq) in enumerate(words):
        sim.inject_data_physical(head, [wi], hop, a0)
        sim.run(*PUMP)
        sim.inject_data_physical(head, [wq], hop, a1)
        sim.run(*PUMP)
        sim.inject_jump_physical(head, hop, entry)
        info = sim.run(*SETTLE)
        out = eng.capture(CHIP_DIE1, "x16_out")
        got.extend(out)
        if on_sample is not None:
            on_sample(k, out, info)
    return got


def reference(words):
    """The whole N=128 transform's streaming reference for ``words``."""
    from gr_kyttar.placement.blocks.fft_large import sdf_streaming_reference
    return sdf_streaming_reference(N, words)


def crossing_reference(words):
    """What die 0 sends across the carrier link — the PARTIALLY transformed
    stream die 1 consumes. Not frequency bins."""
    from gr_kyttar.placement.blocks.fft_large import sdf_streaming_reference
    return sdf_streaming_reference(N, words, (0, 0))


def stimulus(n, seed=9):
    """``n`` random I/Q samples, the stimulus the gates drive."""
    import numpy as np
    rng = np.random.default_rng(seed)
    return [(q15(rng.uniform(-0.6, 0.6)), q15(rng.uniform(-0.6, 0.6)))
            for _ in range(n)]


def grc_stimulus(n: int = BURST):
    """The SHIPPED ``.grc``'s own two-tone stimulus, as Q15 ``(i, q)`` pairs.

    The flowgraph computes the same expression inline (so a reader can see the
    stimulus in the flowgraph); this is the reference the gates drive and
    compare against, so the two cannot drift apart silently.
    """
    import cmath

    out = []
    for k in range(int(n)):
        c = (AMP_A * cmath.exp(2j * cmath.pi * TONE_A * k / N)
             + AMP_B * cmath.exp(2j * cmath.pi * TONE_B * k / N))
        out.append((q15(c.real), q15(c.imag)))
    return out


# ------------------------------------------------- the bin -> Hz mapping
def bin_hz(n_fft: int = N, samp_rate: float = SAMP_RATE) -> float:
    """BIN WIDTH in Hz: ``samp_rate / n_fft`` — 250 Hz at N=128, fs=32 kHz."""
    return float(samp_rate) / int(n_fft)


def bin_to_hz(k: int, n_fft: int = N, samp_rate: float = SAMP_RATE) -> float:
    """NATURAL bin index -> frequency in Hz. THE published mapping.

    ``f(k) = k*fs/N`` for ``k < N/2`` (positive frequencies) and
    ``f(k) = (k-N)*fs/N`` for ``k >= N/2`` (the negative half) — the standard
    DFT bin map, stated once so the README, the ``.grc``'s axis config and the
    gates cite the SAME arithmetic.

    The shipped tones: bin 9 -> 2250.0 Hz, bin 37 -> 9250.0 Hz.
    """
    n, k = int(n_fft), int(k)
    if not 0 <= k < n:
        raise ValueError(f"bin {k} is out of range for an {n}-point transform")
    signed = k if k < n // 2 else k - n
    return signed * bin_hz(n, samp_rate)


def fftshift_order(n_fft: int = N):
    """``natural bin -> index on the CENTRED axis`` (numpy's ``fftshift`` map).

    The natural-order vector runs 0 -> +fs/2 and then JUMPS to -fs/2 -> 0,
    which no single linear axis can label. Rolling by N/2 makes the vector
    monotonic in frequency, so the display can run a real Hz axis from
    ``-samp_rate/2`` in steps of ``bin_hz``.
    """
    n = int(n_fft)
    return [(k + n // 2) % n for k in range(n)]


def axis_hz(n_fft: int = N, samp_rate: float = SAMP_RATE):
    """The CENTRED display axis in Hz — one value per plotted point, exactly
    what the vector sink's ``set_x_axis(-samp_rate/2, bin_hz)`` labels."""
    n = int(n_fft)
    step = bin_hz(n, samp_rate)
    return [-float(samp_rate) / 2 + i * step for i in range(n)]


# ------------------------------------------------ the bin-order contract
def unreverse(n: int = N):
    """``slot -> natural frequency bin``: the chain's BIT-REVERSED output map.

    Output slot ``k`` carries bin ``bit_reverse(k, log2 n)``. The map is an
    involution, so it is its own inverse. This function IS the display
    contract: getting it wrong (or skipping it) puts the tones in the wrong
    place on the plot while every bit-exactness gate still passes, which is
    why the mutation suite attacks it directly.
    """
    bits = int(n).bit_length() - 1
    out = []
    for k in range(n):
        r, v = 0, k
        for _ in range(bits):
            r = (r << 1) | (v & 1)
            v >>= 1
        out.append(r)
    return out


def frames_of(pairs, n_fft: int = N, latency: int = LATENCY):
    """Complete COMPLEX output frames of a per-trigger stream, latency
    stripped. ``pairs`` is the stream of ``(i_word, q_word)`` Q15 pairs."""
    n, out, k = int(n_fft), [], int(latency)
    while k + n <= len(pairs):
        out.append(list(pairs[k:k + n]))
        k += n
    return out


def centred_power_spectrum(frame, n_fft: int = N):
    """ONE complex frame of chip SLOTS -> the CENTRED power vector the shipped
    ``.grc`` plots. THE display arithmetic, in one place.

    ``frame`` is the chip's ``(i_word, q_word)`` slots in emission order. The
    return is indexed by position on the ``-fs/2 .. +fs/2`` axis: index ``i``
    is ``-samp_rate/2 + i*bin_hz`` Hz. Power is ``|z|^2`` at the q15/32768
    scale, so a full-scale coherent bin reads 1.0.

    Three maps, and every one of them is load-bearing:

      1. de-interleave the complex pair (the tail is a COMPLEX exit cell, so
         the sink stream is out_i, out_q, ... — two words per bin);
      2. un-reverse the DIF slot order (:func:`unreverse`);
      3. fftshift, so a single linear Hz axis can label every point.
    """
    n = int(n_fft)
    if len(frame) != n:
        raise ValueError(f"a frame is {n} slots, got {len(frame)}")
    rev = unreverse(n)
    shift = fftshift_order(n)
    nat = [0.0] * n
    for slot, (wi, wq) in enumerate(frame):
        re = s16(wi) / 32768.0
        im = s16(wq) / 32768.0
        nat[rev[slot]] = re * re + im * im
    out = [0.0] * n
    for k, i in enumerate(shift):
        out[i] = nat[k]
    return out
