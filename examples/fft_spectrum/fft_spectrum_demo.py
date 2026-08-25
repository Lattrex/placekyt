# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless spectrum-analyzer demo — a STREAMING FFT on the fabric, end to end.

A complex I/Q burst drives the placed 64-point streaming R2SDF FFT
(``FFT64Block``, 84 cells) whose complex output feeds a placed
``ComplexToMagSquaredBlock`` — so the whole spectrum analyzer, transform AND
per-bin power, runs ON THE CHIP. What leaves ``x16_out`` is one real
power word per bin, which the GRC flowgraph un-reverses and plots.

THREE CONTRACTS COME FROM THE VERIFIED BLOCK AND ARE NOT NEGOTIABLE HERE
-----------------------------------------------------------------------

1. **BIT-REVERSED output order.** The block emits DIF order with deliberately
   no reorder buffer: output slot ``k`` of each 64-sample frame carries
   frequency bin ``bit_reverse_6(k)``. The example must UN-REVERSE for
   display — :func:`unreverse` is that map and :func:`natural_spectrum`
   applies it. A tone at bin 11 leaves the chip at slot 52, and only after
   un-reversal does it appear at 11. This is gated, not assumed.
2. **Scale is FFT/64** (one round-half-to-even ``>>1`` per stage over six
   stages), so a full-scale on-bin complex exponential produces a coherent
   bin at full scale — magnitude ~1.0, power ~1.0.
3. **Latency is 63 samples.** The first 63 outputs are the deterministic
   startup values of the zero-initialized pipeline; frame ``f`` occupies
   output samples ``63 + 64f .. 126 + 64f``.

WHAT FREQUENCY IS A BIN? (the fourth contract — a DISPLAY one)
--------------------------------------------------------------

A bin index is dimensionless. The array is ASYNCHRONOUS: it has no clock and
no notion of seconds, so it transforms whatever word stream you hand it and
the SAMPLE RATE is a property of your stimulus, which you declare. Given a
declared ``fs`` (:data:`SAMP_RATE`, 32 kHz here, matching both ``.grc``s'
``samp_rate``), natural bin ``k`` of an ``N``-point transform is::

    f(k) = k*fs/N          for k <  N/2     (positive frequencies)
    f(k) = (k-N)*fs/N      for k >= N/2     (negative frequencies)

:func:`bin_to_hz` is that map; :func:`bin_hz` is the bin WIDTH ``fs/N``.
Measured on the real chip at the shipped stimulus: the demo tone at natural
bin 11 is **5500 Hz at N=64** (500 Hz per bin) and **11000 Hz at N=32**
(1000 Hz per bin) — the same bin index, twice the frequency, because a
half-length transform has twice the bin width.

The natural-order vector runs 0 -> +fs/2 and then JUMPS to -fs/2, which no
single linear axis can label — so the shipped flowgraphs apply
:func:`fftshift_order` after the un-reversal and plot a MONOTONIC Hz axis from
``-fs/2`` in ``fs/N`` steps (:func:`axis_hz`). That is why the x axis reads
"frequency (Hz)" rather than "FFT bin".

WHAT THE placeKYT PORT TRACES SHOW (and why they look like stairs)
-------------------------------------------------------------------

In placeKYT's waveform pane the ``x16_in`` port carries TWO tagged rails,
labelled ``fft64.xi`` (real) and ``fft64.xq`` (imaginary) — the block lands
them on two consecutive registers of one cell (measured: entry 12, hop 26,
data_addrs [1, 2]).

They are the Q15 WORD stream at the port: ONE 16-bit word per sample, drawn as
a step per word. A staircase is therefore CORRECT and expected, not a defect —
bin 11 of 64 is only 64/11 = 5.8 samples per cycle, so a per-word trace has
under six steps to draw each period. Verified against this module's own
reference: at N=64 the ``xi`` rail delivers 29491, 13902, -16384, -29349, ...
and ``xq`` delivers 0, 26009, 24521, -2891, ... — the tone's cosine and sine,
sample for sample, and demonstrably DIFFERENT streams (a real complex tone).
``verification/tests/test_fft_spectrum_example.py`` pins both facts.

To SEE the sinusoid rather than the stairs, each ``.grc`` also carries a
stimulus time scope on the I/Q burst, and lowering the ``tone_bin`` variable
raises the samples per cycle (``tone_bin = 1`` gives one full cycle per frame
— the slowest, smoothest stimulus this example can show).

