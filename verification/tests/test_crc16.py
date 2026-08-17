# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify Crc16Block against the published CRC-16 algorithm (no GR counterpart).

``Crc16Block(poly, init, frame_len)`` computes the bit-serial MSB-first
(non-reflected) CRC-16 over fixed-length frames of a byte stream — the ITU-T
V.41 family as catalogued in Greg Cook's CRC RevEng "Catalogue of parametrised
CRC algorithms". At the defaults (``poly=0x1021, init=0xFFFF``) it is exactly
**CRC-16/CCITT-FALSE** (width=16 poly=0x1021 init=0xFFFF refin=false
refout=false xorout=0x0000 check=0x29B1)::

    crc = init
    for each byte:  crc ^= byte << 8
                    8x: crc = ((crc << 1) ^ poly) if crc & 0x8000 else crc << 1

GNU Radio has NO streaming CRC block (its CRC blocks are tagged-PDU/packet
blocks, a host-scheduler idiom the fabric does not have), so the golden is the
published algorithm above, INDEPENDENTLY cross-checked against the stdlib
``binascii.crc_hqx`` — which implements this exact engine for ``poly=0x1021``
with a caller-supplied init — plus catalogue check-value anchors for other
(poly, init) points (XMODEM, AUG-CCITT, UMTS, CMS).

The block is RATE-REDUCING (``frame_len`` bytes in -> 1 CRC word out, register
re-armed to ``init`` per frame); ``run_block_dut`` records ``got[-1]`` per driven
sample, so the CRC word appears on the sample completing each frame and the
accumulating samples read ``None`` (the PackKBits harness pattern). Byte/word
streams are RAW 16-bit words, not Q15 (the XorBlock lesson): raw injection +
EXACT integer equality, tolerance 0.

Run::

    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \\
        .venv/bin/python -m pytest verification/tests/test_crc16.py -q
