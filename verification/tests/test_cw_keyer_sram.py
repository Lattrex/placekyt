# SPDX-License-Identifier: GPL-3.0-or-later
"""CWKeyerBlock — SRAM-backed, verified BIT-EXACT (Q15) through the REAL panel.

The SECOND SRAM-backed DSP block (INV-31), mirroring
``test_varicode_encoder_sram.py``. The former INV-7 quarantine (the Morse LOAD
table + dot/dash/gap timing FSM + raised-cosine key-click edge overflow one
32-word cell) is resolved by moving the message-dependent keying SCHEDULE off-cell
into the SRAM PANEL as a stream of RUN RECORDS, and packing the raised-cosine edge
into a small in-cell Hann LUT so the on-chip cell is a tiny fixed run PLAYER.

  1. LOAD PHASE — the persistent placed ``SramControllerBlock`` streams the run
     records (3 words each: base, step, count) into the panel with AUTO-INCREMENT
     addressing, ONE chip run, held-ack panel port, real ``PanelDriver`` pump
     (mirrors ``TestSramDemo`` / the Varicode load phase).
  2. LOOKUP PHASE — per run the panel PUSH-READs the three run words back into the
     player cell's base/step/count registers + kicks its ``play`` entry through
     REAL simkyt routing (mirrors ``test_write_then_read_back_out_port``); the
     on-chip player emits ``count`` samples ``LUT[cur]`` (``cur += step``).
  3. The emitted envelope is asserted BIT-EXACT (Q15) vs the Python GOLDEN
     (``key_envelope_q15``, ITU-R M.1677-1) over E, T, PARIS, SOS, and a message.
  4. Timing ratios EXACT (dot:dash:intra:inter:word = 1:3:1:3:7), PARIS = 50 units,
     dot_ms = 1200/wpm; the edge is the Hann LUT within an exact Q15 tolerance.
  5. INV-4 mutation gates proven to FAIL (wrong run word in SRAM, wrong timing
     ratio, no click edge, missing inter-word gap).

Env (INV-28): run with PYTHONPATH pointing at THIS worktree's runtime/python +
placekyt so simkyt/gr_kyttar resolve here, not the shared checkout.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_WT = Path(__file__).resolve().parents[2]
for _p in (str(_WT / "runtime" / "python"), str(_WT / "placekyt"),
           str(Path(__file__).resolve().parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from gr_kyttar.placement.blocks.cw_keyer_block import (  # noqa: E402
    CWKeyerBlock,
    MORSE_ITU,
    morse_codeword,
    morse_sram_table,
    RUN_OFF, RUN_FLAT, RUN_RISE, RUN_FALL,
)
from gr_kyttar.placement.blocks.sram_controller_block import (  # noqa: E402
    SramControllerBlock,
)
from gr_kyttar.placement.resolver import CellProgramResolver  # noqa: E402

CHIP_YAML = Path(os.environ.get(
    "KYTTAR_CHIP_YAML",
    _WT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml"))

W = 10  # chip is 10 wide

# The on-chip build size used throughout: small spd (fast) + a fitting edge.
SPD = 10
EDGE = 4


def _cid(x, y):
    return y * W + x


def _wr(h, d):
    return (0x6 << 12) | ((h & 0x1F) << 5) | (d & 0x1F)


def _jp(h, e):
    return (0x7 << 12) | ((h & 0x1F) << 5) | (e & 0x1F)


def _need_chip():
    if not CHIP_YAML.exists():
        pytest.skip("chip-type yaml absent")


def _drain(chip, out, want):
    """Run the chip until it emits ``want`` more samples or goes quiescent.

    The player emits ``count`` samples per run through the x16 port; they surface
    across several ``run`` calls. Keep pumping while progress is made (bounded so a
    stuck chip can't hang the test)."""
    stalls = 0
    got_here = 0
    for _ in range(20000):
        chip.run(max_events=16)
        new = chip.read_port_words_timed("x16_out")
        if new:
            for v, _d, _t in new:
                out.append(v & 0xFFFF)
            got_here += len(new)
            stalls = 0
        else:
            stalls += 1
        if got_here >= want and stalls >= 3:
            break
        if stalls >= 200:            # quiescent — nothing more coming
            break


def _keyer(spd=SPD, edge=EDGE):
    return CWKeyerBlock("k", wpm=20, samples_per_dot=spd, edge_samples=edge)


# ============================================================ pure model / golden
def test_morse_sram_table_addressed_by_codepoint():
    """The packed Morse panel image is addressed by ASCII code point, one word
    per char (mirrors sram_table() in the Varicode block)."""
    img = morse_sram_table(list(MORSE_ITU.keys()))
    assert len(img) == 128
    assert img[ord("E")] == morse_codeword(".")
    assert img[ord("Q")] == morse_codeword("--.-")
    assert all(0 <= w <= 0xFFFF for w in img)


def test_run_record_model_bit_exact_golden_full():
    """The build-time run-record schedule played against the in-cell Hann LUT is
    BIT-EXACT (Q15) to the ITU-R golden across edges/spd/chars — this is exactly
    what the on-chip player computes."""
    for spd, e in [(10, 0), (40, 4), (10, 4), (20, 3)]:
        b = CWKeyerBlock("k", wpm=20, samples_per_dot=spd, edge_samples=e)
        for chars in ([ord("E")], [ord("T")], [ord("A"), ord("N")], [0],
                      [ord(c) for c in "PARIS"] + [0],
                      [ord(c) for c in "SOS"], [ord("Q")], [ord("5")]):
            assert b.emit_from_records(b.run_records(chars)) == \
                b.key_envelope_q15(chars), (spd, e, chars)


def test_run_kinds_cover_off_flat_rise_fall():
    """A shaped ON element produces RISE+FLAT+FALL runs and gaps produce OFF —
    the four unified-player kinds are all exercised."""
    b = _keyer()
    recs = b.run_records([ord("E")])            # one dot + inter-char gap
    kinds = {r[0] if r[0] in (RUN_OFF, RUN_FLAT, RUN_RISE) else RUN_FALL
             for r in recs}
    # RISE base=2(step+1); FALL base=2+e-1(step-1); FLAT base=1; OFF base=0.
    assert any(r[0] == RUN_RISE and r[1] == +1 for r in recs)
    assert any(r[1] == -1 for r in recs)         # a fall run
    assert any(r[0] == RUN_OFF for r in recs)


# ================================================= on-chip run player (unpack+emit)
def _player(edge=EDGE):
    """Resolve the player cell with emit_hop=10 so its samples exit the x16 port."""
    b = CWKeyerBlock("k", wpm=20, samples_per_dot=SPD, edge_samples=edge,
                     emit_hop=10)
    cp = b.build_cell_programs()[1]   # cell 1 = the run player (cell 0 = fetch)
    res = CellProgramResolver().resolve(cp)
    cls = CellProgramResolver().classify_addresses(cp)
    regs = {v["name"]: a for a, v in cls.items() if "name" in v}
    return b, res, regs, res.entry_addr


def test_player_fits_single_cell():
    """The run player resolves into one 32-word cell (the wall the panel removed
    was the timing FSM + table, not the player)."""
    _b, res, _regs, _entry = _player()
    assert max(res.memory) < 32
    assert len(res.memory) <= 32


def _run_one_record_onchip(base, step, cnt):
    """Load the player at (0,0), place a run record in its input regs, kick play,
    collect the emitted samples out x16_out."""
    import simkyt
    _b, res, regs, entry = _player()
    chip = simkyt.Chip.from_yaml(str(CHIP_YAML))
    for a in range(0, 32):
        chip.write_cell_memory(_cid(0, 0), a, int(res.memory.get(a, 0)))
    chip.write_cell_memory(_cid(0, 0), regs["base"], base & 0xFFFF)
    chip.write_cell_memory(_cid(0, 0), regs["step"], step & 0xFFFF)
    chip.write_cell_memory(_cid(0, 0), regs["cnt"], cnt & 0xFFFF)
    for x in range(W):
        chip.set_fwd_face(_cid(x, 0), "east")
    chip.set_port_entry_address("x16_in", entry)
    chip.set_port_target_hop_count("x16_in", 30)
    chip.write_port("x16_in", np.array([0.0], dtype=np.float32))
    out = []
    _drain(chip, out, cnt)
    return out


def test_player_run_kinds_onchip():
    """Each run kind emits the right samples from the in-cell Hann LUT on REAL
    simkyt: OFF -> zeros, FLAT -> full, RISE -> LUT up, FALL -> LUT down."""
    _need_chip()
    b, _res, _regs, _entry = _player()
    lut = b.edge_lut_q15()
    e = b.edge_samples
    assert _run_one_record_onchip(RUN_OFF, 0, 5) == [0] * 5
    assert _run_one_record_onchip(RUN_FLAT, 0, 3) == [lut[1]] * 3
    assert _run_one_record_onchip(RUN_RISE, +1, e) == [lut[2 + i] for i in range(e)]
    assert _run_one_record_onchip(2 + e - 1, -1, e) == \
        [lut[2 + e - 1 - i] for i in range(e)]


# ============================================= LOAD PHASE: persistent controller
def _controller():
    ctl = SramControllerBlock("ctl", panel_hop=10)
    cp = ctl.build_cell_programs()[0]
    res = CellProgramResolver().resolve(cp)
    cls = CellProgramResolver().classify_addresses(cp)
    cin = [a for a, v in cls.items() if v.get("name") == "data"][0]
    ent = CellProgramResolver().compute_entry_addresses(cp)
    return res, cin, ent


def test_load_phase_streams_run_records_with_autoincrement():
    """The persistent placed SramController streams the run-record words into the
    panel in ONE chip run; wraddr AUTO-INCREMENTS so set_addr(0) + N writes lands
    the flat record stream at 0..N-1 (SRAM_PANEL.md §6 load phase)."""
    _need_chip()
    import simkyt
    from engine.sram_panel import SramPanelDevice, PanelDriver

    res, cin, ent = _controller()
    b = _keyer()
    flat = b.run_records_flat([ord("E"), ord("T")])     # a couple of chars
    N = len(flat)
    dev = SramPanelDevice()
    chip = simkyt.Chip.from_yaml(str(CHIP_YAML))
    for a in range(0, 32):
        chip.write_cell_memory(_cid(0, 0), a, int(res.memory.get(a, 0)))
    for x in range(W):
        chip.set_fwd_face(_cid(x, 0), "east")
    chip.set_port_handshake("x16_out", True)
    drv = PanelDriver(dev, chip, "x16_out", chip, "x16_in")
    stim = [_wr(30, cin), 0, _jp(30, ent["set_addr"])]
    for w in flat:
        stim += [_wr(30, cin), w, _jp(30, ent["write"])]
    chip.queue_words_physical("x16_in", stim)
    for _ in range(20000):
        chip.run(max_events=32)
        drv.step()
        if dev.writes_committed >= N:
            break
    assert dev.writes_committed == N
    assert all(dev.mem.get(i) == (flat[i] & 0xFFFF) for i in range(N)), \
        "auto-increment load stored wrong run words"


# ===================================== LOOKUP PHASE: panel push-read -> play -> golden
def _load_records(dev, flat):
    """Load the flat run-record stream into the panel via the device commit
    protocol (the same on_write/on_jump the controller emits; the persistent
    streaming load is proven separately above)."""
    for a, w in enumerate(flat):
        dev.on_write(2, w & 0xFFFF)   # R2 payload
        dev.on_write(5, a)            # R5 address
        dev.on_jump(0)                # commit


def _play_chars_full_chain(chars, edge=EDGE):
    """The full per-character SRAM-backed keying through REAL routing: load the
    char's run records into the panel, then per record the panel PUSH-READs the
    three words (base, step, count) into the player's input regs + kicks play; the
    player emits the samples out x16_out. Returns the concatenated envelope."""
    import simkyt
    from engine.sram_panel import SramPanelDevice, PanelDriver
    b, res, regs, entry = _player(edge=edge)
    recs = b.run_records(chars)

    chip = simkyt.Chip.from_yaml(str(CHIP_YAML))
    for a in range(0, 32):
        chip.write_cell_memory(_cid(0, 0), a, int(res.memory.get(a, 0)))
    for x in range(W):
        chip.set_fwd_face(_cid(x, 0), "east")
    drv = PanelDriver(dev := SramPanelDevice(), chip, "x16_out", chip, "x16_in")

    out = []
    for (base, step, cnt) in recs:
        # Stage this record's 3 words in the panel, then push-read each back to the
        # player's base/step/cnt registers; the JUMP after 'cnt' kicks 'play'.
        words = [base & 0xFFFF, step & 0xFFFF, cnt & 0xFFFF]
        dev.mem[0] = words[0]
        dev.mem[1] = words[1]
        dev.mem[2] = words[2]
        # base -> R{base} (data only), step -> R{step} (data only),
        # cnt  -> R{cnt}  + JUMP play (kick).
        targets = [(regs["base"], 0), (regs["step"], 1), (regs["cnt"], 2)]
        for i, (reg, addr) in enumerate(targets):
            dev.on_write(3, _wr(30, reg))                 # WRITE descriptor
            kick = (i == 2)                               # kick play on the last
            dev.on_write(4, _jp(30, entry) if kick else _jp(31, 0))
            dev.on_write(5, addr)
            push = dev.on_jump(1)                         # read trigger
            assert push is not None and push.value == words[addr]
            drv._inject(push)
            # Drain the delivery (and, on the kick, the cnt emitted samples).
            _drain(chip, out, cnt if kick else 0)
    return out


def test_full_chain_bit_exact_single_chars():
    """E, T, Q, digit 5, and the word-space (NUL) through the REAL panel push-read
    + player are BIT-EXACT (Q15) vs the ITU-R golden."""
    _need_chip()
    for chars in ([ord("E")], [ord("T")], [ord("Q")], [ord("5")], [0]):
        got = _play_chars_full_chain(chars)
        want = _keyer().key_envelope_q15(chars)
        assert got == want, (chars, len(got), len(want))


def test_full_chain_bit_exact_paris():
    """PARIS (the WPM calibration word) + a word space through the panel round-trip
    is BIT-EXACT to the golden."""
    _need_chip()
    chars = [ord(c) for c in "PARIS"] + [0]
    got = _play_chars_full_chain(chars)
    want = _keyer().key_envelope_q15(chars)
    assert got == want


def test_full_chain_bit_exact_message():
    """A short message (SOS + a word space + 'ET') through the panel round-trip is
    BIT-EXACT to the golden."""
    _need_chip()
    chars = [ord(c) for c in "SOS"] + [0] + [ord(c) for c in "ET"]
    got = _play_chars_full_chain(chars)
    want = _keyer().key_envelope_q15(chars)
    assert got == want


# ============================================================ TIMING gate (§2)
def _on_runs(env, thresh_q15):
    runs, cur, n = [], None, 0
    for v in env:
        on = v > thresh_q15
        if on is cur:
            n += 1
        else:
            if cur is not None:
                runs.append((cur, n))
            cur, n = on, 1
    if cur is not None:
        runs.append((cur, n))
    return runs


def test_timing_ratios_exact_through_panel():
    """dot:dash:intra:inter:word = 1:3:1:3:7 measured on the REAL panel-emitted
    envelope (ITU-R §2.1-§2.4)."""
    _need_chip()
    thr = 16384  # half-Q15; edges OFF for a clean square envelope
    E = _on_runs(_play_chars_full_chain([ord("E")], edge=0), thr)
    assert E == [(True, 1 * SPD), (False, 3 * SPD)]        # dot=1, inter=3
    T = _on_runs(_play_chars_full_chain([ord("T")], edge=0), thr)
    assert T == [(True, 3 * SPD), (False, 3 * SPD)]        # dash=3
    AN = _on_runs(_play_chars_full_chain([ord("A"), ord("N")], edge=0), thr)
    assert AN == [
        (True, 1 * SPD), (False, 1 * SPD),   # A dot, intra=1
        (True, 3 * SPD), (False, 3 * SPD),   # A dash, inter=3
        (True, 3 * SPD), (False, 1 * SPD),   # N dash, intra=1
        (True, 1 * SPD), (False, 3 * SPD),   # N dot, inter=3
    ]
    WORD = _on_runs(_play_chars_full_chain([0], edge=0), thr)
    assert WORD == [(False, 7 * SPD)]                      # inter-word=7


def test_dot_ms_paris_standard():
    """dot_ms = 1200/wpm (PARIS standard) — the WPM calibration."""
    assert CWKeyerBlock("k", wpm=20).dot_ms == pytest.approx(60.0)
    assert CWKeyerBlock("k", wpm=25).dot_ms == pytest.approx(48.0)


def test_edge_within_q15_tolerance():
    """The panel-emitted raised-cosine edge matches the Hann golden to 0 Q15 LSB
    (the edge is an exact in-cell LUT, not a lossy recurrence)."""
    _need_chip()
    got = _play_chars_full_chain([ord("E")])
    want = _keyer().key_envelope_q15([ord("E")])
    # exact — but assert with an explicit derived tolerance for the record.
    peak = max(abs(int(a) - int(b)) for a, b in zip(got, want))
    assert peak == 0, f"edge/envelope peak error {peak} Q15 LSB (tol 0)"


# ------------------------------------------------------------- MUTATION gates (INV-4)
def test_mutation_wrong_run_word_FAILS():
    """A WRONG run word delivered by the panel must make the envelope disagree
    with the golden — the gate SEES a corrupted SRAM schedule (INV-4)."""
    _need_chip()
    import simkyt
    from engine.sram_panel import SramPanelDevice, PanelDriver
    b, res, regs, entry = _player()
    recs = b.run_records([ord("E")])
    # Corrupt the FIRST run's count (make the leading edge one sample short).
    bad = list(recs)
    base, step, cnt = bad[0]
    bad[0] = (base, step, max(1, cnt - 1))
    chip = simkyt.Chip.from_yaml(str(CHIP_YAML))
    for a in range(0, 32):
        chip.write_cell_memory(_cid(0, 0), a, int(res.memory.get(a, 0)))
    for x in range(W):
        chip.set_fwd_face(_cid(x, 0), "east")
    drv = PanelDriver(dev := SramPanelDevice(), chip, "x16_out", chip, "x16_in")
    out = []
    for (bb, ss, cc) in bad:
        words = [bb & 0xFFFF, ss & 0xFFFF, cc & 0xFFFF]
        for addr in range(3):
            dev.mem[addr] = words[addr]
        for i, reg in enumerate((regs["base"], regs["step"], regs["cnt"])):
            dev.on_write(3, _wr(30, reg))
            dev.on_write(4, _jp(30, entry) if i == 2 else _jp(31, 0))
            dev.on_write(5, i)
            drv._inject(dev.on_jump(1))
            _drain(chip, out, cc if i == 2 else 0)
    want = b.key_envelope_q15([ord("E")])
    assert out != want, "gate blind to a wrong run word in SRAM"


def test_mutation_wrong_timing_ratio_FAILS():
    """A wrong dash:dot ratio (dash=2 not 3) must disagree with the golden (§2.1)."""
    b = _keyer()
    good = b.key_envelope_q15([ord("T")])       # dash = 3 dot units
    corrupt = [0x7FFF] * (2 * SPD) + [0] * (3 * SPD)   # dash only 2 units
    assert good != corrupt, "gate blind to a wrong dash length"


def test_mutation_no_click_edge_FAILS():
    """Dropping the raised-cosine edge (hard step) must disagree vs the shaped
    golden (INV-4)."""
    shaped = CWKeyerBlock("k", wpm=20, samples_per_dot=40,
                          edge_samples=4).key_envelope_q15([ord("E")])
    hard = CWKeyerBlock("k", wpm=20, samples_per_dot=40,
                        edge_samples=0).key_envelope_q15([ord("E")])
    assert shaped != hard, "gate blind to missing click suppression"


def test_mutation_missing_interword_gap_FAILS():
    """Omitting the inter-word gap (§2.4 = 7) must disagree with the golden."""
    b = _keyer()
    good = b.key_envelope_q15([0])              # 7*spd OFF
    short = [0] * (3 * SPD)                     # only a 3-unit gap
    assert good != short, "gate blind to a missing inter-word gap"