THE PLACEMENT IS PINNED, NOT AUTO-PACKED
----------------------------------------

``FFT64Block`` is a CHIP-SCALE block: its verified layout is a vertical
ctl/out spine 12 rows tall that occupies most of the 10x12 die and cannot
rotate. The generic auto-packer does not model that class and shifts the
spine off the array (measured: cells land at y=12, "off the 10x12 array").
So this example HAND-PLACES — the FFT at its own verified anchor and the
one-cell power stage in the free column-9 lane — and then lets the REAL
auto-router draw every corridor. The router and the build are not bypassed;
only the packer's block anchors are chosen for it.

That is why the shipped ``.kyt`` is the artifact to OPEN, not to re-import:
re-importing the ``.grc`` runs the auto-packer, which cannot place a
chip-scale spine.

Run::

    PYTHONPATH=runtime/python:placekyt QT_QPA_PLATFORM=offscreen \
        .venv/bin/python examples/fft_spectrum/fft_spectrum_demo.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for _p in (str(ROOT / "runtime" / "python"), str(ROOT / "placekyt"), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np  # noqa: E402

CHIP_YAML = str(ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml")
CHIP_TYPE = "kyttar_10x12"
GRC_PATH = HERE / "fft_spectrum.grc"
KYT_PATH = HERE / "fft_spectrum.kyt"

#: The SECOND, smaller variant — same chain, same contracts, N = 32. It ships
#: as its own ``.kyt`` / ``.grc`` pair rather than a parameter switch, because
#: the transform size changes the block CLASS, the cell count, the latency and
#: the bin map all at once; a switch would hide that.
GRC32_PATH = HERE / "fft_spectrum_32.grc"
KYT32_PATH = HERE / "fft_spectrum_32.kyt"

#: ``N -> (block class name, cells)``. Both are CHIP_SCALE blocks whose
#: verified layout is a ctl/out spine, which is what forces the pinned
#: placement (see the module docstring).
SIZES = {64: ("FFT64Block", 84), 32: ("FFT32Block", 60)}

N_FFT = 64
LATENCY = N_FFT - 1                 # 63 — the block's pinned pipeline latency
FRAMES = 3
BURST = LATENCY + N_FFT * FRAMES    # 255 complex samples = 3 whole frames

#: The .grc's demo tone, kept literally in sync: an ON-BIN complex exponential
#: at bin 11. On-bin (an exact integer number of cycles per frame) so the
#: answer is a single clean line with no leakage — the most legible possible
#: spectrum, and a mapping the gate can pin exactly. Bin 11 is valid at both
#: sizes (11 < 32), so both variants use it.
TONE_BIN = 11


def latency_of(n: int) -> int:
    """Pipeline latency of the N-point streaming transform: ``N - 1``."""
    return int(n) - 1


def burst_of(n: int, frames: int = FRAMES) -> int:
    """Complex samples needed for ``frames`` whole output frames at size N."""
    return latency_of(n) + int(n) * int(frames)

#: Tone amplitude. 0.9, NOT full scale — and the reason is a real property of
#: the live path, not caution. The hosted server converts an injected float
#: with ``max(-1.0, min(0.999, f))`` before quantizing, so a sample at
#: 32767/32768 = 0.99997 is CLIPPED to 0.999 and lands as word 32735 instead
#: of 32767. That is 8 samples of a 255-sample full-scale burst (measured:
#: indices 0, 48, 64, 112, 128, 176, 192, 240), which perturbs the spectrum
#: just enough that the live user path no longer matches a full-scale
#: reference computed off-server. At 0.9 the server's conversion and this
#: module's agree on EVERY sample, so the headless reference and the live path
#: are the same stream — which is what lets the user-path gate demand
#: bit-exactness instead of a fuzzy tolerance.
AMPLITUDE = 0.9

#: What a coherent bin must clear, and what every other bin must stay under.
#: DERIVED, not tuned: at the FFT/64 scale an ON-BIN tone of amplitude A puts
#: essentially all its energy in one bin at power A^2 = 0.81 (word ~26542), and
#: an on-bin tone has NO leakage beyond Q15 rounding. The floor is 0.75 (a
#: comfortable margin under 0.81 that a half-scale or mis-scaled spectrum still
#: fails) and the leakage ceiling is 1/32 of full scale.
COHERENT_MIN = int(0.75 * 32768)        # 24576
LEAKAGE_MAX = 32768 // 32               # 1024

#: Block anchors. The FFT is pinned at the origin — that is its OWN verified
#: ``default_layout()`` anchor, the geometry ``test_fft64.py`` gates. The power
#: stage takes (9, 1): column 9 is entirely free of FFT cells and (9, 0) is the
#: x16_out port cell, so (9, 1) sits one hop off the egress port with a clear
#: lane. NORTH is the resting face that lets the egress corridor leave toward
#: the port (a WEST/EAST face at this anchor leaves net3 with "no bus path from
#: source to the broker tap" — measured, not guessed).
FFT_ANCHOR = (0, 0)
MAG2_ANCHOR = (9, 1)
MAG2_FACE = "north"

#: Coherent-bin power floor as a FRACTION, so a size-agnostic caller can
#: derive the word threshold itself.
COHERENT_FRACTION = 0.75

#: The sample rate the stimulus is DECLARED to be sampled at, kept literally in
#: sync with both ``.grc``s' ``samp_rate`` variable.
#:
#: The array is ASYNCHRONOUS — it has no clock of its own and no notion of
#: seconds. It transforms whatever word stream you hand it, so the sample rate
#: is a property of YOUR stimulus, not of the chip, and it is what turns a
#: dimensionless bin index into a physical frequency. 32000 is the repo-wide
#: example convention (see examples/bpsk_modem, examples/css_transceiver).
#: Change it and the whole frequency axis rescales; the chip does not change.
SAMP_RATE = 32000.0


def bin_hz(n_fft: int = N_FFT, samp_rate: float = SAMP_RATE) -> float:
    """BIN WIDTH in Hz: ``samp_rate / n_fft``.

    500 Hz at N=64 and 1000 Hz at N=32, both at the shipped 32 kHz rate — the
    same bin INDEX is twice the frequency at the smaller size, because a
    half-length transform has twice the bin width.
    """
    return float(samp_rate) / int(n_fft)


def bin_to_hz(k: int, n_fft: int = N_FFT, samp_rate: float = SAMP_RATE) -> float:
    """NATURAL bin index -> frequency in Hz. THE published mapping.

    ``f(k) = k*fs/N`` for ``k < N/2`` (positive frequencies) and
    ``f(k) = (k-N)*fs/N`` for ``k >= N/2`` (the negative half) — the standard
    DFT bin map, stated here once so the README, the ``.grc``s' axis config and
    the gates all cite the SAME arithmetic instead of three copies that can
    drift apart.

    Measured on the real chip at the shipped stimulus: the demo tone at natural
    bin 11 reads 5500.0 Hz at N=64 and 11000.0 Hz at N=32.
    """
    n = int(n_fft)
    k = int(k)
    if not 0 <= k < n:
        raise ValueError(f"bin {k} is out of range for an {n}-point transform")
    signed = k if k < n // 2 else k - n
    return signed * bin_hz(n, samp_rate)


def fftshift_order(n_fft: int = N_FFT):
    """``natural bin -> index on the CENTRED axis`` (numpy's ``fftshift`` map).

    The natural-order vector runs 0 -> +fs/2 and then JUMPS to -fs/2 -> 0, which
    no single linear axis can label — that is why "FFT bin (natural order)" was
    the only honest x label the example could carry before. Rolling by N/2 makes
    the vector monotonic in frequency, so the display can run a real Hz axis
    from ``-samp_rate/2`` in steps of ``bin_hz``. Both ``.grc``s' display block
    applies exactly this permutation after the un-reversal.
    """
    n = int(n_fft)
    return [(k + n // 2) % n for k in range(n)]


def centred_spectrum(nat, n_fft: int = None):
    """One NATURAL-order frame -> the CENTRED (``-fs/2 .. +fs/2``) display
    vector the shipped flowgraphs plot. Index ``i`` is ``-samp_rate/2 +
    i*bin_hz`` Hz."""
    n = int(n_fft) if n_fft else len(nat)
    if len(nat) != n:
        raise ValueError(f"a frame is {n} bins, got {len(nat)}")
    out = [0] * n
    for k, i in enumerate(fftshift_order(n)):
        out[i] = nat[k]
    return out


def axis_hz(n_fft: int = N_FFT, samp_rate: float = SAMP_RATE):
    """The CENTRED display axis in Hz — one value per plotted point.

    ``[-samp_rate/2 + i*bin_hz for i in range(n_fft)]``, i.e. exactly what the
    vector sink's ``set_x_axis(-samp_rate/2, bin_hz)`` labels the points with.
    """
    n = int(n_fft)
    step = bin_hz(n, samp_rate)
    return [-float(samp_rate) / 2 + i * step for i in range(n)]


# ---------------------------------------------------------------- word codecs
def _wr(h, d):
    return (0x6 << 12) | ((h & 0x1F) << 5) | (d & 0x1F)


def _jp(h, e):
    return (0x7 << 12) | ((h & 0x1F) << 5) | (e & 0x1F)


def _q15(f):
    return max(-32768, min(32767, int(round(f * 32768.0)))) & 0xFFFF


def _s16(v):
    v = int(v) & 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


# ---------------------------------------------------------- the shipped tone
def tone(bin_index: int = TONE_BIN, n: int = None, phase: float = 0.0,
         amplitude: float = AMPLITUDE, n_fft: int = N_FFT):
    """The demo stimulus: an ON-BIN complex exponential at :data:`AMPLITUDE`.

    ``n`` is the sample COUNT (defaults to the size's whole-frames burst) and
    ``n_fft`` the transform size the bin index refers to. Kept identical to
    the ``.grc``'s ``iq_stim`` variable — the .grc computes the same
    expression inline so a reader can see the stimulus in the flowgraph, and
    this module is the reference the gates compare against.
    """
    if n is None:
        n = burst_of(n_fft)
    t = np.arange(n)
    z = amplitude * np.exp(1j * (2 * np.pi * bin_index * t / n_fft + phase))
    return [complex(c) for c in z.astype(np.complex64)]


# ------------------------------------------------------ the bin-order contract
def unreverse(n: int = N_FFT):
    """``slot -> natural frequency bin``: the block's BIT-REVERSED output map.

    Output slot ``k`` carries bin ``bit_reverse(k, log2 n)``. The map is an
    involution, so it is its own inverse — applying it to a streamed frame
    yields natural bin order.

    This function IS the display contract of the example. Getting it wrong
    (or skipping it) puts the tone in the wrong place on the plot while every
    other gate still passes, which is why the mutation suite attacks it
    directly.
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


def natural_spectrum(frame, n_fft: int = None):
    """One streamed frame of per-bin POWER words -> natural bin order.

    ``frame`` is the chip's output slots in emission order; the return is a
    list indexed by true frequency bin. ``n_fft`` defaults to the frame's own
    length, so a caller never has to keep the two in sync by hand.
    """
    n = int(n_fft) if n_fft else len(frame)
    if len(frame) != n:
        raise ValueError(f"a frame is {n} slots, got {len(frame)}")
    nat = [0] * n
    for slot, b in enumerate(unreverse(n)):
        nat[b] = frame[slot]
    return nat


def frames_of(stream, n_fft: int = N_FFT):
    """Complete output frames of a per-trigger stream (latency stripped)."""
    n = int(n_fft)
    out, k = [], latency_of(n)
    while k + n <= len(stream):
        out.append(list(stream[k:k + n]))
        k += n
    return out


# ------------------------------------------------------------------ reference
def reference_power(iq, n_fft: int = N_FFT):
    """The golden per-bin power stream: the VERIFIED block reference for the
    FFT, then the VERIFIED block reference for the power stage.

    Both halves are the shipped blocks' own references (``test_fft64.py`` /
    ``test_fft32.py`` / ``test_complex_to_mag_squared.py`` gate them against
    GNU Radio and against ``numpy.fft``), composed here in the chain's order.
    The chain gate then demands the CHIP match this bit for bit — which is
    what proves the placement, the corridors and the hand-off, none of which
    this composition exercises on its own.
    """
    from gr_kyttar.placement.blocks.fft_large import sdf_streaming_reference
    from gr_kyttar.placement.blocks import ComplexToMagSquaredBlock

    words = [(_q15(c.real), _q15(c.imag)) for c in iq]
    spec = sdf_streaming_reference(int(n_fft), words)
    mag = ComplexToMagSquaredBlock("r")
    re = [w[0] for w in spec]
    im = [w[1] for w in spec]
    return [_s16(int(w) & 0xFFFF)
            for w in mag.process_reference_q15(re, im)]


# ------------------------------------------------------------------- the chip
def build_chain(n_fft: int = N_FFT):
    """HAND-PLACE the two blocks, then AUTO-ROUTE and build.

    ``n_fft`` selects the transform size (64 = the headline, 32 = the smaller
    variant); both are CHIP_SCALE spine blocks pinned at their own verified
    anchor.

    Returns ``(ctrl, build_result, catalog, chip_type)``. The placement is
    pinned (see the module docstring: the auto-packer cannot place a
    chip-scale spine); everything after it — every corridor, every broker,
    the DRC and the bitstream — is the real engine.
    """
    from engine.build import BuildEngine
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from model.connection import BlockEndpoint, ChipPortEndpoint
    from model.enums import Face
    from model.placement import PlacedCell
    from ui.controller import AppController

    n = int(n_fft)
    if n not in SIZES:
        raise ValueError(f"unsupported size N={n}; this example ships "
                         f"{sorted(SIZES)}")
    block_type, _cells = SIZES[n]

    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    ctrl = AppController(catalog=cat)
    ctrl.new_project(f"Kyttar FFT{n} spectrum analyzer", CHIP_TYPE)

    fft = ctrl.place_block(block_type, 0, *FFT_ANCHOR)
    mag = ctrl.place_block("ComplexToMagSquaredBlock", 0, *MAG2_ANCHOR)
    # Pin the power stage's RESTING FACE. default_cells picks EAST, which at
    # (9, 1) points off the array and leaves the egress net unroutable; NORTH
    # points it at the x16_out port cell.
    blocks = {b.name: b for b in ctrl.project.blocks}
    blocks[mag].placement.cells = [
        PlacedCell(cell_id=0, x=MAG2_ANCHOR[0], y=MAG2_ANCHOR[1],
                   face=Face(MAG2_FACE))]

    ctrl.add_logical_connection(
        ChipPortEndpoint(chip=0, port="x16_in"),
        BlockEndpoint(block=fft, port="xi"), name="ingress")
    # ONE wire for the complex link. ``add_logical_connection`` SYNTHESISES the
    # Q-half sibling (out_q -> im) automatically, exactly as the .grc importer
    # does. Adding the Q net by hand as well creates a DUPLICATE net onto the
    # same input register: it routes, it builds, and the chain then emits a
    # frame of pure zeros (measured — every bin 0.0000, no error anywhere).
    ctrl.add_logical_connection(
        BlockEndpoint(block=fft, port="out_i"),
        BlockEndpoint(block=mag, port="re"), name="spectrum")
    ctrl.add_logical_connection(
        BlockEndpoint(block=mag, port="out"),
        ChipPortEndpoint(chip=0, port="x16_out"), name="egress")

    # NAME THE STREAM. This is not cosmetic — it is what makes the live user
    # path work at all.
    #
    # A hosted batch carries the injection landing in its request HEADER, and
    # the GR client fills that header from its OWN defaults:
    # ``data_addrs=(0, 1)``. The server only OVERRIDES those defaults with the
    # build-resolved landing when the burst names a ``stream_id`` it knows
    # (engine.port_config.stream_targets). Without a stream id the single-
    # stream path keeps the client's (0, 1) — but THIS block lands on
    # registers [1, 2] (measured: input_landings resolves entry 12, hop 26,
    # data_addrs [1, 2]).
    #
    # The visible symptom of the mismatch is precise and worth recording,
    # because it looks like a DSP bug rather than an addressing one: I goes to
    # register 0 and Q to register 1, so the block receives the REAL part in
    # its xi register and NOTHING in xq. A real-valued input has a conjugate-
    # symmetric spectrum, so the tone's energy splits evenly between bin b and
    # bin N-b at a quarter of the power each — measured live at N=64: two 6635
    # peaks (bins 11 and 53) instead of one 26539 peak at bin 11. Every gate
    # that only asks "is there a peak" still passes.
    for conn in ctrl.project.connections:
        if conn.name == "ingress":
            conn.stream_id = "spectrum"
    rep = ctrl.auto_route_all({CHIP_TYPE: ct})
    if not rep.ok:
        raise RuntimeError(f"auto-route failed: {getattr(rep, 'reason', rep)}")
    bres = BuildEngine(cat, CHIP_YAML).build(ctrl.project, {CHIP_TYPE: ct})
    if not bres.ok:
        raise RuntimeError(
            "build failed: " + "; ".join(str(e) for e in bres.errors[:5]))
    return ctrl, bres, cat, ct


def run_chain(build_result, iq):
    """Drive the burst per-sample on real simKYT; return the signed power
    words in emission order (one per input sample)."""
    import simkyt

    lin = next(iter(build_result.chips[0].input_landings.values()))
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(build_result.words(0))
    out = []
    for c in iq:
        chip.queue_words_physical("x16_in", [
            _wr(lin["hop"], lin["data_addrs"][0]), _q15(c.real),
            _wr(lin["hop"], lin["data_addrs"][1]), _q15(c.imag),
            _jp(lin["hop"], lin["entry"])])
        idle = 0
        for _ in range(400_000):
            chip.run(max_events=64)
            got = chip.read_port_words_timed("x16_out")
            if got:
                idle = 0
                out.extend(_s16(w) for w, _d, _t in got)
            else:
                idle += 1
            if idle > 300:
                break
    return out


# ----------------------------------------------------------------------- main
def demo_size(n_fft: int) -> bool:
    """Build, run and CHECK one transform size end to end. Returns pass/fail."""
    n = int(n_fft)
    block_type, _cells = SIZES[n]
    print(f"=== N = {n} ({block_type}) " + "=" * (46 - len(block_type)))
    print(f"1. hand-place {block_type} + power stage -> AUTO-ROUTE -> build ...")
    ctrl, bres, _cat, _ct = build_chain(n)
    used = sum(c.cell_count for c in bres.chips.values())
    print(f"   build OK — {used}/120 cells "
          f"({len(ctrl.project.blocks)} blocks + routing)")

    iq = tone(n_fft=n)
    print(f"2. drive {len(iq)} complex samples (amplitude {AMPLITUDE} tone at "
          f"bin {TONE_BIN}) through the placed chain on real simKYT ...")
    got = run_chain(bres, iq)
    exp = reference_power(iq, n)
    exact = got == exp
    print(f"   chip: {len(got)}/{len(exp)} power words, bit-exact vs the "
          f"composed block references: {exact}")

    frames = frames_of(got, n)
    print(f"3. un-reverse each frame's bins and locate the peak "
          f"({len(frames)} whole frames) ...")
    peaks, ok_bins = [], True
    for f, frame in enumerate(frames):
        slot = int(np.argmax(frame))
        nat = natural_spectrum(frame, n)
        peak_bin = int(np.argmax(nat))
        others = [v for b, v in enumerate(nat) if b != TONE_BIN]
        peaks.append((slot, peak_bin, nat[TONE_BIN], max(others)))
        hit = (peak_bin == TONE_BIN and nat[TONE_BIN] > COHERENT_MIN
               and max(others) < LEAKAGE_MAX)
        ok_bins = ok_bins and hit
        print(f"   frame {f}: emitted at SLOT {slot} -> natural BIN "
              f"{peak_bin} (want {TONE_BIN}); peak power "
              f"{nat[TONE_BIN]/32768.0:.4f}, next bin "
              f"{max(others)/32768.0:.4f}")

    rev = unreverse(n)
    order_ok = (rev[peaks[0][0]] == TONE_BIN
                and rev.index(TONE_BIN) == peaks[0][0])
    print(f"   bit-reversal map: slot {peaks[0][0]} <-> bin {TONE_BIN} "
          f"{'CONFIRMED' if order_ok else 'WRONG'}")

    # WHAT FREQUENCY the spike is on. A bin index is dimensionless; at the
    # declared SAMP_RATE bin k of N is k*fs/N Hz, and the shipped .grc plots
    # that axis directly (centred, so -fs/2 .. +fs/2 in bin_hz steps).
    nat0 = natural_spectrum(frames[0], n)
    centred = centred_spectrum(nat0, n)
    axis = axis_hz(n)
    i = int(np.argmax(centred))
    hz = bin_to_hz(TONE_BIN, n)
    hz_ok = abs(axis[i] - hz) < 1e-9
    print(f"4. bin -> Hz at samp_rate {SAMP_RATE:.0f}: {bin_hz(n):.0f} Hz per "
          f"bin, so bin {TONE_BIN} = {hz:.0f} Hz")
    print(f"   peak on the plotted axis: point {i} = {axis[i]:.0f} Hz "
          f"{'MATCHES' if hz_ok else 'MISMATCH'}")

    ok = exact and ok_bins and order_ok and hz_ok and len(frames) == FRAMES
    print("   RESULT:", f"SPECTRUM ON CHIP — the tone lands in its true bin "
          f"after un-reversal ({hz:.0f} Hz), bit-exact vs the block references"
          if ok else "MISMATCH")
    return ok


def main():
    # Headline first, then the smaller variant.
    results = {n: demo_size(n) for n in (64, 32)}
    print()
    print("OVERALL:", " ".join(f"N={n}:{'PASS' if v else 'FAIL'}"
                               for n, v in results.items()))
    if not all(results.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
