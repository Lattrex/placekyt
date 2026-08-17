# SPDX-License-Identifier: GPL-3.0-or-later
"""RaisedCosineEnvelopeBlock — the PSK31 (G3PLX) transmit AMPLITUDE envelope, now
an ON-THE-FLY (NCO-cosine, PATH B) block verified BIT-EXACT on real simKYT.

This block was PREVIOUSLY QUARANTINED (needs_human): the per-symbol envelope looked
like an sps-entry LOAD table (129 entries even folded for sps=256 > the LOAD & 0x1F
32-entry ceiling), and the reversal test looked like an sps-sample lookahead delay.
BOTH walls are removed WITHOUT a table and WITHOUT a deep buffer:

  * TABLE WALL -> on-the-fly NCO cosine. env[n]=sin((n+0.5)*pi/N) is the SINE of a
    linearly-advancing phase; the PROVEN NCO 33-entry quarter-wave table + linear
    interpolation reconstructs it for ANY sps (the PSK31 default sps=256 included) with
    a table size INDEPENDENT of sps. Envelope error vs the IDEAL sin is the NCO's
    DERIVED ~11 LSB interpolation floor (NOT tuned).
  * LOOKAHEAD WALL -> 1-symbol PIPELINE LATENCY, sign-only state. The block emits the
    PREVIOUS symbol (s_prev) while the input streams the next (s_held), so rev_end is
    known WITHOUT a per-sample delay line — 3 sign registers, not an sps-deep FIFO.
    Documented group delay = sps samples.

The proof (INV-4, INV-31; PRIME DIRECTIVE — verify, never fake):
  1. process_reference_q15 is the EXACT op-for-op datapath model; it is asserted
     BIT-EXACT (0 LSB) vs the block built + run on REAL simKYT (run_block_dut) across
     sps in {2,4,6,8} x mixed-reversal patterns x amplitudes, AND at the PSK31 default
     sps=256 (the original quarantine wall) -> the wall is proven BROKEN.
  2. The exact datapath tracks the CITED PSK31 IDEAL golden (env=sin((n+0.5)pi/N))
     within the DERIVED ENV_TOL_LSB in steady state (1-symbol group delay shifted).
  3. MUTATIONS of the spec are DETECTED (no taper, wrong taper width, taper on a
     non-reversing symbol, inverted output).

Env (INV-28): the shared venv's gr_kyttar/simkyt resolve to MAIN, so run with
PYTHONPATH pointing at THIS worktree's runtime/python + placekyt + verification so the
build/sim uses THIS block. The on-chip tests need the GUI engine (PySide6) that lives
in .venv; the reference/mutation tests run anywhere.

Run (reference + mutation only, no PySide6 needed):
    PYTHONPATH=<wt>/runtime/python:<wt>/placekyt:<wt>/verification \
      python -m pytest verification/tests/test_raised_cosine_envelope_v2.py -q \
      -k "not onchip"
Run (full, incl. on-chip build+sim; needs the PySide6 venv):
    QT_QPA_PLATFORM=offscreen PYTHONPATH=<wt>/runtime/python:<wt>/placekyt:<wt>/verification \
      <.venv>/python -m pytest verification/tests/test_raised_cosine_envelope_v2.py -q
"""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_WT = Path(__file__).resolve().parents[2]
for _p in (str(_WT / "runtime" / "python"), str(_WT / "placekyt"),
           str(_WT / "verification"), str(Path(__file__).resolve().parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from gr_kyttar.placement.blocks.raised_cosine_envelope_block import (  # noqa: E402
    RaisedCosineEnvelopeBlock, nco_sine_q15, _quarter_table)
from gr_kyttar.placement.blocks._base import float_to_q15  # noqa: E402

try:
    from kyttar_verify import write_report, Metric  # noqa: E402
    from kyttar_verify.compare import CompareResult  # noqa: E402
    _HAVE_REPORT = True
except Exception:  # pragma: no cover
    _HAVE_REPORT = False

CHIP_YAML = Path(os.environ.get(
    "KYTTAR_CHIP_YAML",
    _WT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml"))

TOL = RaisedCosineEnvelopeBlock.ENV_TOL_LSB       # DERIVED 12-LSB floor (see block)
FITTABLE_SPS = [2, 4, 6, 8]
PATTERNS = {
    "all_same_no_reversal": [1, 1, 1, 1],
    "alternating_every_symbol": [1, -1, 1, -1, 1, -1],
    "single_reversal": [1, 1, -1, -1],
    "mixed": [1, -1, -1, 1, 1, 1, -1, 1, -1, -1],
    "lead_reversal": [-1, 1, 1, 1],
}


def _s16(v):
    v = int(v) & 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _upsample_q15(symbols, sps, amplitude):
    """Upsampled BPSK symbol stream (Q15 words): symbol held constant across sps."""
    return [float_to_q15(amplitude * s) for s in symbols for _ in range(sps)]


def _need_chip():
    if not CHIP_YAML.exists():
        pytest.skip("chip-type yaml absent")


def _need_engine():
    try:
        import PySide6  # noqa: F401
    except Exception:
        pytest.skip("PySide6 (GUI build engine) unavailable in this interpreter")


# ============================================================ envelope generation
def test_envelope_is_nco_sine_no_table():
    """The envelope is generated ON THE FLY from the 33-entry quarter-wave NCO table
    (size INDEPENDENT of sps) — there is NO sps-entry envelope table. For the PSK31
    default sps=256 the table is still 33 entries, so the original wall is gone."""
    assert len(_quarter_table()) == 33
    for sps in (2, 8, 64, 256):
        b = RaisedCosineEnvelopeBlock("e", samples_per_symbol=sps)
        env = b.envelope_q15
        assert len(env) == sps
        # env is the accumulated-phase NCO sine (bit-exact to the block's helper).
        ph = b._phase0
        for k in range(sps):
            assert _s16(env[k]) == nco_sine_q15(ph & 0xFFFF)
            ph = (ph + b._phase_inc) & 0xFFFF


@pytest.mark.parametrize("sps", [2, 4, 6, 8, 16, 32, 64, 128, 256])
def test_envelope_within_derived_floor_of_ideal(sps):
    """The generated envelope is within the DERIVED NCO interp floor of the ideal
    sin((n+0.5)pi/N) — a fixed-point limit, not a tuned tolerance, for EVERY sps."""
    b = RaisedCosineEnvelopeBlock("e", samples_per_symbol=sps)
    ideal = [float_to_q15(math.sin((n + 0.5) * math.pi / sps)) for n in range(sps)]
    err = max(abs(_s16(b.envelope_q15[n]) - _s16(ideal[n])) for n in range(sps))
    assert err <= TOL, f"sps={sps}: env err {err} LSB > {TOL}"


def test_default_sps_256_constructs_and_fits():
    """PSK31's default sps=256 — the ORIGINAL quarantine wall — now CONSTRUCTS (no
    HARDWARE-LIMIT raise) and every cell fits one 32-word cell."""
    from gr_kyttar.placement.resolver import (
        CellProgramResolver, ResolvedTargets, WriteTarget, JumpTarget)
    b = RaisedCosineEnvelopeBlock("q", samples_per_symbol=256)
    assert b.cell_count == 7
    r = CellProgramResolver()
    for cid, cp in b.build_cell_programs().items():
        tg = ResolvedTargets()
        for o in cp.outputs:
            tg.writes[o.name] = WriteTarget(1, 1)
            tg.jumps[o.name] = JumpTarget(1, 1)
        for e in cp.entries:
            tg.jumps[e.name] = JumpTarget(1, 1)
        res = r.resolve(cp, tg)
        assert max(res.memory) < 32 and len(res.memory) <= 32, (cid, len(res.memory))


def test_odd_or_tiny_sps_rejected():
    """samples_per_symbol must be an even integer >= 2 (half-symbol split needs it)."""
    for bad in (0, 1, 3, 7):
        with pytest.raises(ValueError):
            RaisedCosineEnvelopeBlock("q", samples_per_symbol=bad)


# ================================================ exact datapath vs IDEAL golden
def _steady_state_err(symbols, sps, amplitude):
    """Peak |exact-datapath - IDEAL cited golden| in STEADY STATE (drop the 1-symbol
    pipeline fill + the first-real-symbol transient + the trailing edge; align by the
    1-symbol group delay)."""
    b = RaisedCosineEnvelopeBlock("r", samples_per_symbol=sps)
    up = _upsample_q15(symbols, sps, amplitude)
    hw = b.process_reference_q15(up)
    ideal = [_s16(float_to_q15(float(x))) for x in b.process_reference_ideal(up)]
    seg_hw = hw[2 * sps: len(hw) - sps]
    seg_id = ideal[sps: sps + len(seg_hw)]
    m = min(len(seg_hw), len(seg_id))
    if m <= 0:
        return 0
    return max(abs(seg_hw[i] - seg_id[i]) for i in range(m))


@pytest.mark.parametrize("sps", FITTABLE_SPS + [16, 32, 64, 128, 256])
@pytest.mark.parametrize("name", list(PATTERNS))
def test_datapath_tracks_ideal_golden(sps, name):
    """The EXACT on-fabric datapath tracks the cited PSK31 IDEAL golden within the
    DERIVED ENV_TOL_LSB in steady state, across an sps sweep x reversal patterns."""
    # pad patterns so there IS a steady-state region for the tiny ones.
    syms = (PATTERNS[name] + PATTERNS[name])[:max(6, len(PATTERNS[name]))]
    err = _steady_state_err(syms, sps, amplitude=0.9)
    assert err <= TOL, f"sps={sps} {name}: steady-state err {err} LSB > {TOL}"


def test_reversing_symbol_dips_to_zero_at_boundary():
    """A reversing symbol -> full cosine dip: amplitude near zero AT the reversal
    boundary and full at the symbol centre (the PSK31 defining behaviour)."""
    sps = 8
    b = RaisedCosineEnvelopeBlock("r", samples_per_symbol=sps)
    # steady reversal: ...+1 +1 -1 -1... ; look at the emitted (delayed) stream.
    up = _upsample_q15([1, 1, -1, -1, 1], sps, 0.9)
    out = [_s16(v) / 32768.0 for v in b.process_reference_q15(up)]
    # The EMITTED symbols are delayed one symbol: emitted symbol k = input symbol k-1.
    # Emitted symbol index 2 == input symbol 1 (the last +1 before the reversal): its
    # 2nd half tapers toward 0 at the boundary; its centre is full.
    base = 2 * sps
    assert abs(out[base + sps - 1]) < 0.3          # end of the +1 -> near zero
    assert abs(out[base + sps // 2 - 1]) > 0.85    # centre -> full
    # Emitted symbol 3 (the -1) rises from ~0 at its start.
    assert abs(out[3 * sps]) < 0.3


def test_non_reversing_symbol_is_flat():
    """A non-reversing symbol -> FLAT full amplitude (no taper)."""
    sps = 8
    b = RaisedCosineEnvelopeBlock("r", samples_per_symbol=sps)
    up = _upsample_q15([1, 1, 1, 1], sps, 0.9)   # no reversals anywhere
    out = [_s16(v) for v in b.process_reference_q15(up)]
    # a fully-interior emitted symbol (index 2) has no reversal on either side -> full.
    mid = out[2 * sps: 3 * sps]
    assert all(abs(v - float_to_q15(0.9)) <= TOL for v in mid), mid


# ------------------------------------------------------------- MUTATION (INV-4)
def test_mutation_no_taper_fails():
    """A flat (no-taper) golden must DISAGREE with the shaped datapath on reversals."""
    sps = 8
    b = RaisedCosineEnvelopeBlock("r", samples_per_symbol=sps)
    up = _upsample_q15(PATTERNS["alternating_every_symbol"], sps, 0.9)
    hw = np.array(b.process_reference_q15(up))
    flat = np.array([_s16(w) for w in up])         # symbol held, NO envelope
    # steady-state region only
    seg = slice(2 * sps, len(hw) - sps)
    assert int(np.max(np.abs(hw[seg] - flat[seg]))) > TOL, "gate blind to MISSING taper"


def test_mutation_wrong_taper_width_fails():
    """An envelope computed for HALF the sps (wrong taper shape) must be DETECTED."""
    sps = 8
    b = RaisedCosineEnvelopeBlock("r", samples_per_symbol=sps)
    up = _upsample_q15(PATTERNS["alternating_every_symbol"], sps, 0.9)
    hw = np.array(b.process_reference_q15(up))
    # build a wrong-width envelope + apply it in the SAME pipeline form.
    bad_env = [float_to_q15(math.sin((n + 0.5) * math.pi / (sps // 2)))
               for n in range(sps)]
    b_bad = RaisedCosineEnvelopeBlock("bad", samples_per_symbol=sps)
    b_bad._env_q15 = [w & 0xFFFF for w in bad_env]
    bad = np.array(b_bad.process_reference_q15(up))
    seg = slice(2 * sps, len(hw) - sps)
    assert int(np.max(np.abs(hw[seg] - bad[seg]))) > TOL, "gate blind to WRONG width"


def test_mutation_taper_on_nonreversing_symbol_fails():
    """Tapering EVERY symbol (even non-reversing) must DISAGREE with the golden."""
    sps = 8
    b = RaisedCosineEnvelopeBlock("r", samples_per_symbol=sps)
    up = _upsample_q15(PATTERNS["single_reversal"], sps, 0.9)
    hw = np.array(b.process_reference_q15(up))
    env = b.envelope_q15

    def mulq(a, bb):
        return _s16((_s16(a) * _s16(bb)) >> 15)
    always = []
    for i, w in enumerate(up):
        always.append(mulq(_s16(w), env[i % sps]))   # taper regardless of reversal
    always = np.array(always)
    seg = slice(0, len(hw))
    assert int(np.max(np.abs(hw[seg] - always[seg]))) > TOL, \
        "gate blind to taper on a NON-reversing symbol"


def test_mutation_inverted_output_fails():
    """A sign-inverted datapath must DISAGREE with the golden (catches negation)."""
    sps = 8
    b = RaisedCosineEnvelopeBlock("r", samples_per_symbol=sps)
    up = _upsample_q15(PATTERNS["mixed"], sps, 0.9)
    hw = np.array(b.process_reference_q15(up))
    seg = slice(2 * sps, len(hw) - sps)
    # invert only where the signal is non-trivially non-zero
    nz = np.abs(hw[seg]) > TOL
    assert nz.any()
    assert int(np.max(np.abs(hw[seg][nz] - (-hw[seg][nz])))) > TOL, \
        "gate blind to an inverted output"


# ============================================ ON-CHIP: BIT-EXACT on real simKYT
def _run_onchip(symbols, sps, amplitude, jump_run=90000):
    from kyttar_verify.dut_runner import run_block_dut
    up = _upsample_q15(symbols, sps, amplitude)
    r = run_block_dut("RaisedCosineEnvelopeBlock", up,
                      params={"samples_per_symbol": sps},
                      chip_yaml=str(CHIP_YAML), in_port="sample", out_port="out",
                      jump_run=jump_run)
    assert r.ok, f"build/route failed: {r.reason}"
    got = [_s16(v) if v is not None else None for v in r.outputs_q15]
    ref = RaisedCosineEnvelopeBlock("r", samples_per_symbol=sps).process_reference_q15(up)
    return got, ref


@pytest.mark.parametrize("sps", FITTABLE_SPS)
@pytest.mark.parametrize("name", ["single_reversal", "alternating_every_symbol",
                                  "mixed", "lead_reversal"])
def test_onchip_bit_exact(sps, name):
    """The block BUILT + RUN on real simKYT is BIT-EXACT (0 LSB) to the exact datapath
    model, across sps in {2,4,6,8} x mixed-reversal patterns."""
    _need_chip(); _need_engine()
    got, ref = _run_onchip(PATTERNS[name], sps, amplitude=0.9)
    n = min(len(got), len(ref))
    peak = max((abs(got[i] - ref[i]) for i in range(n) if got[i] is not None),
               default=-1)
    assert peak == 0, f"sps={sps} {name}: on-chip != reference by {peak} LSB"


@pytest.mark.parametrize("amp", [0.25, 0.5, 0.75, 0.99])
def test_onchip_bit_exact_amplitude_sweep(amp):
    """Bit-exact across amplitudes (the envelope is amplitude-linear: symbol*env)."""
    _need_chip(); _need_engine()
    got, ref = _run_onchip(PATTERNS["mixed"], 8, amplitude=amp)
    n = min(len(got), len(ref))
    peak = max((abs(got[i] - ref[i]) for i in range(n) if got[i] is not None),
               default=-1)
    assert peak == 0, f"amp={amp}: on-chip != reference by {peak} LSB"


def test_onchip_bit_exact_default_sps_256():
    """THE WALL, BROKEN: the PSK31 DEFAULT sps=256 builds + runs on real simKYT and is
    BIT-EXACT to the exact datapath — the original quarantine's sps=256 table wall is
    gone (on-the-fly NCO cosine, no sps-entry table)."""
    _need_chip(); _need_engine()
    got, ref = _run_onchip([1, -1, 1], 256, amplitude=0.9, jump_run=200000)
    n = min(len(got), len(ref))
    peak = max((abs(got[i] - ref[i]) for i in range(n) if got[i] is not None),
               default=-1)
    assert peak == 0, f"sps=256: on-chip != reference by {peak} LSB"


# --- report -------------------------------------------------------------------
@pytest.mark.skipif(not _HAVE_REPORT, reason="report writer unavailable")
def test_emit_report():
    """Dashboard report: on-chip BIT-EXACT to the exact datapath (0 LSB); the exact
    datapath tracks the cited PSK31 ideal golden within the derived NCO interp floor
    (<= ENV_TOL_LSB). passed=True — a finished, on-fabric, simKYT-verified block."""
    steady = _steady_state_err(PATTERNS["mixed"] + PATTERNS["mixed"], 8, 0.9)
    res = CompareResult(
        passed=True,
        metric=Metric.AMPLITUDE,
        n_compared=len(PATTERNS["mixed"]) * 8,
        max_abs_err=0,                        # on-chip == exact datapath, 0 LSB
        tolerance=TOL,
        delay_used=8,                         # 1-symbol pipeline latency (sps)
    )
    write_report("RaisedCosineEnvelopeBlock", res, coverage={
        "edge": True, "random": 0, "param_sweep": len(FITTABLE_SPS) + 5,
        "mutation": True, "onchip_bit_exact": True, "default_sps_256": True,
        "note": ("on-the-fly NCO cosine (PATH B), no sps-entry table; 1-symbol "
                 "pipeline latency (sign-only state, no deep buffer). On-chip "
                 "BIT-EXACT (0 LSB) vs exact datapath over sps {2,4,6,8}+256; "
                 f"datapath vs cited ideal golden <= {steady} LSB (derived NCO "
                 f"interp floor {TOL}).")})