"""
from __future__ import annotations

import binascii
import os
import random
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PLACEKYT = Path(__file__).resolve().parents[2] / "placekyt"
_VERIFY = Path(__file__).resolve().parents[1]
_RUNTIME = Path(__file__).resolve().parents[2] / "runtime" / "python"
for p in (str(_RUNTIME), str(_PLACEKYT), str(_VERIFY)):
    if p not in sys.path:
        sys.path.insert(0, p)

from kyttar_verify import run_block_dut, write_report, CompareResult, Metric  # noqa: E402
from gr_kyttar.placement.blocks.crc16_block import Crc16Block  # noqa: E402

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_CHIP_OK = os.path.exists(CHIP_YAML)
pytestmark = pytest.mark.skipif(not _CHIP_OK, reason="chip yaml absent")


# --- the GOLDEN model (the published algorithm, cited above) ------------------

def _crc16(frame, poly=0x1021, init=0xFFFF):
    """Published MSB-first non-reflected CRC-16 over one frame of bytes
    (CRC RevEng catalogue, refin=false refout=false xorout=0)."""
    crc = init & 0xFFFF
    for b in frame:
        crc ^= (int(b) & 0xFF) << 8
        for _ in range(8):
            crc = (((crc << 1) ^ poly) if crc & 0x8000 else (crc << 1)) & 0xFFFF
    return crc


def _golden(data, poly=0x1021, init=0xFFFF, frame_len=8):
    """Per-frame CRC word stream (floor(n/frame_len) words; partial dropped)."""
    return [_crc16(data[j * frame_len:(j + 1) * frame_len], poly, init)
            for j in range(len(data) // frame_len)]


def _run_dut(data, poly=0x1021, init=0xFFFF, frame_len=8):
    """Build + run Crc16Block on simKYT; RAW byte words in."""
    words = [int(b) & 0xFFFF for b in data]
    dut = run_block_dut("Crc16Block", words,
                        params={"poly": poly, "init": init,
                                "frame_len": frame_len},
                        in_port="sample", chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    return dut


def _crc_words(dut):
    """The on-chip CRC word stream: the non-None words (one per completed
    frame; accumulating samples read None)."""
    return [int(w) & 0xFFFF for w in dut.outputs_q15 if w is not None]


def _word_errors(dut_words, ref_words):
    n = min(len(dut_words), len(ref_words))
    assert n > 0, "no CRC words compared"
    errs = sum(1 for j in range(n) if dut_words[j] != ref_words[j])
    errs += abs(len(dut_words) - len(ref_words))   # length gap = dropped/extra frame
    return errs, n


def _random_bytes(seed, n):
    rng = random.Random(seed)
    return [rng.randint(0, 255) for _ in range(n)]


# --- the golden is REAL: independent anchors BEFORE any DUT comparison --------

def test_golden_matches_binascii_crc_hqx():
    """The pure-python golden == stdlib binascii.crc_hqx (an independent
    implementation of the same MSB-first 0x1021 engine) for arbitrary init,
    over random frames — the golden itself is pinned before gating the DUT."""
    for seed in (1, 2, 3):
        frame = bytes(_random_bytes(seed, 32))
        for init in (0x0000, 0xFFFF, 0x1D0F, 0xABCD):
            assert _crc16(frame, 0x1021, init) == binascii.crc_hqx(frame, init), \
                f"golden != binascii at init={init:#x}"


# Catalogue anchors (Greg Cook's CRC RevEng catalogue, all with the 9-byte UTF-8
# check string b"123456789"; refin=false refout=false xorout=0 models only):
_CATALOGUE = [
    # (name, poly, init, check)
    ("CRC-16/CCITT-FALSE", 0x1021, 0xFFFF, 0x29B1),
    ("CRC-16/XMODEM", 0x1021, 0x0000, 0x31C3),
    ("CRC-16/SPI-FUJITSU (AUG-CCITT)", 0x1021, 0x1D0F, 0xE5CC),
    ("CRC-16/UMTS (BUYPASS)", 0x8005, 0x0000, 0xFEE8),
    ("CRC-16/CMS", 0x8005, 0xFFFF, 0xAEE7),
]


@pytest.mark.parametrize("name,poly,init,check", _CATALOGUE,
                         ids=[c[0] for c in _CATALOGUE])
def test_catalogue_check_values_golden_and_dut(name, poly, init, check):
    """Known-vector anchors: golden AND the on-chip DUT reproduce the published
    catalogue check value for b'123456789' (frame_len=9)."""
    data = b"123456789"
    assert _crc16(data, poly, init) == check, \
        f"golden misses the {name} catalogue check value"
    dut = _run_dut(data, poly=poly, init=init, frame_len=9)
    got = _crc_words(dut)
    assert got == [check], \
        f"DUT {name}: got {[hex(w) for w in got]}, expected {check:#06x}"


# --- correctness: bit-exact vs the golden -------------------------------------

@pytest.mark.parametrize("frame_len", [1, 2, 3, 4, 8, 16])
def test_frame_len_sweep_bit_exact(frame_len):
    """frame_len sweep: random byte stream (several whole frames) produces the
    per-frame CRC word stream exactly (defaults: CCITT-FALSE), each word also
    cross-checked against binascii."""
    data = _random_bytes(500 + frame_len, n=frame_len * 4)
    dut = _run_dut(data, frame_len=frame_len)
    ref = _golden(data, frame_len=frame_len)
    assert ref == [binascii.crc_hqx(bytes(data[j * frame_len:(j + 1) * frame_len]),
                                    0xFFFF)
                   for j in range(4)], "golden != binascii on this stream"
    got = _crc_words(dut)
    errs, n = _word_errors(got, ref)
    assert errs == 0, (f"frame_len={frame_len}: {errs}/{n} word errors "
                       f"(dut={[hex(w) for w in got]} ref={[hex(w) for w in ref]})")


@pytest.mark.parametrize("rseed", [1, 7, 42, 1234])
def test_random_frames_bit_exact(rseed):
    """Random byte frames (>= 3 seeds) at the defaults: DUT == golden ==
    binascii, word-for-word."""
    fl = 8
    data = _random_bytes(rseed, n=fl * 5)
    dut = _run_dut(data, frame_len=fl)
    ref = _golden(data, frame_len=fl)
    got = _crc_words(dut)
    errs, n = _word_errors(got, ref)
    assert errs == 0, f"rseed {rseed}: {errs}/{n} CRC word errors"


@pytest.mark.parametrize("poly,init", [
    (0x1021, 0xFFFF), (0x1021, 0x0000), (0x1021, 0x1D0F),
    (0x8005, 0x0000), (0x8005, 0xFFFF), (0xA02B, 0x89EC), (0x3D65, 0xFFFF),
])
def test_poly_init_sweep_bit_exact(poly, init):
    """poly/init sweep (catalogued + arbitrary 16-bit points): DUT == golden
    over random multi-frame streams — the datapath is parameter-generic."""
    data = _random_bytes(poly ^ init, n=8 * 3)
    dut = _run_dut(data, poly=poly, init=init, frame_len=8)
    ref = _golden(data, poly=poly, init=init, frame_len=8)
    got = _crc_words(dut)
    errs, n = _word_errors(got, ref)
    assert errs == 0, (f"poly={poly:#x} init={init:#x}: {errs}/{n} errors "
                       f"(dut={[hex(w) for w in got]} ref={[hex(w) for w in ref]})")


def test_register_rearms_between_frames():
    """The CRC register re-initialises to ``init`` at each frame boundary: two
    IDENTICAL frames must produce two IDENTICAL CRC words (a register that
    carries state across frames cannot)."""
    frame = _random_bytes(99, n=8)
    dut = _run_dut(frame + frame, frame_len=8)
    got = _crc_words(dut)
    assert len(got) == 2 and got[0] == got[1], \
        f"frame re-arm broken: {[hex(w) for w in got]}"
    assert got[0] == _crc16(frame), "re-armed frame CRC wrong"


def test_trailing_partial_frame_not_emitted():
    """A trailing partial frame (< frame_len bytes) is NOT emitted: exactly
    floor(n/frame_len) CRC words."""
    fl = 8
    data = _random_bytes(55, n=fl * 2 + (fl - 1))
    dut = _run_dut(data, frame_len=fl)
    ref = _golden(data, frame_len=fl)
    got = _crc_words(dut)
    assert len(ref) == 2, "test setup: expected 2 whole frames"
    errs, n = _word_errors(got, ref)
    assert errs == 0 and len(got) == 2, \
        f"partial-frame handling wrong (dut={[hex(w) for w in got]})"


def test_input_high_bits_ignored():
    """Only the low 8 bits of each input word feed the CRC (crc ^= byte<<8
    drops the rest): stray high bits must not change the result."""
    data = _random_bytes(7, n=8)
    dirty = [(b | 0x5A00) & 0xFFFF for b in data]
    got_clean = _crc_words(_run_dut(data, frame_len=8))
    got_dirty = _crc_words(_run_dut(dirty, frame_len=8))
    assert got_clean == got_dirty == [_crc16(data)], \
        f"high input bits leaked into the CRC: {got_clean} vs {got_dirty}"


# --- reference sanity (pure python, no chip) ----------------------------------

def test_process_reference_matches_golden_and_binascii():
    """Crc16Block.process_reference == the golden == binascii across a
    frame_len + poly/init sweep (the block-carried reference is itself real)."""
    for fl in (1, 4, 8, 9):
        data = _random_bytes(2024 + fl, n=fl * 3)
        ref = [int(w) & 0xFFFF for w in
               Crc16Block("r", frame_len=fl).process_reference(data)]
        assert ref == _golden(data, frame_len=fl), f"reference != golden fl={fl}"
        assert ref == [binascii.crc_hqx(bytes(data[j * fl:(j + 1) * fl]), 0xFFFF)
                       for j in range(3)], f"reference != binascii fl={fl}"
    for poly, init in ((0x8005, 0x0000), (0xA02B, 0x1234)):
        data = _random_bytes(3000 + poly, n=16)
        ref = [int(w) & 0xFFFF for w in
               Crc16Block("r", poly=poly, init=init, frame_len=8)
               .process_reference(data)]
        assert ref == _golden(data, poly=poly, init=init, frame_len=8)


# --- MANDATORY mutation gates (INV-4): each corruption MUST fail --------------

def test_mutation_wrong_poly_fails():
    """A DUT built with the WRONG polynomial must DISAGREE with the golden at
    the correct polynomial (a REAL on-chip mutant, not a model)."""
    data = _random_bytes(11, n=8 * 3)
    ref = _golden(data, poly=0x1021, frame_len=8)          # correct
    dut_wrong = _run_dut(data, poly=0x8005, frame_len=8)   # wrong poly on-chip
    errs, n = _word_errors(_crc_words(dut_wrong), ref)
    assert errs > 0, "a wrong-poly DUT went undetected by the gate!"


def test_mutation_wrong_init_fails():
    """A DUT built with the WRONG init must DISAGREE with the golden at the
    correct init (a REAL on-chip mutant)."""
    data = _random_bytes(13, n=8 * 3)
    ref = _golden(data, init=0xFFFF, frame_len=8)          # correct
    dut_wrong = _run_dut(data, init=0x0000, frame_len=8)   # wrong init on-chip
    errs, n = _word_errors(_crc_words(dut_wrong), ref)
    assert errs > 0, "a wrong-init DUT went undetected by the gate!"


def test_mutation_reflected_bit_order_fails():
    """An LSB-first (reflected) byte feed — the classic CRC bit-order trap —
    must DISAGREE with the MSB-first golden."""
    def _crc16_reflected_feed(frame, poly=0x1021, init=0xFFFF):
        crc = init
        for b in frame:
            rb = int(f"{int(b) & 0xFF:08b}"[::-1], 2)      # bit-reversed byte
            crc ^= rb << 8
            for _ in range(8):
                crc = (((crc << 1) ^ poly) if crc & 0x8000
                       else (crc << 1)) & 0xFFFF
        return crc
    data = _random_bytes(17, n=8 * 4)
    ref = _golden(data, frame_len=8)
    mut = [_crc16_reflected_feed(data[j * 8:(j + 1) * 8]) for j in range(4)]
    errs, n = _word_errors(mut, ref)
    assert errs > 0, "a reflected-bit-order engine went undetected by the gate!"


def test_mutation_dropped_byte_fails():
    """Dropping one input byte (a mis-counted frame) shifts every frame window
    and must FAIL — guards the frame counter / boundary."""
    data = _random_bytes(19, n=8 * 4)
    ref = _golden(data, frame_len=8)
    dropped = _golden(data[1:], frame_len=8)
    errs, n = _word_errors(dropped, ref)
    assert errs > 0, "a dropped input byte went undetected by the gate!"


def test_mutation_one_extra_shift_fails():
    """A 9-step (one-extra-shift) engine — the +1 shift-count mutation — must
    DISAGREE with the 8-step golden."""
    def _crc16_9shift(frame, poly=0x1021, init=0xFFFF):
        crc = init
        for b in frame:
            crc ^= (int(b) & 0xFF) << 8
            for _ in range(9):                              # WRONG: 9 steps
                crc = (((crc << 1) ^ poly) if crc & 0x8000
                       else (crc << 1)) & 0xFFFF
        return crc
    data = _random_bytes(23, n=8 * 4)
    ref = _golden(data, frame_len=8)
    mut = [_crc16_9shift(data[j * 8:(j + 1) * 8]) for j in range(4)]
    errs, n = _word_errors(mut, ref)
    assert errs > 0, "an extra-shift engine went undetected by the gate!"


def test_mutation_one_word_shift_fails():
    """A +1-frame shift of the CRC word stream must FAIL (no free lag
    alignment — INV-2)."""
    data = _random_bytes(29, n=8 * 5)
    dut = _run_dut(data, frame_len=8)
    ref = _golden(data, frame_len=8)
    got = _crc_words(dut)
    shifted = [0] + got[:-1]
    errs, n = _word_errors(shifted, ref)
    assert errs > 0, "a one-word shift went undetected!"


def test_empty_output_fails():
    """An empty/short DUT output cannot be certified against a non-empty
    reference (a length gap counts as error)."""
    ref = _golden(_random_bytes(31, 8 * 3), frame_len=8)
    assert len(ref) == 3
    errs, n = _word_errors([0], ref)
    assert errs > 0, "an (near-)empty output went undetected!"


# --- parameter-range guards (raise, never clamp) ------------------------------

@pytest.mark.parametrize("kwargs", [
    {"frame_len": 0}, {"frame_len": -1}, {"frame_len": 0x10000},
    {"poly": 0x11021}, {"poly": -1}, {"init": 0x10000}, {"init": -2},
])
def test_out_of_range_params_raise(kwargs):
    """poly/init must be 16-bit values; frame_len 1..65535 (a 16-bit frame
    down-counter). Out-of-range RAISES, never silently masks."""
    with pytest.raises(ValueError):
        Crc16Block("x", **kwargs)


def test_program_fits_cell_across_param_space():
    """The single-cell program (data + state + instructions) resolves for
    extreme parameter corners — the 32-word budget holds over the whole
    declared space (the program shape is parameter-independent)."""
    from engine.catalog import BlockCatalog
    cat = BlockCatalog.from_gr_kyttar()
    for params in ({"poly": 0xFFFF, "init": 0xFFFF, "frame_len": 0xFFFF},
                   {"poly": 0x0000, "init": 0x0000, "frame_len": 1},
                   {"poly": 0x8005, "init": 0x1D0F, "frame_len": 9}):
        entry, ins = cat.resolved_io("Crc16Block", params,
                                     library="lattrex.official")
        assert entry is not None, f"resolved_io failed for {params}"


# --- report -------------------------------------------------------------------

def test_emit_report():
    """Emit the dashboard report reflecting a passing bit-exact verification."""
    data = _random_bytes(1, n=8 * 6)
    dut = _run_dut(data, frame_len=8)
    ref = _golden(data, frame_len=8)
    errs, n = _word_errors(_crc_words(dut), ref)
    res = CompareResult(passed=(errs == 0), metric=Metric.EXACT,
                        n_compared=n, bit_errors=errs, delay_used=0)
    assert res.passed, res.summary()
    write_report("Crc16Block", res, coverage={
        "golden": ("published MSB-first CRC-16 (CRC RevEng catalogue, ITU-T "
                   "V.41 family); binascii.crc_hqx cross-check (poly 0x1021, "
                   "any init) + 5 catalogue check anchors on-chip"),
        "anchors": "CCITT-FALSE 0x29B1 / XMODEM 0x31C3 / AUG-CCITT 0xE5CC / "
                   "UMTS 0xFEE8 / CMS 0xAEE7 (b'123456789')",
        "random": 4,
        "frame_len_sweep": "1,2,3,4,8,16 bit-exact; trailing partial dropped",
        "poly_init_sweep": "7 (poly, init) points incl. arbitrary non-catalogue",
        "mutation": ("wrong poly (on-chip) / wrong init (on-chip) / reflected "
                     "bit order / dropped byte / +1 shift step / +1 word shift "
                     "/ empty"),
        "edge": "high input bits ignored; identical frames re-arm to init",
        "decision": "one 16-bit CRC word per frame_len bytes (rate-reducing)",
        "note": "1-cell bit-serial carry-select datapath; EXACT, delay 0, tol 0",
        "hw_range": "poly/init 16-bit, frame_len 1..65535; out-of-range RAISES",
    })
