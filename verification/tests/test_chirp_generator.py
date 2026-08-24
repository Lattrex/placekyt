# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify ChirpGeneratorBlock — the CSS cyclic-shifted linear up-chirp modulator.

There is NO stock GNU Radio streaming counterpart (manifest: Python golden). The
golden is a numpy/integer reference built with the SAME integer phase arithmetic
as the chip: a DOUBLE phase accumulator in 16-bit wrap semantics (freq word
initialized per symbol from ``s``, incremented by the chirp-rate word each
sample; the 16-bit wraparound IS the mod-BW cyclic shift) feeding the verified
NCO quarter-wave-table + linear-interpolation channel path (transcribed from the
NCO test collateral via the shared NCOBlock reference methods). Gates:

  * BIT-EXACT DUT vs the integer golden — every symbol value at n=32 (m=32 and
    m=4), all 8 symbols at n=64/m=8 in ONE stream, spot symbols at n=128/m=128
    and n=256/m=256, multi-symbol streams (phase continuity ACROSS symbols).
  * The WRAP path specifically: a symbol near m-1 crosses +BW/2 mid-symbol; a
    saturate-instead-of-wrap mutant golden must FAIL.
  * PINNED CONVENTION — phase CARRIES across symbols (never reset): gated by a
    reset-per-symbol mutant golden that must FAIL (with n even and m | n each
    symbol advances the carried phase by exactly pi, so the mutant differs by a
    sign flip on odd symbols).
  * SNR vs the IDEAL float chirp (same recursion in exact real arithmetic),
    asserted against the derived table floor and REPORTED. Note the grid
    alignment: for n <= 128 every phase is a multiple of 512 (the 33-entry
    table grid) so the interp error is ~0 (measured ~91 dB); n = 256 exercises
    the interpolation (half-grid phases) and shows the true interp-limited
    floor. Both are reported.
  * INV-4 mutations proven to FAIL: wrong chirp-rate word, frequency-init
    off-by-one, conjugated output, +1 sample delay, empty, no-wrap clamp,
    phase reset.
  * SATURATED == per-sample (bespoke INV-19/20 gate): the whole symbol burst
    queued back-to-back (queue_words_physical, one continuous run) must produce
    the IDENTICAL word stream — this exercises the block's always-on arbiter
    serialize-LOCK (a symbol's n samples drain before the next symbol enters)
    and its self-paced emit->sweep return kick.
  * 8/8 D4 orientation invariance on the FULL burst (run_block_dut_rate drains
    every word; the main orientation gate's run_block_dut keeps only the last
    word per trigger, too weak for a 1:n block).
  * The return kick survives the exit patchers in an ABUTTED (unrouted-exit)
    chain — the regression for the build's backward-internal-jump preservation.
  * INV-17 fan-out budget on the complex emit cell.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \\
      <venv>/python -m pytest verification/tests/test_chirp_generator.py -q
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PLACEKYT = Path(__file__).resolve().parents[2] / "placekyt"
_VERIFY = Path(__file__).resolve().parents[1]
_RUNTIME = Path(__file__).resolve().parents[2] / "runtime" / "python"
for p in (str(_PLACEKYT), str(_VERIFY), str(_RUNTIME)):
    if p not in sys.path:
        sys.path.insert(0, p)

from kyttar_verify import write_session_report  # noqa: E402

from kyttar_verify import run_block_dut_rate, D4_ORIENTATIONS  # noqa: E402
from gr_kyttar.placement.blocks.chirp_generator_block import (  # noqa: E402
    ChirpGeneratorBlock)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_CHIP_OK = os.path.exists(CHIP_YAML)
pytestmark = pytest.mark.skipif(not _CHIP_OK, reason="chip yaml absent")


def _s16(v):
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def _ref_words(n, m, syms, amplitude=1.0):
    ref = ChirpGeneratorBlock("ref", n=n, m=m, amplitude=amplitude
                              ).process_reference_q15(syms)
    return [w for pair in ref for w in pair]


def _run(syms, n, m, amplitude=1.0, orient=None, jump_run=2500000):
    dut = run_block_dut_rate(
        "ChirpGeneratorBlock", list(syms),
        params={"n": n, "m": m, "amplitude": amplitude},
        chip_yaml=CHIP_YAML, in_port="s", out_port="yi",
        orient=orient, jump_run=jump_run, drain_run=8000)
    assert dut.ok, dut.reason
    return dut


# --- structure / rate ---------------------------------------------------------

def test_rate_is_one_to_n():
    """One input symbol word -> exactly n complex (2n word) outputs, per trigger."""
    dut = _run([0, 3, 1], n=16, m=4)
    assert [len(g) for g in dut.per_trigger] == [32, 32, 32], \
        f"per-trigger burst lengths wrong: {[len(g) for g in dut.per_trigger]}"


def test_param_validation_raises():
    """Non-power-of-two / out-of-range n and m RAISE (never silently clamp)."""
    for bad in (dict(n=1), dict(n=12), dict(n=131072), dict(n=16, m=3),
                dict(n=16, m=32), dict(n=16, m=1)):
        with pytest.raises(ValueError):
            ChirpGeneratorBlock("bad", **bad)


# --- bit-exact vs the integer golden ------------------------------------------

@pytest.mark.parametrize("n,m,syms", [
    (32, 32, list(range(32))),          # EVERY symbol value, one stream
    (32, 4, list(range(4)) * 2),        # every symbol value, m < n, repeats
    (16, 4, [0, 3, 1, 2, 3, 0]),        # multi-symbol continuity
    (64, 8, list(range(8))),            # every symbol value at n=64
    (128, 128, [0, 1, 64, 127, 100]),   # spot symbols at the classic n=m=128
    (256, 256, [0, 255, 128]),          # n=256: interpolation (off-grid) path
])
def test_bitexact_vs_integer_golden(n, m, syms):
    """The on-chip output is BIT-EXACT vs the double-accumulator integer golden
    for every driven symbol, across the whole multi-symbol stream (delay=0 is
    pinned by direct positional equality — INV-2)."""
    dut = _run(syms, n=n, m=m, jump_run=max(2500000, 40000 * n))
    exp = _ref_words(n, m, syms)
    assert len(dut.outputs_q15) == len(exp), (
        f"word count {len(dut.outputs_q15)} != {len(exp)}")
    assert dut.outputs_q15 == exp, (
        "bit mismatch at word "
        f"{next(i for i in range(len(exp)) if dut.outputs_q15[i] != exp[i])}")


def test_amplitude_param():
    """The amplitude param scales the emit MULQ — bit-exact vs the golden built
    with the SAME amplitude."""
    dut = _run([1, 2], n=16, m=4, amplitude=0.5)
    assert dut.outputs_q15 == _ref_words(16, 4, [1, 2], amplitude=0.5)


# --- the wrap / cyclic-shift path ---------------------------------------------

def _golden_variant(n, m, syms, *, rate_mul=1, s_off=0, clamp_wrap=False,
                    reset_phase=False):
    """Golden with an injectable defect — the mutation machinery. The default
    (no defect) reproduces process_reference_q15 exactly (asserted below)."""
    blk = ChirpGeneratorBlock("g", n=n, m=m)
    tbl = blk._quarter_table()
    amp = _s16(blk._amp_q15)
    rate = (65536 // n) * rate_mul
    out = []
    phase = 0
    for s in syms:
        if reset_phase:
            phase = 0
        freq = ((((int(s) + s_off) << blk._shift) & 0xFFFF) + 0x8000) & 0xFFFF
        for _ in range(n):
            cos = blk._channel_q15((phase + 16384) & 0xFFFF, tbl, amp) & 0xFFFF
            sin = blk._channel_q15(phase & 0xFFFF, tbl, amp) & 0xFFFF
            out.extend((cos, sin))
            phase = (phase + freq) & 0xFFFF
            if clamp_wrap:
                # the DEFECT: saturate the frequency word at +BW/2 instead of
                # letting the 16-bit wraparound perform the cyclic shift.
                nf = freq + rate
                freq = 0x7FFF if (freq < 0x8000 and nf >= 0x8000) else nf & 0xFFFF
            else:
                freq = (freq + rate) & 0xFFFF
    return out


def test_golden_variant_identity():
    """The defect-injectable golden with NO defect == process_reference_q15
    (so a variant's FAILURE isolates the injected defect, nothing else)."""
    assert _golden_variant(16, 4, [0, 3, 1]) == _ref_words(16, 4, [0, 3, 1])


def test_wrap_midsymbol_symbol_near_m_minus_1():
    """A symbol near m-1 starts just below +BW/2 and must WRAP within its first
    few samples — the cyclic-shift path. The DUT matches the wrapping golden
    bit-exactly, and the freq word provably crosses the wrap mid-symbol."""
    n = m = 64
    s = 63
    # freq word trajectory: fw(63) = 63*1024 + 0x8000 (mod 2^16) = 0x7C00 —
    # 1 step below +BW/2; it wraps on sample k = (0x8000-0x7C00)/rate = 1.
    fw0 = ((s * (65536 // m)) + 0x8000) & 0xFFFF
    rate = 65536 // n
    k_wrap = (0x8000 - fw0) // rate if fw0 < 0x8000 else 0
    assert 0 < k_wrap < n, "test premise: the wrap must occur mid-symbol"
    dut = _run([s], n=n, m=m)
    assert dut.outputs_q15 == _ref_words(n, m, [s])


def test_mutation_no_wrap_clamp_fails():
    """A golden that CLAMPS the frequency word at +BW/2 (no cyclic shift) must
    DISAGREE with the DUT on a wrapping symbol — proof the wrap path is
    genuinely exercised and gated."""
    n = m = 64
    dut = _run([63], n=n, m=m)
    assert dut.outputs_q15 != _golden_variant(n, m, [63], clamp_wrap=True), \
        "gate failed to detect a missing-wrap (clamped) chirp!"


# --- phase continuity (the pinned convention) ---------------------------------

def test_phase_carries_across_symbols():
    """PINNED: the phase accumulator CARRIES across symbol boundaries. With n
    even and m | n each symbol advances the carried phase by exactly pi, so the
    carry model and the reset model differ by a sign flip on odd symbols —
    the DUT must match CARRY and must NOT match RESET."""
    n, m = 32, 8
    syms = [2, 5, 7, 0]
    dut = _run(syms, n=n, m=m)
    carry = _golden_variant(n, m, syms)
    reset = _golden_variant(n, m, syms, reset_phase=True)
    assert carry != reset, "test premise: carry and reset models must differ"
    assert dut.outputs_q15 == carry, "DUT does not carry phase across symbols"
    assert dut.outputs_q15 != reset, \
        "gate failed: DUT indistinguishable from a reset-per-symbol chirp"


# --- SNR vs the ideal float chirp ---------------------------------------------

def _snr_db(n, m, syms):
    import numpy as np
    blk = ChirpGeneratorBlock("s", n=n, m=m)
    ref = blk.process_reference_q15(syms)
    got = np.asarray([complex(_s16(a), _s16(b)) for a, b in ref]) / 32768.0
    ideal = blk.ideal_chirp(syms) * (32767 / 32768.0)  # the amp=1.0 Q15 gain
    err = got - ideal
    return float(10 * np.log10(np.sum(np.abs(ideal) ** 2)
                               / np.sum(np.abs(err) ** 2)))


def test_snr_vs_ideal_float_chirp():
    """SNR of the (chip-bit-exact) integer chirp vs the IDEAL float chirp.
    n <= 128 keeps every phase on the 33-entry table grid (multiples of 512):
    measured ~91 dB. n = 256 hits half-grid phases and shows the true
    linear-interpolation floor of the quarter-wave table (measured 73.4 dB,
    consistent with the NCO's ~11 LSB worst-case bound). Both asserted against
    DERIVED floors (not tuned): grid >= 85 dB, interpolated >= 60 dB."""
    snr_grid = _snr_db(128, 128, [0, 1, 64, 127, 100])
    snr_interp = _snr_db(256, 256, [0, 255, 128, 37])
    print(f"\nSNR vs ideal chirp: n=128 (grid) {snr_grid:.1f} dB, "
          f"n=256 (interpolated) {snr_interp:.1f} dB")
    assert snr_grid >= 85.0, f"grid-aligned SNR collapsed: {snr_grid:.1f} dB"
    assert snr_interp >= 60.0, f"interpolated SNR collapsed: {snr_interp:.1f} dB"


def test_onchip_bitexact_covers_the_interpolated_regime():
    """n=256 (the off-grid/interpolated regime) is ALSO chip-bit-exact — the
    SNR numbers above therefore describe the on-chip waveform, not just the
    model."""
    dut = _run([255], n=256, m=256, jump_run=10000000)
    assert dut.outputs_q15 == _ref_words(256, 256, [255])


# --- INV-4 mutations (each must FAIL) -----------------------------------------

def _dut_16_4():
    return _run([0, 3, 1], n=16, m=4).outputs_q15


def test_mutation_wrong_chirp_rate_fails():
    assert _dut_16_4() != _golden_variant(16, 4, [0, 3, 1], rate_mul=2), \
        "gate failed to detect a wrong chirp-rate word!"


def test_mutation_frequency_init_off_by_one_fails():
    assert _dut_16_4() != _golden_variant(16, 4, [0, 3, 1], s_off=1), \
        "gate failed to detect an off-by-one frequency init!"


def test_mutation_conjugated_output_fails():
    dut = _dut_16_4()
    conj = list(dut)
    for i in range(1, len(conj), 2):           # negate the Q rail
        conj[i] = (0x10000 - conj[i]) & 0xFFFF
    assert conj != _ref_words(16, 4, [0, 3, 1]), \
        "gate failed to detect a conjugated chirp!"


def test_mutation_one_sample_delay_fails():
    dut = _dut_16_4()
    delayed = [0, 0] + dut[:-2]
    assert delayed != _ref_words(16, 4, [0, 3, 1]), \
        "gate failed to detect a +1 complex-sample delay!"


def test_mutation_empty_fails():
    assert [] != _ref_words(16, 4, [0, 3, 1])


# --- saturated == per-sample (bespoke INV-19/20 gate) -------------------------

def _drive_saturated(n, m, syms):
    """Queue the WHOLE symbol burst back-to-back (queue_words_physical) with
    ONE continuous bounded run — the real streaming condition — and return the
    interleaved [yi, yq, ...] egress. Mirrors test_pipeline_saturation's
    _drive_fm_saturated (the real-in/complex-out bespoke driver)."""
    import simkyt  # noqa: PLC0415
    from PySide6.QtWidgets import QApplication  # noqa: PLC0415
    from engine.catalog import BlockCatalog  # noqa: PLC0415
    from engine.io.chip_type_io import load_chip_type  # noqa: PLC0415
    from engine.build import BuildEngine  # noqa: PLC0415
    from engine.registry import ChipTypeRegistry  # noqa: PLC0415
    from engine.port_config import stream_targets  # noqa: PLC0415
    from ui.controller import AppController  # noqa: PLC0415
    from model.connection import BlockEndpoint, ChipPortEndpoint  # noqa: PLC0415

    QApplication.instance() or QApplication([])
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    key = getattr(ct, "name", None) or "kyttar_10x12"
    ctrl = AppController(catalog=cat)
    ctrl.new_project("chirpsat", key)
    blk = ctrl.place_block("ChirpGeneratorBlock", 0, 1, 1,
                           library="lattrex.official", params={"n": n, "m": m})
    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=blk, port="s"), name="in_s")
    ctrl.add_logical_connection(BlockEndpoint(block=blk, port="yi"),
                                ChipPortEndpoint(chip=0, port="x16_out"),
                                name="out_y")
    assert ctrl.auto_route_all({key: ct}).ok
    for conn in ctrl.project.connections:
        s = getattr(conn, "source", None)
        if s is not None and getattr(s, "port", None) == "x16_in":
            conn.stream_id = "tx"
    bres = BuildEngine(cat, CHIP_YAML).build(ctrl.project, {key: ct})
    assert bres.ok, getattr(bres, "errors", None)
    reg = ChipTypeRegistry()
    reg.register_file(CHIP_YAML)
    tg = stream_targets(ctrl.project, reg, cat, 0, build_result=bres)["tx"]
    entry, hop, a0 = tg["entry_addr"], tg["hop_count"], tg["data_addrs"][0]
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(bres.words(0))
    stream = []
    for s in syms:
        stream += [(0x6 << 12) | ((hop & 0x1F) << 5) | (a0 & 0x1F),
                   int(s) & 0xFFFF,
                   (0x7 << 12) | ((hop & 0x1F) << 5) | (entry & 0x1F)]
    chip.queue_words_physical("x16_in", stream)
    # HARNESS SAFETY (INV-19): bound the run; a livelock/deadlock must surface
    # as a clean failure, never a spin.
    res = chip.run(max_events=max(1000000, 60000 * n * len(syms)))
    assert res.get("completed", False), (
        f"saturated run did not complete: {res.get('stop_reason')} after "
        f"{res.get('events_processed')} events — the serialize-LOCK failed")
    return [int(v) & 0xFFFF for (v, _d, _t) in
            chip.read_port_words_timed("x16_out")]


def test_saturated_equals_per_sample():
    """SATURATED drive (whole burst queued, no inter-symbol quiescence) emits
    the IDENTICAL bit-exact stream as the per-sample-verified golden, with the
    exact 2*n*len(syms) word count (no dropped/duplicated samples) — the
    block's always-on arbiter serialize-LOCK + self-paced return kick under
    the real streaming condition."""
    n, m = 16, 4
    syms = [0, 3, 1, 2, 3, 0]
    out = _drive_saturated(n, m, syms)
    exp = _ref_words(n, m, syms)
    assert len(out) == len(exp) == 2 * n * len(syms), (
        f"saturated produced {len(out)} words for {len(syms)} symbols "
        f"(expected {2 * n * len(syms)}) — dropped/duplicated samples")
    assert out == exp, (
        "saturated stream diverges from the per-sample golden at word "
        f"{next(i for i in range(len(exp)) if out[i] != exp[i])}")


# --- 8/8 D4 orientation invariance (full burst) -------------------------------

def test_orientation_invariant_full_burst():
    """All 8 D4 orientations produce the IDENTICAL full 2n-word burst per
    symbol (the main orientation gate's run_block_dut keeps only the last word
    per trigger — too weak for a 1:n block, so the full-burst 8-D4 gate lives
    here; the RationalResampler/R2Butterfly precedent)."""
    n, m = 16, 4
    syms = [1, 3, 0]
    exp = _ref_words(n, m, syms)
    for orient in D4_ORIENTATIONS:
        dut = _run(syms, n=n, m=m, orient=list(orient))
        assert dut.outputs_q15 == exp, \
            f"orientation {orient or 'identity'} diverges from the golden"


# --- the return kick vs the exit patchers (abutted chain regression) ----------

def test_kick_survives_abutted_exit_defaulting():
    """The emit cell's backward return kick (its LAST jump: @1 into the sweep
    cell's iternext entry) must SURVIVE the build's exit patch passes when the
    block's output abuts a downstream block — the exact case _set_cell_hop1
    used to rewrite every exit-cell jump to the consumer's entry (which
    silently killed the iteration: 1 sample per symbol). Asserts (a) the built
    kick jump's entry == the sweep cell's resolved iternext address, and (b)
    the chained data actually FLOWS at the full 1:n rate."""
    import simkyt  # noqa: PLC0415
    from PySide6.QtWidgets import QApplication  # noqa: PLC0415
    from engine.catalog import BlockCatalog  # noqa: PLC0415
    from engine.io.chip_type_io import load_chip_type  # noqa: PLC0415
    from engine.build import BuildEngine  # noqa: PLC0415
    from ui.controller import AppController  # noqa: PLC0415
    from model.connection import BlockEndpoint, ChipPortEndpoint  # noqa: PLC0415
    from gr_kyttar.placement.resolver import CellProgramResolver  # noqa: PLC0415

    QApplication.instance() or QApplication([])
    n, m = 16, 4
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    key = getattr(ct, "name", None) or "kyttar_10x12"
    ctrl = AppController(catalog=cat)
    ctrl.new_project("abut", key)
    blk = ctrl.place_block("ChirpGeneratorBlock", 0, 1, 1,
                           library="lattrex.official", params={"n": n, "m": m})
    g = ctrl.place_block("ComplexGainBlock", 0, 3, 1,
                         library="lattrex.official", params={"gain": 0.5})
    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=blk, port="s"),
                                name="in_blk")
    ctrl.add_logical_connection(BlockEndpoint(block=blk, port="yi"),
                                BlockEndpoint(block=g, port="xi"), name="c2g")
    ctrl.add_logical_connection(BlockEndpoint(block=g, port="yi"),
                                ChipPortEndpoint(chip=0, port="x16_out"),
                                name="g_out")
    assert ctrl.auto_route_all({key: ct}).ok
    bres = BuildEngine(cat, CHIP_YAML).build(ctrl.project, {key: ct})
    assert bres.ok, getattr(bres, "errors", None)

    # (a) the built kick jump: the HIGHEST jump in emit (2,1) targets iternext.
    cb = ChirpGeneratorBlock("probe", n=n, m=m)
    entries = CellProgramResolver().compute_entry_addresses(
        cb.build_cell_programs()["phase"])
    it_entry = entries["iternext"]
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(bres.words(0))
    emit_id = chip.cell_id_at(2, 1)
    jumps = []
    for a in range(32):
        w = chip.read_cell_memory(emit_id, a) & 0xFFFF
        if (w >> 12) == 0x7:
            jumps.append((a, w))
    assert jumps, "emit cell has no JUMPs?"
    _ka, kw = max(jumps)
    assert (kw & 0x1F) == it_entry, (
        f"kick jump entry {kw & 0x1F} != iternext {it_entry} — the exit "
        f"patchers clobbered the return kick")
    assert ((kw >> 5) & 0x1F) == 30, "kick jump hop is not the @1 abutment"

    # (b) data flows at the full rate through the abutted consumer.
    entry, ins = cat.resolved_io("ChirpGeneratorBlock", {"n": n, "m": m})
    port = ct.port("x16_in")
    in_conn = next(c for c in ctrl.project.connections if c.name == "in_blk")
    route = getattr(in_conn, "route", None)
    lc = ctrl.project.block(blk).placement.cells[0]
    dist = (len(route) if route
            else abs(lc.x - port.cell_x) + abs(lc.y - port.cell_y) + 1)
    hop = 31 - dist
    got = []
    for s in (0, 3):
        chip.inject_data_physical([s], target_hop_cnt=hop, target_addr=ins[0])
        chip.run(max_events=6000)
        chip.inject_jump_physical(target_hop_cnt=hop, entry_addr=entry)
        chip.run(max_events=800000)
        while chip.output_available("x16_out"):
            w = chip.read_port_i16("x16_out").view("uint16").tolist()
            got.extend(int(x) & 0xFFFF for x in w)
            chip.release_output_ack("x16_out")
            chip.run(max_events=8000)
    assert len(got) == 2 * n * 2, (
        f"abutted chain emitted {len(got)} words for 2 symbols "
        f"(expected {2 * n * 2}) — the iteration died downstream")
    # Value sanity: the chain output is the chirp through ComplexGain(0.5)'s
    # documented gain/4 + saturating <<2 path (multiples of 4, within 4 LSB of
    # half the chirp) — full gain-path exactness is ComplexGain's own gate.
    ref = _ref_words(n, m, [0, 3])
    for k2 in range(0, len(got), 2):
        assert abs(_s16(got[k2]) - _s16(ref[k2]) / 2) <= 4
        assert abs(_s16(got[k2 + 1]) - _s16(ref[k2 + 1]) / 2) <= 4


# --- INV-17: emit-cell fan-out budget -----------------------------------------

def test_emit_cell_fanout_budget():
    """The complex output cell leaves >= 1 free word for the build's fan-out
    re-sequencing (INV-17): resolved instructions + data + state + the 2 input
    registers + R31 must total <= 31."""
    from gr_kyttar.placement.resolver import CellProgramResolver  # noqa: PLC0415
    cp = ChirpGeneratorBlock("b", n=128, m=128).build_cell_programs()["emit"]
    r = CellProgramResolver()
    instr = r.count_instructions(cp)
    used = (1 + max([d.address for d in cp.data]
                    + list(r.compute_state_registers(cp).values()))) + instr
    assert used <= 30, f"emit cell too full for the fan-out JUMP: {used} words"


# --- dashboard report ---------------------------------------------------------

def test_emit_report():
    n, m = 32, 32
    syms = list(range(m))
    dut = _run(syms, n=n, m=m, jump_run=4000000)
    exp = _ref_words(n, m, syms)
    assert dut.outputs_q15 == exp
    report = {
        "metric": "exact", "n_compared": len(exp), "max_abs_err": 0,
        "tolerance": 0, "bit_errors": 0, "delay_used": 0,
        "snr_db_grid_n128": round(_snr_db(128, 128, [0, 1, 64, 127, 100]), 1),
        "snr_db_interp_n256": round(_snr_db(256, 256, [0, 255, 128, 37]), 1),
        "coverage": {"param_sweep": 6, "bit_exact": True, "mutation": True,
                     "saturated": True, "orientation_8d4_full_burst": True,
                     "wrap_midsymbol": True, "phase_carry_pinned": True},
    }
    write_session_report("ChirpGeneratorBlock", report)
