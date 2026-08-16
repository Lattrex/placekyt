# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless LMS equalizer demo — the CONSTELLATION SNAP, end to end.

QPSK symbols smeared by the [1, 0.35, -0.15] multipath channel (the .grc's
``iq_stim``) drive the placed decision-directed LMS equalizer
(LMSEqualizerBlock, 5 taps, mu 0.03) per-sample on real simKYT — the
adaptation runs ON THE CHIP, sample by sample, within the burst.

Proof structure: the chip's complex output must be BIT-EXACT to the block's
``process_reference`` (whose GR scale-covariant equivalence is proven in
verification/tests/test_lms_equalizer.py), the converged tail must decide
EVERY transmitted symbol correctly (tail BER 0 through the multipath), and
the tail clusters must sit on the +-0.7071 decision constellation — the
"snap" the GRC constellation display shows.

Run::

    PYTHONPATH=runtime/python:placekyt QT_QPA_PLATFORM=offscreen \
        .venv/bin/python examples/lms_equalizer/lms_eq_demo.py
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
GRC_PATH = HERE / "lms_equalizer.grc"
KYT_PATH = HERE / "lms_equalizer.kyt"

BURST_LEN = 600
NUM_TAPS = 5
MU = 0.03
TAIL = 300          # symbols after convergence (mu=0.03 converges in ~200)

# == the .grc's iq_stim, kept literally in sync. Channel + seeded AWGN
# (sigma 0.035/component — a realistic fuzzy input cloud; the noiseless ISI
# superposition is a bare 64-point lattice). The final complex64 cast mirrors
# the WIRE truth: GRC's vector_source_c carries complex64, so the
# float32-rounded values are what the chip actually receives (a float64
# reference here would drift the q15 words by 1 LSB at rounding boundaries
# and the adaptive chain would amplify the mismatch). ==
NOISE_SIGMA = 0.035
_QPSK = np.array([1 + 1j, 1 - 1j, -1 + 1j, -1 - 1j])
SYMS = _QPSK[np.random.default_rng(7).integers(0, 4, BURST_LEN)]
IQ_STIM = [complex(c) for c in
           (np.convolve(SYMS, [1.0, 0.35, -0.15])[:BURST_LEN] / 2.4
            + NOISE_SIGMA
            * (np.random.default_rng(11).standard_normal(BURST_LEN)
               + 1j * np.random.default_rng(12).standard_normal(BURST_LEN))
            ).astype(np.complex64)]


def _wr(h, d):
    return (0x6 << 12) | ((h & 0x1F) << 5) | (d & 0x1F)


def _jp(h, e):
    return (0x7 << 12) | ((h & 0x1F) << 5) | (e & 0x1F)


def _q15(f):
    return max(-32768, min(32767, int(round(f * 32768.0)))) & 0xFFFF


def _s16(v):
    v = int(v) & 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def import_and_pnr():
    from engine.build import BuildEngine
    from engine.catalog import BlockCatalog
    from engine.grc_import import import_grc
    from engine.io.chip_type_io import load_chip_type
    from ui.controller import AppController

    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    last_err = None
    # The auto-P&R sweep is stochastic (seeded CP-SAT packings) — retry; a
    # genuine failure still raises with the last attempt's named reason.
    for _attempt in range(3):
        res = import_grc(str(GRC_PATH), cat)
        if not res.ok:
            raise RuntimeError(f"GRC import failed: unknown blocks {res.unknown}")
        ctrl = AppController(catalog=cat)
        ctrl.project = res.project
        rep = ctrl.auto_pnr({res.project.chip_type: ct})
        if not rep.ok:
            last_err = f"auto_pnr failed: {rep.reason}"
            continue
        bres = BuildEngine(cat, CHIP_YAML).build(res.project,
                                                 {res.project.chip_type: ct})
        if not bres.ok:
            last_err = ("build failed: "
                        + "; ".join(str(e) for e in bres.errors[:5]))
            continue
        return res.project, bres, cat, ct
    raise RuntimeError(last_err or "auto_pnr failed")


def run_chain(build_result, iq):
    """Per-sample drive (the LMS adapts sample by sample — its contract)."""
    import simkyt

    lands = build_result.chips[0].input_landings
    lin = next(iter(lands.values()))
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(build_result.words(0))
    out = []
    for c in iq:
        chip.queue_words_physical("x16_in", [
            _wr(lin["hop"], lin["data_addrs"][0]), _q15(c.real),
            _wr(lin["hop"], lin["data_addrs"][1]), _q15(c.imag),
            _jp(lin["hop"], lin["entry"])])
        idle = 0
        for _ in range(200000):
            chip.run(max_events=64)
            got = chip.read_port_words_timed("x16_out")
            if got:
                idle = 0
                out.extend(_s16(w) for w, _d, _t in got)
            else:
                idle += 1
            if idle > 200:
                break
    return out


def reference_output(iq):
    from gr_kyttar.placement.blocks import LMSEqualizerBlock

    ref = LMSEqualizerBlock("r", num_taps=NUM_TAPS, step_size=MU)
    pairs = [(_q15(c.real), _q15(c.imag)) for c in iq]
    return [_s16(w) for w in ref.process_reference(pairs)]


def _dec(z):
    return (np.real(z) >= 0).astype(int) * 2 + (np.imag(z) >= 0).astype(int)


def main():
    print("1. import lms_equalizer.grc -> auto place-and-route -> build ...")
    project, bres, cat, ct = import_and_pnr()
    used = sum(c.cell_count for c in bres.chips.values())
    print(f"   build OK — {used}/120 cells, {len(project.blocks)} blocks")
    print(f"2. drive {BURST_LEN} multipath QPSK symbols per-sample; the LMS "
          "adapts ON CHIP ...")
    got = run_chain(bres, IQ_STIM)
    exp = reference_output(IQ_STIM)
    exact = got == exp
    print(f"   chip: {len(got)}/{len(exp)} words, bit-exact vs reference: "
          f"{exact}")
    y = np.array([complex(got[2 * k], got[2 * k + 1]) / 32768.0
                  for k in range(len(got) // 2)])
    tail = slice(BURST_LEN - TAIL, len(y))
    errs = int(np.sum(_dec(y[tail]) != _dec(SYMS[tail])))
    rad = np.abs(np.abs(y[tail]) - 1.0)
    print(f"   converged tail ({TAIL} syms): symbol errors {errs} (BER "
          f"{errs / TAIL:.4f}), mean |y| deviation from the unit "
          f"constellation {rad.mean():.3f}")
    ok = exact and errs == 0 and rad.mean() < 0.1
    print("RESULT:", "CONSTELLATION SNAP — multipath equalized ON CHIP, "
          "tail BER 0" if ok else "MISMATCH")
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
